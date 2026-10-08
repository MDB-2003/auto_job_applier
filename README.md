# Job Application Agent — Phase 1.5 local hardening

This project is a local Python foundation. It models candidate information, resumes, approved answers, job openings and source records, applications, contacts, outreach, submission uncertainty, and audit history. **It performs no external actions.** There are no agents, job searches, browser automation, mail clients, spreadsheet integrations, schedulers, or MCP integrations.

Python 3.11+ with standard-library SQLite is required. No third-party runtime or test packages are required or installed. Package installation is optional; setuptools 68+ is only a build dependency declared in `pyproject.toml`.

## Run

From the project directory:

```sh
PYTHONPATH=src python3 -m job_applier init
PYTHONPATH=src python3 -m job_applier status
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`init` atomically initializes or migrates local storage to schema version 3. `status` reads counts without running migrations or acquiring a write transaction. Neither starts a server or worker. If storage requires migration, run `init` explicitly.

Configuration remains `JOB_AGENT_DATABASE_PATH` (default `data/job_agent.sqlite3`) and `JOB_AGENT_LOG_LEVEL` (default `INFO`). Values come from the process environment. `.env.example` documents these settings; `.env` is not automatically loaded. No external credential variables exist. Database files, environment files, resumes, and generated files are excluded from version control.

## Architecture

```text
src/job_applier/
  __main__.py                 Local init/status CLI
  config.py                   Environment configuration
  models/
    __init__.py               Revisioned domain records and enums
    states.py                 Ordinary lifecycle transitions
    validation.py             Runtime types, identity, approval and field validation
  database/
    __init__.py               Repository and transaction protocols
    sqlite.py                 SQL queries and guarded domain persistence
    schema.py                 Version 3 tables, constraints, triggers, indexes
    legacy.py                 Frozen version 1 migration fixture
    migrations.py             Ordered atomic migrations
  interfaces/
    worker.py                 Restricted JSON-lines command interface
  services/
    foundation.py             Trusted-host domain operations; never exposed as an object
    human.py                  Explicit human-owner operations
    identity.py               Exact canonical identity resolution and evidence
    context.py                Exact form/question approval scope
    execution.py              Trusted single-use execution authorization
    matching.py               Supplied-evidence MATCH SCORE
  utilities/serialization.py  Validated JSON serialization
  workflows/local.py          Explicit initialization/migration
  agents/__init__.py          Reserved boundary; no executor
 tests/
  test_foundation.py          Configuration, state graph, scoring tests
  test_hardening.py           Safety, persistence, concurrency, migration regressions
  test_worker_interface.py    Worker protocol and actual subprocess boundary regressions
  test_identity_resolution.py Cross-source identity and duplicate bypass regressions
  test_candidate_values.py    Mandatory candidate-value readiness regressions
  test_submission_attempts.py Durable intent, crash recovery, and outcome regressions
  test_answer_context.py      Contextual approval and migration regressions
  test_execution_authorization.py Execution authority, replay, and scoped retry regressions
```

The domain layer depends on database protocols, not SQL. SQLite is replaceable, but another database still needs an adapter, migrations, and contract tests.

## Restricted worker boundary (blocker 1)

The supported architecture is:

```text
Worker: JSON request bytes only
  → allowlisted local stdio interface
    → trusted application/domain services
      → SQLite
```

The worker receives only the ability to send command messages and receive JSON responses. It is **not** given a Python object, callback into the host interpreter, shell, filesystem tool, database connection/path, repository, human interface, or capability token. The trusted launcher starts the host, selects the database, assigns the worker identity and candidate scope, and owns the process handles. Those decisions are not protocol arguments. No autonomous worker or external service has been implemented.

`FoundationService`, `LocalHumanOperations`, `TrustedWorkerDispatcher`, the database, and internal write capabilities are trusted-host implementation objects. They must not be registered as agent tools or passed to an agent runtime. The actual worker entry point is the **byte protocol**, not `dispatcher.handle` as an inspectable Python object. Fixed command branches and strict argument schemas enforce the allowlist at runtime; there is no generic service invocation, object construction, dynamic import, eval, SQL, pickle, or human route.

This is sufficient under the expressly selected restricted-interface model. It is **not** an OS sandbox and does not protect against a malicious process with arbitrary Python/OS/filesystem access. The privileged host can still construct human operations and access SQLite. External authentication, OS identities, and sandboxing remain outside this phase.

### Trusted launcher

After initialization and human data entry, the trusted local launcher can start:

```sh
PYTHONPATH=src python3 -m job_applier worker-interface --worker-id local-worker-1 --candidate-id EXISTING_CANDIDATE_ID
```

This command uses the existing database configuration; it does not create or migrate a database. The worker itself is not given permission to launch commands or select these flags/environment variables. The host serves local stdin/stdout messages until EOF, with diagnostics on stderr. It opens no network listener and performs no external actions.

One message is one UTF-8 JSON object on one line:

```json
{"command":"get_candidate_profile","arguments":{}}
```

Responses are detached JSON with `protocol_version`, host-generated `correlation_id`, and `status` (`ok`, `blocked`, `rejected`, or `failed`). Reads include projected `data`; domain operations include explicit `entity_id`, `reasons`, and `answers` as applicable. Replies are data, **not execution authorization**. Execution authorization belongs exclusively to the trusted host boundary documented below; it is not a worker command.

Requests are limited to 65,536 bytes and 16 levels of nesting. Duplicate JSON keys, nonstandard numeric constants, unknown commands, unexpected fields, batches, serialized objects, and authority fields are rejected. Pages default to 50 records and have a maximum of 100. Read results omit database paths, resume filesystem paths, and human approval metadata. Denied protocol requests are audited as the launcher-assigned WORKER; workers cannot select actor identity/type or correlation IDs. Domain mutations retain the existing transactional audit and share the response correlation ID. If audit storage fails, the request cannot return success.

### Exact worker allowlist

| Command | Accepted arguments / capability |
| --- | --- |
| `get_candidate_profile` | No arguments; projected profile for the assigned candidate |
| `list_jobs` | Optional `limit`, `offset`; existing local source-job summaries only |
| `list_resumes` | Optional `limit`, `offset`; assigned candidate's resume metadata, no paths |
| `list_answers` | Optional `limit`, `offset`; assigned candidate's answers and approval state, no approval authority |
| `list_applications` | Optional `limit`, `offset`; assigned candidate's applications |
| `get_application` | `application_id`; must belong to assigned candidate |
| `list_unresolved` | `application_id`, optional `limit`, `offset`; scoped unresolved-information records |
| `propose_answer` | `question`, `answer`, `category`, optional `notes`; editing additionally requires `answer_id` and `expected_revision`; always a draft with host-assigned `worker_proposal` source |
| `propose_requirements` | `application_id`, `required_candidate_fields`, `questions`, `context`; edits require `expected_revision`; always unreviewed |
| `create_application` | `job_id`, optional owned `resume_id`; candidate is assigned by host |
| `assign_resume` | Owned `application_id` and `resume_id`; delegates to audited repair operation |
| `shortlist_application` | Owned `application_id`; fixed target state, no arbitrary lifecycle argument |
| `prepare_application` | Owned `application_id`; existing readiness checks only, no submission |

All other operations are unavailable, including human approval/revocation, requirements review, conflict resolution, contact verification, reconciliation, retry authorization, canonical linking, job creation/import, candidate editing, resume file registration, outreach/sending, submission/outcome reporting, generic record writes, and raw queries. New worker commands require an explicit reviewed change to the allowlist and its schemas.

### Trusted human operations

The owner uses `LocalHumanOperations(database, owner_actor_id=...)` exclusively in trusted local code, outside the worker protocol. Human identity is still locally asserted; no authentication system was added. Worker requests to instantiate this class, invoke its methods, or import `_DOMAIN_WRITE` are rejected before dispatch. Existing trusted Python APIs remain available for owner data entry and administration.

Example **trusted-host-only** setup:

```python
from job_applier.config import Settings
from job_applier.services.foundation import FoundationService
from job_applier.services.human import LocalHumanOperations
from job_applier.workflows.local import initialize_local_database

settings = Settings.load()
database = initialize_local_database(settings)
trusted_service = FoundationService(database)
# Construct only from the trusted owner entry point with the owner's actual ID:
# owner = LocalHumanOperations(database, owner_actor_id=owner_actor_id)
# None of these Python objects is passed to an autonomous worker.
```

Trusted domain commands return `Decision` objects. The wire interface translates these to explicit JSON statuses. General data-entry and repair APIs documented below are trusted-host APIs unless listed in the worker allowlist above. Reload records after successful mutations to obtain current revisions.

Every human approval/reconciliation records `actor_id`, `actor_type=HUMAN`, timestamp, reason, previous revision, new revision, source, and correlation ID. Answer and requirement approvals also identify the approved content revision. Caller-supplied stale revisions are rejected.

## Answers and persistent requirements

General library writes only accept draft answers without approval metadata. They cannot silently create approved answers. The owner calls:

```python
# owner.approve_answer(answer_id, requirements_id=requirements_id, question_id=question_id,
#                      expected_revision=current_revision, reason=reason)
# owner.revoke_answer(answer_id, expected_revision=current_revision, reason=reason)
```

To edit an approved answer, submit a draft copy with `approval_state=ApprovalState.DRAFT` and `approval=None`. Its content revision advances, and any previous readiness is invalidated. Only a new human approval can authorize the edited content. Candidate revisions are bound to approvals; candidate changes revoke prior approved answers conservatively.

Create `ReadinessRequirements` with an application, candidate, and source-job identity, optional additional required field paths, and explicit form and question context. Requirements `context` must contain nonblank string `form_id` and `form_version`. Each question must supply `question_id`, exact `text` (the stored question_text), and a `context` dictionary, for example `{"question_id":"q1","text":"Exact question","context":{"section":"current role"}}`. Question IDs identify occurrences, are unique within a form/version, and are never inferred from text. Missing identifiers/context block review and readiness; wildcard identifiers are rejected. Save through `trusted_service.save_requirements(...)`; the owner then calls `review_requirements(id, expected_revision=..., reason=...)`. General editing cannot provide approval metadata.

`trusted_service.prepare_application(application_id)` accepts no caller-provided bypass list or reviewed flag. It reads the persisted requirements and checks current revisions plus these authoritative global fields:

| Required information | Field path |
| --- | --- |
| Full name | `personal_information.full_name` |
| Email | `personal_information.email` |
| Phone | `personal_information.phone` |
| Current location | `personal_information.location` |
| Education | `education` |
| Target roles | `target_roles` |
| Employment type preference | `employment_preferences` |
| Work authorization status | `work_authorization.status` |
| Sponsorship requirement/status | `sponsorship_information.required` |

Readiness validates the values of the nine global fields, not just their presence:

- Full name, email, phone, and current location must be nonblank strings. Booleans, numbers, collections, nulls, and whitespace-only strings do not qualify.
- Target roles and employment preferences must be nonempty lists of nonblank strings.
- Work authorization must be exactly `authorized` or `not_authorized`. Unknown values, other statuses, altered capitalization, and surrounding whitespace are rejected without coercion. A known status is information, not a determination of eligibility for a particular job.
- `sponsorship_information.required` must be an actual boolean. Both `True` and `False` are valid; strings and numbers such as `"false"` and `0` are not.
- Education must be a list of dictionaries containing at least one nonblank textual `degree`, `institution`, or `field_of_study` value across its entries. Administrative IDs, graduation dates, and other fields alone do not count. No particular one of the three meaningful fields is mandatory.

Incomplete candidate drafts can still be stored where the existing model types permit them, but cannot satisfy readiness. Invalid required values use the existing `missing_candidate_field:<path>` blocking reason and unresolved-information records. No missing value is generated. Internal identity/timestamp/revision fields cannot be requirements. Extra factual paths retain their existing presence checks for job/form-specific requirements, including `salary_preferences.minimum`. Salary, LinkedIn, GitHub, portfolio, street address, certifications, specific skills, and graduation date are not globally required.

The owner is responsible for reviewing factual accuracy and the job's actual requirements; the software validates required values, types, revisions and explicit conflicts, not real-world eligibility or truth. Identity text checks do not verify email deliverability, phone reachability, or credentials. Answer approval uses `owner.approve_answer(answer_id, requirements_id=..., question_id=..., expected_revision=..., reason=...)`. The host derives and persists exact scope from that requirements record: candidate, employer, application, source job and canonical opening, form ID/version and shared form context, question ID/text and question context. Only this human operation may bind or change scope. Worker proposals cannot supply scope or approval metadata. There are no wildcard or cross-application approvals. Matching also checks current candidate/job revisions and answer content approval. Conflicting approved answers for the same scope block instead of selecting an arbitrary record. Successful answer maps are keyed by a SHA-256 fingerprint of canonical JSON scope, not question text, so repeated texts remain distinct. It never uses fuzzy matching, confidence-based approval, or generated answers. One failed readiness check returns no partial answer set.

Missing fields and unknown/unapproved questions produce `UnresolvedInformation` records with application ID, source-job ID, timestamp, exact question/field, separate form and question context envelopes plus the exact scope, and reason. The owner's `resolve_unknown_question(unresolved_id, answer_id, reason=...)` records which current approved answer resolves a question. `resolve_candidate_information(...)` supplies missing facts or explicitly resolves a conflict, with revision and audit metadata. Worker edits cannot clear or replace previously recorded conflicts. Historical unresolved records are retained; successful presence/approval checks do not silently rewrite them as human resolutions.

## Revisions and repair

Every record has an integer revision. Updates use optimistic concurrency checks; stale copies cannot overwrite newer data. Candidate, job, answer, criteria, resume, and contact changes invalidate affected decisions. Readiness stores candidate/job/requirements/resume/answer revisions and the selected resume hash. Readiness checks re-evaluate current evidence rather than treating the stored state as authorization.

Resume registration hashes a nonempty local PDF, DOCX, or TXT file. Rechecking detects changed bytes; it does not parse or certify resume contents. `trusted_service.assign_resume(application_id, resume_id)` safely assigns/replaces a candidate-owned active resume and binds its current revision, with an audit event. A changed resume version must be explicitly selected again.

Contact verification is human-evidence-backed and tied to a record revision. No time-based expiry is imposed. Contact edits conservatively invalidate verification and affected outreach, including changes in address, company, or evidence-related data. `verify_contact(...)` records replacement evidence; `revoke_contact_verification(...)` handles explicit revocation or superseded evidence. Outbound use requires current verification. Local outreach drafts can be created and explicitly approved; sending remains disabled.

## Submission uncertainty and retry authorization

No method submits an application. The trusted host calls `reserve_submission(application_id)` before any future external action. It checks current readiness and prior attempts, then atomically persists the attempt, audit events, application pointer, and consumption of any human retry authorization. The result is returned only after commit. A rollback or commit failure returns no successful reservation receipt. This receipt is local durable intent, not execution authorization; all external actions remain disabled and the restricted worker allowlist is unchanged.

The attempt persists its ID and creation timestamp, application/candidate/job/canonical/resume identities, correlation ID, action state, and the application/candidate/job/canonical/resume/requirements revisions plus resume hash and approved-answer revisions. Its snapshot is retained through later edits and reconciliation. New attempts begin with `action_state=reserved` and `outcome=uncertain`; the application immediately becomes `submission_uncertain` and its live readiness is cleared. This deliberately does not assume whether an action actually started.

`record_submission_uncertainty(application_id, evidence=...)` and the failure-report wrapper require an existing unresolved attempt. They cannot create a post-action reservation. An inconclusive report sets `action_state=uncertain`, retains the snapshot, appends supplied evidence, and never enables retry. Human reconciliation sets the action state and outcome to `confirmed_submitted` or `confirmed_not_submitted`, or leaves both uncertain. Neither reporting uncertainty nor reconciling an outcome requires current readiness. Confirmed outcomes remain exclusively behind the human interface in this phase; there is no executor outcome connector.

On local recovery, `outstanding_submission_attempts(limit=100, offset=0)` returns persisted uncertain attempts, including reservations with no outcome report. Page until exhausted; these records require human reconciliation and must never be automatically replayed. Duplicate reservations are blocked, including concurrent requests serialized by the existing SQLite write transaction. There is no atomic transaction with an employer/ATS and no claim that a reservation proves submission.

These fields extend the existing JSON payload without changing relational schema v2. Older payloads load with `action_state=legacy` and no invented historical snapshot. Unresolved legacy attempts appear in recovery and require human reconciliation; they cannot serve as fresh reservations.

Ordinary transition commands cannot leave `submission_uncertain` or `confirmed_not_submitted`. Only the owner can reconcile:

```text
uncertain
  → inconclusive evidence: remains uncertain
  → confirmed submitted: submitted; no retry authorization
  → confirmed not submitted: waits for separate human retry authorization
      → owner authorizes one retry: shortlisted
          → fresh requirements/readiness checks
              → a new durable reservation consumes that authorization
```

Use `reconcile_submission(attempt_id, expected_revision=..., outcome=..., evidence=..., reason=...)`. Evidence entries require `kind`, `reference`, and `description`. Examples of evidence categories are a confirmation page, confirmation ID, employer confirmation email, career-portal status, or reliable evidence of non-submission. This phase stores supplied evidence only; it does not retrieve or independently authenticate it.

After `confirmed_not_submitted`, `authorize_retry(...)` is a separate human operation. Evidence, resolution, and authorization are audited together with actor and revision metadata. Inconclusive evidence never enables retry. Confirmation of submission cannot be revised to non-submission through this interface. The external-action gate remains unconditional even after local retry authorization.

## Canonical job identity

`Job` is a source record; `CanonicalOpening` represents an opening. Source records retain source, source job ID, company, optional canonical URL/requisition ID, and provenance. Import resolves exact same-company identity inside the write transaction. A unique, uncontradicted requisition-ID or canonical-URL match reuses the existing canonical opening, including identifiers from its established source aliases. Genuinely different comparable identifiers permit separate openings. No provider-specific authoritative identifier beyond these two is currently supported.

Every import persists `JobLinkEvidence`: the exact source identity, candidate canonical groups and their identifiers, matching/conflicting fields, resolution, and a fingerprint of the compared alternatives. Automatic matches also record source/canonical revisions. No title similarity, fuzzy matching, URL rewriting, or universal equivalence algorithm exists. Company and identifier strings are compared exactly; company aliases and different URL spellings are not automatically equated.

Missing comparable identifiers, contradictory identifiers, or matches to multiple canonicals require owner review. The source and evidence are retained under a provisional canonical, but import returns `identity_review_required` and application creation is blocked. The owner can use `confirm_job_equivalence(..., evidence=..., reason=...)` or `confirm_job_distinct(..., expected_revision=..., evidence=..., reason=...)`. Distinct confirmation cannot override an uncontested strong match or split an existing canonical group. These operations persist HUMAN approval metadata and remain outside the worker allowlist. Human decisions are bound to the compared alternatives; new identity evidence can require another review. This conservative rule can also block an existing source when a newly imported ambiguous alternative appears.

Application creation rechecks identity in the same transaction, catching legacy unlinked sources before checking `(candidate_id, canonical_id)` uniqueness. A legacy source without application history can be resolved automatically; a canonical group with existing application history cannot be silently moved by this automatic path. Existing duplicate histories are not repaired or merged. The explicit trusted `link_job_source` operation also requires a unique uncontradicted strong match; ambiguous links require the owner. General edits cannot change company, source identity, requisition ID, or canonical URL underneath an existing link. Identity corrections and history merges need separately designed operations. Concurrent imports and application creation are serialized by the existing SQLite write transaction.

## Database, migrations, and audit

Schema version 3 retains the fifteen tables introduced by version 2, including:

- `canonical_openings`
- `job_link_evidence`
- `readiness_requirements`
- `unresolved_information`
- `submission_attempts`

Each table has an explicit non-null primary key, revision, validated JSON payload, and projected identity/query columns. JSON identity/revision/projections must agree with relational columns. Lifecycle values are constrained. Foreign keys and ownership triggers protect application/resume, requirements/application, outreach/application, unresolved-question/job, and email-event relationships.

The public repository is read-only by default. It cannot mutate records using `add`/`update`. Domain transactions require an internal capability; database triggers also require the domain-write connection function for business writes. The repository abstraction remains replaceable. This mechanism prevents accidental API bypass, not hostile code with local database ownership.

Queries filter in SQL with indexes for projected lookups and return bounded pages (default 100, maximum 1000, explicit offset). Read transactions use `BEGIN` with SQLite query-only mode; writes use `BEGIN IMMEDIATE`. Count queries avoid loading payloads. Long-running network work must never be put into a future write transaction.

Version 3 transactionally rebuilds only the answer table to remove `(candidate_id, question)` uniqueness, allowing separate answers for distinct occurrences. Historical payloads are preserved without invented scope; legacy approvals and requirements lacking contextual fingerprints cannot pass readiness. Explicit human contextual approval/review is required. A reviewed requirements record fingerprints its complete form context, question list, and required-field list; changing these clears review and a mismatched fingerprint also blocks readiness. Changes to form identity or question scope require new answer approval.

Migrations are an ordered mapping from version to a function. Initialization applies missing versions inside one transaction and validates foreign keys before commit. Unknown newer versions are rejected. Invalid legacy data or migration failure rolls back schema and data. Version 1 is frozen for regression fixtures.

Migration from version 1 preserves record identities and facts but downgrades legacy answers to draft, clears unverifiable contact verification, blocks prior local readiness/outreach approvals, and turns reported submission failures into explicit uncertainty. Existing source jobs receive separate canonical identities; no cross-source equivalence is invented. Legacy audit actor identities are marked unavailable rather than attributed to a human. A migration audit event records safety changes. Resume hashes/selections and requirements must be reviewed through the new operations. For valuable data, maintain a local backup before applying any future migration; this version does not implement automated backup management.

Audit writes and business changes share a transaction. Audit events include correlation IDs and actor attribution. Application-level protection blocks ordinary updates, deletes, duplicate inserts, and **INSERT OR REPLACE**, including when recursive triggers are disabled. Human approvals, resolutions, retry evidence, revisions, state changes, repairs, duplicates, invalid statuses, and rejected validation commands are logged. Failed business transactions roll back; failure/rejection auditing uses a subsequent transaction. Runtime success is logged only after commit.

If the storage system itself cannot accept an audit event, the command fails and logs an audit failure; durable logging cannot be guaranteed to a broken database. Audit protection is not perfect immutability: a privileged owner can modify the raw file/schema. Personal data is stored locally without application-level encryption. Audit metadata should contain evidence references rather than credential-bearing content.

## Trusted execution authorization (local only)

`TrustedExecutionBoundary(database, candidate_id=..., application_id=...)` is a trusted-host implementation object, never an agent tool or worker-visible Python object. It exposes a narrow typed lifecycle for a future host-controlled executor:

1. Reserve with the trusted `reserve_submission(application_id)` operation and wait for its commit. Reservation now includes the full factual execution context, the originating process lifetime, and any consumed retry grant lineage.
2. `capture_context(attempt_id) -> ExecutionContext` obtains a non-authorizing immutable JSON snapshot. A future adapter must supply/verify its actual observed form against this context; copying stored readiness alone proves no external observation.
3. `authorize_submission(context) -> SubmissionAuthorization` re-reads current data, compares the exact context, and commits a unique issuance claim on the attempt before returning a host-registered authorization object.
4. Immediately before any future action, `consume_submission(authorization) -> ExecutionStartReceipt` re-reads and validates everything again, then atomically persists the consumed marker, `execution_started` attempt state, and audit events. The live authorization is removed from the host registry only after commit. The receipt records this transition; it is not an independently redeemable execution capability.

There is no external executor or action in this phase. None of these three types uses truthiness as authorization. The authorization is frozen and contains only its host-generated ID and immutable serialized execution snapshot; it exposes no database or human operations. Registry object identity rejects shallow/deep copies, reconstructed objects, another boundary's permits, and reused permits. A durable issuance claim prevents replacement after a lost response or restart. Reservations from an earlier process lifetime, including inherited fork copies, require human reconciliation. No arbitrary time expiry is used. A rollback leaves no successful issuance/consumption receipt; a committed-but-unacknowledged transition must be recovered through reconciliation, not replay.

Both issuance and consumption check the configured candidate/application, job/canonical/resume identities and revisions, requirements review and manifest, every contextual approved answer and content revision, resume hash, conflicts, unresolved information, exact reservation snapshot, executable reserved state, application revision, prior submission/attempt history, and any human retry grant and its consumed lineage. The application is already `submission_uncertain` after reservation: factual readiness is recomputed while only the current reserved attempt is exempted from the ordinary readiness retry gate. Stored readiness and cached answers are never substituted for these checks. `execution_started` retains an uncertain outcome until the existing human reconciliation path records evidence.

Human retry approval now stores the complete current revision context: candidate/job/canonical opening, selected resume/hash, reviewed requirements and full form/question manifest, and chosen answer/content revisions/scopes. A relevant change invalidates that grant. The owner can explicitly replace a stale, unconsumed grant after repairing and reviewing current information. Requirements review/edit and resume reselection are allowed during confirmed non-submission solely to support that repair. Fresh readiness and a new reservation remain mandatory; the grant is consumed with that reservation and checked again during issuance/consumption. Legacy grants without scope are not inferred or reused.

All persisted unresolved-information records must be explicitly resolved before execution authorization; this is intentionally stricter than a successful readiness check. No schema migration is needed: scope, issuance and consumption metadata extend the existing attempt JSON. Older reservations lacking the new context remain reconciliation-only.

This boundary protects against the restricted-worker capabilities defined above, not arbitrary privileged Python/OS/database manipulation. It does not provide atomicity with an employer/ATS or erase bytes already returned to a caller. Any future executor must stay inside the trusted host, obtain an original permit, consume it immediately before acting, and use exactly its immutable context. Live observation, browser integration, and external result collection remain Phase 2 work.

## MATCH SCORE

The score remains `100 × sum(weight × supplied evidence) / sum(weights)`. Evidence ranges from 0 to 1 and weights are finite/nonnegative with a positive sum. Supported factors are skills, experience, education, eligibility, location, recency, resume alignment, and job quality. Missing positively weighted evidence or unknown/false eligibility blocks scoring. There are no calibrated defaults, evidence extraction, live jobs, or hiring-probability claims. Scoring does not override readiness or human approval.

## Tests and remaining boundaries

Run the complete unittest suite above. Tests use isolated temporary databases and synthetic records; no external service is used. They cover authority separation, approval metadata and invalidation, every global required field, preserved unknown questions/context, repairs, conflicts, uncertain attempts and explicit retry authorization, audit replacement/rejection/rollback/commit behavior, canonical identity, ownership, read locking, pagination, concurrency, and ordered migration rollback.

Blocker 1 is addressed by the restricted interface; blocker 2 is addressed by automatic canonical resolution and the application-creation identity gate; blocker 3 is addressed by field-specific mandatory-value validation; blocker 4 supplies durable local submission reservations and recovery. Blocker 5 binds answers and requirements to explicit form/question context. Blocker 6 provides the trusted single-use issuance/consumption boundary and revision-bound human retry grants. These are local foundation controls; no external execution is enabled.

Remaining boundaries: local human identity is asserted rather than authenticated; evidence is not independently verified; approval and requirements invalidation is conservative; form observations and context are supplied locally, not independently observed; previously returned answer bytes cannot be recalled, so future execution must revalidate current approvals; canonical linking after application creation is blocked; data is not encrypted; resume contents are not parsed; no external outcome is obtained automatically. Any later execution layer must revalidate current decisions and introduce authorized external action handling. Phase 2 has not begun.
