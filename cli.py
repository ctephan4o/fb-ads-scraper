#!/usr/bin/env python3
"""Find US advertisers running 100+ live Meta ads.

    python cli.py
    python cli.py --threshold 150 --keywords my_keywords.txt --exclude customers.txt
    python cli.py --max-candidates 200      # cap spend on a first run
    python cli.py --no-jev                  # skip Jev ranking, count every candidate

Before any paid counting, candidates are ranked by TypeSafe's Jev model (a Score for
"how many live ads is this likely running" and a Noul for "is this an ad-buying
business at all"). Only the ones that clear --min-heavy-prob and --min-buyer-prob get
counted, most-likely-heavy first, so credits go to advertisers worth paying to verify.
Set TYPESAFE_API_KEY (env or .env) to use it; --no-jev falls back to counting every
candidate in keyword-hit order.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scout.client import ScrapeCreatorsClient
from scout.run import run
from scout.store import load_list

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="Meta Ad Library: heavy US advertisers -> lead list")
    ap.add_argument("--keywords", default=HERE / "keywords.txt", type=Path)
    ap.add_argument("--exclude", default=HERE / "exclude.txt", type=Path,
                    help="page ids or exact page names to skip (existing customers)")
    ap.add_argument("--out", default=HERE / "out", type=Path)
    ap.add_argument("--threshold", type=int, default=100)
    ap.add_argument("--country", default="US")
    ap.add_argument("--max-candidates", type=int, default=None,
                    help="only count the N most-surfaced advertisers (controls credit spend)")
    ap.add_argument("--max-requests", type=int, default=8,
                    help="pagination depth per advertiser; counting stops early once a "
                         "candidate clears --threshold, so this mostly bounds the exact-count case")
    ap.add_argument("--search-depth", type=int, default=1,
                    help="pagination depth per keyword during the sweep")
    ap.add_argument("--min-credits", type=int, default=25, help="stop before the account hits this")
    ap.add_argument("--no-jev", action="store_true",
                    help="skip Jev ranking; count every candidate in keyword-hit order (legacy, spends more)")
    ap.add_argument("--min-heavy-prob", type=float, default=0.35,
                    help="Jev: skip candidates below this P(>= threshold live ads)")
    ap.add_argument("--min-buyer-prob", type=float, default=0.5,
                    help="Jev: skip candidates below this P(is an ad-buying business)")
    ap.add_argument("--jev-model", default="jev-latest", help="TypeSafe System One model for ranking")
    args = ap.parse_args()

    keywords = load_list(args.keywords)
    if not keywords:
        print(f"no keywords in {args.keywords}", file=sys.stderr)
        return 2

    try:
        client = ScrapeCreatorsClient(max_requests_per_call=args.max_requests,
                                      max_search_requests=args.search_depth,
                                      min_credits=args.min_credits)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    run(client, keywords, args.out, threshold=args.threshold, country=args.country,
        exclude=load_list(args.exclude), max_candidates=args.max_candidates,
        use_jev=not args.no_jev, min_heavy_prob=args.min_heavy_prob,
        min_buyer_prob=args.min_buyer_prob, jev_model=args.jev_model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
