#!/usr/bin/env python3
"""
Award-availability logger for LAX-HEL (Finnair) and LAX-MAD (Iberia).

Polls the seats.aero Partner API (cached search) for nonstop business-class
award space in the far-out booking window (~325-366 days ahead), and records:

  - snapshots.jsonl   : every observation, one JSON line per route/date/source per poll
  - transitions.csv   : only the *changes* (seat count went from X to Y) - the
                        release-pattern dataset
  - state.json        : last-known seat counts (internal bookkeeping)
  - errors.log        : any polling errors

Designed to be run every 10 minutes by launchd. Uses only the Python
standard library. API key is read from seatsaero-key.txt in this folder.
"""

import csv
import json
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(BASE_DIR, "seatsaero-key.txt")
SNAPSHOTS = os.path.join(BASE_DIR, "snapshots.jsonl")
TRANSITIONS = os.path.join(BASE_DIR, "transitions.csv")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
ERROR_LOG = os.path.join(BASE_DIR, "errors.log")

ROUTES = [("LAX", "HEL"), ("LAX", "MAD")]
WINDOW_START_DAYS = 325   # start of date window we watch (days from today)
WINDOW_END_DAYS = 366     # end of date window
CABINS = ["Y", "W", "J", "F"]
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
API = "https://seats.aero/partnerapi/search"


def log_error(msg):
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ERROR_LOG, "a") as f:
        f.write(f"{stamp} {msg}\n")


def fetch_route(key, origin, dest, start_date, end_date):
    """Fetch all cached-search records for one route, following pagination."""
    records = []
    cursor = None
    for _ in range(10):  # pagination safety cap
        params = {
            "origin_airport": origin,
            "destination_airport": dest,
            "start_date": start_date,
            "end_date": end_date,
            "take": "500",
        }
        if cursor is not None:
            params["cursor"] = str(cursor)
        url = API + "?" + urllib.parse.urlencode(params)
        # curl instead of urllib: macOS system Python lacks SSL root certs
        result = subprocess.run(
            ["curl", "-sS", "--fail", "--max-time", "60",
             "-H", f"Partner-Authorization: {key}",
             "-H", "Accept: application/json", url],
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(result.stdout)
        records.extend(payload.get("data", []))
        if not payload.get("hasMore"):
            break
        cursor = payload.get("cursor")
    return records


def slim(rec, polled_utc, polled_local):
    """Reduce an API record to the fields we care about (nonstop only)."""
    out = {
        "polled_at_utc": polled_utc,
        "polled_at_pacific": polled_local,
        "route": rec["Route"]["OriginAirport"] + "-" + rec["Route"]["DestinationAirport"],
        "date": rec["Date"],
        "source": rec["Source"],
        "source_updated_at": rec.get("UpdatedAt"),
        "source_created_at": rec.get("CreatedAt"),
    }
    for c in CABINS:
        out[f"{c}_direct_seats"] = rec.get(f"{c}DirectRemainingSeatsRaw", 0)
        out[f"{c}_direct_airlines"] = rec.get(f"{c}DirectAirlinesRaw", "")
        out[f"{c}_direct_miles"] = rec.get(f"{c}DirectMileageCostRaw", 0)
    return out


def main():
    # key comes from the SEATSAERO_KEY env var (GitHub Actions secret) if set,
    # otherwise from the local key file
    key = os.environ.get("SEATSAERO_KEY", "").strip()
    if not key:
        with open(KEY_FILE) as f:
            key = f.read().strip()

    now_utc = datetime.now(timezone.utc)
    polled_utc = now_utc.isoformat(timespec="seconds")
    polled_local = now_utc.astimezone(LOCAL_TZ).isoformat(timespec="seconds")
    today = now_utc.astimezone(LOCAL_TZ).date()
    start_date = (today + timedelta(days=WINDOW_START_DAYS)).isoformat()
    end_date = (today + timedelta(days=WINDOW_END_DAYS)).isoformat()

    # last-known counts: {"LAX-HEL|2027-08-05|qatar": {"J": 2, ...}}
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)

    observations = []
    for origin, dest in ROUTES:
        try:
            recs = fetch_route(key, origin, dest, start_date, end_date)
        except Exception as e:
            log_error(f"fetch failed {origin}-{dest}: {e!r}")
            continue
        for rec in recs:
            # only nonstop-relevant records; keep the record even if all
            # direct counts are zero so we can see space disappear
            observations.append(slim(rec, polled_utc, polled_local))

    if not observations:
        # nothing fetched at all (both routes failed, or genuinely no records)
        return

    with open(SNAPSHOTS, "a") as f:
        for obs in observations:
            f.write(json.dumps(obs) + "\n")

    # detect transitions (including first appearance and disappearance)
    new_state = dict(state)
    seen_keys = set()
    changes = []
    for obs in observations:
        k = f"{obs['route']}|{obs['date']}|{obs['source']}"
        seen_keys.add(k)
        prev = state.get(k, {})
        cur = {c: obs[f"{c}_direct_seats"] for c in CABINS}
        for c in CABINS:
            old = prev.get(c, "none")  # "none" = key never seen before
            if old != cur[c]:
                changes.append({
                    "polled_at_utc": polled_utc,
                    "polled_at_pacific": polled_local,
                    "route": obs["route"],
                    "date": obs["date"],
                    "source": obs["source"],
                    "cabin": c,
                    "old_seats": old,
                    "new_seats": cur[c],
                    "airlines": obs[f"{c}_direct_airlines"],
                    "source_updated_at": obs["source_updated_at"],
                })
        new_state[k] = cur

    # records that vanished from the feed entirely (date fell out of window
    # or seats.aero dropped the record) -> mark as gone once
    for k in list(new_state.keys()):
        if k not in seen_keys and state.get(k) is not None:
            route, date, source = k.split("|")
            for c in CABINS:
                old = state[k].get(c, 0)
                if old not in (0, "none"):
                    changes.append({
                        "polled_at_utc": polled_utc,
                        "polled_at_pacific": polled_local,
                        "route": route,
                        "date": date,
                        "source": source,
                        "cabin": c,
                        "old_seats": old,
                        "new_seats": "gone",
                        "airlines": "",
                        "source_updated_at": "",
                    })
            del new_state[k]

    if changes:
        file_exists = os.path.exists(TRANSITIONS)
        with open(TRANSITIONS, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(changes[0].keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerows(changes)

    with open(STATE_FILE, "w") as f:
        json.dump(new_state, f, indent=1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error(f"unhandled: {e!r}")
        sys.exit(1)
