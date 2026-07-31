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
from decimal import Decimal

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

BASE = "https://x402.obolpay.xyz"
ENDPOINT = f"{BASE}/api/v1/protected-data?types=openunit"
UA = {"User-Agent": "x402-openunit/1.0"}   # Cloudflare blocks empty/raw-urllib UAs — always send one
RPC = "https://mainnet.base.org"

# Seatbelts. The advertised price is 0.01 USDC; anything wildly above that means the quote is not
# what this script was written for, and the right move is to stop rather than to sign.
MAX_SPEND_USDC = Decimal(os.environ.get("X402_MAX_SPEND_USDC", "1.00"))
MAX_SPEND_UNITS = int(MAX_SPEND_USDC.scaleb(6))
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CHAIN_ID = 8453


def main():
    pk = os.environ["X402_AGENT_PRIVATE_KEY"]
    acct = Account.from_key(pk)
    w3 = Web3(Web3.HTTPProvider(RPC))
    print(f"agent wallet: {acct.address}")

    # 1) Discover: 402 challenge (also carries a FREE preview — evaluate before paying)
    r = requests.get(ENDPOINT, headers=UA, timeout=30)
    if r.status_code != 402:
        raise SystemExit(f"expected a 402 challenge, got {r.status_code}: {r.text[:200]}")
    ch = r.json()["payment"]
    token = Web3.to_checksum_address(ch["token_contract"])
    to = Web3.to_checksum_address(ch["recipient"])

    # A 402 quote is a PRICE, not an authorization. Check it before signing — the amount, the
    # token and the chain all arrive from the server, and this script has your private key.
    # Decimal rather than float: money should not be scaled through binary floating point.
    amount = int(Decimal(str(ch["amount"])).scaleb(6))     # USDC has 6 decimals
    if not 0 < amount <= MAX_SPEND_UNITS:
        raise SystemExit(f"refusing quote of {ch['amount']} USDC (cap {MAX_SPEND_USDC})")
    if token.lower() != USDC_BASE.lower():
        raise SystemExit(f"refusing to transfer {token}: not Base USDC")
    if ch.get("chain_id") != CHAIN_ID:
        raise SystemExit(f"challenge names chain {ch.get('chain_id')}, expected {CHAIN_ID}")

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
    #    allow_redirects=False: requests strips only `Authorization` when a redirect changes host,
    #    so a 302 would hand this payment signature to whatever host the response names.
    headers = {**UA, "X-Payment-Invoice-ID": invoice, "X-Payment-Tx-Hash": txh, "X-Payment-Signature": sig}
    for _ in range(10):
        rr = requests.get(ENDPOINT, headers=headers, timeout=30, allow_redirects=False)
        if rr.status_code == 200:
            break
        if rr.status_code == 402 and rr.json().get("retryable"):
            time.sleep(3); continue
        raise SystemExit(f"unlock failed: {rr.status_code} {rr.text[:200]}\n  tx_hash: {txh}")
    else:
        # The retries ran out while the payment was still 'retryable'. Falling through to
        # rr.json() here read the last 402 body and died on a KeyError two lines later, which
        # buried the tx hash — the only reference to USDC that has already left the wallet.
        raise SystemExit(f"still unverified after 10 attempts, but the payment IS on-chain.\n"
                         f"  tx_hash: {txh}\n"
                         f"  re-run the unlock step with this hash to claim the data.")
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
