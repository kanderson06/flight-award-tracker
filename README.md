# Flight Award Release Tracker

Logs when Finnair (LAX-HEL) and Iberia (LAX-MAD) release nonstop business-class
award seats at the far edge of the booking calendar (~325-366 days out), to
discover each carrier's release day/time pattern.

Runs every 10 minutes via GitHub Actions, polling the seats.aero Partner API
(cached search). The API key lives in the `SEATSAERO_KEY` repository secret.

- `snapshots.jsonl` — every observation (route / date / source / per-cabin nonstop seat counts)
- `transitions.csv` — seat-count changes only; this is the release-pattern dataset
- `state.json` — last-known counts (internal bookkeeping)
- `errors.log` — polling errors, if any

Note: GitHub's cron scheduling has a few minutes of jitter, so "every 10
minutes" is approximate — fine for this purpose.
