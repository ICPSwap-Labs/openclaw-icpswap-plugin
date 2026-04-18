import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));
const QUERY_SCRIPT     = path.join(PLUGIN_DIR, 'scripts', 'query_icpswap.py');
const SWAP_SCRIPT      = path.join(PLUGIN_DIR, 'scripts', 'swap_icpswap.py');
const LIQUIDITY_SCRIPT = path.join(PLUGIN_DIR, 'scripts', 'liquidity_icpswap.py');
const TXS_SCRIPT       = path.join(PLUGIN_DIR, 'scripts', 'txs_icpswap.py');

// ─── Script runners ───────────────────────────────────────────────────────────

function runSwapScript(args: string[], timeoutMs = 90_000): string {
  const result = spawnSync('python3', [SWAP_SCRIPT, ...args], {
    cwd: PLUGIN_DIR,
    encoding: 'utf8',
    timeout: timeoutMs,
    env: { ...process.env, DFX_WARNING: '-mainnet_plaintext_identity' },
  });
  if (result.error) return `Error: ${result.error.message}`;
  const stdout = (result.stdout ?? '').trim();
  const stderr = (result.stderr ?? '').trim();
  if (result.status !== 0) {
    const msg = [stdout, stderr].filter(Boolean).join('\n') || `exit code ${result.status}`;
    return `ICPSwap operation failed:\n${msg}`;
  }
  return [stdout, stderr].filter(Boolean).join('\n') || 'No output.';
}

function runQueryScript(args: string[]): string {
  const result = spawnSync('python3', [QUERY_SCRIPT, ...args], {
    cwd: PLUGIN_DIR,
    encoding: 'utf8',
    timeout: 30_000,
  });
  if (result.error) return `Error: ${result.error.message}`;
  if (result.status !== 0) {
    return `ICPSwap query failed: ${(result.stderr ?? '').trim() || `exit code ${result.status}`}`;
  }
  return (result.stdout ?? '').trim() || 'No results found.';
}

function runTxsScript(args: string[]): string {
  const result = spawnSync('python3', [TXS_SCRIPT, ...args], {
    cwd: PLUGIN_DIR,
    encoding: 'utf8',
    timeout: 30_000,
  });
  if (result.error) return `Error: ${result.error.message}`;
  if (result.status !== 0) {
    return `ICPSwap transactions query failed: ${(result.stderr ?? '').trim() || `exit code ${result.status}`}`;
  }
  return (result.stdout ?? '').trim() || 'No transactions found.';
}

function runLiquidityScript(args: string[], timeoutMs = 120_000): string {
  const result = spawnSync('python3', [LIQUIDITY_SCRIPT, ...args], {
    cwd: PLUGIN_DIR,
    encoding: 'utf8',
    timeout: timeoutMs,
    env: { ...process.env, DFX_WARNING: '-mainnet_plaintext_identity' },
  });
  if (result.error) return `Error: ${result.error.message}`;
  const stdout = (result.stdout ?? '').trim();
  const stderr = (result.stderr ?? '').trim();
  if (result.status !== 0) {
    const msg = [stdout, stderr].filter(Boolean).join('\n') || `exit code ${result.status}`;
    return `ICPSwap liquidity operation failed:\n${msg}`;
  }
  return [stdout, stderr].filter(Boolean).join('\n') || 'No output.';
}

// ─── Slash command helpers ────────────────────────────────────────────────────

function queryICPSwap(raw: string): string {
  let args: string[];
  if (!raw || raw === '--help') {
    args = ['--help'];
  } else if (raw.startsWith('--')) {
    args = raw.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [raw];
  } else if (raw.includes('/')) {
    args = ['--pair', raw, '--summary'];
  } else {
    args = ['--query', raw, '--summary'];
  }
  return runQueryScript(args);
}

function swapICPSwap(raw: string): string {
  let args: string[];
  if (!raw || raw === '--help') {
    args = ['--help'];
  } else if (raw.startsWith('--')) {
    args = raw.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [raw];
  } else {
    const parts = raw.trim().split(/\s+/);
    const positional: string[] = [];
    const flags: string[] = [];
    for (let i = 0; i < parts.length; i++) {
      if (parts[i].startsWith('--')) {
        flags.push(parts[i]);
        if (i + 1 < parts.length && !parts[i + 1].startsWith('--')) {
          flags.push(parts[++i]);
        }
      } else {
        positional.push(parts[i]);
      }
    }
    if (positional.length >= 3) {
      const slippageArgs = positional.length >= 4 ? ['--slippage', positional[3]] : [];
      args = ['--from', positional[0], '--amount', positional[1], '--to', positional[2],
              ...slippageArgs, ...flags];
    } else {
      args = raw.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [raw];
    }
  }
  return runSwapScript(args);
}

function parsePair(raw: string, cmd: string): { args: string; error?: string } {
  const pair = raw.slice(cmd.length).trim();
  const parts = pair.includes('/') ? pair.split('/') : pair.split(/\s+/);
  if (parts.length < 2 || !parts[0] || !parts[1]) {
    return { args: '', error: `Usage: /icpswap ${cmd} FROM/TO  e.g. /icpswap ${cmd} ICP/ckUSDC` };
  }
  return { args: `--from ${parts[0].trim()} --to ${parts[1].trim()}` };
}

/**
 * Normalise a token pair from a slash-command remainder string.
 * Accepts "ICP/ckUSDC" or space-separated "ICP ckUSDC".
 * Returns { pair: "ICP/ckUSDC", flags: [...remaining tokens] }
 * or { error: string } when no pair tokens are present.
 */
function normalizeLiquidityPair(
  rest: string,
  cmd: string,
): { pair: string; flags: string[] } | { error: string } {
  const parts = rest.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [];
  if (!parts.length) {
    return { error: `Usage: /icpswap ${cmd} TOKEN0/TOKEN1  e.g. /icpswap ${cmd} ICP/ckUSDC` };
  }
  if (parts[0].includes('/')) {
    return { pair: parts[0], flags: parts.slice(1) };
  }
  // Space-separated: second token must not be a flag
  if (parts[1] && !parts[1].startsWith('-')) {
    return { pair: `${parts[0]}/${parts[1]}`, flags: parts.slice(2) };
  }
  // Single token — pass as-is; Python will give a clear error if it's wrong
  return { pair: parts[0], flags: parts.slice(1) };
}

/**
 * Parse slash-command args for the liquidity script.
 * Passes the subcommand + remaining raw args directly.
 *   "positions ICP/ckUSDC"  →  ['positions', 'ICP/ckUSDC']
 *   "add ICP ckUSDC --amount0 10 --amount1 125 --yes"  →  ['add', 'ICP', 'ckUSDC', ...]
 *   "remove ICP/ckUSDC --position-id 42 --yes"  →  ['remove', 'ICP/ckUSDC', ...]
 */
function liquidityICPSwap(raw: string): string {
  if (!raw || raw === '--help') return runLiquidityScript(['--help']);
  const tokens = raw.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [raw];
  return runLiquidityScript(tokens);
}

// ─── Plugin entry ─────────────────────────────────────────────────────────────

export default {
  id: 'icpswap',
  name: 'ICPSwap',
  description: 'ICPSwap DEX: query pool prices, execute token swaps, manage liquidity positions on the Internet Computer.',
  register(api: any) {

    // ── AI-callable tools ──────────────────────────────────────────────────────

    /**
     * Balance query tool: wallet balance + unclaimed pool balance.
     * Called by the AI when answering "how much ICP/ckUSDC do I have" type questions.
     */
    api.registerTool({
      name: 'icpswap_balance',
      description: 'Query wallet balances and unclaimed ICPSwap pool internal balances for a token pair. Use this to answer questions like "how much X do I have" or "what is my balance".',
      parameters: {
        type: 'object',
        properties: {
          from: {
            type: 'string',
            description: 'First token symbol, e.g. ICP, ckBTC',
          },
          to: {
            type: 'string',
            description: 'Second token symbol, e.g. ckUSDC, ckETH',
          },
        },
        required: ['from', 'to'],
      },
      async execute(_id: string, params: { from: string; to: string }) {
        const result = runSwapScript([
          '--from', params.from,
          '--to', params.to,
          '--balance-only',
        ]);
        return { content: [{ type: 'text', text: result }] };
      },
    });

    /**
     * Swap preview tool: get a quote without executing.
     * The AI calls this before asking the user to confirm execution.
     */
    api.registerTool({
      name: 'icpswap_quote',
      description: 'Get an ICPSwap swap quote (preview only, no execution). Use this to answer "how much Y can I get for X" or "swap preview" questions. After showing the quote, ask the user to confirm before executing.',
      parameters: {
        type: 'object',
        properties: {
          from: {
            type: 'string',
            description: 'Token to sell, e.g. ICP',
          },
          amount: {
            type: 'number',
            description: 'Amount to sell, e.g. 0.1',
          },
          to: {
            type: 'string',
            description: 'Token to buy, e.g. ckUSDC',
          },
          slippage: {
            type: 'number',
            description: 'Maximum slippage tolerance in percent, default 0.5',
          },
        },
        required: ['from', 'amount', 'to'],
      },
      async execute(_id: string, params: { from: string; amount: number; to: string; slippage?: number }) {
        const args = [
          '--from', params.from,
          '--amount', String(params.amount),
          '--to', params.to,
        ];
        if (params.slippage != null) args.push('--slippage', String(params.slippage));
        // No --yes flag: preview only
        const result = runSwapScript(args);
        return { content: [{ type: 'text', text: result }] };
      },
    });

    /**
     * Execute swap tool: actually performs the on-chain swap (financial side effects, marked optional).
     * The AI must only call this after the user explicitly confirms.
     */
    api.registerTool(
      {
        name: 'icpswap_execute_swap',
        description: 'Execute a token swap on ICPSwap. ⚠️ This transfers real assets on-chain. Only call after the user explicitly confirms. Always show a quote with icpswap_quote first.',
        parameters: {
          type: 'object',
          properties: {
            from: {
              type: 'string',
              description: 'Token to sell, e.g. ICP',
            },
            amount: {
              type: 'number',
              description: 'Amount to sell, e.g. 0.1',
            },
            to: {
              type: 'string',
              description: 'Token to buy, e.g. ckUSDC',
            },
            slippage: {
              type: 'number',
              description: 'Maximum slippage tolerance in percent, default 0.5',
            },
          },
          required: ['from', 'amount', 'to'],
        },
        async execute(_id: string, params: { from: string; amount: number; to: string; slippage?: number }) {
          const args = [
            '--from', params.from,
            '--amount', String(params.amount),
            '--to', params.to,
            '--yes',  // actually execute
          ];
          if (params.slippage != null) args.push('--slippage', String(params.slippage));
          const result = runSwapScript(args);
          return { content: [{ type: 'text', text: result }] };
        },
      },
      { optional: true },  // financial side effects — user must explicitly enable in config
    );

    /**
     * Withdraw stuck balance tool.
     */
    api.registerTool({
      name: 'icpswap_withdraw',
      description: 'Withdraw tokens stuck in the ICPSwap pool internal account (e.g. residual balance after a failed swap). Use for "withdraw", "recover", or "claim" requests.',
      parameters: {
        type: 'object',
        properties: {
          from: {
            type: 'string',
            description: 'Token A, e.g. ICP',
          },
          to: {
            type: 'string',
            description: 'Token B, e.g. ckUSDC',
          },
        },
        required: ['from', 'to'],
      },
      async execute(_id: string, params: { from: string; to: string }) {
        const result = runSwapScript([
          '--from', params.from,
          '--to', params.to,
          '--withdraw-only',
        ]);
        return { content: [{ type: 'text', text: result }] };
      },
    });

    /**
     * LP positions query tool.
     */
    api.registerTool({
      name: 'icpswap_positions',
      description: 'List the user\'s active LP (liquidity provider) positions for an ICPSwap pool. Shows position ID, tick range, liquidity, and any uncollected fees. Use for "show my positions", "list LP positions", "my liquidity" requests.',
      parameters: {
        type: 'object',
        properties: {
          from: {
            type: 'string',
            description: 'First token of the pair, e.g. ICP',
          },
          to: {
            type: 'string',
            description: 'Second token of the pair, e.g. ckUSDC',
          },
        },
        required: ['from', 'to'],
      },
      async execute(_id: string, params: { from: string; to: string }) {
        const result = runLiquidityScript(['positions', `${params.from}/${params.to}`]);
        return { content: [{ type: 'text', text: result }] };
      },
    });

    /**
     * Add liquidity preview tool (no execution).
     */
    api.registerTool({
      name: 'icpswap_liquidity_preview',
      description: 'Preview adding liquidity to an ICPSwap pool (no execution). Shows the amounts, tick range, and slippage. Use this before icpswap_add_liquidity to let the user review the details. Use for "add liquidity preview", "how much liquidity can I add" requests.',
      parameters: {
        type: 'object',
        properties: {
          from: {
            type: 'string',
            description: 'First token, e.g. ICP',
          },
          to: {
            type: 'string',
            description: 'Second token, e.g. ckUSDC',
          },
          amount0: {
            type: 'number',
            description: 'Amount of the first token to deposit',
          },
          amount1: {
            type: 'number',
            description: 'Amount of the second token to deposit (estimated from pool price if omitted)',
          },
          slippage: {
            type: 'number',
            description: 'Slippage tolerance in percent, default 1.0',
          },
        },
        required: ['from', 'to', 'amount0'],
      },
      async execute(_id: string, params: {
        from: string; to: string; amount0: number; amount1?: number; slippage?: number;
      }) {
        const args = ['add', params.from, params.to, '--amount0', String(params.amount0)];
        if (params.amount1 != null) args.push('--amount1', String(params.amount1));
        if (params.slippage != null) args.push('--slippage', String(params.slippage));
        // No --yes: preview only
        const result = runLiquidityScript(args);
        return { content: [{ type: 'text', text: result }] };
      },
    });

    /**
     * Add liquidity execution tool (financial side effects, optional).
     */
    api.registerTool(
      {
        name: 'icpswap_add_liquidity',
        description: 'Add liquidity to an ICPSwap pool. ⚠️ Transfers real assets on-chain. Always show a preview with icpswap_liquidity_preview first and only call this after explicit user confirmation. Creates a full-range LP position by default.',
        parameters: {
          type: 'object',
          properties: {
            from: {
              type: 'string',
              description: 'First token, e.g. ICP',
            },
            to: {
              type: 'string',
              description: 'Second token, e.g. ckUSDC',
            },
            amount0: {
              type: 'number',
              description: 'Amount of the first token to deposit',
            },
            amount1: {
              type: 'number',
              description: 'Amount of the second token to deposit (estimated from pool price if omitted)',
            },
            slippage: {
              type: 'number',
              description: 'Slippage tolerance in percent, default 1.0',
            },
          },
          required: ['from', 'to', 'amount0'],
        },
        async execute(_id: string, params: {
          from: string; to: string; amount0: number; amount1?: number; slippage?: number;
        }) {
          const args = ['add', params.from, params.to, '--amount0', String(params.amount0), '--yes'];
          if (params.amount1 != null) args.push('--amount1', String(params.amount1));
          if (params.slippage != null) args.push('--slippage', String(params.slippage));
          const result = runLiquidityScript(args);
          return { content: [{ type: 'text', text: result }] };
        },
      },
      { optional: true },
    );

    /**
     * Remove liquidity execution tool (financial side effects, optional).
     */
    api.registerTool(
      {
        name: 'icpswap_remove_liquidity',
        description: 'Remove liquidity from an ICPSwap LP position. ⚠️ Transfers real assets on-chain. Always show positions with icpswap_positions first and only call this after explicit user confirmation.',
        parameters: {
          type: 'object',
          properties: {
            from: {
              type: 'string',
              description: 'First token of the pair, e.g. ICP',
            },
            to: {
              type: 'string',
              description: 'Second token of the pair, e.g. ckUSDC',
            },
            position_id: {
              type: 'number',
              description: 'Position ID to remove (uses the first position if omitted)',
            },
            percent: {
              type: 'number',
              description: 'Percentage of the position to remove, 1–100 (default 100)',
            },
            slippage: {
              type: 'number',
              description: 'Slippage tolerance in percent, default 1.0',
            },
          },
          required: ['from', 'to'],
        },
        async execute(_id: string, params: {
          from: string; to: string; position_id?: number; percent?: number; slippage?: number;
        }) {
          const args = ['remove', `${params.from}/${params.to}`, '--yes'];
          if (params.position_id != null) args.push('--position-id', String(params.position_id));
          if (params.percent != null) args.push('--percent', String(params.percent));
          if (params.slippage != null) args.push('--slippage', String(params.slippage));
          const result = runLiquidityScript(args);
          return { content: [{ type: 'text', text: result }] };
        },
      },
      { optional: true },
    );

    /**
     * Recent transactions query tool (read-only, no dfx).
     */
    api.registerTool({
      name: 'icpswap_transactions',
      description: 'List recent transactions (swaps, liquidity events, fee claims) on an ICPSwap pool. Use for "recent swaps", "latest trades", "transaction history", "pool activity" questions.',
      parameters: {
        type: 'object',
        properties: {
          from: {
            type: 'string',
            description: 'First token of the pair, e.g. ICP',
          },
          to: {
            type: 'string',
            description: 'Second token of the pair, e.g. ckUSDC',
          },
          limit: {
            type: 'number',
            description: 'Number of transactions to return (default 10, max recommended 50)',
          },
          action_type: {
            type: 'string',
            description: 'Filter by action type(s), comma-separated. Valid: Swap, AddLiquidity, DecreaseLiquidity, Claim',
          },
          principal: {
            type: 'string',
            description: 'Filter by a user principal ID (returns only that user\'s transactions)',
          },
        },
        required: ['from', 'to'],
      },
      async execute(_id: string, params: {
        from: string; to: string; limit?: number; action_type?: string; principal?: string;
      }) {
        const args = [`${params.from}/${params.to}`];
        if (params.limit != null) args.push('--limit', String(params.limit));
        if (params.action_type) args.push('--type', params.action_type);
        if (params.principal) args.push('--principal', params.principal);
        const result = runTxsScript(args);
        return { content: [{ type: 'text', text: result }] };
      },
    });

    // ── Slash commands (bypass the model, execute directly) ───────────────────

    api.registerCommand({
      name: 'icpswap',
      description:
        'ICPSwap: price | balance | swap | withdraw | positions | add-liquidity | remove-liquidity | txs',
      acceptsArgs: true,
      handler: async (ctx: any) => {
        const raw = (ctx.args ?? '').trim();

        // txs ICP/ckUSDC [--limit N] [--type Swap] [--principal ID]
        // (also accepts: txs ICP ckUSDC ...)
        if (raw === 'txs' || raw.startsWith('txs ')) {
          const rest = raw.slice('txs'.length).trim();
          const tokens = rest.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) ?? [];
          if (!tokens.length) {
            return { text: 'Usage: /icpswap txs TOKEN0/TOKEN1 [--limit N] [--type Swap]' };
          }
          // Normalize "ICP ckUSDC" → "ICP/ckUSDC"
          if (!tokens[0].includes('/') && tokens[1] && !tokens[1].startsWith('-')) {
            tokens.splice(0, 2, `${tokens[0]}/${tokens[1]}`);
          }
          return { text: runTxsScript(tokens) };
        }

        // balance ICP/ckUSDC
        if (raw === 'balance' || raw.startsWith('balance ')) {
          const { args, error } = parsePair(raw, 'balance');
          if (error) return { text: error };
          return { text: runSwapScript([...args.split(' '), '--balance-only']) };
        }

        // swap FROM AMOUNT TO [--slippage N] [--yes]
        if (raw === 'swap' || raw.startsWith('swap ')) {
          return { text: swapICPSwap(raw.slice(4).trim()) };
        }

        // withdraw ICP/ckUSDC
        if (raw === 'withdraw' || raw.startsWith('withdraw ')) {
          const { args, error } = parsePair(raw, 'withdraw');
          if (error) return { text: error };
          return { text: runSwapScript([...args.split(' '), '--withdraw-only']) };
        }

        // positions ICP/ckUSDC  (also accepts: positions ICP ckUSDC)
        if (raw === 'positions' || raw.startsWith('positions ')) {
          const rest = raw.slice('positions'.length).trim();
          const parsed = normalizeLiquidityPair(rest, 'positions');
          if ('error' in parsed) return { text: parsed.error };
          return { text: liquidityICPSwap(['positions', parsed.pair, ...parsed.flags].join(' ')) };
        }

        // add-liquidity ICP ckUSDC --amount0 10 [--amount1 125] [--yes]
        if (raw === 'add-liquidity' || raw.startsWith('add-liquidity ')) {
          const rest = raw.slice('add-liquidity'.length).trim();
          return { text: liquidityICPSwap('add ' + rest) };
        }

        // remove-liquidity ICP/ckUSDC [--position-id N] [--percent 50] [--yes]
        // (also accepts: remove-liquidity ICP ckUSDC ...)
        if (raw === 'remove-liquidity' || raw.startsWith('remove-liquidity ')) {
          const rest = raw.slice('remove-liquidity'.length).trim();
          const parsed = normalizeLiquidityPair(rest, 'remove-liquidity');
          if ('error' in parsed) return { text: parsed.error };
          return { text: liquidityICPSwap(['remove', parsed.pair, ...parsed.flags].join(' ')) };
        }

        // default: price query
        return { text: queryICPSwap(raw) };
      },
    });
  },
};
