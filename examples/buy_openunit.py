#!/usr/bin/env python3
"""
Buy today's `openunit` from x402.obolpay.xyz with ONE signed USDC micropayment on Base.

openunit = a population-weighted unit of account (the SDR method, weighted by *people*),
valued at today's ECB reference FX, and byte-for-byte reproducible (verify the hash yourself).

Setup:
    pip install web3 eth-account requests
    export X402_AGENT_PRIVATE_KEY=0x...   # a Base wallet with >= 0.01 USDC + a little ETH for gas
    python buy_openunit.py

What it does: 402 -> pay 0.01 USDC -> sign EIP-191 binding -> unlock data -> verify the receipt.
"""
import os
import time

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

BASE = "https://x402.obolpay.xyz"
ENDPOINT = f"{BASE}/api/v1/protected-data?types=openunit"
UA = {"User-Agent": "x402-openunit/1.0"}   # Cloudflare blocks empty/raw-urllib UAs — always send one
RPC = "https://mainnet.base.org"


def main():
    pk = os.environ["X402_AGENT_PRIVATE_KEY"]
    acct = Account.from_key(pk)
    w3 = Web3(Web3.HTTPProvider(RPC))
    print(f"agent wallet: {acct.address}")

    # 1) Discover: 402 challenge (also carries a FREE preview — evaluate before paying)
    r = requests.get(ENDPOINT, headers=UA, timeout=30)
    assert r.status_code == 402, r.status_code
    ch = r.json()["payment"]
    token = Web3.to_checksum_address(ch["token_contract"])
    to = Web3.to_checksum_address(ch["recipient"])
    amount = int(round(float(ch["amount"]) * 10 ** 6))     # USDC has 6 decimals
    domain, invoice = ch["signature_scheme"]["domain"], ch["invoice_id"]
    print(f"quote: {ch['amount']} {ch['token']} -> {to}  (invoice {invoice[:8]}…)")

    # 2) Pay: standard ERC-20 USDC transfer to the merchant
    erc20 = w3.eth.contract(address=token, abi=[{"name": "transfer", "type": "function",
        "stateMutability": "nonpayable", "inputs": [{"name": "to", "type": "address"},
        {"name": "amount", "type": "uint256"}], "outputs": [{"type": "bool"}]}])
    tx = erc20.functions.transfer(to, amount).build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": 8453, "gas": 90000, "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(0.01, "gwei")})
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    txh = w3.eth.send_raw_transaction(raw).hex()
    if not txh.startswith("0x"):
        txh = "0x" + txh
    print(f"paid: {txh}  (waiting for mining…)")
    w3.eth.wait_for_transaction_receipt(txh, timeout=120)

    # 3) Bind the payment: sign EIP-191 "x402:{domain}:{invoice_id}:{tx_hash}"
    #    (the signer must equal the on-chain sender — this stops anyone reusing your tx)
    msg = f"x402:{domain}:{invoice}:{txh.lower()}"
    sig = Account.sign_message(encode_defunct(text=msg), pk).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig

    # 4) Unlock: re-send with the payment headers (retry a few times while the tx propagates)
    headers = {**UA, "X-Payment-Invoice-ID": invoice, "X-Payment-Tx-Hash": txh, "X-Payment-Signature": sig}
    for _ in range(10):
        rr = requests.get(ENDPOINT, headers=headers, timeout=30)
        if rr.status_code == 200:
            break
        if rr.status_code == 402 and rr.json().get("retryable"):
            time.sleep(3); continue
        raise SystemExit(f"unlock failed: {rr.status_code} {rr.text[:200]}")
    data = rr.json()
    ou = data["data"]["items"][-1]
    print(f"\n=== openunit (as of ECB {ou['ecb_valuation_date']}) ===")
    print(f"  1 openunit = {ou['value_usd_display']} {ou['numeraire']}   [{ou['method']}]")
    print(f"  reproducible hash: {ou['artifact_hash']}")

    # 5) Verify the signed proof-of-purchase (anyone can, independently)
    rc = data.get("receipt")
    if rc:
        v = requests.post(f"{BASE}/verify-receipt", json={"message": rc["message"],
            "signature": rc["signature"]}, headers=UA, timeout=15).json()
        print(f"  receipt verified by server signer: {v.get('valid')} ({v.get('server_signer','')[:10]}…)")


if __name__ == "__main__":
    main()
