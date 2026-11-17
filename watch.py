#!/usr/bin/env python3
"""Watches the Cinema City schedule and reports newly listed showtimes.

By default: the film "Duna" in a hall whose name contains "IMAX".
Data comes from the public cinemacity.cz JSON API (no key, no login).

State (showtimes already seen) is kept in a JSON file, so each run
reports only what was added since last time.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SITE_ID = "10101"  # cinemacity.cz
BASE = f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{SITE_ID}"
LANG = "cs_CZ"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FILM_PATTERN = os.environ.get("FILM_PATTERN", "duna").lower()
AUDITORIUM_PATTERN = os.environ.get("AUDITORIUM_PATTERN", "imax").lower()
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "180"))
# Attribute the API can filter cinemas by — a cheap hint where to look for
# IMAX halls. Complements (doesn't replace) the probe by hall name.
HINT_ATTR = os.environ.get("HINT_ATTR", "70-mm")
DELAY = float(os.environ.get("REQUEST_DELAY", "0.25"))

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# Film name shown in issue titles. The API returns Czech titles (which the
# filter matches), so this lets the alerts use the English name instead.
FILM_DISPLAY = os.environ.get("FILM_DISPLAY", "")

# The API returns eventDateTime without a zone, in the cinema's local time. The GitHub
# Actions runner uses UTC, so showtimes would be compared against a clock one or two
# hours behind — a screening that just ended would look like a future one
# and, once gone from the schedule, would be falsely reported as cancelled.
CINEMA_TZ = ZoneInfo("Europe/Prague")


def now():
    """Current time in the cinema's zone, without tzinfo — comparable with API data."""
    return datetime.now(CINEMA_TZ).replace(tzinfo=None)


def api(path):
    """GET on data-api-service; returns the contents of the "body" key."""
    url = f"{BASE}{path}"
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))["body"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise SystemExit(f"API failed after 4 attempts: {url}\n{last}")


def horizon():
    return (date.today() + timedelta(days=HORIZON_DAYS)).isoformat()


def fetch_cinemas():
    body = api(f"/cinemas/with-event/until/{horizon()}?attr=&lang={LANG}")
    return {c["id"]: c for c in body["cinemas"]}


def fetch_dates(cinema_id):
    time.sleep(DELAY)
    return api(f"/dates/in-cinema/{cinema_id}/until/{horizon()}?attr=&lang={LANG}")["dates"]


def fetch_day(cinema_id, day):
    time.sleep(DELAY)
    body = api(f"/film-events/in-cinema/{cinema_id}/at-date/{day}?attr=&lang={LANG}")
    films = {f["id"]: f for f in body.get("films", [])}
    return films, body.get("events", [])


def hint_cinema_ids():
    """Cinemas that, per the API, have showtimes with the HINT_ATTR attribute."""
    if not HINT_ATTR:
        return set()
    body = api(f"/cinemas/with-event/until/{horizon()}?attr={HINT_ATTR}&lang={LANG}")
    return {c["id"] for c in body["cinemas"]}


def is_target_hall(event):
    return AUDITORIUM_PATTERN in (event.get("auditorium") or "").lower()


def collect():
    """Walks the relevant cinemas and returns {event_id: record} for watched showtimes.

    To avoid pulling every cinema's full schedule, it runs in two phases: first it
    finds which cinemas have the watched hall at all (one probe per cinema + a hint
    from the API), and only those are walked in depth.
    """
    cinemas = fetch_cinemas()
    dates_by_cinema = {cid: fetch_dates(cid) for cid in cinemas}

    candidates = hint_cinema_ids() & set(cinemas)
    day_cache = {}
    for cid, days in dates_by_cinema.items():
        if not days:
            continue
        probe = days[0]
        day_cache[(cid, probe)] = fetch_day(cid, probe)
        if any(is_target_hall(e) for e in day_cache[(cid, probe)][1]):
            candidates.add(cid)

    found = {}
    for cid in sorted(candidates):
        for day in dates_by_cinema.get(cid, []):
            films, events = day_cache.get((cid, day)) or fetch_day(cid, day)
            for e in events:
                film = films.get(e["filmId"], {})
                if FILM_PATTERN not in film.get("name", "").lower():
                    continue
                if not is_target_hall(e):
                    continue
                found[e["id"]] = {
                    "id": e["id"],
                    "film": film.get("name", e["filmId"]),
                    "filmLink": film.get("link"),
                    "cinema": cinemas[cid]["displayName"],
                    "cinemaId": cid,
                    "datetime": e["eventDateTime"],
                    "auditorium": e.get("auditorium"),
                    "attrs": e.get("attributeIds", []),
                    # None of the fields the API offers works as a
                    # link: bookingLink returns 404 on GET, obsoleteBookingUrl
                    # is dead (as the name says) and bookingRouterLaunchLink leads to
                    # a page with a self-submitting POST form whose target
                    # (tickets.rel.…) answers a direct GET with 403. That POST, though,
                    # ends on the plain /order/{id} URL, which also works on
                    # GET and opens seat selection directly. Careful: the lang parameter
                    # causes a 404 here — it must be left out.
                    "booking": f"https://tickets.cinemacity.cz/order/{e.get('presentationCode') or e['id']}",
                    "soldOut": bool(e.get("soldOut")),
                }
    return found


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"updated": None, "events": {}}


def save_state(path, events):
    """Writes the state, but only when the set of showtimes changed.

    If the file were rewritten on every run, its "updated" stamp would change
    and the workflow would commit an empty change dozens of times a day.
    So the list of IDs decides — exactly what the reports are based on.
    Volatile fields (soldOut) are therefore not updated; the value is kept
    from when the showtime first appeared, which is also what gets reported.
    """
    if set(events) == set(load_state(path).get("events", {})):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "updated": now().replace(microsecond=0).isoformat(),
        "events": dict(sorted(events.items(), key=lambda kv: kv[1]["datetime"])),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    return True


def prune_past(events):
    """Drops past showtimes from the state so the file doesn't keep growing."""
    cutoff = (now() - timedelta(days=1)).isoformat()
    return {k: v for k, v in events.items() if v["datetime"] >= cutoff}


def fmt_dt(iso):
    dt = datetime.fromisoformat(iso)
    return f"{DAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]} {dt.year} at {dt:%H:%M}"


def fmt_short(iso):
    dt = datetime.fromisoformat(iso)
    return f"{dt.day} {MONTHS[dt.month - 1]}"


def render(new_events, gone_events):
    """Markdown body of the report."""
    lines = []
    if new_events:
        lines.append(f"### Newly listed ({len(new_events)})\n")
        for cinema, group in group_by_cinema(new_events):
            lines.append(f"**{cinema}**\n")
            for e in group:
                flags = []
                if "70-mm" in e["attrs"]:
                    flags.append("70mm")
                if "subbed" in e["attrs"]:
                    flags.append("subtitles")
                if "dubbed" in e["attrs"]:
                    flags.append("dubbed")
                if e["soldOut"]:
                    flags.append("**sold out**")
                suffix = f" — {', '.join(flags)}" if flags else ""
                link = f" — [buy tickets]({e['booking']})" if e["booking"] else ""
                lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}{suffix}{link}")
            lines.append("")
    if gone_events:
        lines.append(f"### Removed from the schedule ({len(gone_events)})\n")
        for cinema, group in group_by_cinema(gone_events):
            lines.append(f"**{cinema}**\n")
            for e in group:
                lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}")
            lines.append("")
    film_link = next(
        (e["filmLink"] for e in list(new_events) + list(gone_events) if e.get("filmLink")),
        None,
    )
    if film_link:
        lines.append(f"[Film page on Cinema City]({film_link})")
    lines.append("")
    lines.append(
        f"<sub>Checked {now():%d %b %Y %H:%M} (Prague time) · "
        f"film ~ `{FILM_PATTERN}` · hall ~ `{AUDITORIUM_PATTERN}`</sub>"
    )
    return "\n".join(lines)


def group_by_cinema(events):
    order = {}
    for e in sorted(events, key=lambda x: (x["cinema"], x["datetime"])):
        order.setdefault(e["cinema"], []).append(e)
    return order.items()


def title_for(new_events):
    film = FILM_DISPLAY or new_events[0]["film"]
    days = sorted({e["datetime"][:10] for e in new_events})
    span = fmt_short(days[0])
    if len(days) > 1:
        span += f"–{fmt_short(days[-1])}"
    n = len(new_events)
    word = "new showtime" if n == 1 else "new showtimes"
    return f"🎬 {film} in IMAX: {n} {word} ({span})"


def gh_output(**kwargs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            fh.write(f"{key}={value}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="state/seen.json", help="state file")
    ap.add_argument("--seed", action="store_true", help="only save the state, report nothing")
    ap.add_argument("--force-report", action="store_true", help="report everything, including known showtimes")
    ap.add_argument("--report", default="report.md", help="where to write the markdown report")
    ap.add_argument("--title", default="title.txt", help="where to write the issue title")
    args = ap.parse_args()

    current = collect()
    state = load_state(args.state)
    known = state.get("events", {})

    print(f"Found {len(current)} watched showtimes, {len(known)} in state.")

    if args.seed:
        save_state(args.state, prune_past(current))
        print(f"State written to {args.state} (seed, nothing reported).")
        gh_output(has_news="false")
        return

    if args.force_report:
        new_events = sorted(current.values(), key=lambda e: e["datetime"])
        gone = []
    else:
        new_events = sorted(
            (v for k, v in current.items() if k not in known),
            key=lambda e: e["datetime"],
        )
        future = now().isoformat()
        gone = sorted(
            (v for k, v in known.items() if k not in current and v["datetime"] > future),
            key=lambda e: e["datetime"],
        )

    save_state(args.state, prune_past(current))

    if not new_events and not gone:
        print("Nothing new.")
        gh_output(has_news="false")
        return

    body = render(new_events, gone)
    if new_events:
        title = title_for(new_events)
    else:
        film = FILM_DISPLAY or gone[0]["film"]
        title = f"🎬 {film} in IMAX: cancelled showtimes"
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    with open(args.title, "w", encoding="utf-8") as fh:
        fh.write(title + "\n")

    print(f"\n{title}\n")
    print(body)
    gh_output(has_news="true")


if __name__ == "__main__":
    sys.exit(main())
