# Buy verifiable data with USDC — in ~10 lines

Give your AI agent the ability to **pay per call for verifiable data** on Base — autonomously.
Live endpoint: **https://x402.obolpay.xyz** · 1 call = **$0.01 USDC** · standard [x402](https://x402.org) protocol.

**The killer feature: a free preview inside the 402.** Your agent evaluates data *quality
before paying* — no blind spend, no hallucinated garbage. *Don't trust me. Verify me.*

---

## 60-second try (zero setup — stdlib only, no wallet)

```bash
python3 quickstart_preview.py
```
```
→ GET /api/v1/protected-data?types=openunit  ->  HTTP 402
=== FREE PREVIEW (no payment) ===
   product : Obolpay Verifiable Data Feed — openunit index + LLM-equivalence + governance
   freshness: signed, data age 156s  (server-attested, EIP-191)
✓ You just evaluated the dataset WITHOUT paying. That's the point: trustless preview.
```

## Buy it (one signed USDC micropayment)

```bash
pip install web3 eth-account requests
export X402_AGENT_PRIVATE_KEY=0x...      # Base wallet with ≥0.01 USDC + a little ETH
python3 buy_openunit.py
```
```
=== openunit (as of ECB 2026-07-21) ===
  1 openunit = 0.984710 USD   [openunit v0.1]
  reproducible hash: sha256:3e1093a4dd97b2b5…      ← recompute it yourself
  receipt verified by server signer: True
```

## Give it to an agent (LangChain, or any LLM)

```python
from agent_tool import get_openunit          # or the @tool wrapper openunit_value

preview = get_openunit(paid=False)           # free: decide if it's worth $0.01
value   = get_openunit(paid=True)            # buy: {'openunit_usd': '0.984710', 'artifact_hash': 'sha256:…'}
```
An agent that "buys openunit and summarizes today's macro" is ~20 lines: preview → decide →
`get_openunit(paid=True)` → feed the value + hash to your LLM. No API keys to provision.

---

## What's for sale (`?types=<name>`)

| category | what you get |
|---|---|
| **openunit** | Population-weighted unit of account (SDR method, by *people*), valued at today's ECB FX. **Byte-for-byte reproducible** (input_digest + artifact_hash). |
| **jp-equity-convergence** | KCS multi-pillar convergence scan of JP equities — top-20 with pillar breakdown. *Informational only.* |
| **llm-equivalence** | Measured local-LLM backend (llama.cpp / MLX / candle) output-equivalence, consistency, swap-latency & memory matrix on Apple Silicon. |
| **ai-trust-artifacts** | Real content-provenance certificates + a 3-trail (model/inference/economic) verified AI supply-chain binding. |
| **governance** | Verified governed-AI spending benchmark (1000 intents, verify=PASS). |

## Habitual use = gasless

Deposit USDC once (`POST /account/topup`), then pay per call with **off-chain signed vouchers** —
no on-chain tx, no gas, instant. Built for high-frequency agents. See `/mcp-server.py` for the
Model Context Protocol server (Claude Desktop etc.).

## Why trust the data
- **Free preview** before you pay (evaluate quality first).
- **Signed receipt** per purchase (EIP-191) — verify at `POST /verify-receipt`.
- **openunit is reproducible** — recompute the SHA-256 yourself and get the same number.
- **Freshness SLA, fail-closed** — stale data is *not served and not charged*.

Machine-readable everything: `/.well-known/x402` · `/llms.txt` · `/openapi.json`
