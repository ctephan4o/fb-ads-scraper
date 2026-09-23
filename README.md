# Ad Volume Scout

US advertisers running 100+ live Meta ads. A lead list, refreshed every run.

## Run

```bash
pip install -r requirements.txt
export SCRAPECREATORS_API_KEY=your-key      # scrapecreators.com
export TYPESAFE_API_KEY=your-key            # typesafe.ai -- ranks candidates before any paid count
python cli.py --max-candidates 150           # first run: cap spend
python cli.py                                # later runs: full sweep
python cli.py --no-jev                       # skip ranking, count every candidate (spends more)
```

Outputs land in `out/`:

| File | What |
|---|---|
| `leads.csv` | One row per advertiser at the threshold. Name, live ad count, Ad Library page link, oldest live ad, first qualified, which keywords found it. |
| `ads.csv` | One row per ad. Name, page id, ad id, direct link. |
| `leads.json` | Same as both, nested. |
| `digest.md` | This run's changes: new, scaled up, dropped. Paste into Slack. |
| `ranking.csv` | Every ranked candidate: Jev's P(heavy) / P(is a buyer) and the count/skip decision. Only written when Jev ranking is on (the default). |
| `state.json` | Memory between runs. Don't delete it. |

## How it finds them

1. Search the Ad Library for each line in `keywords.txt`, US only, active ads, sorted by impressions. Every advertiser that appears is a candidate, carrying whatever evidence came back with it: sample ad copy, link domain, CTA, how many distinct ads and keywords surfaced it, Meta's collation counts.
2. Rank every non-excluded candidate with TypeSafe's Jev model: a Score for "how many live ads is this business most likely running" and a Noul for "is this actually a company buying ads to sell something." No paid request is spent on this step. Hard evidence overrides the model: any candidate that surfaced 3+ distinct ads in the sweep, or whose ads carry 5+ collated variants, is counted regardless of Jev's score.
3. Drop the ones below `--min-heavy-prob` (unlikely to be heavy) or `--min-buyer-prob` (platforms, publishers, agencies, political/nonprofit accounts). Count the rest, most-likely-heavy first, and stop paging a given advertiser as soon as it clears `--threshold` — the count becomes a floor, not exact, but it's already proven the point.
4. Keep the ones at or above `--threshold` (default 100).

Advertisers listed in `exclude.txt` — by page id or exact page name — are never counted (and never ranked, so they cost nothing). `--no-jev` skips ranking entirely and counts every surfaced candidate in keyword-hit order instead, the old (expensive) behavior.

## Credits

One ScrapeCreators credit per request; Jev ranking spends no ScrapeCreators credits at all (it runs on the evidence already in hand). A keyword costs up to `--search-depth` (default 1). Once ranked, each counted advertiser costs up to `--max-requests` (default 8) requests, because counting stops as soon as it clears `--threshold` — worst case is `ceil(threshold / 15)` requests per advertiser (the company endpoint returns ~15 ads per page), capped by `--max-requests`. Counted advertisers are remembered for 6 days, so re-runs only pay for new candidates. The client stops itself at `--min-credits` remaining.

So a run costs roughly: `(keywords × search-depth)` sweep credits + `(candidates Jev clears to count) × min(ceil(threshold/15), max-requests)` counting credits. With the stock keyword list and defaults, that's about 20 keyword credits plus K×7, where K is however many candidates Jev decided were worth verifying — usually a small fraction of everything the sweep surfaced.

## Counts

`count_is_exact = yes` means every page was read. `no (at least)` means the count hit the 400 cap or the request budget — the real number is higher. Both qualify. A count like `97+` that fell short of the threshold means the request budget ran out first; raise `--max-requests` or re-run, since unfinished floors are never cached.

## Tests

```bash
python tests/test_offline.py
```

Runs the whole pipeline against a fake Ad Library. No key needed.

## Example: one skincare advertiser at 100+

```bash
printf 'skincare
serum
moisturizer
anti-aging
acne
sunscreen
retinol
cleanser
' > skincare-keywords.txt
python cli.py --keywords skincare-keywords.txt --min-heavy-prob 0.2 --max-candidates 12
```

Real run, Sep 2026: 110 advertisers surfaced for 8 credits, Jev cleared 27, 12 counted for 87 credits, 3 confirmed at 100+ (Tatcha, Dove, C4 Energy).

## Windows

Console output is ASCII-only; if you add log lines, keep them ASCII or set `PYTHONIOENCODING=utf-8`.
