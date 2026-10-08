"""Weighted MATCH SCORE over supplied evidence; never a hiring probability."""
from dataclasses import dataclass
from math import isfinite
from typing import Mapping

FACTORS = frozenset({"skills", "experience", "education", "eligibility", "location", "recency", "resume_alignment", "job_quality"})


@dataclass(frozen=True)
class MatchScore:
    score: float | None
    blocked: bool
    reasons: tuple[str, ...]
    contributions: Mapping[str, float]


def calculate_match_score(weights: Mapping[str, float], evidence: Mapping[str, float | None], *, eligible: bool | None) -> MatchScore:
    if not weights or set(weights) - FACTORS or set(evidence) - FACTORS:
        raise ValueError("Provide weights and evidence using supported factors")
    if any(not isfinite(v) or v < 0 for v in weights.values()):
        raise ValueError("Weights must be finite and nonnegative")
    total = sum(weights.values())
    if not isfinite(total) or total <= 0:
        raise ValueError("Total weight must be finite and positive")
    if any(v is not None and (not isfinite(v) or not 0 <= v <= 1) for v in evidence.values()):
        raise ValueError("Evidence values must be finite and between zero and one")
    reasons = []
    if eligible is not True:
        reasons.append("Eligibility unknown" if eligible is None else "Not eligible")
    reasons.extend(f"Missing evidence: {f}" for f, w in weights.items() if w > 0 and evidence.get(f) is None)
    if reasons:
        return MatchScore(None, True, tuple(reasons), {})
    contributions = {f: 100 * (w / total) * evidence[f] for f, w in weights.items() if w > 0}
    return MatchScore(sum(contributions.values()), False, (), contributions)
