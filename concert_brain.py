#!/usr/bin/env python3
"""Concert Precog — concert discovery brain.

Reads your top artists from Last.fm (fed by your Spotify scrobbles), expands
them into similar artists you might like, then checks Ticketmaster AND SeatGeek
for any of those acts playing in North Carolina. Shows everything on one
chronological calendar, color-coded by how each artist reached you.

Free + stdlib only (Last.fm + Ticketmaster + SeatGeek APIs). No pip installs.
"""
import html
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---- settings ---------------------------------------------------------------
STATE_CODE   = "NC"
TOP_PERIOD   = "3month"
TOP_N        = 50
SIMILAR_PER  = 30
MAX_DISCOVER = 50          # top 25 -> "top" (mustard), 26-50 -> "deep" (rust)
LASTFM_API   = "https://ws.audioscrobbler.com/2.0/"
TM_API       = "https://app.ticketmaster.com/discovery/v2/events.json"
SG_API       = "https://api.seatgeek.com/2/events"
DB_PATH   = os.path.join(os.path.dirname(__file__), "concerts.db")
HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")
UA = {"User-Agent": "concert-precog/1.0 (personal project)"}


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ---- Last.fm: seeds + similar -----------------------------------------------
def lastfm_top_artists(user, key):
    q = {"method": "user.gettopartists", "user": user, "period": TOP_PERIOD,
         "limit": str(TOP_N), "api_key": key, "format": "json"}
    data = get_json(LASTFM_API + "?" + urllib.parse.urlencode(q))
    return [a["name"] for a in data.get("topartists", {}).get("artist", []) if a.get("name")]


def lastfm_similar(artist, key):
    q = {"method": "artist.getsimilar", "artist": artist, "limit": str(SIMILAR_PER),
         "autocorrect": "1", "api_key": key, "format": "json"}
    try:
        data = get_json(LASTFM_API + "?" + urllib.parse.urlencode(q))
    except Exception as e:
        print(f"similar lookup failed for {artist}: {e}")
        return []
    return data.get("similarartists", {}).get("artist", []) or []


def build_recommendations(seeds, key):
    seed_set = {s.lower() for s in seeds}
    cand = {}
    for s in seeds:
        for a in lastfm_similar(s, key):
            name = a.get("name")
            if not name or name.lower() in seed_set:
                continue
            m = float(a.get("match") or 0)
            c = cand.setdefault(name, {"score": 0.0, "contrib": []})
            c["score"] += m
            c["contrib"].append((s, m))
        time.sleep(0.25)
    ranked = []
    for name, c in cand.items():
        top = sorted(c["contrib"], key=lambda x: -x[1])[:2]
        reason = "Because You Listen to " + " and ".join(x[0] for x in top)
        ranked.append({"name": name, "score": round(c["score"], 3), "reason": reason})
    ranked.sort(key=lambda x: -x["score"])
    return ranked[:MAX_DISCOVER]


# ---- concert sources: Ticketmaster + SeatGeek -------------------------------
def tm_shows_for(artist, key):
    q = {"apikey": key, "keyword": artist, "classificationName": "Music",
         "stateCode": STATE_CODE, "size": "50", "sort": "date,asc"}
    try:
        data = get_json(TM_API + "?" + urllib.parse.urlencode(q))
    except Exception as e:
        print(f"TM lookup failed for {artist}: {e}")
        return []
    al = artist.lower()
    out = []
    for ev in data.get("_embedded", {}).get("events", []):
        attractions = [a.get("name", "") for a in ev.get("_embedded", {}).get("attractions", [])]
        match = any(al == a.lower() for a in attractions) \
            or (not attractions and al in (ev.get("name") or "").lower())
        if not match:
            continue
        venues = ev.get("_embedded", {}).get("venues", [])
        v = venues[0] if venues else {}
        out.append({"event_id": ev["id"], "name": ev.get("name"),
                    "date": ev.get("dates", {}).get("start", {}).get("localDate"),
                    "venue": v.get("name"), "city": (v.get("city", {}) or {}).get("name"),
                    "url": ev.get("url")})
    return out


def sg_shows_for(artist, cid):
    if not cid:
        return []
    q = {"client_id": cid, "q": artist, "venue.state": STATE_CODE,
         "per_page": "50", "sort": "datetime_local.asc"}
    try:
        data = get_json(SG_API + "?" + urllib.parse.urlencode(q))
    except Exception as e:
        print(f"SeatGeek lookup failed for {artist}: {e}")
        return []
    al = artist.lower()
    out = []
    for ev in data.get("events", []):
        perfs = [p.get("name", "") for p in ev.get("performers", [])]
        v = ev.get("venue") or {}
        if not any(al == p.lower() for p in perfs):
            continue
        if (v.get("state") or "").upper() != STATE_CODE:
            continue
        out.append({"event_id": "sg-" + str(ev["id"]), "name": ev.get("title"),
                    "date": (ev.get("datetime_local") or "")[:10],
                    "venue": v.get("name"), "city": v.get("city"),
                    "url": ev.get("url")})
    return out


# ---- database ---------------------------------------------------------------
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS shows (
        event_id TEXT PRIMARY KEY, artist TEXT, tier TEXT, reason TEXT,
        name TEXT, date TEXT, venue TEXT, city TEXT, url TEXT, first_seen TEXT)""")
    return db


# ---- notify -----------------------------------------------------------------
def notify(subject, body):
    print(subject + "\n" + body)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}",
                                     data=body.encode(), headers={"Title": subject})
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print("ntfy failed:", e)


# ---- dashboard (Concert Precog — retro consolidated calendar) ---------------
def _chip(datestr):
    try:
        d = datetime.strptime(datestr, "%Y-%m-%d")
        return d.strftime("%b"), str(d.day)
    except Exception:
        return "", ""


def build_dashboard(db):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = db.execute(
        "SELECT artist,tier,reason,name,date,venue,city,url FROM shows "
        "WHERE date>=? ORDER BY date", (today,)).fetchall()
    seen, items = set(), []
    for r in raw:
        k = ((r[0] or "").lower(), r[4])          # (artist, date) — collapse dupes
        if k in seen:
            continue
        seen.add(k)
        items.append(r)

    cards = []
    for artist, tier, reason, name, date, venue, city, url in items:
        tier = tier if tier in ("rot", "top", "deep") else "rot"
        mon, day = _chip(date)
        loc = html.escape(venue or "")
        if city:
            loc += " &middot; " + html.escape(city)
        why = (f"<div class='why'>{html.escape(reason or '')}</div>"
               if tier in ("top", "deep") and reason else "")
        cards.append(
            f"<a class='row {tier}' href='{html.escape(url or '#')}'>"
            f"<div class='date'><span class='mon'>{mon}</span><span class='day'>{day}</span></div>"
            f"<div class='meta'><div class='title'>{html.escape(artist or '')}</div>"
            f"<div class='sub'>{loc}</div>{why}</div>"
            f"<span class='go'>&rsaquo;</span></a>")
    list_html = "".join(cards) or ("<p class='empty'>Nothing on the calendar yet — "
                                   "the brain checks daily as artists announce dates.</p>")
    updated = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="theme-color" content="#000000">
<title>Concert Precog</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=League+Spartan:wght@500;600;700;800&family=Poppins:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{
  color-scheme:dark;
  --bg:#000000; --cream:#e0e8c8; --muted:#8c907c;
  --teal:#568a99; --mustard:#e9a716; --rust:#c7481d; --ink:#0a0a0a;
  --hair:rgba(224,232,200,.12); --hover:rgba(224,232,200,.05);
}}
*{{box-sizing:border-box}}
html{{background:#000;min-height:100%}}
body{{margin:0;background:#000;color:var(--cream);
  font-family:'Poppins',system-ui,sans-serif;line-height:1.4;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:660px;margin:0 auto;padding:2.2rem 1.2rem 4rem}}

.brand{{text-align:center;margin-bottom:1.8rem}}
.mark{{display:block;width:380px;max-width:86%;margin:0 auto}}
.tagline{{margin:.7rem 0 0;font-family:'League Spartan',sans-serif;color:var(--cream);
  opacity:.85;font-size:.82rem;font-weight:600;text-transform:uppercase;letter-spacing:.14em}}

.legend{{display:flex;flex-wrap:wrap;gap:1.1rem;padding:.85rem 0;
  border-top:1px solid var(--hair);border-bottom:1px solid var(--hair);margin-bottom:1.5rem}}
.key{{display:flex;align-items:center;gap:.45rem;font-family:'League Spartan',sans-serif;
  font-size:.82rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:600}}
.sw{{width:13px;height:13px;border-radius:4px;flex:0 0 auto}}
.sw.rot{{background:var(--teal)}} .sw.top{{background:var(--mustard)}} .sw.deep{{background:var(--rust)}}

.list{{display:flex;flex-direction:column}}
.row{{display:flex;align-items:center;gap:.95rem;text-decoration:none;color:inherit;
  padding:.65rem .55rem;border-radius:9px;transition:background .15s}}
.row:hover{{background:var(--hover)}}
.row:focus-visible{{outline:2px solid var(--cream);outline-offset:-2px}}
.date{{flex:0 0 auto;width:52px;height:52px;border-radius:9px;color:var(--ink);background:#555;
  display:flex;flex-direction:column;align-items:center;justify-content:center}}
.date .mon{{font-family:'League Spartan',sans-serif;font-size:.64rem;font-weight:600;
  letter-spacing:.06em;text-transform:uppercase;opacity:.9}}
.date .day{{font-family:'League Spartan',sans-serif;font-size:1.5rem;font-weight:800;line-height:.95}}
.row.rot .date{{background:var(--teal)}}
.row.top .date{{background:var(--mustard)}}
.row.deep .date{{background:var(--rust)}}
.meta{{flex:1 1 auto;min-width:0}}
.title{{font-weight:500;font-size:1rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.sub{{color:var(--muted);font-size:.82rem;margin-top:.1rem;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.why{{font-size:.76rem;margin-top:.22rem;font-weight:500}}
.row.top .why{{color:var(--mustard)}} .row.deep .why{{color:var(--rust)}}
.go{{flex:0 0 auto;color:var(--muted);font-size:1.25rem;line-height:1;opacity:0;
  transform:translateX(-5px);transition:opacity .15s,transform .15s}}
.row:hover .go{{opacity:1;transform:translateX(0)}}
.empty{{color:var(--muted);font-size:.85rem;padding:.6rem .55rem}}
footer{{color:var(--muted);font-size:.72rem;margin-top:2.6rem;
  border-top:1px solid var(--hair);padding-top:1.1rem}}
@media(max-width:440px){{.sub{{white-space:normal}}.go{{display:none}}}}
</style></head><body><div class="wrap">

<div class="brand">
  <img class="mark" alt="Precog" src="precog-mark.png">
  <div class="tagline">Concerts in NC, tuned to your taste.</div>
</div>

<div class="legend">
  <div class="key"><span class="sw rot"></span>In rotation</div>
  <div class="key"><span class="sw top"></span>New &middot; top matches</div>
  <div class="key"><span class="sw deep"></span>New &middot; deeper cuts</div>
</div>

<div class="list">
{list_html}
</div>

<footer>One calendar, chronological. Tile color shows how each artist reached you.
Seeded from your Last.fm top artists, matched against Ticketmaster &amp; SeatGeek in NC. Updated {updated}.</footer>

</div></body></html>"""
    with open(HTML_PATH, "w") as f:
        f.write(doc)


# ---- main -------------------------------------------------------------------
def main():
    lastfm_key = os.environ["LASTFM_API_KEY"]
    lastfm_user = os.environ["LASTFM_USER"]
    tm_key = os.environ["TM_API_KEY"]
    sg_cid = os.environ.get("SEATGEEK_CLIENT_ID")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    seeds = lastfm_top_artists(lastfm_user, lastfm_key)
    manual = [a.strip() for a in os.environ.get("SEED_ARTISTS", "").split(",") if a.strip()]
    seeds_out, seen = [], {}
    for a in manual + seeds:
        k = a.lower()
        if k not in seen:
            seen[k] = len(seeds_out)
            seeds_out.append(a)
        elif seeds_out[seen[k]].islower() and not a.islower():
            seeds_out[seen[k]] = a
    seeds = seeds_out
    print(f"Seeds ({len(seeds)}): {', '.join(seeds[:8])}...")

    recs = build_recommendations(seeds, lastfm_key)
    print(f"Recommended {len(recs)} artists to check.")

    # tiers: rotation (teal) + discover top-25 (mustard) + discover 26-50 (rust)
    watch = [(s, "rot", "in your rotation") for s in seeds]
    for i, r in enumerate(recs):
        watch.append((r["name"], "top" if i < 25 else "deep", r["reason"]))

    db = get_db()
    existing = {r[0] for r in db.execute("SELECT event_id FROM shows").fetchall()}
    new_shows = []
    for artist, tier, reason in watch:
        found = tm_shows_for(artist, tm_key) + sg_shows_for(artist, sg_cid)
        for s in found:
            is_new = s["event_id"] not in existing
            db.execute(
                """INSERT INTO shows
                   (event_id,artist,tier,reason,name,date,venue,city,url,first_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET
                     artist=excluded.artist, tier=excluded.tier,
                     reason=excluded.reason, name=excluded.name, date=excluded.date,
                     venue=excluded.venue, city=excluded.city, url=excluded.url""",
                (s["event_id"], artist, tier, reason, s["name"], s["date"],
                 s["venue"], s["city"], s["url"], now))
            if is_new and s["date"]:
                new_shows.append((tier, artist, s, reason))
        time.sleep(0.2)
    db.commit()

    seen, deduped = set(), []
    for tier, artist, s, reason in new_shows:
        k = ((artist or "").lower(), s["date"])
        if k not in seen:
            seen.add(k)
            deduped.append((tier, artist, s, reason))
    new_shows = deduped

    build_dashboard(db)
    db.close()

    if new_shows:
        lines = []
        for tier, artist, s, reason in new_shows:
            tag = "" if tier == "rot" else f" ({reason})"
            lines.append(f"{artist} - {s['date']} @ {s['venue']}, {s['city']}{tag}\n{s['url']}")
        notify(f"{len(new_shows)} new NC show(s) for you", "\n\n".join(lines))
    else:
        print("No new shows.")


if __name__ == "__main__":
    main()
