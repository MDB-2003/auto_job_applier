"""Local-only regression coverage for the authoritative Phase 1.5 requirements."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from job_applier.database import _DOMAIN_WRITE
from job_applier.database.legacy import SCHEMA_V1
from job_applier.database.migrations import MIGRATIONS, execute_script
from job_applier.database.schema import TABLES
from job_applier.database.sqlite import SQLiteDatabase, SQLiteRepository
from job_applier.models import *
from job_applier.models import ApplicationStatus as A, OutreachStatus as O
from job_applier.models.validation import GLOBAL_REQUIRED_FIELDS
from job_applier.services.foundation import FoundationService
from job_applier.services.human import LocalHumanOperations

EVIDENCE=[{'kind':'local_test_evidence','reference':'fixture:confirmed-outcome','description':'Synthetic fixture, no external connection.'}]


class LocalCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.db=SQLiteDatabase(self.root/'test.sqlite3'); self.db.initialize()
        self.worker=FoundationService(self.db)
        self.human=LocalHumanOperations(self.db,owner_actor_id='test-owner')
        self.candidate=CandidateProfile(personal_information={'full_name':'Test Candidate','email':'test@example.invalid','phone':'test-phone','location':'Test City'},education=[{'degree':'Test degree'}],target_roles=['Test role'],employment_preferences=['full_time'],work_authorization={'status':'authorized'},sponsorship_information={'required':False})
        self.save(self.candidate)
        self.job=Job(title='Test role',company='Test Company',source='local_test',source_job_id='1',requisition_id='REQ-1')
        self.save(self.job)
        self.file=self.root/'resume.txt';self.file.write_text('Synthetic resume fixture')
        self.resume=Resume(candidate_id=self.candidate.id,name='Test',file_path=str(self.file),job_family='test',version='1')
        self.save(self.resume)
        self.app_id=self.ok(self.worker.create_application(self.candidate.id,self.job.id,self.resume.id)).entity_id
        self.ok(self.worker.transition_application(self.app_id,A.SHORTLISTED))
        self.req=ReadinessRequirements(application_id=self.app_id,candidate_id=self.candidate.id,job_id=self.job.id,context={'form_id':'fixture-form','form_version':'1'})
        self.ok(self.worker.save_requirements(self.req)); self.review()

    def ok(self,result):
        self.assertFalse(result.blocked,result.reasons); return result

    def save(self,item,update=False): return self.ok(self.worker.save_library_record(item,update=update))

    def get(self,model,identity):
        with self.db.transaction() as repo: return repo.get(model,identity)

    def rows(self,model,**filters):
        with self.db.transaction() as repo: return repo.list(model,limit=1000,**filters)

    def review(self):
        req=self.get(ReadinessRequirements,self.req.id)
        return self.ok(self.human.review_requirements(req.id,expected_revision=req.revision,reason='Owner reviewed requirements'))

    def questions(self,questions,fields=None):
        req=self.get(ReadinessRequirements,self.req.id); req.approval=None
        req.questions=[{'question_id':f'q-{i}','context':{},**q} for i,q in enumerate(questions)]
        if fields is not None: req.required_candidate_fields=fields
        self.ok(self.worker.save_requirements(req,update=True));self.review()

    def answer(self,question='Test question?',approve=True):
        item=ApprovedAnswer(candidate_id=self.candidate.id,question=question,answer='Human supplied answer',category='test',source='local_owner')
        self.save(item)
        self.questions([{'text':question}])
        if approve: self.ok(self.human.approve_answer(item.id,requirements_id=self.req.id,question_id='q-0',expected_revision=1,reason='Owner reviewed exact answer'))
        return self.get(ApprovedAnswer,item.id)

    def ready(self): return self.ok(self.worker.prepare_application(self.app_id))

    def uncertain(self):
        self.ready(); result=self.ok(self.worker.reserve_submission(self.app_id))
        return self.get(SubmissionAttempt,result.entity_id)

    def contact(self):
        contact=Contact(name='Test contact',company='Test Company',email='contact@example.invalid')
        self.save(contact)
        self.ok(self.human.verify_contact(contact.id,expected_revision=1,evidence=EVIDENCE,reason='Owner checked evidence'))
        return self.get(Contact,contact.id)


class ApprovalTests(LocalCase):
    def test_worker_has_no_approval_or_reconciliation_operations(self):
        for name in ('approve_answer','review_requirements','reconcile_submission','authorize_retry','resolve_candidate_information'):
            self.assertFalse(hasattr(self.worker,name))

    def test_ordinary_create_cannot_self_approve(self):
        answer=ApprovedAnswer(candidate_id=self.candidate.id,question='Q?',answer='Proposed',category='test',source='worker',approval_state=ApprovalState.APPROVED)
        self.assertTrue(self.worker.save_library_record(answer).blocked)
        self.assertEqual(self.rows(ApprovedAnswer),[])

    def test_ordinary_edit_cannot_self_approve(self):
        answer=self.answer(approve=False); answer.approval_state=ApprovalState.APPROVED
        self.assertTrue(self.worker.save_library_record(answer,update=True).blocked)
        self.assertEqual(self.get(ApprovedAnswer,answer.id).approval_state,ApprovalState.DRAFT)

    def test_owner_approval_records_required_metadata(self):
        answer=self.answer()
        self.assertEqual(answer.approval['actor_type'],'HUMAN')
        self.assertEqual(answer.approval['actor_id'],'test-owner')
        self.assertEqual(answer.approval['previous_revision'],1)
        self.assertEqual(answer.approval['new_revision'],2)
        self.assertEqual(answer.approval['approved_content_revision'],answer.content_revision)
        for key in ('timestamp','reason','source','correlation_id'): self.assertTrue(answer.approval[key])
        events=self.rows(AuditEvent,action='approve_answer',entity_id=answer.id)
        self.assertTrue(any(e.previous_state=='draft' and e.new_state=='approved' and 'approval' in e.metadata for e in events))

    def test_owner_requires_reason_and_current_revision(self):
        answer=self.answer(approve=False)
        self.assertTrue(self.human.approve_answer(answer.id,requirements_id=self.req.id,question_id='q-0',expected_revision=1,reason='').blocked)
        self.assertTrue(self.human.approve_answer(answer.id,expected_revision=99,reason='Review').blocked)

    def test_editing_answer_invalidates_approval_and_readiness(self):
        answer=self.answer(); self.questions([{'text':answer.question}]); self.ready()
        answer.approval_state=ApprovalState.DRAFT;answer.approval=None;answer.answer='Edited proposal'
        self.save(answer,update=True)
        changed=self.get(ApprovedAnswer,answer.id)
        self.assertEqual(changed.content_revision,2);self.assertIsNone(changed.approval)
        self.assertEqual(self.get(Application,self.app_id).status,A.BLOCKED)
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)

    def test_revoked_answer_cannot_be_used(self):
        answer=self.answer();self.questions([{'text':answer.question}])
        self.ok(self.human.revoke_answer(answer.id,expected_revision=answer.revision,reason='Owner revoked'))
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)

    def test_draft_high_confidence_does_not_approve(self):
        answer=self.answer(approve=False);answer.confidence=1;self.save(answer,update=True)
        self.questions([{'text':answer.question}])
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)

    def test_another_candidate_answer_does_not_match(self):
        other=CandidateProfile();self.save(other)
        answer=ApprovedAnswer(candidate_id=other.id,question='Other?',answer='No',category='test',source='owner');self.save(answer)
        other_app=self.ok(self.worker.create_application(other.id,self.job.id)).entity_id
        other_req=ReadinessRequirements(application_id=other_app,candidate_id=other.id,job_id=self.job.id,
            context={'form_id':'fixture-form','form_version':'1'},questions=[{'question_id':'q-0','text':answer.question,'context':{}}])
        self.ok(self.worker.save_requirements(other_req))
        self.ok(self.human.approve_answer(answer.id,requirements_id=other_req.id,question_id='q-0',expected_revision=1,reason='Review'))
        self.questions([{'text':answer.question}]); self.assertTrue(self.worker.prepare_application(self.app_id).blocked)

    def test_questions_require_exact_text(self):
        answer=self.answer();self.questions([{'text':answer.question.lower()}])
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)


class ReadinessTests(LocalCase):
    def test_global_requirements_are_enforced_without_caller_list(self):
        candidate=self.get(CandidateProfile,self.candidate.id);candidate.personal_information={}
        self.save(candidate,update=True);self.review()
        result=self.worker.prepare_application(self.app_id)
        self.assertTrue(result.blocked)
        self.assertIn('missing_candidate_field:personal_information.full_name',result.reasons)

    def test_each_global_requirement_is_required(self):
        from job_applier.models.validation import fact
        for path in GLOBAL_REQUIRED_FIELDS:
            with self.subTest(path=path):
                original=self.get(CandidateProfile,self.candidate.id)
                item=deepcopy(original);parts=path.split('.')
                if len(parts)==1: setattr(item,parts[0],[])
                else: getattr(item,parts[0])[parts[1]]=None
                self.save(item,update=True);self.review()
                self.assertIn('missing_candidate_field:'+path,self.worker.prepare_application(self.app_id).reasons)
                original.revision=self.get(CandidateProfile,self.candidate.id).revision
                self.save(original,update=True)

    def test_false_sponsorship_and_optional_fields_do_not_block(self):
        self.ready()
        self.assertEqual(self.get(Application,self.app_id).status,A.READY_TO_APPLY)

    def test_optional_graduation_date_is_not_globally_required(self):
        candidate=self.get(CandidateProfile,self.candidate.id)
        candidate.education=[{'degree':'Owner-supplied degree','graduation_date':None}]
        self.save(candidate,update=True);self.review();self.ready()

    def test_empty_salary_object_does_not_satisfy_specific_requirement(self):
        self.questions([],fields=['salary_preferences'])
        self.assertIn('missing_candidate_field:salary_preferences',self.worker.prepare_application(self.app_id).reasons)

    def test_internal_fields_cannot_satisfy_requirements(self):
        for path in ('id','created_at','updated_at','revision','personal_information.id'):
            req=self.get(ReadinessRequirements,self.req.id);req.approval=None;req.required_candidate_fields=[path]
            self.assertTrue(self.worker.save_requirements(req,update=True).blocked)

    def test_required_job_specific_field_blocks_until_present(self):
        self.questions([],fields=['salary_preferences.minimum'])
        self.assertIn('missing_candidate_field:salary_preferences.minimum',self.worker.prepare_application(self.app_id).reasons)

    def test_requirements_must_be_persisted_and_human_reviewed(self):
        req=self.get(ReadinessRequirements,self.req.id);req.approval=None
        self.ok(self.worker.save_requirements(req,update=True))
        self.assertIn('requirements_not_reviewed_or_stale',self.worker.prepare_application(self.app_id).reasons)

    def test_unknown_question_stores_text_and_context(self):
        context={'form':'local-fixture','field_id':'q-1'}
        self.questions([{'text':'Exact unknown question?', 'context':context}])
        result=self.worker.prepare_application(self.app_id)
        self.assertTrue(result.blocked);self.assertEqual(result.answers,{})
        unresolved=self.rows(UnresolvedInformation,application_id=self.app_id)[0]
        self.assertEqual(unresolved.question_text,'Exact unknown question?')
        self.assertEqual(unresolved.context['question_context'],context);self.assertEqual(unresolved.job_id,self.job.id)
        self.assertTrue(unresolved.created_at.endswith('+00:00'))
        self.assertEqual(self.get(Application,self.app_id).status,A.BLOCKED_UNKNOWN_QUESTION)

    def test_unresolved_question_preserves_shared_form_context(self):
        req=self.get(ReadinessRequirements,self.req.id);req.approval=None
        req.context={'form_id':'local-form','form_version':'1'};req.questions=[{'question_id':'q-0','text':'Unknown?', 'context':{'field_id':'q1'}}]
        self.ok(self.worker.save_requirements(req,update=True));self.review()
        self.worker.prepare_application(self.app_id)
        unresolved=self.rows(UnresolvedInformation,application_id=self.app_id)[0]
        self.assertEqual(unresolved.context['form_context'],{'form_id':'local-form','form_version':'1'})
        self.assertEqual(unresolved.context['question_context'],{'field_id':'q1'})

    def test_no_partial_answers_when_any_question_unknown(self):
        answer=self.answer();self.questions([{'text':answer.question},{'text':'Unknown'}])
        self.assertEqual(self.worker.prepare_application(self.app_id).answers,{})

    def test_unknown_question_repair_is_audited(self):
        self.questions([{'text':'Test question?'}]);self.worker.prepare_application(self.app_id)
        unresolved=self.rows(UnresolvedInformation,application_id=self.app_id)[0]
        answer=self.answer()
        self.ok(self.human.resolve_unknown_question(unresolved.id,answer.id,reason='Matched owner-approved answer'))
        self.assertTrue(self.get(UnresolvedInformation,unresolved.id).resolved)
        self.ready()
        self.assertTrue(self.rows(AuditEvent,action='resolve_unknown_question'))

    def test_missing_information_repair_is_audited(self):
        candidate=self.get(CandidateProfile,self.candidate.id);candidate.personal_information['phone']=None
        self.save(candidate,update=True);self.review();self.worker.prepare_application(self.app_id)
        candidate=self.get(CandidateProfile,self.candidate.id)
        self.ok(self.human.resolve_candidate_information(candidate.id,field_path='personal_information.phone',value='owner-provided',expected_revision=candidate.revision,reason='Owner supplied phone'))
        resolved=self.rows(UnresolvedInformation,application_id=self.app_id,field_path='personal_information.phone')[0]
        self.assertTrue(resolved.resolved);self.assertEqual(resolved.resolution['actor_type'],'HUMAN')
        self.review();self.ready()

    def test_conflicts_cannot_be_cleared_by_worker(self):
        candidate=self.get(CandidateProfile,self.candidate.id);candidate.conflicts={'work_authorization.status':['yes','no']}
        self.save(candidate,update=True)
        candidate=self.get(CandidateProfile,candidate.id);candidate.conflicts={}
        self.assertTrue(self.worker.save_library_record(candidate,update=True).blocked)
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)
        candidate=self.get(CandidateProfile,candidate.id)
        self.ok(self.human.resolve_candidate_information(candidate.id,field_path='work_authorization.status',value='authorized',expected_revision=candidate.revision,resolve_conflict=True,reason='Owner resolved conflicting evidence'))
        self.review();self.ready()

    def test_candidate_change_revokes_answer_and_readiness(self):
        answer=self.answer();self.ready()
        candidate=self.get(CandidateProfile,self.candidate.id);candidate.personal_information['location']='New City'
        self.save(candidate,update=True)
        self.assertEqual(self.get(ApprovedAnswer,answer.id).approval_state,ApprovalState.REVOKED)
        self.assertEqual(self.get(Application,self.app_id).status,A.BLOCKED)
        self.assertIsNone(self.get(ReadinessRequirements,self.req.id).approval)

    def test_job_change_invalidates_review_and_readiness(self):
        self.ready();job=self.get(Job,self.job.id);job.title='Changed title';self.save(job,update=True)
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)

    def test_resume_content_change_is_detected(self):
        self.ready();self.file.write_text('Changed after selection')
        self.assertIn('resume_content_changed',self.worker.prepare_application(self.app_id).reasons)

    def test_resume_repair_requires_current_revision_and_is_audited(self):
        resume=self.get(Resume,self.resume.id);resume.version='2';self.save(resume,update=True)
        self.assertIn('stale_or_invalid_resume_selection',self.worker.prepare_application(self.app_id).reasons)
        self.ok(self.worker.assign_resume(self.app_id,resume.id));self.ready()
        self.assertEqual(self.get(Application,self.app_id).resume_revision,2)
        self.assertTrue(self.rows(AuditEvent,action='assign_resume'))

    def test_missing_resume_can_be_assigned_without_recreating_application(self):
        job=Job(title='Other role',company='Test',source='local',source_job_id='other');self.save(job)
        app=self.ok(self.worker.create_application(self.candidate.id,job.id)).entity_id
        self.ok(self.worker.assign_resume(app,self.resume.id))
        self.assertEqual(self.get(Application,app).resume_id,self.resume.id)

    def test_stale_record_update_is_rejected(self):
        first=self.get(CandidateProfile,self.candidate.id);stale=deepcopy(first)
        first.skills=['one'];self.save(first,update=True)
        stale.skills=['two'];self.assertTrue(self.worker.save_library_record(stale,update=True).blocked)
        self.assertEqual(self.get(CandidateProfile,first.id).skills,['one'])


class SubmissionTests(LocalCase):
    def test_uncertain_cannot_escape_via_generic_states(self):
        attempt=self.uncertain()
        for target in (A.BLOCKED,A.SHORTLISTED,A.READY_TO_APPLY,A.SUBMITTED,A.CLOSED):
            self.assertTrue(self.worker.transition_application(self.app_id,target).blocked)
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)
        self.assertTrue(self.worker.record_submission_uncertainty(self.app_id).blocked)
        self.assertEqual(self.get(Application,self.app_id).status,A.SUBMISSION_UNCERTAIN)
        self.assertEqual(attempt.previous_state,'ready_to_apply')

    def test_inconclusive_evidence_preserves_uncertain_state(self):
        attempt=self.uncertain()
        result=self.human.reconcile_submission(attempt.id,expected_revision=1,outcome='uncertain',evidence=EVIDENCE,reason='Evidence inconclusive')
        self.assertTrue(result.blocked)
        self.assertEqual(self.get(Application,self.app_id).status,A.SUBMISSION_UNCERTAIN)
        self.assertTrue(self.human.authorize_retry(attempt.id,expected_revision=2,reason='Retry').blocked)

    def test_confirmed_submission_never_authorizes_retry(self):
        attempt=self.uncertain()
        self.ok(self.human.reconcile_submission(attempt.id,expected_revision=1,outcome='confirmed_submitted',evidence=EVIDENCE,reason='Owner saw confirmation'))
        self.assertEqual(self.get(Application,self.app_id).status,A.SUBMITTED)
        self.assertTrue(self.human.authorize_retry(attempt.id,expected_revision=2,reason='Retry').blocked)
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)

    def test_non_submission_requires_separate_human_retry_authorization(self):
        attempt=self.uncertain()
        self.ok(self.human.reconcile_submission(attempt.id,expected_revision=1,outcome='confirmed_not_submitted',evidence=EVIDENCE,reason='Owner verified no submission'))
        self.assertEqual(self.get(Application,self.app_id).status,A.CONFIRMED_NOT_SUBMITTED)
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)
        self.ok(self.human.authorize_retry(attempt.id,expected_revision=2,reason='Owner authorizes one new attempt'))
        self.ready()
        second=self.ok(self.worker.reserve_submission(self.app_id))
        self.assertNotEqual(second.entity_id,attempt.id)
        self.assertTrue(self.get(SubmissionAttempt,attempt.id).retry_consumed)
        events=self.rows(AuditEvent,action='authorize_retry',entity_id=attempt.id)
        self.assertTrue(any(e.metadata.get('evidence')==EVIDENCE and e.actor_type=='HUMAN' for e in events))

    def test_reconciliation_requires_evidence(self):
        attempt=self.uncertain()
        self.assertTrue(self.human.reconcile_submission(attempt.id,expected_revision=1,outcome='confirmed_not_submitted',evidence=[],reason='Guess').blocked)
        self.assertEqual(self.get(Application,self.app_id).status,A.SUBMISSION_UNCERTAIN)

    def test_reported_failure_is_not_proof_of_non_submission(self):
        self.ready();self.ok(self.worker.reserve_submission(self.app_id))
        self.worker.record_submission_failure(self.app_id,outcome_uncertain=False)
        self.assertEqual(self.get(Application,self.app_id).status,A.SUBMISSION_UNCERTAIN)

    def test_external_actions_remain_unconditionally_disabled(self):
        self.ready()
        self.assertTrue(self.worker.request_external_action(Application,self.app_id,action='submit').blocked)
        self.assertTrue(self.worker.transition_application(self.app_id,A.SUBMITTED).blocked)


class AuditAndPersistenceTests(LocalCase):
    def test_insert_or_replace_update_and_delete_are_blocked(self):
        event=self.rows(AuditEvent)[0]
        with sqlite3.connect(self.db.path) as con:
            con.execute('PRAGMA recursive_triggers=OFF')  # Must protect even without it.
            row=con.execute('SELECT * FROM audit_logs WHERE id=?',(event.id,)).fetchone()
            for statement,params in (("INSERT OR REPLACE INTO audit_logs VALUES (?,?,?,?,?,?,?,?)",row),
                ("UPDATE audit_logs SET payload=payload WHERE id=?",(event.id,)),
                ("DELETE FROM audit_logs WHERE id=?",(event.id,))):
                with self.assertRaises(sqlite3.IntegrityError):con.execute(statement,params)
        self.assertEqual(self.get(AuditEvent,event.id),event)

    def test_invalid_status_is_audited(self):
        result=self.worker.transition_application(self.app_id,'invented-status')
        self.assertTrue(result.blocked)
        events=self.rows(AuditEvent,action='transition_application',result='rejected')
        self.assertTrue(any(e.error=='invalid_status' for e in events))

    def test_duplicate_insertion_is_audited(self):
        self.assertTrue(self.worker.save_library_record(self.candidate).blocked)
        self.assertTrue(self.rows(AuditEvent,action='create_record',result='rejected'))

    def test_failed_validation_is_audited(self):
        invalid=Contact(name='')
        self.assertTrue(self.worker.save_library_record(invalid).blocked)
        self.assertTrue(self.rows(AuditEvent,entity_id=invalid.id,result='rejected'))

    def test_rollback_has_no_success_event_or_runtime_success(self):
        original=SQLiteRepository.add
        def fail_once(repo,record):
            if isinstance(record,AuditEvent) and record.result=='success' and record.action=='create_record':
                raise RuntimeError('simulated audit storage failure')
            return original(repo,record)
        contact=Contact(name='Rollback fixture')
        with patch.object(SQLiteRepository,'add',fail_once), self.assertLogs('job_applier.services.foundation',level='WARNING') as captured:
            with self.assertRaises(RuntimeError):self.worker.save_library_record(contact)
        self.assertEqual(self.rows(Contact,id=contact.id),[])
        self.assertFalse(any('result=success' in line for line in captured.output))
        self.assertTrue(self.rows(AuditEvent,entity_id=contact.id,result='failed'))

    def test_commit_failure_rolls_back_and_reports_no_success(self):
        original=self.db.transaction
        first=True
        @contextmanager
        def fail_first_commit(*,capability=None):
            nonlocal first
            with original(capability=capability) as repo:
                yield repo
                if capability is _DOMAIN_WRITE and first:
                    first=False
                    raise RuntimeError('simulated commit failure')
        contact=Contact(name='Commit failure fixture')
        with patch.object(self.db,'transaction',fail_first_commit), self.assertLogs('job_applier.services.foundation',level='INFO') as captured:
            with self.assertRaises(RuntimeError): self.worker.save_library_record(contact)
        self.assertEqual(self.rows(Contact,id=contact.id),[])
        self.assertEqual(self.rows(AuditEvent,entity_id=contact.id,result='success'),[])
        self.assertTrue(self.rows(AuditEvent,entity_id=contact.id,result='failed'))
        self.assertFalse(any('result=success' in line for line in captured.output))

    def test_related_events_share_correlation_id(self):
        answer=self.answer()
        events=self.rows(AuditEvent,action='approve_answer',entity_id=answer.id)
        self.assertEqual(len({e.correlation_id for e in events}),1)

    def test_public_repository_writes_are_denied(self):
        with self.db.transaction() as repo:
            with self.assertRaises(PermissionError):repo.add(Contact(name='Bypass'))
            with self.assertRaises(PermissionError):repo.update(self.get(CandidateProfile,self.candidate.id))

    def test_raw_business_writes_require_domain_boundary(self):
        with sqlite3.connect(self.db.path) as con:
            con.create_function('domain_write_allowed',0,lambda:0)
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('DELETE FROM candidate_profiles WHERE id=?',(self.candidate.id,))

    def test_ids_and_payloads_reject_null_and_invalid_json(self):
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            con=repo._connection
            for identity,payload in ((None,'{}'),('x','not json'),('x','{}')):
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute('INSERT INTO contacts(id,revision,payload) VALUES (?,1,?)',(identity,payload))

    def test_invalid_model_types_and_states_are_rejected(self):
        candidate=self.get(CandidateProfile,self.candidate.id);candidate.education='made-up-string'
        self.assertTrue(self.worker.save_library_record(candidate,update=True).blocked)
        answer=ApprovedAnswer(candidate_id=self.candidate.id,question='Q',answer='A',category='test',source='test')
        answer.approval_state='invented'
        self.assertTrue(self.worker.save_library_record(answer).blocked)

    def test_read_transactions_use_begin_and_allow_writer(self):
        trace=[];original=self.db._connect
        def traced(*,writable=False):
            con=original(writable=writable);con.set_trace_callback(trace.append);return con
        with patch.object(self.db,'_connect',traced):
            with self.db.transaction() as repo:
                self.assertEqual(repo.count(Application),1)
                other=original(writable=True)
                try: other.execute('BEGIN IMMEDIATE');other.rollback()
                finally:other.close()
        self.assertIn('BEGIN',trace);self.assertNotIn('BEGIN IMMEDIATE',trace)

    def test_read_does_not_create_missing_database(self):
        path=self.root/'absent.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            with SQLiteDatabase(path).transaction(): pass
        self.assertFalse(path.exists())

    def test_queries_are_bounded_and_use_sql_filters(self):
        with self.db.transaction() as repo:
            trace=[];repo._connection.set_trace_callback(trace.append)
            self.assertEqual(len(repo.list(Application,candidate_id=self.candidate.id,limit=1)),1)
            self.assertTrue(any('WHERE candidate_id IS' in sql and 'LIMIT 1' in sql for sql in trace))
            with self.assertRaises(ValueError):repo.list(Application,limit=1001)
            with self.assertRaises(ValueError):repo.list(Application,offset=-1)

    def test_stale_revision_cannot_overwrite(self):
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            item=repo.get(CandidateProfile,self.candidate.id)
            with self.assertRaises(ValueError):repo.update(item)

    def test_ownership_constraints_reject_wrong_resume(self):
        other=CandidateProfile();self.save(other)
        resume=Resume(candidate_id=other.id,name='Other',file_path=str(self.file),job_family='test',version='1');self.save(resume)
        self.assertTrue(self.worker.assign_resume(self.app_id,resume.id).blocked)
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            app=repo.get(Application,self.app_id);app.resume_id=resume.id;app.revision+=1
            with self.assertRaises(ValueError):repo.update(app)

    def test_requirements_ownership_is_enforced(self):
        other=CandidateProfile();self.save(other)
        req=ReadinessRequirements(application_id=self.app_id,candidate_id=other.id,job_id=self.job.id)
        self.assertTrue(self.worker.save_requirements(req).blocked)

    def test_outreach_ownership_is_enforced(self):
        other=CandidateProfile();self.save(other);contact=self.contact()
        self.assertTrue(self.worker.create_outreach(other.id,contact.id,application_id=self.app_id).blocked)


class JobAndContactTests(LocalCase):
    def test_strong_evidence_links_sources_to_one_canonical(self):
        source=Job(title='Other source title',company='Test Company',source='second_source',source_job_id='2',requisition_id='REQ-1')
        self.save(source);target=self.get(Job,self.job.id).canonical_id
        self.ok(self.worker.link_job_source(source.id,target,evidence={'reference':'fixture:REQ-1','description':'Same exact company and requisition ID'}))
        self.assertEqual(self.get(Job,source.id).canonical_id,target)
        result=self.worker.create_application(self.candidate.id,source.id)
        self.assertTrue(result.blocked);self.assertEqual(result.entity_id,self.app_id)
        self.assertTrue(self.rows(JobLinkEvidence,source_record_id=source.id,method='strong_identity'))

    def test_exact_company_and_canonical_url_can_link(self):
        first=Job(title='URL role',company='URL Company',source='local-a',source_job_id='url-a',canonical_url='https://example.invalid/jobs/123')
        second=Job(title='URL role',company='URL Company',source='local-b',source_job_id='url-b',canonical_url='https://example.invalid/jobs/123')
        self.save(first);self.save(second)
        target=self.get(Job,first.id).canonical_id
        self.ok(self.worker.link_job_source(second.id,target,evidence={'reference':'local:matching-url','description':'Exact company and canonical URL'}))
        evidence=self.rows(JobLinkEvidence,source_record_id=second.id,method='strong_identity')[0].evidence
        self.assertEqual(evidence['compared_identity']['canonical_url'],first.canonical_url)

    def test_source_identity_cannot_change_under_existing_link(self):
        job=self.get(Job,self.job.id);job.requisition_id='DIFFERENT-OPENING'
        self.assertTrue(self.worker.save_library_record(job,update=True).blocked)
        self.assertEqual(self.get(Job,self.job.id).requisition_id,'REQ-1')

    def test_ambiguous_link_requires_human_confirmation(self):
        source=Job(title='Similar role',company='Different Company',source='second',source_job_id='2');self.save(source)
        target=self.get(Job,self.job.id).canonical_id;evidence={'reference':'fixture:manual','description':'Owner checked identities'}
        self.assertTrue(self.worker.link_job_source(source.id,target,evidence=evidence).blocked)
        self.ok(self.human.confirm_job_equivalence(source.id,target,evidence=evidence,reason='Owner confirmed same opening'))
        links=self.rows(JobLinkEvidence,source_record_id=source.id,method='human_confirmation')
        self.assertEqual(links[0].approval['actor_type'],'HUMAN')

    def test_duplicate_applications_concurrently_block(self):
        job=Job(title='Concurrent',company='Test',source='test',source_job_id='parallel');self.save(job)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.worker.create_application(self.candidate.id,job.id),range(4)))
        self.assertEqual(sum(not r.blocked for r in results),1)
        self.assertEqual(len({r.entity_id for r in results}),1)

    def test_unverified_contact_blocks_outreach(self):
        contact=Contact(name='Unverified',email='test@example.invalid');self.save(contact)
        oid=self.ok(self.worker.create_outreach(self.candidate.id,contact.id)).entity_id
        self.assertTrue(self.worker.transition_outreach(oid,O.CONTACT_VERIFIED).blocked)
        self.assertEqual(self.get(Outreach,oid).status,O.BLOCKED)

    def test_contact_change_invalidates_verification_and_outreach(self):
        contact=self.contact();oid=self.ok(self.worker.create_outreach(self.candidate.id,contact.id)).entity_id
        self.ok(self.worker.transition_outreach(oid,O.CONTACT_VERIFIED))
        self.ok(self.worker.transition_outreach(oid,O.DRAFT_CREATED,draft='Owner draft'))
        outreach=self.get(Outreach,oid)
        self.ok(self.human.approve_outreach(oid,expected_revision=outreach.revision,reason='Owner approved draft'))
        contact.company='Changed company';self.save(contact,update=True)
        self.assertFalse(self.get(Contact,contact.id).verified)
        self.assertEqual(self.get(Outreach,oid).status,O.BLOCKED)

    def test_contact_verification_has_no_time_expiry_and_can_be_revoked(self):
        contact=self.contact();self.assertNotIn('expires_at',contact.verification)
        self.ok(self.human.revoke_contact_verification(contact.id,expected_revision=contact.revision,reason='Evidence superseded'))
        self.assertFalse(self.get(Contact,contact.id).verified)

    def test_worker_cannot_fabricate_contact_verification(self):
        contact=Contact(name='Test',email='test@example.invalid',verified=True)
        self.assertTrue(self.worker.save_library_record(contact).blocked)

    def test_outreach_cannot_be_sent(self):
        contact=self.contact();oid=self.ok(self.worker.create_outreach(self.candidate.id,contact.id)).entity_id
        self.ok(self.worker.transition_outreach(oid,O.CONTACT_VERIFIED));self.ok(self.worker.transition_outreach(oid,O.DRAFT_CREATED,draft='Local draft'))
        self.assertTrue(self.worker.transition_outreach(oid,O.APPROVED).blocked)
        item=self.get(Outreach,oid);self.ok(self.human.approve_outreach(oid,expected_revision=item.revision,reason='Owner approved'))
        self.assertTrue(self.worker.transition_outreach(oid,O.SENT).blocked)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'legacy.db'
        con=sqlite3.connect(self.path);con.executescript(SCHEMA_V1);con.execute('PRAGMA user_version=1');con.close()

    def insert_legacy(self,con,table,record,projected=()):
        data=asdict(record)
        # Genuine v1-shaped payload: remove Phase 1.5 fields.
        for key in ('revision','content_revision','approval','candidate_revision','canonical_id','canonical_url','requisition_id','provenance','resume_revision','readiness','latest_attempt_id','content_hash'):
            data.pop(key,None)
        values=(record.id,json.dumps(data),*(getattr(record,p) for p in projected))
        con.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})",values)

    def test_existing_data_migrates_and_legacy_approval_is_not_trusted(self):
        candidate=CandidateProfile();job=Job(title='Legacy',company='Test',source='legacy',source_job_id='1')
        answer=ApprovedAnswer(candidate_id=candidate.id,question='Q',answer='A',category='test',source='legacy',approval_state=ApprovalState.APPROVED)
        app=Application(candidate_id=candidate.id,job_id=job.id,status=A.READY_TO_APPLY)
        with sqlite3.connect(self.path) as con:
            for table,item,columns in [('candidate_profiles',candidate,()),('jobs',job,('source','source_job_id')),('approved_answers',answer,('candidate_id','question')),('applications',app,('candidate_id','job_id','resume_id'))]:
                self.insert_legacy(con,table,item,columns)
        db=SQLiteDatabase(self.path);db.initialize();db.initialize()
        with db.transaction() as repo:
            self.assertEqual(repo.get(ApprovedAnswer,answer.id).approval_state,ApprovalState.DRAFT)
            self.assertEqual(repo.get(Application,app.id).status,A.BLOCKED)
            self.assertIsNotNone(repo.get(Job,job.id).canonical_id)
            self.assertEqual(repo.count(CanonicalOpening),1)
        with sqlite3.connect(self.path) as con:self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],3)

    def test_migration_failure_rolls_back_schema_and_data(self):
        candidate=CandidateProfile()
        with sqlite3.connect(self.path) as con:self.insert_legacy(con,'candidate_profiles',candidate)
        original=MIGRATIONS[2]
        def fail(con):original(con);raise RuntimeError('simulated migration failure')
        with patch.dict(MIGRATIONS,{2:fail}),self.assertRaises(RuntimeError):SQLiteDatabase(self.path).initialize()
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual(con.execute('SELECT id FROM candidate_profiles').fetchone()[0],candidate.id)
            self.assertNotIn('revision',[r[1] for r in con.execute('PRAGMA table_info(candidate_profiles)')])

    def test_legacy_failure_becomes_uncertain(self):
        candidate=CandidateProfile();job=Job(title='Legacy',company='Test',source='legacy',source_job_id='1');app=Application(candidate_id=candidate.id,job_id=job.id,status=A.SUBMISSION_FAILED)
        with sqlite3.connect(self.path) as con:
            self.insert_legacy(con,'candidate_profiles',candidate)
            self.insert_legacy(con,'jobs',job,('source','source_job_id'))
            self.insert_legacy(con,'applications',app,('candidate_id','job_id','resume_id'))
        db=SQLiteDatabase(self.path);db.initialize()
        with db.transaction() as repo:
            result=repo.get(Application,app.id)
            self.assertEqual(result.status,A.SUBMISSION_UNCERTAIN)
            self.assertEqual(repo.get(SubmissionAttempt,result.latest_attempt_id).outcome,'uncertain')

    def test_fresh_migrations_run_in_order(self):
        path=Path(self.temp.name)/'fresh.sqlite3'
        calls=[]; originals=dict(MIGRATIONS)
        def wrapped(version):
            def run(con):calls.append(version);originals[version](con)
            return run
        with patch.dict(MIGRATIONS,{v:wrapped(v) for v in originals}):
            SQLiteDatabase(path).initialize()
        self.assertEqual(calls,[1,2,3])

    def test_future_schema_is_rejected(self):
        with sqlite3.connect(self.path) as con:con.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError):SQLiteDatabase(self.path).initialize()

    def test_invalid_legacy_identity_aborts(self):
        with sqlite3.connect(self.path) as con:con.execute("INSERT INTO contacts VALUES (NULL,'{}')")
        with self.assertRaises(ValueError):SQLiteDatabase(self.path).initialize()
        with sqlite3.connect(self.path) as con:self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],1)


if __name__=='__main__':unittest.main()
