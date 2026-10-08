"""Domain records; optional facts remain unknown until provided."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid4())


class ApplicationStatus(str, Enum):
    DISCOVERED = "discovered"
    SHORTLISTED = "shortlisted"
    READY_TO_APPLY = "ready_to_apply"
    BLOCKED_UNKNOWN_QUESTION = "blocked_unknown_question"
    BLOCKED = "blocked"
    SUBMISSION_FAILED = "submission_failed"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted"
    SUBMITTED = "submitted"
    APPLICATION_RECEIVED = "application_received"
    UNDER_REVIEW = "under_review"
    RECRUITER_SCREEN = "recruiter_screen"
    INTERVIEW = "interview"
    ASSESSMENT = "assessment"
    FINAL_INTERVIEW = "final_interview"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


class OutreachStatus(str, Enum):
    PERSON_DISCOVERED = "person_discovered"
    CONTACT_VERIFIED = "contact_verified"
    DRAFT_CREATED = "draft_created"
    APPROVED = "approved"
    SENT = "sent"
    REPLIED = "replied"
    FOLLOW_UP_REQUIRED = "follow_up_required"
    BLOCKED = "blocked"
    CLOSED = "closed"


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REVOKED = "revoked"


@dataclass(kw_only=True)
class Record:
    revision: int = 1
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)


@dataclass(kw_only=True)
class SalaryPreferences:
    minimum: float | None = None
    maximum: float | None = None
    currency: str | None = None
    period: str | None = None

    def __post_init__(self) -> None:
        import math
        for value in (self.minimum, self.maximum):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("Salary values must be finite and nonnegative")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Minimum salary exceeds maximum")


@dataclass(kw_only=True)
class CandidateProfile(Record):
    personal_information: dict[str, Any] = field(default_factory=lambda: {
        "full_name": None, "email": None, "phone": None, "location": None,
    })
    education: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    experience: list[dict[str, Any]] = field(default_factory=list)
    target_roles: list[str] = field(default_factory=list)
    preferred_locations: list[str] = field(default_factory=list)
    work_authorization: dict[str, Any] = field(default_factory=dict)
    sponsorship_information: dict[str, Any] = field(default_factory=dict)
    salary_preferences: SalaryPreferences = field(default_factory=SalaryPreferences)
    employment_preferences: list[str] = field(default_factory=list)
    other_criteria: dict[str, Any] = field(default_factory=dict)
    # Unresolved contradictory facts, retained for human resolution.
    conflicts: dict[str, list[Any]] = field(default_factory=dict)


@dataclass(kw_only=True)
class Resume(Record):
    candidate_id: str
    name: str
    file_path: str
    job_family: str
    version: str
    active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str | None = None


@dataclass(kw_only=True)
class ApprovedAnswer(Record):
    candidate_id: str
    question: str
    answer: str
    category: str
    source: str
    approval_state: ApprovalState = ApprovalState.DRAFT
    content_revision: int = 1
    approval: dict[str, Any] | None = None
    candidate_revision: int | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not all(v.strip() for v in (self.question, self.answer, self.category, self.source)):
            raise ValueError("Question, answer, category, and source must be nonempty")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("Confidence must be between zero and one")


@dataclass(kw_only=True)
class JobCriteria(Record):
    candidate_id: str
    target_roles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    work_modes: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    salary_requirements: SalaryPreferences = field(default_factory=SalaryPreferences)
    experience_requirements: dict[str, Any] = field(default_factory=dict)
    education_requirements: list[str] = field(default_factory=list)
    sponsorship_requirements: dict[str, Any] = field(default_factory=dict)
    excluded_companies: list[str] = field(default_factory=list)
    excluded_roles: list[str] = field(default_factory=list)
    other_filters: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class Job(Record):
    canonical_id: str | None = None
    canonical_url: str | None = None
    requisition_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    title: str
    company: str
    source: str
    source_job_id: str
    location: str | None = None
    work_mode: str | None = None
    employment_type: str | None = None
    posted_at: str | None = None
    requirements: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class Application(Record):
    canonical_id: str | None = None
    resume_revision: int | None = None
    readiness: dict[str, Any] | None = None
    latest_attempt_id: str | None = None
    candidate_id: str
    job_id: str
    resume_id: str | None = None
    status: ApplicationStatus = ApplicationStatus.DISCOVERED
    block_reasons: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class Contact(Record):
    name: str
    company: str | None = None
    email: str | None = None
    verified: bool = False
    verification_revision: int | None = None
    verification: dict[str, Any] | None = None
    verification_source: str | None = None
    verified_at: str | None = None


@dataclass(kw_only=True)
class Outreach(Record):
    contact_revision: int | None = None
    candidate_revision: int | None = None
    approval: dict[str, Any] | None = None
    candidate_id: str
    contact_id: str
    application_id: str | None = None
    status: OutreachStatus = OutreachStatus.PERSON_DISCOVERED
    draft: str | None = None
    block_reasons: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class EmailEvent(Record):
    source: str
    external_id: str
    event_type: str
    occurred_at: str
    application_id: str | None = None
    outreach_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class AuditEvent(Record):
    correlation_id: str = field(default_factory=new_id)
    actor_id: str = "local_worker"
    actor_type: str = "WORKER"
    timestamp: str = field(default_factory=now)
    action: str
    entity: str
    entity_id: str
    previous_state: Any
    new_state: Any
    result: str
    source: str
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class CanonicalOpening(Record):
    company: str
    title: str
    canonical_url: str | None = None
    requisition_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class JobLinkEvidence(Record):
    source_record_id: str
    canonical_id: str
    evidence: dict[str, Any]
    method: str
    approval: dict[str, Any] | None = None


@dataclass(kw_only=True)
class ReadinessRequirements(Record):
    application_id: str
    candidate_id: str
    job_id: str
    required_candidate_fields: list[str] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    content_revision: int = 1
    approval: dict[str, Any] | None = None
    candidate_revision: int | None = None
    job_revision: int | None = None


@dataclass(kw_only=True)
class UnresolvedInformation(Record):
    application_id: str
    job_id: str
    question_text: str | None = None
    field_path: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    block_reason: str
    resolved: bool = False
    resolution: dict[str, Any] | None = None


@dataclass(kw_only=True)
class SubmissionAttempt(Record):
    application_id: str
    previous_state: str
    # Legacy JSON has no reliable execution snapshot; never invent one on load.
    action_state: str = 'legacy'
    candidate_id: str | None = None
    job_id: str | None = None
    canonical_id: str | None = None
    resume_id: str | None = None
    correlation_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    outcome: str = "uncertain"
    evidence: list[dict[str, Any]] = field(default_factory=list)
    resolution: dict[str, Any] | None = None
    retry_authorization: dict[str, Any] | None = None
    retry_consumed: bool = False
