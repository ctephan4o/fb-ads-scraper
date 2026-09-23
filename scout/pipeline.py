"""Keyword sweep -> candidate pages -> count each page's live US ads -> keep the heavy ones."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .client import Ad, AdLibrary, PAGE_URL

Log = Callable[[str], None]


@dataclass
class Candidate:
    page_id: str
    page_name: str
    hits: int = 0                  # how many keyword sweeps surfaced it
    keywords: set[str] = field(default_factory=set)
    ad_ids: set[str] = field(default_factory=set)          # distinct ads surfaced, any keyword
    bodies: list[str] = field(default_factory=list)        # up to 3 distinct non-empty ad bodies
    domains: set[str] = field(default_factory=set)
    ctas: set[str] = field(default_factory=set)
    collation_total: int = 0       # sum of collation_count over distinct collation ids seen
    _seen_collation_ids: set[str] = field(default_factory=set, repr=False, compare=False)


@dataclass
class Qualified:
    page_id: str
    page_name: str
    active_ads: int                # exact if `exact`, else a floor (we stopped counting)
    exact: bool
    ad_ids: list[str]
    keywords: list[str]
    earliest_start: int | None     # oldest live ad's start date, unix seconds
    checked_at: float

    @property
    def page_url(self) -> str:
        return PAGE_URL.format(country="US", page_id=self.page_id)

    def as_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "page_name": self.page_name,
            "active_ads": self.active_ads,
            "exact": self.exact,
            "page_url": self.page_url,
            "ad_ids": self.ad_ids,
            "keywords": self.keywords,
            "earliest_start": self.earliest_start,
            "checked_at": self.checked_at,
        }


def discover(
    client: AdLibrary,
    keywords: list[str],
    country: str = "US",
    log: Log | None = None,
) -> dict[str, Candidate]:
    """Sweep keywords; every advertiser that shows up with a live ad is a candidate."""
    found: dict[str, Candidate] = {}
    for kw in keywords:
        seen_this_kw: set[str] = set()
        n = 0
        try:
            for ad in client.search(kw, country):
                n += 1
                if not ad.is_active:
                    continue
                c = found.setdefault(ad.page_id, Candidate(ad.page_id, ad.page_name))
                if not c.page_name and ad.page_name:
                    c.page_name = ad.page_name
                if ad.page_id not in seen_this_kw:
                    seen_this_kw.add(ad.page_id)
                    c.hits += 1
                    c.keywords.add(kw)
                c.ad_ids.add(ad.ad_id)
                ev = ad.evidence or {}
                body = ev.get("body")
                if body and body not in c.bodies and len(c.bodies) < 3:
                    c.bodies.append(body)
                domain = ev.get("link_domain")
                if domain:
                    c.domains.add(domain)
                cta = ev.get("cta")
                if cta:
                    c.ctas.add(cta)
                cid = ev.get("collation_id")
                cc = ev.get("collation_count")
                if cid is not None and cc is not None and cid not in c._seen_collation_ids:
                    c._seen_collation_ids.add(cid)
                    c.collation_total += cc
        except RuntimeError as exc:
            if log:
                log(f"  sweep '{kw}' stopped: {exc}")
            break
        if log:
            log(f"  '{kw}': {n} ads, {len(seen_this_kw)} advertisers (total {len(found)})")
    return found


def count_active(
    client: AdLibrary,
    page_id: str,
    country: str = "US",
    hard_cap: int = 400,
    stop_at: int | None = None,
) -> tuple[int, bool, list[str], int | None]:
    """Count a page's live ads. Cost is bounded by `hard_cap` and the client's request budget.

    `stop_at`: once this many are seen, stop paging and return early. The result is then a
    floor, not an exact count -- good enough to know it clears a threshold without paying to
    read every remaining page.

    Returns (count, exact, ad_ids, earliest_start).
    """
    ids: list[str] = []
    seen: set[str] = set()
    earliest: int | None = None
    hit_cap = False
    hit_floor = False
    for ad in client.company_ads(page_id, country):
        if not ad.is_active or ad.ad_id in seen:
            continue
        seen.add(ad.ad_id)
        ids.append(ad.ad_id)
        if ad.start_date and (earliest is None or ad.start_date < earliest):
            earliest = ad.start_date
        if len(ids) >= hard_cap:
            hit_cap = True
            break
        if stop_at is not None and len(ids) >= stop_at:
            hit_floor = True
            break
    # Exact only if we saw every page: neither our cap, the floor, nor the client's request
    # budget cut it short.
    truncated = hit_cap or hit_floor or bool(getattr(client, "last_truncated", False))
    return len(ids), not truncated, ids, earliest


def qualify(
    client: AdLibrary,
    candidates: dict[str, Candidate],
    threshold: int = 100,
    country: str = "US",
    skip_ids: set[str] | None = None,
    known: dict[str, dict] | None = None,
    recheck_after_s: float = 6 * 24 * 3600,
    log: Log | None = None,
    stop_at_threshold: bool = True,
    order: list[Candidate] | None = None,
) -> tuple[list[Qualified], dict[str, dict]]:
    """Check each candidate.

    `stop_at_threshold`: stop counting a page's ads as soon as it clears `threshold` (a floor,
    not an exact count) -- the point of ranking first is to spend counting credits, not to
    read every last page of an advertiser we already know qualifies.

    `order`: an explicit counting order (e.g. Jev-ranked, most-likely-heavy first). When not
    given, falls back to sorting by keyword hits.

    Returns (qualified, checked) where `checked` maps every page_id counted THIS run to a
    record {page_name, active_ads, exact, ad_ids, earliest_start, checked_at} — including
    the ones that fell short, so they are not re-counted for `recheck_after_s`.
    """
    skip_ids = skip_ids or set()
    known = known or {}
    out: list[Qualified] = []
    checked: dict[str, dict] = {}
    now = time.time()

    # Check the most-surfaced advertisers first by default: more keyword hits, more likely heavy.
    if order is None:
        order = sorted(candidates.values(), key=lambda c: (-c.hits, c.page_name.lower()))

    for i, c in enumerate(order, 1):
        if c.page_id in skip_ids:
            continue
        prior = known.get(c.page_id)
        prior_usable = bool(prior) and (prior.get("exact", False) or prior.get("active_ads", 0) >= threshold)
        if prior_usable and now - float(prior.get("checked_at", 0)) < recheck_after_s:
            # Recently checked: reuse.
            if prior.get("active_ads", 0) >= threshold:
                out.append(Qualified(
                    page_id=c.page_id, page_name=prior.get("page_name") or c.page_name,
                    active_ads=prior["active_ads"], exact=prior.get("exact", False),
                    ad_ids=prior.get("ad_ids", []), keywords=sorted(c.keywords),
                    earliest_start=prior.get("earliest_start"), checked_at=prior["checked_at"],
                ))
            continue

        try:
            n, exact, ids, earliest = count_active(
                client, c.page_id, country,
                stop_at=threshold if stop_at_threshold else None,
            )
        except RuntimeError as exc:
            if log:
                log(f"  stopped at {i}/{len(order)}: {exc}")
            break
        checked[c.page_id] = {
            "page_name": c.page_name, "active_ads": n, "exact": exact,
            "ad_ids": ids if n >= threshold else [],   # don't bloat state with small advertisers
            "earliest_start": earliest, "checked_at": now, "keywords": sorted(c.keywords),
        }
        if log:
            mark = "  <-- QUALIFIES" if n >= threshold else ""
            log(f"  [{i}/{len(order)}] {c.page_name or c.page_id}: {n}{'' if exact else '+'} live ads{mark}")
        if n >= threshold:
            out.append(Qualified(
                page_id=c.page_id, page_name=c.page_name, active_ads=n, exact=exact,
                ad_ids=ids, keywords=sorted(c.keywords), earliest_start=earliest, checked_at=now,
            ))

    out.sort(key=lambda q: -q.active_ads)
    return out, checked
