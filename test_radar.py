#!/usr/bin/env python3
"""Offline test: fakes the Discovery API so the whole pipeline can be exercised
without a key or network. Run: python3 test_radar.py"""
import json, os, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.now(timezone.utc)


def iso(**kw):
    return (NOW + timedelta(**kw)).isoformat().replace("+00:00", "Z")


FAKE_ATTRACTIONS = {
    "Coldplay": [{"id": "A_COLD", "name": "Coldplay"},
                 {"id": "A_TRIB", "name": "Coldplay Tribute Band"}],
    "Bad Bunny": [{"id": "A_BB", "name": "Bad Bunny"}],
    "AC DC": [{"id": "A_ACDC", "name": "AC/DC"}],
    "Pink": [{"id": "A_PINK", "name": "P!NK"}],
    "Zach Bryan": [],           # Ticketmaster knows nobody by this name
}


def ev(eid, name, aid, aname, venue, city, st, pub=None, presales=(),
       date="2027-06-01", prices=None, tbd=False):
    e = {"id": eid, "name": name, "url": f"https://tm/{eid}",
         "dates": {"start": {"localDate": date}},
         "_embedded": {"venues": [{"name": venue, "city": {"name": city},
                                   "state": {"stateCode": st}}]},
         "sales": {"presales": list(presales)}}
    if aid:
        e["_embedded"]["attractions"] = [{"id": aid, "name": aname}]
    if pub:
        e["sales"]["public"] = {"startDateTime": pub, "startTBD": tbd}
    if prices:
        e["priceRanges"] = prices
    return e


SWEEP = [
    # watched, matched by attraction ID
    ev("E1", "Coldplay: Moon Music Tour", "A_COLD", "Coldplay",
       "Bank of America Stadium", "Charlotte", "NC", pub=iso(hours=40),
       presales=[{"name": "Citi Presale", "startDateTime": iso(hours=16)},
                 {"name": "Expired", "startDateTime": iso(days=-3)}]),
    # watched, but filed under a second attraction ID we never resolved --
    # only the name matches
    ev("E2", "Bad Bunny en Vivo", "A_BB_ALT", "Bad Bunny", "Bridgestone Arena",
       "Nashville", "TN", pub=iso(days=3)),
    # watched, no attraction link at all - only the event title identifies it
    ev("E9", "P!nk", None, None, "Thompson-Boling Arena", "Knoxville", "TN",
       pub=iso(days=7)),
    # a tribute act must NOT match its headliner by title
    ev("E10", "Coldplay Tribute", None, None, "The Down Home",
       "Johnson City", "TN", pub=iso(days=8), prices=[{"max": 18.0}]),
    # watched but public onsale is TBD -> must be skipped
    ev("E3", "AC/DC: Power Up", "A_ACDC", "AC/DC", "Nissan Stadium",
       "Nashville", "TN", pub=iso(hours=5), tbd=True),
    # not watched, big by venue keyword
    ev("E4", "Some Huge Act", "A_X", "Some Huge Act", "MetLife Stadium",
       "East Rutherford", "NJ", pub=iso(days=2)),
    # not watched, big by Verified Fan presale even at a club-sized venue
    ev("E5", "Mystery Tour", "A_Y", "Mystery Tour", "The Fillmore",
       "Philadelphia", "PA", pub=iso(days=5),
       presales=[{"name": "Verified Fan Onsale", "startDateTime": iso(days=4)},
                 {"name": "VIP Package Onsale", "startDateTime": iso(days=4, hours=2)}]),
    # not watched, big by price
    ev("E6", "Pricey Gig", "A_Z", "Pricey Gig", "Beacon Theatre", "New York",
       "NY", pub=iso(days=6), prices=[{"max": 420.0}]),
    # the false positive that broke the first live run: a 575-cap club with
    # routine Artist/VIP presales must NOT read as big
    # regex precision traps found in live output: "PurCHASEr" must not match
    # the Chase card presale, and Brooklyn Bowl is a 600-cap club
    ev("E14", "Someone Small", "A_SS", "Someone Small", "Brooklyn Bowl",
       "Brooklyn", "NY", pub=iso(days=12), prices=[{"max": 35.0}],
       presales=[{"name": "Past Purchaser Presale", "startDateTime": iso(days=11)}]),
    ev("E11", "Lexi Jayde", "A_LJ", "Lexi Jayde", "Bowery Ballroom",
       "New York", "NY", pub=iso(days=9), prices=[{"max": 45.0}],
       presales=[{"name": "Artist Presale", "startDateTime": iso(days=8)},
                 {"name": "VIP Package Onsale", "startDateTime": iso(days=8, hours=1)},
                 {"name": "Venue Presale", "startDateTime": iso(days=8, hours=2)}]),
    # small local show -> counted, never alerted
    ev("E7", "Open Mic Night", "A_W", "Local Band", "The Basement",
       "Johnson City", "TN", pub=iso(days=1), prices=[{"max": 12.0}]),
    # beyond the horizon -> dropped
    ev("E8", "Far Future Fest", "A_V", "Far Future", "Citi Field", "Queens",
       "NY", pub=iso(days=400)),
]


def fake_api(path, params, key, retries=4):
    if path == "attractions":
        return {"_embedded": {"attractions": FAKE_ATTRACTIONS.get(params["keyword"], [])}}
    if path == "events":
        if params.get("page", 0) > 0:
            return {"page": {"totalPages": 1, "totalElements": len(SWEEP)}}
        return {"_embedded": {"events": SWEEP},
                "page": {"totalPages": 1, "totalElements": len(SWEEP)}}
    return None


def run():
    tmp = tempfile.mkdtemp()
    shutil.copy(os.path.join(HERE, "radar.py"), tmp)
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
    got = {(w["artist"], w["bucket"], w["kind"], w["presale_name"]) for w in p1["windows"]}
    buckets = {w["event_id"]: w["bucket"] for w in p1["windows"]}

    check("tribute band excluded from Coldplay",
          "A_TRIB" not in ids["artists"]["Coldplay"]["ids"])
    check("P!nk matched to P!NK despite punctuation",
          ids["artists"]["P!nk"]["exact_match"] is True)
    check("unknown artist dropped from cache", "Zach Bryan" not in ids["artists"])
    check("watched matched by attraction id", ("Coldplay", "watched", "Public onsale", "") in got)
    check("watched matched by attraction name when id is unknown",
          ("Bad Bunny", "watched", "Public onsale", "") in got)
    check("watched matched by event title when no attraction link",
          ("P!nk", "watched", "Public onsale", "") in got)
    check("tribute act not matched to headliner by title",
          buckets.get("E10") != "watched")
    check("live presale kept", ("Coldplay", "watched", "Presale", "Citi Presale") in got)
    check("expired presale dropped",
          not any(w["presale_name"] == "Expired" for w in p1["windows"]))
    check("startTBD onsale dropped", "E3" not in buckets)
    check("big by venue keyword", buckets.get("E4") == "big")
    check("big by Verified Fan presale", buckets.get("E5") == "big")
    check("big by ticket price", buckets.get("E6") == "big")
    check("club with routine Artist/VIP presales is NOT big",
          buckets.get("E11") != "big")
    check("Brooklyn Bowl does not match the stadium 'bowl' rule",
          buckets.get("E14") != "big")
    check("'Past Purchaser' does not match the Chase card presale",
          not radar.NOTABLE_PRESALE.search("Past Purchaser Presale"))
    check("real Chase/Citi presales still match",
          bool(radar.NOTABLE_PRESALE.search("Chase Cardmember Presale"))
          and bool(radar.NOTABLE_PRESALE.search("Citi® Cardmember Preferred Tickets")))
    check("Hollywood Bowl still counts as big",
          bool(radar.BIG_VENUE.search("Hollywood Bowl")))
    check("small local show not listed", "E7" not in buckets)
    check("small local shows still counted", p1["other_count"] == 4)
    check("beyond-horizon dropped", "E8" not in buckets)
    check("windows sorted ascending",
          [w["starts"] for w in p1["windows"]] == sorted(w["starts"] for w in p1["windows"]))
    check("first run is a silent baseline", p1["baseline_run"] is True
          and p1["new_count"] == 0 and a1.strip() == "")

    print("\n--- run 1b (a genuinely new announcement) ---")
    SWEEP.append(
        ev("E12", "Coldplay: extra night", "A_COLD", "Coldplay",
           "Soldier Field", "Chicago", "IL", pub=iso(days=10),
           presales=[{"name": "VIP Package Onsale", "startDateTime": iso(days=9)},
                     {"name": "Verified Fan", "startDateTime": iso(days=9, hours=1)}]))
    SWEEP.append(
        ev("E13", "Nobody Famous", "A_NF", "Nobody Famous", "Levi's Stadium",
           "Santa Clara", "CA", pub=iso(days=11),
           presales=[{"name": "VIP Package Onsale", "startDateTime": iso(days=10)}]))
    radar.main()
    p1b = json.load(open(os.path.join(tmp, "docs", "onsales.json")))
    a1b = open(os.path.join(tmp, "alerts.md")).read()
    newids = {w["event_id"] for w in p1b["windows"]}
    check("baseline not repeated on second run", p1b["baseline_run"] is False)
    check("new watchlist announcement alerts", "Coldplay: extra night" in a1b)
    check("watchlist VIP presale still alerts (it's on the list)",
          a1b.count("Coldplay: extra night") >= 2)
    check("off-list stadium public onsale alerts", "Nobody Famous" in a1b)
    # Coldplay is on the list so ITS VIP presale is fair game; the off-list
    # act's VIP presale is the one that must stay quiet.
    nf_lines = [l for l in a1b.split("\n") if "Nobody Famous" in l]
    check("off-list VIP package presale does NOT alert",
          any("Public onsale" in l for l in nf_lines)
          and not any("VIP Package" in l for l in nf_lines))
    check("off-list entries flagged", "(not on your list)" in a1b)
    check("previously-seen windows do not re-alert",
          "Some Huge Act" not in a1b and "Mystery Tour" not in a1b)
    check("run 1b fires the <24h reminder", p1b["soon_count"] >= 1
          and "Opening within" in a1b)

    print("\n--- run 2 (nothing changed) ---")
    calls = []
    orig = radar.api
    radar.api = lambda p, q, k, retries=4: (calls.append(p), orig(p, q, k))[1]
    radar.main()
    radar.api = orig
    p2 = json.load(open(os.path.join(tmp, "docs", "onsales.json")))
    a2 = open(os.path.join(tmp, "alerts.md")).read()
    check("run 2 reuses cached IDs (no attraction lookups)", "attractions" not in calls)
    check("run 2 reports nothing new", p2["new_count"] == 0)
    check("a reminder never fires twice", p2["soon_count"] == 0)
    check("run 2 alerts.md is empty", a2.strip() == "")

    print("\n--- run 3 (idempotent) ---")
    radar.main()
    p3 = json.load(open(os.path.join(tmp, "docs", "onsales.json")))
    a3 = open(os.path.join(tmp, "alerts.md")).read()
    check("run 3 is silent", p3["new_count"] == 0 and p3["soon_count"] == 0)
    check("run 3 alerts.md empty", a3.strip() == "")

    html = open(os.path.join(tmp, "docs", "index.html")).read()
    check("dashboard has both sections",
          "Your watchlist" in html and "Big onsales" in html)
    check("dashboard explains an empty list",
          "not that" in html and "asleep" in html)
    check("dashboard reports swept count", str(p3["events_swept"]) in html)

    print("\n--- run 4 (watchlist edited) ---")
    wl = json.load(open(os.path.join(tmp, "watchlist.json")))
    wl["artists"].append({"name": "Nobody At All", "search": "Nobody At All"})
    json.dump(wl, open(os.path.join(tmp, "watchlist.json"), "w"))
    radar.main()
    ids4 = json.load(open(os.path.join(tmp, "state", "attraction_ids.json")))
    check("watchlist change triggers re-resolve", "Nobody At All" in ids4["_watchlist"])

    with open(os.path.join(HERE, "sample-dashboard.html"), "w") as f:
        f.write(html)
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
#!/usr/bin/env python3
"""Offline test: fakes the Discovery API so the whole pipeline can be exercised
without a key or network. Run: python3 test_radar.py"""
import json, os, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.now(timezone.utc)


def iso(**kw):
    return (NOW + timedelta(**kw)).isoformat().replace("+00:00", "Z")


FAKE_ATTRACTIONS = {
    "Coldplay": [{"id": "A_COLD", "name": "Coldplay"},
                 {"id": "A_TRIB", "name": "Coldplay Tribute Band"}],
    "Bad Bunny": [{"id": "A_BB", "name": "Bad Bunny"}],
    "AC DC": [{"id": "A_ACDC", "name": "AC/DC"}],
    "Pink": [{"id": "A_PINK", "name": "P!NK"}],
    "Zach Bryan": [],           # Ticketmaster knows nobody by this name
}


def ev(eid, name, aid, aname, venue, city, st, pub=None, presales=(),
       date="2027-06-01", prices=None, tbd=False):
    e = {"id": eid, "name": name, "url": f"https://tm/{eid}",
         "dates": {"start": {"localDate": date}},
         "_embedded": {"venues": [{"name": venue, "city": {"name": city},
                                   "state": {"stateCode": st}}]},
         "sales": {"presales": list(presales)}}
    if aid:
        e["_embedded"]["attractions"] = [{"id": aid, "name": aname}]
    if pub:
        e["sales"]["public"] = {"startDateTime": pub, "startTBD": tbd}
    if prices:
        e["priceRanges"] = prices
    return e


SWEEP = [
    # watched, matched by attraction ID
    ev("E1", "Coldplay: Moon Music Tour", "A_COLD", "Coldplay",
       "Bank of America Stadium", "Charlotte", "NC", pub=iso(hours=40),
       presales=[{"name": "Citi Presale", "startDateTime": iso(hours=16)},
                 {"name": "Expired", "startDateTime": iso(days=-3)}]),
    # watched, but filed under a second attraction ID we never resolved --
    # only the name matches
    ev("E2", "Bad Bunny en Vivo", "A_BB_ALT", "Bad Bunny", "Bridgestone Arena",
       "Nashville", "TN", pub=iso(days=3)),
    # watched, no attraction link at all - only the event title identifies it
    ev("E9", "P!nk", None, None, "Thompson-Boling Arena", "Knoxville", "TN",
       pub=iso(days=7)),
    # a tribute act must NOT match its headliner by title
    ev("E10", "Coldplay Tribute", None, None, "The Down Home",
       "Johnson City", "TN", pub=iso(days=8), prices=[{"max": 18.0}]),
    # watched but public onsale is TBD -> must be skipped
    ev("E3", "AC/DC: Power Up", "A_ACDC", "AC/DC", "Nissan Stadium",
       "Nashville", "TN", pub=iso(hours=5), tbd=True),
    # not watched, big by venue keyword
    ev("E4", "Some Huge Act", "A_X", "Some Huge Act", "MetLife Stadium",
       "East Rutherford", "NJ", pub=iso(days=2)),
    # not watched, big by Verified Fan presale even at a club-sized venue
    ev("E5", "Mystery Tour", "A_Y", "Mystery Tour", "The Fillmore",
       "Philadelphia", "PA", pub=iso(days=5),
       presales=[{"name": "Verified Fan Onsale", "startDateTime": iso(days=4)},
                 {"name": "VIP Package Onsale", "startDateTime": iso(days=4, hours=2)}]),
    # not watched, big by price
    ev("E6", "Pricey Gig", "A_Z", "Pricey Gig", "Beacon Theatre", "New York",
       "NY", pub=iso(days=6), prices=[{"max": 420.0}]),
    # the false positive that broke the first live run: a 575-cap club with
    # routine Artist/VIP presales must NOT read as big
    ev("E11", "Lexi Jayde", "A_LJ", "Lexi Jayde", "Bowery Ballroom",
       "New York", "NY", pub=iso(days=9), prices=[{"max": 45.0}],
       presales=[{"name": "Artist Presale", "startDateTime": iso(days=8)},
                 {"name": "VIP Package Onsale", "startDateTime": iso(days=8, hours=1)},
                 {"name": "Venue Presale", "startDateTime": iso(days=8, hours=2)}]),
    # small local show -> counted, never alerted
    ev("E7", "Open Mic Night", "A_W", "Local Band", "The Basement",
       "Johnson City", "TN", pub=iso(days=1), prices=[{"max": 12.0}]),
    # beyond the horizon -> dropped
    ev("E8", "Far Future Fest", "A_V", "Far Future", "Citi Field", "Queens",
       "NY", pub=iso(days=400)),
]


def fake_api(path, params, key, retries=4):
    if path == "attractions":
        return {"_embedded": {"attractions": FAKE_ATTRACTIONS.get(params["keyword"], [])}}
    if path == "events":
        if params.get("page", 0) > 0:
            return {"page": {"totalPages": 1, "totalElements": len(SWEEP)}}
        return {"_embedded": {"events": SWEEP},
                "page": {"totalPages": 1, "totalElements": len(SWEEP)}}
    return None


def run():
    tmp = tempfile.mkdtemp()
    shutil.copy(os.path.join(HERE, "radar.py"), tmp)
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
    got = {(w["artist"], w["bucket"], w["kind"], w["presale_name"]) for w in p1["windows"]}
    buckets = {w["event_id"]: w["bucket"] for w in p1["windows"]}

    check("tribute band excluded from Coldplay",
          "A_TRIB" not in ids["artists"]["Coldplay"]["ids"])
    check("P!nk matched to P!NK despite punctuation",
          ids["artists"]["P!nk"]["exact_match"] is True)
    check("unknown artist dropped from cache", "Zach Bryan" not in ids["artists"])
    check("watched matched by attraction id", ("Coldplay", "watched", "Public onsale", "") in got)
    check("watched matched by attraction name when id is unknown",
          ("Bad Bunny", "watched", "Public onsale", "") in got)
    check("watched matched by event title when no attraction link",
          ("P!nk", "watched", "Public onsale", "") in got)
    check("tribute act not matched to headliner by title",
          buckets.get("E10") != "watched")
    check("live presale kept", ("Coldplay", "watched", "Presale", "Citi Presale") in got)
    check("expired presale dropped",
          not any(w["presale_name"] == "Expired" for w in p1["windows"]))
    check("startTBD onsale dropped", "E3" not in buckets)
    check("big by venue keyword", buckets.get("E4") == "big")
    check("big by Verified Fan presale", buckets.get("E5") == "big")
    check("big by ticket price", buckets.get("E6") == "big")
    check("club with routine Artist/VIP presales is NOT big",
          buckets.get("E11") != "big")
    check("small local show not listed", "E7" not in buckets)
    check("small local shows still counted", p1["other_count"] == 3)
    check("beyond-horizon dropped", "E8" not in buckets)
    check("windows sorted ascending",
          [w["starts"] for w in p1["windows"]] == sorted(w["starts"] for w in p1["windows"]))
    check("first run is a silent baseline", p1["baseline_run"] is True
          and p1["new_count"] == 0 and a1.strip() == "")

    print("\n--- run 1b (a genuinely new announcement) ---")
    SWEEP.append(
        ev("E12", "Coldplay: extra night", "A_COLD", "Coldplay",
           "Soldier Field", "Chicago", "IL", pub=iso(days=10),
           presales=[{"name": "VIP Package Onsale", "startDateTime": iso(days=9)},
                     {"name": "Verified Fan", "startDateTime": iso(days=9, hours=1)}]))
    SWEEP.append(
        ev("E13", "Nobody Famous", "A_NF", "Nobody Famous", "Levi's Stadium",
           "Santa Clara", "CA", pub=iso(days=11),
           presales=[{"name": "VIP Package Onsale", "startDateTime": iso(days=10)}]))
    radar.main()
    p1b = json.load(open(os.path.join(tmp, "docs", "onsales.json")))
    a1b = open(os.path.join(tmp, "alerts.md")).read()
    newids = {w["event_id"] for w in p1b["windows"]}
    check("baseline not repeated on second run", p1b["baseline_run"] is False)
    check("new watchlist announcement alerts", "Coldplay: extra night" in a1b)
    check("watchlist VIP presale still alerts (it's on the list)",
          a1b.count("Coldplay: extra night") >= 2)
    check("off-list stadium public onsale alerts", "Nobody Famous" in a1b)
    # Coldplay is on the list so ITS VIP presale is fair game; the off-list
    # act's VIP presale is the one that must stay quiet.
    nf_lines = [l for l in a1b.split("\n") if "Nobody Famous" in l]
    check("off-list VIP package presale does NOT alert",
          any("Public onsale" in l for l in nf_lines)
          and not any("VIP Package" in l for l in nf_lines))
    check("off-list entries flagged", "(not on your list)" in a1b)
    check("previously-seen windows do not re-alert",
          "Some Huge Act" not in a1b and "Mystery Tour" not in a1b)
    check("run 1b fires the <24h reminder", p1b["soon_count"] >= 1
          and "Opening within" in a1b)

    print("\n--- run 2 (nothing changed) ---")
    calls = []
    orig = radar.api
    radar.api = lambda p, q, k, retries=4: (calls.append(p), orig(p, q, k))[1]
    radar.main()
    radar.api = orig
    p2 = json.load(open(os.path.join(tmp, "docs", "onsales.json")))
    a2 = open(os.path.join(tmp, "alerts.md")).read()
    check("run 2 reuses cached IDs (no attraction lookups)", "attractions" not in calls)
    check("run 2 reports nothing new", p2["new_count"] == 0)
    check("a reminder never fires twice", p2["soon_count"] == 0)
    check("run 2 alerts.md is empty", a2.strip() == "")

    print("\n--- run 3 (idempotent) ---")
    radar.main()
    p3 = json.load(open(os.path.join(tmp, "docs", "onsales.json")))
    a3 = open(os.path.join(tmp, "alerts.md")).read()
    check("run 3 is silent", p3["new_count"] == 0 and p3["soon_count"] == 0)
    check("run 3 alerts.md empty", a3.strip() == "")

    html = open(os.path.join(tmp, "docs", "index.html")).read()
    check("dashboard has both sections",
          "Your watchlist" in html and "Big onsales" in html)
    check("dashboard explains an empty list",
          "not that" in html and "asleep" in html)
    check("dashboard reports swept count", str(p3["events_swept"]) in html)

    print("\n--- run 4 (watchlist edited) ---")
    wl = json.load(open(os.path.join(tmp, "watchlist.json")))
    wl["artists"].append({"name": "Nobody At All", "search": "Nobody At All"})
    json.dump(wl, open(os.path.join(tmp, "watchlist.json"), "w"))
    radar.main()
    ids4 = json.load(open(os.path.join(tmp, "state", "attraction_ids.json")))
    check("watchlist change triggers re-resolve", "Nobody At All" in ids4["_watchlist"])

    with open(os.path.join(HERE, "sample-dashboard.html"), "w") as f:
        f.write(html)
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
