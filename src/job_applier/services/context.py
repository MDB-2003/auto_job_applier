"""Exact form/question scope. No inferred identifiers or fuzzy equivalence."""
import hashlib
import json
from job_applier.models import Application, Job


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def identifier(value):
    return isinstance(value, str) and bool(value.strip()) and '*' not in value


def form_manifest(repo, req):
    if not identifier(req.context.get('form_id')) or not identifier(req.context.get('form_version')):
        raise ValueError('explicit_form_identity_required')
    ids = []
    for question in req.questions:
        if (not identifier(question.get('question_id')) or not isinstance(question.get('text'), str)
            or not question['text'].strip() or not isinstance(question.get('context'), dict)):
            raise ValueError('explicit_question_identity_required')
        ids.append(question['question_id'])
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate_question_occurrence')
    app = repo.get(Application, req.application_id)
    job = repo.get(Job, req.job_id)
    return {'candidate_id': req.candidate_id, 'employer': job.company,
            'application_id': app.id, 'job_id': job.id, 'canonical_id': job.canonical_id,
            'form_context': req.context, 'questions': req.questions,
            'required_candidate_fields': req.required_candidate_fields}


def question_scope(repo, req, question):
    manifest = form_manifest(repo, req)
    if question not in req.questions:
        raise ValueError('question_not_in_form')
    return {key: manifest[key] for key in ('candidate_id','employer','application_id','job_id','canonical_id')} | {
        'form_id': req.context['form_id'], 'form_version': req.context['form_version'],
        'form_context': req.context, 'question_id': question['question_id'],
        'question_text': question['text'], 'question_context': question['context'],
    }


def approved_for(answer, scope, candidate, job):
    from job_applier.models import ApprovalState
    approval = answer.approval or {}
    return (answer.approval_state == ApprovalState.APPROVED
            and answer.candidate_id == candidate.id and answer.question == scope['question_text']
            and answer.candidate_revision == candidate.revision
            and approval.get('approved_content_revision') == answer.content_revision
            and approval.get('job_revision') == job.revision
            and bool(answer.scope) and fingerprint(answer.scope) == fingerprint(scope)
            and approval.get('scope_fingerprint') == fingerprint(scope))
