"""Explicit local owner interface. Never give this object to autonomous workers.

The operator invoking this interface asserts they are the human owner. This is
not authentication and does not defend against malicious code on the machine.
"""
from copy import deepcopy
from job_applier.models import *
from job_applier.models import ApplicationStatus as A, OutreachStatus as O
from job_applier.models.validation import nonblank, validate_path, validate_evidence, fact, present
from job_applier.services.context import form_manifest, question_scope, fingerprint, approved_for
from job_applier.services.foundation import FoundationService, Decision, Rejected, PREAPPLICATION, all_records


class LocalHumanOperations:
    def __init__(self,database,*,owner_actor_id):
        if not nonblank(owner_actor_id): raise ValueError('human_owner_actor_required')
        self._service=FoundationService(database)
        self._actor_id=owner_actor_id

    def _run(self,action,identity,reason,operation):
        def checked(repo,ctx):
            if not nonblank(reason): raise Rejected('human_reason_required')
            return operation(repo,ctx)
        return self._service._run(action,identity,checked,human={'actor_id':self._actor_id,'reason':reason})

    @staticmethod
    def _approval(record,ctx,*,source='explicit_local_human_operation'):
        return {'actor_id':ctx['actor_id'],'actor_type':'HUMAN','timestamp':now(),
            'reason':ctx['reason'],'source':source,'previous_revision':record.revision,
            'new_revision':record.revision+1,'approved_content_revision':getattr(record,'content_revision',record.revision),
            'correlation_id':ctx['correlation_id']}

    def approve_answer(self,answer_id,*,expected_revision,reason,requirements_id=None,question_id=None):
        def operation(repo,ctx):
            answer=repo.get(ApprovedAnswer,answer_id); old=deepcopy(answer)
            if answer.revision!=expected_revision: raise Rejected('stale_revision')
            candidate=repo.get(CandidateProfile,answer.candidate_id)
            if candidate.conflicts: raise Rejected('candidate_conflicts_require_resolution')
            if not requirements_id or not question_id: raise Rejected('explicit_approval_scope_required')
            req=repo.get(ReadinessRequirements,requirements_id)
            questions=[question for question in req.questions if question.get('question_id')==question_id]
            if len(questions)!=1 or req.candidate_id!=answer.candidate_id or questions[0]['text']!=answer.question:
                raise Rejected('scope_question_mismatch')
            scope=question_scope(repo,req,questions[0])
            answer.scope=deepcopy(scope)
            answer.approval_state=ApprovalState.APPROVED; answer.approval=self._approval(old,ctx)
            answer.approval.update(scope_fingerprint=fingerprint(scope),job_revision=repo.get(Job,req.job_id).revision)
            answer.candidate_revision=candidate.revision
            self._service._put(repo,ctx,answer,old,metadata={'approval':answer.approval})
            self._service._invalidate(repo,ctx,candidate_id=answer.candidate_id,invalidate_facts=False)
        return self._run('approve_answer',answer_id,reason,operation)

    def revoke_answer(self,answer_id,*,expected_revision,reason):
        def operation(repo,ctx):
            answer=repo.get(ApprovedAnswer,answer_id); old=deepcopy(answer)
            if answer.revision!=expected_revision: raise Rejected('stale_revision')
            approval=self._approval(old,ctx)
            answer.approval_state=ApprovalState.REVOKED; answer.approval=None
            self._service._put(repo,ctx,answer,old,metadata={'revocation':approval})
            self._service._invalidate(repo,ctx,candidate_id=answer.candidate_id,invalidate_facts=False)
        return self._run('revoke_answer',answer_id,reason,operation)

    def review_requirements(self,requirements_id,*,expected_revision,reason):
        def operation(repo,ctx):
            req=repo.get(ReadinessRequirements,requirements_id); old=deepcopy(req)
            if req.revision!=expected_revision: raise Rejected('stale_revision')
            app=repo.get(Application,req.application_id)
            if app.status not in PREAPPLICATION | {A.CONFIRMED_NOT_SUBMITTED}: raise Rejected('requirements_review_not_allowed')
            candidate=repo.get(CandidateProfile,req.candidate_id)
            if candidate.conflicts: raise Rejected('candidate_conflicts_require_resolution')
            manifest=form_manifest(repo,req)
            req.approval=self._approval(old,ctx); req.approval['form_fingerprint']=fingerprint(manifest)
            req.candidate_revision=candidate.revision
            req.job_revision=repo.get(Job,req.job_id).revision
            self._service._put(repo,ctx,req,old,metadata={'approval':req.approval})
        return self._run('review_requirements',requirements_id,reason,operation)

    def resolve_candidate_information(self,candidate_id,*,field_path,value,expected_revision,reason,resolve_conflict=False):
        def operation(repo,ctx):
            validate_path(field_path)
            if not present(value): raise Rejected('resolution_cannot_be_missing')
            candidate=repo.get(CandidateProfile,candidate_id); old=deepcopy(candidate)
            if candidate.revision!=expected_revision: raise Rejected('stale_revision')
            if field_path in candidate.conflicts and not resolve_conflict: raise Rejected('explicit_conflict_resolution_required')
            parts=field_path.split('.')
            if len(parts)==1:
                setattr(candidate,parts[0],deepcopy(value))
            else:
                target=getattr(candidate,parts[0])
                for part in parts[1:-1]:
                    if not isinstance(target,dict): raise Rejected('unsupported_candidate_field_path')
                    target=target.setdefault(part,{})
                if isinstance(target,dict): target[parts[-1]]=deepcopy(value)
                elif hasattr(target,parts[-1]): setattr(target,parts[-1],deepcopy(value))
                else: raise Rejected('unsupported_candidate_field_path')
            if resolve_conflict:
                if field_path not in candidate.conflicts: raise Rejected('conflict_not_found')
                del candidate.conflicts[field_path]
            resolution=self._approval(old,ctx)
            self._service._put(repo,ctx,candidate,old,metadata={'resolution':resolution,'field_path':field_path,'conflict_resolved':resolve_conflict})
            self._service._invalidate(repo,ctx,candidate_id=candidate_id)
            for app in all_records(repo,Application,candidate_id=candidate_id):
                for unresolved in all_records(repo,UnresolvedInformation,application_id=app.id,resolved=False):
                    if unresolved.field_path and present(fact(candidate,unresolved.field_path)):
                        before=deepcopy(unresolved); unresolved.resolved=True; unresolved.resolution=self._approval(before,ctx)
                        self._service._put(repo,ctx,unresolved,before,metadata={'resolution':unresolved.resolution})
        return self._run('resolve_candidate_information',candidate_id,reason,operation)

    def resolve_unknown_question(self,unresolved_id,answer_id,*,reason):
        def operation(repo,ctx):
            unresolved=repo.get(UnresolvedInformation,unresolved_id); old=deepcopy(unresolved)
            app=repo.get(Application,unresolved.application_id)
            answer=repo.get(ApprovedAnswer,answer_id); candidate=repo.get(CandidateProfile,app.candidate_id)
            if unresolved.resolved or not unresolved.question_text: raise Rejected('open_question_required')
            req=repo.list(ReadinessRequirements,application_id=app.id,limit=1)[0]
            questions=[q for q in req.questions if q.get('question_id')==unresolved.context.get('question_id')]
            if len(questions)!=1: raise Rejected('current_exact_approved_answer_required')
            scope=question_scope(repo,req,questions[0])
            if (fingerprint(unresolved.context.get('scope'))!=fingerprint(scope)
                or not approved_for(answer,scope,candidate,repo.get(Job,app.job_id))):
                raise Rejected('current_exact_approved_answer_required')
            unresolved.resolved=True; unresolved.resolution={**self._approval(old,ctx),'answer_id':answer.id,'answer_revision':answer.revision}
            self._service._put(repo,ctx,unresolved,old,metadata={'resolution':unresolved.resolution})
        return self._run('resolve_unknown_question',unresolved_id,reason,operation)

    def verify_contact(self,contact_id,*,expected_revision,evidence,reason):
        def operation(repo,ctx):
            validate_evidence(evidence)
            contact=repo.get(Contact,contact_id); old=deepcopy(contact)
            if contact.revision!=expected_revision or not nonblank(contact.email): raise Rejected('contact_revision_or_email_invalid')
            contact.verified=True; contact.verification_revision=old.revision+1
            contact.verified_at=now(); contact.verification_source='local_human_evidence'
            contact.verification={**self._approval(old,ctx),'evidence':evidence}
            self._service._put(repo,ctx,contact,old,metadata={'verification':contact.verification})
            self._service._invalidate(repo,ctx,contact_id=contact.id)
        return self._run('verify_contact',contact_id,reason,operation)

    def revoke_contact_verification(self,contact_id,*,expected_revision,reason):
        def operation(repo,ctx):
            contact=repo.get(Contact,contact_id); old=deepcopy(contact)
            if contact.revision!=expected_revision: raise Rejected('stale_revision')
            contact.verified=False; contact.verification=None; contact.verification_revision=None
            contact.verification_source=None; contact.verified_at=None
            self._service._put(repo,ctx,contact,old,metadata={'revocation':self._approval(old,ctx)})
            self._service._invalidate(repo,ctx,contact_id=contact.id)
        return self._run('revoke_contact_verification',contact_id,reason,operation)

    def approve_outreach(self,outreach_id,*,expected_revision,reason):
        def operation(repo,ctx):
            item=repo.get(Outreach,outreach_id); old=deepcopy(item)
            if item.revision!=expected_revision or item.status!=O.DRAFT_CREATED or not nonblank(item.draft): raise Rejected('current_draft_required')
            contact=repo.get(Contact,item.contact_id); candidate=repo.get(CandidateProfile,item.candidate_id)
            if not contact.verified or contact.verification_revision!=contact.revision or item.contact_revision!=contact.revision or item.candidate_revision!=candidate.revision or candidate.conflicts:
                raise Rejected('stale_outreach_context')
            item.status=O.APPROVED; item.approval=self._approval(old,ctx)
            self._service._put(repo,ctx,item,old,metadata={'approval':item.approval})
        return self._run('approve_outreach',outreach_id,reason,operation)

    def reconcile_submission(self,attempt_id,*,expected_revision,outcome,evidence,reason):
        def operation(repo,ctx):
            validate_evidence(evidence)
            if outcome not in {'uncertain','confirmed_submitted','confirmed_not_submitted'}: raise Rejected('invalid_reconciliation_outcome')
            attempt=repo.get(SubmissionAttempt,attempt_id); old=deepcopy(attempt)
            app=repo.get(Application,attempt.application_id); before=deepcopy(app)
            if attempt.revision!=expected_revision or attempt.outcome!='uncertain' or app.latest_attempt_id!=attempt.id:
                raise Rejected('current_uncertain_attempt_required')
            attempt.evidence.extend(deepcopy(evidence)); attempt.resolution=self._approval(old,ctx); attempt.outcome=outcome
            if attempt.action_state != 'legacy': attempt.action_state=outcome
            self._service._put(repo,ctx,attempt,old,metadata={'resolution':attempt.resolution,'evidence':evidence,'outcome':outcome})
            app.readiness=None
            if outcome=='uncertain': app.status=A.SUBMISSION_UNCERTAIN; app.block_reasons=['submission_uncertain']
            elif outcome=='confirmed_submitted': app.status=A.SUBMITTED; app.block_reasons=[]
            elif outcome=='confirmed_not_submitted': app.status=A.CONFIRMED_NOT_SUBMITTED; app.block_reasons=['human_retry_authorization_required']
            self._service._put(repo,ctx,app,before)
            return Decision(outcome=='uncertain',('submission_uncertain',) if outcome=='uncertain' else (),app.id)
        return self._run('reconcile_submission',attempt_id,reason,operation)

    def authorize_retry(self,attempt_id,*,expected_revision,reason):
        def operation(repo,ctx):
            attempt=repo.get(SubmissionAttempt,attempt_id); old=deepcopy(attempt)
            app=repo.get(Application,attempt.application_id); before=deepcopy(app)
            if attempt.revision!=expected_revision or attempt.outcome!='confirmed_not_submitted' or not attempt.resolution or not attempt.evidence or attempt.retry_consumed or app.status not in PREAPPLICATION | {A.CONFIRMED_NOT_SUBMITTED} or app.latest_attempt_id!=attempt.id:
                raise Rejected('confirmed_non_submission_required')
            from job_applier.services.execution import revision_context
            scope=revision_context(repo,self._service,app)
            attempt.retry_authorization={**self._approval(old,ctx),'revision_context':scope}
            self._service._put(repo,ctx,attempt,old,metadata={'retry_authorization':attempt.retry_authorization,'evidence':attempt.evidence})
            app.status=A.SHORTLISTED; app.readiness=None; app.block_reasons=['readiness_recheck_required']
            self._service._put(repo,ctx,app,before)
        return self._run('authorize_retry',attempt_id,reason,operation)

    def confirm_job_equivalence(self,source_record_id,canonical_id,*,evidence,reason):
        def operation(repo,ctx):
            source=repo.get(Job,source_record_id)
            return self._service._link(repo,ctx,source_record_id,canonical_id,evidence,approval=self._approval(source,ctx))
        return self._run('confirm_job_equivalence',source_record_id,reason,operation)


    def confirm_job_distinct(self,source_record_id,*,expected_revision,evidence,reason):
        """Owner confirms that an unresolved source is a separate opening."""
        def operation(repo,ctx):
            from job_applier.services.identity import resolve_identity
            source=repo.get(Job,source_record_id);old=deepcopy(source)
            if source.revision != expected_revision: raise Rejected('stale_revision')
            if repo.count(Application,job_id=source.id): raise Rejected('review_before_application_creation_required')
            if not isinstance(evidence,dict) or not all(nonblank(evidence.get(k)) for k in ('reference','description')):
                raise Rejected('identity_evidence_required')
            resolution=resolve_identity(repo,source,exclude_canonical_id=source.canonical_id)
            if resolution.kind == 'strong': raise Rejected('strong_identity_cannot_be_declared_distinct')
            # A distinct declaration cannot split sources already sharing one
            # canonical opening. It resolves only a provisional source opening.
            if repo.count(Job,canonical_id=source.canonical_id) != 1:
                raise Rejected('canonical_split_not_supported')
            approval=self._approval(old,ctx)
            self._service._put(repo,ctx,source,old,metadata={'identity_decision':approval})
            self._service._put(repo,ctx,JobLinkEvidence(source_record_id=source.id,canonical_id=source.canonical_id,
                method='human_distinct',approval=approval,evidence={**deepcopy(evidence),**resolution.evidence(source)}))
            return Decision(False,entity_id=source.id)
        return self._run('confirm_job_distinct',source_record_id,reason,operation)
