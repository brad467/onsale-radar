#!/usr/bin/env python3
"""Offline test: fakes the Discovery API so the whole pipeline can be exercised
without a key or network. Run: python3 test_radar.py"""
import json, os, shutil, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.now(timezone.utc)


def iso(**kw):
    return (NOW + timedelta(**kw)).isoformat().replace("+00:00", "Z")


FAKE_ATTRACTIONS = {
    "Coldplay": [{"id": "K8vZ917_ku0", "name": "Coldplay", "url": "https://tm/coldplay"},
                 {"id": "TRIB", "name": "Coldplay Tribute Band", "url": "https://tm/x"}],
    "Bad Bunny": [{"id": "K8vZ9174v57", "name": "Bad Bunny", "url": "https://tm/bb"}],
    "AC DC": [{"id": "K8vZ9171C-7", "name": "AC/DC", "url": "https://tm/acdc"}],
    "Pink": [{"id": "K8vZ9171CBf", "name": "P!NK", "url": "https://tm/pink"}],
    "Zach Bryan": [],  # nothing found at all
}

FAKE_EVENTS = {
    "K8vZ917_ku0": [
        {"id": "EV1", "name": "Coldplay: Moon Music Tour", "url": "https://tm/ev1",
         "dates": {"start": {"localDate": "2027-05-20"}},
         "_embedded": {"venues": [{"name": "Bank of America Stadium",
                                   "city": {"name": "Charlotte"},
                                   "state": {"stateCode": "NC"}}]},
         "sales": {"public": {"startDateTime": iso(hours=40), "endDateTime": iso(days=200)},
                   "presales": [{"name": "Citi Cardmember Presale", "startDateTime": iso(hours=16)},
                                {"name": "Expired Fan Presale", "startDateTime": iso(days=-4)}]}},
    ],
    "K8vZ9174v57": [
        {"id": "EV2", "name": "Bad Bunny", "url": "https://tm/ev2",
         "dates": {"start": {"localDate": "2027-02-11"}},
         "_embedded": {"venues": [{"name": "Bridgestone Arena",
                                   "city": {"name": "Nashville"},
                                   "state": {"stateCode": "TN"}}]},
         "sales": {"public": {"startDateTime": iso(days=4)},
                   "presales": [{"name": "Verified Fan", "startDateTime": iso(days=2)}]}},
        {"id": "EV3", "name": "Bad Bunny", "url": "https://tm/ev3",
         "dates": {"start": {"localDate": "2027-02-14"}},
         "_embedded": {"venues": [{"name": "State Farm Arena",
                                   "city": {"name": "Atlanta"},
                                   "state": {"stateCode": "GA"}}]},
         "sales": {"public": {"startDateTime": iso(days=400)}, "presales": []}},  # past horizon
    ],
    "K8vZ9171C-7": [
        {"id": "EV4", "name": "AC/DC: Power Up Tour", "url": "https://tm/ev4",
         "dates": {"start": {"localDate": "2027-08-02"}},
         "_embedded": {"venues": [{"name": "Nissan Stadium",
                                   "city": {"name": "Nashville"},
                                   "state": {"stateCode": "TN"}}]},
         "sales": {"public": {"startDateTime": iso(hours=5), "startTBD": True},  # TBD -> skip
                   "presales": []}},
    ],
    "K8vZ9171CBf": [],
    "TRIB": [{"id": "EVX", "name": "Tribute show", "url": "https://tm/evx",
              "dates": {"start": {"localDate": "2027-01-01"}}, "_embedded": {"venues": [{}]},
              "sales": {"public": {"startDateTime": iso(days=3)}}}],
}


def fake_api(path, params, key, retries=4):
    if path == "attractions":
        return {"_embedded": {"attractions": FAKE_ATTRACTIONS.get(params["keyword"], [])}}
    if path == "events":
        if params.get("page", 0) > 0:
            return {"page": {"totalPages": 1}}
        return {"_embedded": {"events": FAKE_EVENTS.get(params["attractionId"], [])},
                "page": {"totalPages": 1}}
    return None


def run():
    tmp = tempfile.mkdtemp()
    for f in ("radar.py",):
        shutil.copy(os.path.join(HERE, f), tmp)
    # trimmed watchlist covering the interesting cases
    with open(os.path.join(tmp, "watchlist.json"), "w") as f:
        json.dump({"artists": [
            {"name": "Coldplay", "search": "Coldplay"},
            {"name": "Bad Bunny", "search": "Bad Bunny"},
            {"name": "AC/DC", "search": "AC DC"},
            {"name": "P!nk", "search": "Pink"},
            {"name": "Zach Bryan", "search": "Zach Bryan"},
        ]}, f)

    sys.path.insert(0, tmp)
    os.environ["TM_API_KEY"] = "test"
    import radar
    radar.api = fake_api
    radar.THROTTLE = 0

    failures = []

    def check(label, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(label)

    print("\n--- run 1 (cold start) ---")
    sys.argv = ["radar.py"]
    radar.main()
    p1 = json.load(open(os.path.join(tmp, "docs", "onsales.json")))
    ids = json.load(open(os.path.join(tmp, "state", "attraction_ids.json")))
    a1 = open(os.path.join(tmp, "alerts.md")).read()

    got = {(w["artist"], w["kind"], w["presale_name"]) for w in p1["windows"]}
    check("tribute band excluded from Coldplay",
          "TRIB" not in ids["artists"]["Coldplay"]["ids"])
    check("P!nk matched to P!NK despite punctuation",
          ids["artists"]["P!nk"]["exact_match"] is True)
    check("Zach Bryan (no attraction) dropped", "Zach Bryan" not in ids["artists"])
    check("expired presale excluded", ("Coldplay", "Presale", "Expired Fan Presale") not in got)
    check("live presale included", ("Coldplay", "Presale", "Citi Cardmember Presale") in got)
    check("public onsale included", ("Coldplay", "Public onsale", "") in got)
    check("startTBD onsale excluded", ("AC/DC", "Public onsale", "") not in got)
    check("beyond-horizon onsale excluded", len([w for w in p1["windows"] if w["event_id"] == "EV3"]) == 0)
    check("windows sorted ascending",
          [w["starts"] for w in p1["windows"]] == sorted(w["starts"] for w in p1["windows"]))
    check("run 1 reports everything as new", p1["new_count"] == len(p1["windows"]))
    check("run 1 has no soon-reminders", p1["soon_count"] == 0)
    check("alerts.md non-empty on run 1", "Newly announced" in a1)
    check("artists with no dates listed", "P!nk" in p1["artists_no_upcoming"])

    print("\n--- run 2 (nothing changed) ---")
    radar.main()
    p2 = json.load(open(os.path.join(tmp, "docs", "onsales.json")))
    a2 = open(os.path.join(tmp, "alerts.md")).read()
    check("run 2 reports no new windows", p2["new_count"] == 0)
    check("run 2 fires the <24h reminder once",
          p2["soon_count"] == len([w for w in p2["windows"]
                                   if radar.parse_dt(w["starts"]) <= NOW + timedelta(hours=24)]))
    check("run 2 alerts.md is reminder-only", "Newly announced" not in a2 and "within" in a2)

    print("\n--- run 3 (idempotent) ---")
    calls = []
    orig = radar.api
    radar.api = lambda p, q, k, retries=4: (calls.append(p), orig(p, q, k))[1]
    radar.main()
    radar.api = orig
    check("run 3 reuses the cached IDs (no attraction lookups)",
          "attractions" not in calls)
    p3 = json.load(open(os.path.join(tmp, "docs", "onsales.json")))
    a3 = open(os.path.join(tmp, "alerts.md")).read()
    check("run 3 is silent", p3["new_count"] == 0 and p3["soon_count"] == 0)
    check("run 3 alerts.md empty", a3.strip() == "")
    check("dashboard still lists every window",
          open(os.path.join(tmp, "docs", "index.html")).read().count("<tr>") >= len(p3["windows"]))

    print("\n--- run 4 (new artist added to watchlist) ---")
    wl = json.load(open(os.path.join(tmp, "watchlist.json")))
    wl["artists"].append({"name": "Nobody At All", "search": "Nobody At All"})
    json.dump(wl, open(os.path.join(tmp, "watchlist.json"), "w"))
    radar.main()
    ids4 = json.load(open(os.path.join(tmp, "state", "attraction_ids.json")))
    check("watchlist change triggers re-resolve",
          "Nobody At All" in ids4["_watchlist"])

    shutil.copy(os.path.join(tmp, "docs", "index.html"),
                os.path.join(HERE, "docs", "sample-dashboard.html"))
    print(f"\nSample dashboard -> docs/sample-dashboard.html")
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
