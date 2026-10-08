"""Exercise the actual worker message boundary, including a trusted subprocess host.

Test code itself is trusted setup/inspection code; the simulated worker can only
send JSON bytes and receive JSON bytes. No evaluator or database object is granted.
"""
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from job_applier.database.sqlite import SQLiteDatabase
from job_applier.interfaces.worker import (
    COMMANDS, MAX_MESSAGE_BYTES, TrustedWorkerDispatcher, WorkerScope, serve_worker_stream,
)
from job_applier.models import (
    Application, ApplicationStatus, ApprovedAnswer, ApprovalState, AuditEvent,
    CandidateProfile, Job, ReadinessRequirements, Resume,
)
from job_applier.services.foundation import FoundationService
from job_applier.services.human import LocalHumanOperations


EXPECTED_COMMANDS = {
    'get_candidate_profile', 'list_jobs', 'list_resumes', 'list_answers',
    'list_applications', 'get_application', 'list_unresolved', 'propose_answer',
    'propose_requirements', 'create_application', 'assign_resume',
    'shortlist_application', 'prepare_application',
}


class WorkerInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = SQLiteDatabase(self.root / 'private.sqlite3')
        self.db.initialize()
        # Only trusted fixture setup receives host objects.
        self.trusted_service = FoundationService(self.db)
        self.owner = LocalHumanOperations(self.db, owner_actor_id='fixture-owner')
        self.candidate = CandidateProfile(
            personal_information={'full_name': 'Fixture Person', 'email': 'fixture@example.invalid',
                                  'phone': 'fixture-phone', 'location': 'Fixture City'},
            education=[{'degree': 'Fixture degree'}], target_roles=['Fixture role'],
            employment_preferences=['full_time'], work_authorization={'status': 'authorized'},
            sponsorship_information={'required': False},
        )
        self.other_candidate = CandidateProfile()
        self.job = Job(title='Fixture role', company='Fixture Company', source='local_fixture', source_job_id='1')
        self.resume_file = self.root / 'private-resume.txt'
        self.resume_file.write_text('Synthetic local test fixture')
        self.resume = Resume(candidate_id=self.candidate.id, name='Fixture resume',
                             file_path=str(self.resume_file), job_family='fixture', version='1')
        self.other_resume = Resume(candidate_id=self.other_candidate.id, name='Other resume',
                                   file_path=str(self.resume_file), job_family='fixture', version='1')
        for record in (self.candidate, self.other_candidate, self.job, self.resume, self.other_resume):
            self.assertFalse(self.trusted_service.save_library_record(record).blocked)
        self.app_id = self.trusted_service.create_application(self.candidate.id, self.job.id, self.resume.id).entity_id
        self.other_app_id = self.trusted_service.create_application(self.other_candidate.id, self.job.id, self.other_resume.id).entity_id
        self.scope = WorkerScope('fixture-worker', self.candidate.id)
        self.dispatcher = TrustedWorkerDispatcher(self.db, self.scope)

    def request(self, command, arguments=None, **extra):
        message = {'command': command, 'arguments': arguments or {}, **extra}
        wire = json.dumps(message).encode('utf-8')
        response = self.dispatcher.handle(wire)
        self.assertIs(type(response), bytes)
        return json.loads(response)

    def record(self, model, identity):
        with self.db.transaction() as repo:
            return repo.get(model, identity)

    def events(self, **filters):
        with self.db.transaction() as repo:
            return repo.list(AuditEvent, limit=1000, **filters)

    def propose(self):
        response = self.request('propose_answer', {'question': 'Fixture question?', 'answer': 'Proposed answer', 'category': 'fixture'})
        self.assertEqual(response['status'], 'ok', response)
        req = self.requirements()
        req.questions = [{'question_id':'q-0','text':'Fixture question?','context':{}}]
        self.assertFalse(self.trusted_service.save_requirements(req, update=True).blocked)
        return self.record(ApprovedAnswer, response['entity_id'])

    def _answer_requirements_id(self):
        with self.db.transaction() as repo:
            return repo.list(ReadinessRequirements,application_id=self.app_id)[0].id

    def requirements(self):
        response = self.request('propose_requirements', {
            'application_id': self.app_id, 'required_candidate_fields': [],
            'questions': [], 'context': {'form_id': 'fixture-form', 'form_version': '1'},
        })
        self.assertEqual(response['status'], 'ok', response)
        return self.record(ReadinessRequirements, response['entity_id'])

    def test_allowlist_is_exact_and_not_derived_from_service_methods(self):
        self.assertEqual(COMMANDS, EXPECTED_COMMANDS)

    def test_exact_human_constructor_bypass_is_rejected(self):
        answer = self.propose()
        for command in ('LocalHumanOperations', 'job_applier.services.human.LocalHumanOperations',
                        'construct', 'instantiate', '__import__', 'approve_answer'):
            with self.subTest(command=command):
                response = self.request(command, {'database': 'worker.database', 'owner_actor_id': 'agent-claiming-owner',
                    'answer_id': answer.id, 'expected_revision': 1, 'reason': 'Self-approval'})
                self.assertEqual(response['status'], 'rejected')
        self.assertEqual(self.record(ApprovedAnswer, answer.id).approval_state, ApprovalState.DRAFT)

    def test_exact_imported_write_capability_bypass_is_rejected(self):
        before = self.record(Application, self.app_id)
        for command in ('_DOMAIN_WRITE', 'transaction', 'repository.update', 'SQLiteRepository',
                        'SQLiteDatabase', 'database._connect', '_run', '_put'):
            with self.subTest(command=command):
                response = self.request(command, {'capability': '_DOMAIN_WRITE', 'application_id': self.app_id,
                                                 'status': 'submitted', 'revision': before.revision + 1})
                self.assertEqual(response['status'], 'rejected')
        self.assertEqual(self.record(Application, self.app_id), before)
        self.assertTrue(self.events(action='worker_interface.reject', result='rejected'))

    def test_all_human_operations_are_unavailable(self):
        commands = ('approve_answer', 'revoke_answer', 'review_requirements', 'resolve_candidate_information',
                    'resolve_unknown_question', 'verify_contact', 'revoke_contact_verification',
                    'approve_outreach', 'reconcile_submission', 'authorize_retry', 'confirm_job_equivalence')
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.request(command)['error'], 'command_not_allowed')

    def test_actor_role_capability_and_database_cannot_be_supplied(self):
        for name, value in (('actor_id', 'owner'), ('actor_type', 'HUMAN'), ('owner_actor_id', 'owner'),
                            ('human', {'actor_id': 'owner'}), ('capability', '_DOMAIN_WRITE'),
                            ('database_path', str(self.db.path)), ('method', 'approve_answer')):
            with self.subTest(field=name):
                self.assertEqual(self.request('get_candidate_profile', **{name: value})['status'], 'rejected')
                self.assertEqual(self.request('get_candidate_profile', {name: value})['status'], 'rejected')

    def test_approval_fields_cannot_be_smuggled_into_answer_creation(self):
        for name, value in (('approval_state', 'approved'), ('approval', {'actor_type': 'HUMAN'}),
                            ('source', 'human_owner'), ('candidate_id', self.other_candidate.id),
                            ('content_revision', 99), ('id', 'chosen-id')):
            args = {'question': 'Q', 'answer': 'A', 'category': 'fixture', name: value}
            self.assertEqual(self.request('propose_answer', args)['status'], 'rejected')
        with self.db.transaction() as repo:
            self.assertEqual(repo.count(ApprovedAnswer), 0)

    def test_answer_edit_is_draft_and_invalidates_existing_approval(self):
        answer = self.propose()
        self.assertFalse(self.owner.approve_answer(answer.id, requirements_id=self._answer_requirements_id(), question_id='q-0', expected_revision=1, reason='Owner reviewed').blocked)
        result = self.request('propose_answer', {'answer_id': answer.id, 'expected_revision': 2,
            'question': answer.question, 'answer': 'Edited proposal', 'category': answer.category})
        self.assertEqual(result['status'], 'ok', result)
        current = self.record(ApprovedAnswer, answer.id)
        self.assertEqual(current.approval_state, ApprovalState.DRAFT)
        self.assertIsNone(current.approval)
        self.assertEqual(current.source, 'worker_proposal')

    def test_stale_answer_edit_is_rejected(self):
        answer = self.propose()
        result = self.request('propose_answer', {'answer_id': answer.id, 'expected_revision': 99,
            'question': answer.question, 'answer': 'Stale edit', 'category': answer.category})
        self.assertEqual(result['error'], 'stale_revision')
        self.assertEqual(self.record(ApprovedAnswer, answer.id).revision, 1)

    def test_answer_edit_cannot_cross_candidate_scope(self):
        answer = ApprovedAnswer(candidate_id=self.other_candidate.id, question='Other?', answer='Other', category='fixture', source='fixture')
        self.assertFalse(self.trusted_service.save_library_record(answer).blocked)
        result = self.request('propose_answer', {'answer_id': answer.id, 'expected_revision': 1,
            'question': 'Changed', 'answer': 'Changed', 'category': 'fixture'})
        self.assertEqual(result['error'], 'access_denied')

    def test_requirements_are_proposals_not_reviews(self):
        req = self.requirements()
        self.assertIsNone(req.approval)
        self.assertEqual(self.request('shortlist_application', {'application_id': self.app_id})['status'], 'ok')
        result = self.request('prepare_application', {'application_id': self.app_id})
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('requirements_not_reviewed_or_stale', result['reasons'])

    def test_requirements_cannot_smuggle_human_review(self):
        base = {'application_id': self.app_id, 'required_candidate_fields': [], 'questions': [], 'context': {}}
        for name, value in (('approval', {'actor_type': 'HUMAN'}), ('requirements_reviewed', True),
                            ('candidate_revision', 1), ('candidate_id', self.other_candidate.id)):
            self.assertEqual(self.request('propose_requirements', {**base, name: value})['status'], 'rejected')

    def test_requirement_edit_revokes_review(self):
        req = self.requirements()
        self.assertFalse(self.owner.review_requirements(req.id, expected_revision=1, reason='Owner review').blocked)
        result = self.request('propose_requirements', {'application_id': self.app_id,
            'required_candidate_fields': [], 'questions': [{'text': 'New question?'}],
            'context': {}, 'expected_revision': 2})
        self.assertEqual(result['status'], 'ok', result)
        self.assertIsNone(self.record(ReadinessRequirements, req.id).approval)

    def test_allowed_local_workflow_still_requires_owner_review(self):
        job = Job(title='Second local role', company='Other Fixture Company', source='local_fixture', source_job_id='2')
        self.assertFalse(self.trusted_service.save_library_record(job).blocked)
        result = self.request('create_application', {'job_id': job.id})
        self.assertEqual(result['status'], 'ok', result)
        app_id = result['entity_id']
        self.assertEqual(self.request('assign_resume', {'application_id': app_id, 'resume_id': self.resume.id})['status'], 'ok')
        self.assertEqual(self.request('shortlist_application', {'application_id': app_id})['status'], 'ok')
        result = self.request('propose_requirements', {'application_id': app_id,
            'required_candidate_fields': [], 'questions': [], 'context': {'form_id':'fixture-form','form_version':'1'}})
        self.assertEqual(result['status'], 'ok', result)
        req = self.record(ReadinessRequirements, result['entity_id'])
        self.assertEqual(self.request('prepare_application', {'application_id': app_id})['status'], 'blocked')
        self.assertFalse(self.owner.review_requirements(req.id, expected_revision=1, reason='Owner reviewed form').blocked)
        self.assertEqual(self.request('prepare_application', {'application_id': app_id})['status'], 'ok')
        self.assertEqual(self.record(Application, app_id).status, ApplicationStatus.READY_TO_APPLY)

    def test_unknown_question_is_visible_as_scoped_data_only(self):
        self.request('shortlist_application', {'application_id': self.app_id})
        result = self.request('propose_requirements', {'application_id': self.app_id,
            'required_candidate_fields': [], 'questions': [{'question_id':'q-0','text': 'Unapproved?', 'context': {'field': 'q1'}}], 'context': {'form_id':'fixture-form','form_version':'1'}})
        req = self.record(ReadinessRequirements, result['entity_id'])
        self.assertFalse(self.owner.review_requirements(req.id, expected_revision=1, reason='Owner reviewed').blocked)
        self.assertEqual(self.request('prepare_application', {'application_id': self.app_id})['status'], 'blocked')
        result = self.request('list_unresolved', {'application_id': self.app_id})
        self.assertEqual(result['data'][0]['question_text'], 'Unapproved?')
        self.assertEqual(result['data'][0]['context']['question_context'], {'field': 'q1'})

    def test_scoped_application_reads_and_mutations(self):
        for command in ('get_application', 'list_unresolved', 'shortlist_application', 'prepare_application', 'propose_requirements'):
            args = {'application_id': self.other_app_id}
            if command == 'propose_requirements':
                args.update(required_candidate_fields=[], questions=[], context={})
            self.assertEqual(self.request(command, args)['error'], 'access_denied')
        result = self.request('assign_resume', {'application_id': self.app_id, 'resume_id': self.other_resume.id})
        self.assertEqual(result['error'], 'access_denied')
        result = self.request('create_application', {'job_id': self.job.id, 'resume_id': self.other_resume.id})
        self.assertEqual(result['error'], 'access_denied')

    def test_scoped_lists_do_not_return_other_candidate_records(self):
        apps = self.request('list_applications')['data']
        self.assertEqual([app['id'] for app in apps], [self.app_id])
        resumes = self.request('list_resumes')['data']
        self.assertEqual([resume['id'] for resume in resumes], [self.resume.id])
        self.assertEqual(self.request('get_candidate_profile')['data']['id'], self.candidate.id)

    def test_no_generic_lifecycle_write_or_submit_command(self):
        for command in ('transition_application', 'save_library_record', 'update', 'add', 'submit', 'request_external_action'):
            result = self.request(command, {'application_id': self.app_id, 'status': 'submitted'})
            self.assertEqual(result['status'], 'rejected')
        result = self.request('shortlist_application', {'application_id': self.app_id, 'target': 'submitted'})
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.record(Application, self.app_id).status, ApplicationStatus.DISCOVERED)

    def test_sql_python_shell_and_file_commands_are_rejected(self):
        marker = self.root / 'must-not-exist'
        code = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
        for command in ('eval', 'exec', 'python', 'shell', 'open', 'read_file', 'write_file', 'sql', 'execute', 'import_module'):
            result = self.request(command, {'code': code, 'sql': 'DELETE FROM audit_logs', 'path': str(self.db.path)})
            self.assertEqual(result['status'], 'rejected')
        self.assertFalse(marker.exists())

    def test_code_inside_proposal_is_inert_text(self):
        marker = self.root / 'must-not-exist'
        code = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
        result = self.request('propose_answer', {'question': 'Q?', 'answer': code, 'category': 'fixture'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.record(ApprovedAnswer, result['entity_id']).answer, code)
        self.assertFalse(marker.exists())

    def test_arbitrary_python_objects_are_not_deserialized_or_called(self):
        class Trap:
            def __str__(self):
                raise AssertionError('Object execution is forbidden')
            def __reduce__(self):
                raise AssertionError('Pickle is forbidden')
        result = json.loads(self.dispatcher.handle(Trap()))
        self.assertEqual(result['status'], 'rejected')

    def test_pickle_bytes_batch_and_dynamic_method_envelopes_rejected(self):
        for message in (b'\x80\x04pickle payload', b'[]', b'null', b'"approve_answer"',
                        b'{"command":"get_candidate_profile","arguments":{},"__class__":"LocalHumanOperations"}'):
            self.assertEqual(json.loads(self.dispatcher.handle(message))['status'], 'rejected')

    def test_duplicate_json_keys_nan_and_excessive_nesting_rejected(self):
        messages = [b'{"command":"get_candidate_profile","command":"approve_answer","arguments":{}}',
                    b'{"command":"get_candidate_profile","arguments":{"x":NaN}}',
                    json.dumps({'command': 'get_candidate_profile', 'arguments': {'x': [[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]}}).encode()]
        for message in messages:
            self.assertEqual(json.loads(self.dispatcher.handle(message))['status'], 'rejected')

    def test_pagination_limits_and_boolean_integers_are_enforced(self):
        for args in ({'limit': 101}, {'limit': True}, {'offset': -1}, {'offset': False}, {'filters': {'candidate_id': self.other_candidate.id}}):
            self.assertEqual(self.request('list_applications', args)['status'], 'rejected')

    def test_projection_has_no_paths_capabilities_or_human_metadata(self):
        answer = self.propose()
        self.assertFalse(self.owner.approve_answer(answer.id, requirements_id=self._answer_requirements_id(), question_id='q-0', expected_revision=1, reason='Owner').blocked)
        results = [self.request('list_resumes'), self.request('list_answers'), self.request('get_application', {'application_id': self.app_id})]
        wire = json.dumps(results)
        for secret in (str(self.db.path), str(self.resume_file), '_DOMAIN_WRITE', 'LocalHumanOperations'):
            self.assertNotIn(secret, wire)
        self.assertNotIn('file_path', results[0]['data'][0])
        self.assertNotIn('approval', results[1]['data'][0])

    def test_response_mutation_cannot_modify_stored_records(self):
        result = self.request('get_application', {'application_id': self.app_id})
        result['data']['status'] = 'submitted'
        self.assertEqual(self.record(Application, self.app_id).status, ApplicationStatus.DISCOVERED)

    def test_rejected_requests_are_audited_as_host_assigned_worker(self):
        result = self.request('approve_answer', {'actor_type': 'HUMAN'})
        events = self.events(correlation_id=result['correlation_id'])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].actor_type, 'WORKER')
        self.assertEqual(events[0].actor_id, self.scope.actor_id)
        self.assertEqual(events[0].result, 'rejected')

    def test_successful_mutation_is_correlated_with_domain_audit(self):
        result = self.request('propose_answer', {'question': 'Q?', 'answer': 'A', 'category': 'fixture'})
        events = self.events(correlation_id=result['correlation_id'])
        self.assertTrue(any(event.entity == 'ApprovedAnswer' for event in events))
        self.assertTrue(all(event.actor_type == 'WORKER' for event in events))

    def test_read_request_is_audited(self):
        result = self.request('list_jobs')
        events = self.events(correlation_id=result['correlation_id'])
        self.assertEqual(events[0].action, 'worker_interface.list_jobs')
        self.assertEqual(events[0].result, 'success')

    def test_storage_failure_never_returns_paths_or_success(self):
        with patch.object(self.db, 'transaction', side_effect=RuntimeError('secret/path/to/db')):
            response = self.request('get_candidate_profile')
        self.assertEqual(response['status'], 'failed')
        self.assertNotIn('secret', json.dumps(response))

    def test_rejection_audit_failure_fails_closed(self):
        with patch.object(self.db, 'transaction', side_effect=RuntimeError('disk failed')):
            result = self.request('approve_answer')
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['error'], 'audit_unavailable')

    def test_oversized_message_is_drained_before_next_command(self):
        reader = BytesIO(b'x' * (MAX_MESSAGE_BYTES + 100) + b'\n' +
                         b'{"command":"list_jobs","arguments":{}}\n')
        writer = BytesIO()
        serve_worker_stream(self.db, self.scope, reader, writer)
        responses = [json.loads(line) for line in writer.getvalue().splitlines()]
        self.assertEqual([r['status'] for r in responses], ['rejected', 'ok'])

    def test_actual_subprocess_transport_never_exports_trusted_objects(self):
        # The trusted launcher selects the executable, database, actor and scope.
        # The simulated worker controls ONLY the message bytes passed as input.
        environment = dict(os.environ)
        source = str(Path(__file__).resolve().parents[1] / 'src')
        environment['PYTHONPATH'] = source
        environment['JOB_AGENT_DATABASE_PATH'] = str(self.db.path)
        messages = [
            {'command': 'get_candidate_profile', 'arguments': {}},
            {'command': 'LocalHumanOperations', 'arguments': {'owner_actor_id': 'self'}},
            {'command': 'import_module', 'arguments': {'module': 'job_applier.database', 'name': '_DOMAIN_WRITE'}},
            {'command': 'repository.update', 'arguments': {'application_id': self.app_id, 'status': 'submitted'}},
            {'command': 'shortlist_application', 'arguments': {'application_id': self.app_id}},
        ]
        completed = subprocess.run(
            [sys.executable, '-B', '-m', 'job_applier', 'worker-interface',
             '--worker-id', self.scope.actor_id, '--candidate-id', self.candidate.id],
            input=b'\n'.join(json.dumps(m).encode() for m in messages) + b'\n',
            capture_output=True, env=environment, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([r['status'] for r in responses], ['ok', 'rejected', 'rejected', 'rejected', 'ok'])
        self.assertEqual(self.record(Application, self.app_id).status, ApplicationStatus.SHORTLISTED)
        self.assertNotIn(str(self.db.path).encode(), completed.stdout)


if __name__ == '__main__':
    unittest.main()
