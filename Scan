#!/usr/bin/env python3
"""
Weekly NFT market scan with a living watchlist.

Three stages, cheapest first:
  1. Page the market for metadata (few requests, kills most of it)
  2. Stats per survivor: floor, volume, owners
  3. Offers per survivor: top bid, spread, bid depth

Watchlist members always get all three stages regardless of whether they would
qualify today. That is the point of a watchlist: it keeps measuring things
after they stop looking good, which is exactly when the measurement matters.

Nothing here decides anything. It gathers numbers and records what changed.
Judgement happens downstream, with a human or a model reading these files.

Usage:
    python scan.py            Weekly scan
    python scan.py --probe    Check endpoints and exit
    python scan.py --dry-run  Scan a small pool, write nothing
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, date
from pathlib import Path

import requests

OPENSEA = "https://api.opensea.io/api/v2"
COINGECKO = "https://api.coingecko.com/api/v3/simple/price"
RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.drpc.org",
    "https://eth.llamarpc.com",
    "https://1rpc.io/eth",
]

CONFIG = Path("config.json")
WATCHLIST = Path("watchlist.json")
HISTORY = Path("history.json")
LATEST = Path("latest-scan.json")

TIMEOUT = 30
PAUSE = 0.35


def log(m):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {m}", flush=True)


def dig(o, *path, default=None):
    cur = o
    for k in path:
        if cur is None:
            return default
        if isinstance(k, int):
            if not isinstance(cur, (list, tuple)) or len(cur) <= k:
                return default
            cur = cur[k]
        else:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
    return cur if cur is not None else default


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


class OpenSeaClient:
    def __init__(self, key):
        self.s = requests.Session()
        self.s.headers.update({"X-API-KEY": key, "Accept": "application/json"})
        self.calls = 0

    def get(self, path, params=None):
        try:
            r = self.s.get(f"{OPENSEA}{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            return None, f"network: {type(e).__name__}"
        self.calls += 1
        time.sleep(PAUSE)
        if r.status_code == 401:
            return None, "auth_failed"
        if r.status_code == 429:
            return None, "rate_limited"
        if r.status_code >= 400:
            return None, f"http_{r.status_code}"
        try:
            return r.json(), None
        except ValueError:
            return None, "invalid_json"


# ------------------------------------------------------------------ market data

def eth_usd():
    try:
        r = requests.get(COINGECKO, params={"ids": "ethereum", "vs_currencies": "usd"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        return dig(r.json(), "ethereum", "usd"), None
    except Exception as e:
        return None, f"eth_price: {type(e).__name__}"


def wallet_eth(addr):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
               "params": [addr, "latest"]}
    fails = []
    for ep in RPCS:
        try:
            r = requests.post(ep, json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            h = dig(r.json(), "result")
            if h:
                return int(h, 16) / 1e18, None
        except Exception as e:
            fails.append(f"{ep.split('//')[1]}: {type(e).__name__}")
    return None, "rpc_failed (" + "; ".join(fails) + ")"


def bid_profile(payload):
    """Top per-item bid, plus how much support sits near it.

    price.value is the total for the whole offer; bulk offers are common
    (0.088 WETH for 80 items is 0.0011 each). Dividing by remaining_quantity
    is mandatory — without it the top bid can exceed the floor.

    depth_near_top counts items bid for within 10% of the best price. A top bid
    with nothing behind it means one buyer, and if they walk there is no market.
    """
    offers = dig(payload, "offers", default=[])
    if not isinstance(offers, list) or not offers:
        return None, 0, 0

    priced = []
    for o in offers:
        raw = dig(o, "price", "value")
        if raw is None:
            continue
        try:
            total = int(raw) / 1e18
            qty = int(o.get("remaining_quantity") or 1)
        except (TypeError, ValueError):
            continue
        if qty < 1 or total <= 0:
            continue
        priced.append((total / qty, qty))

    if not priced:
        return None, 0, 0

    priced.sort(key=lambda x: -x[0])
    top = priced[0][0]
    depth = sum(q for per, q in priced if per >= top * 0.90)
    return top, depth, len(priced)


# ------------------------------------------------------------------ stages

def stage1_pool(client, cfg):
    """Page collection metadata. Cheap: a few requests for hundreds of names."""
    pool, errors, cursor = {}, [], None
    target = cfg["scan"]["pool_size"]
    while len(pool) < target:
        params = {"chain": "ethereum", "order_by": "seven_day_volume", "limit": 100}
        if cursor:
            params["next"] = cursor
        data, err = client.get("/collections", params)
        if err:
            errors.append(f"collections: {err}")
            break
        items = dig(data, "collections", default=[])
        if not items:
            break
        for c in items:
            slug = c.get("collection")
            if not slug or slug in pool:
                continue
            if c.get("is_disabled") or c.get("is_nsfw"):
                continue
            pool[slug] = c
        cursor = dig(data, "next")
        if not cursor:
            break
    return pool, errors


def stage2_stats(client, slug):
    """Floor, volume, owners. One request."""
    data, err = client.get(f"/collections/{slug}/stats")
    if err:
        return None, err
    total = dig(data, "total", default={})
    intervals = {b.get("interval"): b for b in dig(data, "intervals", default=[])}
    return {
        "floor_eth": dig(total, "floor_price"),
        "owners": dig(total, "num_owners"),
        "lifetime_sales": dig(total, "sales"),
        "sales_1d": dig(intervals, "one_day", "sales", default=0),
        "sales_7d": dig(intervals, "seven_day", "sales", default=0),
        "sales_30d": dig(intervals, "thirty_day", "sales", default=0),
        "volume_7d_eth": round(dig(intervals, "seven_day", "volume", default=0), 3),
        "volume_30d_eth": round(dig(intervals, "thirty_day", "volume", default=0), 3),
    }, None


def stage3_offers(client, slug, floor):
    """Top bid, spread, depth. One request."""
    data, err = client.get(f"/offers/collection/{slug}")
    if err:
        return None, err
    top, depth, count = bid_profile(data)
    return {
        "top_bid_eth": round(top, 6) if top else None,
        "spread_pct": round((floor - top) / floor * 100, 1) if (top and floor) else None,
        "bid_depth_near_top": depth,
        "active_offers": count,
    }, None


def wash_flags(stats):
    """Cheap heuristics for volume that isn't real. Not proof — prompts a look."""
    flags = []
    sales_7d = stats.get("sales_7d") or 0
    owners = stats.get("owners") or 0
    vol_7d = stats.get("volume_7d_eth") or 0
    vol_30d = stats.get("volume_30d_eth") or 0
    sales_30d = stats.get("sales_30d") or 0

    if sales_7d > 30 and owners and sales_7d > owners * 0.25:
        flags.append("sales_high_vs_owners")
    if vol_30d > 0 and vol_7d > vol_30d * 0.75 and sales_30d > 0:
        flags.append("volume_concentrated_in_one_week")
    if sales_7d > 0 and vol_7d > 0:
        avg = vol_7d / sales_7d
        floor = stats.get("floor_eth") or 0
        if floor and avg > floor * 4:
            flags.append("avg_sale_far_above_floor")
    return flags


# ------------------------------------------------------------------ criteria

def evaluate(row, cfg, budget_eth):
    """Apply thresholds. Returns (passes, failed_conditions, size_band).

    Size is not a gate. It sets how demanding the other conditions become,
    because the cost of being wrong scales with position size while confidence
    does not.
    """
    t = cfg["criteria"]
    floor = row.get("floor_eth")
    fails = []

    if not floor or floor <= 0:
        return False, ["no_floor"], None

    size_frac = floor / budget_eth if budget_eth else 99
    if size_frac <= 0.20:
        band, mult = "small", 1.0
    elif size_frac <= 0.40:
        band, mult = "medium", 0.75
    else:
        band, mult = "large", 0.5

    if size_frac > t["max_position_fraction"]:
        fails.append(f"costs {size_frac:.0%} of budget (cap {t['max_position_fraction']:.0%})")

    spread = row.get("spread_pct")
    max_spread = t["max_spread_pct"] * mult
    if spread is None:
        fails.append("no_bid")
    elif spread > max_spread:
        fails.append(f"spread {spread}% > {max_spread:.0f}% for {band} position")
    elif spread < 0:
        fails.append(f"NEGATIVE spread {spread}% — likely a parsing fault, treat as suspect")

    min_sales = t["min_sales_7d"] / mult
    if (row.get("sales_7d") or 0) < min_sales:
        fails.append(f"sales_7d {row.get('sales_7d')} < {min_sales:.0f} for {band} position")

    if (row.get("owners") or 0) < t["min_owners"]:
        fails.append(f"owners {row.get('owners')} < {t['min_owners']}")

    min_depth = t["min_bid_depth"] / mult
    if (row.get("bid_depth_near_top") or 0) < min_depth:
        fails.append(f"bid depth {row.get('bid_depth_near_top')} < {min_depth:.0f} for {band}")

    if row.get("safelist_status") != "verified":
        fails.append("not verified")

    return (len(fails) == 0), fails, band


# ------------------------------------------------------------------ watchlist

def update_watchlist(wl, scanned, today, cfg):
    """Entries join on qualifying, serve a minimum tenure, and exit with a
    recorded reason. Silent removal is what creates survivorship bias — exiting
    is fine as long as it is written down."""
    tenure = cfg["watchlist"]["min_tenure_weeks"]
    grace = cfg["watchlist"]["consecutive_fails_to_exit"]
    by_slug = {e["slug"]: e for e in wl["active"]}
    events = []

    for slug, row in scanned.items():
        entry = by_slug.get(slug)
        if entry:
            entry["weeks_on_list"] += 1
            entry["last_seen"] = today
            if row["passes"]:
                entry["consecutive_fails"] = 0
            else:
                entry["consecutive_fails"] = entry.get("consecutive_fails", 0) + 1
        elif row["passes"]:
            new = {
                "slug": slug,
                "name": row.get("name"),
                "entered_on": today,
                "weeks_on_list": 1,
                "consecutive_fails": 0,
                "last_seen": today,
                "entry_metrics": {k: row.get(k) for k in
                                  ("floor_eth", "top_bid_eth", "spread_pct",
                                   "sales_7d", "owners")},
                "research": None,
            }
            wl["active"].append(new)
            by_slug[slug] = new
            events.append({"type": "entered", "slug": slug, "date": today})

    still_active = []
    for e in wl["active"]:
        row = scanned.get(e["slug"])
        reason = None
        if row is None:
            e["missed_scans"] = e.get("missed_scans", 0) + 1
            if e["missed_scans"] >= 3:
                reason = "disappeared from scan for 3 consecutive weeks"
        else:
            e["missed_scans"] = 0
            if e["weeks_on_list"] > tenure and e.get("consecutive_fails", 0) >= grace:
                reason = (f"failed criteria {e['consecutive_fails']} weeks running "
                          f"after {tenure}-week tenure: "
                          + "; ".join(row.get("fails", [])[:3]))

        if reason:
            e["exited_on"] = today
            e["exit_reason"] = reason
            e["exit_metrics"] = {k: (row or {}).get(k) for k in
                                 ("floor_eth", "top_bid_eth", "spread_pct", "sales_7d")}
            wl["exited"].append(e)
            events.append({"type": "exited", "slug": e["slug"],
                           "date": today, "reason": reason})
        else:
            still_active.append(e)

    wl["active"] = still_active
    return events


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("OPENSEA_API_KEY")
    if not key:
        log("FATAL: OPENSEA_API_KEY not set.")
        sys.exit(1)

    cfg = load(CONFIG, None)
    if not cfg:
        log("FATAL: config.json missing or invalid.")
        sys.exit(1)

    client = OpenSeaClient(key)
    today = date.today().isoformat()
    errors = []

    price, e = eth_usd()
    if e:
        errors.append(e)
    bal, e = wallet_eth(cfg["wallet_address"])
    if e:
        errors.append(e)

    if args.probe:
        data, err = client.get("/collections", {"chain": "ethereum", "limit": 5})
        log(f"collections: {'FAIL ' + err if err else 'ok'}")
        log(f"eth_usd={price}  wallet_eth={bal}")
        return

    budget = bal if bal else cfg.get("fallback_budget_eth", 0.28)
    if bal is None:
        log(f"Wallet unreadable; using fallback budget {budget} ETH")
    log(f"Budget: {budget:.4f} ETH (${budget * price:,.0f})" if price else f"Budget: {budget}")

    wl = load(WATCHLIST, {"active": [], "exited": []})
    watched = {e["slug"] for e in wl["active"]}
    log(f"Watchlist: {len(watched)} active")

    pool, e1 = stage1_pool(client, cfg)
    errors += e1
    if args.dry_run:
        pool = dict(list(pool.items())[:15])
    log(f"Stage 1: {len(pool)} collections")

    for slug in watched:
        pool.setdefault(slug, {"collection": slug, "name": slug, "_watchlist_only": True})

    t = cfg["criteria"]
    max_floor = budget * t["max_position_fraction"]
    scanned, rejected = {}, {"stats_error": 0, "too_expensive": 0, "too_illiquid": 0}

    for i, (slug, meta) in enumerate(pool.items(), 1):
        if i % 40 == 0:
            log(f"  stage 2: {i}/{len(pool)}")
        watched_now = slug in watched

        stats, err = stage2_stats(client, slug)
        if err:
            rejected["stats_error"] += 1
            continue

        # Watchlist members bypass the cheap cuts: we keep measuring them even
        # once they stop qualifying, which is the whole point of the list.
        if not watched_now:
            if not stats["floor_eth"] or stats["floor_eth"] > max_floor:
                rejected["too_expensive"] += 1
                continue
            if (stats["sales_7d"] or 0) < t["min_sales_7d"]:
                rejected["too_illiquid"] += 1
                continue

        row = {"slug": slug, "name": meta.get("name") or slug,
               "safelist_status": meta.get("safelist_status"),
               "total_supply": meta.get("total_supply"),
               "created": meta.get("created_date"),
               "url": f"https://opensea.io/collection/{slug}",
               "on_watchlist": watched_now, **stats}

        offers, err = stage3_offers(client, slug, stats["floor_eth"])
        if err:
            row["offers_error"] = err
        else:
            row.update(offers)

        row["wash_flags"] = wash_flags(stats)
        passes, fails, band = evaluate(row, cfg, budget)
        row["passes"], row["fails"], row["size_band"] = passes, fails, band
        row["position_pct_of_budget"] = (round(stats["floor_eth"] / budget * 100, 1)
                                         if stats["floor_eth"] and budget else None)
        scanned[slug] = row

    log(f"Stage 3 complete. {len(scanned)} fully measured, {client.calls} API calls.")

    events = update_watchlist(wl, scanned, today, cfg)
    for ev in events:
        log(f"  {ev['type'].upper()}: {ev['slug']}")

    qualifying = [r for r in scanned.values() if r["passes"]]
    # Near misses: one condition short. Surfaced rather than dropped, so the
    # screen can be argued with rather than just obeyed.
    near = [r for r in scanned.values() if not r["passes"] and len(r["fails"]) == 1]
    near.sort(key=lambda r: r.get("spread_pct") if r.get("spread_pct") is not None else 999)

    snapshot = {
        "date": today,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "eth_usd": price,
        "wallet_eth": bal,
        "budget_usd": round(budget * price, 2) if price else None,
        "pool_size": len(pool),
        "measured": len(scanned),
        "api_calls": client.calls,
        "rejected": rejected,
        "qualifying": qualifying,
        "near_misses": near[:10],
        "watchlist_rows": [scanned[s] for s in watched if s in scanned],
        "events": events,
        "errors": errors,
    }

    if args.dry_run:
        print(json.dumps(snapshot, indent=2)[:3000])
        log("Dry run — nothing written.")
        return

    LATEST.write_text(json.dumps(snapshot, indent=2))
    WATCHLIST.write_text(json.dumps(wl, indent=2))

    hist = load(HISTORY, [])
    hist.append({
        "date": today,
        "eth_usd": price,
        "wallet_eth": bal,
        "qualifying_count": len(qualifying),
        "rows": [{k: r.get(k) for k in
                  ("slug", "floor_eth", "top_bid_eth", "spread_pct", "sales_7d",
                   "volume_7d_eth", "owners", "bid_depth_near_top", "passes",
                   "on_watchlist", "wash_flags")}
                 for r in list(scanned.values())],
    })
    HISTORY.write_text(json.dumps(hist[-60:], indent=2))

    print("\n" + "=" * 66)
    print(f"SCAN {today}   ETH ${price}   budget ${snapshot['budget_usd']}")
    print(f"measured {len(scanned)}  |  watchlist {len(wl['active'])}  |  "
          f"qualifying {len(qualifying)}  |  near misses {len(near)}")
    print("=" * 66)
    for r in wl["active"]:
        row = scanned.get(r["slug"])
        if not row:
            print(f"  {r['slug'][:30]:<31} (not seen this scan)")
            continue
        mark = "OK " if row["passes"] else "   "
        print(f"{mark}{(row['name'] or row['slug'])[:29]:<30}"
              f"floor {row.get('floor_eth', 0):>8.4f}  "
              f"spread {str(row.get('spread_pct')):>6}%  "
              f"7d {row.get('sales_7d', 0):>4}  wk{r['weeks_on_list']:>3}")
    if not wl["active"]:
        print("  (watchlist empty)")
    print("=" * 66)
    if errors:
        print(f"errors: {errors}")


if __name__ == "__main__":
    main()
