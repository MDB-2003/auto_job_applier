"""Exact local job identity resolution; no similarity, discovery, or provider APIs."""
from dataclasses import dataclass
import hashlib
import json

from job_applier.models import CanonicalOpening, Job

IDENTIFIERS = ('requisition_id', 'canonical_url')


def _rows(repo, model, **filters):
    offset = 0
    while True:
        page = repo.list(model, limit=100, offset=offset, **filters)
        yield from page
        if len(page) < 100:
            return
        offset += len(page)


def identity_values(job):
    """Exact values only. Whitespace-only identifiers are missing, not matches."""
    return {
        'company': job.company,
        **{name: value if isinstance(value := getattr(job, name), str) and value.strip() else None
           for name in IDENTIFIERS},
    }


@dataclass(frozen=True)
class IdentityResolution:
    kind: str
    target_id: str | None
    candidates: tuple[dict, ...]
    fingerprint: str

    def evidence(self, source):
        return {
            'policy': 'exact_company_identity_v1',
            'source_identity': identity_values(source),
            'resolution': self.kind,
            'resolution_fingerprint': self.fingerprint,
            'candidates': list(self.candidates),
        }


def resolve_identity(repo, source, *, exclude_canonical_id=None):
    """Compare same-company live canonical groups, including established aliases.

    Equal identifiers provide positive evidence. If one identifier matches but
    another supplied identifier contradicts the group, require human review.
    With no positive match, groups lacking comparable identifiers are ambiguous.
    An orphan canonical (no source records) is not an independently actionable job.
    """
    source_values = identity_values(source)
    groups = {}
    for other in _rows(repo, Job, company=source.company):
        if other.canonical_id == exclude_canonical_id:
            continue
        group = groups.setdefault(other.canonical_id, {'sources': [], 'values': {key: set() for key in IDENTIFIERS}})
        group['sources'].append(other.id)
        values = identity_values(other)
        for key in IDENTIFIERS:
            if values[key] is not None:
                group['values'][key].add(values[key])
    candidates = []
    for canonical_id, group in sorted(groups.items()):
        canonical = repo.get(CanonicalOpening, canonical_id)
        if canonical.company == source.company:
            values = identity_values(canonical)
            for key in IDENTIFIERS:
                if values[key] is not None:
                    group['values'][key].add(values[key])
        matching = [key for key in IDENTIFIERS if source_values[key] is not None and source_values[key] in group['values'][key]]
        differing = [key for key in IDENTIFIERS if source_values[key] is not None and group['values'][key] and source_values[key] not in group['values'][key]]
        candidates.append({
            'canonical_id': canonical_id,
            'source_record_ids': sorted(group['sources']),
            'identifiers': {key: sorted(group['values'][key]) for key in IDENTIFIERS},
            'matching_identifiers': matching,
            'differing_identifiers': differing,
            'comparable': bool(matching or differing),
        })
    matches = [candidate for candidate in candidates if candidate['matching_identifiers']]
    if len(matches) > 1 or any(candidate['differing_identifiers'] for candidate in matches):
        kind, target = 'conflicting', None
    elif len(matches) == 1:
        kind, target = 'strong', matches[0]['canonical_id']
    elif any(not candidate['comparable'] for candidate in candidates):
        kind, target = 'ambiguous', None
    else:
        kind, target = 'distinct', None
    fingerprint = hashlib.sha256(json.dumps(
        {'source': source_values, 'candidates': candidates}, sort_keys=True, separators=(',', ':')
    ).encode()).hexdigest()
    return IdentityResolution(kind, target, tuple(candidates), fingerprint)
