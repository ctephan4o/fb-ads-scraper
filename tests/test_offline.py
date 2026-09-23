"""End-to-end against a fake Ad Library. No network, no key."""

import csv
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scout.client import Ad, ScrapeCreatorsClient, _parse
from scout.pipeline import Candidate, count_active, discover, qualify
from scout.rank import Ranked, decide, rank_candidates
from scout.run import run
from scout.store import load_list, load_state, merge, write_outputs

failures = []


def check(name, ok, extra=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  {extra}"))
    if not ok:
        failures.append(name)


class FakeLibrary:
    """A handful of advertisers with known live-ad counts."""

    def __init__(self, counts: dict[str, int], per_page: int = 30, request_cap: int = 12):
        self.counts = counts               # page_id -> number of live ads
        self.names = {pid: f"Brand {pid}" for pid in counts}
        self.per_page = per_page
        self.request_cap = request_cap
        self.credits_used = 0
        self.last_truncated = False

    def search(self, query, country):
        self.credits_used += 1
        # every keyword surfaces every advertiser once, plus one inactive ad
        for pid in self.counts:
            yield Ad(f"{pid}-s-{query}", pid, self.names[pid], True, 1_700_000_000)
        yield Ad("dead", "999", "Dead Brand", False, None)

    def company_ads(self, page_id, country):
        self.last_truncated = False
        total = self.counts.get(page_id, 0)
        requests = 0
        for start in range(0, total, self.per_page):
            if requests >= self.request_cap:
                self.last_truncated = True
                return
            self.credits_used += 1
            requests += 1
            for i in range(start, min(start + self.per_page, total)):
                yield Ad(f"{page_id}-{i}", page_id, self.names[page_id], True, 1_690_000_000 + i)
        if total == 0:
            self.credits_used += 1


print("\n--- parse ---")
check("parses a result", _parse({"ad_archive_id": 1, "page_id": 2, "page_name": " X ", "is_active": True, "start_date": 5})
      == Ad("1", "2", "X", True, 5))
check("rejects missing ids", _parse({"page_name": "x"}) is None)
check("non-int start_date is None", _parse({"ad_archive_id": 1, "page_id": 2, "start_date": "2025"}).start_date is None)
check("ad url", Ad("123", "p", "n", True, None).url == "https://www.facebook.com/ads/library/?id=123")

print("\n--- _parse evidence ---")
item_cards = {
    "ad_archive_id": 10, "page_id": "p1", "page_name": "Acme", "is_active": True,
    "start_date": 100, "collation_id": 55, "collation_count": 7,
    "snapshot": {
        "page_name": "Acme", "cta_text": "Shop Now",
        "cards": [
            {"body": "  Buy now and save big!  ", "title": "Acme Sale",
             "link_url": "https://shop.acme.com/x?y=1", "cta_text": "Learn More"},
        ],
    },
}
ad_ev = _parse(item_cards)
check("evidence body from cards[].body", ad_ev.evidence.get("body") == "Buy now and save big!", ad_ev.evidence)
check("evidence domain from cards[0].link_url", ad_ev.evidence.get("link_domain") == "shop.acme.com", ad_ev.evidence)
check("evidence cta prefers snapshot.cta_text", ad_ev.evidence.get("cta") == "Shop Now", ad_ev.evidence)
check("evidence collation_count", ad_ev.evidence.get("collation_count") == 7, ad_ev.evidence)
check("evidence collation_id kept for dedupe", ad_ev.evidence.get("collation_id") == "55", ad_ev.evidence)

item_body_text = {
    "ad_archive_id": 11, "page_id": "p2", "page_name": "Zeta", "is_active": True,
    "snapshot": {"body": {"text": "x" * 400}, "caption": "zeta.com"},
}
ad_ev2 = _parse(item_body_text)
check("evidence body from snapshot.body.text, trimmed to 300", ad_ev2.evidence.get("body") == "x" * 300,
      len(ad_ev2.evidence.get("body", "")))
check("evidence domain from snapshot.caption", ad_ev2.evidence.get("link_domain") == "zeta.com", ad_ev2.evidence)
check("no cta present -> key absent", "cta" not in ad_ev2.evidence, ad_ev2.evidence)

item_bare = {"ad_archive_id": 12, "page_id": "p3", "page_name": "Bare", "is_active": True}
ad_ev3 = _parse(item_bare)
check("no snapshot at all -> empty evidence dict", ad_ev3.evidence == {}, ad_ev3.evidence)

item_title_fallback = {
    "ad_archive_id": 13, "page_id": "p4", "page_name": "Title Only", "is_active": True,
    "snapshot": {"cards": [{"title": "  Fallback Title  ", "link_url": None}]},
}
ad_ev4 = _parse(item_title_fallback)
check("body falls back to cards[].title", ad_ev4.evidence.get("body") == "Fallback Title", ad_ev4.evidence)
check("missing link fields tolerated", "link_domain" not in ad_ev4.evidence, ad_ev4.evidence)

print("\n--- client refuses without key ---")
import os
os.environ.pop("SCRAPECREATORS_API_KEY", None)
os.environ.pop("TYPESAFE_API_KEY", None)   # guarantee Jev falls back offline, never calls out
try:
    ScrapeCreatorsClient()
    check("no key raises", False)
except RuntimeError:
    check("no key raises", True)

print("\n--- _paginate reads either results key ---")


class FakeResp:
    def __init__(self, data):
        self.status_code = 200
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class FakeHTTP:
    """Stub for client._http -- no network, no key needed."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def get(self, path, params=None):
        data = self.pages[self.calls]
        self.calls += 1
        return FakeResp(data)


sc = ScrapeCreatorsClient(api_key="x")
sc._http = FakeHTTP([
    {"searchResults": [{"ad_archive_id": 1, "page_id": "p1", "page_name": "A", "is_active": True}],
     "cursor": "next", "success": True, "credits_remaining": 999, "credits_charged": 1},
    {"searchResults": [{"ad_archive_id": 2, "page_id": "p1", "page_name": "A", "is_active": True}],
     "success": True, "credits_remaining": 998, "credits_charged": 1},
])
ads = list(sc.search("shoes"))
check("search reads searchResults across pages", [a.ad_id for a in ads] == ["1", "2"], [a.ad_id for a in ads])
check("credits incremented via _paginate", sc.credits_used == 2, sc.credits_used)

sc2 = ScrapeCreatorsClient(api_key="x")
sc2._http = FakeHTTP([
    {"results": [{"ad_archive_id": 3, "page_id": "p2", "page_name": "B", "is_active": True}],
     "success": True, "credits_remaining": 999, "credits_charged": 1},
])
ads2 = list(sc2.company_ads("p2"))
check("company_ads reads results key", [a.ad_id for a in ads2] == ["3"], [a.ad_id for a in ads2])

print("\n--- discover ---")
lib = FakeLibrary({"a": 5, "b": 150, "c": 100, "d": 99, "e": 500})
cands = discover(lib, ["shoes", "skincare"], "US")
check("all advertisers found", set(cands) == {"a", "b", "c", "d", "e"}, set(cands))
check("inactive ad ignored", "999" not in cands)
check("hits counted per keyword", cands["b"].hits == 2, cands["b"].hits)
check("keywords recorded", cands["b"].keywords == {"shoes", "skincare"})
check("sweep cost", lib.credits_used == 2, lib.credits_used)

print("\n--- count_active ---")
n, exact, ids, earliest = count_active(lib, "b", "US")
check("counts 150", n == 150, n)
check("exact when fully paged", exact)
check("ids unique", len(set(ids)) == 150)
check("earliest start found", earliest == 1_690_000_000, earliest)
deep = FakeLibrary({"e": 500}, per_page=50, request_cap=100)
n, exact, ids, _ = count_active(deep, "e", "US")
check("stops at hard cap 400", n == 400, n)
check("capped is not exact", not exact)
check("cap saves credits", deep.credits_used == 8, deep.credits_used)
small = FakeLibrary({"z": 100}, per_page=10, request_cap=3)
n, exact, _, _ = count_active(small, "z", "US")
check("client budget truncation detected", n == 30 and not exact, (n, exact))
n, exact, _, _ = count_active(lib, "a", "US")
check("small advertiser exact", n == 5 and exact)

print("\n--- count_active stop_at (floor, credit-saving) ---")
floor_lib = FakeLibrary({"e": 400}, per_page=30, request_cap=20)
n, exact, ids, _ = count_active(floor_lib, "e", "US", stop_at=100)
check("stop_at reaches at least the threshold", n >= 100, n)
check("stop_at bounds pages to ceil(threshold/page_size)", floor_lib.credits_used <= 4, floor_lib.credits_used)
check("stop_at result is a floor, not exact", not exact)
check("ids collected match count", len(ids) == n)

print("\n--- qualify ---")
# stop_at_threshold=False here so these tests keep validating exhaustive/exact counting,
# same as before floors existed; the floor behavior itself is covered above and in the
# Jev-ranking run() test below (qualify's own default is stop_at_threshold=True).
lib = FakeLibrary({"a": 5, "b": 150, "c": 100, "d": 99, "e": 500})
cands = discover(lib, ["x"], "US")
q, checked = qualify(lib, cands, threshold=100, skip_ids={"e"}, stop_at_threshold=False)
check("threshold inclusive", {x.page_id for x in q} == {"b", "c"}, {x.page_id for x in q})
check("skip honoured", "e" not in checked)
check("all non-skipped counted", set(checked) == {"a", "b", "c", "d"}, set(checked))
check("sorted by volume", [x.page_id for x in q] == ["b", "c"])
check("small advertisers store no ad ids", checked["d"]["ad_ids"] == [] and checked["b"]["ad_ids"])
check("keywords carried", checked["b"]["keywords"] == ["x"])

# Recheck window: a recently-counted page is reused, not re-counted.
before = lib.credits_used
q2, checked2 = qualify(lib, cands, threshold=100, skip_ids={"e"}, known=checked, stop_at_threshold=False)
check("recent pages not recounted", lib.credits_used == before and checked2 == {}, (lib.credits_used - before, checked2))
check("known qualifiers still returned", {x.page_id for x in q2} == {"b", "c"})
stale = {pid: {**rec, "checked_at": time.time() - 30 * 86400} for pid, rec in checked.items()}
q3, checked3 = qualify(lib, cands, threshold=100, skip_ids={"e"}, known=stale, stop_at_threshold=False)
check("stale pages recounted", set(checked3) == {"a", "b", "c", "d"})

print("\n--- qualify: explicit order overrides hits sort ---")
lib_order = FakeLibrary({"a": 5, "b": 150, "c": 100, "d": 99, "e": 500})
cands_order = discover(lib_order, ["x"], "US")
explicit_order = [cands_order["c"], cands_order["b"], cands_order["a"], cands_order["d"]]
q_order, checked_order = qualify(lib_order, cands_order, threshold=100, skip_ids={"e"}, order=explicit_order)
check("qualify counts in the given order, not hits order",
      list(checked_order) == ["c", "b", "a", "d"], list(checked_order))

print("\n--- merge + outputs ---")
tmp = Path(tempfile.mkdtemp())
try:
    state = load_state(tmp / "state.json")
    changes = merge(state, q, checked, 100)
    check("new detected", set(changes["new"]) == {"b", "c"}, changes["new"])
    check("first_qualified set", "first_qualified" in state["pages"]["b"])
    check("non-qualifier stored too", "d" in state["pages"])
    check("run logged", len(state["runs"]) == 1)

    # Second run: b scales to 200 (grew), c drops to 50 (dropped), f is new.
    checked_2 = {
        "b": {**checked["b"], "active_ads": 200, "ad_ids": [f"b-{i}" for i in range(200)], "checked_at": time.time()},
        "c": {**checked["c"], "active_ads": 50, "ad_ids": [], "checked_at": time.time()},
        "f": {"page_name": "Brand f", "active_ads": 120, "exact": True, "ad_ids": [f"f-{i}" for i in range(120)],
              "earliest_start": None, "checked_at": time.time(), "keywords": ["x"]},
    }
    time.sleep(0.02)  # time.time() ticks at ~16ms on Windows; first_qualified must differ between runs
    changes2 = merge(state, [], checked_2, 100)
    check("grew detected", changes2["grew"] == ["b"], changes2)
    check("dropped detected", changes2["dropped"] == ["c"], changes2)
    check("new on second run", changes2["new"] == ["f"], changes2)
    check("first_qualified preserved on grow", state["pages"]["b"]["first_qualified"] < state["pages"]["f"]["first_qualified"])

    paths = write_outputs(tmp, state, 100, changes2)
    rows = list(csv.DictReader(paths["leads_csv"].open()))
    check("leads csv has current qualifiers only", {r["page_id"] for r in rows} == {"b", "f"}, {r["page_id"] for r in rows})
    check("leads sorted desc", [r["page_id"] for r in rows] == ["b", "f"])
    check("page url present", "view_all_page_id=b" in rows[0]["ad_library_page"])
    check("exact flag text", rows[0]["count_is_exact"] == "yes")
    ads = list(csv.DictReader(paths["ads_csv"].open()))
    check("ads csv one row per ad", len(ads) == 320, len(ads))
    check("ad url shape", ads[0]["ad_url"].startswith("https://www.facebook.com/ads/library/?id="))
    lj = json.loads(paths["leads_json"].read_text())
    check("json has ad_urls", len(lj[0]["ad_urls"]) == 200)
    d = paths["digest"].read_text()
    check("digest sections", "## New this run" in d and "## Scaled up" in d and "## Fell below" in d)
    check("digest mentions f", "Brand f" in d)

    # load_list
    (tmp / "l.txt").write_text("# comment\nAmazon\n\n 12345 # trailing\n")
    check("load_list parses", load_list(tmp / "l.txt") == ["Amazon", "12345"], load_list(tmp / "l.txt"))
    check("load_list missing file", load_list(tmp / "nope.txt") == [])

    # Full run with exclusions by name and by id, and a candidate cap. use_jev=False: this
    # predates ranking and is testing exclusion/cap/incremental behavior, not Jev.
    lib = FakeLibrary({"a": 5, "b": 150, "c": 100, "d": 99, "e": 500, "g": 300})
    out = tmp / "run"
    logs = []
    res = run(lib, ["k1", "k2"], out, threshold=100, exclude=["Brand e", "g"], log=logs.append, use_jev=False)
    check("run excludes by name and id", res["qualified_total"] == 2, res["qualified_total"])
    check("run writes state", (out / "state.json").exists())
    check("run logs summary", any("advertisers at 100+" in l for l in logs))
    res2 = run(lib, ["k1"], out, threshold=100, exclude=["Brand e", "g"], max_candidates=2, log=logs.append,
               use_jev=False)
    check("second run is incremental", res2["changes"]["new"] == [], res2["changes"])

    print("\n--- rank_candidates ---")

    def make_candidate(pid, name, hits=1):
        c = Candidate(pid, name)
        c.hits = hits
        c.ad_ids = {f"{pid}-1", f"{pid}-2"}
        c.bodies = [f"{name} body text"]
        c.domains = {f"{pid}.com"}
        c.ctas = {"Shop Now"}
        c.collation_total = 5
        return c

    rank_cands = {
        "h1": make_candidate("h1", "Heavy Co", hits=3),
        "h2": make_candidate("h2", "Medium Co", hits=1),
        "empty": Candidate("empty", ""),   # no page_name, no bodies -> no-evidence path
    }

    def fake_judge(state, questions):
        answers = {}
        for item in state["candidates"]:
            i = item["i"]
            if item["page_name"] == "Heavy Co":
                answers[f"heavy_{i}"] = {"probabilities": {"0": 0.0, "1": 0.0, "2": 0.2, "3": 0.8}}
                answers[f"buyer_{i}"] = {"noul": 0.9}
            else:
                answers[f"heavy_{i}"] = {"probabilities": {"0": 0.5, "1": 0.5, "2": 0.0, "3": 0.0}}
                answers[f"buyer_{i}"] = {"noul": 0.2}
        return answers

    ranked = rank_candidates(rank_cands, fake_judge, threshold=100)
    by_id = {r.page_id: r for r in ranked}
    check("rank_candidates returns every candidate", len(ranked) == 3, len(ranked))
    check("p_heavy sums score levels 2+3", abs(by_id["h1"].p_heavy - 1.0) < 1e-9, by_id["h1"].p_heavy)
    check("p_buyer taken from noul", by_id["h1"].p_buyer == 0.9, by_id["h1"].p_buyer)
    check("low-heavy candidate scored near 0", abs(by_id["h2"].p_heavy - 0.0) < 1e-9, by_id["h2"].p_heavy)
    check("empty-evidence candidate defaults without asking",
          by_id["empty"].p_heavy == 0.5 and by_id["empty"].p_buyer == 0.5, by_id["empty"])

    check("decision: count", decide(by_id["h1"], 0.35, 0.5) == "count")
    check("decision: skip_unlikely", decide(by_id["h2"], 0.35, 0.5) == "skip_unlikely")
    buyer_low = Ranked("x", "X", p_heavy=0.9, p_buyer=0.1, probabilities={})
    check("decision: skip_not_buyer", decide(buyer_low, 0.35, 0.5) == "skip_not_buyer")

    def raising_judge(state, questions):
        raise RuntimeError("boom")

    ranked_fallback = rank_candidates(rank_cands, raising_judge, threshold=100)
    check("judge exception falls back without raising", len(ranked_fallback) == 3, len(ranked_fallback))
    check("fallback uses neutral probabilities for everything",
          all(r.p_heavy == 0.5 and r.p_buyer == 0.5 for r in ranked_fallback), ranked_fallback)

    many_cands = {f"c{i}": make_candidate(f"c{i}", f"Brand {i}", hits=1) for i in range(45)}
    batch_sizes = []

    def counting_judge(state, questions):
        batch_sizes.append(len(state["candidates"]))
        answers = {}
        for item in state["candidates"]:
            answers[f"heavy_{item['i']}"] = {"probabilities": {"0": 0.25, "1": 0.25, "2": 0.25, "3": 0.25}}
            answers[f"buyer_{item['i']}"] = {"noul": 0.5}
        return answers

    rank_candidates(many_cands, counting_judge, threshold=100)
    check("batches split at 40", batch_sizes == [40, 5], batch_sizes)

    print("\n--- run: Jev ranking gates counting ---")
    jev_lib = FakeLibrary({"a": 5, "b": 150, "c": 100, "d": 99, "e": 500, "g": 300})

    def rank_judge(state, questions):
        answers = {}
        for item in state["candidates"]:
            i = item["i"]
            name = item["page_name"]
            if name in ("Brand b", "Brand g"):
                answers[f"heavy_{i}"] = {"probabilities": {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9}}
                answers[f"buyer_{i}"] = {"noul": 0.9}
            elif name == "Brand c":
                answers[f"heavy_{i}"] = {"probabilities": {"0": 0.0, "1": 0.0, "2": 0.6, "3": 0.1}}
                answers[f"buyer_{i}"] = {"noul": 0.8}
            elif name == "Brand a":
                answers[f"heavy_{i}"] = {"probabilities": {"0": 0.9, "1": 0.1, "2": 0.0, "3": 0.0}}
                answers[f"buyer_{i}"] = {"noul": 0.9}
            else:   # d, e: plausible volume but not an ad-buying business
                answers[f"heavy_{i}"] = {"probabilities": {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9}}
                answers[f"buyer_{i}"] = {"noul": 0.1}
        return answers

    jev_out = tmp / "jevrun"
    jev_logs = []
    jev_res = run(jev_lib, ["k1"], jev_out, threshold=100, judge=rank_judge, log=jev_logs.append)
    ranking_rows = list(csv.DictReader((jev_out / "ranking.csv").open()))
    check("ranking.csv written for every candidate", len(ranking_rows) == 6, len(ranking_rows))
    check("ranking.csv has expected columns",
          set(ranking_rows[0].keys()) == {"page_id", "page_name", "distinct_ads_surfaced", "keywords",
                                           "collation_total", "p_heavy", "p_buyer", "decision"},
          ranking_rows[0].keys())
    by_name = {r["page_name"]: r for r in ranking_rows}
    check("unlikely-heavy advertiser skipped", by_name["Brand a"]["decision"] == "skip_unlikely", by_name["Brand a"])
    check("non-buyer advertiser skipped despite volume",
          by_name["Brand e"]["decision"] == "skip_not_buyer", by_name["Brand e"])
    counted_names = {r["page_name"] for r in ranking_rows if r["decision"] == "count"}
    check("only heavy buyers counted", counted_names == {"Brand b", "Brand c", "Brand g"}, counted_names)
    check("qualified total reflects only the counted, ranked candidates",
          jev_res["qualified_total"] == 3, jev_res["qualified_total"])
    check("counted lowest-p_heavy candidate (Brand c) last",
          any("[3/3] Brand c" in l for l in jev_logs), jev_logs)
    check("run reports a ranking path", "ranking" in jev_res["paths"])

    # --max-candidates caps AFTER ranking: only the single top-p_heavy candidate gets counted,
    # even though three cleared the Jev filters.
    jev_out2 = tmp / "jevrun2"
    jev_res2 = run(jev_lib, ["k1"], jev_out2, threshold=100, judge=rank_judge, max_candidates=1, log=lambda s: None)
    ranking_rows2 = list(csv.DictReader((jev_out2 / "ranking.csv").open()))
    counted2 = [r for r in ranking_rows2 if r["decision"] == "count"]
    check("ranking.csv still lists every eligible candidate regardless of the cap", len(counted2) == 3, len(counted2))
    check("--max-candidates caps counting to 1 after ranking", jev_res2["qualified_total"] == 1,
          jev_res2["qualified_total"])

    # No key, no injected judge: JevJudge() construction fails -> falls back to counting
    # everyone in hits order, same as --no-jev, never raising or touching the network.
    os.environ.pop("TYPESAFE_API_KEY", None)
    nokey_out = tmp / "nokeyrun"
    nokey_res = run(jev_lib, ["k1"], nokey_out, threshold=100, log=lambda s: None)
    # No ranking filter at all in this fallback, so every real qualifier counts: b, c, e, g.
    check("missing TYPESAFE_API_KEY falls back instead of crashing",
          nokey_res["qualified_total"] == 4, nokey_res["qualified_total"])
    check("fallback run writes no ranking.csv", not (nokey_out / "ranking.csv").exists())
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "-" * 40)
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("All offline checks passed.")
