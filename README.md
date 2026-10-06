# obolpay-x402-mcp

[![Listed on Glama](https://glama.ai/mcp/servers/@Hiroshi-Ichiyanagi/obolpay-x402-mcp/badges/score.svg)](https://glama.ai/mcp/servers/@Hiroshi-Ichiyanagi/obolpay-x402-mcp)

<a href="https://glama.ai/mcp/servers/@Hiroshi-Ichiyanagi/obolpay-x402-mcp">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@Hiroshi-Ichiyanagi/obolpay-x402-mcp/badge" alt="obolpay-x402-mcp MCP server" />
</a>

> **Status (2026-10-06): maintenance.** Since 2026-09-24 the Obolpay gateway accepts payments only through the standard x402 `exact` scheme (EIP-3009 `transferWithAuthorization`). This package and the buy examples still use the older pay-then-unlock flow, so **they cannot buy at the moment** — they stop before sending any USDC, because the gateway no longer quotes a recipient for that flow. Prepaid top-ups (`topup()`) ended on 2026-09-24 as well. `discover()`, `preview()` and `verify_receipt()` still work. To buy, use a standard x402 client against https://pay.obolpay.xyz (the gateway's own reference client: https://pay.obolpay.xyz/client.py).

> 📖 **Tutorial** (written for the older prepaid flow — see the status note above): [Let your AI agent autonomously buy data — x402 + gasless prepaid + MCP on Base](https://dev.to/hiroshi_ichiyanagi/let-your-ai-agent-autonomously-buy-data-x402-gasless-prepaid-mcp-on-base-8l8)


MCP server for the **live [Obolpay x402 gateway](https://pay.obolpay.xyz)** — pay-per-call premium data for AI agents, settled in **USDC on Base mainnet** via HTTP 402. Business use only (see the gateway's [terms](https://pay.obolpay.xyz/terms)).

Give any MCP-compatible agent (Claude Desktop, etc.) the ability to **discover → evaluate (free preview) → verify a signed receipt** (buying: see the status note above).

## 🚀 Copy-paste examples (start here) → [`examples/`](examples/)

No MCP needed to try it. **Zero setup, no wallet, stdlib only:**
```bash
python3 examples/quickstart_preview.py   # evaluate the data QUALITY for free (trustless preview)
```
The buy examples ([`buy_openunit.py`](examples/buy_openunit.py), [`agent_tool.py`](examples/agent_tool.py)) use the older
pay-then-unlock flow and **cannot buy since 2026-09-24** (see the status note). Full walkthrough: [`examples/README.md`](examples/README.md).

## Tools

| Tool | What it does | Status (2026-10-06) |
|---|---|---|
| `discover()` | Machine-readable service manifest (price, token, network, capabilities) | works |
| `preview()` | Free data sample + price from the 402 challenge (no spend) | works |
| `purchase()` | One-shot on-chain pay-and-fetch | **cannot buy since 2026-09-24** (stops before paying) |
| `topup(tx_hash)` | Fund a prepaid balance from a USDC deposit (once) | **ended 2026-09-24** (the gateway answers 410) |
| `balance(address)` | Balance, calls remaining, next nonce | works — the balance now holds only SLA and warranty credits |
| `spend_gasless()` | **Instant, gasless** pay-per-call from the prepaid balance (no on-chain tx/gas) | spends SLA and warranty credits only |
| `verify_receipt(message, signature)` | Third-party verify a proof-of-purchase receipt | works |

## Why it's different

- **Free preview before paying** — evaluate the data, no blind spend.
- **Signed receipts** — EIP-191 proof-of-purchase, verifiable at `/verify-receipt`.
- **Freshness SLA** — stale data earns a credit to a non-refundable, non-transferable account balance at the gateway (spendable via signed vouchers).
- **Delta delivery** (`X-Since-Seq`) — only new items, saves your context tokens.

## Run

```bash
pip install -r requirements.txt          # mcp[cli], web3, eth-account, requests
export X402_AGENT_PRIVATE_KEY=0x...       # a Base wallet with USDC + a little ETH for gas
# optional: export X402_BASE_URL=https://pay.obolpay.xyz   (the default https://x402.obolpay.xyz redirects there)
python x402_mcp_server.py
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "obolpay-x402": {
      "command": "python",
      "args": ["/absolute/path/to/x402_mcp_server.py"],
      "env": { "X402_AGENT_PRIVATE_KEY": "0x..." }
    }
  }
}
```

## Links

- Live endpoint: https://pay.obolpay.xyz (the old host x402.obolpay.xyz redirects here)
- Discovery manifest: https://pay.obolpay.xyz/.well-known/x402
- Reference client (non-MCP, standard `exact` scheme): https://pay.obolpay.xyz/client.py
- Terms · Pricing · Legal: https://pay.obolpay.xyz/terms · https://pay.obolpay.xyz/pricing · https://pay.obolpay.xyz/legal

Built on [x402](https://x402.org) + Base. USDC micropayments for the AI agent economy.
