"""Adversarial tests of the exact, human-approved form/question scope."""
from copy import deepcopy
from dataclasses import asdict
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from test_hardening import LocalCase
from job_applier.database import _DOMAIN_WRITE
from job_applier.database.sqlite import SQLiteDatabase
from job_applier.database.schema import UNIQUE
from job_applier.interfaces.worker import TrustedWorkerDispatcher, WorkerScope
from job_applier.models import (
    Application, ApplicationStatus as A, ApprovedAnswer, ApprovalState,
    CandidateProfile, Job, ReadinessRequirements, UnresolvedInformation,
)
from job_applier.services.context import fingerprint


class AnswerContextTests(LocalCase):
    def approved(self):
        return self.answer('Same question?')

    def change_form(self, *, context=None, questions=None):
        req=self.get(ReadinessRequirements,self.req.id)
        req.approval=None
        if context is not None: req.context=context
        if questions is not None: req.questions=questions
        self.ok(self.worker.save_requirements(req,update=True))
        return self.get(ReadinessRequirements,req.id)

    def blocked(self, app_id=None):
        result=self.worker.prepare_application(app_id or self.app_id)
        self.assertTrue(result.blocked)
        self.assertEqual(result.answers,{})
        return result

    def another_application(self, company):
        job=Job(company=company,title='Another role',source='fixture',source_job_id='other',requisition_id='REQ-OTHER')
        self.save(job)
        app_id=self.ok(self.worker.create_application(self.candidate.id,job.id,self.resume.id)).entity_id
        self.ok(self.worker.transition_application(app_id,A.SHORTLISTED))
        req=ReadinessRequirements(application_id=app_id,candidate_id=self.candidate.id,job_id=job.id,
            context={'form_id':'fixture-form','form_version':'1'},
            questions=[{'question_id':'q-0','text':'Same question?','context':{}}])
        self.ok(self.worker.save_requirements(req))
        self.ok(self.human.review_requirements(req.id,expected_revision=1,reason='Owner reviewed other form'))
        return app_id,req

    def test_exact_scope_reuses_approved_answer(self):
        answer=self.approved()
        result=self.ready()
        self.assertEqual(result.answers,{fingerprint(answer.scope):answer.answer})
        self.assertEqual(answer.scope['application_id'],self.app_id)
        self.assertEqual(answer.scope['employer'],self.job.company)
        self.assertEqual(answer.scope['question_id'],'q-0')
        self.assertEqual(answer.approval['scope_fingerprint'],fingerprint(answer.scope))

    def test_identical_text_different_employer_is_not_reused(self):
        self.approved()
        app_id,_=self.another_application('Other Employer')
        self.assertIn('unknown_or_unapproved_question',self.blocked(app_id).reasons)

    def test_identical_text_same_employer_different_job_is_not_reused(self):
        self.approved()
        app_id,_=self.another_application(self.job.company)
        self.assertIn('unknown_or_unapproved_question',self.blocked(app_id).reasons)

    def test_identical_text_different_form_is_not_reused(self):
        self.approved()
        self.change_form(context={'form_id':'other-form','form_version':'1'})
        self.review()
        self.assertIn('unknown_or_unapproved_question',self.blocked().reasons)

    def test_identical_text_different_occurrence_is_not_reused(self):
        self.approved()
        self.change_form(questions=[{'question_id':'q-other','text':'Same question?','context':{}}])
        self.review()
        self.blocked()

    def test_same_text_twice_produces_distinct_unresolved_and_answer_identities(self):
        questions=[{'question_id':qid,'text':'Same question?','context':{'section':section}}
                   for qid,section in [('q-a','current'),('q-b','future')]]
        self.change_form(questions=questions);self.review();self.blocked()
        rows=self.rows(UnresolvedInformation,application_id=self.app_id)
        self.assertEqual(len(rows),2)
        self.assertEqual({r.context['question_id'] for r in rows},{'q-a','q-b'})
        self.blocked();self.assertEqual(len(self.rows(UnresolvedInformation,application_id=self.app_id)),2)
        for question in questions:
            answer=ApprovedAnswer(candidate_id=self.candidate.id,question=question['text'],answer=question['context']['section'],category='fixture',source='local')
            self.save(answer)
            self.ok(self.human.approve_answer(answer.id,requirements_id=self.req.id,question_id=question['question_id'],expected_revision=1,reason='Owner approved occurrence'))
        result=self.ready()
        self.assertEqual(len(result.answers),2)
        self.assertEqual(set(result.answers.values()),{'current','future'})

    def test_changed_form_version_requires_review_and_answer_reapproval(self):
        answer=self.approved();self.ready()
        self.change_form(context={'form_id':'fixture-form','form_version':'2'})
        self.assertIn('form_review_missing_or_stale',self.blocked().reasons)
        self.review();self.blocked()
        self.ok(self.human.approve_answer(answer.id,requirements_id=self.req.id,question_id='q-0',expected_revision=answer.revision,reason='Owner approves new version'))
        self.ready()

    def test_changed_question_context_requires_new_approval(self):
        self.approved()
        self.change_form(questions=[{'question_id':'q-0','text':'Same question?','context':{'time':'future'}}])
        self.review();self.blocked()

    def test_changed_shared_context_cannot_be_hidden_by_question_context(self):
        self.approved()
        self.change_form(context={'form_id':'fixture-form','form_version':'1','section':'new'},
            questions=[{'question_id':'q-0','text':'Same question?','context':{'section':'old'}}])
        self.review();self.blocked()
        row=self.rows(UnresolvedInformation,application_id=self.app_id)[0]
        self.assertEqual(row.context['form_context']['section'],'new')
        self.assertEqual(row.context['question_context']['section'],'old')

    def test_missing_form_id_blocks_review_and_readiness(self):
        self.approved();req=self.change_form(context={'form_version':'1'})
        self.assertTrue(self.human.review_requirements(req.id,expected_revision=req.revision,reason='Review').blocked)
        self.assertIn('form_identity_missing_or_invalid',self.blocked().reasons)

    def test_missing_form_version_blocks_review_and_readiness(self):
        self.approved();req=self.change_form(context={'form_id':'fixture-form'})
        self.assertTrue(self.human.review_requirements(req.id,expected_revision=req.revision,reason='Review').blocked)
        self.blocked()

    def test_missing_question_id_blocks_review_and_approval(self):
        answer=self.approved()
        req=self.change_form(questions=[{'text':answer.question,'context':{}}])
        self.assertTrue(self.human.review_requirements(req.id,expected_revision=req.revision,reason='Review').blocked)
        self.assertTrue(self.human.approve_answer(answer.id,requirements_id=req.id,question_id='q-0',expected_revision=answer.revision,reason='Guess identity').blocked)
        self.blocked()

    def test_missing_question_context_blocks_review(self):
        self.approved();req=self.change_form(questions=[{'question_id':'q-0','text':'Same question?'}])
        self.assertTrue(self.human.review_requirements(req.id,expected_revision=req.revision,reason='Review').blocked)
        self.blocked()

    def test_duplicate_question_id_blocks_review(self):
        self.approved();req=self.change_form(questions=[{'question_id':'q-0','text':text,'context':{}} for text in ('First?','Second?')])
        self.assertTrue(self.human.review_requirements(req.id,expected_revision=req.revision,reason='Review').blocked)
        self.blocked()

    def test_wildcard_form_or_question_identifiers_cannot_be_approved(self):
        self.approved()
        for key in ('form_id','form_version'):
            req=self.change_form(context={'form_id':'fixture-form','form_version':'1',key:'*'})
            self.assertTrue(self.human.review_requirements(req.id,expected_revision=req.revision,reason='Broad review').blocked)
            self.blocked()
        req=self.change_form(context={'form_id':'fixture-form','form_version':'1'},questions=[{'question_id':'*','text':'Same question?','context':{}}])
        self.assertTrue(self.human.review_requirements(req.id,expected_revision=req.revision,reason='Broad review').blocked)

    def test_legacy_unscoped_approved_answer_is_never_reused(self):
        answer=self.approved()
        # Trusted fixture reproduces pre-binding approval shape, not a worker write.
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            old=repo.get(ApprovedAnswer,answer.id)
            old.scope={};old.approval.pop('scope_fingerprint');old.approval.pop('job_revision');old.revision+=1
            repo.update(old)
        self.blocked()
        current=self.get(ApprovedAnswer,answer.id)
        self.ok(self.human.approve_answer(answer.id,requirements_id=self.req.id,question_id='q-0',expected_revision=current.revision,reason='Explicit legacy reapproval'))
        self.ready()

    def test_legacy_review_without_fingerprint_cannot_be_reused(self):
        self.approved()
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            req=repo.get(ReadinessRequirements,self.req.id)
            req.approval.pop('form_fingerprint');req.revision+=1;repo.update(req)
        self.assertIn('form_review_missing_or_stale',self.blocked().reasons)

    def test_stale_review_manifest_is_detected_independently_of_revision(self):
        self.approved()
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            req=repo.get(ReadinessRequirements,self.req.id)
            req.context['new_field']='new form detail';req.revision+=1;repo.update(req)
        self.assertIn('form_review_missing_or_stale',self.blocked().reasons)

    def test_worker_cannot_create_or_broaden_scope(self):
        answer=self.approved()
        dispatcher=TrustedWorkerDispatcher(self.db,WorkerScope('worker',self.candidate.id))
        for extra in ({'scope':{'application_id':'*'}},{'requirements_id':self.req.id},{'approval':answer.approval}):
            result=json.loads(dispatcher.handle(json.dumps({'command':'propose_answer','arguments':{
                'question':answer.question,'answer':answer.answer,'category':answer.category,**extra}}).encode()))
            self.assertEqual(result['status'],'rejected')
        proposal=deepcopy(answer);proposal.approval_state=ApprovalState.DRAFT;proposal.approval=None
        proposal.scope['application_id']='*'
        self.assertIn('human_scope_binding_required',self.worker.save_library_record(proposal,update=True).reasons)
        self.assertEqual(self.get(ApprovedAnswer,answer.id).scope,answer.scope)

    def test_explicit_cross_application_reuse_attempt_is_blocked(self):
        answer=self.approved();app_id,req=self.another_application('Another Company')
        self.blocked(app_id)
        unresolved=self.rows(UnresolvedInformation,application_id=app_id)[0]
        self.assertTrue(self.human.resolve_unknown_question(unresolved.id,answer.id,reason='Same text').blocked)

    def test_cached_answer_is_not_returned_after_revocation(self):
        answer=self.approved();cached=self.ready().answers
        self.assertTrue(cached)
        self.ok(self.human.revoke_answer(answer.id,expected_revision=answer.revision,reason='Withdraw approval'))
        self.assertIsNone(self.get(Application,self.app_id).readiness)
        self.blocked()

    def test_candidate_revision_change_blocks_contextual_answer(self):
        self.approved();candidate=self.get(CandidateProfile,self.candidate.id);candidate.skills=['new']
        self.save(candidate,update=True);self.review();self.blocked()

    def test_job_revision_change_requires_answer_reapproval(self):
        answer=self.approved();job=self.get(Job,self.job.id);job.title='Updated role'
        self.save(job,update=True);self.review();self.blocked()
        self.ok(self.human.approve_answer(answer.id,requirements_id=self.req.id,question_id='q-0',expected_revision=answer.revision,reason='Owner checked updated job'))
        self.ready()

    def test_context_scalar_types_are_not_coerced(self):
        self.approved()
        self.change_form(questions=[{'question_id':'q-0','text':'Same question?','context':{'value':True}}]);self.review()
        answer=self.get(ApprovedAnswer,self.rows(ApprovedAnswer)[0].id)
        self.ok(self.human.approve_answer(answer.id,requirements_id=self.req.id,question_id='q-0',expected_revision=answer.revision,reason='Boolean context'))
        self.change_form(questions=[{'question_id':'q-0','text':'Same question?','context':{'value':1}}]);self.review();self.blocked()

    def test_migration_preserves_unscoped_payload_and_allows_same_text_records(self):
        from job_applier.database import migrations
        path=self.root/'v2.sqlite3'
        # Reproduce old text-only uniqueness and v2 user_version.
        with patch.object(migrations,'CURRENT_VERSION',2),patch.dict(UNIQUE,{'approved_answers':[('candidate_id','question')]}):
            SQLiteDatabase(path).initialize()
        with sqlite3.connect(path) as con:
            con.create_function('domain_write_allowed',0,lambda:1)
            candidate=self.get(CandidateProfile,self.candidate.id)
            answer=ApprovedAnswer(candidate_id=candidate.id,question='Legacy?',answer='Legacy',category='test',source='legacy')
            payload=asdict(answer);payload.pop('scope')
            con.execute('INSERT INTO candidate_profiles VALUES (?,?,?)',(candidate.id,candidate.revision,json.dumps(asdict(candidate))))
            con.execute('INSERT INTO approved_answers VALUES (?,?,?,?,?,?)',(answer.id,1,json.dumps(payload),candidate.id,answer.question,'draft'))
        SQLiteDatabase(path).initialize()
        with SQLiteDatabase(path).transaction(capability=_DOMAIN_WRITE) as repo:
            self.assertEqual(repo.get(ApprovedAnswer,answer.id).scope,{})
            repo.add(ApprovedAnswer(candidate_id=candidate.id,question='Legacy?',answer='Another scoped proposal',category='test',source='local'))
        with sqlite3.connect(path) as con:
            self.assertEqual(json.loads(con.execute('SELECT payload FROM approved_answers WHERE id=?',(answer.id,)).fetchone()[0]),payload)

    def test_conflicting_answers_for_same_exact_scope_block(self):
        self.approved()
        alternative=ApprovedAnswer(candidate_id=self.candidate.id,question='Same question?',answer='Contradictory',category='fixture',source='local')
        self.save(alternative)
        self.ok(self.human.approve_answer(alternative.id,requirements_id=self.req.id,question_id='q-0',expected_revision=1,reason='Conflicting fixture approval'))
        self.blocked()

    def test_v3_migration_failure_rolls_back_answer_table_and_version(self):
        from job_applier.database import migrations
        path=self.root/'rollback-v2.sqlite3'
        with patch.object(migrations,'CURRENT_VERSION',2),patch.dict(UNIQUE,{'approved_answers':[('candidate_id','question')]}):
            SQLiteDatabase(path).initialize()
        with sqlite3.connect(path) as con:
            schema_before=con.execute("SELECT sql FROM sqlite_master WHERE name='approved_answers'").fetchone()[0]
        original=migrations.MIGRATIONS[3]
        def fail(con):
            original(con)
            raise RuntimeError('v3 migration interrupted')
        with patch.dict(migrations.MIGRATIONS,{3:fail}),self.assertRaises(RuntimeError):
            SQLiteDatabase(path).initialize()
        with sqlite3.connect(path) as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertEqual(con.execute("SELECT sql FROM sqlite_master WHERE name='approved_answers'").fetchone()[0],schema_before)
