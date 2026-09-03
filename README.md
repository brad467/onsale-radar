# Onsale Radar

Watches Ticketmaster's public Discovery API for presales and public onsales
from a watchlist of 50 top-grossing touring acts, and tells you when to show up.

**This is a calendar, not a bot.** It reads published onsale times and notifies
you. It does not buy, queue, hold, or reserve tickets, and it doesn't try to
look like anything other than what it is. Automated ticket *purchasing* is
illegal in the US under the BOTS Act (2016) and gets accounts permanently
banned and orders cancelled — so this deliberately stops at the alert.

## What you get

- **Push to your phone** the moment a new tour date is announced, and again
  ~24h before the window opens
- **Email** for the same events, via GitHub notifications
- **A live dashboard** at `https://<you>.github.io/onsale-radar/`
- Presales tracked separately from public onsales, because presales are where
  the tickets actually are

Runs every 3 hours on GitHub's servers. Free. Your laptop can be closed.

## How it hunts

The obvious design — "check each of my 50 acts" — turns out to be both slow
and nearly useless. Ticketmaster's API returns an artist's *listed* events,
and by the time an event is listed its onsale has almost always already
happened. Scanning 50 acts costs ~150 API calls to mostly rediscover tours
that went on sale months ago.

So it asks the API the question that actually matters instead:

> which US music events have an onsale starting from today onward?

One sweep, about 5 calls, returns everything pending nationwide. Each result
is then bucketed:

| Bucket | What it is | Alerted? |
|---|---|---|
| **watched** | one of your watchlist acts | always, every window |
| **big** | arena/stadium venue, a Verified Fan presale, or a $250+ top ticket | public onsale and major presales only |
| **other** | small local shows | no, just counted |

The **big** bucket is the point: it catches a tour announcement from an act you
never thought to add to the list, in the hour it goes up.

### Why "big" is defined the way it is

The first live sweep returned 495 events. A looser first definition — counting
3+ presales, or a $150 top ticket — flagged **137** of them, including Bowery
Ballroom (575 capacity) and Mercury Lounge (250). The reason is that "Artist
Presale" appeared on 35 of those events and card presales on 36; they're
routine at club level and carry no signal at all.

So the rules were tightened against that real data:

- **Venue name** is the honest signal. The regex deliberately excludes
  "Center", "Theatre", "Ballroom", "Hall" and "Auditorium" — matching those
  buries the real finds. It costs some genuine arenas that don't say "Arena"
  (Heritage Bank Center, Echostage). That trade is intentional: this bucket
  sends unsolicited alerts about acts you never asked to watch, so precision
  beats recall. Anything you actually care about goes on the watchlist, which
  is matched exactly.
- **Verified Fan** is kept because it's Ticketmaster's high-demand mechanism
  and effectively only appears on major tours.
- Generic presale counting is gone.

### The first run is silent

A cold start would otherwise fire several hundred notifications for onsales
that mostly went up weeks ago. Instead the first run records the current
picture as a baseline and sends nothing. From the second run on, "new" means
genuinely newly announced. Alerts are also capped at 30 items per section,
with a "+N more" pointer to the dashboard — GitHub rejects issue bodies over
64KB, which is exactly how the first live attempt failed.

Artist matching is by Ticketmaster attraction ID first, then by exact
normalized name, then by event title. Tribute bands don't match their
headliners — "Coldplay Tribute" is not Coldplay.

---

## Setup (about 10 minutes, once)

### 1. Get a Ticketmaster API key — free, ~2 minutes

1. Go to **https://developer.ticketmaster.com/**
2. Click **Sign Up** (top right) and register — email, name, password. No
   payment, no approval wait.
3. Confirm your email, then sign in.
4. You land on **My Apps**. Click **Add a new app**.
   - App name: `onsale-radar` (anything works)
   - Description: `Personal onsale calendar`
   - Everything else can stay blank.
5. Click the app you just made. Copy the **Consumer Key** — that long
   alphanumeric string is your API key. (Ignore the Consumer Secret; the
   Discovery API doesn't use it.)

Limits on the free tier: 5,000 calls/day, 5/second. This uses about 10 calls
per run (~80/day), plus one 50-call artist re-resolve a week. Plenty of room.

### 2. Create the repo

1. Go to **https://github.com/new**
2. Name it `onsale-radar`. **Private** is fine and recommended.
3. Check **Add a README file** so the repo isn't empty, then **Create**.
4. Upload these files — click **Add file → Upload files**, drag the whole
   unzipped folder in, commit. Keep the structure:

```
   radar.py
   watchlist.json
   test_radar.py
   README.md
   .github/workflows/radar.yml
   docs/            (created on first run)
   state/           (created on first run)
```

### 3. Add your API key as a secret

**Settings → Secrets and variables → Actions → New repository secret**

- Name: `TM_API_KEY`
- Secret: the Consumer Key from step 1

### 4. Let the workflow write back to the repo

**Settings → Actions → General → Workflow permissions** →
select **Read and write permissions** → Save.

(The job commits the refreshed dashboard and its "already alerted" state.)

### 5. Turn on the dashboard

**Settings → Pages** → Source: **Deploy from a branch** →
Branch: `main`, folder: `/docs` → Save.

The URL appears on that page. It'll 404 until the first run finishes.

### 6. Run it once by hand

**Actions → Onsale Radar → Run workflow**.

The first run resolves all 50 artists to Ticketmaster IDs, sweeps the current
onsales, and **sends nothing** — it records what's already pending as a
baseline. From the second run on, an alert means something was genuinely just
announced.

Check the run log for lines marked `<-- fuzzy, verify`. Those are artists where
the name didn't match cleanly and it fell back to Ticketmaster's best guess.
Fix them by editing the `search` field in `watchlist.json`.

### 7. Phone push (optional, 2 minutes)

Two ways:

**Easiest** — install the **GitHub mobile app**, sign in, and enable
notifications. New alert issues push automatically. Email works out of the box
with no app at all.

**Better alerts** — use [ntfy.sh](https://ntfy.sh), free and no account:

1. Install the **ntfy** app (iOS/Android).
2. Subscribe to a topic — pick something unguessable, e.g.
   `brad-onsale-a7f3k9`. Anyone who knows the topic name can read it, so make
   it random.
3. Add it as a second repo secret named `NTFY_TOPIC` with that topic string.

---

## Tuning it

**The watchlist** — `watchlist.json`. Add or remove acts freely; edit the file
on GitHub and the next run picks it up and re-resolves IDs automatically.
`name` is what shows in alerts, `search` is what gets sent to Ticketmaster.

**How often it runs** — the `cron` line in `.github/workflows/radar.yml`.
`0 */3 * * *` is every 3 hours. `0 */1 * * *` is hourly if you want tighter
coverage on day-of reminders.

**Reminder lead time** — `SOON_HOURS` in `radar.py`, default 24.

**How far ahead it looks** — `HORIZON_DAYS`, default 180.

**Region** — `COUNTRY = "US"` in `radar.py`. Currently nationwide. To narrow it,
add `"stateCode": "TN"` (or `"dmaId"`) to the params in `sweep_onsales()`.

**What counts as "big"** — `BIG_VENUE`, `VERIFIED_FAN`, `BIG_PRICE` and
`NOTABLE_PRESALE` at the top of `radar.py`; see *Why "big" is defined the way
it is* above. Too much noise: raise `BIG_PRICE` or trim `BIG_VENUE`. Too
little: add venue words or loosen `NOTABLE_PRESALE`. `MAX_ALERT_ITEMS` caps how
many land in a single alert.

**Starting over** — delete `state/seen.json` and the next run treats itself as
a fresh baseline (silent), then alerts normally after that.

## Testing changes

```bash
python3 test_radar.py     # 21 checks, no API key or network needed
```

It fakes the API and verifies the things that actually break: tribute bands
getting matched instead of the real act, expired presales leaking through,
TBD onsale dates, the three artist-matching paths, big/small bucketing,
alert de-duplication across runs, and ID cache reuse.

To try a real scan locally without touching state:

```bash
export TM_API_KEY=...
python3 radar.py --dry-run
```

## How alerting decides what's worth your attention

Each buying window has an ID built from the event, type, presale name, and
start time. `state/seen.json` remembers two things per window: whether you've
been told it exists, and whether you've had the 24-hour reminder. So a given
onsale pings you twice — once when announced, once the day before — and never
again. Windows that have passed get dropped from state so the file stays small.

## Getting the tickets once you're alerted

The alert is the easy half. What actually matters on the day:

- **Presales beat onsales.** Most of the good inventory is gone before the
  public window. Artist fan clubs, Ticketmaster Verified Fan (register early,
  codes are capped), and card presales — Amex, Citi, Capital One, Chase — each
  open their own door. The dashboard lists these separately for that reason.
- **One tab.** Multiple tabs or windows can get you flagged and dumped from
  the queue.
- **Be logged in beforehand**, with payment and delivery details saved. The
  queue doesn't wait while you type a card number.
- **Join at the exact drop time**, not early — position in the queue is
  randomized among everyone waiting when it opens, so arriving 20 minutes
  early buys you nothing.
- **Resale sags late.** Verified resale prices often fall in the final 48 hours
  before an event if you're willing to hold your nerve.
