"""Runtime validation shared by persistence and domain operations."""
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from enum import Enum
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints, Union
from urllib.parse import urlsplit
from job_applier.models import *

GLOBAL_REQUIRED_FIELDS = (
    "personal_information.full_name", "personal_information.email",
    "personal_information.phone", "personal_information.location", "education",
    "target_roles", "employment_preferences", "work_authorization.status",
    "sponsorship_information.required",
)
FACT_ROOTS = frozenset({"personal_information", "education", "skills", "experience",
    "target_roles", "preferred_locations", "work_authorization", "sponsorship_information",
    "salary_preferences", "employment_preferences", "other_criteria"})


def nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_path(path: str) -> None:
    if not nonblank(path) or path.split('.')[0] not in FACT_ROOTS or any(
        not part.isidentifier() or part.startswith('_') or part in {"id", "created_at", "updated_at", "revision"}
        for part in path.split('.')
    ):
        raise ValueError("invalid_candidate_requirement")


def fact(candidate: CandidateProfile, path: str) -> Any:
    validate_path(path)
    value: Any = candidate
    for part in path.split('.'):
        if is_dataclass(value):
            value = getattr(value, part, None)
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def present(value: Any) -> bool:
    if value is None: return False
    if isinstance(value, str): return bool(value.strip())
    if is_dataclass(value): return present(asdict(value))
    if isinstance(value, (list, dict)):
        values = value.values() if isinstance(value, dict) else value
        # A globally required collection needs supplied information, not every
        # optional subfield. Specific required subfields use explicit paths.
        return bool(value) and any(present(item) for item in values)
    return True  # A known False or zero is not missing.


def valid_required_value(path: str, value: Any) -> bool:
    """Validate the nine global facts; preserve presence rules for extra paths.

    Incomplete candidate drafts may be stored. This check gates readiness,
    not factual truth, eligibility, or a new requirement for optional data.
    """
    if path in GLOBAL_REQUIRED_FIELDS[:4]:
        return nonblank(value)
    if path == 'work_authorization.status':
        return isinstance(value, str) and value in ('authorized', 'not_authorized')
    if path == 'sponsorship_information.required':
        return type(value) is bool
    if path == 'education':
        return isinstance(value, list) and all(isinstance(entry, dict) for entry in value) and any(
            nonblank(entry.get(key))
            for entry in value for key in ('degree', 'institution', 'field_of_study')
        )
    if path in ('target_roles', 'employment_preferences'):
        return isinstance(value, list) and bool(value) and all(nonblank(item) for item in value)
    return present(value)


def check_type(value: Any, hint: Any) -> bool:
    if hint is Any: return True
    origin, args = get_origin(hint), get_args(hint)
    if origin in (Union, UnionType): return any(check_type(value, h) for h in args)
    if origin is list: return isinstance(value, list) and all(check_type(v, args[0]) for v in value)
    if origin is dict: return isinstance(value, dict) and all(check_type(k,args[0]) and check_type(v,args[1]) for k,v in value.items())
    if hint is float: return type(value) in (int, float)
    if hint in (int, bool): return type(value) is hint
    return isinstance(value, hint)


def validate_record(record: Record) -> None:
    for name, hint in get_type_hints(type(record)).items():
        if not check_type(getattr(record, name), hint):
            raise ValueError("invalid_field_type:" + name)
    if not nonblank(record.id) or record.revision < 1:
        raise ValueError("invalid_identity_or_revision")
    for name in ("created_at", "updated_at"):
        if datetime.fromisoformat(getattr(record, name)).tzinfo is None:
            raise ValueError("timestamp_requires_timezone")
    for f in fields(record):
        if f.name.endswith('_id') and getattr(record, f.name) is not None and not nonblank(getattr(record, f.name)):
            raise ValueError("invalid_identity:" + f.name)
    if isinstance(record, ApprovedAnswer):
        record.__post_init__()
        if record.content_revision < 1: raise ValueError("invalid_content_revision")
        if record.approval_state == ApprovalState.APPROVED:
            validate_approval(record.approval)
            if record.approval['approved_content_revision'] != record.content_revision or not record.candidate_revision:
                raise ValueError("stale_answer_approval")
        elif record.approval is not None:
            raise ValueError("unapproved_answer_has_approval")
    if isinstance(record, (CandidateProfile, JobCriteria)):
        salary = record.salary_preferences if isinstance(record, CandidateProfile) else record.salary_requirements
        salary.__post_init__()
    if isinstance(record, Job):
        if not all(nonblank(v) for v in (record.title, record.company, record.source, record.source_job_id, record.canonical_id)):
            raise ValueError("missing_job_identity")
    if isinstance(record, CanonicalOpening) and not all(nonblank(v) for v in (record.company, record.title)):
        raise ValueError("missing_canonical_identity")
    if isinstance(record, Resume) and not all(nonblank(v) for v in (record.name, record.file_path, record.job_family, record.version)):
        raise ValueError("invalid_resume_metadata")
    if isinstance(record, Contact):
        if not nonblank(record.name): raise ValueError("missing_contact_name")
        if record.verified:
            validate_approval(record.verification)
            if record.verification_revision != record.revision or not all(nonblank(v) for v in (record.email,record.verification_source,record.verified_at)):
                raise ValueError("stale_contact_verification")
    if isinstance(record, ReadinessRequirements):
        for path in record.required_candidate_fields: validate_path(path)
        for question in record.questions:
            if not nonblank(question.get('text')) or not isinstance(question.get('context', {}), dict):
                raise ValueError("invalid_question")
        if record.approval:
            validate_approval(record.approval)
            if record.approval['approved_content_revision'] != record.content_revision:
                raise ValueError("stale_requirements_approval")
    if isinstance(record, SubmissionAttempt):
        if record.action_state not in {'legacy','reserved','execution_started','uncertain','confirmed_submitted','confirmed_not_submitted'}:
            raise ValueError('invalid_attempt_action_state')
        if record.action_state != 'legacy':
            if not all(nonblank(getattr(record,key)) for key in ('candidate_id','job_id','canonical_id','resume_id','correlation_id')):
                raise ValueError('missing_attempt_identity')
            for key in ('application_revision','candidate_revision','job_revision','canonical_revision','resume_revision','requirements_revision'):
                if type(record.context.get(key)) is not int or record.context[key] < 1:
                    raise ValueError('missing_attempt_revision:' + key)
            if not nonblank(record.context.get('resume_hash')) or not isinstance(record.context.get('answer_revisions'),dict):
                raise ValueError('missing_attempt_context')
            expected = 'uncertain' if record.action_state in {'reserved','execution_started','uncertain'} else record.action_state
            if record.outcome != expected: raise ValueError('attempt_state_outcome_mismatch')
        if record.outcome not in {'uncertain','confirmed_submitted','confirmed_not_submitted'}:
            raise ValueError("invalid_submission_outcome")
        ApplicationStatus(record.previous_state)
        if record.outcome != 'uncertain':
            validate_approval(record.resolution)
            validate_evidence(record.evidence)
        if record.retry_authorization:
            validate_approval(record.retry_authorization)
            if record.outcome != 'confirmed_not_submitted': raise ValueError("retry_requires_not_submitted")
    if isinstance(record, JobLinkEvidence):
        if not record.evidence or record.method not in {'initial_source','strong_identity','human_confirmation','human_distinct'}:
            raise ValueError("invalid_link_evidence")
        if record.method in {'human_confirmation','human_distinct'}: validate_approval(record.approval)
    if isinstance(record, AuditEvent):
        if record.actor_type not in {'HUMAN','WORKER','SYSTEM'} or not all(nonblank(v) for v in (record.action,record.entity,record.entity_id,record.source,record.correlation_id,record.actor_id)):
            raise ValueError("invalid_audit_identity")
        if record.result not in {'success','blocked','rejected','failed'}: raise ValueError("invalid_audit_result")
    for name in ('canonical_url',):
        value = getattr(record, name, None)
        if value is not None:
            parsed = urlsplit(value)
            if parsed.scheme not in {'https','http'} or not parsed.netloc: raise ValueError("invalid_canonical_url")


def validate_approval(value: dict | None) -> None:
    if not isinstance(value, dict) or value.get('actor_type') != 'HUMAN':
        raise ValueError("human_approval_required")
    for name in ('actor_id','timestamp','reason','source'):
        if not nonblank(value.get(name)): raise ValueError("invalid_approval:" + name)
    if datetime.fromisoformat(value['timestamp']).tzinfo is None: raise ValueError("invalid_approval_timestamp")
    if type(value.get('previous_revision')) is not int or type(value.get('new_revision')) is not int or value['new_revision'] != value['previous_revision'] + 1:
        raise ValueError("invalid_approval_revisions")


def validate_evidence(evidence: list[dict]) -> None:
    if not evidence or any(not isinstance(e,dict) or not all(nonblank(e.get(k)) for k in ('kind','reference','description')) for e in evidence):
        raise ValueError("submission_evidence_required")
