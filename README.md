# obolpay-x402-mcp

MCP server for the **live [Obolpay x402 gateway](https://x402.obolpay.xyz)** — pay-per-call premium data for AI agents, settled in **USDC on Base mainnet** via HTTP 402.

Give any MCP-compatible agent (Claude Desktop, etc.) the ability to **discover → evaluate (free preview) → pay once → then call GASLESS forever → verify a signed receipt** — autonomously.

## Tools

| Tool | What it does |
|---|---|
| `discover()` | Machine-readable service manifest (price, token, network, capabilities) |
| `preview()` | Free data sample + price from the 402 challenge (no spend) |
| `purchase()` | One-shot on-chain pay-and-fetch |
| `topup(tx_hash)` | Fund a prepaid balance from a USDC deposit (once) |
| `balance(address)` | Balance, calls remaining, next nonce |
| `spend_gasless()` | **Instant, gasless** pay-per-call from the prepaid balance (no on-chain tx/gas) |
| `verify_receipt(message, signature)` | Third-party verify a proof-of-purchase receipt |

## Why it's different

- **Free preview before paying** — evaluate the data, no blind spend.
- **Prepaid GASLESS balance** — deposit USDC once, then unlimited cheap calls via off-chain signed vouchers.
- **Signed receipts** — EIP-191 proof-of-purchase, verifiable at `/verify-receipt`.
- **Freshness SLA + auto-refund** — stale data is auto-refunded to your balance.
- **Delta delivery** (`X-Since-Seq`) — only new items, saves your context tokens.

## Run

```bash
pip install -r requirements.txt          # mcp[cli], web3, eth-account, requests
export X402_AGENT_PRIVATE_KEY=0x...       # a Base wallet with USDC + a little ETH for gas
# optional: export X402_BASE_URL=https://x402.obolpay.xyz   (default)
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

- Live endpoint: https://x402.obolpay.xyz
- Discovery manifest: https://x402.obolpay.xyz/.well-known/x402
- Reference client (non-MCP): https://x402.obolpay.xyz/client.py

Built on [x402](https://x402.org) + Base. USDC micropayments for the AI agent economy.
