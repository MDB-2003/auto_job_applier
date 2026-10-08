"""Allowlisted JSON-line interface for workers without Python/OS capabilities.

Run this in the trusted host process. The worker receives only a byte-message
channel, never an instance of this module's classes, database, or domain services.
No pickle, dynamic import, eval, shell, arbitrary method dispatch, or human route
is available through the protocol. This is not an OS sandbox.
"""
from dataclasses import dataclass
import json
from typing import BinaryIO, Any

from job_applier.database import Database, NotFoundError, _DOMAIN_WRITE
from job_applier.models import (
    Application, ApplicationStatus, ApprovedAnswer, ApprovalState, AuditEvent,
    CandidateProfile, Job, ReadinessRequirements, Resume, UnresolvedInformation,
    new_id,
)
from job_applier.services.foundation import FoundationService

MAX_MESSAGE_BYTES = 65_536
MAX_DEPTH = 16
MAX_PAGE = 100
PROTOCOL_VERSION = 1

# Explicit field allowlists prevent the API from growing when domain records grow.
CANDIDATE_FIELDS = ('id', 'revision', 'personal_information', 'education', 'skills',
    'experience', 'target_roles', 'preferred_locations', 'work_authorization',
    'sponsorship_information', 'employment_preferences', 'conflicts')
JOB_FIELDS = ('id', 'revision', 'canonical_id', 'title', 'company', 'source',
    'source_job_id', 'canonical_url', 'requisition_id', 'location', 'work_mode',
    'employment_type')
RESUME_FIELDS = ('id', 'revision', 'name', 'job_family', 'version', 'active')
ANSWER_FIELDS = ('id', 'revision', 'question', 'answer', 'category', 'notes',
    'approval_state', 'content_revision')
APPLICATION_FIELDS = ('id', 'revision', 'candidate_id', 'job_id', 'canonical_id',
    'resume_id', 'resume_revision', 'status', 'block_reasons', 'latest_attempt_id')
UNRESOLVED_FIELDS = ('id', 'revision', 'application_id', 'job_id', 'question_text',
    'field_path', 'context', 'block_reason', 'resolved', 'created_at')

COMMANDS = frozenset({
    'get_candidate_profile', 'list_jobs', 'list_resumes', 'list_answers',
    'list_applications', 'get_application', 'list_unresolved', 'propose_answer',
    'propose_requirements', 'create_application', 'assign_resume',
    'shortlist_application', 'prepare_application',
})


class InvalidRequest(ValueError):
    """A stable public error code, never a traceback or arbitrary exception text."""


@dataclass(frozen=True)
class WorkerScope:
    """Trusted launch configuration; the worker cannot select or change it."""
    actor_id: str
    candidate_id: str

    def __post_init__(self):
        for value in (self.actor_id, self.candidate_id):
            if type(value) is not str or not value.strip() or len(value) > 128:
                raise ValueError('invalid_trusted_worker_scope')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRequest('duplicate_json_key')
        result[key] = value
    return result


def _no_constant(_value):
    raise InvalidRequest('invalid_json_number')


def _depth(value, level=0):
    if level > MAX_DEPTH:
        raise InvalidRequest('request_too_deep')
    if isinstance(value, dict):
        for child in value.values():
            _depth(child, level + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, level + 1)


def _keys(args, required=(), optional=()):
    if type(args) is not dict or set(args) - set(required) - set(optional) or set(required) - set(args):
        raise InvalidRequest('invalid_arguments')


def _string(value):
    if type(value) is not str or not value.strip():
        raise InvalidRequest('invalid_arguments')
    return value


def _revision(value):
    if type(value) is not int or value < 1:
        raise InvalidRequest('invalid_revision')
    return value


def _page(args, required=()):
    _keys(args, required, ('limit', 'offset'))
    limit, offset = args.get('limit', 50), args.get('offset', 0)
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE or type(offset) is not int or offset < 0:
        raise InvalidRequest('invalid_pagination')
    return {'limit': limit, 'offset': offset}


def _project(record, names):
    return {name: getattr(record, name) for name in names}


class TrustedWorkerDispatcher:
    """Trusted-host implementation. Only JSON requests/responses cross the boundary."""
    def __init__(self, database: Database, scope: WorkerScope):
        self._database = database
        self._scope = scope
        # Fail at trusted startup if the assigned candidate does not exist.
        with database.transaction() as repo:
            repo.get(CandidateProfile, scope.candidate_id)

    def _audit(self, correlation_id, command, result, error=None):
        with self._database.transaction(capability=_DOMAIN_WRITE) as repo:
            repo.add(AuditEvent(
                action='worker_interface.' + command,
                entity='WorkerCommand', entity_id=self._scope.candidate_id,
                previous_state=None, new_state=None, result=result, error=error,
                source='local_worker_interface', actor_id=self._scope.actor_id,
                actor_type='WORKER', correlation_id=correlation_id,
                metadata={'protocol_version': PROTOCOL_VERSION},
            ))

    def _application(self, application_id):
        with self._database.transaction() as repo:
            app = repo.get(Application, _string(application_id))
        if app.candidate_id != self._scope.candidate_id:
            raise InvalidRequest('access_denied')
        return app

    def _resume(self, resume_id):
        with self._database.transaction() as repo:
            resume = repo.get(Resume, _string(resume_id))
        if resume.candidate_id != self._scope.candidate_id:
            raise InvalidRequest('access_denied')
        return resume

    def handle(self, message: bytes) -> bytes:
        """Exactly one bounded JSON object in; detached JSON bytes out."""
        correlation_id = new_id()
        command = 'reject'
        try:
            if type(message) is not bytes or len(message) > MAX_MESSAGE_BYTES:
                raise InvalidRequest('invalid_message_size_or_type')
            request = json.loads(message.decode('utf-8'), object_pairs_hook=_object,
                                 parse_constant=_no_constant)
            _depth(request)
            _keys(request, ('command', 'arguments'))
            if type(request['command']) is not str or request['command'] not in COMMANDS:
                raise InvalidRequest('command_not_allowed')
            command = request['command']
            args = request['arguments']
            if type(args) is not dict:
                raise InvalidRequest('invalid_arguments')
            service = FoundationService(self._database, actor_id=self._scope.actor_id,
                                        correlation_id=correlation_id)
            payload, decision = self._dispatch(command, args, service)
            if decision is not None:
                response = {'status': 'blocked' if decision.blocked else 'ok',
                            'entity_id': decision.entity_id, 'reasons': list(decision.reasons),
                            'answers': decision.answers}
                # Domain command audit already committed before this response exists.
            else:
                response = {'status': 'ok', 'data': payload}
                self._audit(correlation_id, command, 'success')
        except (InvalidRequest, ValueError, TypeError, UnicodeError, RecursionError, LookupError) as exc:
            code = str(exc) if isinstance(exc, InvalidRequest) else 'invalid_request_or_record'
            try:
                self._audit(correlation_id, command, 'rejected', code)
            except Exception:
                code = 'audit_unavailable'
            response = {'status': 'rejected', 'error': code}
        except Exception:
            # Service operational failures are already audited when storage permits.
            # Do not disclose paths, SQL, exception objects, or Python tracebacks.
            response = {'status': 'failed', 'error': 'local_operation_failed'}
        response['protocol_version'] = PROTOCOL_VERSION
        response['correlation_id'] = correlation_id
        return json.dumps(response, allow_nan=False, sort_keys=True).encode('utf-8')

    def _dispatch(self, command: str, args: dict, service: FoundationService):
        # Fixed branches, never getattr(service, user_input) or a generic invocation.
        if command == 'get_candidate_profile':
            _keys(args)
            with self._database.transaction() as repo:
                return _project(repo.get(CandidateProfile, self._scope.candidate_id), CANDIDATE_FIELDS), None

        if command in {'list_jobs', 'list_resumes', 'list_answers', 'list_applications'}:
            page = _page(args)
            choices = {
                'list_jobs': (Job, JOB_FIELDS, {}),
                'list_resumes': (Resume, RESUME_FIELDS, {'candidate_id': self._scope.candidate_id}),
                'list_answers': (ApprovedAnswer, ANSWER_FIELDS, {'candidate_id': self._scope.candidate_id}),
                'list_applications': (Application, APPLICATION_FIELDS, {'candidate_id': self._scope.candidate_id}),
            }
            model, names, filters = choices[command]
            with self._database.transaction() as repo:
                return [_project(row, names) for row in repo.list(model, **page, **filters)], None

        if command == 'get_application':
            _keys(args, ('application_id',))
            return _project(self._application(args['application_id']), APPLICATION_FIELDS), None

        if command == 'list_unresolved':
            page = _page(args, ('application_id',))
            app = self._application(args['application_id'])
            with self._database.transaction() as repo:
                return [_project(row, UNRESOLVED_FIELDS) for row in repo.list(
                    UnresolvedInformation, application_id=app.id, **page)], None

        if command == 'propose_answer':
            _keys(args, ('question', 'answer', 'category'), ('notes', 'answer_id', 'expected_revision'))
            for name in ('question', 'answer', 'category'):
                _string(args[name])
            if 'notes' in args and type(args['notes']) is not str:
                raise InvalidRequest('invalid_arguments')
            update = 'answer_id' in args
            if update != ('expected_revision' in args):
                raise InvalidRequest('revision_required_for_edit')
            if update:
                with self._database.transaction() as repo:
                    answer = repo.get(ApprovedAnswer, _string(args['answer_id']))
                if answer.candidate_id != self._scope.candidate_id:
                    raise InvalidRequest('access_denied')
                if answer.revision != _revision(args['expected_revision']):
                    raise InvalidRequest('stale_revision')
                answer.question = args['question']
                answer.answer = args['answer']
                answer.category = args['category']
                answer.notes = args.get('notes', '')
                answer.source = 'worker_proposal'
                answer.approval_state = ApprovalState.DRAFT
                answer.approval = None
            else:
                answer = ApprovedAnswer(candidate_id=self._scope.candidate_id,
                    question=args['question'], answer=args['answer'], category=args['category'],
                    notes=args.get('notes', ''), source='worker_proposal')
            return None, service.save_library_record(answer, update=update)

        if command == 'propose_requirements':
            _keys(args, ('application_id', 'required_candidate_fields', 'questions', 'context'), ('expected_revision',))
            app = self._application(args['application_id'])
            if type(args['required_candidate_fields']) is not list or any(type(p) is not str for p in args['required_candidate_fields']):
                raise InvalidRequest('invalid_arguments')
            if type(args['questions']) is not list or type(args['context']) is not dict:
                raise InvalidRequest('invalid_arguments')
            for question in args['questions']:
                _keys(question, ('text',), ('context','question_id'))
                _string(question['text'])
                if 'context' in question and type(question['context']) is not dict:
                    raise InvalidRequest('invalid_arguments')
            with self._database.transaction() as repo:
                existing = repo.list(ReadinessRequirements, application_id=app.id, limit=1)
            if existing:
                req = existing[0]
                if req.revision != _revision(args.get('expected_revision')):
                    raise InvalidRequest('stale_revision')
            else:
                if 'expected_revision' in args:
                    raise InvalidRequest('unexpected_revision')
                req = ReadinessRequirements(application_id=app.id, candidate_id=app.candidate_id, job_id=app.job_id)
            req.required_candidate_fields = args['required_candidate_fields']
            req.questions = args['questions']
            req.context = args['context']
            req.approval = None
            return None, service.save_requirements(req, update=bool(existing))

        if command == 'create_application':
            _keys(args, ('job_id',), ('resume_id',))
            job_id = _string(args['job_id'])
            resume_id = self._resume(args['resume_id']).id if args.get('resume_id') is not None else None
            return None, service.create_application(self._scope.candidate_id, job_id, resume_id)

        if command == 'assign_resume':
            _keys(args, ('application_id', 'resume_id'))
            app = self._application(args['application_id'])
            resume = self._resume(args['resume_id'])
            return None, service.assign_resume(app.id, resume.id)

        if command in {'shortlist_application', 'prepare_application'}:
            _keys(args, ('application_id',))
            app = self._application(args['application_id'])
            if command == 'shortlist_application':
                return None, service.transition_application(app.id, ApplicationStatus.SHORTLISTED)
            return None, service.prepare_application(app.id)

        raise InvalidRequest('command_not_allowed')


def serve_worker_stream(database: Database, scope: WorkerScope, reader: BinaryIO, writer: BinaryIO) -> None:
    """Local stdio transport. Trusted launcher owns scope, streams, and database.

    A line is one command, never a batch or executable object. Oversized lines
    are drained completely before accepting another message.
    """
    dispatcher = TrustedWorkerDispatcher(database, scope)
    while True:
        line = reader.readline(MAX_MESSAGE_BYTES + 2)
        if not line:
            return
        oversized = len(line) > MAX_MESSAGE_BYTES
        if oversized:
            while line and not line.endswith(b'\n'):
                line = reader.readline(MAX_MESSAGE_BYTES + 2)
            response = dispatcher.handle(b' ' * (MAX_MESSAGE_BYTES + 1))
        else:
            response = dispatcher.handle(line)
        writer.write(response + b'\n')
        writer.flush()
