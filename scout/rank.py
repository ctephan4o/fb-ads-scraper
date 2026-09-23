"""Rank candidates with TypeSafe's Jev model before spending paid credits on exact counts.

`judge` is any callable (state: dict, questions: dict) -> answers: dict -- the real one is
JevJudge in scout/jev.py; tests inject a fake. Batches 40 candidates per request, same as
hookbench-test/tag.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .pipeline import Candidate

Log = Callable[[str], None]
Judge = Callable[[dict, dict], dict]

BATCH_SIZE = 40

HEAVY_INSTRUCTIONS = (
    "How many live Meta (Facebook/Instagram) ads is this advertiser most likely running in "
    "the United States right now? Judge from the evidence in `candidates[{i}]`: how many "
    "distinct ads it surfaced in impression-sorted searches, how many different search "
    "keywords it appeared under, the total of Meta's collation counts (each is a group of "
    "near-duplicate ads; null means unknown), sample ad copy, link domain, and what kind of "
    "business it is. Big DTC brands, apps, and national services run hundreds of variants; "
    "small local businesses run a handful."
)
HEAVY_CRITERIA = [
    "fewer than 20 live ads",
    "20 to 99 live ads",
    "100 to 299 live ads",
    "300 or more live ads",
]
BUYER_INSTRUCTIONS = (
    "Is the advertiser in `candidates[{i}]` a company that pays for Meta ads to sell its own products or "
    "services (e-commerce/DTC brand, consumer app or subscription, national or local "
    "service business, financial or health provider) — as opposed to a social/media "
    "platform, publisher, political or government or nonprofit entity, or a marketing "
    "agency advertising itself?"
)


@dataclass
class Ranked:
    page_id: str
    page_name: str
    p_heavy: float          # P(>= threshold live ads), from the Score's probabilities
    p_buyer: float          # P(this is an ad-buying business), from the Noul
    probabilities: dict     # raw Score probabilities, keyed "0".."3"


def decide(r: Ranked, min_heavy_prob: float, min_buyer_prob: float) -> str:
    """count / skip_unlikely / skip_not_buyer, in that precedence."""
    if r.p_heavy < min_heavy_prob:
        return "skip_unlikely"
    if r.p_buyer < min_buyer_prob:
        return "skip_not_buyer"
    return "count"


def _candidate_state(i: int, c: Candidate) -> dict:
    return {
        "i": i,
        "page_name": c.page_name,
        "distinct_ads_surfaced": len(c.ad_ids),
        "keywords_matched": sorted(c.keywords),
        "collation_total": c.collation_total,
        "sample_ad_copy": list(c.bodies),
        "link_domains": sorted(c.domains),
        "cta": sorted(c.ctas),
    }


def _has_evidence(c: Candidate) -> bool:
    return bool(c.page_name.strip()) or bool(c.bodies)


def _no_evidence(c: Candidate) -> Ranked:
    return Ranked(c.page_id, c.page_name, p_heavy=0.5, p_buyer=0.5, probabilities={})


def _fallback(order: list[Candidate]) -> list[Ranked]:
    return [Ranked(c.page_id, c.page_name, p_heavy=0.5, p_buyer=0.5, probabilities={}) for c in order]


def rank_candidates(
    candidates: dict[str, Candidate],
    judge: Judge,
    threshold: int = 100,
    log: Log | None = None,
) -> list[Ranked]:
    order = list(candidates.values())
    if not order:
        return []

    results: dict[str, Ranked] = {}
    to_ask: list[Candidate] = []
    for c in order:
        if _has_evidence(c):
            to_ask.append(c)
        else:
            results[c.page_id] = _no_evidence(c)

    for start in range(0, len(to_ask), BATCH_SIZE):
        batch = to_ask[start:start + BATCH_SIZE]
        state = {"candidates": [_candidate_state(i, c) for i, c in enumerate(batch)]}
        questions: dict = {}
        for i in range(len(batch)):
            questions[f"heavy_{i}"] = {
                "type": "score",
                "instructions": HEAVY_INSTRUCTIONS.format(i=i),
                "criteria": HEAVY_CRITERIA,
            }
            questions[f"buyer_{i}"] = {
                "type": "noul",
                "instructions": BUYER_INSTRUCTIONS.format(i=i),
            }
        try:
            answers = judge(state, questions) or {}
        except Exception as exc:  # network/auth/parse error -- never crash the run
            if log:
                log(f"  Jev ranking failed ({exc}); falling back to hits-based order")
            return _fallback(order)

        for i, c in enumerate(batch):
            heavy = answers.get(f"heavy_{i}") or {}
            buyer = answers.get(f"buyer_{i}") or {}
            probs = heavy.get("probabilities") or {}
            p_heavy = float(probs.get("2", 0.0)) + float(probs.get("3", 0.0))
            p_buyer = float(buyer.get("noul", 0.5))
            results[c.page_id] = Ranked(c.page_id, c.page_name, p_heavy=p_heavy, p_buyer=p_buyer, probabilities=probs)

    return [results[c.page_id] for c in order]
