"""Local-only adversarial execution-authorization and scoped retry tests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy, deepcopy
import json
from unittest.mock import patch

from test_hardening import LocalCase, EVIDENCE
from job_applier.database import _DOMAIN_WRITE
from job_applier.models import (Application, ApplicationStatus as A, CandidateProfile, Job,
    Resume, ReadinessRequirements, ApprovedAnswer, ApprovalState, SubmissionAttempt,
    UnresolvedInformation, AuditEvent)
from job_applier.services.execution import (TrustedExecutionBoundary, ExecutionContext,
    SubmissionAuthorization, ExecutionStartReceipt)
from job_applier.services.foundation import Decision, Rejected
from job_applier.interfaces.worker import TrustedWorkerDispatcher, WorkerScope


class ExecutionAuthorizationTests(LocalCase):
    def reserve(self, with_answer=False):
        if with_answer: self.answer_record=self.answer()
        self.ready()
        identity=self.ok(self.worker.reserve_submission(self.app_id)).entity_id
        self.boundary=TrustedExecutionBoundary(self.db,candidate_id=self.candidate.id,application_id=self.app_id)
        return self.get(SubmissionAttempt,identity)

    def issue(self, with_answer=False):
        attempt=self.reserve(with_answer)
        context=self.boundary.capture_context(attempt.id)
        return attempt,self.boundary.authorize_submission(context)

    def mutate(self, kind):
        if kind=='candidate':
            item=self.get(CandidateProfile,self.candidate.id);item.skills=['changed'];self.save(item,update=True)
        elif kind=='conflict':
            item=self.get(CandidateProfile,self.candidate.id);item.conflicts={'work_authorization.status':['authorized','not_authorized']};self.save(item,update=True)
        elif kind=='job':
            item=self.get(Job,self.job.id);item.title='Updated title';self.save(item,update=True)
        elif kind=='resume':
            item=self.get(Resume,self.resume.id);item.version='2';self.save(item,update=True)
        elif kind=='resume_bytes': self.file.write_text('Different resume bytes')
        elif kind=='answer':
            item=self.get(ApprovedAnswer,self.answer_record.id)
            item.approval_state=ApprovalState.DRAFT;item.approval=None;item.answer='Changed proposal';self.save(item,update=True)
        elif kind=='revocation':
            item=self.get(ApprovedAnswer,self.answer_record.id)
            self.ok(self.human.revoke_answer(item.id,expected_revision=item.revision,reason='Owner revoked'))
        else:
            # Trusted fixture simulates a new observation or invalidation arriving
            # after reservation. Workers do not have these repository capabilities.
            with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
                if kind in ('form','question','review'):
                    item=repo.get(ReadinessRequirements,self.req.id)
                    if kind=='form': item.context['form_version']='2'
                    elif kind=='question': item.questions[0]['context']={'section':'changed'}
                    else: item.approval=None
                    item.revision+=1;repo.update(item)
                elif kind=='readiness':
                    item=repo.get(Application,self.app_id);item.status=A.BLOCKED;item.revision+=1;repo.update(item)
                elif kind=='unresolved':
                    repo.add(UnresolvedInformation(application_id=self.app_id,job_id=self.job.id,
                        question_text='New question?',context={'question_id':'new'},block_reason='unknown_or_unapproved_question'))

    def stale(self, kind):
        attempt,authorization=self.issue(with_answer=True)
        self.mutate(kind)
        with self.assertRaises((Rejected,ValueError)): self.boundary.consume_submission(authorization)
        self.assertNotEqual(self.get(SubmissionAttempt,attempt.id).action_state,'execution_started')

    def test_valid_authorization_succeeds_once_and_durably_starts_attempt(self):
        attempt,authorization=self.issue(with_answer=True)
        self.assertIs(type(authorization),SubmissionAuthorization)
        receipt=self.boundary.consume_submission(authorization)
        self.assertIs(type(receipt),ExecutionStartReceipt)
        current=self.get(SubmissionAttempt,attempt.id)
        self.assertEqual(current.action_state,'execution_started')
        self.assertEqual(current.outcome,'uncertain')
        self.assertTrue(current.context['execution_authorization']['consumed'])
        self.assertEqual(receipt.attempt_id,attempt.id)
        with self.assertRaises(Rejected): self.boundary.consume_submission(authorization)
        self.assertTrue(self.rows(AuditEvent,action='consume_submission_authorization',entity_id=attempt.id))

    def test_copied_shallow_deep_and_reconstructed_authorizations_fail(self):
        _,authorization=self.issue()
        for forged in (copy(authorization),deepcopy(authorization),SubmissionAuthorization(authorization.authorization_id,authorization.snapshot_json)):
            with self.assertRaises(Rejected): self.boundary.consume_submission(forged)
        self.boundary.consume_submission(authorization)

    def test_truthiness_is_never_authority(self):
        _,authorization=self.issue()
        with self.assertRaises(TypeError): bool(authorization)
        for wrong in (Decision(False),True,{},authorization.snapshot_json):
            with self.assertRaises(Rejected): self.boundary.consume_submission(wrong)

    def test_cached_answer_revocation_blocks(self): self.stale('revocation')
    def test_candidate_change_blocks(self): self.stale('candidate')
    def test_job_change_blocks(self): self.stale('job')
    def test_resume_revision_change_blocks(self): self.stale('resume')
    def test_resume_hash_change_blocks(self): self.stale('resume_bytes')
    def test_form_version_change_blocks(self): self.stale('form')
    def test_question_context_change_blocks(self): self.stale('question')
    def test_answer_revision_change_blocks(self): self.stale('answer')
    def test_readiness_invalidation_blocks(self): self.stale('readiness')
    def test_review_invalidation_blocks(self): self.stale('review')
    def test_new_unresolved_question_blocks(self): self.stale('unresolved')
    def test_candidate_conflict_blocks(self): self.stale('conflict')

    def test_uncertain_attempt_cannot_execute(self):
        attempt,authorization=self.issue()
        self.worker.record_submission_uncertainty(self.app_id)
        with self.assertRaises(Rejected): self.boundary.consume_submission(authorization)
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)

    def test_confirmed_submitted_attempt_cannot_execute_or_retry(self):
        attempt,authorization=self.issue()
        current=self.get(SubmissionAttempt,attempt.id)
        self.ok(self.human.reconcile_submission(attempt.id,expected_revision=current.revision,outcome='confirmed_submitted',evidence=EVIDENCE,reason='Owner confirmation'))
        with self.assertRaises(Rejected): self.boundary.consume_submission(authorization)
        current=self.get(SubmissionAttempt,attempt.id)
        self.assertTrue(self.human.authorize_retry(attempt.id,expected_revision=current.revision,reason='Retry').blocked)

    def test_wrong_candidate_cannot_authorize(self):
        attempt=self.reserve()
        other=CandidateProfile();self.save(other)
        boundary=TrustedExecutionBoundary(self.db,candidate_id=other.id,application_id=self.app_id)
        with self.assertRaises(Rejected): boundary.capture_context(attempt.id)

    def test_wrong_application_cannot_authorize(self):
        attempt=self.reserve()
        other=CandidateProfile();self.save(other)
        other_app=self.ok(self.worker.create_application(other.id,self.job.id)).entity_id
        boundary=TrustedExecutionBoundary(self.db,candidate_id=other.id,application_id=other_app)
        with self.assertRaises(Rejected): boundary.capture_context(attempt.id)

    def test_forged_identity_revision_actor_and_approval_metadata_fail(self):
        attempt=self.reserve()
        context=self.boundary.capture_context(attempt.id)
        for key,value in (('candidate_id','other'),('application_id','other'),('canonical_id','other'),
                          ('resume_id','other'),('candidate_revision',999),('application_revision',999),
                          ('actor_type','HUMAN'),('approval',{'actor_type':'HUMAN'})):
            with self.subTest(key=key):
                data=json.loads(context.snapshot_json);data[key]=value
                forged=ExecutionContext(json.dumps(data,sort_keys=True,separators=(',',':')))
                with self.assertRaises(Rejected): self.boundary.authorize_submission(forged)
        self.boundary.authorize_submission(context)

    def test_prepared_context_cannot_survive_candidate_change(self):
        attempt=self.reserve();context=self.boundary.capture_context(attempt.id)
        self.mutate('candidate')
        with self.assertRaises(Rejected): self.boundary.authorize_submission(context)

    def test_first_submission_without_attempt_fails(self):
        self.ready()
        boundary=TrustedExecutionBoundary(self.db,candidate_id=self.candidate.id,application_id=self.app_id)
        with self.assertRaises(LookupError): boundary.capture_context('missing-attempt')
        with self.assertRaises(Rejected): boundary.authorize_submission(Decision(False,entity_id=self.app_id))

    def test_no_replacement_authorization_after_restart(self):
        attempt,authorization=self.issue()
        restarted=TrustedExecutionBoundary(self.db,candidate_id=self.candidate.id,application_id=self.app_id)
        with self.assertRaises(Rejected): restarted.consume_submission(authorization)
        context=restarted.capture_context(attempt.id)
        with self.assertRaises(Rejected): restarted.authorize_submission(context)
        self.assertEqual(self.worker.outstanding_submission_attempts()[0].id,attempt.id)

    def test_interrupted_execution_requires_reconciliation_and_no_replacement(self):
        attempt,authorization=self.issue()
        self.boundary.consume_submission(authorization)
        restarted=TrustedExecutionBoundary(self.db,candidate_id=self.candidate.id,application_id=self.app_id)
        with self.assertRaises(Rejected): restarted.capture_context(attempt.id)
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)
        self.assertEqual(self.worker.outstanding_submission_attempts()[0].action_state,'execution_started')

    def test_concurrent_authorization_issuance_allows_only_one_host(self):
        attempt=self.reserve()
        hosts=[TrustedExecutionBoundary(self.db,candidate_id=self.candidate.id,application_id=self.app_id) for _ in range(2)]
        contexts=[host.capture_context(attempt.id) for host in hosts]
        def issue(i):
            try: return hosts[i].authorize_submission(contexts[i])
            except Rejected: return None
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(issue,range(2)))
        self.assertEqual(sum(result is not None for result in results),1)

    def test_concurrent_consumption_succeeds_once(self):
        _,authorization=self.issue()
        def consume(_):
            try: return self.boundary.consume_submission(authorization)
            except Rejected: return None
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(consume,range(2)))
        self.assertEqual(sum(result is not None for result in results),1)

    def test_consumption_and_execution_start_rollback_atomically(self):
        attempt,authorization=self.issue()
        original=self.db.transaction;fail_next=True
        @contextmanager
        def failing(*,capability=None):
            nonlocal fail_next
            with original(capability=capability) as repo:
                yield repo
                if capability is _DOMAIN_WRITE and fail_next:
                    fail_next=False;raise RuntimeError('simulated commit failure')
        with patch.object(self.db,'transaction',failing),self.assertRaises(RuntimeError):
            self.boundary.consume_submission(authorization)
        current=self.get(SubmissionAttempt,attempt.id)
        self.assertEqual(current.action_state,'reserved')
        self.assertFalse(current.context['execution_authorization']['consumed'])
        events=self.rows(AuditEvent,action='consume_submission_authorization')
        self.assertFalse(any(event.result=='success' for event in events))
        self.boundary.consume_submission(authorization)

    def retry_setup(self):
        attempt=self.reserve(with_answer=True)
        self.ok(self.human.reconcile_submission(attempt.id,expected_revision=1,outcome='confirmed_not_submitted',evidence=EVIDENCE,reason='Owner verified no submission'))
        current=self.get(SubmissionAttempt,attempt.id)
        self.ok(self.human.authorize_retry(attempt.id,expected_revision=current.revision,reason='One scoped retry'))
        return self.get(SubmissionAttempt,attempt.id)

    def stale_retry(self,kind):
        self.retry_setup();self.mutate(kind)
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)

    def test_retry_requires_exact_current_human_scope(self):
        previous=self.retry_setup()
        self.ready();identity=self.ok(self.worker.reserve_submission(self.app_id)).entity_id
        context=self.boundary.capture_context(identity)
        authorization=self.boundary.authorize_submission(context)
        self.boundary.consume_submission(authorization)
        self.assertEqual(self.get(SubmissionAttempt,identity).action_state,'execution_started')
        self.assertEqual(self.get(SubmissionAttempt,identity).context['retry_of'],previous.id)

    def test_retry_candidate_change_invalidates_grant(self): self.stale_retry('candidate')
    def test_retry_job_change_invalidates_grant(self): self.stale_retry('job')
    def test_retry_resume_change_invalidates_grant(self): self.stale_retry('resume')
    def test_retry_resume_hash_change_invalidates_grant(self): self.stale_retry('resume_bytes')
    def test_retry_form_change_invalidates_grant(self): self.stale_retry('form')
    def test_retry_question_change_invalidates_grant(self): self.stale_retry('question')
    def test_retry_answer_change_invalidates_grant(self): self.stale_retry('answer')

    def test_confirmed_not_submitted_without_human_retry_grant_blocks(self):
        attempt=self.reserve()
        self.ok(self.human.reconcile_submission(attempt.id,expected_revision=1,outcome='confirmed_not_submitted',evidence=EVIDENCE,reason='No submission'))
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)

    def test_uncertain_attempt_cannot_receive_retry_grant(self):
        attempt=self.reserve()
        self.assertTrue(self.human.authorize_retry(attempt.id,expected_revision=1,reason='Probably failed').blocked)

    def test_fresh_human_reauthorization_can_replace_stale_unconsumed_grant(self):
        prior=self.retry_setup()
        job=self.get(Job,self.job.id);job.title='Updated';self.save(job,update=True)
        self.review()
        answer=self.get(ApprovedAnswer,self.answer_record.id)
        self.ok(self.human.approve_answer(answer.id,requirements_id=self.req.id,question_id='q-0',expected_revision=answer.revision,reason='Updated job checked'))
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)
        self.ok(self.human.authorize_retry(prior.id,expected_revision=prior.revision,reason='Fresh scoped grant'))
        self.ready();self.ok(self.worker.reserve_submission(self.app_id))

    def test_worker_cannot_issue_consume_retry_broaden_or_bypass(self):
        attempt=self.reserve()
        dispatcher=TrustedWorkerDispatcher(self.db,WorkerScope('worker',self.candidate.id))
        for command in ('authorize_submission','consume_submission','capture_context','authorize_retry','reserve_submission',
                        'TrustedExecutionBoundary','SubmissionAuthorization','request_external_action','save_library_record'):
            response=json.loads(dispatcher.handle(json.dumps({'command':command,'arguments':{
                'application_id':self.app_id,'attempt_id':attempt.id,'actor_type':'HUMAN',
                'retry_authorization':{'revision_context':'*'}}}).encode()))
            self.assertEqual(response['status'],'rejected',command)
        self.assertEqual(self.get(SubmissionAttempt,attempt.id).action_state,'reserved')

    def test_authorization_from_other_boundary_is_rejected(self):
        _,authorization=self.issue()
        other=TrustedExecutionBoundary(self.db,candidate_id=self.candidate.id,application_id=self.app_id)
        with self.assertRaises(Rejected): other.consume_submission(authorization)

    def test_unissued_reservation_cannot_be_executed_after_real_process_restart(self):
        import os
        import subprocess
        import sys
        from pathlib import Path
        attempt=self.reserve()
        script='''import sys
from pathlib import Path
from job_applier.database.sqlite import SQLiteDatabase
from job_applier.services.execution import TrustedExecutionBoundary
from job_applier.services.foundation import Rejected
boundary=TrustedExecutionBoundary(SQLiteDatabase(Path(sys.argv[1])),candidate_id=sys.argv[2],application_id=sys.argv[3])
try: boundary.capture_context(sys.argv[4])
except Rejected as error:
    assert str(error)=='interrupted_host_requires_reconciliation'
else: raise AssertionError('Restarted process accepted old reservation')
'''
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src'),'PYTHONDONTWRITEBYTECODE':'1'}
        result=subprocess.run([sys.executable,'-B','-c',script,str(self.db.path),self.candidate.id,self.app_id,attempt.id],env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.get(SubmissionAttempt,attempt.id).action_state,'reserved')

    def test_other_unresolved_prior_attempt_blocks(self):
        attempt=self.reserve()
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            repo.add(SubmissionAttempt(application_id=self.app_id,previous_state='ready_to_apply'))
        with self.assertRaisesRegex(Rejected,'prior_attempt_unresolved'):
            self.boundary.capture_context(attempt.id)

    def test_prior_confirmed_submission_blocks_even_with_current_reserved_attempt(self):
        attempt=self.reserve()
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            prior=SubmissionAttempt(application_id=self.app_id,previous_state='ready_to_apply')
            prior.outcome='confirmed_submitted';prior.evidence=EVIDENCE
            prior.resolution=self.human._approval(prior,{'actor_id':'owner','reason':'Legacy confirmation','correlation_id':'fixture'})
            repo.add(prior)
        with self.assertRaisesRegex(Rejected,'already_submitted'):
            self.boundary.capture_context(attempt.id)

    def test_issuance_commit_failure_never_returns_authorization(self):
        attempt=self.reserve();context=self.boundary.capture_context(attempt.id)
        original=self.db.transaction;fail_next=True
        @contextmanager
        def failing(*,capability=None):
            nonlocal fail_next
            with original(capability=capability) as repo:
                yield repo
                if capability is _DOMAIN_WRITE and fail_next:
                    fail_next=False;raise RuntimeError('issuance commit failed')
        with patch.object(self.db,'transaction',failing),self.assertRaises(RuntimeError):
            self.boundary.authorize_submission(context)
        self.assertNotIn('execution_authorization',self.get(SubmissionAttempt,attempt.id).context)
        self.assertFalse(any(event.result=='success' for event in self.rows(AuditEvent,action='authorize_submission')))

    def test_retry_grant_scope_cannot_be_replaced_by_generic_write(self):
        prior=self.retry_setup()
        forged=deepcopy(prior);forged.retry_authorization['revision_context']={'candidate_id':'*'}
        self.assertTrue(self.worker.save_library_record(forged,update=True).blocked)
        self.assertEqual(self.get(SubmissionAttempt,prior.id).retry_authorization,prior.retry_authorization)

    def test_forked_copy_does_not_inherit_execution_authority(self):
        import os
        if not hasattr(os,'fork'): self.skipTest('fork unavailable')
        attempt,authorization=self.issue()
        pid=os.fork()
        if pid==0:
            try:
                self.boundary.consume_submission(authorization)
            except Rejected:
                os._exit(0)
            except BaseException:
                os._exit(2)
            os._exit(1)
        _,status=os.waitpid(pid,0)
        self.assertEqual(os.waitstatus_to_exitcode(status),0)
        self.assertEqual(self.get(SubmissionAttempt,attempt.id).action_state,'reserved')
        self.boundary.consume_submission(authorization)

    def lost_ack_transaction(self):
        original=self.db.transaction
        fail_next=True
        @contextmanager
        def committed_then_failed(*,capability=None):
            nonlocal fail_next
            with original(capability=capability) as repo:
                yield repo
            if capability is _DOMAIN_WRITE and fail_next:
                fail_next=False
                raise RuntimeError('commit succeeded but acknowledgement was lost')
        return committed_then_failed

    def test_lost_issuance_acknowledgement_does_not_allow_replacement(self):
        attempt=self.reserve();context=self.boundary.capture_context(attempt.id)
        with patch.object(self.db,'transaction',self.lost_ack_transaction()),self.assertRaises(RuntimeError):
            self.boundary.authorize_submission(context)
        self.assertIn('execution_authorization',self.get(SubmissionAttempt,attempt.id).context)
        current=self.boundary.capture_context(attempt.id)
        with self.assertRaises(Rejected): self.boundary.authorize_submission(current)

    def test_lost_consumption_acknowledgement_cannot_replay(self):
        attempt,authorization=self.issue()
        with patch.object(self.db,'transaction',self.lost_ack_transaction()),self.assertRaises(RuntimeError):
            self.boundary.consume_submission(authorization)
        self.assertEqual(self.get(SubmissionAttempt,attempt.id).action_state,'execution_started')
        with self.assertRaises(Rejected): self.boundary.consume_submission(authorization)
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)
