#!/usr/bin/env python3
"""
Onsale Radar - watches Ticketmaster's public Discovery API for presales and
public onsales, and tells you when to show up.

This is a READ-ONLY monitor. It reads publicly published onsale schedules.
It does not buy, queue, hold, reserve, or automate any purchase, and it makes
no attempt to disguise itself or evade any access control. Ticket-buying bots
are illegal in the US under the BOTS Act; this is a calendar, not a bot.

How it hunts
------------
Rather than asking "what's up for each of my 50 acts" (which burns ~150 API
calls to mostly rediscover tours that went on sale months ago), it asks the
API the question that actually matters:

    which US music events have an onsale starting from today onward?

That single sweep (~5 calls) returns everything pending nationwide. Each
result is then bucketed:

    watched  - one of your watchlist acts. Always alerted.
    big      - arena/stadium-scale, or carrying the presale stack that marks
               a major tour onsale. Alerted even if the act isn't on your
               list, because that's how you catch a tour you didn't know was
               coming.
    other    - small local shows. Counted on the dashboard, never alerted.

Usage:
    export TM_API_KEY=...          # free key from developer.ticketmaster.com
    python3 radar.py
    python3 radar.py --dry-run     # scan and report, write nothing
    python3 radar.py --refresh-ids # re-resolve watchlist -> attraction IDs

Outputs:
    docs/index.html       dashboard (served by GitHub Pages)
    docs/onsales.json     machine-readable snapshot
    state/seen.json       which windows have already been alerted on
    alerts.md             this run's alerts, empty if none
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://app.ticketmaster.com/discovery/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.json")
IDCACHE = os.path.join(HERE, "state", "attraction_ids.json")
SEEN = os.path.join(HERE, "state", "seen.json")
DOCS = os.path.join(HERE, "docs")
ALERTS_MD = os.path.join(HERE, "alerts.md")

THROTTLE = 0.25          # API allows 5 req/sec
SOON_HOURS = 24          # "starting soon" reminder window
HORIZON_DAYS = 180       # how far ahead to track
ID_MAX_AGE_DAYS = 7      # re-resolve attraction IDs this often
COUNTRY = "US"
PAGE_SIZE = 200          # API max; deep paging capped at size*page < 1000
MAX_PAGES = 5

# What counts as "big" - tuned against a real 495-event sweep, where a naive
# version flagged 137 events including 200-capacity clubs.
#
# Venue name is the honest signal. Deliberately excludes "Center", "Theatre",
# "Ballroom", "Hall" and "Auditorium": Bowery Ballroom, Mercury Lounge and
# Lexington Opera House are small rooms, and matching them buries the real
# finds. Some genuine arenas are missed as a result (Heritage Bank Center,
# Echostage) - that trade is deliberate. This bucket is unsolicited alerts
# about acts you never asked to watch, so precision beats recall; anything
# you actually care about belongs on the watchlist, which is matched exactly.
BIG_VENUE = re.compile(
    r"\b(stadium|arena|amphitheat\w*|coliseum|colosseum|dome|sphere|"
    r"ballpark|speedway|field)\b|madison square garden|"
    r"hollywood bowl|rose bowl", re.I)   # bare "bowl" caught Brooklyn Bowl (600 cap)

# Ticketmaster's high-demand mechanism. Effectively only appears on major
# tours, unlike "Artist Presale" (35 of 137) or card presales (36 of 137),
# which are routine at club level.
VERIFIED_FAN = re.compile(r"verified fan", re.I)

BIG_PRICE = 250.0        # top-end ticket price suggesting an arena show

# Presale types worth waking someone up for on an act they don't follow.
# Skips "VIP Package Onsale", "Venue Presale", "Radio Presale" and friends.
# Word boundaries matter here: bare "chase" matched "Past PurCHASEr Presale"
# and bare "citi" would match "Citizens".
NOTABLE_PRESALE = re.compile(
    r"verified fan|american express|\bamex\b|\bciti\b|\bchase\b|"
    r"capital one|spotify|fan club", re.I)

MAX_ALERT_ITEMS = 30     # GitHub caps issue bodies at 64KB; stay well under


# ---------------------------------------------------------------- API

def api(path, params, key, retries=4):
    """One Discovery API call. Returns parsed JSON, or None on hard failure."""
    url = f"{BASE}/{path}.json?" + urllib.parse.urlencode(dict(params, apikey=key))
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("FATAL: Ticketmaster rejected the API key (401). "
                         "Check the TM_API_KEY secret.")
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if e.code >= 500:
                time.sleep(1 + attempt)
                continue
            print(f"    ! HTTP {e.code} on {path} {params}", file=sys.stderr)
            return None
        except Exception as e:
            if attempt == retries - 1:
                print(f"    ! {type(e).__name__}: {e}", file=sys.stderr)
                return None
            time.sleep(1 + attempt)
    return None


def norm(s):
    """Loose name comparison: lowercase, drop punctuation and filler words."""
    s = (s or "").lower()
    for ch in ".,'&!/-_":
        s = s.replace(ch, " ")
    return " ".join(w for w in s.split() if w not in ("the", "and"))


def resolve_attractions(artists, key):
    """Map watchlist names to Ticketmaster attraction IDs.

    Only used to make artist matching exact - keyword search returns tribute
    bands, so an exact normalized name match wins and anything else is flagged.
    """
    resolved = {}
    for a in artists:
        data = api("attractions", {
            "keyword": a["search"], "classificationName": "music",
            "size": 20, "sort": "relevance,desc",
        }, key)
        time.sleep(THROTTLE)
        found = (data or {}).get("_embedded", {}).get("attractions", [])
        hits = [x for x in found if norm(x.get("name")) == norm(a["name"])]
        exact = bool(hits)
        if not hits:
            hits = found[:1]
        if not hits:
            print(f"    - {a['name']}: no attraction found")
            continue
        resolved[a["name"]] = {
            "ids": [h["id"] for h in hits],
            "tm_names": [h.get("name") for h in hits],
            "exact_match": exact,
        }
        print(f"    + {a['name']}: {', '.join(h.get('name', '?') for h in hits)}"
              f"{'' if exact else '   <-- fuzzy, verify'}")
    return resolved


def sweep_onsales(key, now):
    """Every US music event whose onsale starts today or later.

    This is the whole hunt in one query. onsaleOnAfterStartDate filters on
    the onsale start date, so a tour announced this morning shows up here
    within the hour, months before its show date.
    """
    events, page = [], 0
    total = None
    while page < MAX_PAGES:
        data = api("events", {
            "countryCode": COUNTRY,
            "classificationName": "music",
            "onsaleOnAfterStartDate": now.strftime("%Y-%m-%d"),
            "size": PAGE_SIZE,
            "page": page,
            "sort": "date,asc",
        }, key)
        time.sleep(THROTTLE)
        if not data:
            break
        pg = data.get("page", {})
        total = pg.get("totalElements", total)
        events.extend(data.get("_embedded", {}).get("events", []))
        if page + 1 >= pg.get("totalPages", 1):
            break
        page += 1
    truncated = bool(total and total > len(events))
    print(f"  swept {len(events)} events"
          + (f" of {total} (paging capped)" if truncated else ""))
    return events, truncated


# ---------------------------------------------------------------- parsing

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def venue_of(ev):
    v = (ev.get("_embedded", {}).get("venues") or [{}])[0]
    name = v.get("name", "") or ""
    city = (v.get("city") or {}).get("name", "") or ""
    state = (v.get("state") or {}).get("stateCode", "") or ""
    return {"venue": name, "city": city, "state": state,
            "label": ", ".join(p for p in [name, city, state] if p)}


def artists_of(ev):
    return [a.get("name", "") for a in
            (ev.get("_embedded", {}).get("attractions") or []) if a.get("name")]


def classify(ev, watch_ids, watch_names):
    """Bucket an event: which watchlist act it is, or how big it looks."""
    attractions = ev.get("_embedded", {}).get("attractions") or []
    for a in attractions:
        # ID is the reliable match. Name is the fallback for when Ticketmaster
        # files an act under a second attraction ID we didn't resolve.
        if a.get("id") in watch_ids:
            return "watched", watch_ids[a["id"]]
        if norm(a.get("name")) in watch_names:
            return "watched", watch_names[norm(a.get("name"))]
    # Last resort: some events carry no attraction link at all, only a title.
    # Exact normalized equality only - "Coldplay Tribute" must not match.
    if norm(ev.get("name")) in watch_names:
        return "watched", watch_names[norm(ev.get("name"))]

    label = venue_of(ev)["venue"]
    presales = (ev.get("sales") or {}).get("presales") or []
    prices = ev.get("priceRanges") or []
    top = max((p.get("max") or 0) for p in prices) if prices else 0
    big = (BIG_VENUE.search(label)
           or any(VERIFIED_FAN.search(p.get("name") or "") for p in presales)
           or top >= BIG_PRICE)
    who = (artists_of(ev) or [ev.get("name", "")])[0]
    return ("big" if big else "other"), who


def alertable(w):
    """Should this window interrupt someone?

    Everything on the watchlist does - that's the whole point of the list.
    For an act they never asked about, only the public onsale and the presales
    that actually gate a major tour; a "VIP Package Onsale" is not news.
    """
    if w["bucket"] == "watched":
        return True
    return w["kind"] == "Public onsale" or bool(NOTABLE_PRESALE.search(w["presale_name"]))


def extract_windows(ev, artist, bucket, now, horizon):
    """Every future buying window on one event: public onsale plus presales."""
    rows = []
    base = {
        "artist": artist,
        "bucket": bucket,
        "event": ev.get("name", ""),
        "event_id": ev.get("id", ""),
        "event_date": ((ev.get("dates") or {}).get("start") or {}).get("localDate", ""),
        "url": ev.get("url", ""),
        **venue_of(ev),
    }
    sales = ev.get("sales") or {}

    pub = sales.get("public") or {}
    start = parse_dt(pub.get("startDateTime"))
    if start and now < start <= horizon and not pub.get("startTBD"):
        rows.append({**base, "kind": "Public onsale", "presale_name": "",
                     "starts": start.isoformat(), "ends": pub.get("endDateTime", "")})

    for ps in sales.get("presales") or []:
        pstart = parse_dt(ps.get("startDateTime"))
        if pstart and now < pstart <= horizon:
            rows.append({**base, "kind": "Presale",
                         "presale_name": ps.get("name", "Presale"),
                         "starts": pstart.isoformat(), "ends": ps.get("endDateTime", "")})

    for r in rows:
        r["id"] = f"{r['event_id']}|{r['kind']}|{r['presale_name']}|{r['starts']}"
    return rows


# ---------------------------------------------------------------- output

def fmt_when(iso):
    """Render a UTC timestamp in US Eastern."""
    dt = parse_dt(iso)
    if not dt:
        return "?"
    offset = -4 if 3 <= dt.month <= 11 else -5   # display only; JSON keeps UTC
    return (dt + timedelta(hours=offset)).strftime("%a %b %-d, %-I:%M %p ET")


def build_dashboard(p):
    watched = [w for w in p["windows"] if w["bucket"] == "watched"]
    big = [w for w in p["windows"] if w["bucket"] == "big"]
    now = parse_dt(p["generated"])
    soon = [w for w in p["windows"]
            if parse_dt(w["starts"]) <= now + timedelta(hours=SOON_HOURS)]

    def rows(items, empty):
        if not items:
            return f'<tr><td colspan="4" class="empty">{empty}</td></tr>'
        out = []
        for x in items:
            tag = html.escape(x["presale_name"] or x["kind"])
            cls = "presale" if x["kind"] == "Presale" else "public"
            out.append(
                f'<tr><td class="when">{html.escape(fmt_when(x["starts"]))}</td>'
                f'<td class="who"><a href="{html.escape(x["url"])}" target="_blank" '
                f'rel="noopener">{html.escape(x["artist"])}</a>'
                f'<span class="ev">{html.escape(x["event"])}</span></td>'
                f'<td class="where">{html.escape(x["label"])}'
                f'<span class="ev">show {html.escape(x["event_date"])}</span></td>'
                f'<td><span class="tag {cls}">{tag}</span></td></tr>')
        return "\n".join(out)

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Onsale Radar</title>
<style>
  :root {{ color-scheme: light dark;
    --bg:#faf9f7; --card:#fff; --ink:#1c1b19; --dim:#6b6862; --line:#e6e3dd;
    --hot:#b4341f; --cool:#2f5d8a; --ok:#3d6b45; }}
  @media (prefers-color-scheme:dark) {{ :root {{
    --bg:#16151a; --card:#1e1d23; --ink:#eceaf0; --dim:#9b97a3; --line:#2e2c35;
    --hot:#ff8a6e; --cool:#7fb0e0; --ok:#8fc79a; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--ink);
    font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif; }}
  main {{ max-width:940px; margin:0 auto; }}
  h1 {{ font-size:1.5rem; margin:0 0 .2rem; letter-spacing:-.02em; }}
  .sub {{ color:var(--dim); font-size:.85rem; margin:0 0 1.6rem; }}
  .status {{ background:var(--card); border:1px solid var(--line); border-left:3px solid var(--ok);
    border-radius:8px; padding:.7rem .9rem; font-size:.87rem; margin-bottom:1.4rem; }}
  h2 {{ font-size:.78rem; text-transform:uppercase; letter-spacing:.09em;
    color:var(--dim); margin:2.2rem 0 .2rem; font-weight:600; }}
  h2 .n {{ color:var(--ink); }}
  .note {{ color:var(--dim); font-size:.8rem; margin:.15rem 0 .6rem; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; min-width:640px; }}
  td {{ padding:.7rem .85rem; border-top:1px solid var(--line); vertical-align:top; }}
  tr:first-child td {{ border-top:none; }}
  .when {{ white-space:nowrap; font-variant-numeric:tabular-nums; font-weight:600; width:1%; }}
  .who a {{ color:inherit; text-decoration:none; border-bottom:1px solid var(--line); }}
  .who a:hover {{ border-color:currentColor; }}
  .ev {{ display:block; color:var(--dim); font-size:.8rem; font-weight:400; }}
  .tag {{ font-size:.7rem; padding:.16rem .5rem; border-radius:99px; white-space:nowrap;
    border:1px solid currentColor; }}
  .tag.presale {{ color:var(--cool); }}
  .tag.public {{ color:var(--hot); }}
  .empty {{ color:var(--dim); font-style:italic; }}
  footer {{ margin-top:2.5rem; color:var(--dim); font-size:.78rem; }}
</style></head><body><main>
<h1>Onsale Radar</h1>
<p class="sub">{p['artists_watched']} acts watched &middot; swept
{p['events_swept']} US music events with pending onsales &middot;
updated {html.escape(fmt_when(p['generated']))}</p>

<div class="status">
{'<strong>' + str(len(soon)) + ' window(s) open in the next 24 hours.</strong>' if soon
 else '<strong>Nothing opens in the next 24 hours.</strong>'}
Checked every 3 hours. An empty list means nothing is pending &mdash; not that
the radar is asleep.
</div>

<h2>Your watchlist <span class="n">({len(watched)})</span></h2>
<div class="card"><table>{rows(watched,
  'None of your 50 acts has a pending onsale. Their current dates are already on sale.')}</table></div>

<h2>Big onsales <span class="n">({len(big)})</span></h2>
<p class="note">Arena/stadium-scale shows outside your watchlist, or events
carrying the presale stack that marks a major tour.</p>
<div class="card"><table>{rows(big, 'Nothing large pending right now.')}</table></div>

<footer>
{p['other_count']} smaller local onsales were seen and not listed.
{'Paging cap reached &mdash; some events may be missing.<br>' if p.get('truncated') else ''}
Read-only monitor of Ticketmaster's public Discovery API. It reports published
onsale times; it does not buy, queue, or hold tickets.
</footer>
</main></body></html>
"""


def build_alerts_md(new, soon):
    if not new and not soon:
        return ""

    def block(title, items):
        # Watchlist first - those are the ones actually asked for.
        items = sorted(items, key=lambda x: (x["bucket"] != "watched", x["starts"]))
        shown, extra = items[:MAX_ALERT_ITEMS], max(0, len(items) - MAX_ALERT_ITEMS)
        out = [f"### {title}\n"]
        for x in shown:
            tag = x["presale_name"] or x["kind"]
            star = "" if x["bucket"] == "watched" else " _(not on your list)_"
            out.append(f"- **{fmt_when(x['starts'])}** — {x['artist']}{star} · {tag}  \n"
                       f"  {x['event']} · {x['label']} · show {x['event_date']}  \n"
                       f"  {x['url']}")
        if extra:
            out.append(f"\n_+{extra} more — see the dashboard._")
        out.append("")
        return out

    out = []
    if new:
        out += block("Newly announced", new)
    if soon:
        out += block(f"Opening within {SOON_HOURS}h", soon)
    return "\n".join(out)


# ---------------------------------------------------------------- main

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-ids", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--soon-hours", type=int, default=SOON_HOURS)
    args = ap.parse_args()

    key = os.environ.get("TM_API_KEY", "").strip()
    if not key:
        sys.exit("FATAL: TM_API_KEY is not set. Add it as a repository secret.")

    artists = load_json(WATCHLIST, {}).get("artists", [])
    if not artists:
        sys.exit("FATAL: watchlist.json has no artists.")

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=HORIZON_DAYS)

    cache = load_json(IDCACHE, {})
    cached_at = parse_dt(cache.get("_resolved_at", ""))
    stale = (not cached_at) or (now - cached_at > timedelta(days=ID_MAX_AGE_DAYS))
    changed = set(cache.get("_watchlist", [])) != {a["name"] for a in artists}

    if args.refresh_ids or stale or changed:
        why = ("forced" if args.refresh_ids
               else "watchlist changed" if changed else "cache expired")
        print(f"Resolving {len(artists)} artists to attraction IDs ({why})...")
        resolved = resolve_attractions(artists, key)
        cache = {"_resolved_at": now.isoformat(),
                 "_watchlist": [a["name"] for a in artists],
                 "artists": resolved}
        if not args.dry_run:
            os.makedirs(os.path.dirname(IDCACHE), exist_ok=True)
            with open(IDCACHE, "w") as f:
                json.dump(cache, f, indent=2)
    else:
        resolved = cache["artists"]
        print(f"Using cached attraction IDs for {len(resolved)} artists "
              f"(resolved {cached_at:%Y-%m-%d}).")

    watch_ids = {i: name for name, info in resolved.items() for i in info["ids"]}
    watch_names = {norm(name): name for name in resolved}

    print(f"\nSweeping US music onsales from {now:%Y-%m-%d} onward...")
    events, truncated = sweep_onsales(key, now)

    windows, counts = [], {"watched": 0, "big": 0, "other": 0}
    for ev in events:
        bucket, who = classify(ev, watch_ids, watch_names)
        counts[bucket] += 1
        if bucket == "other":
            continue
        windows.extend(extract_windows(ev, who, bucket, now, horizon))

    windows.sort(key=lambda r: r["starts"])
    print(f"  watched={counts['watched']} big={counts['big']} other={counts['other']}"
          f" -> {len(windows)} buy windows")

    seen = load_json(SEEN, None)
    baseline = seen is None          # first ever run
    seen = seen or {"alerted": {}, "reminded": {}}
    alerted, reminded = seen.get("alerted", {}), seen.get("reminded", {})

    soon_cut = now + timedelta(hours=args.soon_hours)
    if baseline:
        # Everything pending nationwide is "new" on a cold start. Alerting on
        # all of it would be several hundred notifications that are mostly
        # already-known onsales. Record them silently instead; from the next
        # run on, "new" means genuinely newly announced.
        new, soon = [], []
        print("  first run - recording the current picture as the baseline, "
              "no alerts sent")
    else:
        new = [x for x in windows if x["id"] not in alerted and alertable(x)]
        soon = [x for x in windows if x["id"] in alerted and x["id"] not in reminded
                and parse_dt(x["starts"]) <= soon_cut and alertable(x)]

    payload = {
        "generated": now.isoformat(),
        "artists_watched": len(resolved),
        "events_swept": len(events),
        "truncated": truncated,
        "watched_count": counts["watched"],
        "big_count": counts["big"],
        "other_count": counts["other"],
        "baseline_run": baseline,
        "new_count": len(new),
        "soon_count": len(soon),
        "windows": windows,
    }

    if args.dry_run:
        print(json.dumps({k: v for k, v in payload.items() if k != "windows"}, indent=2))
        for x in windows[:15]:
            print(f"  [{x['bucket']}] {fmt_when(x['starts'])} {x['artist']} "
                  f"- {x['presale_name'] or x['kind']} @ {x['label']}")
        return

    os.makedirs(DOCS, exist_ok=True)
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    with open(os.path.join(DOCS, "onsales.json"), "w") as f:
        json.dump(payload, f, indent=2)
    with open(os.path.join(DOCS, "index.html"), "w") as f:
        f.write(build_dashboard(payload))
    with open(ALERTS_MD, "w") as f:
        f.write(build_alerts_md(new, soon))

    # Mark every live window as seen, not just the alerted ones - otherwise a
    # window we chose not to alert on stays "new" forever and would fire the
    # moment the rules loosened.
    live = {x["id"] for x in windows}
    for x in windows:
        alerted.setdefault(x["id"], now.isoformat())
    for x in soon:
        reminded[x["id"]] = now.isoformat()
    with open(SEEN, "w") as f:
        json.dump({"alerted": {k: v for k, v in alerted.items() if k in live},
                   "reminded": {k: v for k, v in reminded.items() if k in live}},
                  f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"{len(windows)} windows | {len(new)} new | "
          f"{len(soon)} opening within {args.soon_hours}h")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"alert_count={len(new) + len(soon)}\n")


if __name__ == "__main__":
    main()
