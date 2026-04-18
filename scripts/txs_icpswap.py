#!/usr/bin/env python3
"""
txs_icpswap.py — Query recent transactions on an ICPSwap pool.

Uses the public ICPSwap transaction API:
  https://api.icpswap.com/info/transaction/find

No dfx required.

Usage:
  txs_icpswap.py ICP/ckUSDC                              # latest 10 txs
  txs_icpswap.py ICP/ckUSDC --limit 20
  txs_icpswap.py ICP/ckUSDC --type Swap
  txs_icpswap.py ICP/ckUSDC --type Swap,AddLiquidity
  txs_icpswap.py --pool-id mohjv-bqaaa-aaaag-qjyia-cai --principal <ID>
  txs_icpswap.py ICP/ckUSDC --json
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent))
from query_icpswap import fetch_pools, pair_matches, pool_pair  # type: ignore[import]

API_URL = "https://api.icpswap.com/info/transaction/find"
VALID_ACTIONS = ("Swap", "AddLiquidity", "DecreaseLiquidity", "Claim")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query recent ICPSwap transactions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Valid action types: {', '.join(VALID_ACTIONS)}",
    )
    parser.add_argument("positional", nargs="?", help="Pair, e.g. ICP/ckUSDC")
    parser.add_argument("--pair", help="Pair symbol, e.g. ICP/ckUSDC")
    parser.add_argument("--pool-id", dest="pool_id",
                        help="Pool canister ID (skips pair resolution)")
    parser.add_argument("--token-id", dest="token_id",
                        help="Filter by token ledger canister ID")
    parser.add_argument("--principal", help="Filter by user principal ID")
    parser.add_argument("--type", dest="action_types", metavar="TYPES",
                        help="Action types, comma-separated "
                             "(Swap,AddLiquidity,DecreaseLiquidity,Claim)")
    parser.add_argument("--limit", type=int, default=10, help="Results per page (default 10)")
    parser.add_argument("--page", type=int, default=1, help="Page number (default 1)")
    parser.add_argument("--begin", type=int, metavar="MS",
                        help="Start epoch in milliseconds")
    parser.add_argument("--end", type=int, metavar="MS",
                        help="End epoch in milliseconds")
    parser.add_argument("--json", action="store_true", help="Print raw JSON response")
    args = parser.parse_args()

    if args.positional and not args.pair:
        args.pair = args.positional

    if args.action_types:
        parts = [p.strip() for p in args.action_types.split(",") if p.strip()]
        invalid = [p for p in parts if p not in VALID_ACTIONS]
        if invalid:
            parser.error(
                f"Invalid action types: {', '.join(invalid)}. "
                f"Valid: {', '.join(VALID_ACTIONS)}"
            )
        args.action_types = ",".join(parts)

    return args


def resolve_pool_id(pair: str) -> tuple[Optional[str], Optional[str]]:
    """Return (poolId, displayPair) for the best matching pool."""
    try:
        pools = fetch_pools()
    except Exception as exc:
        print(f"Failed to fetch ICPSwap pool list: {exc}", file=sys.stderr)
        return None, None

    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for pool in pools:
        matched, score = pair_matches(pool, pair)
        if not matched:
            continue
        try:
            tvl = float(pool.get("tvlUSD") or 0)
        except (TypeError, ValueError):
            tvl = 0.0
        candidates.append((score, tvl, pool))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (-item[0], -item[1]))
    best = candidates[0][2]
    return best.get("poolId"), pool_pair(best)


def fetch_transactions(params: dict[str, Any]) -> dict[str, Any]:
    cleaned = {k: v for k, v in params.items() if v is not None and v != ""}
    qs = urllib.parse.urlencode(cleaned)
    url = f"{API_URL}?{qs}" if qs else API_URL
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "openclaw-icpswap-txs/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)
    if payload.get("code") != 200:
        raise RuntimeError(payload.get("message") or f"Unexpected response: {payload!r}")
    return payload.get("data") or {}


def format_timestamp(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
            "%m-%d %H:%M"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return "n/a"


def format_amount(raw: Any) -> str:
    """Human-friendly amount: wide ranges get fewer decimals."""
    if raw in (None, ""):
        return "0"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if value == 0:
        return "0"
    abs_v = abs(value)
    if abs_v >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    if abs_v >= 10_000:
        return f"{value:,.0f}"
    if abs_v >= 100:
        return f"{value:,.2f}"
    if abs_v >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def format_usd(raw: Any) -> Optional[str]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value == 0:
        return "$0"
    abs_v = abs(value)
    if abs_v < 0.01:
        return "<$0.01"
    if abs_v < 10:
        return f"${value:,.4f}".rstrip("0").rstrip(".")
    if abs_v < 1000:
        return f"${value:,.2f}"
    if abs_v < 1_000_000:
        return f"${value:,.0f}"
    return f"${value / 1_000_000:,.2f}M"


def short_principal(principal: Optional[str]) -> str:
    if not principal:
        return "n/a"
    if len(principal) <= 18:
        return principal
    return f"{principal[:8]}…{principal[-5:]}"


def is_nonzero(raw: Any) -> bool:
    try:
        return float(raw or 0) > 0
    except (TypeError, ValueError):
        return False


def describe_action(tx: dict[str, Any]) -> tuple[str, str]:
    """Return (label, amounts) for a transaction. Label is emoji + short verb."""
    action = tx.get("actionType", "")
    t0 = tx.get("token0Symbol") or "token0"
    t1 = tx.get("token1Symbol") or "token1"
    in0 = tx.get("token0AmountIn")
    in1 = tx.get("token1AmountIn")
    out0 = tx.get("token0AmountOut")
    out1 = tx.get("token1AmountOut")

    if action == "Swap":
        if is_nonzero(in0) and is_nonzero(out1):
            return "🔁 Swap", f"{format_amount(in0)} {t0} → {format_amount(out1)} {t1}"
        if is_nonzero(in1) and is_nonzero(out0):
            return "🔁 Swap", f"{format_amount(in1)} {t1} → {format_amount(out0)} {t0}"
    if action == "AddLiquidity":
        return "➕ Add LP", f"{format_amount(in0)} {t0} + {format_amount(in1)} {t1}"
    if action == "DecreaseLiquidity":
        return "➖ Remove LP", f"{format_amount(out0)} {t0} + {format_amount(out1)} {t1}"
    if action == "Claim":
        return "💰 Claim", f"{format_amount(out0)} {t0} + {format_amount(out1)} {t1}"
    return action or "?", ""


def tx_usd_value(tx: dict[str, Any]) -> Optional[str]:
    """Pick a USD value for this tx, preferring the nonzero side."""
    for key in ("token0TxValue", "token1TxValue"):
        if is_nonzero(tx.get(key)):
            return format_usd(tx.get(key))
    return None


def format_table(data: dict[str, Any], pair_label: Optional[str]) -> str:
    content = data.get("content") or []
    if not content:
        return "No transactions found."

    total = data.get("totalElements", len(content))
    page = data.get("page", 1)
    limit = data.get("limit", len(content))

    header_pair = pair_label or content[0].get("toAlias") or content[0].get("poolId") or "pool"
    try:
        total_str = f"{int(total):,}"
    except (TypeError, ValueError):
        total_str = str(total)

    canonical_t0 = pair_label.split("/")[0] if pair_label and "/" in pair_label else None

    price_symbol: Optional[str] = None
    price_value: Optional[str] = None
    for tx in content:
        if canonical_t0 and tx.get("token0Symbol") == canonical_t0:
            price_symbol = canonical_t0
            price_value = format_usd(tx.get("token0Price"))
            if price_value:
                break
        if canonical_t0 and tx.get("token1Symbol") == canonical_t0:
            price_symbol = canonical_t0
            price_value = format_usd(tx.get("token1Price"))
            if price_value:
                break
    if not price_value:
        first = content[0]
        price_symbol = first.get("token0Symbol") or "token0"
        price_value = format_usd(first.get("token0Price"))

    page_info = f"page {page}" if page > 1 else "latest"
    header_parts = [f"📜 {header_pair}"]
    if price_value:
        header_parts.append(f"{price_symbol} {price_value}")
    header_parts.append(f"{len(content)} of {total_str} txs · {page_info}, limit {limit}")

    rows = []
    for tx in content:
        ts = format_timestamp(tx.get("txTime"))
        label, amounts = describe_action(tx)
        value = tx_usd_value(tx) or ""
        who = short_principal(tx.get("fromPrincipalId"))
        rows.append((ts, label, amounts, value, who))

    w_label = max((len(r[1]) for r in rows), default=10)
    w_amounts = max((len(r[2]) for r in rows), default=20)
    w_value = max((len(r[3]) for r in rows), default=0)

    lines = [
        "  ·  ".join(header_parts),
        "─" * 72,
    ]
    for ts, label, amounts, value, who in rows:
        value_col = value.rjust(w_value) if w_value else ""
        row = f"{ts}  {label:<{w_label}}  {amounts:<{w_amounts}}"
        if value_col:
            row = f"{row}  {value_col}"
        row = f"{row}  {who}"
        lines.append(row)
    return "\n".join(lines)


def main() -> int:
    args = parse_args()

    pair_label: Optional[str] = None
    pool_id = args.pool_id
    if not pool_id and args.pair:
        pool_id, pair_label = resolve_pool_id(args.pair)
        if not pool_id:
            print(f"No ICPSwap pool found for pair: {args.pair}", file=sys.stderr)
            return 1

    params = {
        "poolId": pool_id,
        "tokenId": args.token_id,
        "principal": args.principal,
        "actionTypes": args.action_types,
        "page": args.page,
        "limit": args.limit,
        "begin": args.begin,
        "end": args.end,
    }

    try:
        data = fetch_transactions(params)
    except Exception as exc:
        print(f"Failed to fetch transactions: {exc}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(data, sys.stdout, indent=2, ensure_ascii=True)
        sys.stdout.write("\n")
    else:
        print(format_table(data, pair_label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
