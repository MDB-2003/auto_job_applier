"""Local durable intent, interruption recovery, and submission authority tests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_hardening import LocalCase, EVIDENCE
from job_applier.database import _DOMAIN_WRITE
from job_applier.database.sqlite import SQLiteDatabase
from job_applier.interfaces.worker import TrustedWorkerDispatcher, WorkerScope
from job_applier.models import (
    Application, ApplicationStatus as A, AuditEvent, CandidateProfile, Job,
    ReadinessRequirements, Resume, SubmissionAttempt,
)
from job_applier.services.foundation import FoundationService
from job_applier.services.human import LocalHumanOperations
from job_applier.utilities.serialization import decode


class DurableAttemptTests(LocalCase):
    def reserve(self):
        self.ready()
        result = self.ok(self.worker.reserve_submission(self.app_id))
        return self.get(SubmissionAttempt, result.entity_id)

    def reconcile(self, attempt, outcome):
        current = self.get(SubmissionAttempt, attempt.id)
        return self.human.reconcile_submission(attempt.id, expected_revision=current.revision,
            outcome=outcome, evidence=EVIDENCE, reason='Owner checked local fixture evidence')

    def test_reservation_persists_complete_identity_context_and_audit(self):
        self.ready()
        before = self.get(Application, self.app_id)
        attempt_id = self.ok(self.worker.reserve_submission(self.app_id)).entity_id
        # Separate database connection after receipt proves visibility after commit.
        with SQLiteDatabase(self.db.path).transaction() as repo:
            attempt = repo.get(SubmissionAttempt, attempt_id)
            self.assertEqual(attempt.action_state, 'reserved')
            self.assertEqual(attempt.outcome, 'uncertain')
            self.assertEqual(attempt.application_id, before.id)
            self.assertEqual(attempt.candidate_id, before.candidate_id)
            self.assertEqual(attempt.job_id, before.job_id)
            self.assertEqual(attempt.canonical_id, before.canonical_id)
            self.assertEqual(attempt.resume_id, before.resume_id)
            self.assertTrue(attempt.created_at)
            self.assertTrue(attempt.correlation_id)
            self.assertEqual(attempt.context['application_revision'], before.revision)
            for key, value in before.readiness.items():
                self.assertEqual(attempt.context[key], value)
            self.assertGreaterEqual(attempt.context['canonical_revision'], 1)
            events = repo.list(AuditEvent, correlation_id=attempt.correlation_id)
            self.assertTrue(any(event.action == 'reserve_submission' and event.result == 'success' for event in events))
            self.assertEqual(repo.get(Application, before.id).latest_attempt_id, attempt.id)

    def test_process_crash_after_commit_is_discovered_and_never_replayed(self):
        self.ready()
        code = '''import os, sys
from pathlib import Path
from job_applier.database.sqlite import SQLiteDatabase
from job_applier.services.foundation import FoundationService
result = FoundationService(SQLiteDatabase(Path(sys.argv[1]))).reserve_submission(sys.argv[2])
if result.blocked: raise RuntimeError(result.reasons)
os._exit(17)
'''
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.db.path), self.app_id], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 17, result.stderr)
        reopened = FoundationService(SQLiteDatabase(self.db.path))
        outstanding = reopened.outstanding_submission_attempts()
        self.assertEqual(len(outstanding), 1)
        self.assertEqual(outstanding[0].application_id, self.app_id)
        self.assertEqual(outstanding[0].action_state, 'reserved')
        self.assertIn('outstanding_attempt_requires_reconciliation', reopened.reserve_submission(self.app_id).reasons)
        self.assertTrue(reopened.prepare_application(self.app_id).blocked)
        self.assertEqual(self.get(Application, self.app_id).status, A.SUBMISSION_UNCERTAIN)

    def test_commit_failure_returns_no_receipt_or_success_event(self):
        self.ready()
        original = self.db.transaction
        fail_next = True
        @contextmanager
        def fail_commit(*, capability=None):
            nonlocal fail_next
            with original(capability=capability) as repo:
                yield repo
                if capability is _DOMAIN_WRITE and fail_next:
                    fail_next = False
                    raise RuntimeError('simulated commit failure')
        with patch.object(self.db, 'transaction', fail_commit):
            with self.assertRaisesRegex(RuntimeError, 'simulated commit failure'):
                self.worker.reserve_submission(self.app_id)
        self.assertEqual(self.rows(SubmissionAttempt), [])
        self.assertIsNone(self.get(Application, self.app_id).latest_attempt_id)
        events = self.rows(AuditEvent, action='reserve_submission')
        self.assertTrue(events)
        self.assertFalse(any(event.result == 'success' for event in events))

    def check_change_after_attempt(self, model, identity, mutate):
        attempt = self.reserve()
        record = self.get(model, identity)
        mutate(record)
        self.save(record, update=True)
        self.assertEqual(self.get(SubmissionAttempt, attempt.id), attempt)
        self.assertIsNone(self.get(Application, self.app_id).readiness)
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)
        self.ok(self.reconcile(attempt, 'confirmed_submitted'))
        result = self.get(SubmissionAttempt, attempt.id)
        self.assertEqual(result.context, attempt.context)
        self.assertEqual(result.action_state, 'confirmed_submitted')
        self.assertEqual(self.get(Application, self.app_id).status, A.SUBMITTED)

    def test_candidate_change_does_not_erase_attempt_or_block_outcome(self):
        self.check_change_after_attempt(CandidateProfile, self.candidate.id,
            lambda record: record.personal_information.update(full_name=False))

    def test_resume_change_does_not_erase_attempt_or_block_outcome(self):
        self.check_change_after_attempt(Resume, self.resume.id, lambda record: setattr(record, 'version', '2'))

    def test_resume_bytes_change_does_not_block_outcome(self):
        attempt = self.reserve()
        self.file.write_text('Changed synthetic resume after possible submission')
        self.ok(self.reconcile(attempt, 'confirmed_submitted'))
        self.assertEqual(self.get(SubmissionAttempt, attempt.id).context, attempt.context)

    def test_job_change_does_not_erase_attempt_or_block_outcome(self):
        self.check_change_after_attempt(Job, self.job.id, lambda record: setattr(record, 'title', 'Updated title'))

    def test_requirements_invalidation_does_not_block_outcome(self):
        attempt = self.reserve()
        self.assertIsNotNone(self.get(ReadinessRequirements, self.req.id).approval)
        candidate = self.get(CandidateProfile, self.candidate.id)
        candidate.target_roles = ['Changed role']
        self.save(candidate, update=True)
        self.assertIsNone(self.get(ReadinessRequirements, self.req.id).approval)
        self.assertEqual(self.get(SubmissionAttempt, attempt.id), attempt)
        self.ok(self.reconcile(attempt, 'confirmed_submitted'))
        self.assertEqual(self.get(Application, self.app_id).status, A.SUBMITTED)

    def test_inconclusive_report_after_invalid_candidate_uses_existing_attempt(self):
        attempt = self.reserve()
        candidate = self.get(CandidateProfile, self.candidate.id)
        candidate.personal_information.clear()
        self.save(candidate, update=True)
        result = self.worker.record_submission_uncertainty(self.app_id, evidence=EVIDENCE)
        self.assertTrue(result.blocked)
        self.assertEqual(result.entity_id, attempt.id)
        self.assertEqual(len(self.rows(SubmissionAttempt)), 1)
        current = self.get(SubmissionAttempt, attempt.id)
        self.assertEqual(current.action_state, 'uncertain')
        self.assertEqual(current.context, attempt.context)
        self.assertIsNone(current.resolution)
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)

    def test_confirmed_submitted_never_retries_or_accepts_rewriting_outcome(self):
        attempt = self.reserve()
        self.ok(self.reconcile(attempt, 'confirmed_submitted'))
        self.assertIn('already_submitted', self.worker.reserve_submission(self.app_id).reasons)
        current = self.get(SubmissionAttempt, attempt.id)
        self.assertTrue(self.human.authorize_retry(attempt.id, expected_revision=current.revision, reason='Retry').blocked)
        self.assertTrue(self.reconcile(attempt, 'confirmed_not_submitted').blocked)
        self.assertTrue(self.worker.record_submission_failure(self.app_id).blocked)
        self.assertEqual(self.get(Application, self.app_id).status, A.SUBMITTED)
        self.assertEqual(self.worker.outstanding_submission_attempts(), [])

    def test_confirmed_not_submitted_requires_owner_authorization_and_new_readiness(self):
        attempt = self.reserve()
        self.ok(self.reconcile(attempt, 'confirmed_not_submitted'))
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)
        self.assertTrue(self.worker.prepare_application(self.app_id).blocked)
        current = self.get(SubmissionAttempt, attempt.id)
        self.ok(self.human.authorize_retry(attempt.id, expected_revision=current.revision, reason='One retry'))
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)
        self.ready()
        second_id = self.ok(self.worker.reserve_submission(self.app_id)).entity_id
        self.assertNotEqual(second_id, attempt.id)
        self.assertTrue(self.get(SubmissionAttempt, attempt.id).retry_consumed)
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)
        self.assertEqual(len(self.rows(SubmissionAttempt)), 2)

    def test_inconclusive_human_reconciliation_remains_uncertain(self):
        attempt = self.reserve()
        self.assertTrue(self.reconcile(attempt, 'uncertain').blocked)
        current = self.get(SubmissionAttempt, attempt.id)
        self.assertEqual(current.action_state, 'uncertain')
        self.assertEqual(current.resolution['actor_type'], 'HUMAN')
        self.assertTrue(self.human.authorize_retry(attempt.id, expected_revision=current.revision, reason='Probably failed').blocked)
        self.assertTrue(self.worker.reserve_submission(self.app_id).blocked)

    def test_outcome_requires_nonempty_evidence(self):
        attempt = self.reserve()
        for outcome in ('confirmed_submitted', 'confirmed_not_submitted'):
            result = self.human.reconcile_submission(attempt.id, expected_revision=1, outcome=outcome,
                evidence=[], reason='No reliable evidence')
            self.assertTrue(result.blocked)
        self.assertEqual(self.get(SubmissionAttempt, attempt.id), attempt)

    def test_duplicate_attempts_block(self):
        attempt = self.reserve()
        for _ in range(3):
            self.assertIn('outstanding_attempt_requires_reconciliation', self.worker.reserve_submission(self.app_id).reasons)
        self.assertEqual(self.rows(SubmissionAttempt), [attempt])

    def test_concurrent_reservation_creates_exactly_one_attempt(self):
        self.ready()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.worker.reserve_submission(self.app_id), range(2)))
        self.assertEqual(sum(not result.blocked for result in results), 1)
        attempts = self.rows(SubmissionAttempt)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.get(Application, self.app_id).latest_attempt_id, attempts[0].id)

    def test_outcome_cannot_retroactively_create_attempt(self):
        self.ready()
        for operation in (self.worker.record_submission_uncertainty, self.worker.record_submission_failure):
            self.assertIn('durable_attempt_required', operation(self.app_id).reasons)
        self.assertEqual(self.rows(SubmissionAttempt), [])
        self.assertEqual(self.get(Application, self.app_id).status, A.READY_TO_APPLY)

    def test_stale_readiness_cannot_reserve(self):
        self.ready()
        self.file.write_text('Changed bytes before reservation')
        self.assertIn('stale_readiness', self.worker.reserve_submission(self.app_id).reasons)
        self.assertEqual(self.rows(SubmissionAttempt), [])

    def test_worker_cannot_reserve_reconcile_retry_or_report_confirmed_outcome(self):
        attempt = self.reserve()
        dispatcher = TrustedWorkerDispatcher(self.db, WorkerScope('worker', self.candidate.id))
        for command in ('reserve_submission', 'reconcile_submission', 'authorize_retry',
                        'record_submission_uncertainty', 'record_submission_failure', 'record_submission_outcome',
                        'mark_submitted', 'save_library_record', 'LocalHumanOperations'):
            response = json.loads(dispatcher.handle(json.dumps({'command': command, 'arguments': {
                'application_id': self.app_id, 'attempt_id': attempt.id, 'outcome': 'confirmed_submitted',
                'actor_type': 'HUMAN', 'evidence': EVIDENCE,
            }}).encode()))
            self.assertEqual(response['status'], 'rejected', command)
        self.assertEqual(self.get(SubmissionAttempt, attempt.id), attempt)
        self.assertTrue(self.worker.save_library_record(attempt, update=True).blocked)
        self.assertTrue(self.worker.transition_application(self.app_id, A.SUBMITTED).blocked)

    def test_reservation_receipt_does_not_enable_external_actions(self):
        self.reserve()
        self.assertIn('external_actions_disabled', self.worker.request_external_action(Application, self.app_id, action='submit').reasons)

    def test_reconciliation_preserves_prior_evidence_and_snapshot(self):
        attempt = self.reserve()
        self.worker.record_submission_uncertainty(self.app_id, evidence=EVIDENCE)
        self.ok(self.reconcile(attempt, 'confirmed_submitted'))
        current = self.get(SubmissionAttempt, attempt.id)
        self.assertEqual(current.evidence, EVIDENCE + EVIDENCE)
        self.assertEqual(current.context, attempt.context)
        self.assertEqual(current.correlation_id, attempt.correlation_id)

    def test_legacy_json_without_snapshot_remains_reconciliation_only(self):
        attempt = self.reserve()
        payload = asdict(attempt)
        for key in ('action_state','candidate_id','job_id','canonical_id','resume_id','correlation_id','context'):
            payload.pop(key)
        legacy = decode(SubmissionAttempt, json.dumps(payload))
        self.assertEqual(legacy.action_state, 'legacy')
        self.assertEqual(legacy.context, {})
        self.assertIsNone(legacy.candidate_id)

    def test_recovery_inventory_is_bounded_and_survives_reopen(self):
        attempt = self.reserve()
        reopened = FoundationService(SQLiteDatabase(self.db.path))
        self.assertEqual(reopened.outstanding_submission_attempts(limit=1), [attempt])
        self.assertEqual(reopened.outstanding_submission_attempts(limit=1, offset=1), [])
        with self.assertRaises(ValueError): reopened.outstanding_submission_attempts(limit=0)
