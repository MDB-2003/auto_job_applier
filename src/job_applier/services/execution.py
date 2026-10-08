"""Trusted-host, process-local one-shot authorization. No external executor.

Only the host owns this object. Workers receive neither it nor its Python runtime.
Issuance and consumption are separate commits; consumption revalidates all facts.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from threading import RLock

from job_applier.models import (Application, ApplicationStatus as A, ApprovedAnswer,
    CanonicalOpening, ReadinessRequirements, SubmissionAttempt, UnresolvedInformation, new_id, now)
from job_applier.services.context import form_manifest, fingerprint
from job_applier.services.foundation import FoundationService, Decision, Rejected, all_records


# Fresh on process start. Unissued reservations from a dead host are recovery work,
# not silently executable intent in a new host.
_HOST_EPOCH = new_id()

def _host_epoch():
    # A fork inherits Python objects but must not inherit execution authority.
    return f"{_HOST_EPOCH}:{os.getpid()}"

def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def revision_context(repo, service, app):
    """Fresh factual readiness, independent of the separately validated attempt gate."""
    probe=deepcopy(app);probe.latest_attempt_id=None
    reasons,_,_,answers,snapshot=service._check_readiness(repo,probe)
    if reasons: raise Rejected('current_readiness_invalid:' + ','.join(reasons))
    req=repo.list(ReadinessRequirements,application_id=app.id,limit=1)[0]
    canonical=repo.get(CanonicalOpening,app.canonical_id)
    answer_context={identity:{'revision':revision,'content_revision':repo.get(ApprovedAnswer,identity).content_revision,
        'scope':repo.get(ApprovedAnswer,identity).scope} for identity,revision in snapshot['answer_revisions'].items()}
    return {**snapshot,'candidate_id':app.candidate_id,'application_id':app.id,'job_id':app.job_id,
        'canonical_id':app.canonical_id,'canonical_revision':canonical.revision,'resume_id':app.resume_id,
        'requirements_id':req.id,'manifest':form_manifest(repo,req),'answer_context':answer_context,'answers':answers}


def validate_retry_scope(repo, service, app, attempt):
    authorization=attempt.retry_authorization or {}
    if authorization.get('actor_type')!='HUMAN' or not authorization.get('revision_context'):
        raise Rejected('scoped_human_retry_authorization_required')
    if fingerprint(authorization['revision_context']) != fingerprint(revision_context(repo,service,app)):
        raise Rejected('stale_human_retry_authorization')


@dataclass(frozen=True)
class ExecutionContext:
    snapshot_json: str
    def __bool__(self): raise TypeError('ExecutionContext is not authorization')


@dataclass(frozen=True, eq=False)
class SubmissionAuthorization:
    authorization_id: str
    snapshot_json: str
    def __bool__(self): raise TypeError('Use trusted consume_submission; never truthiness')


@dataclass(frozen=True)
class ExecutionStartReceipt:
    attempt_id: str
    authorization_id: str
    snapshot_json: str
    def __bool__(self): raise TypeError('Receipt records local execution-start intent only')


class TrustedExecutionBoundary:
    """Host-configured candidate/application scope; never registered as a worker tool.

    Object identity plus a private registry rejects reconstructed/copied permits.
    Durable issuance metadata prevents replacement after restart or interruption.
    """
    def __init__(self,database,*,candidate_id,application_id):
        self._service=FoundationService(database,actor_id='trusted_execution_host')
        self._candidate_id=candidate_id;self._application_id=application_id
        self._session=new_id();self._issued={};self._lock=RLock()

    def _current(self,repo,attempt_id):
        app=repo.get(Application,self._application_id)
        if app.candidate_id!=self._candidate_id: raise Rejected('execution_scope_mismatch')
        attempt=repo.get(SubmissionAttempt,attempt_id)
        if (app.latest_attempt_id!=attempt.id or attempt.application_id!=app.id
            or attempt.candidate_id!=app.candidate_id or attempt.job_id!=app.job_id
            or attempt.canonical_id!=app.canonical_id or attempt.resume_id!=app.resume_id):
            raise Rejected('attempt_scope_mismatch')
        if app.status!=A.SUBMISSION_UNCERTAIN or attempt.action_state!='reserved' or attempt.outcome!='uncertain':
            raise Rejected('attempt_not_executable')
        if app.revision!=attempt.context['application_revision']+1:
            raise Rejected('application_changed_since_reservation')
        if attempt.context.get('execution_host_epoch') != _host_epoch():
            raise Rejected('interrupted_host_requires_reconciliation')
        current=revision_context(repo,self._service,app)
        if not attempt.context.get('execution_context') or fingerprint(attempt.context['execution_context'])!=fingerprint(current):
            raise Rejected('reservation_context_stale_or_legacy')
        if repo.count(UnresolvedInformation,application_id=app.id,resolved=False):
            raise Rejected('unresolved_information_requires_resolution')
        predecessors=[]
        for prior in all_records(repo,SubmissionAttempt,application_id=app.id):
            if prior.id==attempt.id: continue
            if prior.outcome=='confirmed_submitted': raise Rejected('already_submitted')
            if prior.outcome!='confirmed_not_submitted': raise Rejected('prior_attempt_unresolved')
            predecessors.append(prior)
        previous_id=attempt.context.get('retry_of')
        if predecessors and not previous_id: raise Rejected('retry_lineage_required')
        if previous_id:
            prior=repo.get(SubmissionAttempt,previous_id)
            if prior not in predecessors or not prior.retry_consumed or not prior.resolution or not prior.evidence:
                raise Rejected('retry_evidence_or_consumption_missing')
            if prior.revision!=attempt.context.get('retry_revision'): raise Rejected('retry_revision_changed')
            validate_retry_scope(repo,self._service,app,prior)
        return app,attempt,{**current,'application_revision':app.revision,'attempt_id':attempt.id,
            'attempt_revision':attempt.revision,'correlation_id':attempt.correlation_id,
            'retry_of':previous_id,'retry_revision':attempt.context.get('retry_revision')}

    def capture_context(self,attempt_id):
        """Non-authorizing snapshot to compare with the executor's observed context."""
        with self._service.database.transaction() as repo:
            _,_,snapshot=self._current(repo,attempt_id)
            return ExecutionContext(encoded(snapshot))

    def _deny(self,action,reason):
        def operation(repo,ctx): raise Rejected(reason)
        self._service._run(action,self._application_id,operation)
        raise Rejected(reason)

    def authorize_submission(self,context):
        with self._lock:
            if type(context) is not ExecutionContext: self._deny('authorize_submission','execution_context_required')
            try: supplied=json.loads(context.snapshot_json)
            except (TypeError,ValueError): self._deny('authorize_submission','invalid_execution_context')
            if not isinstance(supplied,dict): self._deny('authorize_submission','invalid_execution_context')
            holder={}
            def operation(repo,ctx):
                _,attempt,current=self._current(repo,supplied.get('attempt_id'))
                if encoded(current)!=context.snapshot_json: raise Rejected('execution_context_mismatch')
                if attempt.context.get('execution_authorization'): raise Rejected('authorization_already_issued_reconcile_required')
                old=deepcopy(attempt);identity=new_id()
                attempt.context['execution_authorization']={'id':identity,'session':self._session,'issued_at':now(),'consumed':False}
                self._service._put(repo,ctx,attempt,old,metadata={'authorization_id':identity,'authorization_context':current})
                current['attempt_revision']=attempt.revision+1
                holder['authorization']=SubmissionAuthorization(identity,encoded(current))
                return Decision(False,entity_id=attempt.id)
            result=self._service._run('authorize_submission',supplied.get('attempt_id'),operation)
            if result.blocked: raise Rejected(','.join(result.reasons))
            authorization=holder['authorization']
            self._issued[authorization.authorization_id]=authorization
            return authorization

    def consume_submission(self,authorization):
        with self._lock:
            if type(authorization) is not SubmissionAuthorization or self._issued.get(authorization.authorization_id) is not authorization:
                self._deny('consume_submission_authorization','unrecognized_copied_or_consumed_authorization')
            supplied=json.loads(authorization.snapshot_json)
            holder={}
            def operation(repo,ctx):
                _,attempt,current=self._current(repo,supplied['attempt_id'])
                issued=attempt.context.get('execution_authorization',{})
                if (issued.get('id')!=authorization.authorization_id or issued.get('session')!=self._session or issued.get('consumed')
                    or encoded(current)!=authorization.snapshot_json):
                    raise Rejected('stale_execution_authorization')
                old=deepcopy(attempt)
                issued['consumed']=True;issued['consumed_at']=now()
                attempt.action_state='execution_started'
                self._service._put(repo,ctx,attempt,old,metadata={'authorization_id':authorization.authorization_id,'action_state':'execution_started'})
                holder['receipt']=ExecutionStartReceipt(attempt.id,authorization.authorization_id,authorization.snapshot_json)
                return Decision(False,entity_id=attempt.id)
            result=self._service._run('consume_submission_authorization',supplied['attempt_id'],operation)
            if result.blocked: raise Rejected(','.join(result.reasons))
            del self._issued[authorization.authorization_id]
            return holder['receipt']
