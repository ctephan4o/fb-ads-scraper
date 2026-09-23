"""ScrapeCreators Meta Ad Library client.

Two calls are used:
  GET /v1/facebook/adLibrary/search/ads     keyword sweep  -> candidate pages
  GET /v1/facebook/adLibrary/company/ads    per-page ads   -> active ad count + ad ids

Both cost 1 credit per request. Both paginate with a `cursor`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol

import httpx

BASE = "https://api.scrapecreators.com"
AD_URL = "https://www.facebook.com/ads/library/?id={ad_id}"
PAGE_URL = (
    "https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
    "&country={country}&view_all_page_id={page_id}&search_type=page"
)


@dataclass(frozen=True)
class Ad:
    ad_id: str
    page_id: str
    page_name: str
    is_active: bool
    start_date: int | None   # unix seconds
    # Cheap signal for ranking before any paid counting: body copy, link domain, cta,
    # Meta's collation grouping. Empty dict when the source item carried none of it.
    evidence: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return AD_URL.format(ad_id=self.ad_id)


class AdLibrary(Protocol):
    """What the pipeline needs. The real client and the test fake both satisfy it."""

    def search(self, query: str, country: str) -> Iterator[Ad]: ...
    def company_ads(self, page_id: str, country: str) -> Iterator[Ad]: ...
    @property
    def credits_used(self) -> int: ...


def _host(url: str | None) -> str | None:
    """Bare host from a URL, or from a value that's already just a domain."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    if "//" in url:
        url = url.split("//", 1)[1]
    url = url.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return url or None


def _evidence(item: dict) -> dict:
    """Cheap ranking signal from a search/company item. Defensive: `snapshot`,
    `cards`, and `body` all vary by ad -- some ads have cards, some have a bare
    `snapshot.body.text`, some have neither."""
    snapshot = item.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    cards = snapshot.get("cards")
    if not isinstance(cards, list):
        cards = []
    cards = [c for c in cards if isinstance(c, dict)]

    body = None
    for c in cards:
        text = (c.get("body") or "").strip()
        if text:
            body = text
            break
    if not body:
        snap_body = snapshot.get("body")
        if isinstance(snap_body, dict):
            text = (snap_body.get("text") or "").strip()
            if text:
                body = text
    if not body:
        for c in cards:
            title = (c.get("title") or "").strip()
            if title:
                body = title
                break

    link_url = (cards[0].get("link_url") if cards else None) or snapshot.get("link_url") or snapshot.get("caption")
    link_domain = _host(link_url)

    cta = snapshot.get("cta_text") or (cards[0].get("cta_text") if cards else None)

    collation_count = item.get("collation_count")
    if not isinstance(collation_count, int):
        collation_count = None

    collation_id = item.get("collation_id")

    ev: dict = {}
    if body:
        ev["body"] = body[:300]
    if link_domain:
        ev["link_domain"] = link_domain
    if cta:
        ev["cta"] = cta
    if collation_count is not None:
        ev["collation_count"] = collation_count
    if collation_id is not None:
        ev["collation_id"] = str(collation_id)
    return ev


def _parse(item: dict) -> Ad | None:
    ad_id = item.get("ad_archive_id")
    page_id = item.get("page_id")
    if not ad_id or not page_id:
        return None
    return Ad(
        ad_id=str(ad_id),
        page_id=str(page_id),
        page_name=str(item.get("page_name") or "").strip(),
        is_active=bool(item.get("is_active", True)),
        start_date=item.get("start_date") if isinstance(item.get("start_date"), int) else None,
        evidence=_evidence(item),
    )


class ScrapeCreatorsClient:
    def __init__(
        self,
        api_key: str | None = None,
        max_requests_per_call: int = 12,
        max_search_requests: int = 4,
        min_credits: int = 25,
        pause: float = 0.4,
        timeout: float = 40.0,
    ):
        self.api_key = api_key or os.environ.get("SCRAPECREATORS_API_KEY", "")
        if not self.api_key:
            from .jev import _read_dotenv
            from pathlib import Path
            self.api_key = _read_dotenv(Path(__file__).resolve().parent.parent / ".env").get("SCRAPECREATORS_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("SCRAPECREATORS_API_KEY is not set")
        self.max_requests_per_call = max_requests_per_call   # pagination depth per advertiser
        self.max_search_requests = max_search_requests       # pagination depth per keyword
        self.min_credits = min_credits                       # stop before the account runs dry
        self.pause = pause
        self._credits_used = 0
        self.credits_remaining: int | None = None
        self._http = httpx.Client(
            base_url=BASE,
            timeout=timeout,
            headers={"x-api-key": self.api_key, "accept": "application/json"},
        )

    @property
    def credits_used(self) -> int:
        return self._credits_used

    def _get(self, path: str, params: dict) -> dict:
        for attempt in range(4):
            resp = self._http.get(path, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            self._credits_used += int(data.get("credits_charged", 1) or 1)
            if isinstance(data.get("credits_remaining"), int):
                self.credits_remaining = data["credits_remaining"]
            if not data.get("success", True):
                raise RuntimeError(f"API error on {path}: {data.get('message') or data}")
            return data
        raise RuntimeError(f"gave up on {path} after retries")

    def _paginate(self, path: str, params: dict, max_requests: int) -> Iterator[Ad]:
        self.last_truncated = False
        cursor = None
        for _ in range(max_requests):
            if self.credits_remaining is not None and self.credits_remaining < self.min_credits:
                raise RuntimeError(
                    f"stopping: only {self.credits_remaining} credits left (floor {self.min_credits})"
                )
            q = dict(params)
            if cursor:
                q["cursor"] = cursor
            data = self._get(path, q)
            # search/ads returns "searchResults"; company/ads returns "results".
            for item in data.get("searchResults") or data.get("results") or []:
                ad = _parse(item)
                if ad:
                    yield ad
            cursor = data.get("cursor")
            if not cursor:
                return
            time.sleep(self.pause)
        # Ran out of request budget with pages still to fetch.
        self.last_truncated = True

    def search(self, query: str, country: str = "US") -> Iterator[Ad]:
        return self._paginate(
            "/v1/facebook/adLibrary/search/ads",
            {"query": query, "country": country, "status": "ACTIVE", "trim": "true",
             "sort_by": "total_impressions"},
            self.max_search_requests,
        )

    def company_ads(self, page_id: str, country: str = "US") -> Iterator[Ad]:
        return self._paginate(
            "/v1/facebook/adLibrary/company/ads",
            {"pageId": page_id, "country": country, "status": "ACTIVE", "trim": "true"},
            self.max_requests_per_call,
        )
