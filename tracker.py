#!/usr/bin/env python3
"""
Weekly NFT paper-trading tracker.

Collects a fixed set of numbers for a frozen watchlist of collections and
appends one row per run to nft-experiment-log.json.

Design rules (these are the point of the script, not incidental):
  - Never fabricate. A missing value is written as null, never estimated,
    never carried forward from a previous week.
  - Never revise history. Append only.
  - Never trade. This script has no wallet keys and makes no write calls.

Usage:
    python tracker.py --probe    Dump raw API responses to probe_output.json
    python tracker.py            Normal weekly run
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- config

OPENSEA_BASE = "https://api.opensea.io/api/v2"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
ETH_RPC_ENDPOINTS = [
    "https://cloudflare-eth.com",
    "https://eth.llamarpc.com",
]

LOG_PATH = Path("nft-experiment-log.json")
CONFIG_PATH = Path("config.json")
PROBE_PATH = Path("probe_output.json")

REQUEST_TIMEOUT = 30
PAUSE_BETWEEN_CALLS = 0.5  # be polite; free tier allows 600/hour


# ---------------------------------------------------------------- helpers

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def dig(obj, *path, default=None):
    """Safely walk nested dicts/lists. Returns default on any miss.

    Deliberately silent: a missing field must produce None, not a crash and
    not a guess. The null propagates into the log where it is visible.
    """
    cur = obj
    for key in path:
        if cur is None:
            return default
        if isinstance(key, int):
            if not isinstance(cur, (list, tuple)) or len(cur) <= key:
                return default
            cur = cur[key]
        else:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(key)
    return cur if cur is not None else default


def find_interval(stats, name):
    """Pull one interval block (one_day / seven_day / thirty_day) from stats."""
    intervals = dig(stats, "intervals", default=[])
    if not isinstance(intervals, list):
        return {}
    for block in intervals:
        if isinstance(block, dict) and block.get("interval") == name:
            return block
    return {}


class OpenSea:
    def __init__(self, api_key):
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-KEY": api_key,
            "Accept": "application/json",
        })

    def get(self, path, params=None):
        """Returns (data, error_string). Never raises on HTTP failure."""
        url = f"{OPENSEA_BASE}{path}"
        try:
            r = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            return None, f"network_error: {e}"

        time.sleep(PAUSE_BETWEEN_CALLS)

        if r.status_code == 401:
            return None, "auth_failed: API key rejected"
        if r.status_code == 429:
            return None, "rate_limited"
        if r.status_code == 404:
            return None, "not_found"
        if r.status_code >= 400:
            return None, f"http_{r.status_code}"

        try:
            return r.json(), None
        except ValueError:
            return None, "invalid_json"


# ---------------------------------------------------------------- fetchers

def fetch_eth_usd():
    try:
        r = requests.get(
            COINGECKO_URL,
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return dig(r.json(), "ethereum", "usd"), None
    except Exception as e:
        return None, f"eth_price_failed: {e}"


def fetch_wallet_eth(address):
    """Read ETH balance straight from chain. No API key, no marketplace involved."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getBalance",
        "params": [address, "latest"],
    }
    for endpoint in ETH_RPC_ENDPOINTS:
        try:
            r = requests.post(endpoint, json=payload, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            hex_wei = dig(r.json(), "result")
            if hex_wei:
                return int(hex_wei, 16) / 1e18, None
        except Exception:
            continue
    return None, "all_rpc_endpoints_failed"


def fetch_collection(client, slug):
    """One collection, one row. Returns (row, list_of_errors)."""
    errors = []
    row = {"slug": slug}

    stats, err = client.get(f"/collections/{slug}/stats")
    if err:
        errors.append(f"{slug}/stats: {err}")
    else:
        total = dig(stats, "total", default={})
        row["floor_eth"] = dig(total, "floor_price")
        row["num_owners"] = dig(total, "num_owners")
        row["market_cap_eth"] = dig(total, "market_cap")

        one_day = find_interval(stats, "one_day")
        seven_day = find_interval(stats, "seven_day")
        row["sales_1d"] = dig(one_day, "sales")
        row["volume_1d_eth"] = dig(one_day, "volume")
        row["sales_7d"] = dig(seven_day, "sales")
        row["volume_7d_eth"] = dig(seven_day, "volume")

    meta, err = client.get(f"/collections/{slug}")
    if err:
        errors.append(f"{slug}/meta: {err}")
    else:
        row["total_supply"] = dig(meta, "total_supply")
        row["is_disabled"] = dig(meta, "is_disabled")
        row["safelist_status"] = dig(meta, "safelist_status")

    # Top collection-wide bid: what you could actually sell into today.
    offers, err = client.get(f"/offers/collection/{slug}")
    if err:
        errors.append(f"{slug}/offers: {err}")
    else:
        row["top_bid_eth"] = extract_top_bid(offers)

    return row, errors


def extract_top_bid(offers_payload):
    """Best collection offer in ETH, or None.

    Offer amounts come back as wei strings in the protocol data. Field layout
    has moved around across API versions, so try several shapes and give up
    quietly rather than returning a number we aren't sure about.
    """
    offers = dig(offers_payload, "offers", default=[])
    if not isinstance(offers, list):
        return None

    best = None
    for offer in offers:
        raw = (
            dig(offer, "price", "value")
            or dig(offer, "protocol_data", "parameters", "offer", 0, "startAmount")
            or dig(offer, "current", "value")
        )
        if raw is None:
            continue
        try:
            val = int(raw) / 1e18
        except (TypeError, ValueError):
            continue
        if val > 0 and (best is None or val > best):
            best = val
    return best


# ---------------------------------------------------------------- probe

def run_probe(client, slug):
    """Hit each endpoint once and dump raw structure, so field paths can be
    verified against reality instead of assumed."""
    log(f"Probing endpoints with slug: {slug}")
    out = {"probed_at": datetime.now(timezone.utc).isoformat(), "slug": slug}

    for name, path in [
        ("collection_stats", f"/collections/{slug}/stats"),
        ("collection_meta", f"/collections/{slug}"),
        ("collection_offers", f"/offers/collection/{slug}"),
    ]:
        data, err = client.get(path)
        out[name] = {"error": err} if err else data
        log(f"  {name}: {'FAILED - ' + err if err else 'ok'}")

    eth_usd, err = fetch_eth_usd()
    out["eth_usd"] = {"value": eth_usd, "error": err}
    log(f"  eth_price: {'FAILED - ' + err if err else eth_usd}")

    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    addr = cfg.get("wallet_address")
    if addr:
        bal, err = fetch_wallet_eth(addr)
        out["wallet_eth"] = {"value": bal, "error": err}
        log(f"  wallet_balance: {'FAILED - ' + err if err else bal}")

    PROBE_PATH.write_text(json.dumps(out, indent=2)[:200000])
    log(f"Wrote {PROBE_PATH}")


# ---------------------------------------------------------------- main run

def load_log():
    if not LOG_PATH.exists():
        return None
    try:
        return json.loads(LOG_PATH.read_text())
    except json.JSONDecodeError as e:
        log(f"FATAL: {LOG_PATH} is corrupt ({e}). Refusing to overwrite it.")
        sys.exit(1)


def weekly_run(client, cfg):
    now = datetime.now(timezone.utc)
    state = load_log()
    first_run = state is None

    if first_run:
        log("No existing log. This is run 1 (bootstrap).")
        state = {
            "experiment_start_date": now.date().isoformat(),
            "wallet_address": cfg["wallet_address"],
            "baseline": None,
            "watchlist": cfg["watchlist"],
            "weekly_observations": [],
        }

    eth_usd, price_err = fetch_eth_usd()
    wallet_eth, bal_err = fetch_wallet_eth(cfg["wallet_address"])
    errors = [e for e in (price_err, bal_err) if e]

    if first_run:
        if eth_usd is None or wallet_eth is None:
            log("FATAL: cannot establish baseline without price and balance.")
            log("Aborting so run 2 can retry a clean bootstrap.")
            sys.exit(1)
        state["baseline"] = {
            "eth_usd": eth_usd,
            "eth_held": wallet_eth,
            "portfolio_usd": round(wallet_eth * eth_usd, 2),
            "set_on": now.date().isoformat(),
        }
        log(f"Baseline frozen: {wallet_eth:.6f} ETH @ ${eth_usd}")

    baseline = state["baseline"]
    flags = []

    # Wallet drift. The baseline is never silently re-anchored: doing so would
    # erase the only comparison the experiment exists to make.
    if wallet_eth is not None and baseline:
        drift = wallet_eth - baseline["eth_held"]
        if abs(drift) > 1e-9:
            flags.append(
                f"WALLET DRIFT: holding {wallet_eth:.6f} ETH vs baseline "
                f"{baseline['eth_held']:.6f} ({drift:+.6f}). Baseline NOT reset — "
                f"benchmark comparisons are stale until you decide."
            )

    collections = []
    for entry in state["watchlist"]:
        slug = entry["slug"] if isinstance(entry, dict) else entry
        log(f"Fetching {slug}...")
        row, errs = fetch_collection(client, slug)
        errors.extend(errs)

        floor = row.get("floor_eth")
        bid = row.get("top_bid_eth")
        if floor and bid and floor > 0:
            row["spread_pct"] = round((floor - bid) / floor * 100, 2)
        else:
            row["spread_pct"] = None

        supply = row.get("total_supply")
        listed = row.get("listed_count")
        row["listed_ratio"] = round(listed / supply, 4) if listed and supply else None

        collections.append(row)

        if row.get("sales_7d") is not None and row["sales_7d"] < 10:
            flags.append(f"{slug}: only {row['sales_7d']} sales in 7d — floor price is now fiction")
        if row.get("spread_pct") is not None and row["spread_pct"] < 15:
            flags.append(f"{slug}: spread {row['spread_pct']}% — unusually liquid")
        if row.get("is_disabled"):
            flags.append(f"{slug}: collection disabled on OpenSea")

    # Week-over-week floor moves, compared against the previous row only.
    if state["weekly_observations"]:
        prev = {c["slug"]: c for c in state["weekly_observations"][-1].get("collections", [])}
        for row in collections:
            old = dig(prev, row["slug"], "floor_eth")
            new = row.get("floor_eth")
            if old and new and old > 0:
                change = (new - old) / old * 100
                if abs(change) > 25:
                    flags.append(f"{row['slug']}: floor moved {change:+.1f}% week-over-week")

    benchmark_usd = (
        round(baseline["eth_held"] * eth_usd, 2)
        if (baseline and eth_usd) else None
    )

    observation = {
        "date": now.date().isoformat(),
        "timestamp_utc": now.isoformat(),
        "week": len(state["weekly_observations"]) + 1,
        "eth_usd": eth_usd,
        "wallet_eth_held": wallet_eth,
        "eth_benchmark_usd": benchmark_usd,
        "collections": collections,
        "flags": flags,
        "errors": errors,
    }
    state["weekly_observations"].append(observation)

    LOG_PATH.write_text(json.dumps(state, indent=2))
    log(f"Wrote week {observation['week']} to {LOG_PATH}")

    print("\n" + "=" * 60)
    print(f"WEEK {observation['week']} — {observation['date']}")
    print("=" * 60)
    print(f"ETH: ${eth_usd}   Benchmark (hold): ${benchmark_usd}")
    print(f"Collections tracked: {len(collections)}")
    if flags:
        print(f"\nFLAGS ({len(flags)}):")
        for f in flags:
            print(f"  ! {f}")
    else:
        print("\nNo flags this week.")
    if errors:
        print(f"\nDATA GAPS ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
    print("=" * 60)

    # Non-zero exit surfaces a red run in the Actions tab, so silent decay
    # is visible without anyone needing to read the log.
    if len(errors) > len(collections):
        log("More errors than collections — exiting non-zero to flag this run.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", help="Dump raw API responses and exit")
    parser.add_argument("--probe-slug", default="pudgypenguins")
    args = parser.parse_args()

    api_key = os.environ.get("OPENSEA_API_KEY")
    if not api_key:
        log("FATAL: OPENSEA_API_KEY not set.")
        sys.exit(1)

    client = OpenSea(api_key)

    if args.probe:
        run_probe(client, args.probe_slug)
        return

    if not CONFIG_PATH.exists():
        log(f"FATAL: {CONFIG_PATH} not found.")
        sys.exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    if not cfg.get("watchlist"):
        log("FATAL: watchlist is empty. Pick collections before the first real run.")
        sys.exit(1)

    weekly_run(client, cfg)


if __name__ == "__main__":
    main()
