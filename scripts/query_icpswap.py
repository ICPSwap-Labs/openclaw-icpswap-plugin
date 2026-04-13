#!/usr/bin/env python3

import argparse
import json
import sys
import urllib.request
from typing import Any, Optional


API_URL = "https://api.icpswap.com/info/pool/all"
TOKEN_URL = "https://api.icpswap.com/info/token"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search ICPSwap pools by pair, token, or free text."
    )
    parser.add_argument("--pair", help='Pair symbol, for example "ICP/ckBTC".')
    parser.add_argument(
        "--token",
        action="append",
        default=[],
        help="Token symbol, name, or ledger canister ID. Repeat to provide two tokens.",
    )
    parser.add_argument(
        "--query",
        help="Free-text query such as a symbol, canister ID, or pair fragment.",
    )
    parser.add_argument("--limit", type=int, default=10, help="Maximum results to print.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print matching pools as JSON instead of a text table.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print the best match as a compact market summary.",
    )
    args = parser.parse_args()

    if not args.pair and not args.token and not args.query:
        parser.error("Provide at least one of --pair, --token, or --query.")

    return args


def fetch_token_change(ledger_id: str) -> Optional[float]:
    """Fetch 24h price change % for a token via /info/token/{ledgerId}."""
    try:
        req = urllib.request.Request(
            f"{TOKEN_URL}/{ledger_id}",
            headers={"Accept": "application/json", "User-Agent": "openclaw-icpswap-pairs/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
        if payload.get("code") != 200:
            return None
        return number_value(payload["data"].get("priceChange24H"))
    except Exception:
        return None


def fetch_pools() -> list[dict[str, Any]]:
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "openclaw-icpswap-pairs/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if payload.get("code") != 200 or not isinstance(payload.get("data"), list):
        raise RuntimeError(f"Unexpected ICPSwap response: {payload!r}")
    return payload["data"]


def normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def tokenize(value: str) -> list[str]:
    cleaned = value.replace("/", " ").replace("-", " ")
    return [part for part in (normalize(piece) for piece in cleaned.split()) if part]


def pool_pair(pool: dict[str, Any]) -> str:
    return f'{pool.get("token0Symbol", "")}/{pool.get("token1Symbol", "")}'


def pool_terms(pool: dict[str, Any]) -> set[str]:
    terms = {
        normalize(pool.get("poolId")),
        normalize(pool.get("token0Symbol")),
        normalize(pool.get("token1Symbol")),
        normalize(pool.get("token0Name")),
        normalize(pool.get("token1Name")),
        normalize(pool.get("token0LedgerId")),
        normalize(pool.get("token1LedgerId")),
        normalize(pool_pair(pool)),
    }
    return {term for term in terms if term}


def pair_matches(pool: dict[str, Any], pair: str) -> tuple[bool, int]:
    parts = [part for part in tokenize(pair) if part]
    if len(parts) != 2:
        return False, 0

    symbols = [normalize(pool.get("token0Symbol")), normalize(pool.get("token1Symbol"))]
    names = [normalize(pool.get("token0Name")), normalize(pool.get("token1Name"))]
    ledgers = [normalize(pool.get("token0LedgerId")), normalize(pool.get("token1LedgerId"))]
    exact_terms = set(symbols + names + ledgers)

    if parts[0] in exact_terms and parts[1] in exact_terms:
        return True, 100

    text = " ".join(sorted(pool_terms(pool)))
    if all(part in text for part in parts):
        return True, 60

    return False, 0


def token_match_score(pool: dict[str, Any], token: str) -> int:
    needle = normalize(token)
    if not needle:
        return 0

    exact_fields = [
        normalize(pool.get("token0Symbol")),
        normalize(pool.get("token1Symbol")),
        normalize(pool.get("token0Name")),
        normalize(pool.get("token1Name")),
        normalize(pool.get("token0LedgerId")),
        normalize(pool.get("token1LedgerId")),
    ]
    if needle in exact_fields:
        return 40

    if any(needle in field for field in exact_fields):
        return 20

    return 0


def query_match_score(pool: dict[str, Any], query: str) -> int:
    parts = tokenize(query)
    if not parts:
        return 0

    terms = pool_terms(pool)
    normalized_query = normalize(query)
    if normalized_query in terms:
        return 50

    text = " ".join(sorted(terms))

    if all(part in text for part in parts):
        return 10 + len(parts)

    return 0


def score_pool(pool: dict[str, Any], args: argparse.Namespace) -> int:
    score = 0

    if args.pair:
        matched, pair_score = pair_matches(pool, args.pair)
        if not matched:
            return 0
        score += pair_score

    if args.token:
        for token in args.token:
            token_score = token_match_score(pool, token)
            if token_score == 0:
                return 0
            score += token_score

    if args.query:
        query_score = query_match_score(pool, args.query)
        if query_score == 0:
            return 0
        score += query_score

    return score


def tvl_value(pool: dict[str, Any]) -> float:
    try:
        return float(pool.get("tvlUSD", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def number_value(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def fee_percent(pool_fee: Any) -> str:
    try:
        return f"{float(pool_fee) / 10000:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def simplify_pool(pool: dict[str, Any], score: int) -> dict[str, Any]:
    token0_price = number_value(pool.get("token0Price"))
    token1_price = number_value(pool.get("token1Price"))
    price_token0_in_token1 = None
    price_token1_in_token0 = None
    if token0_price and token1_price:
        price_token0_in_token1 = token0_price / token1_price
        price_token1_in_token0 = token1_price / token0_price

    return {
        "score": score,
        "pair": pool_pair(pool),
        "poolId": pool.get("poolId"),
        "fee": pool.get("poolFee"),
        "feePercent": fee_percent(pool.get("poolFee")),
        "token0Symbol": pool.get("token0Symbol"),
        "token1Symbol": pool.get("token1Symbol"),
        "token0Name": pool.get("token0Name"),
        "token1Name": pool.get("token1Name"),
        "token0LedgerId": pool.get("token0LedgerId"),
        "token1LedgerId": pool.get("token1LedgerId"),
        "token0Price": pool.get("token0Price"),
        "token1Price": pool.get("token1Price"),
        "token0PerToken1": price_token1_in_token0,
        "token1PerToken0": price_token0_in_token1,
        "tvlUSD": pool.get("tvlUSD"),
        "volumeUSD24H": pool.get("volumeUSD24H"),
        "txCount24H": pool.get("txCount24H"),
    }


def format_float(value: Optional[float], decimals: int = 6) -> str:
    if value is None:
        return "n/a"
    formatted = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return formatted or "0"


def fmt_usd(value: Any) -> str:
    """Format a USD amount with commas and 2 decimal places."""
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def format_change(pct: Optional[float]) -> str:
    if pct is None:
        return "n/a"
    arrow = "▲" if pct >= 0 else "▼"
    sign  = "+" if pct >= 0 else ""
    return f"{arrow} {sign}{pct:.2f}%"


def format_summary(item: dict[str, Any]) -> str:
    t0 = item["token0Symbol"]
    t1 = item["token1Symbol"]
    p0 = format_float(item["token1PerToken0"], 4)
    p1 = format_float(item["token0PerToken1"], 4)

    change = fetch_token_change(item["token0LedgerId"])
    change_str = format_change(change)

    sep = "─" * 38
    lines = [
        f'💱 {item["pair"]}  ·  ⚡ {item["feePercent"]}',
        sep,
        f'💰 1 {t0:<8} = {p0} {t1}',
        f'   1 {t1:<8} = {p1} {t0}',
        f'🔄 24h Δ     {change_str}',
        f'💧 TVL       {fmt_usd(item["tvlUSD"])}',
        f'📈 24h Vol   {fmt_usd(item["volumeUSD24H"])}',
        sep,
        f'🔑 {item["poolId"]}',
    ]
    return "\n".join(lines)


def format_table(results: list[dict[str, Any]]) -> str:
    headers = ["💱 pair", "🔑 poolId", "⚡ fee", "💧 TVL", "📈 24h Vol", "score"]
    rows = []
    for item in results:
        rows.append(
            [
                str(item["pair"]),
                str(item["poolId"]),
                str(item["feePercent"]),
                fmt_usd(item["tvlUSD"]),
                fmt_usd(item["volumeUSD24H"]),
                str(item["score"]),
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    lines = []
    lines.append("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    try:
        pools = fetch_pools()
    except Exception as exc:
        print(f"Failed to fetch ICPSwap pools: {exc}", file=sys.stderr)
        return 1

    matches = []
    for pool in pools:
        score = score_pool(pool, args)
        if score > 0:
            matches.append((score, tvl_value(pool), simplify_pool(pool, score)))

    matches.sort(key=lambda item: (-item[0], -item[1], item[2]["pair"]))
    results = [item[2] for item in matches[: max(args.limit, 1)]]

    if args.json:
        json.dump(results, sys.stdout, indent=2, ensure_ascii=True)
        sys.stdout.write("\n")
        return 0

    if not results:
        print("No matching ICPSwap pools found.")
        return 0

    if args.summary:
        print(format_summary(results[0]))
        return 0

    print(format_table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
