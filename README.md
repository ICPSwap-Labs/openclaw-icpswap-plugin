# openclaw-icpswap-plugin

An [OpenClaw](https://openclaw.ai) plugin for interacting with [ICPSwap](https://app.icpswap.com) DEX on the Internet Computer.

## Features

- **Query pool prices** — look up real-time price and market data for any ICPSwap trading pair
- **Swap tokens** — execute on-chain token swaps via the one-step `depositFromAndSwap` flow (ICRC-2)
- **Check balances** — view wallet balances and any unclaimed tokens stuck in the pool internal account
- **Withdraw stuck funds** — recover residual balances left in the pool after a failed or partial swap

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

To enable the execute-swap tool (which transfers real assets), also add:

```json
{
  "tools": {
    "alsoAllow": ["icpswap"],
    "allow": ["icpswap_execute_swap"]
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

## AI tools

When enabled, the plugin exposes four tools the AI model can call directly in conversation:

| Tool | Description |
|------|-------------|
| `icpswap_balance` | Query wallet and pool internal balances |
| `icpswap_quote` | Get a swap quote without executing |
| `icpswap_execute_swap` | Execute a swap (optional — requires explicit user confirmation) |
| `icpswap_withdraw` | Withdraw stuck tokens from the pool internal account |

## Swap flow

This plugin uses the ICPSwap **one-step** swap mode:

1. `icrc2_approve` — authorize the SwapPool to debit the input token
2. `depositFromAndSwap` — deposit, swap, and withdraw output token in a single call

If the swap fails due to slippage, the pool automatically refunds the input token. No manual recovery is needed in the normal case.

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
