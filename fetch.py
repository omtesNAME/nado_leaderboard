import hashlib
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from pathlib import Path

import requests


API_URL = "https://archive.prod.nado.xyz/v1"
HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip",
}

API_MAX_PAGE_LIMIT = 500
PAGE_LIMIT = min(int(os.getenv("NADO_PAGE_LIMIT", str(API_MAX_PAGE_LIMIT))), API_MAX_PAGE_LIMIT)
REQUEST_DELAY = float(os.getenv("NADO_REQUEST_DELAY", "0.1"))
RECENT_KEY_LIMIT = int(os.getenv("NADO_RECENT_KEY_LIMIT", "50000"))
SEEN_PAGE_STOP_THRESHOLD = int(os.getenv("NADO_SEEN_PAGE_STOP_THRESHOLD", "2"))
MAX_INCREMENTAL_PAGES = int(os.getenv("NADO_MAX_INCREMENTAL_PAGES", "200"))
BOOTSTRAP_PAGE_BUDGET = int(os.getenv("NADO_BOOTSTRAP_PAGE_BUDGET", "0"))
BOOTSTRAP_MAX_SECONDS = int(os.getenv("NADO_BOOTSTRAP_MAX_SECONDS", "0"))
ASSUME_NEWEST_FIRST = os.getenv("NADO_ASSUME_NEWEST_FIRST", "1") != "0"

DATA_DIR = Path("data")
STATE_PATH = DATA_DIR / "pipeline_state.json"
LEADERBOARD_PATH = Path("leaderboard.json")

SCHEMA_VERSION = 2
SCALE = 10**18


def post(payload, retries=8):
    for attempt in range(retries):
        try:
            response = requests.post(API_URL, json=payload, headers=HEADERS, timeout=30)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after else min(60, 2 ** attempt)
                print(
                    f"Rate limited by Archive API; waiting {wait_seconds:.1f}s before retry...",
                    flush=True,
                )
                time.sleep(wait_seconds)
                continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Archive API returned a non-object JSON response")
            return data
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)


def empty_metric():
    return {"realized_pnl": 0, "volume": 0, "fees": 0, "matches": 0}


def empty_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "last_updated": None,
        "last_processed_at": None,
        "address_totals": {},
        "hourly_buckets": {},
        "daily_buckets": {},
        "recent_match_keys": [],
        "stats": {"total_matches_processed": 0},
    }


def load_state():
    if not STATE_PATH.exists():
        return empty_state()

    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except JSONDecodeError as exc:
        raise RuntimeError(
            f"State file {STATE_PATH} is not valid JSON; restore it or run "
            "NADO_FULL_REFRESH=1 after moving the corrupt file aside."
        ) from exc

    if state.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported state schema {state.get('schema_version')}; "
            "run with NADO_FULL_REFRESH=1 to rebuild state."
        )

    validate_state(state)
    return state


def validate_state(state):
    required_dicts = ("address_totals", "hourly_buckets", "daily_buckets", "stats")
    for key in required_dicts:
        if not isinstance(state.get(key), dict):
            raise RuntimeError(f"State field {key} must be an object")
    if not isinstance(state.get("recent_match_keys"), list):
        raise RuntimeError("State field recent_match_keys must be a list")


def save_state(state):
    DATA_DIR.mkdir(exist_ok=True)
    validate_state(state)
    tmp_path = STATE_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"), sort_keys=True)
    tmp_path.replace(STATE_PATH)


def is_explicit_full_refresh():
    return os.getenv("NADO_FULL_REFRESH") == "1"


def is_bootstrap_in_progress(state):
    stats = state.get("stats", {})
    return bool(stats.get("bootstrap_in_progress")) and not bool(stats.get("archive_complete"))


def remember_key(keys, key_set, key):
    if key in key_set:
        return
    keys.append(key)
    key_set.add(key)
    if len(keys) > RECENT_KEY_LIMIT:
        removed = keys.pop(0)
        key_set.discard(removed)


def process_archive(state, now, metrics):
    started_at = time.perf_counter()
    explicit_full_refresh = is_explicit_full_refresh()
    bootstrap_resume = is_bootstrap_in_progress(state) and not explicit_full_refresh
    bootstrap_mode = explicit_full_refresh or bootstrap_resume or not state["recent_match_keys"] or not ASSUME_NEWEST_FIRST
    previous_total = 0 if explicit_full_refresh else state["stats"].get("total_matches_processed", 0)
    target_state = empty_state() if explicit_full_refresh else state
    known_recent = set() if bootstrap_mode else set(state["recent_match_keys"])
    run_seen_keys = []
    run_seen = set()
    newest_keys = [] if explicit_full_refresh else list(target_state["recent_match_keys"])
    newest_seen = set(newest_keys)
    start = int(target_state["stats"].get("bootstrap_next_start", 0)) if bootstrap_resume else 0
    seen_pages = 0
    pages_scanned = 0

    if explicit_full_refresh:
        print("Full refresh: starting streamed bootstrap from page 0...", flush=True)
    elif bootstrap_resume:
        print(f"Resuming streamed bootstrap from start={start}...", flush=True)
    elif bootstrap_mode:
        print("Bootstrap required: streaming complete match history into aggregates...", flush=True)
    else:
        print("Incremental refresh: streaming newest pages until known matches...", flush=True)

    while True:
        data = post({"matches": {"limit": PAGE_LIMIT, "start": start}})
        items = data.get("matches", [])
        txs = data.get("txs", [])
        if not isinstance(items, list):
            raise ValueError("Archive API response field matches must be a list")
        if not items:
            break
        attach_tx_timestamps(items, txs)

        pages_scanned += 1
        page_seen = 0
        page_processed = 0

        for item in items:
            if not isinstance(item, dict):
                metrics["malformed_matches"] += 1
                continue

            key = match_key(item)
            if bootstrap_mode and len(newest_keys) < RECENT_KEY_LIMIT and key not in newest_seen:
                newest_keys.append(key)
                newest_seen.add(key)

            if not bootstrap_mode and key in known_recent:
                page_seen += 1
                continue
            if key in run_seen:
                metrics["duplicate_matches"] += 1
                continue

            remember_key(run_seen_keys, run_seen, key)
            if apply_match(target_state, item, now, metrics):
                page_processed += 1

        metrics["fetched_matches"] += len(items)
        metrics["candidate_matches"] += len(items) - page_seen
        metrics["known_matches_seen"] += page_seen

        if pages_scanned == 1 or metrics["fetched_matches"] % 5000 < PAGE_LIMIT:
            print(
                f"Scanned page {pages_scanned}, fetched {metrics['fetched_matches']} "
                f"matches, processed {metrics['processed_matches']}...",
                flush=True,
            )

        if not bootstrap_mode:
            if page_seen == len(items):
                seen_pages += 1
            else:
                seen_pages = 0
            if seen_pages >= SEEN_PAGE_STOP_THRESHOLD:
                metrics["overlap_reached"] = 1
                break
            if pages_scanned >= MAX_INCREMENTAL_PAGES:
                raise RuntimeError(
                    f"Reached NADO_MAX_INCREMENTAL_PAGES={MAX_INCREMENTAL_PAGES}; "
                    "known overlap was not reached, so the run is aborted to avoid "
                    "committing a partial incremental state. Increase the limit or "
                    "run with NADO_FULL_REFRESH=1."
                )

        tx_count = len(txs) if isinstance(txs, list) else len(items)
        if tx_count < PAGE_LIMIT:
            metrics["archive_complete"] = 1
            break

        start += PAGE_LIMIT
        page_budget_reached = BOOTSTRAP_PAGE_BUDGET and pages_scanned >= BOOTSTRAP_PAGE_BUDGET
        time_budget_reached = BOOTSTRAP_MAX_SECONDS and time.perf_counter() - started_at >= BOOTSTRAP_MAX_SECONDS
        if bootstrap_mode and (page_budget_reached or time_budget_reached):
            metrics["bootstrap_paused"] = 1
            reason = "page budget" if page_budget_reached else "time budget"
            print(
                f"Bootstrap {reason} reached at start={start}; saving checkpoint...",
                flush=True,
            )
            break
        time.sleep(REQUEST_DELAY)

    if bootstrap_mode:
        target_state["recent_match_keys"] = newest_keys[:RECENT_KEY_LIMIT]
    else:
        target_state["recent_match_keys"] = list(
            dict.fromkeys(run_seen_keys + target_state["recent_match_keys"])
        )[:RECENT_KEY_LIMIT]

    target_state["last_updated"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    target_state["last_processed_at"] = target_state["last_updated"]
    target_state["stats"]["total_matches_processed"] = previous_total + metrics["processed_matches"]
    target_state["stats"]["recent_matches_processed"] = metrics["processed_matches"]
    target_state["stats"]["recent_duplicates"] = metrics["duplicate_matches"]
    target_state["stats"]["recent_skipped"] = metrics["skipped_matches"]
    target_state["stats"]["archive_complete"] = bool(metrics["archive_complete"])
    target_state["stats"]["bootstrap_in_progress"] = bool(bootstrap_mode and not metrics["archive_complete"])
    target_state["stats"]["bootstrap_next_start"] = start if target_state["stats"]["bootstrap_in_progress"] else None
    target_state["stats"]["last_full_refresh"] = (
        target_state["last_updated"] if bootstrap_mode and metrics["archive_complete"] else target_state["stats"].get("last_full_refresh")
    )
    metrics["pages_scanned"] = pages_scanned
    metrics["full_refresh"] = bootstrap_mode
    metrics["bootstrap_resume"] = bootstrap_resume
    return target_state


def match_key(match):
    for field in (
        "digest",
        "id",
        "match_id",
        "matchId",
        "hash",
        "tx_hash",
        "transaction_hash",
        "transactionHash",
    ):
        value = match.get(field)
        if value:
            return f"{field}:{value}"

    encoded = json.dumps(match, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def attach_tx_timestamps(matches, txs):
    if not isinstance(txs, list):
        return

    timestamp_by_submission = {}
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        submission_idx = tx.get("submission_idx")
        timestamp = tx.get("timestamp")
        if submission_idx is not None and timestamp is not None:
            timestamp_by_submission[str(submission_idx)] = timestamp

    for match in matches:
        if not isinstance(match, dict) or match.get("timestamp"):
            continue
        submission_idx = match.get("submission_idx")
        if submission_idx is None:
            continue
        timestamp = timestamp_by_submission.get(str(submission_idx))
        if timestamp is not None:
            match["timestamp"] = timestamp


def match_timestamp(match):
    ts = match.get("timestamp", 0)
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return 0
    if ts > 10_000_000_000:
        ts = ts / 1000
    return int(ts)


def match_day(match):
    ts = match_timestamp(match)
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def match_hour(match):
    ts = match_timestamp(match)
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")


def match_address(match):
    try:
        sender = match["order"]["sender"]
    except (KeyError, TypeError):
        return None
    if not isinstance(sender, str) or len(sender) < 42:
        return None
    return sender[:42].lower()


def metric_from_match(match):
    return {
        "realized_pnl": parse_int(match.get("realized_pnl", 0)),
        "volume": abs(parse_int(match.get("quote_filled", 0))),
        "fees": parse_int(match.get("fee", 0)),
        "matches": 1,
    }


def parse_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def add_metric(target, delta):
    target["realized_pnl"] = target.get("realized_pnl", 0) + delta["realized_pnl"]
    target["volume"] = target.get("volume", 0) + delta["volume"]
    target["fees"] = target.get("fees", 0) + delta["fees"]
    target["matches"] = target.get("matches", 0) + delta["matches"]


def apply_match(state, match, now, metrics):
    address = match_address(match)
    if not address:
        metrics["skipped_matches"] += 1
        return False

    day = match_day(match)
    hour = match_hour(match)
    delta = metric_from_match(match)

    total = state["address_totals"].setdefault(address, empty_metric())
    add_metric(total, delta)

    hour_bucket = state.setdefault("hourly_buckets", {}).setdefault(hour, {})
    hourly_total = hour_bucket.setdefault(address, empty_metric())
    add_metric(hourly_total, delta)

    day_bucket = state["daily_buckets"].setdefault(day, {})
    daily_total = day_bucket.setdefault(address, empty_metric())
    add_metric(daily_total, delta)

    metrics["processed_matches"] += 1
    return True


def prune_daily_buckets(state, now, retention_days=45):
    cutoff = (now - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    state["daily_buckets"] = {
        day: bucket for day, bucket in state["daily_buckets"].items() if day >= cutoff
    }


def prune_hourly_buckets(state, now, retention_hours=35 * 24):
    cutoff = (now - timedelta(hours=retention_hours)).strftime("%Y-%m-%dT%H:00:00Z")
    state["hourly_buckets"] = {
        hour: bucket for hour, bucket in state.get("hourly_buckets", {}).items() if hour >= cutoff
    }


def compact_state(state):
    for key in ("address_totals",):
        state[key] = {
            address: metric
            for address, metric in state[key].items()
            if metric.get("matches", 0) > 0
            and (metric.get("volume", 0) != 0 or metric.get("realized_pnl", 0) != 0)
        }

    for bucket_key in ("hourly_buckets", "daily_buckets"):
        compacted = {}
        for bucket_name, bucket in state.get(bucket_key, {}).items():
            cleaned_bucket = {
                address: metric
                for address, metric in bucket.items()
                if metric.get("matches", 0) > 0
                and (metric.get("volume", 0) != 0 or metric.get("realized_pnl", 0) != 0)
            }
            if cleaned_bucket:
                compacted[bucket_name] = cleaned_bucket
        state[bucket_key] = compacted


def sum_hourly_buckets(state, hours, now):
    cutoff = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:00:00Z")
    result = defaultdict(empty_metric)

    for hour, bucket in state.get("hourly_buckets", {}).items():
        if hour < cutoff:
            continue
        for address, metric in bucket.items():
            add_metric(result[address], metric)

    return result


def shorten(address):
    return address[:6] + "..." + address[-4:]


def as_usd_units(raw_value):
    return round(raw_value / SCALE, 2)


def build_rows(metrics_by_address):
    rows = []
    for address, metric in metrics_by_address.items():
        pnl = as_usd_units(metric.get("realized_pnl", 0))
        volume = as_usd_units(metric.get("volume", 0))
        fees = as_usd_units(metric.get("fees", 0))
        if volume == 0 and pnl == 0:
            continue
        rows.append(
            {
                "address": address,
                "display_address": shorten(address),
                "realized_pnl": pnl,
                "volume": volume,
                "fees": fees,
                "matches": metric.get("matches", 0),
            }
        )

    rows.sort(key=lambda row: row["realized_pnl"], reverse=True)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def materialize_leaderboard(state, now):
    stats = state.get("stats", {})
    leaderboard = {
        "last_updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": {
            "address_count": len(state["address_totals"]),
            "total_matches_processed": stats.get("total_matches_processed", 0),
            "recent_matches_processed": stats.get("recent_matches_processed", 0),
            "archive_complete": bool(stats.get("archive_complete")),
            "bootstrap_in_progress": bool(stats.get("bootstrap_in_progress")),
            "bootstrap_next_start": stats.get("bootstrap_next_start"),
            "last_full_refresh": stats.get("last_full_refresh"),
        },
        "periods": {
            "1d": build_rows(sum_hourly_buckets(state, 24, now)),
            "1w": build_rows(sum_hourly_buckets(state, 7 * 24, now)),
            "1m": build_rows(sum_hourly_buckets(state, 30 * 24, now)),
            "all_time": build_rows(state["address_totals"]),
        },
    }

    tmp_path = LEADERBOARD_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(leaderboard, f, separators=(",", ":"))
    tmp_path.replace(LEADERBOARD_PATH)


def state_size_bytes():
    if not STATE_PATH.exists():
        return 0
    return STATE_PATH.stat().st_size


def new_metrics():
    return defaultdict(int)


def print_metrics(metrics, state, started_at):
    elapsed = time.perf_counter() - started_at
    state_bytes = state_size_bytes()
    print("Pipeline metrics:", flush=True)
    print(f"  full_refresh={bool(metrics['full_refresh'])}", flush=True)
    print(f"  bootstrap_resume={bool(metrics['bootstrap_resume'])}", flush=True)
    print(f"  bootstrap_paused={bool(metrics['bootstrap_paused'])}", flush=True)
    print(f"  pages_scanned={metrics['pages_scanned']}", flush=True)
    print(f"  fetched_matches={metrics['fetched_matches']}", flush=True)
    print(f"  candidate_matches={metrics['candidate_matches']}", flush=True)
    print(f"  processed_matches={metrics['processed_matches']}", flush=True)
    print(f"  duplicate_matches={metrics['duplicate_matches']}", flush=True)
    print(f"  known_matches_seen={metrics['known_matches_seen']}", flush=True)
    print(f"  overlap_reached={bool(metrics['overlap_reached'])}", flush=True)
    print(f"  skipped_matches={metrics['skipped_matches']}", flush=True)
    print(f"  malformed_matches={metrics['malformed_matches']}", flush=True)
    print(f"  fetch_seconds={metrics['fetch_seconds']:.2f}", flush=True)
    print(f"  aggregation_seconds={metrics['aggregation_seconds']:.2f}", flush=True)
    print(f"  total_seconds={elapsed:.2f}", flush=True)
    print(f"  address_count={len(state['address_totals'])}", flush=True)
    print(f"  hourly_bucket_count={len(state.get('hourly_buckets', {}))}", flush=True)
    print(f"  daily_bucket_count={len(state.get('daily_buckets', {}))}", flush=True)
    print(f"  recent_key_count={len(state['recent_match_keys'])}", flush=True)
    print(f"  bootstrap_next_start={state['stats'].get('bootstrap_next_start')}", flush=True)
    print(f"  state_size_bytes={state_bytes}", flush=True)


def main():
    started_at = time.perf_counter()
    now = datetime.now(timezone.utc)
    metrics = new_metrics()
    state = load_state()

    fetch_started_at = time.perf_counter()
    state = process_archive(state, now, metrics)
    metrics["fetch_seconds"] = time.perf_counter() - fetch_started_at

    aggregation_started_at = time.perf_counter()
    prune_hourly_buckets(state, now)
    prune_daily_buckets(state, now)
    compact_state(state)
    materialize_leaderboard(state, now)
    save_state(state)
    metrics["aggregation_seconds"] = time.perf_counter() - aggregation_started_at

    print(
        "Saved leaderboard.json and data/pipeline_state.json "
        f"({state['stats']['recent_matches_processed']} processed this run)",
        flush=True,
    )
    print_metrics(metrics, state, started_at)


if __name__ == "__main__":
    main()
