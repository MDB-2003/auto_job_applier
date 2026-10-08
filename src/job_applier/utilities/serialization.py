"""Validated JSON serialization; explicit conversion of nested domain types."""
from dataclasses import asdict
import json
from job_applier.models import (
    Application, ApplicationStatus, ApprovedAnswer, ApprovalState, CandidateProfile,
    JobCriteria, Outreach, OutreachStatus, Record, SalaryPreferences,
)
from job_applier.models.validation import validate_record


def encode(record: Record) -> str:
    validate_record(record)
    return json.dumps(asdict(record), allow_nan=False, sort_keys=True)


def decode(model: type[Record], payload: str) -> Record:
    values = json.loads(payload)
    if not isinstance(values, dict): raise ValueError("invalid_record_payload")
    if model is Application:
        values['status'] = ApplicationStatus(values['status'])
    elif model is Outreach:
        values['status'] = OutreachStatus(values['status'])
    elif model is ApprovedAnswer:
        values['approval_state'] = ApprovalState(values['approval_state'])
    elif model is CandidateProfile:
        values['salary_preferences'] = SalaryPreferences(**values['salary_preferences'])
    elif model is JobCriteria:
        values['salary_requirements'] = SalaryPreferences(**values['salary_requirements'])
    record = model(**values)
    validate_record(record)
    return record
