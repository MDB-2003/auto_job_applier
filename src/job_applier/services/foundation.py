"""Trusted domain operations. Expose only the restricted JSON interface to workers."""
from copy import deepcopy
from dataclasses import dataclass, field, asdict
import hashlib
import logging
from pathlib import Path
from typing import Any

from job_applier.database import Database, Repository, _DOMAIN_WRITE, ConflictError
from job_applier.models import *
from job_applier.models import ApplicationStatus as A, OutreachStatus as O
from job_applier.models.states import validate_transition
from job_applier.services.context import form_manifest, question_scope, fingerprint, approved_for
from job_applier.services.identity import identity_values, resolve_identity
from job_applier.models.validation import GLOBAL_REQUIRED_FIELDS, fact, valid_required_value, nonblank, validate_path, validate_record

logger = logging.getLogger(__name__)
LIBRARY_TYPES = (CandidateProfile, Resume, ApprovedAnswer, JobCriteria, Job, Contact)
PREAPPLICATION = {A.DISCOVERED,A.SHORTLISTED,A.READY_TO_APPLY,A.BLOCKED,A.BLOCKED_UNKNOWN_QUESTION}


@dataclass(frozen=True)
class Decision:
    blocked: bool
    reasons: tuple[str,...] = ()
    entity_id: str | None = None
    answers: dict[str,str] = field(default_factory=dict)


class Rejected(ValueError): pass


def all_records(repo, model, **filters):
    offset = 0
    while True:
        page = repo.list(model,limit=100,offset=offset,**filters)
        yield from page
        if len(page)<100: break
        offset += len(page)


def resume_hash(path):
    file = Path(path)
    if file.suffix.lower() not in {'.pdf','.docx','.txt'} or not file.is_file():
        raise ValueError('resume_file_unavailable_or_unsupported')
    if file.stat().st_size == 0: raise ValueError('empty_resume_file')
    with file.open('rb') as handle:
        return hashlib.file_digest(handle,'sha256').hexdigest()


class FoundationService:
    """Trusted-host service, not a worker capability or a worker-visible object."""
    def __init__(self, database: Database, *, actor_id: str = 'local_worker', correlation_id: str | None = None):
        if not nonblank(actor_id): raise ValueError('actor_required')
        self.database = database
        self.actor_id = actor_id
        if correlation_id is not None and not nonblank(correlation_id):
            raise ValueError('correlation_id_required')
        self._correlation_id = correlation_id

    def _event(self, repo, context, *, entity='Command', entity_id='local', previous=None, new=None,
               result='success', error=None, metadata=None):
        repo.add(AuditEvent(action=context['action'],entity=entity,entity_id=entity_id,
            previous_state=previous,new_state=new,result=result,error=error,source='local_foundation',
            actor_id=context['actor_id'],actor_type=context['actor_type'],correlation_id=context['correlation_id'],metadata=metadata or {}))

    def _run(self, action, entity_id, operation, *, human=None):
        context = dict(action=action,actor_id=self.actor_id,actor_type='WORKER',correlation_id=self._correlation_id or new_id())
        if human: context.update(actor_id=human['actor_id'],actor_type='HUMAN',reason=human['reason'])
        try:
            with self.database.transaction(capability=_DOMAIN_WRITE) as repo:
                result = operation(repo,context)
                if result is None: result = Decision(False,entity_id=entity_id)
                self._event(repo,context,entity_id=entity_id or 'local',result='blocked' if result.blocked else 'success',error=','.join(result.reasons) or None)
            logger.info('action=%s correlation_id=%s result=%s',action,context['correlation_id'],'blocked' if result.blocked else 'success')
            return result
        except Exception as exc:
            rejected = isinstance(exc,(ValueError,LookupError,PermissionError,TypeError))
            # The command transaction is gone: rejection is recorded separately.
            try:
                with self.database.transaction(capability=_DOMAIN_WRITE) as repo:
                    self._event(repo,context,entity_id=entity_id or 'local',result='rejected' if rejected else 'failed',
                        error=str(exc) if isinstance(exc,Rejected) else type(exc).__name__)
            except Exception:
                logger.error('audit_failure action=%s correlation_id=%s',action,context['correlation_id'])
                raise  # No business success and no false durable-audit claim.
            logger.warning('action=%s correlation_id=%s result=%s',action,context['correlation_id'],'rejected' if rejected else 'failed')
            if rejected:
                return Decision(True,(str(exc) if isinstance(exc,Rejected) else type(exc).__name__,),entity_id)
            raise

    def _put(self, repo, context, record, old=None, *, metadata=None):
        record = deepcopy(record)
        if old:
            if record.revision != old.revision: raise Rejected('stale_revision')
            record.revision += 1
            record.created_at = old.created_at
            record.updated_at = now()
            repo.update(record)
        else:
            if record.revision != 1: raise Rejected('new_record_revision_must_be_one')
            repo.add(record)
        extra = {'previous_revision':old.revision if old else 0,'new_revision':record.revision}
        if old:
            extra['changed_fields']=[key for key,value in asdict(record).items()
                if key not in {'revision','updated_at'} and value!=asdict(old)[key]]
        if 'reason' in context: extra['reason']=context['reason']
        if isinstance(record,Contact):
            extra['previous_verification']=old.verified if old else False
            extra['new_verification']=record.verified
        if isinstance(record,ReadinessRequirements):
            extra['previous_approval']=bool(old and old.approval)
            extra['new_approval']=bool(record.approval)
        if metadata: extra.update(metadata)
        self._event(repo,context,entity=type(record).__name__,entity_id=record.id,
            previous=getattr(old,'status',getattr(old,'approval_state',None)),
            new=getattr(record,'status',getattr(record,'approval_state',None)),metadata=extra)
        return record

    def _invalidate(self, repo, context, candidate_id=None, job_id=None, resume_id=None, contact_id=None, invalidate_facts=True):
        filters = {'candidate_id':candidate_id} if candidate_id else {'job_id':job_id} if job_id else {'resume_id':resume_id} if resume_id else None
        if filters is not None:
            for app in all_records(repo,Application,**filters):
                if app.status in PREAPPLICATION and app.readiness is not None:
                    old=deepcopy(app); app.status=A.BLOCKED; app.readiness=None; app.block_reasons=['stale_readiness']
                    self._put(repo,context,app,old)
        if candidate_id and invalidate_facts:
            for answer in all_records(repo,ApprovedAnswer,candidate_id=candidate_id):
                if answer.approval_state == ApprovalState.APPROVED:
                    old=deepcopy(answer); answer.approval_state=ApprovalState.REVOKED; answer.approval=None
                    self._put(repo,context,answer,old,metadata={'reason':'candidate_revision_changed'})
            for req in all_records(repo,ReadinessRequirements,candidate_id=candidate_id):
                if req.approval:
                    old=deepcopy(req); req.approval=None
                    self._put(repo,context,req,old)
        if job_id:
            for req in all_records(repo,ReadinessRequirements,job_id=job_id):
                if req.approval:
                    old=deepcopy(req); req.approval=None
                    self._put(repo,context,req,old)
        outreach_filters={'contact_id':contact_id} if contact_id else {'candidate_id':candidate_id} if candidate_id else None
        if outreach_filters:
            for outreach in all_records(repo,Outreach,**outreach_filters):
                if outreach.status in {O.CONTACT_VERIFIED,O.DRAFT_CREATED,O.APPROVED}:
                    old=deepcopy(outreach); outreach.status=O.BLOCKED; outreach.approval=None; outreach.block_reasons=['stale_verification_or_candidate']
                    self._put(repo,context,outreach,old)

    def save_library_record(self, record: Record, *, update: bool = False) -> Decision:
        def operation(repo,ctx):
            item=deepcopy(record)
            if type(item) not in LIBRARY_TYPES: raise Rejected('unsupported_library_record')
            old=repo.get(type(item),item.id) if update else None
            if old:
                if item.revision != old.revision: raise Rejected('stale_revision')
                for key in ('candidate_id','canonical_id','source_job_id'):
                    if hasattr(old,key) and getattr(old,key)!=getattr(item,key): raise Rejected('immutable_identity')
                if isinstance(item,Job) and any(getattr(item,key)!=getattr(old,key) for key in ('source','company','requisition_id','canonical_url')):
                    raise Rejected('immutable_job_identity_requires_explicit_relink')
            if isinstance(item,CandidateProfile) and old:
                if any(key not in item.conflicts or item.conflicts[key]!=value for key,value in old.conflicts.items()):
                    raise Rejected('human_conflict_resolution_required')
            if isinstance(item,ApprovedAnswer):
                if item.approval_state != ApprovalState.DRAFT or item.approval is not None:
                    raise Rejected('dedicated_human_approval_required')
                if item.scope != (old.scope if old else {}): raise Rejected('human_scope_binding_required')
                item.content_revision = old.content_revision+1 if old else 1
                item.candidate_revision=None
            if isinstance(item,Contact):
                if item.verified or item.verification is not None or item.verification_revision is not None:
                    # Unchanged verification may be supplied when editing a loaded record;
                    # edits always invalidate it rather than carrying it forward.
                    if not old or item.verification != old.verification or item.verified != old.verified or item.verification_revision != old.verification_revision:
                        raise Rejected('dedicated_verification_required')
                item.verified=False; item.verification=None; item.verification_revision=None
                item.verified_at=None; item.verification_source=None
            if isinstance(item,Resume):
                item.content_hash=resume_hash(item.file_path)
            if isinstance(item,Job) and old is None:
                if item.canonical_id is not None: raise Rejected('use_evidence_link_operation')
                resolution=resolve_identity(repo,item)
                if resolution.kind == 'strong':
                    item.canonical_id=resolution.target_id
                else:
                    # Ambiguous/conflicting imports are retained, but their
                    # provisional canonical cannot authorize an application.
                    canonical=CanonicalOpening(company=item.company,title=item.title,canonical_url=item.canonical_url,requisition_id=item.requisition_id,provenance=item.provenance)
                    self._put(repo,ctx,canonical)
                    item.canonical_id=canonical.id
            saved=self._put(repo,ctx,item,old)
            if isinstance(item,Job) and old is None:
                evidence={**resolution.evidence(item),'source':item.source,'source_job_id':item.source_job_id,'provenance':item.provenance}
                if resolution.kind == 'strong':
                    target=repo.get(CanonicalOpening,item.canonical_id)
                    evidence.update(self._compared_identity(item,target))
                self._put(repo,ctx,JobLinkEvidence(source_record_id=item.id,canonical_id=item.canonical_id,
                    method='strong_identity' if resolution.kind == 'strong' else 'initial_source',evidence=evidence))
                if resolution.kind in {'ambiguous','conflicting'}:
                    return Decision(True,('identity_review_required',),saved.id)
            if old:
                if isinstance(item,CandidateProfile): self._invalidate(repo,ctx,candidate_id=item.id)
                elif isinstance(item,ApprovedAnswer): self._invalidate(repo,ctx,candidate_id=item.candidate_id,invalidate_facts=False)
                elif isinstance(item,JobCriteria): self._invalidate(repo,ctx,candidate_id=item.candidate_id)
                elif isinstance(item,Resume): self._invalidate(repo,ctx,resume_id=item.id)
                elif isinstance(item,Job): self._invalidate(repo,ctx,job_id=item.id)
                elif isinstance(item,Contact): self._invalidate(repo,ctx,contact_id=item.id)
            return Decision(False,entity_id=saved.id)
        return self._run('update_record' if update else 'create_record',getattr(record,'id',None),operation)

    def create_application(self,candidate_id,job_id,resume_id=None):
        def operation(repo,ctx):
            repo.get(CandidateProfile,candidate_id); job=repo.get(Job,job_id)
            job, identity_block=self._resolve_application_identity(repo,ctx,job)
            if identity_block:
                return Decision(True,(identity_block,),job.id)
            duplicates=repo.list(Application,candidate_id=candidate_id,canonical_id=job.canonical_id,limit=1)
            if duplicates: return Decision(True,('duplicate_application',),duplicates[0].id)
            resume=repo.get(Resume,resume_id) if resume_id else None
            if resume and resume.candidate_id!=candidate_id: raise Rejected('resume_ownership_mismatch')
            app=Application(candidate_id=candidate_id,job_id=job_id,canonical_id=job.canonical_id,
                resume_id=resume_id,resume_revision=resume.revision if resume else None)
            self._put(repo,ctx,app)
            return Decision(False,entity_id=app.id)
        return self._run('create_application',job_id,operation)

    def assign_resume(self,application_id,resume_id):
        def operation(repo,ctx):
            app=repo.get(Application,application_id); old=deepcopy(app)
            if app.status not in PREAPPLICATION | {A.CONFIRMED_NOT_SUBMITTED}: raise Rejected('resume_change_not_allowed')
            resume=repo.get(Resume,resume_id)
            if resume.candidate_id!=app.candidate_id or not resume.active: raise Rejected('invalid_resume_ownership_or_status')
            if resume.content_hash!=resume_hash(resume.file_path): raise Rejected('resume_content_changed')
            app.resume_id=resume.id; app.resume_revision=resume.revision; app.readiness=None
            if app.status==A.READY_TO_APPLY: app.status=A.BLOCKED
            app.block_reasons=['readiness_recheck_required']
            self._put(repo,ctx,app,old)
        return self._run('assign_resume',application_id,operation)

    def save_requirements(self,requirements: ReadinessRequirements,*,update=False):
        def operation(repo,ctx):
            req=deepcopy(requirements)
            if req.approval is not None: raise Rejected('human_requirements_review_required')
            app=repo.get(Application,req.application_id)
            if app.status not in PREAPPLICATION | {A.CONFIRMED_NOT_SUBMITTED}: raise Rejected('requirements_change_not_allowed')
            old=repo.get(ReadinessRequirements,req.id) if update else None
            if old and any(getattr(old,k)!=getattr(req,k) for k in ('application_id','candidate_id','job_id')):
                raise Rejected('immutable_requirements_identity')
            req.content_revision=old.content_revision+1 if old else 1
            req.candidate_revision=None; req.job_revision=None
            self._put(repo,ctx,req,old)
            self._invalidate(repo,ctx,job_id=req.job_id)
            return Decision(False,entity_id=req.id)
        return self._run('save_requirements',getattr(requirements,'id',None),operation)

    def transition_application(self,application_id,target):
        def operation(repo,ctx):
            app=repo.get(Application,application_id); old=deepcopy(app)
            try: status=A(target)
            except (ValueError,TypeError): raise Rejected('invalid_status')
            if app.status in {A.SUBMISSION_UNCERTAIN,A.CONFIRMED_NOT_SUBMITTED}:
                raise Rejected('dedicated_reconciliation_or_retry_required')
            validate_transition(app.status,status)
            if status not in {A.SHORTLISTED,A.BLOCKED,A.BLOCKED_UNKNOWN_QUESTION,A.WITHDRAWN,A.CLOSED}:
                raise Rejected('dedicated_operation_required')
            app.status=status; app.readiness=None
            app.block_reasons=['manual_block'] if status in {A.BLOCKED,A.BLOCKED_UNKNOWN_QUESTION} else []
            self._put(repo,ctx,app,old)
        return self._run('transition_application',application_id,operation)

    def _unresolved(self,repo,ctx,app,*,reason,question=None,path=None,context=None):
        existing=any(fingerprint(row.context)==fingerprint(context or {}) for row in all_records(repo,UnresolvedInformation,application_id=app.id,resolved=False,question_text=question,field_path=path))
        if not existing:
            self._put(repo,ctx,UnresolvedInformation(application_id=app.id,job_id=app.job_id,question_text=question,field_path=path,context=context or {},block_reason=reason))

    def _check_readiness(self,repo,app):
        candidate=repo.get(CandidateProfile,app.candidate_id); job=repo.get(Job,app.job_id)
        reqs=repo.list(ReadinessRequirements,application_id=app.id,limit=1)
        req=reqs[0] if reqs else None
        reasons=[]; missing=[]; unknown=[]; answers={}; answer_revisions={}
        if not req or not req.approval or req.candidate_revision!=candidate.revision or req.job_revision!=job.revision:
            reasons.append('requirements_not_reviewed_or_stale')
        try:
            manifest=form_manifest(repo,req) if req else None
            if not req or not req.approval or req.approval.get('form_fingerprint') != fingerprint(manifest):
                reasons.append('form_review_missing_or_stale')
        except ValueError:
            reasons.append('form_identity_missing_or_invalid')
        if candidate.conflicts: reasons.append('conflicting_candidate_information')
        required=list(GLOBAL_REQUIRED_FIELDS)+(req.required_candidate_fields if req else [])
        for path in dict.fromkeys(required):
            if not valid_required_value(path,fact(candidate,path)):
                missing.append(path); reasons.append('missing_candidate_field:'+path)
        resume=repo.get(Resume,app.resume_id) if app.resume_id else None
        if not resume: reasons.append('missing_resume')
        elif resume.candidate_id!=candidate.id or not resume.active or app.resume_revision!=resume.revision:
            reasons.append('stale_or_invalid_resume_selection')
        else:
            try:
                if resume.content_hash!=resume_hash(resume.file_path): reasons.append('resume_content_changed')
            except (ValueError,OSError): reasons.append('resume_file_unavailable')
        if req:
            for question in req.questions:
                scope=None
                try: scope=question_scope(repo,req,question)
                except ValueError: pass
                matches=[answer for answer in all_records(repo,ApprovedAnswer,candidate_id=candidate.id,question=question['text'])
                    if scope and approved_for(answer,scope,candidate,job)]
                # Conflicting approvals are not resolved by arbitrary row order.
                if not matches or len({answer.answer for answer in matches}) != 1:
                    unknown.append({'text':question['text'],'context':{
                        'form_context':deepcopy(req.context),'question_context':deepcopy(question.get('context',{})),
                        'question_id':question.get('question_id'),'scope':scope}})
                else:
                    answer=matches[0]
                    answers[fingerprint(scope)]=answer.answer; answer_revisions[answer.id]=answer.revision
        if unknown: reasons.append('unknown_or_unapproved_question')
        if app.latest_attempt_id:
            attempt=repo.get(SubmissionAttempt,app.latest_attempt_id)
            if attempt.outcome!='confirmed_not_submitted' or not attempt.retry_authorization or attempt.retry_consumed:
                reasons.append('submission_reconciliation_or_retry_authorization_required')
            else:
                from job_applier.services.execution import validate_retry_scope
                try: validate_retry_scope(repo,self,app,attempt)
                except (ValueError,LookupError): reasons.append('stale_human_retry_authorization')
        snapshot={'candidate_revision':candidate.revision,'job_revision':job.revision,
            'requirements_revision':req.revision if req else None,'resume_revision':resume.revision if resume else None,
            'resume_hash':resume.content_hash if resume else None,'answer_revisions':answer_revisions}
        return reasons,missing,unknown,answers,snapshot

    def prepare_application(self,application_id):
        def operation(repo,ctx):
            app=repo.get(Application,application_id); old=deepcopy(app)
            if app.status not in {A.SHORTLISTED,A.READY_TO_APPLY,A.BLOCKED,A.BLOCKED_UNKNOWN_QUESTION}:
                raise Rejected('shortlist_or_reconciliation_required')
            reasons,missing,unknown,answers,snapshot=self._check_readiness(repo,app)
            for path in missing: self._unresolved(repo,ctx,app,reason='missing_candidate_information',path=path)
            for question in unknown:
                self._unresolved(repo,ctx,app,reason='unknown_or_unapproved_question',question=question['text'],context=question.get('context',{}))
            app.status=A.BLOCKED_UNKNOWN_QUESTION if unknown else A.BLOCKED if reasons else A.READY_TO_APPLY
            app.block_reasons=reasons; app.readiness=None if reasons else snapshot
            self._put(repo,ctx,app,old)
            return Decision(bool(reasons),tuple(reasons),app.id,{} if reasons else answers)
        return self._run('prepare_application',application_id,operation)

    def reserve_submission(self,application_id):
        """Commit local intent before any future executor can receive its receipt.

        This trusted-host operation grants no external execution authority. An
        unresolved reservation is uncertain even if no action was ever started.
        """
        def operation(repo,ctx):
            app=repo.get(Application,application_id); old=deepcopy(app)
            # Inspect all history, not just a caller-visible latest pointer.
            for prior in all_records(repo,SubmissionAttempt,application_id=app.id):
                if prior.outcome == 'confirmed_submitted': raise Rejected('already_submitted')
                if prior.outcome == 'uncertain': raise Rejected('outstanding_attempt_requires_reconciliation')
            if app.status!=A.READY_TO_APPLY: raise Rejected('ready_application_required')
            reasons,_,_,_,snapshot=self._check_readiness(repo,app)
            if reasons: raise Rejected('stale_readiness')
            from job_applier.services.execution import revision_context, validate_retry_scope, _host_epoch
            execution_context=revision_context(repo,self,app)
            retry_context={}
            if app.latest_attempt_id:
                previous=repo.get(SubmissionAttempt,app.latest_attempt_id); before=deepcopy(previous)
                if (previous.application_id != app.id or previous.outcome != 'confirmed_not_submitted'
                    or not previous.resolution or not previous.evidence
                    or not previous.retry_authorization or previous.retry_consumed):
                    raise Rejected('retry_not_authorized')
                validate_retry_scope(repo,self,app,previous)
                previous.retry_consumed=True
                previous=self._put(repo,ctx,previous,before)
                retry_context={'retry_of':previous.id,'retry_revision':previous.revision}
            canonical=repo.get(CanonicalOpening,app.canonical_id)
            attempt=SubmissionAttempt(application_id=app.id,previous_state=app.status.value,
                action_state='reserved',candidate_id=app.candidate_id,job_id=app.job_id,
                canonical_id=app.canonical_id,resume_id=app.resume_id,correlation_id=ctx['correlation_id'],
                context={**snapshot,'application_revision':app.revision,'canonical_revision':canonical.revision,
                    'execution_context':execution_context,'execution_host_epoch':_host_epoch(),**retry_context})
            self._put(repo,ctx,attempt,metadata={'action_state':'reserved','context':attempt.context})
            app.status=A.SUBMISSION_UNCERTAIN; app.latest_attempt_id=attempt.id
            app.readiness=None; app.block_reasons=['outstanding_attempt_requires_reconciliation']
            self._put(repo,ctx,app,old)
            return Decision(False,entity_id=attempt.id)
        # _run returns success only after the database transaction has committed.
        return self._run('reserve_submission',application_id,operation)

    def outstanding_submission_attempts(self,*,limit=100,offset=0):
        """Recovery inventory; returning an attempt never grants permission to run it.

        Callers must page until exhausted. Reserved, uncertain, and legacy
        unresolved attempts all require human reconciliation, never replay.
        """
        with self.database.transaction() as repo:
            return repo.list(SubmissionAttempt,outcome='uncertain',limit=limit,offset=offset)

    def record_submission_uncertainty(self,application_id,*,evidence=None):
        """Report inconclusive execution only against an already durable attempt."""
        def operation(repo,ctx):
            app=repo.get(Application,application_id); before=deepcopy(app)
            if not app.latest_attempt_id: raise Rejected('durable_attempt_required')
            attempt=repo.get(SubmissionAttempt,app.latest_attempt_id); old=deepcopy(attempt)
            if attempt.application_id != app.id or attempt.outcome != 'uncertain':
                raise Rejected('current_uncertain_attempt_required')
            if evidence:
                from job_applier.models.validation import validate_evidence
                validate_evidence(evidence)
                attempt.evidence.extend(deepcopy(evidence))
            if attempt.action_state != 'legacy': attempt.action_state='uncertain'
            self._put(repo,ctx,attempt,old,metadata={'outcome':'uncertain','evidence':evidence or []})
            app.status=A.SUBMISSION_UNCERTAIN; app.readiness=None; app.block_reasons=['submission_uncertain']
            self._put(repo,ctx,app,before)
            return Decision(True,('submission_uncertain',),attempt.id)
        return self._run('record_submission_uncertainty',application_id,operation)

    def record_submission_failure(self,application_id,*,outcome_uncertain=False):
        # A failure assertion is not proof of non-submission and cannot create intent retroactively.
        return self.record_submission_uncertainty(application_id)

    def request_external_action(self,entity_type,entity_id,*,action):
        def operation(repo,ctx):
            repo.get(entity_type,entity_id)
            return Decision(True,('external_actions_disabled',),entity_id)
        return self._run(action,entity_id,operation)

    def create_outreach(self,candidate_id,contact_id,*,application_id=None):
        def operation(repo,ctx):
            repo.get(CandidateProfile,candidate_id); repo.get(Contact,contact_id)
            item=Outreach(candidate_id=candidate_id,contact_id=contact_id,application_id=application_id)
            self._put(repo,ctx,item)
            return Decision(False,entity_id=item.id)
        return self._run('create_outreach',contact_id,operation)

    def transition_outreach(self,outreach_id,target,*,draft=None):
        def operation(repo,ctx):
            item=repo.get(Outreach,outreach_id); old=deepcopy(item)
            try: status=O(target)
            except (ValueError,TypeError): raise Rejected('invalid_status')
            validate_transition(item.status,status)
            if status in {O.SENT,O.REPLIED,O.FOLLOW_UP_REQUIRED}: raise Rejected('external_actions_disabled')
            if status==O.APPROVED: raise Rejected('human_outreach_approval_required')
            if status in {O.CONTACT_VERIFIED,O.DRAFT_CREATED}:
                contact=repo.get(Contact,item.contact_id); candidate=repo.get(CandidateProfile,item.candidate_id)
                if not contact.verified or contact.verification_revision!=contact.revision or candidate.conflicts:
                    item.status=O.BLOCKED; item.block_reasons=['unverified_contact_or_conflicting_candidate']
                    self._put(repo,ctx,item,old)
                    return Decision(True,tuple(item.block_reasons),item.id)
                item.contact_revision=contact.revision; item.candidate_revision=candidate.revision
            if status==O.DRAFT_CREATED:
                if not nonblank(draft): raise Rejected('missing_draft')
                item.draft=draft; item.approval=None
            item.status=status; item.block_reasons=[]
            self._put(repo,ctx,item,old)
        return self._run('transition_outreach',outreach_id,operation)

    def link_job_source(self,source_record_id,canonical_id,*,evidence):
        return self._run('link_job_source',source_record_id,lambda repo,ctx:self._link(repo,ctx,source_record_id,canonical_id,evidence))

    @staticmethod
    def _compared_identity(source,target):
        return {'source_revision':source.revision,'canonical_revision':target.revision,
            'compared_identity':{'source_company':source.company,'canonical_company':target.company,
            'source_requisition_id':source.requisition_id,'canonical_requisition_id':target.requisition_id,
            'source_canonical_url':source.canonical_url,'canonical_url':target.canonical_url}}

    @staticmethod
    def _human_identity_resolution(repo,source,resolution):
        for link in all_records(repo,JobLinkEvidence,source_record_id=source.id):
            if (link.canonical_id == source.canonical_id and link.method in {'human_confirmation','human_distinct'}
                and link.approval and link.approval.get('actor_type') == 'HUMAN'
                and link.evidence.get('source_identity') == identity_values(source)
                and link.evidence.get('resolution_fingerprint') == resolution.fingerprint):
                return True
        return False

    def _resolve_application_identity(self,repo,ctx,source):
        resolution=resolve_identity(repo,source,exclude_canonical_id=source.canonical_id)
        if self._human_identity_resolution(repo,source,resolution):
            return source,None
        if resolution.kind == 'strong':
            if repo.count(Application,canonical_id=source.canonical_id):
                # Existing histories cannot be silently moved or merged.
                return source,'existing_application_identity_requires_human_review'
            old=deepcopy(source);source.canonical_id=resolution.target_id
            source=self._put(repo,ctx,source,old)
            target=repo.get(CanonicalOpening,source.canonical_id)
            self._put(repo,ctx,JobLinkEvidence(source_record_id=source.id,canonical_id=target.id,
                method='strong_identity',evidence={**resolution.evidence(source),**self._compared_identity(source,target),
                'trigger':'application_creation'}))
            self._invalidate(repo,ctx,job_id=source.id)
        elif resolution.kind in {'ambiguous','conflicting'}:
            self._event(repo,ctx,entity='Job',entity_id=source.id,result='blocked',error='identity_review_required',
                metadata=resolution.evidence(source))
            return source,'identity_review_required'
        return source,None

    def _link(self,repo,ctx,source_record_id,canonical_id,evidence,*,approval=None):
        source=repo.get(Job,source_record_id); target=repo.get(CanonicalOpening,canonical_id)
        if not isinstance(evidence,dict) or not all(nonblank(evidence.get(k)) for k in ('reference','description')):
            raise Rejected('link_evidence_required')
        if source.canonical_id == target.id and not approval:
            # Imports now perform strong linking automatically; keep the explicit
            # request idempotent without rewriting identity or evidence.
            return Decision(False,entity_id=source.id)
        resolution=resolve_identity(repo,source,exclude_canonical_id=source.canonical_id)
        if not approval and (resolution.kind != 'strong' or resolution.target_id != target.id):
            raise Rejected('ambiguous_equivalence_requires_human')
        if repo.count(Application,job_id=source.id): raise Rejected('link_before_application_creation_required')
        old=deepcopy(source); source.canonical_id=target.id
        source=self._put(repo,ctx,source,old)
        # Bind a human decision to the remaining alternatives after relinking.
        # Orphan canonicals are excluded because they have no source records.
        current=resolve_identity(repo,source,exclude_canonical_id=target.id)
        recorded_evidence={**deepcopy(evidence),**self._compared_identity(source,target),
            **(current.evidence(source) if approval else resolution.evidence(source))}
        self._put(repo,ctx,JobLinkEvidence(source_record_id=source.id,canonical_id=target.id,evidence=recorded_evidence,
            method='human_confirmation' if approval else 'strong_identity',approval=approval))
        self._invalidate(repo,ctx,job_id=source.id)
        return Decision(False,entity_id=source.id)
