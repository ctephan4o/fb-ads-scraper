"""One run: sweep -> rank (Jev) -> qualify -> merge state -> write outputs."""

from __future__ import annotations

import csv
import math
import time
from pathlib import Path
from typing import Callable

from .client import AdLibrary
from .jev import JevJudge
from .pipeline import Candidate, discover, qualify
from .rank import Ranked, decide, rank_candidates
from .store import load_list, load_state, merge, save_state, write_outputs

Judge = Callable[[dict, dict], dict]


def _write_ranking_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page_id", "page_name", "distinct_ads_surfaced", "keywords",
                    "collation_total", "p_heavy", "p_buyer", "decision"])
        for r in rows:
            w.writerow([
                r["page_id"], r["page_name"], r["distinct_ads_surfaced"],
                ", ".join(r["keywords"]), r["collation_total"],
                f"{r['p_heavy']:.3f}", f"{r['p_buyer']:.3f}", r["decision"],
            ])


def run(
    client: AdLibrary,
    keywords: list[str],
    out_dir: Path,
    threshold: int = 100,
    country: str = "US",
    exclude: list[str] | None = None,
    max_candidates: int | None = None,
    log=print,
    use_jev: bool = True,
    judge: Judge | None = None,
    min_heavy_prob: float = 0.35,
    min_buyer_prob: float = 0.5,
    jev_model: str = "jev-latest",
) -> dict:
    started = time.time()
    state_path = out_dir / "state.json"
    state = load_state(state_path)

    log(f"Sweeping {len(keywords)} keywords ({country}, active ads only)")
    candidates = discover(client, keywords, country, log)
    log(f"{len(candidates)} distinct advertisers surfaced, {client.credits_used} credits used")

    # Exclusions: page ids or exact page names (case-insensitive) of accounts to skip.
    ex = {e.strip().lower() for e in (exclude or [])}
    skip = {pid for pid, c in candidates.items() if pid in ex or c.page_name.lower() in ex}
    if skip:
        log(f"Skipping {len(skip)} excluded advertisers")

    if use_jev and judge is None:
        try:
            judge = JevJudge(model=jev_model)
        except RuntimeError as exc:
            log(f"Jev ranking disabled: {exc}")
            use_jev = False

    order: list[Candidate] | None = None
    ranking_path: Path | None = None

    if use_jev:
        rankable = {pid: c for pid, c in candidates.items() if pid not in skip}
        log(f"Ranking {len(rankable)} candidates with Jev before spending counting credits")
        ranked: list[Ranked] = rank_candidates(rankable, judge, threshold=threshold, log=log)

        rows = []
        counted: list[tuple[Ranked, Candidate]] = []
        for r in ranked:
            c = rankable[r.page_id]
            d = decide(r, min_heavy_prob, min_buyer_prob)
            # Hard evidence beats the model: several distinct ads already surfaced in
            # impression-sorted search, or Meta itself reports 5+ collated variants.
            if d == "skip_unlikely" and (len(c.ad_ids) >= 3 or c.collation_total >= 5):
                d = "count"
            if d == "count":
                counted.append((r, c))
            rows.append({
                "page_id": r.page_id, "page_name": r.page_name,
                "distinct_ads_surfaced": len(c.ad_ids), "keywords": sorted(c.keywords),
                "collation_total": c.collation_total, "p_heavy": r.p_heavy, "p_buyer": r.p_buyer,
                "decision": d,
            })
        counted.sort(key=lambda rc: (-rc[0].p_heavy, -len(rc[1].ad_ids), -rc[1].hits))
        n_eligible = len(counted)
        if max_candidates:
            counted = counted[:max_candidates]
        order = [c for _, c in counted]

        ranking_path = out_dir / "ranking.csv"
        _write_ranking_csv(ranking_path, rows)
        log(f"Ranked {len(ranked)} candidates -> {ranking_path}")

        requests_per_advertiser = min(math.ceil(threshold / 15), getattr(client, "max_requests_per_call", 12))
        log(f"Counting {len(order)} of {n_eligible} candidates; worst case "
            f"{len(order)}x{requests_per_advertiser} credits (R = requests to reach "
            f"threshold ~ ceil(threshold/15), capped by --max-requests)")
    elif max_candidates:
        keep = sorted(candidates.values(), key=lambda c: (-c.hits, c.page_name.lower()))[:max_candidates]
        candidates = {c.page_id: c for c in keep}
        log(f"Capped to the {len(candidates)} most-surfaced candidates")

    log(f"Counting live ads per advertiser (threshold {threshold})")
    qualified, checked = qualify(
        client, candidates, threshold=threshold, country=country,
        skip_ids=skip, known=state["pages"], log=log, order=order,
    )

    changes = merge(state, qualified, checked, threshold)
    save_state(state_path, state)
    paths = write_outputs(out_dir, state, threshold, changes, country)
    if ranking_path:
        paths["ranking"] = ranking_path

    total = sum(1 for r in state["pages"].values() if r.get("active_ads", 0) >= threshold)
    log("")
    log(f"Done in {time.time() - started:.0f}s | {client.credits_used} credits | "
        f"{len(checked)} counted | {total} advertisers at {threshold}+ "
        f"({len(changes['new'])} new)")
    for name, p in paths.items():
        log(f"  {name:<10} {p}")
    return {"qualified_total": total, "changes": changes, "paths": paths,
            "credits_used": client.credits_used}
