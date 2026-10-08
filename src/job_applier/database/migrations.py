"""Ordered, atomic local migrations. Invalid legacy data aborts without data loss."""
import json
import sqlite3
from job_applier.database.legacy import SCHEMA_V1
from job_applier.database.schema import CURRENT_VERSION, TABLES, statements
from job_applier.database import _DOMAIN_WRITE
from job_applier.models import *
from job_applier.utilities.serialization import decode


def execute_script(connection, script):
    statement = ''
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ''
    if statement.strip(): raise RuntimeError('incomplete_migration_statement')


def version_1(connection): execute_script(connection, SCHEMA_V1)


def version_2(connection):
    from job_applier.database.sqlite import SQLiteRepository
    old = {}
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        rows = []
        for identity, payload in connection.execute(f'SELECT id,payload FROM {table}'):
            data = json.loads(payload)
            if not identity or not isinstance(data,dict) or data.get('id') != identity:
                raise ValueError('invalid_legacy_identity')
            data['revision'] = 1
            rows.append(data)
        old[table] = rows
    for table in tables: connection.execute(f'DROP TABLE {table}')
    for statement in statements(): connection.execute(statement)
    repo = SQLiteRepository(connection,capability=_DOMAIN_WRITE)
    generated = {model:[] for model in TABLES}
    job_map = {}
    changes = []
    for data in old.get('jobs',[]):
        canonical = CanonicalOpening(company=data['company'],title=data['title'],provenance={'migration':'v1_to_v2','source_record_id':data['id']})
        generated[CanonicalOpening].append(canonical)
        data['canonical_id'] = canonical.id
        job_map[data['id']] = canonical.id
        generated[JobLinkEvidence].append(JobLinkEvidence(source_record_id=data['id'],canonical_id=canonical.id,method='initial_source',evidence={'migration':'v1_to_v2','source':data['source'],'source_job_id':data['source_job_id']}))
    for model, (table, _) in TABLES.items():
        for data in old.get(table,[]):
            if model is ApprovedAnswer:
                previous = data['approval_state']
                data.update(approval_state='draft',approval=None,candidate_revision=None)
                if previous != 'draft': changes.append({'entity_id':data['id'],'previous':previous,'new':'draft'})
            if model is AuditEvent:
                data.update(actor_id='legacy_unattributed',actor_type='SYSTEM')
                data['metadata']={**data.get('metadata',{}),'legacy_actor_unavailable':True}
            if model is Contact:
                if data.get('verified'):
                    changes.append({'entity_id':data['id'],'previous':'verified','new':'unverified'})
                data.update(verified=False,verification=None,verification_revision=None,verification_source=None,verified_at=None)
            if model is Application:
                data['canonical_id'] = job_map[data['job_id']]
                previous = data['status']
                data.update(readiness=None,resume_revision=None)
                if previous == 'submission_failed':
                    attempt = SubmissionAttempt(application_id=data['id'],previous_state=previous)
                    generated[SubmissionAttempt].append(attempt)
                    data.update(status='submission_uncertain',latest_attempt_id=attempt.id,block_reasons=['legacy_outcome_requires_reconciliation'])
                elif previous == 'ready_to_apply':
                    data.update(status='blocked',block_reasons=['migration_requires_review'])
                if data['status'] != previous: changes.append({'entity_id':data['id'],'previous':previous,'new':data['status']})
            if model is Outreach and data['status'] in {'contact_verified','draft_created','approved'}:
                changes.append({'entity_id':data['id'],'previous':data['status'],'new':'blocked'})
                data.update(status='blocked',block_reasons=['migration_requires_verification'],approval=None)
            generated[model].append(decode(model,json.dumps(data)))
    # TABLES order respects all immediate references. Attempts follow applications.
    for model in TABLES:
        for record in generated[model]: repo.add(record)
    repo.add(AuditEvent(action='migrate_schema',entity='Database',entity_id='local_database',previous_state=1,new_state=2,result='success',source='local_migration',actor_id='migration',actor_type='SYSTEM',metadata={'safety_changes':changes,'legacy_approvals_require_human_review':True}))


def version_3(connection):
    # Remove text-only uniqueness without attributing context to historical data.
    rows = connection.execute('SELECT id,revision,payload,candidate_id,question,approval_state FROM approved_answers').fetchall()
    connection.execute('DROP TABLE approved_answers')
    for statement in statements():
        if statement.startswith(('CREATE TABLE approved_answers ', 'CREATE INDEX idx_approved_answers_', 'CREATE TRIGGER approved_answers_')):
            connection.execute(statement)
    connection.executemany('INSERT INTO approved_answers(id,revision,payload,candidate_id,question,approval_state) VALUES (?,?,?,?,?,?)', rows)
    from job_applier.database.sqlite import SQLiteRepository
    SQLiteRepository(connection,capability=_DOMAIN_WRITE).add(AuditEvent(
        action='migrate_schema',entity='Database',entity_id='local_database',previous_state=2,new_state=3,
        result='success',source='local_migration',actor_id='migration',actor_type='SYSTEM',
        metadata={'legacy_context_not_invented':True}))


MIGRATIONS = {1: version_1, 2: version_2, 3: version_3}


def migrate(connection):
    # Disable references only during the atomic table rebuild; validate before commit.
    connection.execute('PRAGMA foreign_keys=OFF')
    try:
        connection.execute('BEGIN IMMEDIATE')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version < 0 or version > CURRENT_VERSION: raise RuntimeError('unsupported_schema_version')
        for target in range(version+1,CURRENT_VERSION+1):
            MIGRATIONS[target](connection)
            connection.execute(f'PRAGMA user_version={target}')
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('migration_relationship_violation')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally: connection.execute('PRAGMA foreign_keys=ON')
