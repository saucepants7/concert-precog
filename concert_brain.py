#!/usr/bin/env python3
"""Concert discovery brain.

Reads your top artists from Last.fm (fed by your Spotify scrobbles), expands
them into similar artists you might like, then checks Ticketmaster AND SeatGeek
for any of those acts playing in North Carolina. Flags shows from artists you
already listen to and new artists it thinks you'd like, with the "why" attached.

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
STATE_CODE   = "NC"        # wider NC: Charlotte, Greensboro, Triangle, etc.
TOP_PERIOD   = "3month"    # Last.fm window: 7day|1month|3month|6month|12month|overall
TOP_N        = 25          # how many of your top artists to seed from
SIMILAR_PER  = 30          # similar artists pulled per seed
MAX_DISCOVER = 40          # recommended artists to check for shows
LASTFM_API   = "https://ws.audioscrobbler.com/2.0/"
TM_API       = "https://app.ticketmaster.com/discovery/v2/events.json"
SG_API       = "https://api.seatgeek.com/2/events"
DB_PATH   = os.path.join(os.path.dirname(__file__), "concerts.db")
HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")
UA = {"User-Agent": "concert-brain/1.0 (personal project)"}


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
        reason = "because you listen to " + " and ".join(x[0] for x in top)
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
                    "url": ev.get("url"), "source": "TM"})
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
                    "url": ev.get("url"), "source": "SG"})
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


# ---- dashboard --------------------------------------------------------------
def _chip(datestr):
    try:
        d = datetime.strptime(datestr, "%Y-%m-%d")
        return d.strftime("%b").upper(), str(d.day)
    except Exception:
        return "", ""


def build_dashboard(db):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def rows(tier):
        raw = db.execute(
            "SELECT artist,reason,name,date,venue,city,url FROM shows "
            "WHERE tier=? AND date>=? ORDER BY date", (tier, today)).fetchall()
        seen, out = set(), []
        for r in raw:
            k = ((r[0] or "").lower(), r[3])
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
        return out

    def section(items, discover):
        if not items:
            return ("<p class='empty'>Nothing on the calendar yet. "
                    "The brain checks daily as artists announce dates.</p>")
        cards = []
        for artist, reason, name, date, venue, city, url in items:
            mon, day = _chip(date)
            loc = html.escape(venue or "")
            if city:
                loc += f" &middot; {html.escape(city)}"
            why = (f"<div class='why'>{html.escape(reason or '')}</div>"
                   if discover and reason else "")
            cards.append(
                f"<a class='show' href='{html.escape(url or '#')}'>"
                f"<div class='stub'><span class='mon'>{mon}</span>"
                f"<span class='day'>{day}</span></div>"
                f"<div class='info'><div class='artist'>{html.escape(artist or '')}</div>"
                f"<div class='loc'>{loc}</div>{why}</div>"
                f"<span class='go'>Tickets &rarr;</span></a>")
        return "<div class='list'>" + "".join(cards) + "</div>"

    following, discover = rows("following"), rows("discover")
    updated = datetime.now(timezone.utc).strftime("%b %-d, %H:%M UTC") \
        if hasattr(datetime.now(), "strftime") else ""
    updated = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Marquee &mdash; NC concerts for your taste</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#14121c; --panel:#1c1928; --line:rgba(255,255,255,.08);
  --ink:#efecf6; --muted:#9b95ad; --amber:#ffc857; --stub:#221e30;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  line-height:1.45}}
.wrap{{max-width:760px;margin:0 auto;padding:2rem 1.1rem 4rem}}
header{{border-bottom:1px solid var(--line);padding-bottom:1.1rem;margin-bottom:1.6rem}}
h1{{font-family:'Bebas Neue',sans-serif;font-size:clamp(2.4rem,7vw,3.4rem);
  letter-spacing:.04em;margin:0;line-height:.95}}
h1 .amp{{color:var(--amber)}}
.tagline{{color:var(--muted);font-size:.86rem;margin-top:.5rem}}
.eyebrow{{font-family:'Bebas Neue',sans-serif;letter-spacing:.12em;
  font-size:1.15rem;color:var(--amber);margin:2rem 0 .7rem}}
.eyebrow span{{color:var(--muted);font-size:.75rem;letter-spacing:.04em}}
.list{{display:flex;flex-direction:column;gap:.55rem}}
.show{{display:flex;align-items:stretch;gap:.9rem;background:var(--panel);
  border:1px solid var(--line);border-radius:12px;padding:.7rem .9rem;
  text-decoration:none;color:inherit;transition:border-color .15s,transform .15s}}
.show:hover{{border-color:var(--amber);transform:translateY(-1px)}}
.show:focus-visible{{outline:2px solid var(--amber);outline-offset:2px}}
.stub{{flex:0 0 auto;width:52px;background:var(--stub);border-radius:8px;
  border-right:2px dashed var(--line);display:flex;flex-direction:column;
  align-items:center;justify-content:center;padding:.35rem 0}}
.stub .mon{{font-family:'Bebas Neue',sans-serif;font-size:.9rem;
  letter-spacing:.08em;color:var(--amber)}}
.stub .day{{font-family:'Bebas Neue',sans-serif;font-size:1.6rem;line-height:1}}
.info{{flex:1 1 auto;min-width:0;align-self:center}}
.artist{{font-weight:650;font-size:1.02rem}}
.loc{{color:var(--muted);font-size:.85rem;margin-top:.1rem}}
.why{{color:var(--amber);font-size:.78rem;margin-top:.25rem;opacity:.85}}
.go{{align-self:center;flex:0 0 auto;color:var(--muted);font-size:.8rem;white-space:nowrap}}
.show:hover .go{{color:var(--amber)}}
.empty{{color:var(--muted);font-size:.9rem;background:var(--panel);
  border:1px dashed var(--line);border-radius:12px;padding:1rem}}
footer{{color:var(--muted);font-size:.75rem;margin-top:2.5rem;
  border-top:1px solid var(--line);padding-top:1rem}}
@media(max-width:460px){{.go{{display:none}}}}
</style></head><body><div class="wrap">
<header>
<h1>MARQUEE</h1>
<div class="tagline">Live shows in North Carolina, tuned to what you actually listen to.</div>
</header>

<div class="eyebrow">In rotation <span>&mdash; artists you already play</span></div>
{section(following, False)}

<div class="eyebrow">On your radar <span>&mdash; you might like these</span></div>
{section(discover, True)}

<footer>Seeded from your Last.fm top artists, expanded through similar artists,
matched against Ticketmaster &amp; SeatGeek in NC. Updated {updated}.</footer>
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

    watch = [(s, "following", "in your rotation") for s in seeds]
    watch += [(r["name"], "discover", r["reason"]) for r in recs]

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

    # one alert per real show (a show on both sources collapses)
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
            tag = "" if tier == "following" else f" ({reason})"
            lines.append(f"{artist} - {s['date']} @ {s['venue']}, {s['city']}{tag}\n{s['url']}")
        notify(f"{len(new_shows)} new NC show(s) for you", "\n\n".join(lines))
    else:
        print("No new shows.")


if __name__ == "__main__":
    main()
