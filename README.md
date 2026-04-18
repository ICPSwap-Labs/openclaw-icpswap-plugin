# openclaw-icpswap-plugin

An [OpenClaw](https://openclaw.ai) plugin for interacting with [ICPSwap](https://app.icpswap.com) DEX on the Internet Computer.

## Features

- **Query pool prices** — look up real-time price and market data for any ICPSwap trading pair
- **Swap tokens** — execute on-chain token swaps via the one-step `depositFromAndSwap` flow (ICRC-2)
- **Check balances** — view wallet balances and any unclaimed tokens stuck in the pool internal account
- **Withdraw stuck funds** — recover residual balances left in the pool after a failed or partial swap
- **Add liquidity** — create full-range or concentrated LP positions via `mint`
- **Remove liquidity** — close or partially reduce LP positions and withdraw both tokens
- **Recent transactions** — browse swaps, liquidity events and fee claims on any pool

## Requirements

- [OpenClaw](https://openclaw.ai) gateway running locally
- [dfx CLI](https://internetcomputer.org/docs/current/developer-docs/getting-started/install/) installed and configured with a non-anonymous identity
- Python 3.9+

## Installation

```bash
openclaw plugins install @openclaw/icpswap
```

Then add the plugin to your `openclaw.json`:

```json
{
  "plugins": {
    "allow": ["icpswap"],
    "entries": {
      "icpswap": { "enabled": true, "config": {} }
    }
  },
  "tools": {
    "profile": "messaging",
    "alsoAllow": ["icpswap"]
  }
}
```

To enable the optional tools that transfer real assets (swap execution and liquidity management), also add:

```json
{
  "tools": {
    "alsoAllow": ["icpswap", "icpswap_execute_swap", "icpswap_add_liquidity", "icpswap_remove_liquidity"]
  }
}
```

## Slash commands

| Command | Description |
|---------|-------------|
| `/icpswap ICP/ckUSDC` | Query pool price and market summary |
| `/icpswap balance ICP/ckUSDC` | Show wallet and pool internal balances |
| `/icpswap swap ICP 0.1 ckUSDC` | Preview a swap (no execution) |
| `/icpswap swap ICP 0.1 ckUSDC --yes` | Execute the swap on-chain |
| `/icpswap swap ICP 0.1 ckUSDC --slippage 1.0 --yes` | Swap with custom slippage tolerance |
| `/icpswap withdraw ICP/ckUSDC` | Withdraw stuck pool balance to wallet |
| `/icpswap positions ICP/ckUSDC` | List your active LP positions |
| `/icpswap add-liquidity ICP ckUSDC --amount0 10` | Preview adding liquidity (estimates counterpart amount) |
| `/icpswap add-liquidity ICP ckUSDC --amount0 10 --amount1 125 --yes` | Add liquidity on-chain |
| `/icpswap remove-liquidity ICP/ckUSDC --position-id 42 --yes` | Remove 100% of a position |
| `/icpswap remove-liquidity ICP/ckUSDC --position-id 42 --percent 50 --yes` | Partially remove liquidity |
| `/icpswap txs ICP/ckUSDC` | List latest transactions on a pool |
| `/icpswap txs ICP/ckUSDC --limit 20 --type Swap` | Latest 20 swaps only (also: AddLiquidity, DecreaseLiquidity, Claim) |
| `/icpswap txs ICP/ckUSDC --principal <PID>` | Show only a specific user's transactions on that pool |

## AI tools

When enabled, the plugin exposes tools the AI model can call directly in conversation:

| Tool | Description |
|------|-------------|
| `icpswap_balance` | Query wallet and pool internal balances |
| `icpswap_quote` | Get a swap quote without executing |
| `icpswap_execute_swap` | Execute a swap (optional — requires explicit user confirmation) |
| `icpswap_withdraw` | Withdraw stuck tokens from the pool internal account |
| `icpswap_positions` | List active LP positions for a pool |
| `icpswap_liquidity_preview` | Preview adding liquidity (no execution) |
| `icpswap_add_liquidity` | Add liquidity on-chain (optional — requires explicit user confirmation) |
| `icpswap_remove_liquidity` | Remove liquidity from a position (optional — requires explicit user confirmation) |
| `icpswap_transactions` | List recent transactions on a pool (swaps, liquidity events, fee claims) |

## Swap flow

This plugin uses the ICPSwap **one-step** swap mode:

1. `icrc2_approve` — authorize the SwapPool to debit the input token
2. `depositFromAndSwap` — deposit, swap, and withdraw output token in a single call

If the swap fails due to slippage, the pool automatically refunds the input token. No manual recovery is needed in the normal case.

## Liquidity flow

### Adding liquidity

1. `icrc2_approve` (token0) — authorize the SwapPool to pull token0
2. `icrc2_approve` (token1) — authorize the SwapPool to pull token1
3. `depositFrom` (token0) — move token0 into the pool internal account
4. `depositFrom` (token1) — move token1 into the pool internal account
5. `mint` — create the LP position NFT; any unused tokens are automatically returned to wallet

By default, positions are created as **full-range** (equivalent to Uniswap v2 behaviour). Specify `--tick-lower` and `--tick-upper` for a custom concentrated range.

### Removing liquidity

1. `decreaseLiquidity` — burn liquidity from the position; tokens move to pool internal account
2. `withdraw` (token0) — return token0 to wallet
3. `withdraw` (token1) — return token1 to wallet

Use `--percent` to partially remove (e.g. `--percent 50` to halve a position).

## Transactions query

Read-only; no dfx or local identity needed. Data source: `https://api.icpswap.com/info/transaction/find`.

Supported action types (via `--type`, comma-separated):

| Type | Meaning |
|------|---------|
| `Swap` | Trade between token0 and token1 |
| `AddLiquidity` | Deposit into an LP position |
| `DecreaseLiquidity` | Burn liquidity from an LP position |
| `Claim` | Collect accrued LP fees |

Other filters: `--principal <PID>` (user), `--limit N` (default 10), `--page N` (pagination), `--begin <ms>` / `--end <ms>` (time range in epoch milliseconds), `--pool-id <canister>` (skip pair lookup). Results are newest-first.

Example output:

```
📜 ICP/ckUSDC  ·  ICP $2.5563  ·  5 of 355,366 txs · latest, limit 5
────────────────────────────────────────────────────────────────────────
04-18 14:57  🔁 Swap       0.522574 ICP → 1.3185 ckUSDC  $1.3358  gqe6j-mm…t-rqe
04-18 14:53  🔁 Swap       9.2476 ckUSDC → 3.6432 ICP     $9.369  neom5-2u…i-mae
04-18 14:22  ➖ Remove LP  0.1 ICP + 0.253071 ckUSDC     $0.2577  rwrwc-k3…z-7qe
04-18 14:21  ➕ Add LP     0.1 ICP + 0.253072 ckUSDC     $0.2577  rwrwc-k3…z-7qe
04-18 13:48  🔁 Swap       601.21 ckUSDC → 236.94 ICP    $610.67  d3ynq-da…q-cai
```

## Supported tokens (built-in)

| Token | Decimals | Transfer fee |
|-------|----------|--------------|
| ICP | 8 | 0.0001 ICP |
| ckBTC | 8 | 10 satoshi |
| ckETH | 18 | 0.000002 ETH |
| ckUSDC | 6 | 0.00001 USDC |
| ckUSDT | 6 | 0.00001 USDT |

Other tokens are fetched automatically from the ICPSwap token API.

## License

MIT
