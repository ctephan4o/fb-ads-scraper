"""State between runs, and the output files."""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import AD_URL
from .pipeline import Qualified

STATE_VERSION = 1


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "pages": {}, "runs": []}
    data = json.loads(path.read_text())
    data.setdefault("pages", {})
    data.setdefault("runs", [])
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def load_list(path: Path | None) -> list[str]:
    """One entry per line; blank lines and # comments ignored."""
    if not path or not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def merge(state: dict, qualified: list[Qualified], checked: dict[str, dict], threshold: int) -> dict:
    """Fold this run into the state. Returns a summary of what changed."""
    now = time.time()
    pages = state["pages"]
    new_ids, grew, dropped = [], [], []

    for page_id, rec in checked.items():
        prior = pages.get(page_id)
        was_in = bool(prior and prior.get("active_ads", 0) >= threshold)
        now_in = rec["active_ads"] >= threshold
        entry = dict(prior or {})
        entry.update(rec)
        entry.setdefault("first_seen", now)
        if now_in and not was_in:
            entry["first_qualified"] = now
            new_ids.append(page_id)
        elif now_in and was_in and rec["active_ads"] > prior.get("active_ads", 0) * 1.25:
            grew.append(page_id)
        elif was_in and not now_in:
            dropped.append(page_id)
        pages[page_id] = entry

    state["runs"].append({
        "at": now,
        "checked": len(checked),
        "qualified": len(qualified),
        "new": len(new_ids),
        "dropped": len(dropped),
    })
    state["runs"] = state["runs"][-60:]
    return {"new": new_ids, "grew": grew, "dropped": dropped}


def _date(ts) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def write_outputs(out_dir: Path, state: dict, threshold: int, changes: dict, country: str = "US") -> dict:
    """leads.csv, leads.json, ads.csv, and a digest.md for the run. Returns the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = state["pages"]
    leads = [
        (pid, rec) for pid, rec in pages.items() if rec.get("active_ads", 0) >= threshold
    ]
    leads.sort(key=lambda kv: -kv[1]["active_ads"])
    page_url = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
                f"&country={country}&view_all_page_id={{pid}}&search_type=page")

    leads_csv = out_dir / "leads.csv"
    with leads_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page_name", "active_ads", "count_is_exact", "ad_library_page",
                    "oldest_live_ad", "first_qualified", "last_checked", "page_id", "found_via"])
        for pid, rec in leads:
            w.writerow([
                rec.get("page_name", ""), rec["active_ads"], "yes" if rec.get("exact") else "no (at least)",
                page_url.format(pid=pid), _date(rec.get("earliest_start")),
                _date(rec.get("first_qualified")), _date(rec.get("checked_at")), pid,
                ", ".join(rec.get("keywords", [])),
            ])

    ads_csv = out_dir / "ads.csv"
    with ads_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["page_name", "page_id", "ad_id", "ad_url"])
        for pid, rec in leads:
            for ad_id in rec.get("ad_ids", []):
                w.writerow([rec.get("page_name", ""), pid, ad_id, AD_URL.format(ad_id=ad_id)])

    leads_json = out_dir / "leads.json"
    leads_json.write_text(json.dumps(
        [{"page_id": pid, **rec, "page_url": page_url.format(pid=pid),
          "ad_urls": [AD_URL.format(ad_id=a) for a in rec.get("ad_ids", [])]}
         for pid, rec in leads], indent=1))

    digest = out_dir / "digest.md"
    lines = [f"# Heavy Meta advertisers — {country}, {threshold}+ live ads",
             f"_{_date(time.time())} · {len(leads)} total · "
             f"{len(changes['new'])} new · {len(changes['grew'])} scaled up · {len(changes['dropped'])} dropped_", ""]
    if changes["new"]:
        lines.append("## New this run")
        for pid in changes["new"]:
            r = pages[pid]
            lines.append(f"- **{r.get('page_name') or pid}** — {r['active_ads']}{'' if r.get('exact') else '+'} live ads · "
                         f"[ad library]({page_url.format(pid=pid)})")
        lines.append("")
    if changes["grew"]:
        lines.append("## Scaled up since last check")
        for pid in changes["grew"]:
            r = pages[pid]
            lines.append(f"- **{r.get('page_name') or pid}** — now {r['active_ads']} live ads")
        lines.append("")
    if changes["dropped"]:
        lines.append("## Fell below threshold")
        for pid in changes["dropped"]:
            r = pages[pid]
            lines.append(f"- {r.get('page_name') or pid} — {r['active_ads']} live ads")
        lines.append("")
    lines.append("## Full list")
    for pid, rec in leads[:200]:
        lines.append(f"- {rec.get('page_name') or pid} — {rec['active_ads']}{'' if rec.get('exact') else '+'} · "
                     f"[ads]({page_url.format(pid=pid)})")
    digest.write_text("\n".join(lines) + "\n")

    return {"leads_csv": leads_csv, "ads_csv": ads_csv, "leads_json": leads_json, "digest": digest}
