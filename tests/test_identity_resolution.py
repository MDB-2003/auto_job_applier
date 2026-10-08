"""Adversarial local identity tests; privileged writes only seed legacy fixtures."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from job_applier.database import _DOMAIN_WRITE
from job_applier.database.sqlite import SQLiteDatabase
from job_applier.interfaces.worker import TrustedWorkerDispatcher, WorkerScope
from job_applier.models import Application, AuditEvent, CandidateProfile, CanonicalOpening, Job, JobLinkEvidence
from job_applier.services.foundation import FoundationService
from job_applier.services.human import LocalHumanOperations

EVIDENCE = {'reference': 'fixture:owner-review', 'description': 'Owner reviewed local identity evidence'}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = SQLiteDatabase(Path(temp.name) / 'identity.sqlite3')
        self.db.initialize()
        self.service = FoundationService(self.db)
        self.owner = LocalHumanOperations(self.db, owner_actor_id='owner')
        self.candidate = CandidateProfile()
        self.ok(self.service.save_library_record(self.candidate))

    def ok(self, decision):
        self.assertFalse(decision.blocked, decision.reasons)
        return decision

    def rows(self, model, **filters):
        with self.db.transaction() as repo:
            return repo.list(model, limit=1000, **filters)

    def get(self, job):
        with self.db.transaction() as repo:
            return repo.get(Job, job.id)

    def source(self, source, req=None, url=None, company='Company', blocked=False):
        job = Job(company=company, title='Role', source=source, source_job_id=source,
                  requisition_id=req, canonical_url=url)
        result = self.service.save_library_record(job)
        self.assertEqual(result.blocked, blocked, result.reasons)
        if blocked:
            self.assertIn('identity_review_required', result.reasons)
        return self.get(job)

    def apply(self, job):
        return self.service.create_application(self.candidate.id, job.id)

    def legacy(self, source, req=None, url=None):
        canonical = CanonicalOpening(company='Company', title='Role', requisition_id=req, canonical_url=url)
        job = Job(company='Company', title='Role', source=source, source_job_id=source,
                  requisition_id=req, canonical_url=url, canonical_id=canonical.id)
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            repo.add(canonical)
            repo.add(job)
        return job

    def assert_duplicate(self, first, second):
        self.assertEqual(first.canonical_id, second.canonical_id)
        result = self.apply(second)
        self.assertTrue(result.blocked)
        self.assertIn('duplicate_application', result.reasons)
        self.assertEqual(len(self.rows(Application)), 1)

    def test_same_requisition_across_sources_resolves_before_application(self):
        a = self.source('A', req='REQ-1')
        self.ok(self.apply(a))
        b = self.source('B', req='REQ-1')
        self.assert_duplicate(a, b)
        self.assertEqual(len(self.rows(CanonicalOpening)), 1)
        link = self.rows(JobLinkEvidence, source_record_id=b.id)[0]
        self.assertEqual(link.method, 'strong_identity')
        self.assertEqual(link.evidence['source_identity']['requisition_id'], 'REQ-1')
        self.assertEqual(link.evidence['candidates'][0]['matching_identifiers'], ['requisition_id'])
        self.assertTrue(self.rows(AuditEvent, entity_id=link.id))
        # Persistence survives reopening the local database.
        reopened = SQLiteDatabase(self.db.path)
        with reopened.transaction() as repo:
            self.assertEqual(repo.get(Job, b.id).canonical_id, a.canonical_id)
            self.assertEqual(repo.get(JobLinkEvidence, link.id).evidence, link.evidence)

    def test_same_canonical_url_across_sources(self):
        a = self.source('A', url='https://company.invalid/job/1')
        self.ok(self.apply(a))
        b = self.source('B', url=a.canonical_url)
        self.assert_duplicate(a, b)

    def test_both_identifiers_match(self):
        a = self.source('A', req='R1', url='https://company.invalid/1')
        self.ok(self.apply(a))
        self.assert_duplicate(a, self.source('B', req=a.requisition_id, url=a.canonical_url))

    def test_distinct_requisitions_and_urls_allow_distinct_applications(self):
        a = self.source('A', req='R1', url='https://company.invalid/1')
        b = self.source('B', req='R2', url='https://company.invalid/2')
        self.assertNotEqual(a.canonical_id, b.canonical_id)
        self.ok(self.apply(a)); self.ok(self.apply(b))
        self.assertEqual(len(self.rows(Application)), 2)

    def test_distinct_requisitions_without_urls(self):
        a = self.source('A', req='R1'); b = self.source('B', req='R2')
        self.ok(self.apply(a)); self.ok(self.apply(b))

    def test_distinct_urls_without_requisitions(self):
        a = self.source('A', url='https://company.invalid/1')
        b = self.source('B', url='https://company.invalid/2')
        self.ok(self.apply(a)); self.ok(self.apply(b))

    def test_same_identifier_different_companies_is_distinct(self):
        a = self.source('A', req='R1'); b = self.source('B', req='R1', company='Other Company')
        self.ok(self.apply(a)); self.ok(self.apply(b))
        self.assertNotEqual(a.canonical_id, b.canonical_id)

    def test_missing_identifiers_require_owner_equivalence(self):
        a = self.source('A'); self.ok(self.apply(a))
        b = self.source('B', blocked=True)
        self.assertNotEqual(a.canonical_id, b.canonical_id)
        self.assertIn('identity_review_required', self.apply(b).reasons)
        self.assertTrue(self.service.link_job_source(b.id, a.canonical_id, evidence=EVIDENCE).blocked)
        self.ok(self.owner.confirm_job_equivalence(b.id, a.canonical_id, evidence=EVIDENCE, reason='Same opening'))
        self.assert_duplicate(a, self.get(b))
        approval = self.rows(JobLinkEvidence, source_record_id=b.id, method='human_confirmation')[0].approval
        self.assertEqual(approval['actor_id'], 'owner')
        self.assertEqual(approval['actor_type'], 'HUMAN')
        for key in ('timestamp', 'reason', 'previous_revision', 'new_revision'):
            self.assertIn(key, approval)

    def test_owner_can_confirm_ambiguous_source_is_distinct(self):
        a = self.source('A'); self.ok(self.apply(a))
        b = self.source('B', blocked=True)
        self.ok(self.owner.confirm_job_distinct(b.id, expected_revision=b.revision, evidence=EVIDENCE, reason='Separate opening'))
        self.ok(self.apply(b))
        self.assertEqual(len(self.rows(Application)), 2)
        link = self.rows(JobLinkEvidence, source_record_id=b.id, method='human_distinct')[0]
        self.assertEqual(link.approval['actor_type'], 'HUMAN')
        self.assertEqual(link.approval['previous_revision'], 1)
        self.assertEqual(link.approval['new_revision'], 2)

    def test_one_sided_identifiers_cannot_establish_distinction(self):
        self.source('A', req='R1')
        b = self.source('B', url='https://company.invalid/1', blocked=True)
        self.assertIn('identity_review_required', self.apply(b).reasons)

    def test_whitespace_identifiers_do_not_provide_strong_evidence(self):
        job = Job(company='Company', title='Role', source='A', source_job_id='A', requisition_id=' ')
        self.assertTrue(self.service.save_library_record(job).blocked)
        self.assertEqual(self.rows(Job), [])
        self.assertEqual(self.rows(CanonicalOpening), [])

    def test_same_requisition_conflicting_urls_require_review(self):
        a = self.source('A', req='R1', url='https://company.invalid/1')
        self.ok(self.apply(a))
        b = self.source('B', req='R1', url='https://company.invalid/2', blocked=True)
        self.assertTrue(self.apply(b).blocked)
        self.assertTrue(self.service.link_job_source(b.id, a.canonical_id, evidence=EVIDENCE).blocked)
        link = self.rows(JobLinkEvidence, source_record_id=b.id)[0]
        self.assertEqual(link.evidence['resolution'], 'conflicting')
        self.ok(self.owner.confirm_job_equivalence(b.id, a.canonical_id, evidence=EVIDENCE, reason='URL alias checked'))
        self.assert_duplicate(a, self.get(b))

    def test_same_url_conflicting_requisitions_require_review(self):
        self.source('A', req='R1', url='https://company.invalid/1')
        b = self.source('B', req='R2', url='https://company.invalid/1', blocked=True)
        self.assertIn('identity_review_required', self.apply(b).reasons)

    def test_identifiers_matching_two_canonicals_are_conflicting(self):
        a = self.source('A', req='R1', url='https://company.invalid/1')
        b = self.source('B', req='R2', url='https://company.invalid/2')
        self.ok(self.apply(a)); self.ok(self.apply(b))
        c = self.source('C', req='R1', url=b.canonical_url, blocked=True)
        self.assertTrue(self.apply(c).blocked)
        self.assertEqual(len(self.rows(Application)), 2)

    def test_established_aliases_supply_transitive_strong_identity(self):
        a = self.source('A', req='R1'); self.ok(self.apply(a))
        b = self.source('B', req='R1', url='https://company.invalid/1')
        c = self.source('C', url=b.canonical_url)
        self.assert_duplicate(a, c)
        self.assertEqual(len(self.rows(CanonicalOpening)), 1)

    def test_legacy_unlinked_source_is_resolved_at_application_creation(self):
        a = self.source('A', req='R1'); self.ok(self.apply(a))
        b = self.legacy('B', req='R1')
        self.assertNotEqual(a.canonical_id, b.canonical_id)
        self.assertIn('duplicate_application', self.apply(b).reasons)
        self.assertEqual(self.get(b).canonical_id, a.canonical_id)
        self.assertEqual(self.rows(JobLinkEvidence, source_record_id=b.id)[0].evidence['trigger'], 'application_creation')

    def test_existing_legacy_application_history_is_never_silently_merged(self):
        a = self.source('A', req='R1'); self.ok(self.apply(a))
        b = self.legacy('B', req='R1')
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            repo.add(Application(candidate_id=self.candidate.id, job_id=b.id, canonical_id=b.canonical_id))
        self.assertIn('existing_application_identity_requires_human_review', self.apply(b).reasons)
        self.assertEqual(self.get(b).canonical_id, b.canonical_id)

    def test_owner_decision_is_invalidated_by_new_identity_alternatives(self):
        self.source('A')
        b = self.source('B', blocked=True)
        self.ok(self.owner.confirm_job_distinct(b.id, expected_revision=1, evidence=EVIDENCE, reason='Distinct'))
        self.source('C', blocked=True)
        self.assertIn('identity_review_required', self.apply(b).reasons)

    def test_owner_cannot_declare_uncontested_strong_match_distinct(self):
        self.source('A', req='R1')
        b = self.legacy('B', req='R1')
        result = self.owner.confirm_job_distinct(b.id, expected_revision=1, evidence=EVIDENCE, reason='Try split')
        self.assertIn('strong_identity_cannot_be_declared_distinct', result.reasons)

    def test_idempotent_self_link_cannot_clear_ambiguity(self):
        self.source('A'); b = self.source('B', blocked=True)
        self.ok(self.service.link_job_source(b.id, b.canonical_id, evidence=EVIDENCE))
        self.assertIn('identity_review_required', self.apply(b).reasons)

    def test_worker_cannot_confirm_distinction_or_supply_canonical_id(self):
        a = self.source('A'); self.ok(self.apply(a)); b = self.source('B', blocked=True)
        dispatcher = TrustedWorkerDispatcher(self.db, WorkerScope('worker', self.candidate.id))
        for command in ('confirm_job_distinct', 'confirm_job_equivalence', 'link_job_source', 'save_library_record'):
            result = json.loads(dispatcher.handle(json.dumps({'command': command, 'arguments': {'source_record_id': b.id}}).encode()))
            self.assertEqual(result['status'], 'rejected')
        result = json.loads(dispatcher.handle(json.dumps({'command': 'create_application', 'arguments': {'job_id': b.id}}).encode()))
        self.assertEqual(result['status'], 'blocked')
        result = json.loads(dispatcher.handle(json.dumps({'command': 'create_application', 'arguments': {'job_id': b.id, 'canonical_id': a.canonical_id}}).encode()))
        self.assertEqual(result['status'], 'rejected')

    def test_generic_record_write_cannot_forge_link_evidence(self):
        a = self.source('A')
        forged = JobLinkEvidence(source_record_id=a.id, canonical_id=a.canonical_id, method='human_distinct', evidence=EVIDENCE)
        self.assertTrue(self.service.save_library_record(forged).blocked)
        job = Job(company='Company', title='Role', source='B', source_job_id='B', canonical_id=a.canonical_id)
        self.assertTrue(self.service.save_library_record(job).blocked)

    def test_concurrent_imports_and_applications_share_one_identity(self):
        jobs = [Job(company='Company', title='Role', source=s, source_job_id=s, requisition_id='R1') for s in ('A', 'B')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            for result in pool.map(self.service.save_library_record, jobs):
                self.ok(result)
        self.assertEqual(self.get(jobs[0]).canonical_id, self.get(jobs[1]).canonical_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.apply, jobs))
        self.assertEqual(sum(not result.blocked for result in results), 1)
        self.assertEqual(len(self.rows(Application)), 1)
        self.assertEqual(len(self.rows(CanonicalOpening)), 1)

    def test_match_beyond_first_query_page_is_not_missed(self):
        # Seed old rows in one transaction; identity resolver must inspect page 2.
        with self.db.transaction(capability=_DOMAIN_WRITE) as repo:
            for index in range(105):
                canonical = CanonicalOpening(company='Company', title='Role', requisition_id=f'R{index}')
                repo.add(canonical)
                repo.add(Job(company='Company', title='Role', source='A', source_job_id=str(index),
                             requisition_id=f'R{index}', canonical_id=canonical.id))
        existing = self.rows(Job)
        for index, job in enumerate(existing):
            b = self.source(f'B{index}', req=job.requisition_id)
            self.assertEqual(b.canonical_id, job.canonical_id)
        self.assertEqual(len(self.rows(CanonicalOpening)), 105)
