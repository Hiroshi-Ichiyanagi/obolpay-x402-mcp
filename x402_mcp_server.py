#!/usr/bin/env python3
"""x402 MCP server — expose the Obolpay x402 gateway as native tools for AI agents.

Any MCP-compatible agent (Claude Desktop, etc.) can add this server and then
autonomously DISCOVER -> PREVIEW -> PURCHASE paid data, and VERIFY receipts.

Install & run:
    pip install "mcp[cli]" web3 eth-account requests
    # purchase() needs a funded Base wallet:
    export X402_AGENT_PRIVATE_KEY=0x...        # holds >= the quoted USDC + a little ETH (gas)
    export X402_BASE_URL=https://x402.obolpay.xyz   # optional; this is the default
    python x402_mcp_server.py

Claude Desktop config (claude_desktop_config.json):
    {
      "mcpServers": {
        "x402-obolpay": {
          "command": "python",
          "args": ["/absolute/path/to/x402_mcp_server.py"],
          "env": { "X402_AGENT_PRIVATE_KEY": "0x..." }
        }
      }
    }

Tools:
    discover()                     -> the machine-readable service manifest (free)
    preview()                      -> the free data preview from the 402 challenge (free, no spend)
    purchase()                     -> pay the quoted USDC and return {data, receipt} (spends real USDC)
    verify_receipt(message, sig)   -> third-party verification of a proof-of-purchase receipt
"""
import os
import time

import requests
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("X402_BASE_URL", "https://x402.obolpay.xyz").rstrip("/")
ENDPOINT = BASE + "/api/v1/protected-data"
RPC = os.environ.get("X402_BASE_RPC_URL", "https://mainnet.base.org")
UA = {"User-Agent": "x402-mcp/1.0"}   # send a UA (Cloudflare blocks empty/raw-urllib)

mcp = FastMCP("x402-obolpay")


@mcp.tool()
def discover() -> dict:
    """Return the x402 gateway's machine-readable manifest: price, token, network, recipient,
    payment flow, free-preview and proof-of-purchase capabilities. No payment required."""
    return requests.get(BASE + "/.well-known/x402", headers=UA, timeout=30).json()


@mcp.tool()
def preview() -> dict:
    """Fetch the HTTP 402 challenge and return its FREE preview (a sample of the paid dataset)
    plus the price/invoice, so the agent can decide whether to pay. No payment required."""
    r = requests.get(ENDPOINT, headers=UA, timeout=30)
    if r.status_code != 402:
        return {"error": f"expected 402, got {r.status_code}", "body": r.text[:400]}
    p = r.json().get("payment", {})
    return {
        "preview": p.get("preview"),
        "price": {"amount": p.get("amount"), "token": p.get("token"), "network": p.get("network")},
        "invoice_id": p.get("invoice_id"),
        "recipient": p.get("recipient"),
    }


@mcp.tool()
def purchase() -> dict:
    """Pay the quoted USDC on Base and return the unlocked {data, receipt}.
    Requires env X402_AGENT_PRIVATE_KEY (a Base wallet with >= the quoted USDC + gas).
    WARNING: this spends real USDC on-chain."""
    pk = os.environ.get("X402_AGENT_PRIVATE_KEY")
    if not pk:
        return {"error": "X402_AGENT_PRIVATE_KEY not set; cannot pay."}
    from web3 import Web3
    from eth_account import Account
    from eth_account.messages import encode_defunct

    acct = Account.from_key(pk)
    w3 = Web3(Web3.HTTPProvider(RPC))

    r = requests.get(ENDPOINT, headers=UA, timeout=30)
    if r.status_code != 402:
        return {"error": f"expected 402, got {r.status_code}"}
    ch = r.json()["payment"]
    token = Web3.to_checksum_address(ch["token_contract"])
    to = Web3.to_checksum_address(ch["recipient"])
    amount = int(round(float(ch["amount"]) * 10 ** 6))  # USDC 6 decimals
    domain, invoice = ch["signature_scheme"]["domain"], ch["invoice_id"]

    erc20 = w3.eth.contract(address=token, abi=[{"name": "transfer", "type": "function",
        "stateMutability": "nonpayable", "inputs": [{"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"}], "outputs": [{"type": "bool"}]}])
    tx = erc20.functions.transfer(to, amount).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": ch["chain_id"], "gas": 120000,
        "maxFeePerGas": w3.eth.gas_price * 2, "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei")})
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    txh = w3.eth.send_raw_transaction(raw).hex()
    if not txh.startswith("0x"):
        txh = "0x" + txh
    w3.eth.wait_for_transaction_receipt(txh)

    msg = "x402:" + domain + ":" + invoice + ":" + txh.lower()
    sig = Account.sign_message(encode_defunct(text=msg), pk).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig

    headers = {**UA, "X-Payment-Invoice-ID": invoice, "X-Payment-Tx-Hash": txh, "X-Payment-Signature": sig}
    for _ in range(40):
        rr = requests.get(ENDPOINT, headers=headers, timeout=30)
        if rr.status_code == 200:
            body = rr.json()
            return {"tx_hash": txh, "data": body.get("data"), "receipt": body.get("receipt")}
        if rr.status_code == 402 and rr.json().get("retryable"):
            time.sleep(3)
            continue
        return {"error": f"rejected: {rr.status_code}", "body": rr.text[:400], "tx_hash": txh}
    return {"error": "timed out waiting for verification", "tx_hash": txh}


@mcp.tool()
def verify_receipt(message: str, signature: str) -> dict:
    """Verify a proof-of-purchase receipt with the gateway (recovers the EIP-191 signer and
    checks it equals the server's receipt signer). Anyone can call this — no payment required."""
    return requests.post(BASE + "/verify-receipt", headers={**UA, "Content-Type": "application/json"},
                         json={"message": message, "signature": signature}, timeout=30).json()


# ---- Prepaid gasless balance (for HABITUAL use: deposit once, then gasless calls) ----
def _agent_address() -> str:
    from eth_account import Account
    return Account.from_key(os.environ["X402_AGENT_PRIVATE_KEY"]).address.lower()


@mcp.tool()
def balance(address: str = "") -> dict:
    """Check a prepaid balance: remaining balance, calls_remaining, and next_nonce.
    Pass an address, or leave blank to use the wallet from X402_AGENT_PRIVATE_KEY."""
    addr = (address or "").strip().lower()
    if not addr:
        if not os.environ.get("X402_AGENT_PRIVATE_KEY"):
            return {"error": "provide address or set X402_AGENT_PRIVATE_KEY"}
        addr = _agent_address()
    return requests.get(BASE + "/account/" + addr, headers=UA, timeout=30).json()


@mcp.tool()
def topup(tx_hash: str) -> dict:
    """Credit a prepaid balance from an on-chain USDC deposit to the recipient.
    First send USDC to the recipient on Base, then call this with the tx_hash."""
    return requests.post(BASE + "/account/topup", headers={**UA, "Content-Type": "application/json"},
                         json={"tx_hash": tx_hash}, timeout=60).json()


@mcp.tool()
def spend_gasless() -> dict:
    """Fetch the paid data by drawing from the prepaid balance — GASLESS, NO on-chain tx.
    This is the habitual-use path: after one topup you can call this many times for free (only gas-free
    balance debit). Requires X402_AGENT_PRIVATE_KEY with a funded balance."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    pk = os.environ["X402_AGENT_PRIVATE_KEY"]
    addr = _agent_address()
    acc = requests.get(BASE + "/account/" + addr, headers=UA, timeout=30).json()
    if acc.get("balance_units", 0) < acc.get("price_units", 1):
        return {"error": "insufficient_balance", "account": acc, "hint": "call topup(tx_hash) after depositing USDC"}
    msg = acc["voucher_message"]                      # server-provided message for the current next_nonce
    sig = Account.sign_message(encode_defunct(text=msg), pk).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    headers = {**UA, "X-Account-Address": addr, "X-Account-Nonce": str(acc["next_nonce"]), "X-Account-Voucher": sig}
    return requests.get(ENDPOINT, headers=headers, timeout=30).json()


if __name__ == "__main__":
    mcp.run()
