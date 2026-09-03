#!/usr/bin/env python3
"""
Onsale Radar - watches Ticketmaster's public Discovery API for presales and
public onsales from a watchlist of top touring acts, and tells you when to
show up.

This is a READ-ONLY monitor. It reads publicly published onsale schedules.
It does not buy, queue, hold, reserve, or automate any purchase, and it makes
no attempt to disguise itself or evade any access control. Ticket-buying bots
are illegal in the US under the BOTS Act; this is a calendar, not a bot.

Usage:
    export TM_API_KEY=...          # free key from developer.ticketmaster.com
    python3 radar.py               # normal run
    python3 radar.py --refresh-ids # re-resolve artist -> attraction IDs
    python3 radar.py --dry-run     # scan and report, write nothing

Outputs:
    docs/index.html       dashboard (served by GitHub Pages)
    docs/onsales.json     machine-readable snapshot
    state/seen.json       which windows have already been alerted on
    alerts.md             this run's new alerts, empty if none
"""

import argparse
import html
import json
import os
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
            if e.code == 429:          # rate limited - back off hard
                time.sleep(3 * (attempt + 1))
                continue
            if e.code >= 500:
                time.sleep(1 + attempt)
                continue
            print(f"    ! HTTP {e.code} on {path}", file=sys.stderr)
            return None
        except Exception as e:
            if attempt == retries - 1:
                print(f"    ! {type(e).__name__}: {e}", file=sys.stderr)
                return None
            time.sleep(1 + attempt)
    return None


def norm(s):
    """Loose name comparison: lowercase, drop punctuation and filler words."""
    s = s.lower()
    for ch in ".,'&!/-_":
        s = s.replace(ch, " ")
    return " ".join(w for w in s.split() if w not in ("the", "and"))


def resolve_attractions(artists, key):
    """Map watchlist names to Ticketmaster attraction IDs.

    Keyword search happily returns tribute bands, so an exact normalized name
    match wins; anything else is kept but flagged for a human to eyeball.
    """
    resolved = {}
    for a in artists:
        name, search = a["name"], a["search"]
        data = api("attractions", {
            "keyword": search,
            "classificationName": "music",
            "size": 20,
            "sort": "relevance,desc",
        }, key)
        time.sleep(THROTTLE)
        found = (data or {}).get("_embedded", {}).get("attractions", [])
        hits = [x for x in found if norm(x.get("name", "")) == norm(name)]
        exact = bool(hits)
        if not hits:
            hits = found[:1]
        if not hits:
            print(f"    - {name}: no attraction found")
            continue
        resolved[name] = {
            "ids": [h["id"] for h in hits],
            "tm_names": [h.get("name") for h in hits],
            "exact_match": exact,
            "url": hits[0].get("url", ""),
        }
        print(f"    + {name}: {', '.join(h.get('name', '?') for h in hits)}"
              f"{'' if exact else '   <-- fuzzy, verify'}")
    return resolved


def events_for(ids, key):
    """Every upcoming US event for one artist, across their attraction IDs."""
    out, seen_ids = [], set()
    for aid in ids:
        page = 0
        while page < 5:       # deep paging is capped at size*page < 1000
            data = api("events", {
                "attractionId": aid,
                "countryCode": COUNTRY,
                "size": 100,
                "page": page,
                "sort": "date,asc",
            }, key)
            time.sleep(THROTTLE)
            if not data:
                break
            for ev in data.get("_embedded", {}).get("events", []):
                if ev.get("id") not in seen_ids:
                    seen_ids.add(ev.get("id"))
                    out.append(ev)
            if page + 1 >= data.get("page", {}).get("totalPages", 1):
                break
            page += 1
    return out


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
    city = (v.get("city") or {}).get("name", "")
    state = (v.get("state") or {}).get("stateCode", "")
    return {
        "venue": v.get("name", ""),
        "city": city,
        "state": state,
        "label": ", ".join(p for p in [v.get("name", ""), city, state] if p),
    }


def extract_windows(ev, artist, now, horizon):
    """Every future buying window on one event: public onsale plus presales."""
    rows = []
    base = {
        "artist": artist,
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
    """Render a UTC timestamp in US Eastern, which is where Brad buys from."""
    dt = parse_dt(iso)
    if not dt:
        return "?"
    # -4 Mar-Nov, -5 otherwise. Close enough for a display string, and the
    # exact instant is always in the JSON.
    offset = -4 if 3 <= dt.month <= 11 else -5
    local = dt + timedelta(hours=offset)
    return local.strftime("%a %b %-d, %-I:%M %p ET")


def build_dashboard(payload):
    w = payload["windows"]
    now = parse_dt(payload["generated"])
    soon = [x for x in w if parse_dt(x["starts"]) <= now + timedelta(hours=SOON_HOURS)]
    week = [x for x in w if x not in soon
            and parse_dt(x["starts"]) <= now + timedelta(days=7)]
    later = [x for x in w if x not in soon and x not in week]

    def rows(items):
        if not items:
            return '<tr><td colspan="4" class="empty">Nothing here right now.</td></tr>'
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

    missing = payload.get("artists_no_upcoming", [])
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Onsale Radar</title>
<style>
  :root {{ color-scheme: light dark;
    --bg:#faf9f7; --card:#fff; --ink:#1c1b19; --dim:#6b6862; --line:#e6e3dd;
    --hot:#b4341f; --cool:#2f5d8a; }}
  @media (prefers-color-scheme:dark) {{ :root {{
    --bg:#16151a; --card:#1e1d23; --ink:#eceaf0; --dim:#9b97a3; --line:#2e2c35;
    --hot:#ff8a6e; --cool:#7fb0e0; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--ink);
    font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif; }}
  main {{ max-width:920px; margin:0 auto; }}
  h1 {{ font-size:1.5rem; margin:0 0 .2rem; letter-spacing:-.02em; }}
  .sub {{ color:var(--dim); font-size:.85rem; margin:0 0 2rem; }}
  h2 {{ font-size:.78rem; text-transform:uppercase; letter-spacing:.09em;
    color:var(--dim); margin:2.2rem 0 .6rem; font-weight:600; }}
  h2 .n {{ color:var(--ink); }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; min-width:620px; }}
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
<p class="sub">{payload['artists_watched']} acts &middot; {len(w)} upcoming buy windows &middot;
updated {html.escape(fmt_when(payload['generated']))}</p>

<h2>Next 24 hours <span class="n">({len(soon)})</span></h2>
<div class="card"><table>{rows(soon)}</table></div>

<h2>This week <span class="n">({len(week)})</span></h2>
<div class="card"><table>{rows(week)}</table></div>

<h2>Further out <span class="n">({len(later)})</span></h2>
<div class="card"><table>{rows(later)}</table></div>

<footer>
Read-only monitor of Ticketmaster's public Discovery API. It reports published
onsale times; it does not buy, queue, or hold tickets.
{('<br>No dates announced yet: ' + html.escape(', '.join(missing))) if missing else ''}
</footer>
</main></body></html>
"""


def build_alerts_md(new, soon):
    if not new and not soon:
        return ""
    out = []
    if new:
        out.append("### Newly announced\n")
        for x in new:
            tag = x["presale_name"] or x["kind"]
            out.append(f"- **{fmt_when(x['starts'])}** — {x['artist']} · {tag}  \n"
                       f"  {x['event']} · {x['label']} · show {x['event_date']}  \n"
                       f"  {x['url']}")
        out.append("")
    if soon:
        out.append(f"### Opening within {SOON_HOURS}h\n")
        for x in soon:
            tag = x["presale_name"] or x["kind"]
            out.append(f"- **{fmt_when(x['starts'])}** — {x['artist']} · {tag}  \n"
                       f"  {x['event']} · {x['label']}  \n"
                       f"  {x['url']}")
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
    # Compare against the names we last *attempted*, not the ones that
    # resolved - otherwise an artist Ticketmaster has never heard of makes
    # the cache look permanently out of date and we re-resolve every run.
    changed = set(cache.get("_watchlist", [])) != {a["name"] for a in artists}

    if args.refresh_ids or stale or changed:
        why = "forced" if args.refresh_ids else ("watchlist changed" if changed else "cache expired")
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

    print(f"\nScanning {COUNTRY} events...")
    windows, no_dates = [], []
    for name, info in resolved.items():
        evs = events_for(info["ids"], key)
        rows = []
        for ev in evs:
            rows.extend(extract_windows(ev, name, now, horizon))
        windows.extend(rows)
        if not rows:
            no_dates.append(name)
        print(f"    {name}: {len(evs)} events -> {len(rows)} windows")

    windows.sort(key=lambda r: r["starts"])

    seen = load_json(SEEN, {"alerted": {}, "reminded": {}})
    alerted, reminded = seen.get("alerted", {}), seen.get("reminded", {})

    soon_cut = now + timedelta(hours=args.soon_hours)
    new = [x for x in windows if x["id"] not in alerted]
    soon = [x for x in windows
            if x["id"] not in reminded
            and x["id"] in alerted
            and parse_dt(x["starts"]) <= soon_cut]

    payload = {
        "generated": now.isoformat(),
        "artists_watched": len(resolved),
        "artists_no_upcoming": sorted(no_dates),
        "new_count": len(new),
        "soon_count": len(soon),
        "windows": windows,
    }

    if args.dry_run:
        print(json.dumps({k: v for k, v in payload.items() if k != "windows"}, indent=2))
        print(f"\n{len(new)} new, {len(soon)} opening within {args.soon_hours}h")
        return

    os.makedirs(DOCS, exist_ok=True)
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    with open(os.path.join(DOCS, "onsales.json"), "w") as f:
        json.dump(payload, f, indent=2)
    with open(os.path.join(DOCS, "index.html"), "w") as f:
        f.write(build_dashboard(payload))
    with open(ALERTS_MD, "w") as f:
        f.write(build_alerts_md(new, soon))

    live = {x["id"] for x in windows}
    for x in new:
        alerted[x["id"]] = now.isoformat()
    for x in soon:
        reminded[x["id"]] = now.isoformat()
    # forget windows that have passed, so state/seen.json stays small
    with open(SEEN, "w") as f:
        json.dump({"alerted": {k: v for k, v in alerted.items() if k in live},
                   "reminded": {k: v for k, v in reminded.items() if k in live}},
                  f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"{len(windows)} upcoming windows | {len(new)} new | "
          f"{len(soon)} opening within {args.soon_hours}h")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"alert_count={len(new) + len(soon)}\n")


if __name__ == "__main__":
    main()
