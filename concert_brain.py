#!/usr/bin/env python3
"""Concert discovery brain.

Reads your top artists from Last.fm (fed by your Spotify scrobbles), expands
them into similar artists you might like, then checks Ticketmaster for any of
those acts playing in North Carolina. Flags shows from artists you already
listen to AND new artists it thinks you'd like, with the "why" attached.

Free + stdlib only (Last.fm API + Ticketmaster API). No pip installs.
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
TOP_PERIOD   = "3month"    # Last.fm window for "top artists": 7day|1month|3month|6month|12month|overall
TOP_N        = 25          # how many of your top artists to seed from
SIMILAR_PER  = 30          # similar artists pulled per seed
MAX_DISCOVER = 40          # recommended artists to check for shows
LASTFM_API   = "https://ws.audioscrobbler.com/2.0/"
TM_API       = "https://app.ticketmaster.com/discovery/v2/events.json"
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
    arts = data.get("topartists", {}).get("artist", [])
    return [a["name"] for a in arts if a.get("name")]


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
    """Rank artists similar to the seeds; reason = which seeds drove them."""
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
        time.sleep(0.25)                       # be polite to Last.fm
    ranked = []
    for name, c in cand.items():
        top = sorted(c["contrib"], key=lambda x: -x[1])[:2]
        reason = "because you listen to " + " and ".join(x[0] for x in top)
        ranked.append({"name": name, "score": round(c["score"], 3), "reason": reason})
    ranked.sort(key=lambda x: -x["score"])
    return ranked[:MAX_DISCOVER]


# ---- Ticketmaster: NC shows for an artist -----------------------------------
def tm_shows_for(artist, key):
    q = {"apikey": key, "keyword": artist, "classificationName": "Music",
         "stateCode": STATE_CODE, "size": "50", "sort": "date,asc"}
    try:
        data = get_json(TM_API + "?" + urllib.parse.urlencode(q))
    except Exception as e:
        print(f"TM lookup failed for {artist}: {e}")
        return []
    events = data.get("_embedded", {}).get("events", [])
    al = artist.lower()
    out = []
    for ev in events:
        attractions = [a.get("name", "") for a in
                       ev.get("_embedded", {}).get("attractions", [])]
        # confirm it's really this artist, not a keyword coincidence
        match = any(al == a.lower() for a in attractions) \
            or (not attractions and al in (ev.get("name") or "").lower())
        if not match:
            continue
        venues = ev.get("_embedded", {}).get("venues", [])
        v = venues[0] if venues else {}
        out.append({
            "event_id": ev["id"],
            "name": ev.get("name"),
            "date": ev.get("dates", {}).get("start", {}).get("localDate"),
            "venue": v.get("name"),
            "city": (v.get("city", {}) or {}).get("name"),
            "url": ev.get("url"),
        })
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
def build_dashboard(db):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def rows(tier):
        raw = db.execute(
            "SELECT artist,reason,name,date,venue,city,url FROM shows "
            "WHERE tier=? AND date>=? ORDER BY date", (tier, today)).fetchall()
        seen, out = set(), []                      # collapse same show listed twice
        for r in raw:
            k = ((r[0] or "").lower(), r[3])       # (artist, date)
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
        return out

    def table(items, discover):
        if not items:
            return "<p style='color:#888'>Nothing upcoming right now.</p>"
        trs = []
        for artist, reason, name, date, venue, city, url in items:
            why = (f"<td style='color:#666;font-size:.85rem'>"
                   f"{html.escape(reason or '')}</td>") if discover else ""
            loc = html.escape(venue or "")
            if city:
                loc += f" <span style='color:#888'>({html.escape(city)})</span>"
            trs.append(
                f"<tr><td><b>{html.escape(artist or '')}</b></td>"
                f"<td>{html.escape(date or '')}</td><td>{loc}</td>{why}"
                f"<td><a href='{html.escape(url or '#')}'>tickets</a></td></tr>")
        head = ("<th>Artist</th><th>Date</th><th>Venue</th>"
                + ("<th>Why</th>" if discover else "") + "<th></th>")
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(trs)}</tbody></table>"

    following, discover = rows("following"), rows("discover")
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Concert Brain</title><style>
body{{font-family:system-ui,sans-serif;margin:1rem;color:#111;max-width:820px}}
h1{{font-size:1.25rem}} h2{{font-size:1rem;margin-top:1.6rem}}
.sub{{color:#666;font-size:.8rem;margin-bottom:1rem}}
table{{border-collapse:collapse;width:100%}}
th,td{{padding:.45rem .6rem;border-bottom:1px solid #eee;text-align:left;font-size:.9rem;vertical-align:top}}
th{{background:#fafafa}} a{{color:#1d8cf8;text-decoration:none}}</style></head><body>
<h1>Concert Brain - NC shows for your taste</h1>
<div class="sub">Seeded from your Last.fm top artists; expanded via similar artists; matched against Ticketmaster in NC. Updated {updated}.</div>
<h2>Artists you listen to</h2>
{table(following, False)}
<h2>You might like</h2>
{table(discover, True)}
</body></html>"""
    with open(HTML_PATH, "w") as f:
        f.write(doc)


# ---- main -------------------------------------------------------------------
def main():
    lastfm_key = os.environ["LASTFM_API_KEY"]
    lastfm_user = os.environ["LASTFM_USER"]
    tm_key = os.environ["TM_API_KEY"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    seeds = lastfm_top_artists(lastfm_user, lastfm_key)
    # cold-start / booster: optional comma-separated SEED_ARTISTS env
    manual = [a.strip() for a in os.environ.get("SEED_ARTISTS", "").split(",") if a.strip()]
    seeds_out, seen = [], {}                       # dedupe case-insensitively
    for a in manual + seeds:
        k = a.lower()
        if k not in seen:
            seen[k] = len(seeds_out)
            seeds_out.append(a)
        elif seeds_out[seen[k]].islower() and not a.islower():
            seeds_out[seen[k]] = a                  # prefer proper capitalization
    seeds = seeds_out
    print(f"Seeds ({len(seeds)}): {', '.join(seeds[:8])}...")
    recs = build_recommendations(seeds, lastfm_key)
    print(f"Recommended {len(recs)} artists to check.")

    # artist -> (tier, reason)
    watch = [(s, "following", "in your rotation") for s in seeds]
    watch += [(r["name"], "discover", r["reason"]) for r in recs]

    db = get_db()
    existing = {r[0] for r in db.execute("SELECT event_id FROM shows").fetchall()}
    new_shows = []
    for artist, tier, reason in watch:
        for s in tm_shows_for(artist, tm_key):
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
        time.sleep(0.2)                        # stay under TM rate limit
    db.commit()
    build_dashboard(db)
    db.close()

    if new_shows:
        seen, deduped = set(), []                  # one ping per show
        for tier, artist, s, reason in new_shows:
            k = ((artist or "").lower(), s["date"])
            if k not in seen:
                seen.add(k)
                deduped.append((tier, artist, s, reason))
        new_shows = deduped
        lines = []
        for tier, artist, s, reason in new_shows:
            tag = "" if tier == "following" else f" ({reason})"
            lines.append(f"{artist} - {s['date']} @ {s['venue']}, {s['city']}{tag}\n{s['url']}")
        notify(f"{len(new_shows)} new NC show(s) for you", "\n\n".join(lines))
    else:
        print("No new shows.")


if __name__ == "__main__":
    main()
