import math
from pathlib import Path
import unittest
from job_applier.config import Settings
from job_applier.models import ApplicationStatus as A, OutreachStatus as O, SalaryPreferences
from job_applier.models.states import APPLICATION_TRANSITIONS, OUTREACH_TRANSITIONS, validate_transition
from job_applier.services.matching import calculate_match_score


class ConfigTests(unittest.TestCase):
    def test_defaults_and_environment(self):
        self.assertEqual(Settings.load({}).database_path, Path("data/job_agent.sqlite3"))
        settings = Settings.load({"JOB_AGENT_DATABASE_PATH": "test.db", "JOB_AGENT_LOG_LEVEL": "debug"})
        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.database_path, Path("test.db"))

    def test_invalid_configuration(self):
        for value in ("", " ", ":memory:", "https://example.invalid/db"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings.load({"JOB_AGENT_DATABASE_PATH": value})
        with self.assertRaises(ValueError):
            Settings.load({"JOB_AGENT_LOG_LEVEL": "unknown"})


class StateAndScoreTests(unittest.TestCase):
    def test_all_state_pairs_follow_graph(self):
        for enum, graph in ((A, APPLICATION_TRANSITIONS), (O, OUTREACH_TRANSITIONS)):
            self.assertEqual(set(enum), set(graph))
            for previous in enum:
                for target in enum:
                    if target in graph[previous]:
                        validate_transition(previous, target)
                    else:
                        with self.assertRaises(ValueError):
                            validate_transition(previous, target)
        with self.assertRaises(ValueError):
            validate_transition(A.CLOSED, O.CLOSED)

    def test_score_is_weighted_match_not_probability(self):
        result = calculate_match_score({"skills": 3, "location": 1}, {"skills": 1, "location": 0}, eligible=True)
        self.assertFalse(result.blocked)
        self.assertEqual(result.score, 75)
        self.assertEqual(result.contributions, {"skills": 75, "location": 0})

    def test_missing_evidence_and_eligibility_block(self):
        for eligible in (None, False):
            self.assertTrue(calculate_match_score({"skills": 1}, {"skills": 1}, eligible=eligible).blocked)
        result = calculate_match_score({"skills": 1}, {}, eligible=True)
        self.assertTrue(result.blocked)
        self.assertIsNone(result.score)

    def test_invalid_scores_rejected(self):
        for weights, evidence in (({}, {}), ({"invented": 1}, {}), ({"skills": -1}, {}), ({"skills": math.nan}, {}), ({"skills": 0}, {}), ({"skills": 1}, {"skills": 1.1}), ({"skills": 1}, {"skills": math.inf})):
            with self.subTest(weights=weights, evidence=evidence), self.assertRaises(ValueError):
                calculate_match_score(weights, evidence, eligible=True)

    def test_salary_validation(self):
        for values in ({"minimum": -1}, {"minimum": 10, "maximum": 1}, {"maximum": math.nan}):
            with self.assertRaises(ValueError):
                SalaryPreferences(**values)


if __name__ == "__main__":
    unittest.main()
