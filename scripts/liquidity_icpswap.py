#!/usr/bin/env python3
"""
liquidity_icpswap.py — Add/remove liquidity on ICPSwap DEX

Usage:
  liquidity_icpswap.py positions ICP/ckUSDC
  liquidity_icpswap.py add ICP ckUSDC --amount0 10 [--amount1 125] [--yes]
  liquidity_icpswap.py add ICP ckUSDC --amount0 10 --tick-lower -100 --tick-upper 100 --yes
  liquidity_icpswap.py remove ICP/ckUSDC [--position-id N] [--percent 50] [--yes]

Requires: dfx CLI installed and configured with a non-anonymous identity
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# ── Shared utilities from swap_icpswap ───────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from swap_icpswap import (  # type: ignore[import]
    check_dfx,
    dfx_call,
    dfx_identity_principal,
    do_withdraw,
    fetch_balance,
    fetch_ledger_fee_live,
    fetch_pool,
    fetch_pool_balance,
    fetch_pool_canister_token_addresses,
    fetch_token_info,
    format_amount,
    from_base_units,
    to_base_units,
    _do_pool_withdrawals,
)

# ── Tick / fee constants ──────────────────────────────────────────────────────
MAX_TICK = 887_272
TICK_SPACING: dict[int, int] = {500: 10, 3_000: 60, 10_000: 200}


def full_range_ticks(fee: int) -> tuple[int, int]:
    """Return (tickLower, tickUpper) for a full-range position given pool fee tier."""
    spacing = TICK_SPACING.get(fee, 60)
    tick_max = (MAX_TICK // spacing) * spacing
    return -tick_max, tick_max


def _pool_fee(pool: dict[str, Any]) -> int:
    try:
        return int(pool.get("fee", 3_000))
    except (TypeError, ValueError):
        return 3_000


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add/remove ICPSwap liquidity positions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s positions ICP/ckUSDC
  %(prog)s add ICP ckUSDC --amount0 10 --amount1 125
  %(prog)s add ICP ckUSDC --amount0 10 --amount1 125 --yes
  %(prog)s remove ICP/ckUSDC --position-id 42 --yes
  %(prog)s remove ICP/ckUSDC --position-id 42 --percent 50 --yes
""",
    )
    sub = parser.add_subparsers(dest="action", metavar="ACTION")
    sub.required = True

    # ── positions ─────────────────────────────────────────────────────────────
    p_pos = sub.add_parser("positions", help="List your LP positions for a pair")
    p_pos.add_argument("pair", help='Token pair, e.g. "ICP/ckUSDC"')

    # ── add ───────────────────────────────────────────────────────────────────
    p_add = sub.add_parser("add", help="Add liquidity to a pool")
    p_add.add_argument("token0", metavar="TOKEN0", help="First token, e.g. ICP")
    p_add.add_argument("token1", metavar="TOKEN1", help="Second token, e.g. ckUSDC")
    p_add.add_argument("--amount0", type=float, required=True,
                       help="Amount of TOKEN0 to deposit")
    p_add.add_argument("--amount1", type=float,
                       help="Amount of TOKEN1 to deposit (estimated from pool price if omitted)")
    p_add.add_argument("--tick-lower", type=int,
                       help="Lower tick bound (default: full range for the pool fee tier)")
    p_add.add_argument("--tick-upper", type=int,
                       help="Upper tick bound (default: full range for the pool fee tier)")
    p_add.add_argument("--slippage", type=float, default=1.0,
                       help="Slippage tolerance in percent (default 1.0)")
    p_add.add_argument("--yes", action="store_true",
                       help="Execute the transaction (otherwise show preview only)")

    # ── remove ────────────────────────────────────────────────────────────────
    p_rm = sub.add_parser("remove", help="Remove liquidity from a position")
    p_rm.add_argument("pair", help='Token pair, e.g. "ICP/ckUSDC"')
    p_rm.add_argument("--position-id", type=int, metavar="N",
                      help="Position ID (uses the first position if omitted)")
    p_rm.add_argument("--percent", type=float, default=100.0,
                      help="Percent of position liquidity to remove (default 100)")
    p_rm.add_argument("--slippage", type=float, default=1.0,
                      help="Slippage tolerance in percent (default 1.0)")
    p_rm.add_argument("--yes", action="store_true",
                      help="Execute the transaction (otherwise show preview only)")

    return parser.parse_args()


# ── Pair parser helper ────────────────────────────────────────────────────────

def parse_pair(pair: str) -> tuple[str, str]:
    parts = pair.split("/") if "/" in pair else pair.split()
    if len(parts) < 2 or not parts[0] or not parts[1]:
        print(f"❌ Invalid pair: {pair!r}  — use format: ICP/ckUSDC")
        sys.exit(1)
    return parts[0].upper(), parts[1].upper()


# ── Ledger resolution helper ──────────────────────────────────────────────────

def resolve_ledgers(
    pool: dict[str, Any],
    from_sym: str,
    to_sym: str,
) -> tuple[str, str]:
    """Return (from_ledger, to_ledger) matching the given symbols."""
    if pool["token0Symbol"].upper() == from_sym:
        return pool["token0LedgerId"], pool["token1LedgerId"]
    elif pool["token1Symbol"].upper() == from_sym:
        return pool["token1LedgerId"], pool["token0LedgerId"]
    else:
        # Loose match — default to API order
        return pool["token0LedgerId"], pool["token1LedgerId"]


def canonical_order(
    dfx: str,
    pool: dict[str, Any],
    from_ledger: str,
    to_ledger: str,
    from_sym: str,
    to_sym: str,
    from_info: dict[str, int],
    to_info: dict[str, int],
) -> tuple[str, str, str, str, dict[str, int], dict[str, int]]:
    """
    Resolve canonical (pool contract) token0 / token1 order.
    Returns (ledger0, ledger1, sym0, sym1, info0, info1).
    """
    pool_id = pool["poolId"]
    addrs = fetch_pool_canister_token_addresses(dfx, pool_id)
    if addrs:
        ledger0, ledger1 = addrs
    else:
        ledger0, ledger1 = sorted([from_ledger, to_ledger])

    if from_ledger == ledger0:
        return ledger0, ledger1, from_sym, to_sym, from_info, to_info
    else:
        return ledger0, ledger1, to_sym, from_sym, to_info, from_info


# ── Position queries ──────────────────────────────────────────────────────────

def fetch_user_positions(dfx: str, pool_id: str, principal: str) -> list[dict[str, Any]]:
    """Query the user's LP positions in a pool via getUserPositionsByPrincipal."""
    result = subprocess.run(
        [dfx, "canister", "call", "--network", "ic", "--query",
         pool_id, "getUserPositionsByPrincipal", f"(principal \"{principal}\")"],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        return []
    return _parse_positions(result.stdout)


def _parse_int(raw: str) -> Optional[int]:
    try:
        cleaned = raw.replace("_", "").split(":")[0].strip()
        return int(cleaned)
    except (ValueError, AttributeError):
        return None


def _parse_positions(candid: str) -> list[dict[str, Any]]:
    """
    Parse Candid output of getUserPositionsByPool into position dicts.
    Fields extracted: id, tickLower, tickUpper, liquidity, tokensOwed0, tokensOwed1.
    """
    INT_FIELDS = (
        "id", "tickLower", "tickUpper", "liquidity",
        "tokensOwed0", "tokensOwed1",
        "feeGrowthInside0LastX128", "feeGrowthInside1LastX128",
    )

    # Extract brace-delimited blocks
    depth = 0
    start = -1
    blocks: list[str] = []
    for i, ch in enumerate(candid):
        if ch == "{":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                blocks.append(candid[start:i])
                start = -1

    positions: list[dict[str, Any]] = []
    for block in blocks:
        pos: dict[str, Any] = {}
        for field in INT_FIELDS:
            m = re.search(
                rf'\b{re.escape(field)}\s*=\s*(-?[\d_]+)(?:\s*:\s*\w+)?',
                block,
            )
            if m:
                v = _parse_int(m.group(1))
                if v is not None:
                    pos[field] = v
        if "id" in pos:
            positions.append(pos)

    return positions


def print_positions(
    positions: list[dict[str, Any]],
    pool: dict[str, Any],
    sym0: str,
    sym1: str,
    info0: dict[str, int],
    info1: dict[str, int],
) -> None:
    sep = "─" * 50
    fee = _pool_fee(pool)
    full_low, full_high = full_range_ticks(fee)

    print(f"\n🏊 LP Positions — {pool.get('pair', f'{sym0}/{sym1}')}  ·  ⚡ {pool.get('feePercent', 'n/a')}")
    print(sep)

    if not positions:
        print("  No LP positions found in this pool.")
        print(sep)
        return

    for pos in positions:
        pos_id      = pos.get("id", "?")
        liquidity   = pos.get("liquidity", 0)
        tick_lower  = pos.get("tickLower")
        tick_upper  = pos.get("tickUpper")
        owed0       = pos.get("tokensOwed0", 0)
        owed1       = pos.get("tokensOwed1", 0)

        if tick_lower == full_low and tick_upper == full_high:
            range_str = "Full range"
        else:
            range_str = f"[{tick_lower}, {tick_upper}]"

        print(f"  Position #{pos_id}")
        print(f"    Range:      {range_str}")
        print(f"    Liquidity:  {liquidity:,}")
        if owed0 > 0 or owed1 > 0:
            owed0_h = from_base_units(owed0, info0["decimals"])
            owed1_h = from_base_units(owed1, info1["decimals"])
            print(f"    Fees owed:  {format_amount(owed0_h, sym0)}  +  {format_amount(owed1_h, sym1)}")
        print()

    pair_str = pool.get("pair", f"{sym0}/{sym1}")
    print(sep)
    print(f"  Remove:  /icpswap remove-liquidity {pair_str} --position-id N [--percent 50] [--yes]")
    print(sep)


# ── Add liquidity ─────────────────────────────────────────────────────────────

def _estimate_amount1(
    pool: dict[str, Any],
    from_ledger: str,
    ledger0: str,
    amount0: float,
) -> Optional[float]:
    """Estimate amount1 from current pool price for a full-range position."""
    try:
        if from_ledger == ledger0:
            rate = pool.get("token1PerToken0")
        else:
            rate = pool.get("token0PerToken1")
        if rate is None:
            return None
        return float(rate) * amount0
    except (TypeError, ValueError, KeyError):
        return None


def add_liquidity(
    dfx: str,
    pool: dict[str, Any],
    from_ledger: str,
    to_ledger: str,
    from_info: dict[str, int],
    to_info: dict[str, int],
    from_sym: str,
    to_sym: str,
    amount0_input: float,
    amount1_input: float,
    tick_lower: int,
    tick_upper: int,
    slippage: float,
    principal: str,
    execute: bool,
) -> int:
    pool_id = pool["poolId"]
    sep = "─" * 50
    fee = _pool_fee(pool)

    # Resolve canonical order; amounts/infos are mapped accordingly
    ledger0, ledger1, sym0, sym1, info0, info1 = canonical_order(
        dfx, pool, from_ledger, to_ledger, from_sym, to_sym, from_info, to_info
    )
    if from_ledger == ledger0:
        amount0, amount1 = amount0_input, amount1_input
    else:
        amount0, amount1 = amount1_input, amount0_input

    amount0_base = to_base_units(amount0, info0["decimals"])
    amount1_base = to_base_units(amount1, info1["decimals"])
    amount0_min  = int(amount0_base * (1.0 - slippage / 100.0))
    amount1_min  = int(amount1_base * (1.0 - slippage / 100.0))

    full_low, full_high = full_range_ticks(fee)
    is_full_range = (tick_lower == full_low and tick_upper == full_high)
    range_str = "Full range" if is_full_range else f"[{tick_lower}, {tick_upper}]"

    # Preview
    print(f"\n💧 Add Liquidity Preview")
    print(sep)
    print(f"  Pool:         {pool.get('pair', f'{sym0}/{sym1}')}  ·  ⚡ {pool.get('feePercent', 'n/a')}")
    print(f"  {sym0:<10}  {format_amount(amount0, sym0)}")
    print(f"  {sym1:<10}  {format_amount(amount1, sym1)}")
    print(f"  Range:        {range_str}")
    print(f"  Slippage:     {slippage}%")

    # Wallet balances
    w0 = fetch_balance(dfx, ledger0, principal, info0["decimals"])
    w1 = fetch_balance(dfx, ledger1, principal, info1["decimals"])
    print(sep)
    print(f"  Wallet {sym0:<8}  {format_amount(w0, sym0) if w0 is not None else 'unavailable'}"
          + ("  ⚠️  insufficient!" if w0 is not None and w0 < amount0 else ""))
    print(f"  Wallet {sym1:<8}  {format_amount(w1, sym1) if w1 is not None else 'unavailable'}"
          + ("  ⚠️  insufficient!" if w1 is not None and w1 < amount1 else ""))
    print(sep)

    if not execute:
        print("Add --yes to execute.\n")
        return 0

    # Guard: insufficient balance
    if w0 is not None and w0 < amount0:
        print(f"❌ Insufficient {sym0}: need {format_amount(amount0, sym0)}, have {format_amount(w0, sym0)}")
        return 1
    if w1 is not None and w1 < amount1:
        print(f"❌ Insufficient {sym1}: need {format_amount(amount1, sym1)}, have {format_amount(w1, sym1)}")
        return 1

    print(f"\n💧 Adding liquidity to {pool.get('pair', f'{sym0}/{sym1}')} pool...")
    print(f"   Pool: {pool_id}")

    # Refresh transfer fees live from each ledger canister.
    # Pool contracts validate that the fee in depositFrom exactly matches their
    # cached ledger fee — using a stale hardcoded value causes "Wrong fee cache".
    print(f"  📡 Verifying ledger fees...", end=" ", flush=True)
    live0 = fetch_ledger_fee_live(dfx, ledger0)
    live1 = fetch_ledger_fee_live(dfx, ledger1)
    if live0 is not None and live0 != info0["transfer_fee"]:
        print(f"\n  📋 {sym0} fee updated: {info0['transfer_fee']} → {live0}")
        info0 = {**info0, "transfer_fee": live0}
    if live1 is not None and live1 != info1["transfer_fee"]:
        print(f"\n  📋 {sym1} fee updated: {info1['transfer_fee']} → {live1}")
        info1 = {**info1, "transfer_fee": live1}
    print("ok")

    # Recompute base units and approve amounts with refreshed fees
    amount0_base = to_base_units(amount0, info0["decimals"])
    amount1_base = to_base_units(amount1, info1["decimals"])
    amount0_min  = int(amount0_base * (1.0 - slippage / 100.0))
    amount1_min  = int(amount1_base * (1.0 - slippage / 100.0))

    # Step 1 — icrc2_approve token0
    r = dfx_call(
        dfx, ledger0, "icrc2_approve",
        f"(record {{ spender = record {{ owner = principal \"{pool_id}\"; subaccount = null }}; "
        f"amount = {amount0_base + info0['transfer_fee']} }})",
        f"Step 1/5 — icrc2_approve {sym0}",
    )
    if not r["ok"]:
        print(f"❌ Approval of {sym0} failed: {r['error']}")
        return 1

    # Step 2 — icrc2_approve token1
    r = dfx_call(
        dfx, ledger1, "icrc2_approve",
        f"(record {{ spender = record {{ owner = principal \"{pool_id}\"; subaccount = null }}; "
        f"amount = {amount1_base + info1['transfer_fee']} }})",
        f"Step 2/5 — icrc2_approve {sym1}",
    )
    if not r["ok"]:
        print(f"❌ Approval of {sym1} failed: {r['error']}")
        return 1

    # Step 3 — depositFrom token0
    r = dfx_call(
        dfx, pool_id, "depositFrom",
        f"(record {{ token = \"{ledger0}\"; amount = {amount0_base}; fee = {info0['transfer_fee']} }})",
        f"Step 3/5 — depositFrom {sym0}",
    )
    if not r["ok"]:
        _print_deposit_error(r["error"], sym0, ledger0, dfx)
        return 1

    # Step 4 — depositFrom token1
    r = dfx_call(
        dfx, pool_id, "depositFrom",
        f"(record {{ token = \"{ledger1}\"; amount = {amount1_base}; fee = {info1['transfer_fee']} }})",
        f"Step 4/5 — depositFrom {sym1}",
    )
    if not r["ok"]:
        _print_deposit_error(r["error"], sym1, ledger1, dfx)
        print(f"\n⚠️  Attempting to recover already-deposited {sym0}...")
        _try_recover(dfx, pool_id, principal, ledger0, ledger1, sym0, sym1, info0, info1)
        return 1

    # Step 5 — mint (create position)
    mint_args = (
        f"(record {{ "
        f"token0 = \"{ledger0}\"; "
        f"token1 = \"{ledger1}\"; "
        f"fee = {fee}; "
        f"tickLower = {tick_lower}; "
        f"tickUpper = {tick_upper}; "
        f"amount0Desired = \"{amount0_base}\"; "
        f"amount1Desired = \"{amount1_base}\"; "
        f"amount0Min = {amount0_min}; "
        f"amount1Min = {amount1_min} "
        f"}})"
    )
    r = dfx_call(dfx, pool_id, "mint", mint_args, "Step 5/5 — mint (create LP position)")
    if not r["ok"]:
        print(f"❌ Mint failed: {r['error']}")
        print(f"\n⚠️  Recovering deposited tokens from pool internal account...")
        _try_recover(dfx, pool_id, principal, ledger0, ledger1, sym0, sym1, info0, info1)
        return 1

    position_id = r.get("value")
    if position_id is not None:
        print(f"\n✅ LP position created! Position ID: #{position_id}")
    else:
        print(f"\n✅ LP position created!")

    # Return any unused tokens (concentrated liquidity rarely uses exact ratio)
    time.sleep(2)
    _try_recover(dfx, pool_id, principal, ledger0, ledger1, sym0, sym1, info0, info1,
                 label="Returning unused tokens")

    # Updated balances
    _print_updated_balances(dfx, ledger0, ledger1, sym0, sym1, info0, info1, principal, sep)
    return 0


# ── Remove liquidity ──────────────────────────────────────────────────────────

def remove_liquidity(
    dfx: str,
    pool: dict[str, Any],
    from_ledger: str,
    to_ledger: str,
    from_info: dict[str, int],
    to_info: dict[str, int],
    from_sym: str,
    to_sym: str,
    principal: str,
    position_id: Optional[int],
    percent: float,
    slippage: float,
    execute: bool,
) -> int:
    pool_id = pool["poolId"]
    sep = "─" * 50

    ledger0, ledger1, sym0, sym1, info0, info1 = canonical_order(
        dfx, pool, from_ledger, to_ledger, from_sym, to_sym, from_info, to_info
    )

    # Fetch positions
    print(f"🔍 Querying LP positions for {pool.get('pair', f'{from_sym}/{to_sym}')}...", end=" ", flush=True)
    positions = fetch_user_positions(dfx, pool_id, principal)
    print(f"found {len(positions)}")

    if not positions:
        print("❌ No LP positions found in this pool.")
        return 1

    # Select position
    if position_id is not None:
        pos = next((p for p in positions if p.get("id") == position_id), None)
        if pos is None:
            ids = [p.get("id") for p in positions]
            print(f"❌ Position #{position_id} not found. Available: {ids}")
            return 1
    else:
        pos = positions[0]
        if len(positions) > 1:
            ids = [p.get("id") for p in positions]
            print(f"ℹ️  Multiple positions found {ids}. Using #{pos.get('id')}. Use --position-id to select.")

    pos_id         = pos.get("id")
    total_liq      = pos.get("liquidity", 0)
    liq_to_remove  = int(total_liq * percent / 100.0)

    if liq_to_remove == 0:
        print(f"❌ Liquidity to remove is 0 (position #{pos_id} has {total_liq} total liquidity).")
        return 1

    fee = _pool_fee(pool)
    full_low, full_high = full_range_ticks(fee)
    tick_lower = pos.get("tickLower")
    tick_upper = pos.get("tickUpper")
    range_str = (
        "Full range"
        if tick_lower == full_low and tick_upper == full_high
        else f"[{tick_lower}, {tick_upper}]"
    )

    owed0 = pos.get("tokensOwed0", 0)
    owed1 = pos.get("tokensOwed1", 0)

    # Preview
    print(f"\n🔥 Remove Liquidity Preview")
    print(sep)
    print(f"  Pool:         {pool.get('pair', f'{sym0}/{sym1}')}  ·  ⚡ {pool.get('feePercent', 'n/a')}")
    print(f"  Position:     #{pos_id}")
    print(f"  Liquidity:    {liq_to_remove:,} / {total_liq:,}  ({percent:.0f}%)")
    print(f"  Range:        {range_str}")
    print(f"  Slippage:     {slippage}%")
    if owed0 > 0 or owed1 > 0:
        owed0_h = from_base_units(owed0, info0["decimals"])
        owed1_h = from_base_units(owed1, info1["decimals"])
        print(f"  Fees owed:    {format_amount(owed0_h, sym0)}  +  {format_amount(owed1_h, sym1)}")
    print(sep)

    if not execute:
        print("Add --yes to execute.\n")
        return 0

    # ── Execution ────────────────────────────────────────────────────────────
    print(f"\n🔥 Removing {percent:.0f}% of position #{pos_id}...")

    # Step 1 — decreaseLiquidity
    r = dfx_call(
        dfx, pool_id, "decreaseLiquidity",
        f"(record {{ positionId = {pos_id} : nat; liquidity = \"{liq_to_remove}\" }})",
        f"Step 1/3 — decreaseLiquidity (position #{pos_id})",
    )
    if not r["ok"]:
        print(f"❌ decreaseLiquidity failed: {r['error']}")
        return 1

    # Parse returned amounts (variant { ok = record { amount0 = X; amount1 = Y } })
    returned0 = returned1 = 0
    if r.get("output"):
        m0 = re.search(r'\bamount0\s*=\s*([\d_]+)', r["output"])
        m1 = re.search(r'\bamount1\s*=\s*([\d_]+)', r["output"])
        if m0:
            returned0 = int(m0.group(1).replace("_", ""))
        if m1:
            returned1 = int(m1.group(1).replace("_", ""))
    if returned0 > 0 or returned1 > 0:
        r0h = from_base_units(returned0, info0["decimals"])
        r1h = from_base_units(returned1, info1["decimals"])
        print(f"   → {format_amount(r0h, sym0)}  +  {format_amount(r1h, sym1)} moved to pool account")

    # Wait and query pool internal balance
    time.sleep(2)
    pool_bal = fetch_pool_balance(dfx, pool_id, principal)
    if pool_bal is None:
        print(f"\n⚠️  Cannot query pool internal balance. Withdraw manually:")
        print(f"   /icpswap withdraw {from_sym}/{to_sym}")
        return 1

    bal0 = pool_bal["balance0"]
    bal1 = pool_bal["balance1"]

    if bal0 == 0 and bal1 == 0:
        print(f"\n⚠️  Pool internal account is empty after decreaseLiquidity.")
        print(f"   Tokens may still be in tokensOwed — retry or contact support.")
        return 1

    # Refresh transfer fees live before withdraw (same as add_liquidity does)
    live0 = fetch_ledger_fee_live(dfx, ledger0)
    live1 = fetch_ledger_fee_live(dfx, ledger1)
    if live0 is not None and live0 != info0["transfer_fee"]:
        info0 = {**info0, "transfer_fee": live0}
    if live1 is not None and live1 != info1["transfer_fee"]:
        info1 = {**info1, "transfer_fee": live1}

    # Step 2 — withdraw token0 (skip dust: balance must exceed transfer fee)
    step = 2
    any_failed = False
    if bal0 > info0["transfer_fee"]:
        rr = dfx_call(
            dfx, pool_id, "withdraw",
            f"(record {{ token = \"{ledger0}\"; amount = {bal0}; fee = {info0['transfer_fee']} }})",
            f"Step {step}/3 — withdraw {sym0}",
        )
        step += 1
        if not rr["ok"]:
            print(f"❌ Withdraw {sym0} failed: {rr['error']}")
            any_failed = True
    elif bal0 > 0:
        print(f"   ⚠️  {sym0} dust ({bal0} base units) skipped — below transfer fee")

    # Step 3 — withdraw token1 (skip dust)
    if bal1 > info1["transfer_fee"]:
        rr = dfx_call(
            dfx, pool_id, "withdraw",
            f"(record {{ token = \"{ledger1}\"; amount = {bal1}; fee = {info1['transfer_fee']} }})",
            f"Step {step}/3 — withdraw {sym1}",
        )
        if not rr["ok"]:
            print(f"❌ Withdraw {sym1} failed: {rr['error']}")
            any_failed = True
    elif bal1 > 0:
        print(f"   ⚠️  {sym1} dust ({bal1} base units) skipped — below transfer fee")

    if any_failed:
        pair_str = pool.get("pair", f"{from_sym}/{to_sym}")
        print(f"\n⚠️  Some withdrawals failed. Retry: /icpswap withdraw {pair_str}")
        return 1

    action_str = "closed" if percent >= 100.0 else "partially reduced"
    r0h = from_base_units(bal0, info0["decimals"])
    r1h = from_base_units(bal1, info1["decimals"])
    print(f"\n✅ Position #{pos_id} {action_str}!")
    print(f"   Received: {format_amount(r0h, sym0)}  +  {format_amount(r1h, sym1)}")

    _print_updated_balances(dfx, ledger0, ledger1, sym0, sym1, info0, info1, principal, sep)
    return 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_deposit_error(error: str, sym: str, ledger_id: str, dfx: str) -> None:
    """
    Print a clear error for a failed depositFrom call.
    Detects the 'Wrong fee cache' case and explains what happened.
    """
    if "Wrong fee cache" in error:
        # Parse "expected: 10_000, received: 10"
        m_exp = re.search(r'expected[:\s]+([\d_]+)', error, re.IGNORECASE)
        m_got = re.search(r'received[:\s]+([\d_]+)', error, re.IGNORECASE)
        pool_fee = int(m_exp.group(1).replace("_", "")) if m_exp else None
        our_fee  = int(m_got.group(1).replace("_", "")) if m_got else None

        # Query the real ledger fee to diagnose whose cache is wrong
        live_fee = fetch_ledger_fee_live(dfx, ledger_id)

        print(f"❌ Deposit of {sym} failed: pool fee cache mismatch")
        if pool_fee is not None and our_fee is not None:
            print(f"   Pool expected fee={pool_fee:,}, we sent fee={our_fee:,}")
        if live_fee is not None and pool_fee is not None and live_fee != pool_fee:
            print(f"   Ledger reports actual fee={live_fee:,} — the pool's cache is stale.")
            print(f"   This is a temporary ICPSwap state issue. Please try again in a few minutes.")
        elif live_fee is not None:
            print(f"   Ledger actual fee={live_fee:,}. Pool cache may need a moment to sync.")
            print(f"   Please try again shortly.")
        else:
            print(f"   Please try again later (the pool fee cache will refresh automatically).")
    else:
        print(f"❌ Deposit of {sym} failed: {error}")


def _try_recover(
    dfx: str,
    pool_id: str,
    principal: str,
    ledger0: str,
    ledger1: str,
    sym0: str,
    sym1: str,
    info0: dict[str, int],
    info1: dict[str, int],
    label: str = "Recovering pool internal balance",
) -> None:
    """Withdraw any non-zero unusedBalance from the pool back to wallet."""
    pool_bal = fetch_pool_balance(dfx, pool_id, principal)
    if pool_bal is None:
        return
    bal0, bal1 = pool_bal["balance0"], pool_bal["balance1"]
    if bal0 == 0 and bal1 == 0:
        return
    b0h = from_base_units(bal0, info0["decimals"])
    b1h = from_base_units(bal1, info1["decimals"])
    print(f"\n💰 {label}:")
    print(f"   {sym0}: {format_amount(b0h, sym0)}")
    print(f"   {sym1}: {format_amount(b1h, sym1)}")
    _do_pool_withdrawals(dfx, pool_id, principal,
                         ledger0, ledger1, sym0, sym1, info0, info1,
                         bal0, bal1)


def _print_updated_balances(
    dfx: str,
    ledger0: str,
    ledger1: str,
    sym0: str,
    sym1: str,
    info0: dict[str, int],
    info1: dict[str, int],
    principal: str,
    sep: str,
) -> None:
    time.sleep(2)
    print(f"\n📊 Updated wallet balances")
    print(sep)
    b0 = fetch_balance(dfx, ledger0, principal, info0["decimals"])
    b1 = fetch_balance(dfx, ledger1, principal, info1["decimals"])
    print(f"💼 {sym0:<10} {format_amount(b0, sym0) if b0 is not None else 'unavailable'}")
    print(f"💼 {sym1:<10} {format_amount(b1, sym1) if b1 is not None else 'unavailable'}")
    print(sep)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()

    dfx       = check_dfx()
    principal = dfx_identity_principal(dfx)
    print(f"👤 Identity: {principal[:20]}...{principal[-6:]}")

    # ── positions ─────────────────────────────────────────────────────────────
    if args.action == "positions":
        from_sym, to_sym = parse_pair(args.pair)
        print(f"🔍 Looking up {from_sym}/{to_sym} pool...", end=" ", flush=True)
        pool = fetch_pool(from_sym, to_sym)
        print(f"found {pool['pair']} ({pool['poolId'][:12]}...)")

        from_ledger, to_ledger = resolve_ledgers(pool, from_sym, to_sym)
        from_info = fetch_token_info(from_ledger, from_sym)
        to_info   = fetch_token_info(to_ledger, to_sym)

        ledger0, ledger1, sym0, sym1, info0, info1 = canonical_order(
            dfx, pool, from_ledger, to_ledger, from_sym, to_sym, from_info, to_info
        )

        print(f"🔍 Querying LP positions...", end=" ", flush=True)
        positions = fetch_user_positions(dfx, pool["poolId"], principal)
        print(f"found {len(positions)}")
        print_positions(positions, pool, sym0, sym1, info0, info1)
        return 0

    # ── add ───────────────────────────────────────────────────────────────────
    if args.action == "add":
        from_sym = args.token0.upper()
        to_sym   = args.token1.upper()

        if args.amount0 <= 0:
            print("❌ --amount0 must be greater than 0")
            return 1

        print(f"🔍 Looking up {from_sym}/{to_sym} pool...", end=" ", flush=True)
        pool = fetch_pool(from_sym, to_sym)
        print(f"found {pool['pair']} ({pool['poolId'][:12]}...)")

        from_ledger, to_ledger = resolve_ledgers(pool, from_sym, to_sym)
        from_info = fetch_token_info(from_ledger, from_sym)
        to_info   = fetch_token_info(to_ledger, to_sym)

        fee = _pool_fee(pool)
        tick_lower, tick_upper = full_range_ticks(fee)
        if args.tick_lower is not None:
            tick_lower = args.tick_lower
        if args.tick_upper is not None:
            tick_upper = args.tick_upper

        amount0 = args.amount0
        if args.amount1 is not None:
            amount1 = args.amount1
        else:
            # Estimate from current pool price
            addrs = fetch_pool_canister_token_addresses(dfx, pool["poolId"])
            ledger0_canon = addrs[0] if addrs else sorted([from_ledger, to_ledger])[0]
            amount1 = _estimate_amount1(pool, from_ledger, ledger0_canon, amount0)
            if amount1 is None:
                print(f"❌ Cannot estimate {to_sym} amount from pool price. Specify --amount1 explicitly.")
                return 1
            print(f"📊 Estimated {to_sym} needed: {format_amount(amount1, to_sym)} (from pool price)")

        return add_liquidity(
            dfx=dfx,
            pool=pool,
            from_ledger=from_ledger,
            to_ledger=to_ledger,
            from_info=from_info,
            to_info=to_info,
            from_sym=from_sym,
            to_sym=to_sym,
            amount0_input=amount0,
            amount1_input=amount1,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            slippage=args.slippage,
            principal=principal,
            execute=args.yes,
        )

    # ── remove ────────────────────────────────────────────────────────────────
    if args.action == "remove":
        from_sym, to_sym = parse_pair(args.pair)

        if not (0 < args.percent <= 100):
            print("❌ --percent must be between 0 and 100")
            return 1

        print(f"🔍 Looking up {from_sym}/{to_sym} pool...", end=" ", flush=True)
        pool = fetch_pool(from_sym, to_sym)
        print(f"found {pool['pair']} ({pool['poolId'][:12]}...)")

        from_ledger, to_ledger = resolve_ledgers(pool, from_sym, to_sym)
        from_info = fetch_token_info(from_ledger, from_sym)
        to_info   = fetch_token_info(to_ledger, to_sym)

        return remove_liquidity(
            dfx=dfx,
            pool=pool,
            from_ledger=from_ledger,
            to_ledger=to_ledger,
            from_info=from_info,
            to_info=to_info,
            from_sym=from_sym,
            to_sym=to_sym,
            principal=principal,
            position_id=args.position_id,
            percent=args.percent,
            slippage=args.slippage,
            execute=args.yes,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
