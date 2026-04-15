#!/usr/bin/env python3
"""
swap_icpswap.py — Execute token swaps on ICPSwap DEX

Usage:
  swap_icpswap.py ICP 10 ckUSDC               # preview (no execution)
  swap_icpswap.py ICP 10 ckUSDC --yes         # execute swap
  swap_icpswap.py --from ICP --amount 10 --to ckUSDC [--slippage 0.5] [--yes]

Requires: dfx CLI installed and configured with a non-anonymous identity
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional


SKILL_DIR = Path(__file__).parent.parent
QUERY_SCRIPT = SKILL_DIR / "scripts" / "query_icpswap.py"
TOKEN_URL = "https://api.icpswap.com/info/token"

# Known token decimals and transfer fees (avoids extra API calls)
# Keys are uppercase; use symbol.upper() for lookups
KNOWN_TOKEN_INFO: dict[str, dict[str, int]] = {
    "ICP":    {"decimals": 8,  "transfer_fee": 10_000},
    "CKBTC":  {"decimals": 8,  "transfer_fee": 10},
    "CKETH":  {"decimals": 18, "transfer_fee": 2_000_000_000_000},
    "CKUSDC": {"decimals": 6,  "transfer_fee": 10},
    "CKUSDT": {"decimals": 6,  "transfer_fee": 10},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute a token swap on ICPSwap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s ICP 10 ckUSDC               # preview, no execution
  %(prog)s ICP 10 ckUSDC --yes         # execute swap
  %(prog)s --from ICP --amount 0.5 --to ckBTC --slippage 1.0 --yes
""",
    )
    parser.add_argument("positional", nargs="*", help="Shorthand form: FROM AMOUNT TO")
    parser.add_argument("--from", dest="from_token", metavar="TOKEN", help="Token to sell (e.g. ICP)")
    parser.add_argument("--amount", type=float, help="Amount to sell (human-readable, e.g. 10.5)")
    parser.add_argument("--to", dest="to_token", metavar="TOKEN", help="Token to buy (e.g. ckUSDC)")
    parser.add_argument("--slippage", type=float, default=0.5, help="Max slippage tolerance in percent (default 0.5%%)")
    parser.add_argument("--yes", action="store_true", help="Actually execute the swap (otherwise show preview only)")
    parser.add_argument("--withdraw-only", action="store_true",
                        help="Skip swap, only query and withdraw stuck pool internal balances")
    parser.add_argument("--balance-only", action="store_true",
                        help="Only query wallet and pool internal balances, no swap")
    args = parser.parse_args()

    # Handle positional shorthand: FROM AMOUNT TO [SLIPPAGE]
    if args.positional:
        pos = args.positional
        tokens = [p for p in pos if not p.startswith("-")]
        if len(tokens) >= 3 and args.from_token is None:
            args.from_token = tokens[0]
            if args.amount is None:
                try:
                    args.amount = float(tokens[1])
                except ValueError:
                    parser.error(f"Invalid amount: {tokens[1]}")
            args.to_token = tokens[2]
            # 4th positional: optional slippage (e.g. "1" means 1%)
            if len(tokens) >= 4:
                try:
                    args.slippage = float(tokens[3])
                except ValueError:
                    parser.error(f"Invalid slippage: {tokens[3]} — must be a number (e.g. 1 for 1%)")
        elif len(tokens) == 2 and args.from_token is None:
            args.from_token = tokens[0]
            args.to_token = tokens[1]

    # --balance-only / --withdraw-only only need from/to, not amount
    if args.balance_only or args.withdraw_only:
        if not args.from_token or not args.to_token:
            parser.error("--from and --to are required (or shorthand: FROM TO)")
    else:
        if not args.from_token or not args.to_token or args.amount is None:
            parser.error("Provide: --from TOKEN --amount AMOUNT --to TOKEN  or shorthand: FROM AMOUNT TO")
        if args.amount <= 0:
            parser.error("--amount must be greater than 0")
    if not (0 < args.slippage < 100):
        parser.error("--slippage must be between 0 and 100")

    return args


# ─── dfx helpers ─────────────────────────────────────────────────────────────

def _find_dfx_in_cache(home: Path) -> Optional[str]:
    """Find the latest dfx binary under ~/.cache/dfinity/versions/."""
    for base in [
        home / ".cache" / "dfinity" / "versions",
        home / "Library" / "Application Support" / "org.dfinity.dfx" / "versions",
    ]:
        if not base.is_dir():
            continue
        candidates: list[tuple[tuple[int, ...], Path]] = []
        for entry in base.iterdir():
            dfx_bin = entry / "dfx"
            if dfx_bin.is_file() and entry.is_dir():
                try:
                    ver = tuple(int(x) for x in entry.name.split("."))
                except ValueError:
                    ver = (0,)
                candidates.append((ver, dfx_bin))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return str(candidates[0][1])
    return None


def _find_dfx_via_shell(home: Path) -> Optional[str]:
    """Find dfx via a login shell (picks up PATH from .zshrc/.bashrc)."""
    import shutil
    for shell in [
        shutil.which("zsh") or "/bin/zsh",
        shutil.which("bash") or "/bin/bash",
    ]:
        if not Path(shell).exists():
            continue
        try:
            r = subprocess.run(
                [shell, "-l", "-c", "which dfx 2>/dev/null || command -v dfx 2>/dev/null"],
                capture_output=True, text=True, timeout=5,
            )
            found = r.stdout.strip()
            if found and Path(found).is_file():
                return found
        except Exception:
            continue
    return None


def check_dfx() -> str:
    """
    Verify dfx is installed and return its path.
    Search order:
      1. Current PATH (shutil.which)
      2. DFX_INSTALL_ROOT environment variable
      3. Common install locations (macOS / Linux)
      4. ~/.cache/dfinity/versions/ (latest version)
      5. Login shell (reads PATH from .zshrc / .bashrc)
    Exits with a friendly message if not found.
    """
    import shutil as _shutil
    import os as _os

    # 1. Current process PATH
    found = _shutil.which("dfx")
    if found:
        return found

    home = Path.home()

    # 2. DFX_INSTALL_ROOT env var (custom install directory)
    install_root = _os.environ.get("DFX_INSTALL_ROOT")
    if install_root:
        p = Path(install_root) / "dfx"
        if p.is_file():
            return str(p)

    # 3. Common static paths (macOS + Linux)
    common: list[Path] = [
        home / "Library" / "Application Support" / "org.dfinity.dfx" / "bin" / "dfx",
        home / ".local" / "bin" / "dfx",
        Path("/opt/homebrew/bin/dfx"),
        Path("/usr/local/bin/dfx"),
        Path("/usr/bin/dfx"),
        Path("/snap/bin/dfx"),
        Path("/nix/var/nix/profiles/default/bin/dfx"),
    ]
    for p in common:
        if p.is_file():
            return str(p)

    # 4. ~/.cache/dfinity/versions/<latest>/dfx
    cached = _find_dfx_in_cache(home)
    if cached:
        return cached

    # 5. Login shell fallback
    via_shell = _find_dfx_via_shell(home)
    if via_shell:
        return via_shell

    print(
        "❌ dfx not found — please install the IC SDK:\n"
        "   sh -ci \"$(curl -fsSL https://internetcomputer.org/install.sh)\"",
    )
    sys.exit(1)


def dfx_identity_principal(dfx: str) -> str:
    """Return the principal of the current dfx identity."""
    result = subprocess.run(
        [dfx, "identity", "get-principal"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"❌ Failed to get dfx identity principal: {result.stderr.strip()}")
        sys.exit(1)
    principal = result.stdout.strip()
    if principal == "2vxsx-fae":  # anonymous principal
        print(
            "❌ Current dfx identity is anonymous — cannot execute swaps.\n"
            "   Create and select an identity first: dfx identity new mywallet && dfx identity use mywallet",
        )
        sys.exit(1)
    return principal


def dfx_call(dfx: str, canister: str, method: str, args: str, step_label: str) -> dict[str, Any]:
    """
    Call dfx canister call --network ic.
    Returns {"ok": True, "output": str, "value": int|None} or {"ok": False, "error": str}.
    Checks both the process exit code and Candid err variants in the response.
    """
    import re
    cmd = [dfx, "canister", "call", "--network", "ic", canister, method, args]
    print(f"  ⏳ {step_label}...", end=" ", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        print("failed")
        return {"ok": False, "error": stderr or stdout}

    # Check for Candid err variants in the response
    # Common formats: (variant { err = variant { InsufficientFunds = ... } })
    #                 (variant { Err = "..." })
    if re.search(r'variant\s*\{\s*[Ee]rr\b', stdout):
        print("failed")
        err_match = re.search(r'variant\s*\{\s*[Ee]rr\s*=\s*(.+?)(?:\s*\})+\s*\)', stdout, re.DOTALL)
        err_detail = err_match.group(1).strip() if err_match else stdout
        return {"ok": False, "error": f"canister returned error: {err_detail}"}

    # Extract ok value (nat integer): variant { ok = 12345 } or (12345 : nat)
    value: Optional[int] = None
    m = re.search(r'[Oo]k\s*=\s*([\d_]+)', stdout)
    if m:
        value = int(m.group(1).replace("_", ""))
    else:
        m2 = re.search(r'\(\s*([\d_]+)\s*:', stdout)
        if m2:
            value = int(m2.group(1).replace("_", ""))

    print("ok")
    return {"ok": True, "output": stdout, "value": value}


# ─── ICPSwap API ──────────────────────────────────────────────────────────────

def _run_query(pair: str) -> list[dict[str, Any]]:
    """Call query_icpswap.py and return the matching pool list (JSON)."""
    result = subprocess.run(
        [sys.executable, str(QUERY_SCRIPT), "--pair", pair, "--json", "--limit", "1"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout) or []
    except json.JSONDecodeError:
        return []


def fetch_pool(from_sym: str, to_sym: str) -> dict[str, Any]:
    """Fetch pool info from query_icpswap.py (JSON). Tries both directions."""
    pools = _run_query(f"{from_sym}/{to_sym}")
    if not pools:
        pools = _run_query(f"{to_sym}/{from_sym}")
    if not pools:
        print(f"❌ No active pool found for {from_sym}/{to_sym}")
        sys.exit(1)
    return pools[0]


def fetch_token_info(ledger_id: str, symbol: str) -> dict[str, int]:
    """
    Fetch token decimals and transfer_fee.
    Checks KNOWN_TOKEN_INFO first; falls back to the ICPSwap token API.
    """
    if symbol.upper() in KNOWN_TOKEN_INFO:
        return KNOWN_TOKEN_INFO[symbol.upper()]

    try:
        req = urllib.request.Request(
            f"{TOKEN_URL}/{ledger_id}",
            headers={"Accept": "application/json", "User-Agent": "openclaw-icpswap-swap/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
        if payload.get("code") != 200:
            raise ValueError(f"API code={payload.get('code')}")
        data = payload["data"]
        decimals = int(data.get("decimals", 8))
        # transFee is returned in human-readable form (e.g. "0.0001"); convert to integer units
        trans_fee_raw = data.get("transFee", "0.0001")
        transfer_fee = int(float(trans_fee_raw) * (10 ** decimals))
        print(f"  📋 {symbol}: decimals={decimals}, transfer_fee={transfer_fee}")
        return {"decimals": decimals, "transfer_fee": transfer_fee}
    except Exception as exc:
        # Refuse to use a wrong default (decimals=8 would cause a 10^10 error for 18-decimal tokens)
        print(f"\n❌ Cannot fetch token info for {symbol} ({ledger_id}): {exc}")
        print(f"   Check your network connection, or manually add the token to KNOWN_TOKEN_INFO.")
        raise SystemExit(1)


def to_base_units(amount: float, decimals: int) -> int:
    """Convert a human-readable amount to on-chain base units (integer)."""
    return int(round(amount * (10 ** decimals)))


def from_base_units(amount_int: int, decimals: int) -> float:
    """Convert on-chain base units to a human-readable amount."""
    return amount_int / (10 ** decimals)


def fetch_balance(dfx: str, ledger_id: str, principal: str, decimals: int) -> Optional[float]:
    """
    Query token balance via icrc1_balance_of. Returns human-readable amount.
    Returns None on failure (does not interrupt the flow).
    """
    args = f"(record {{ owner = principal \"{principal}\"; subaccount = null }})"
    result = subprocess.run(
        [dfx, "canister", "call", "--network", "ic", ledger_id, "icrc1_balance_of", args],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return None
    import re
    # Response format: (1_234_567_890 : nat) or (1234567890 : nat)
    m = re.search(r"\((\d[\d_]*)\s*:", result.stdout)
    if not m:
        return None
    raw = int(m.group(1).replace("_", ""))
    return from_base_units(raw, decimals)


def fetch_pool_canister_token_addresses(dfx: str, pool_id: str) -> Optional[tuple[str, str]]:
    """
    Query SwapPool contract metadata to get the canonical (token0_address, token1_address).
    The contract determines token0/token1 by lexicographic canister ID order,
    which may differ from the API response order.
    """
    import re
    result = subprocess.run(
        [dfx, "canister", "call", "--network", "ic", "--query", pool_id, "metadata", "()"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout
    # Format: token0 = record { address = "mxzaz-..."; standard = "ICRC1" };
    #         token1 = record { address = "ryjl3-..."; standard = "ICRC1" };
    t0 = re.search(r'token0\s*=\s*record\s*\{[^}]*address\s*=\s*"([^"]+)"', stdout)
    t1 = re.search(r'token1\s*=\s*record\s*\{[^}]*address\s*=\s*"([^"]+)"', stdout)
    if t0 and t1:
        return t0.group(1), t1.group(1)
    return None


def fetch_ledger_fee_live(dfx: str, ledger_id: str) -> Optional[int]:
    """
    Query the current transfer fee directly from the ledger canister via icrc1_fee().
    Returns fee in base units, or None on failure.
    Always use this instead of KNOWN_TOKEN_INFO when executing on-chain transactions —
    pool contracts validate that the fee you pass matches their own cached value,
    which must agree with the real ledger fee.
    """
    import re
    result = subprocess.run(
        [dfx, "canister", "call", "--network", "ic", "--query", ledger_id, "icrc1_fee", "()"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return None
    m = re.search(r'\(\s*([\d_]+)\s*(?::\s*nat)?\s*\)', result.stdout)
    if m:
        return int(m.group(1).replace("_", ""))
    return None


def fetch_pool_balance(dfx: str, pool_id: str, principal: str) -> Optional[dict[str, int]]:
    """
    Query the user's unclaimed balance in the SwapPool internal account.
    Returns {"balance0": int, "balance1": int} in base units, or None on failure.
    """
    import re
    result = subprocess.run(
        [dfx, "canister", "call", "--network", "ic", pool_id,
         "getUserUnusedBalance", f"(principal \"{principal}\")"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return None
    b0 = re.search(r'balance0\s*=\s*([\d_]+)', result.stdout)
    b1 = re.search(r'balance1\s*=\s*([\d_]+)', result.stdout)
    if b0 and b1:
        return {
            "balance0": int(b0.group(1).replace("_", "")),
            "balance1": int(b1.group(1).replace("_", "")),
        }
    return None


def do_withdraw(
    dfx: str,
    pool_id: str,
    ledger_id: str,
    amount_base: int,
    transfer_fee: int,
    symbol: str,
) -> bool:
    """Execute a single withdraw call. Returns True on success."""
    args = (
        f"(record {{ "
        f"token = \"{ledger_id}\"; "
        f"amount = {amount_base}; "
        f"fee = {transfer_fee} "
        f"}})"
    )
    r = dfx_call(dfx, pool_id, "withdraw", args, f"withdraw {symbol}")
    if not r["ok"]:
        print(f"❌ withdraw failed: {r['error']}")
        return False
    return True


def format_balance_line(balance: Optional[float], symbol: str, amount: float) -> str:
    """Format a balance line; appends a warning if balance is insufficient."""
    if balance is None:
        return f"💼 {symbol} balance:  unavailable"
    line = f"💼 {symbol} balance:  {format_amount(balance, symbol)}"
    if balance < amount:
        line += "  ⚠️  insufficient balance!"
    return line


# ─── Swap logic ───────────────────────────────────────────────────────────────

def determine_zero_for_one(pool: dict[str, Any], from_ledger: str) -> bool:
    """
    zeroForOne = True means selling token0 for token1.
    Determined by whether pool's token0LedgerId matches our from_ledger.
    """
    return pool["token0LedgerId"] == from_ledger


def fetch_on_chain_quote(
    dfx: str,
    pool_id: str,
    zero_for_one: bool,
    amount_e8s: int,
) -> Optional[int]:
    """
    Get an accurate on-chain price quote via the SwapPool query method.
    Note: the method is called 'quote', not 'quoteExactInput'.
    Returns the expected output in base units, or None on failure.
    """
    import re
    args = (
        f"(record {{ "
        f"zeroForOne = {'true' if zero_for_one else 'false'}; "
        f"amountIn = \"{amount_e8s}\"; "
        f"amountOutMinimum = \"0\" "
        f"}})"
    )
    result = subprocess.run(
        [dfx, "canister", "call", "--network", "ic", "--query",
         pool_id, "quote", args],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return None
    if re.search(r'variant\s*\{\s*[Ee]rr\b', result.stdout):
        return None
    m = re.search(r'[Oo]k\s*=\s*([\d_]+)', result.stdout)
    if m:
        return int(m.group(1).replace("_", ""))
    return None


def estimate_output(pool: dict[str, Any], from_ledger: str, amount_in: float) -> Optional[float]:
    """Estimate output from REST API pool price (preview only; execution uses on-chain quote)."""
    try:
        zero_for_one = determine_zero_for_one(pool, from_ledger)
        rate = pool.get("token1PerToken0") if zero_for_one else pool.get("token0PerToken1")
        if rate is None:
            return None
        fee_multiplier = 1.0 - float(pool.get("fee", 3000)) / 1_000_000
        return float(rate) * amount_in * fee_multiplier
    except (TypeError, ValueError, KeyError):
        return None


def format_amount(amount: float, symbol: str) -> str:
    """Format a token amount for display."""
    if amount >= 1000:
        return f"{amount:,.4f} {symbol}"
    elif amount >= 1:
        return f"{amount:.6f} {symbol}"
    else:
        return f"{amount:.8f} {symbol}"


def print_preview(
    from_sym: str,
    to_sym: str,
    amount: float,
    estimated_out: Optional[float],
    min_out: Optional[float],
    slippage: float,
    pool: dict[str, Any],
    from_balance: Optional[float],
    to_balance: Optional[float],
) -> None:
    sep = "─" * 44
    print(f"\n🔄 Swap Preview")
    print(sep)
    print(format_balance_line(from_balance, from_sym, amount))
    print(format_balance_line(to_balance, to_sym, 0))
    print(sep)
    print(f"📤 Send:          {format_amount(amount, from_sym)}")
    if estimated_out is not None:
        print(f"📥 Expected:      {format_amount(estimated_out, to_sym)}")
        print(f"📉 Minimum:       {format_amount(min_out, to_sym)}  (slippage {slippage}%)")
    else:
        print(f"📥 Expected:      unavailable (check pool price)")
    print(f"⚡ Pool fee:      {pool.get('feePercent', 'n/a')}")
    print(f"🔑 Pool:          {pool.get('poolId', 'n/a')}")
    print(sep)
    print("Add --yes to execute the swap.\n")


def _resolve_pool_tokens(
    dfx: str,
    pool: dict[str, Any],
    from_ledger: str,
    to_ledger: str,
    from_info: dict[str, int],
    to_info: dict[str, int],
    from_sym: str,
    to_sym: str,
) -> tuple[str, str, str, str, dict[str, int], dict[str, int]]:
    """
    Resolve the real ledger addresses for balance0/balance1 via pool contract metadata.
    Returns (ledger0, ledger1, sym0, sym1, info0, info1) where 0/1 matches getUserUnusedBalance.
    """
    pool_id = pool["poolId"]
    addrs = fetch_pool_canister_token_addresses(dfx, pool_id)
    if addrs:
        real_ledger0, real_ledger1 = addrs
    else:
        # Fallback: sort lexicographically (matches contract ordering)
        real_ledger0, real_ledger1 = sorted([from_ledger, to_ledger])

    def info_for(ledger: str) -> tuple[str, dict[str, int]]:
        if ledger == from_ledger:
            return from_sym, from_info
        return to_sym, to_info

    sym0, info0 = info_for(real_ledger0)
    sym1, info1 = info_for(real_ledger1)
    return real_ledger0, real_ledger1, sym0, sym1, info0, info1


def _do_pool_withdrawals(
    dfx: str,
    pool_id: str,
    principal: str,
    ledger0: str,
    ledger1: str,
    sym0: str,
    sym1: str,
    info0: dict[str, int],
    info1: dict[str, int],
    bal0: int,
    bal1: int,
) -> bool:
    """Withdraw non-zero balance0/balance1. Returns True if all withdrawals succeeded."""
    any_failed = False
    if bal0 > 0:
        ok = do_withdraw(dfx, pool_id, ledger0, bal0, info0["transfer_fee"], sym0)
        if not ok:
            any_failed = True
    if bal1 > 0:
        ok = do_withdraw(dfx, pool_id, ledger1, bal1, info1["transfer_fee"], sym1)
        if not ok:
            any_failed = True
    return not any_failed


def _handle_withdraw_failure(
    dfx: str,
    pool_id: str,
    principal: str,
    pool: dict[str, Any],
    from_ledger: str,
    to_ledger: str,
    from_info: dict[str, int],
    to_info: dict[str, int],
    from_sym: str,
    to_sym: str,
) -> None:
    """After a withdraw failure: query pool balance and attempt to recover."""
    sep = "─" * 44
    print(f"\n⚠️  Checking pool internal account balance...")
    pool_bal = fetch_pool_balance(dfx, pool_id, principal)
    if pool_bal is None:
        print(f"   Cannot query internal balance. Run manually:")
        print(f"   /icpswap withdraw {from_sym}/{to_sym}")
        return

    bal0, bal1 = pool_bal["balance0"], pool_bal["balance1"]
    ledger0, ledger1, sym0, sym1, info0, info1 = _resolve_pool_tokens(
        dfx, pool, from_ledger, to_ledger, from_info, to_info, from_sym, to_sym
    )

    print(sep)
    print(f"💰 Pool internal account balance:")
    print(f"   {sym0}: {format_amount(from_base_units(bal0, info0['decimals']), sym0)}")
    print(f"   {sym1}: {format_amount(from_base_units(bal1, info1['decimals']), sym1)}")
    print(sep)

    if bal0 == 0 and bal1 == 0:
        print("   Pool internal account is empty.")
        return

    ok = _do_pool_withdrawals(dfx, pool_id, principal,
                               ledger0, ledger1, sym0, sym1, info0, info1,
                               bal0, bal1)
    if not ok:
        print(f"\n   Tokens remain unclaimed. Retry later: /icpswap withdraw {from_sym}/{to_sym}")
    else:
        import time
        print(f"   ⏳ Waiting for on-chain confirmation...", end=" ", flush=True)
        time.sleep(8)
        print("done")

        post_bal = fetch_pool_balance(dfx, pool_id, principal)
        if post_bal is not None:
            remaining = post_bal["balance0"] + post_bal["balance1"]
            if remaining > 0:
                rem0 = from_base_units(post_bal["balance0"], info0["decimals"])
                rem1 = from_base_units(post_bal["balance1"], info1["decimals"])
                print(f"\n⚠️  Withdraw sent but pool still has balance:")
                print(f"   {sym0}: {format_amount(rem0, sym0)}  {sym1}: {format_amount(rem1, sym1)}")
                print(f"   Retry: /icpswap withdraw {from_sym}/{to_sym}")

    # Always show the latest wallet balances for both tokens
    print(f"\n📊 Updated wallet balances")
    print(sep)
    b0 = fetch_balance(dfx, ledger0, principal, info0["decimals"])
    b1 = fetch_balance(dfx, ledger1, principal, info1["decimals"])
    print(f"💼 {sym0:<10} {format_amount(b0, '') if b0 is not None else 'unavailable'}")
    print(f"💼 {sym1:<10} {format_amount(b1, '') if b1 is not None else 'unavailable'}")
    print(sep)


def withdraw_stuck(
    dfx: str,
    pool: dict[str, Any],
    from_ledger: str,
    to_ledger: str,
    from_info: dict[str, int],
    to_info: dict[str, int],
    from_sym: str,
    to_sym: str,
    principal: str,
) -> int:
    """
    Query and withdraw tokens stuck in the pool internal account.
    Corresponds to: /icpswap withdraw ICP/ckUSDC
    """
    pool_id = pool["poolId"]
    sep = "─" * 44

    print(f"🔍 Querying {from_sym}/{to_sym} pool internal account balance...")
    pool_bal = fetch_pool_balance(dfx, pool_id, principal)
    if pool_bal is None:
        print("❌ Cannot query pool internal balance")
        return 1

    bal0, bal1 = pool_bal["balance0"], pool_bal["balance1"]
    ledger0, ledger1, sym0, sym1, info0, info1 = _resolve_pool_tokens(
        dfx, pool, from_ledger, to_ledger, from_info, to_info, from_sym, to_sym
    )

    print(sep)
    print(f"💰 Pool internal account balance ({pool_id[:16]}...):")
    print(f"   {sym0:<10} {format_amount(from_base_units(bal0, info0['decimals']), sym0)}")
    print(f"   {sym1:<10} {format_amount(from_base_units(bal1, info1['decimals']), sym1)}")
    print(sep)

    if bal0 == 0 and bal1 == 0:
        print("✅ No stuck balance in pool internal account.")
        return 0

    ok = _do_pool_withdrawals(dfx, pool_id, principal,
                               ledger0, ledger1, sym0, sym1, info0, info1,
                               bal0, bal1)

    import time
    print(f"   ⏳ Waiting for on-chain confirmation...", end=" ", flush=True)
    time.sleep(8)
    print("done")

    if not ok:
        print(f"\n⚠️  Some tokens failed to withdraw. Retry: /icpswap withdraw {from_sym}/{to_sym}")
    else:
        post_bal = fetch_pool_balance(dfx, pool_id, principal)
        if post_bal is not None:
            remaining = post_bal["balance0"] + post_bal["balance1"]
            if remaining > 0:
                rem0 = from_base_units(post_bal["balance0"], info0["decimals"])
                rem1 = from_base_units(post_bal["balance1"], info1["decimals"])
                print(f"\n⚠️  Withdraw sent but pool still has balance:")
                print(f"   {sym0}: {format_amount(rem0, sym0)}  {sym1}: {format_amount(rem1, sym1)}")
                print(f"   Retry: /icpswap withdraw {from_sym}/{to_sym}")
                return 1

    # Always show the latest wallet balances for both tokens
    print(f"\n📊 Updated wallet balances")
    print(sep)
    b0 = fetch_balance(dfx, ledger0, principal, info0["decimals"])
    b1 = fetch_balance(dfx, ledger1, principal, info1["decimals"])
    print(f"💼 {sym0:<10} {format_amount(b0, '') if b0 is not None else 'unavailable'}")
    print(f"💼 {sym1:<10} {format_amount(b1, '') if b1 is not None else 'unavailable'}")
    print(sep)
    return 0


def query_balance(
    dfx: str,
    pool: dict[str, Any],
    from_ledger: str,
    to_ledger: str,
    from_info: dict[str, int],
    to_info: dict[str, int],
    from_sym: str,
    to_sym: str,
    principal: str,
) -> int:
    """
    Display:
    1. Wallet balances (both tokens)
    2. Pool internal account (getUserUnusedBalance)
    """
    pool_id = pool["poolId"]
    sep = "─" * 44

    # ── Wallet balances ───────────────────────────────
    print(f"💼 Wallet balances")
    print(sep)
    w0 = fetch_balance(dfx, from_ledger, principal, from_info["decimals"])
    w1 = fetch_balance(dfx, to_ledger,   principal, to_info["decimals"])
    print(f"   {from_sym:<10} {format_amount(w0, from_sym) if w0 is not None else 'unavailable'}")
    print(f"   {to_sym:<10} {format_amount(w1, to_sym)   if w1 is not None else 'unavailable'}")
    print(sep)

    # ── Pool internal account ─────────────────────────
    print(f"\n🏊 Pool internal account  ({pool_id[:16]}...)")
    print(sep)
    pool_bal = fetch_pool_balance(dfx, pool_id, principal)
    if pool_bal is None:
        print("   Unavailable (check dfx and network connection)")
    else:
        ledger0, ledger1, sym0, sym1, info0r, info1r = _resolve_pool_tokens(
            dfx, pool, from_ledger, to_ledger, from_info, to_info, from_sym, to_sym
        )
        bal0 = pool_bal["balance0"]
        bal1 = pool_bal["balance1"]
        v0 = from_base_units(bal0, info0r["decimals"])
        v1 = from_base_units(bal1, info1r["decimals"])
        print(f"   {sym0:<10} {format_amount(v0, sym0)}")
        print(f"   {sym1:<10} {format_amount(v1, sym1)}")
        if bal0 > 0 or bal1 > 0:
            print(f"\n   ⚠️  Stuck balance detected. Run: /icpswap withdraw {from_sym}/{to_sym}")
    print(sep)
    return 0


def execute_swap(
    dfx: str,
    pool: dict[str, Any],
    from_ledger: str,
    to_ledger: str,
    from_info: dict[str, int],
    to_info: dict[str, int],
    amount: float,
    min_out: float,
    from_sym: str,
    to_sym: str,
    principal: str,
    args_slippage: float = 0.5,
) -> None:
    pool_id = pool["poolId"]
    zero_for_one = determine_zero_for_one(pool, from_ledger)

    amount_e8s = to_base_units(amount, from_info["decimals"])
    from_fee = from_info["transfer_fee"]
    to_fee = to_info["transfer_fee"]
    approve_amount = amount_e8s + from_fee
    min_out_e8s = to_base_units(min_out, to_info["decimals"])

    sep = "─" * 44
    print(f"\n🔄 Executing swap: {format_amount(amount, from_sym)} → {to_sym}")
    print(f"   Pool: {pool_id}")
    bal_from = fetch_balance(dfx, from_ledger, principal, from_info["decimals"])
    bal_to   = fetch_balance(dfx, to_ledger,   principal, to_info["decimals"])
    print(format_balance_line(bal_from, from_sym, amount))
    print(format_balance_line(bal_to,   to_sym,   0))
    if bal_from is not None and bal_from < amount:
        print(f"❌ Insufficient {from_sym}: need {format_amount(amount, from_sym)}, have {format_amount(bal_from, from_sym)}")
        sys.exit(1)
    print()

    # ── Auto-clear any stuck pool balance first ───────────────────────────────
    pool_bal = fetch_pool_balance(dfx, pool_id, principal)
    if pool_bal and (pool_bal["balance0"] + pool_bal["balance1"]) > 0:
        ledger0, ledger1, sym0, sym1, info0r, info1r = _resolve_pool_tokens(
            dfx, pool, from_ledger, to_ledger, from_info, to_info, from_sym, to_sym
        )
        v0 = from_base_units(pool_bal["balance0"], info0r["decimals"])
        v1 = from_base_units(pool_bal["balance1"], info1r["decimals"])
        print(f"⚠️  Stuck pool balance detected: {sym0} {format_amount(v0, '')}  {sym1} {format_amount(v1, '')}")
        print(f"   Auto-withdrawing first...")
        _handle_withdraw_failure(
            dfx=dfx, pool_id=pool_id, principal=principal,
            pool=pool,
            from_ledger=from_ledger, to_ledger=to_ledger,
            from_info=from_info, to_info=to_info,
            from_sym=from_sym, to_sym=to_sym,
        )
        print()

    # ── Step 1: Get on-chain quote, calculate amountOutMinimum ───────────────
    print(f"  📡 Fetching on-chain quote...", end=" ", flush=True)
    quoted_out = fetch_on_chain_quote(dfx, pool_id, zero_for_one, amount_e8s)
    if quoted_out is not None:
        min_out_e8s = int(quoted_out * (1.0 - args_slippage / 100.0))
        quoted_human = from_base_units(quoted_out, to_info["decimals"])
        min_human    = from_base_units(min_out_e8s, to_info["decimals"])
        print(f"expected {format_amount(quoted_human, to_sym)}, minimum {format_amount(min_human, to_sym)}")
    else:
        min_out_e8s = to_base_units(min_out, to_info["decimals"])
        print(f"failed, using REST API estimate (may be inaccurate)")

    # ── Step 2: icrc2_approve ─────────────────────────────────────────────────
    approve_args = (
        f"(record {{ "
        f"spender = record {{ owner = principal \"{pool_id}\"; subaccount = null }}; "
        f"amount = {approve_amount} "
        f"}})"
    )
    r1 = dfx_call(dfx, from_ledger, "icrc2_approve", approve_args,
                  "Step 1/2 — icrc2_approve (authorize pool to debit)")
    if not r1["ok"]:
        print(f"❌ Approval failed: {r1['error']}")
        sys.exit(1)

    # ── Step 3: depositFromAndSwap (deposit + swap + withdraw in one call) ────
    # Pool auto-refunds on slippage failure — no manual withdraw needed
    one_step_args = (
        f"(record {{ "
        f"zeroForOne = {'true' if zero_for_one else 'false'}; "
        f"amountIn = \"{amount_e8s}\"; "
        f"amountOutMinimum = \"{min_out_e8s}\"; "
        f"tokenInFee = {from_fee}; "
        f"tokenOutFee = {to_fee} "
        f"}})"
    )
    r2 = dfx_call(dfx, pool_id, "depositFromAndSwap", one_step_args,
                  "Step 2/2 — depositFromAndSwap (deposit + swap + withdraw)")
    if not r2["ok"]:
        err_msg = r2["error"]
        is_slippage = "slippage" in err_msg.lower()
        if is_slippage:
            print(f"❌ Swap failed (slippage exceeded): price moved more than {args_slippage}% from quote")
            print(f"   ✅ Tokens have been automatically refunded by the pool")
            print(f"   Retry with higher slippage: /icpswap swap {from_sym} {amount} {to_sym} --slippage 1 --yes")
        else:
            print(f"❌ Swap failed: {err_msg}")
            print(f"\n🔄 Checking for stuck pool balance...")
            _handle_withdraw_failure(
                dfx=dfx, pool_id=pool_id, principal=principal,
                pool=pool,
                from_ledger=from_ledger, to_ledger=to_ledger,
                from_info=from_info, to_info=to_info,
                from_sym=from_sym, to_sym=to_sym,
            )
        sys.exit(1)

    actual_out_e8s = r2.get("value") or min_out_e8s
    actual_out = from_base_units(actual_out_e8s, to_info["decimals"])
    print(f"\n🎉 Swap complete! {format_amount(amount, from_sym)} → {format_amount(actual_out, to_sym)}")

    import time
    time.sleep(2)
    print("\n📊 Updated balances")
    print(sep)
    new_from_bal = fetch_balance(dfx, from_ledger, principal, from_info["decimals"])
    new_to_bal   = fetch_balance(dfx, to_ledger,   principal, to_info["decimals"])
    print(f"💼 {from_sym:<10} {format_amount(new_from_bal, '') if new_from_bal is not None else 'unavailable'}")
    print(f"💼 {to_sym:<10} {format_amount(new_to_bal,   '') if new_to_bal   is not None else 'unavailable'}")
    print(sep)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()

    from_sym = args.from_token.upper()
    to_sym = args.to_token.upper()

    # Verify dfx
    dfx = check_dfx()

    # Verify identity
    principal = dfx_identity_principal(dfx)
    print(f"👤 Identity: {principal[:20]}...{principal[-6:]}")

    # Fetch pool
    print(f"🔍 Looking up {from_sym}/{to_sym} pool...", end=" ", flush=True)
    pool = fetch_pool(from_sym, to_sym)
    print(f"found {pool['pair']} ({pool['poolId'][:12]}...)")

    # Determine which token is token0 / token1
    if pool["token0Symbol"].upper() == from_sym:
        from_ledger = pool["token0LedgerId"]
        to_ledger = pool["token1LedgerId"]
    elif pool["token1Symbol"].upper() == from_sym:
        from_ledger = pool["token1LedgerId"]
        to_ledger = pool["token0LedgerId"]
    else:
        # Loose match fallback
        from_ledger = pool["token0LedgerId"]
        to_ledger = pool["token1LedgerId"]
        from_sym = pool["token0Symbol"]
        to_sym = pool["token1Symbol"]

    # Fetch token info (decimals + transfer fees)
    from_info = fetch_token_info(from_ledger, from_sym)
    to_info = fetch_token_info(to_ledger, to_sym)

    # --balance-only: query and display balances only
    if args.balance_only:
        return query_balance(
            dfx=dfx, pool=pool,
            from_ledger=from_ledger, to_ledger=to_ledger,
            from_info=from_info, to_info=to_info,
            from_sym=from_sym, to_sym=to_sym,
            principal=principal,
        )

    # --withdraw-only: recover stuck pool balance only
    if args.withdraw_only:
        return withdraw_stuck(
            dfx=dfx, pool=pool,
            from_ledger=from_ledger, to_ledger=to_ledger,
            from_info=from_info, to_info=to_info,
            from_sym=from_sym, to_sym=to_sym,
            principal=principal,
        )

    # Fetch balances for preview and pre-execution display
    print(f"💼 Fetching balances...", end=" ", flush=True)
    from_balance = fetch_balance(dfx, from_ledger, principal, from_info["decimals"])
    to_balance   = fetch_balance(dfx, to_ledger,   principal, to_info["decimals"])
    print("done")

    # Estimate output and minimum output
    estimated_out = estimate_output(pool, from_ledger, args.amount)
    min_out: Optional[float] = None
    if estimated_out is not None:
        min_out = estimated_out * (1.0 - args.slippage / 100.0)

    if not args.yes:
        # Preview only
        print_preview(from_sym, to_sym, args.amount, estimated_out, min_out, args.slippage, pool,
                      from_balance, to_balance)
        return 0

    # min_out is required for execution
    if min_out is None:
        print("❌ Cannot estimate output amount — check pool price data")
        return 1

    execute_swap(
        dfx=dfx,
        pool=pool,
        from_ledger=from_ledger,
        to_ledger=to_ledger,
        from_info=from_info,
        to_info=to_info,
        amount=args.amount,
        min_out=min_out,
        from_sym=from_sym,
        to_sym=to_sym,
        principal=principal,
        args_slippage=args.slippage,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
