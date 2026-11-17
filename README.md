# cinemacity-watchdog

Watches the Cinema City schedule and, when a new **Dune: Part Three** showtime appears in IMAX, opens an issue in this repo and assigns it to the repo owner. GitHub then sends an email and a push notification to the GitHub mobile app.

The assignment matters: by default GitHub only emails "Participating" notifications (assignments, mentions, replies). Merely watching a repo gives only web/mobile notifications — email for that is off in Settings → Notifications unless you turn it on.

It runs in GitHub Actions, so it works even when your computer is off.

Forked from [TarkDetrius/cinemacity-watchdog](https://github.com/TarkDetrius/cinemacity-watchdog) and translated to English.

## How it works

- The workflow `.github/workflows/watch.yml` checks about every 11 minutes. GitHub's own scheduler turned out to be too unreliable for this (its runs arrived hours late or not at all), so each run queues the next one itself: after the check it waits 10 minutes (a wait timer on the `ten-minute-wait` environment, which uses no runner time) and then starts the workflow again. An hourly cron line is kept only as a backup that restarts this chain if it ever breaks. The repo is public, so Actions minutes are free and unlimited.
- `watch.py` downloads the schedule from the public cinemacity.cz JSON API (`/cz/data-api-service/v1/quickbook/10101/…`) — no key, no login.
- The list of showtimes already seen is kept in `state/seen.json`, which the workflow commits back after each run. Only additions are reported.
- New showtimes → an issue with the time, hall, flags (70mm / subtitles / sold out) and a direct link to buy tickets (it opens seat selection). Showtimes that disappear from the schedule (cancelled screenings) are reported too.
- The issue is closed right after it's created. It's only a delivery channel for the notification, which GitHub sends when the issue is created — so the list of open issues stays empty and nothing needs cleaning up. The content stays readable under closed issues.
- Times are calculated in the cinema's time zone (Europe/Prague), not the runner's UTC. Without that, a screening that just ended would look like a future one and, once gone from the schedule, would be falsely reported as cancelled.

One run is about 45 HTTP requests and takes about 20 seconds.

## What exactly is watched

Showtimes where the film name contains `třetí` and the hall name contains `imax`. The API returns Czech titles, and Dune: Part Three is listed as *Duna: část třetí* ("third part"), so `třetí` matches Part Three only and skips re-runs of the earlier Dune films. Right now that means one cinema in the Czech Republic — Prague Flora, hall IMAX VOLVO, where Part Three plays in 70mm with subtitles.

To avoid downloading the full schedule of all thirteen cinemas, the search runs in two phases: first it finds which cinemas have an IMAX hall at all (one probe on the nearest showing day, plus a hint from the API via the `70-mm` attribute), and only those are checked in depth. If IMAX were added at another cinema, it would be picked up automatically.

The behaviour can be changed with environment variables in the workflow:

| Variable | Value here | Meaning |
|---|---|---|
| `FILM_PATTERN` | `třetí` | part of the film's (Czech) name, case-insensitive |
| `FILM_DISPLAY` | `Dune: Part Three` | film name used in issue titles |
| `AUDITORIUM_PATTERN` | `imax` | part of the hall's name |
| `HORIZON_DAYS` | `180` | how far ahead to look |
| `HINT_ATTR` | `70-mm` | attribute used to cheaply find candidate cinemas |
| `REQUEST_DELAY` | `0.25` | pause between API requests (seconds) |

To watch something else, change `FILM_PATTERN` (and `FILM_DISPLAY`, and `AUDITORIUM_PATTERN` if needed) in the workflow and clear `state/seen.json`.

## Running it by hand

Actions → Cinema City watchdog → Run workflow. Ticking **force_report** reports all current showtimes, including known ones — handy for checking it's alive, or as "show me what's on right now". A manual run also restarts the chain of checks if it has ever stopped.

## Maintenance

- **Actions quota:** the repo is public on purpose — Actions minutes are free and unlimited for public repos. If it were made private, runs would count against the free 2,000 minutes a month and checking this often would use them up; you'd then need a longer wait timer on the `ten-minute-wait` environment (Settings → Environments).
- **60-day pause:** GitHub automatically disables scheduled workflows after 60 days with no repo activity. That won't happen here — the workflow commits its own state.
- **When Dune's run ends,** the watchdog just stops reporting anything. Either turn it off (Actions → Cinema City watchdog → Disable workflow) or change `FILM_PATTERN` to another film.
- **If a check fails** (for example because the Cinema City API changes), the workflow opens one issue titled "Watchdog problem: a check failed", assigned to you, and closes it by itself once checks work again.
