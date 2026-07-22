#!/usr/bin/env python3
"""
Give ANY LLM agent a tool that buys verifiable data with USDC — autonomously.

This is the whole point of x402: your agent decides when a dataset is worth $0.01,
pays, and acts on it — no human in the loop, no API keys to provision.

Two things here:
  1. `get_openunit(paid=False)` — a plain function. paid=False returns the FREE preview
     (evaluate first); paid=True buys and returns today's verifiable value.
  2. A LangChain @tool wrapper (optional) so an agent can call it.

    pip install web3 eth-account requests            # (+ langchain, optional)
    export X402_AGENT_PRIVATE_KEY=0x...               # only needed for paid=True

Swap `?types=openunit` for jp-equity-convergence / llm-equivalence /
ai-trust-artifacts / governance to sell/buy the other datasets.
"""
import os
import requests

BASE = "https://x402.obolpay.xyz"
UA = {"User-Agent": "x402-agent-tool/1.0"}


def get_openunit(paid: bool = False) -> dict:
    """Fetch the population-weighted unit of account.
    paid=False -> free preview (no wallet). paid=True -> one USDC micropayment, full value + hash.
    """
    url = f"{BASE}/api/v1/protected-data?types=openunit"
    if not paid:
        r = requests.get(url, headers=UA, timeout=30)
        prev = r.json()["payment"]["preview"]
        return {"paid": False, "preview": prev,
                "note": "free preview — call with paid=True to unlock the verifiable value"}
    # Paid path reuses the reference flow (see buy_openunit.py for the full, commented version).
    # (full commented flow in buy_openunit.py; inlined here for tool use)
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from web3 import Web3
    import time
    pk = os.environ["X402_AGENT_PRIVATE_KEY"]
    acct = Account.from_key(pk); w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
    ch = requests.get(url, headers=UA, timeout=30).json()["payment"]
    token, to = Web3.to_checksum_address(ch["token_contract"]), Web3.to_checksum_address(ch["recipient"])
    amount = int(round(float(ch["amount"]) * 10 ** 6))
    erc20 = w3.eth.contract(address=token, abi=[{"name": "transfer", "type": "function",
        "stateMutability": "nonpayable", "inputs": [{"name": "to", "type": "address"},
        {"name": "amount", "type": "uint256"}], "outputs": [{"type": "bool"}]}])
    tx = erc20.functions.transfer(to, amount).build_transaction({"from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address), "chainId": 8453, "gas": 90000,
        "maxFeePerGas": w3.eth.gas_price * 2, "maxPriorityFeePerGas": w3.to_wei(0.01, "gwei")})
    s = acct.sign_transaction(tx); raw = getattr(s, "raw_transaction", None) or s.rawTransaction
    txh = w3.eth.send_raw_transaction(raw).hex(); txh = txh if txh.startswith("0x") else "0x" + txh
    w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    msg = f"x402:{ch['signature_scheme']['domain']}:{ch['invoice_id']}:{txh.lower()}"
    sig = Account.sign_message(encode_defunct(text=msg), pk).signature.hex()
    sig = sig if sig.startswith("0x") else "0x" + sig
    hdr = {**UA, "X-Payment-Invoice-ID": ch["invoice_id"], "X-Payment-Tx-Hash": txh, "X-Payment-Signature": sig}
    for _ in range(10):
        rr = requests.get(url, headers=hdr, timeout=30)
        if rr.status_code == 200:
            ou = rr.json()["data"]["items"][-1]
            return {"paid": True, "openunit_usd": ou["value_usd_display"],
                    "ecb_date": ou["ecb_valuation_date"], "artifact_hash": ou["artifact_hash"]}
        if rr.status_code == 402 and rr.json().get("retryable"):
            time.sleep(3); continue
        raise RuntimeError(f"unlock failed: {rr.status_code}")


# --- Optional: expose it to a LangChain agent ---------------------------------
try:
    from langchain_core.tools import tool

    @tool
    def openunit_value(paid: bool = False) -> str:
        """Get the openunit — a population-weighted unit of account valued at today's ECB FX,
        byte-for-byte reproducible. Use paid=False to preview quality for free; paid=True to
        buy today's verifiable value for 0.01 USDC on Base."""
        return str(get_openunit(paid=paid))
except ImportError:
    pass  # langchain not installed — the plain function still works


if __name__ == "__main__":
    print("FREE preview:", get_openunit(paid=False)["preview"]["product"])
    print("\nTo buy (needs X402_AGENT_PRIVATE_KEY on a funded Base wallet):  get_openunit(paid=True)")
