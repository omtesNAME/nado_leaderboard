import requests
import json
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

API_URL = "https://archive.prod.nado.xyz/v1"
HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip",
}
DELAY = 0.1


def post(payload, retries=3):
    for attempt in range(retries):
        try:
            r = requests.post(API_URL, json=payload, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                raise e


def fetch_all_matches():
    all_matches = []
    start = 0
    limit = 500
    while True:
        data = post({"matches": {"limit": limit, "start": start}})
        items = data.get("matches", [])
        all_matches.extend(items)
        fetched = len(all_matches)
        if fetched % 1000 < limit:
            print(f"Fetched {fetched} matches so far...", flush=True)
        if len(items) < limit:
            break
        start += limit
        time.sleep(DELAY)
    return all_matches


def shorten(addr):
    return addr[:6] + "..." + addr[-4:]


def group_by_address(all_matches):
    grouped = defaultdict(list)
    for m in all_matches:
        try:
            addr = m["order"]["sender"][:42].lower()
        except (KeyError, TypeError):
            continue
        grouped[addr].append(m)
    return grouped


def calc_metrics(matches, since_ts=None):
    realized_pnl = 0.0
    volume = 0.0
    fees = 0.0
    for m in matches:
        ts = m.get("timestamp", 0)
        if since_ts and ts < since_ts:
            continue
        realized_pnl += int(m.get("realized_pnl", 0)) / 1e18
        volume += abs(int(m.get("quote_filled", 0))) / 1e18
        fees += int(m.get("fee", 0)) / 1e18
    return realized_pnl, volume, fees


def build_period_rows(grouped, since_ts=None):
    rows = []
    for addr, matches in grouped.items():
        pnl, vol, fee = calc_metrics(matches, since_ts)
        if vol == 0 and pnl == 0:
            continue
        rows.append({
            "address": addr,
            "display_address": shorten(addr),
            "realized_pnl": round(pnl, 2),
            "volume": round(vol, 2),
            "fees": round(fee, 2),
        })
    rows.sort(key=lambda x: x["realized_pnl"], reverse=True)
    for i, row in enumerate(rows):
        row["rank"] = i + 1
    return rows


def main():
    now = datetime.now(timezone.utc)

    print("Fetching all matches...", flush=True)
    all_matches = fetch_all_matches()
    print(f"Done. Total matches: {len(all_matches)}", flush=True)

    print("Grouping by address...", flush=True)
    grouped = group_by_address(all_matches)
    print(f"Unique addresses: {len(grouped)}", flush=True)

    ts_1d = (now - timedelta(days=1)).timestamp()
    ts_1w = (now - timedelta(days=7)).timestamp()
    ts_1m = (now - timedelta(days=30)).timestamp()

    print("Building leaderboard periods...", flush=True)
    leaderboard = {
        "last_updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "periods": {
            "1d": build_period_rows(grouped, since_ts=ts_1d),
            "1w": build_period_rows(grouped, since_ts=ts_1w),
            "1m": build_period_rows(grouped, since_ts=ts_1m),
            "all_time": build_period_rows(grouped, since_ts=None),
        }
    }

    with open("leaderboard.json", "w") as f:
        json.dump(leaderboard, f, separators=(",", ":"))

    print("Saved leaderboard.json")


if __name__ == "__main__":
    main()
