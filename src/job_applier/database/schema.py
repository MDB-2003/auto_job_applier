"""Schema v3: contextual answer multiplicity and guarded relational identities."""
from job_applier.models import *

CURRENT_VERSION = 3
TABLES = {
    CandidateProfile: ('candidate_profiles', ()),
    CanonicalOpening: ('canonical_openings', ('company','requisition_id','canonical_url')),
    Job: ('jobs', ('source','source_job_id','canonical_id')),
    JobLinkEvidence: ('job_link_evidence', ('source_record_id','canonical_id')),
    Resume: ('resumes', ('candidate_id',)),
    ApprovedAnswer: ('approved_answers', ('candidate_id','question','approval_state')),
    JobCriteria: ('job_criteria', ('candidate_id',)),
    Application: ('applications', ('candidate_id','job_id','canonical_id','resume_id','status')),
    Contact: ('contacts', ('email',)),
    Outreach: ('outreach', ('candidate_id','contact_id','application_id','status')),
    EmailEvent: ('email_events', ('source','external_id','application_id','outreach_id')),
    ReadinessRequirements: ('readiness_requirements', ('application_id','candidate_id','job_id')),
    UnresolvedInformation: ('unresolved_information', ('application_id','job_id','resolved')),
    SubmissionAttempt: ('submission_attempts', ('application_id','outcome')),
    AuditEvent: ('audit_logs', ('timestamp','entity','entity_id','correlation_id','action')),
}
REFERENCES = {
    'candidate_id':'candidate_profiles', 'job_id':'jobs', 'canonical_id':'canonical_openings',
    'resume_id':'resumes', 'contact_id':'contacts', 'application_id':'applications',
    'outreach_id':'outreach', 'source_record_id':'jobs',
}
NULLABLE = {('canonical_openings','requisition_id'),('canonical_openings','canonical_url'),
    ('applications','resume_id'),('contacts','email'),('outreach','application_id'),
    ('email_events','application_id'),('email_events','outreach_id')}
UNIQUE = {
    'jobs':[('source','source_job_id')],
    'applications':[('candidate_id','canonical_id')], 'email_events':[('source','external_id')],
    'readiness_requirements':[('application_id',)],
}


def statements():
    for model, (table, projections) in TABLES.items():
        columns = ["id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0)",
            "revision INTEGER NOT NULL CHECK(revision >= 1)",
            "payload TEXT NOT NULL CHECK(json_valid(payload) AND json_type(payload) = 'object')",
            "CHECK(json_extract(payload, '$.id') IS id)",
            "CHECK(json_extract(payload, '$.revision') IS revision)"]
        # Table constraints must follow all column declarations.
        declarations, checks = columns[:3], columns[3:]
        for name in projections:
            typ = 'INTEGER' if name == 'resolved' else 'TEXT'
            declaration = f'{name} {typ}'
            if (table,name) not in NULLABLE: declaration += ' NOT NULL'
            if name in REFERENCES: declaration += f' REFERENCES {REFERENCES[name]}(id)'
            if typ == 'TEXT': declaration += f' CHECK({name} IS NULL OR length(trim({name})) > 0)'
            declarations.append(declaration)
            checks.append(f"CHECK(json_extract(payload, '$.{name}') IS {name})")
        if model is Application:
            checks.append('CHECK(status IN (' + ','.join(repr(s.value) for s in ApplicationStatus) + '))')
        if model is Outreach:
            checks.append('CHECK(status IN (' + ','.join(repr(s.value) for s in OutreachStatus) + '))')
        if model is ApprovedAnswer:
            checks.append("CHECK(approval_state IN ('draft','approved','revoked'))")
        if model is SubmissionAttempt:
            checks.append("CHECK(outcome IN ('uncertain','confirmed_submitted','confirmed_not_submitted'))")
        checks.extend('UNIQUE('+','.join(keys)+')' for keys in UNIQUE.get(table,[]))
        yield f'CREATE TABLE {table} (' + ','.join(declarations+checks) + ')'
        if model is not AuditEvent:
            for operation in ('INSERT','UPDATE','DELETE'):
                yield f"CREATE TRIGGER {table}_domain_{operation.lower()} BEFORE {operation} ON {table} WHEN domain_write_allowed() != 1 BEGIN SELECT RAISE(ABORT, 'domain_service_required'); END"
        # Index every projected lookup; composite uniqueness indexes cover their first field.
        covered = {keys[0] for keys in UNIQUE.get(table,[])}
        for name in projections:
            if name not in covered:
                yield f'CREATE INDEX idx_{table}_{name} ON {table}({name})'
    for operation in ('UPDATE','DELETE'):
        yield f"CREATE TRIGGER audit_no_{operation.lower()} BEFORE {operation} ON audit_logs BEGIN SELECT RAISE(ABORT, 'audit_append_only'); END"
    yield "CREATE TRIGGER audit_no_replace BEFORE INSERT ON audit_logs WHEN EXISTS(SELECT 1 FROM audit_logs WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT, 'audit_identity_exists'); END"
    # Enforce ownership even if a future service implementation makes a mistake.
    rules = {
        'applications': "(NEW.resume_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM resumes WHERE id=NEW.resume_id AND candidate_id=NEW.candidate_id)) OR NOT EXISTS(SELECT 1 FROM jobs WHERE id=NEW.job_id AND canonical_id=NEW.canonical_id)",
        'outreach': "NEW.application_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM applications WHERE id=NEW.application_id AND candidate_id=NEW.candidate_id)",
        'readiness_requirements': "NOT EXISTS(SELECT 1 FROM applications WHERE id=NEW.application_id AND candidate_id=NEW.candidate_id AND job_id=NEW.job_id)",
        'unresolved_information': "NOT EXISTS(SELECT 1 FROM applications WHERE id=NEW.application_id AND job_id=NEW.job_id)",
        'email_events': "NEW.application_id IS NOT NULL AND NEW.outreach_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM outreach WHERE id=NEW.outreach_id AND application_id=NEW.application_id)",
    }
    for table, rule in rules.items():
        for operation in ('INSERT','UPDATE'):
            yield f"CREATE TRIGGER {table}_ownership_{operation.lower()} BEFORE {operation} ON {table} WHEN {rule} BEGIN SELECT RAISE(ABORT, 'ownership_mismatch'); END"
