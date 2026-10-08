"""Blocker 3: field values, not structural presence, must satisfy readiness."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from job_applier.database.sqlite import SQLiteDatabase
from job_applier.interfaces.worker import TrustedWorkerDispatcher, WorkerScope
from job_applier.models import (
    Application, ApplicationStatus, CandidateProfile, Job, ReadinessRequirements,
    Resume, UnresolvedInformation,
)
from job_applier.models.validation import GLOBAL_REQUIRED_FIELDS, valid_required_value
from job_applier.services.foundation import FoundationService
from job_applier.services.human import LocalHumanOperations


class CandidateValueTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.db = SQLiteDatabase(root / 'values.sqlite3')
        self.db.initialize()
        self.service = FoundationService(self.db)
        self.owner = LocalHumanOperations(self.db, owner_actor_id='fixture-owner')
        self.baseline = CandidateProfile(
            personal_information={'full_name': 'Fixture Person', 'email': 'person@example.invalid',
                                  'phone': 'fixture-phone', 'location': 'Fixture City'},
            education=[{'degree': 'Fixture degree'}], target_roles=['Fixture role'],
            employment_preferences=['full_time'], work_authorization={'status': 'authorized'},
            sponsorship_information={'required': False},
        )
        job = Job(company='Fixture Company', title='Fixture role', source='local', source_job_id='1')
        resume_file = root / 'resume.txt'
        resume_file.write_text('Local synthetic fixture')
        resume = Resume(candidate_id=self.baseline.id, name='Fixture resume', file_path=str(resume_file),
                        job_family='fixture', version='1')
        for record in (self.baseline, job, resume):
            self.ok(self.service.save_library_record(record))
        self.app_id = self.ok(self.service.create_application(self.baseline.id, job.id, resume.id)).entity_id
        self.ok(self.service.transition_application(self.app_id, ApplicationStatus.SHORTLISTED))
        self.requirements = ReadinessRequirements(application_id=self.app_id, candidate_id=self.baseline.id, job_id=job.id, context={'form_id':'fixture-form','form_version':'1'})
        self.ok(self.service.save_requirements(self.requirements))
        self.review()

    def ok(self, result):
        self.assertFalse(result.blocked, result.reasons)
        return result

    def get(self, model, identity):
        with self.db.transaction() as repo:
            return repo.get(model, identity)

    def review(self):
        req = self.get(ReadinessRequirements, self.requirements.id)
        self.ok(self.owner.review_requirements(req.id, expected_revision=req.revision, reason='Owner reviewed fixture'))

    def candidate_with(self, path, value):
        candidate = deepcopy(self.baseline)
        candidate.revision = self.get(CandidateProfile, candidate.id).revision
        parts = path.split('.')
        if len(parts) == 1:
            setattr(candidate, path, value)
        else:
            getattr(candidate, parts[0])[parts[1]] = value
        return candidate

    def set_value(self, path, value):
        self.ok(self.service.save_library_record(self.candidate_with(path, value), update=True))
        # Review current requirements so rejection cannot be a stale-approval false positive.
        self.review()

    def assert_blocked_value(self, path, value):
        self.set_value(path, value)
        result = self.service.prepare_application(self.app_id)
        self.assertTrue(result.blocked)
        self.assertEqual(result.reasons, ('missing_candidate_field:' + path,))
        app = self.get(Application, self.app_id)
        self.assertEqual(app.status, ApplicationStatus.BLOCKED)
        self.assertIsNone(app.readiness)
        with self.db.transaction() as repo:
            self.assertTrue(repo.list(UnresolvedInformation, application_id=self.app_id, field_path=path))

    def assert_ready(self):
        self.ok(self.service.prepare_application(self.app_id))
        self.assertEqual(self.get(Application, self.app_id).status, ApplicationStatus.READY_TO_APPLY)

    def test_false_textual_identity_values_block_each_field(self):
        for path in GLOBAL_REQUIRED_FIELDS[:4]:
            with self.subTest(path=path):
                self.assert_blocked_value(path, False)

    def test_blank_and_null_textual_identity_values_block(self):
        for path in GLOBAL_REQUIRED_FIELDS[:4]:
            for value in (None, '', '  \t\n'):
                with self.subTest(path=path, value=value):
                    self.assert_blocked_value(path, value)

    def test_invalid_nested_identity_types_block(self):
        for path in GLOBAL_REQUIRED_FIELDS[:4]:
            for value in (True, 0, 42, [], {}, ['supplied'], {'value': 'supplied'}):
                with self.subTest(path=path, value=value):
                    self.assert_blocked_value(path, value)

    def test_authorization_accepts_exactly_the_two_controlled_values(self):
        for value in ('authorized', 'not_authorized'):
            with self.subTest(value=value):
                self.set_value('work_authorization.status', value)
                self.assert_ready()

    def test_unknown_and_other_authorization_values_block(self):
        for value in ('unknown', '', ' ', None, 'pending', 'OPT', 'CPT', 'visa', 'AUTHORIZED',
                      ' authorized ', True, False, 0, [], {}, {'status': 'authorized'}):
            with self.subTest(value=value):
                self.assert_blocked_value('work_authorization.status', value)

    def test_sponsorship_accepts_both_actual_booleans(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.set_value('sponsorship_information.required', value)
                self.assert_ready()

    def test_unknown_and_nonboolean_sponsorship_values_block(self):
        for value in ('unknown', '', ' ', None, 'true', 'false', 'yes', 'no', 0, 1, [], {}, {'required': False}):
            with self.subTest(value=value):
                self.assert_blocked_value('sponsorship_information.required', value)

    def test_each_meaningful_education_field_alone_is_valid(self):
        for key in ('degree', 'institution', 'field_of_study'):
            with self.subTest(key=key):
                self.set_value('education', [{key: 'Owner-supplied education', 'graduation_date': None}])
                self.assert_ready()

    def test_id_only_blank_and_irrelevant_education_block(self):
        for value in ([], [{}], [{'id': 'admin-123'}], [{'graduation_date': '2020'}],
                      [{'id': 'admin-123', 'degree': ' ', 'institution': '', 'field_of_study': '\t'}],
                      [{'degree': None, 'institution': None, 'field_of_study': None}],
                      [{'description': 'An arbitrary administrative note'}]):
            with self.subTest(value=value):
                self.assert_blocked_value('education', value)

    def test_nontext_education_information_does_not_count(self):
        for key in ('degree', 'institution', 'field_of_study'):
            for value in (True, False, 0, [], {}, ['Education'], {'name': 'Education'}):
                with self.subTest(key=key, value=value):
                    self.assert_blocked_value('education', [{key: value, 'id': 'admin-123'}])

    def test_one_meaningful_education_value_suffices_across_entries(self):
        self.set_value('education', [{'id': 'admin-123'}, {'institution': 'Fixture School', 'degree': '', 'field_of_study': None}])
        self.assert_ready()

    def test_empty_or_blank_roles_and_employment_preferences_block(self):
        for path in ('target_roles', 'employment_preferences'):
            for value in ([], [''], [' \t'], ['Fixture choice', ' ']):
                with self.subTest(path=path, value=value):
                    self.assert_blocked_value(path, value)

    def test_valid_roles_and_employment_preferences(self):
        for path, value in (('target_roles', ['Role A', 'Role B']),
                            ('employment_preferences', ['full_time', 'part_time'])):
            self.set_value(path, value)
            self.assert_ready()

    def test_invalid_collection_types_are_rejected_at_persistence_and_validation(self):
        cases = {
            'education': (False, 'degree', {'degree': 'Degree'}, None, [False], ['Degree']),
            'target_roles': (False, 'Role', {}, None, [False], [123]),
            'employment_preferences': (False, 'full_time', {}, None, [True], [123]),
        }
        for path, values in cases.items():
            for value in values:
                with self.subTest(path=path, value=value):
                    self.assertFalse(valid_required_value(path, value))
                    before = self.get(CandidateProfile, self.baseline.id)
                    result = self.service.save_library_record(self.candidate_with(path, value), update=True)
                    self.assertTrue(result.blocked)
                    self.assertEqual(self.get(CandidateProfile, self.baseline.id), before)

    def test_optional_fields_remain_optional_and_global_set_is_exact(self):
        self.assertEqual(set(GLOBAL_REQUIRED_FIELDS), {
            'personal_information.full_name', 'personal_information.email',
            'personal_information.phone', 'personal_information.location', 'education',
            'target_roles', 'employment_preferences', 'work_authorization.status',
            'sponsorship_information.required',
        })
        # Baseline has no salary, links, street address, certifications, skills, or graduation date.
        self.assert_ready()

    def test_existing_valid_profile_reaches_readiness(self):
        self.assert_ready()

    def test_invalid_edit_revokes_existing_readiness_and_repair_restores_it(self):
        self.assert_ready()
        self.assert_blocked_value('personal_information.full_name', False)
        self.set_value('personal_information.full_name', 'Fixture Person')
        self.assert_ready()

    def test_restricted_worker_prepare_cannot_bypass_value_validation(self):
        self.set_value('work_authorization.status', 'unknown')
        dispatcher = TrustedWorkerDispatcher(self.db, WorkerScope('fixture-worker', self.baseline.id))
        response = json.loads(dispatcher.handle(json.dumps({
            'command': 'prepare_application', 'arguments': {'application_id': self.app_id},
        }).encode()))
        self.assertEqual(response['status'], 'blocked')
        self.assertIn('missing_candidate_field:work_authorization.status', response['reasons'])
        self.assertEqual(self.get(Application, self.app_id).status, ApplicationStatus.BLOCKED)

    def test_specific_optional_boolean_requirement_preserves_false_as_information(self):
        candidate = self.candidate_with('other_criteria', {'willing_to_relocate': False})
        self.ok(self.service.save_library_record(candidate, update=True))
        req = self.get(ReadinessRequirements, self.requirements.id)
        req.required_candidate_fields = ['other_criteria.willing_to_relocate']
        self.ok(self.service.save_requirements(req, update=True))
        self.review()
        self.assert_ready()
