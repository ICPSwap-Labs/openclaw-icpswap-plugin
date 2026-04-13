import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));
const QUERY_SCRIPT = path.join(PLUGIN_DIR, 'scripts', 'query_icpswap.py');
const SWAP_SCRIPT = path.join(PLUGIN_DIR, 'scripts', 'swap_icpswap.py');

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

// ─── Plugin entry ─────────────────────────────────────────────────────────────

export default {
  id: 'icpswap',
  name: 'ICPSwap',
  description: 'ICPSwap DEX: query pool prices, execute token swaps, check balances on the Internet Computer.',
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

    // ── Slash commands (bypass the model, execute directly) ───────────────────

    api.registerCommand({
      name: 'icpswap',
      description:
        'ICPSwap: /icpswap balance ICP/ckUSDC  |  /icpswap swap FROM AMOUNT TO [--yes]  |  /icpswap withdraw FROM/TO',
      acceptsArgs: true,
      handler: async (ctx: any) => {
        const raw = (ctx.args ?? '').trim();

        if (raw === 'balance' || raw.startsWith('balance ')) {
          const { args, error } = parsePair(raw, 'balance');
          if (error) return { text: error };
          return { text: runSwapScript([...args.split(' '), '--balance-only']) };
        }

        if (raw === 'swap' || raw.startsWith('swap ')) {
          return { text: swapICPSwap(raw.slice(4).trim()) };
        }

        if (raw === 'withdraw' || raw.startsWith('withdraw ')) {
          const { args, error } = parsePair(raw, 'withdraw');
          if (error) return { text: error };
          return { text: runSwapScript([...args.split(' '), '--withdraw-only']) };
        }

        return { text: queryICPSwap(raw) };
      },
    });
  },
};
