#!/usr/bin/env python3
"""x402 MCP server — expose the Obolpay x402 gateway as native tools for AI agents.

Any MCP-compatible agent (Claude Desktop, etc.) can add this server and then
autonomously DISCOVER -> PREVIEW -> PURCHASE paid data, and VERIFY receipts.

Install & run:
    pip install "mcp[cli]" web3 eth-account requests
    # purchase() needs a funded Base wallet:
    export X402_AGENT_PRIVATE_KEY=0x...        # holds >= the quoted USDC + a little ETH (gas)
    export X402_BASE_URL=https://x402.obolpay.xyz   # optional; this is the default (https required)
    python x402_mcp_server.py

Spending guards (optional; the defaults are the safe ones):
    X402_MAX_SPEND_USDC=1.00                   # refuse any quote above this — the wallet's seatbelt
    X402_CHAIN_ID=8453                         # only sign for Base mainnet
    X402_TOKEN_ALLOWLIST=0x8335...2913         # only transfer these ERC-20s (Base USDC by default)
    X402_RECEIPT_TIMEOUT_S=180                 # how long to wait for the payment to confirm

The quote in a 402 reply is a price, not an authorization. This client re-checks the amount,
token, recipient and chain against the guards above before it signs anything, so a gateway that
is compromised — or an X402_BASE_URL pointed somewhere it should not be — cannot turn "buy a
$0.01 dataset" into an arbitrary transfer.

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
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import requests
from requests import RequestException     # 一度だけ束縛する: except 節で
                                          # requests.RequestException と書くと、例外クラスを
                                          # 差し替え可能なモジュール参照経由で解決する事になる
from mcp.server.mcpserver import MCPServer

BASE = os.environ.get("X402_BASE_URL", "https://x402.obolpay.xyz").rstrip("/")
ENDPOINT = BASE + "/api/v1/protected-data"
RPC = os.environ.get("X402_BASE_RPC_URL", "https://mainnet.base.org")
UA = {"User-Agent": "x402-mcp/1.0"}   # send a UA (Cloudflare blocks empty/raw-urllib)

# --- spending guards -----------------------------------------------------------------
# Everything `purchase()` signs — how much, which token, to whom, on which chain — arrives in
# the server's 402 reply. That is fine for a quote and not fine for an authorization: an agent
# runs this tool with no human in the loop, so whatever the endpoint says, it signs. These three
# constants are the only things standing between a hostile or compromised gateway and the
# wallet's whole balance. They are deliberately env-overridable but never taken from the wire.
MAX_SPEND_USDC = Decimal(os.environ.get("X402_MAX_SPEND_USDC", "1.00"))
CHAIN_ID = int(os.environ.get("X402_CHAIN_ID", "8453"))          # Base mainnet
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TOKEN_ALLOWLIST = {
    a.strip().lower()
    for a in os.environ.get("X402_TOKEN_ALLOWLIST", USDC_BASE).split(",")
    if a.strip()
}
USDC_DECIMALS = 6
RECEIPT_TIMEOUT_S = int(os.environ.get("X402_RECEIPT_TIMEOUT_S", "180"))
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

mcp = MCPServer("x402-obolpay")


class PaymentRefused(Exception):
    """A quote we will not sign. Raised before anything touches the chain, never after."""


# --- small helpers: never let a transport detail surface as an unhandled exception ------
def _json(r, *, what: str) -> dict:
    """Parse a JSON body, or raise PaymentRefused with something an agent can act on.

    The gateway sits behind Cloudflare, so a bad minute yields an HTML challenge page or a 5xx
    error page, not JSON. A bare `.json()` turns that into a ValueError from deep inside
    requests, which an MCP client shows the agent as an opaque crash.
    """
    try:
        return r.json()
    except ValueError:
        raise PaymentRefused(
            f"{what}: expected JSON, got {r.status_code} "
            f"{r.headers.get('content-type', '?')} — {r.text[:200]!r}") from None


def _require_https(url: str) -> None:
    """Payment instructions must not arrive over a channel anyone can rewrite.

    Plain http is allowed only against loopback, so local development still works.
    """
    u = urlparse(url)
    if u.scheme == "https":
        return
    if u.scheme == "http" and (u.hostname or "") in ("localhost", "127.0.0.1", "::1"):
        return
    raise PaymentRefused(
        f"refusing to take payment instructions from {url!r}: https required "
        f"(set X402_BASE_URL to an https:// endpoint)")


def _checked_address(value, *, field: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.match(value.strip()):
        raise PaymentRefused(f"{field} is not a 0x-prefixed 20-byte address: {value!r}")
    return value.strip()


def _quoted_units(raw) -> int:
    """Convert a quoted USDC amount to integer minor units, exactly and within the cap.

    Decimal, not float: `float('0.07') * 10**6` is 70000.00000000001, and while round() happens
    to rescue that one, binary floating point has no business deciding how much money leaves a
    wallet. More precision than the token has is refused rather than rounded — a quote we cannot
    represent is a quote we do not understand.
    """
    if isinstance(raw, float):                       # never round someone else's money for them
        raise PaymentRefused(f"amount must be a decimal string, not a float: {raw!r}")
    try:
        amount = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError):
        raise PaymentRefused(f"amount is not a decimal number: {raw!r}") from None
    if not amount.is_finite() or amount <= 0:
        raise PaymentRefused(f"amount must be positive and finite: {raw!r}")
    if -amount.as_tuple().exponent > USDC_DECIMALS:
        raise PaymentRefused(
            f"amount has more precision than the token: {raw!r} > {USDC_DECIMALS} decimals")
    if amount > MAX_SPEND_USDC:
        raise PaymentRefused(
            f"quote {amount} USDC exceeds the spend cap of {MAX_SPEND_USDC} USDC. "
            f"Raise X402_MAX_SPEND_USDC deliberately if this price is genuinely expected.")
    return int(amount.scaleb(USDC_DECIMALS))


def _checked_voucher(message, *, addr: str, nonce: int) -> str:
    """Check a server-supplied voucher before signing it with the payment key.

    `spend_gasless` used to sign `voucher_message` verbatim. Signing an arbitrary string handed
    over by the party being paid is the same mistake as signing an unread contract: the wire
    format is `x402-spend:{domain}:{address}:{nonce}:{amount_units}`, so a server is free to name
    a different spender, a nonce we are not sending, or an amount we never agreed to, and the
    signature it gets back is perfectly valid for it. Verify the fields we can know ourselves.
    """
    if not isinstance(message, str) or not message:
        raise PaymentRefused(f"account endpoint gave no voucher_message: {message!r}")
    parts = message.split(":")
    if len(parts) != 5 or parts[0] != "x402-spend":
        raise PaymentRefused(
            f"refusing to sign an unrecognized voucher: {message[:120]!r}. Expected "
            f"'x402-spend:<domain>:<address>:<nonce>:<units>'.")
    _, _domain, voucher_addr, voucher_nonce, voucher_units = parts
    if voucher_addr.lower() != addr.lower():
        raise PaymentRefused(
            f"voucher names spender {voucher_addr} but this wallet is {addr}")
    if voucher_nonce != str(nonce):
        raise PaymentRefused(
            f"voucher nonce {voucher_nonce} does not match the nonce being sent ({nonce})")
    try:
        units = int(voucher_units)
    except ValueError:
        raise PaymentRefused(f"voucher amount is not an integer: {voucher_units!r}") from None
    if units <= 0 or Decimal(units).scaleb(-USDC_DECIMALS) > MAX_SPEND_USDC:
        raise PaymentRefused(
            f"voucher debits {Decimal(units).scaleb(-USDC_DECIMALS)} USDC, over the "
            f"{MAX_SPEND_USDC} USDC cap")
    return message


def _checked_quote(challenge: dict) -> dict:
    """Validate every economic term before a signature exists. Fail closed, spend nothing."""
    if not isinstance(challenge, dict):
        raise PaymentRefused(f"402 body has no 'payment' object: {challenge!r}")
    token = _checked_address(challenge.get("token_contract"), field="token_contract")
    if token.lower() not in TOKEN_ALLOWLIST:
        raise PaymentRefused(
            f"refusing to transfer token {token}: not in X402_TOKEN_ALLOWLIST. A gateway that "
            f"names an unexpected contract is asking for an unexpected asset.")
    chain_id = challenge.get("chain_id")
    if chain_id != CHAIN_ID:
        raise PaymentRefused(
            f"challenge names chain {chain_id!r}, expected {CHAIN_ID}. Signing against a chain "
            f"we did not choose is how the same signature becomes valid somewhere it should not be.")
    scheme = challenge.get("signature_scheme")
    if not isinstance(scheme, dict) or not isinstance(scheme.get("domain"), str):
        raise PaymentRefused("challenge is missing signature_scheme.domain")
    invoice = challenge.get("invoice_id")
    if not isinstance(invoice, str) or not invoice:
        raise PaymentRefused("challenge is missing invoice_id")
    return {
        "token": token,
        "recipient": _checked_address(challenge.get("recipient"), field="recipient"),
        "units": _quoted_units(challenge.get("amount")),
        "chain_id": CHAIN_ID,
        "domain": scheme["domain"],
        "invoice": invoice,
    }


@mcp.tool()
def discover() -> dict:
    """Return the x402 gateway's machine-readable manifest: price, token, network, recipient,
    payment flow, free-preview and proof-of-purchase capabilities. No payment required."""
    try:
        r = requests.get(BASE + "/.well-known/x402", headers=UA, timeout=30)
        return _json(r, what="manifest")
    except PaymentRefused as e:
        return {"error": str(e)}
    except RequestException as e:
        return {"error": f"could not reach {BASE}: {type(e).__name__}"}


@mcp.tool()
def preview() -> dict:
    """Fetch the HTTP 402 challenge and return its FREE preview (a sample of the paid dataset)
    plus the price/invoice, so the agent can decide whether to pay. No payment required."""
    try:
        r = requests.get(ENDPOINT, headers=UA, timeout=30)
        if r.status_code != 402:
            return {"error": f"expected 402, got {r.status_code}", "body": r.text[:400]}
        body = _json(r, what="402 challenge")
    except PaymentRefused as e:
        return {"error": str(e)}
    except RequestException as e:
        return {"error": f"could not reach {BASE}: {type(e).__name__}"}
    p = body.get("payment")
    if not isinstance(p, dict):
        return {"error": "402 body has no 'payment' object", "body": str(body)[:400]}
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

    # --- everything before the transaction is sent may fail freely: nothing has been spent ---
    try:
        _require_https(ENDPOINT)
        r = requests.get(ENDPOINT, headers=UA, timeout=30)
        if r.status_code != 402:
            return {"error": f"expected 402, got {r.status_code}", "body": r.text[:400],
                    "spent": False}
        quote = _checked_quote(_json(r, what="402 challenge").get("payment"))
    except PaymentRefused as e:
        return {"error": str(e), "spent": False}
    except RequestException as e:
        return {"error": f"could not reach {BASE}: {type(e).__name__}", "spent": False}

    from web3 import Web3
    from eth_account import Account
    from eth_account.messages import encode_defunct

    acct = Account.from_key(pk)
    w3 = Web3(Web3.HTTPProvider(RPC))
    token = Web3.to_checksum_address(quote["token"])
    to = Web3.to_checksum_address(quote["recipient"])
    domain, invoice = quote["domain"], quote["invoice"]

    erc20 = w3.eth.contract(address=token, abi=[{"name": "transfer", "type": "function",
        "stateMutability": "nonpayable", "inputs": [{"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"}], "outputs": [{"type": "bool"}]}])
    try:
        tx = erc20.functions.transfer(to, quote["units"]).build_transaction({
            "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": quote["chain_id"], "gas": 120000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei")})
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        txh = w3.eth.send_raw_transaction(raw).hex()
    except Exception as e:
        # The transfer was never broadcast, so this is still a clean refusal.
        return {"error": f"could not submit payment: {type(e).__name__}: {e}", "spent": False}
    if not txh.startswith("0x"):
        txh = "0x" + txh

    # --- from here the money is gone; every path must hand the tx hash back -----------
    # Losing it inside a traceback is the worst outcome this tool has: the USDC left the wallet
    # and the agent has no reference with which to claim the data or prove the payment.
    try:
        w3.eth.wait_for_transaction_receipt(txh, timeout=RECEIPT_TIMEOUT_S)
    except Exception as e:
        return {"error": f"payment sent but not confirmed within {RECEIPT_TIMEOUT_S}s "
                         f"({type(e).__name__}); retry the unlock with this tx_hash",
                "tx_hash": txh, "spent": True}

    msg = "x402:" + domain + ":" + invoice + ":" + txh.lower()
    sig = Account.sign_message(encode_defunct(text=msg), pk).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig

    headers = {**UA, "X-Payment-Invoice-ID": invoice, "X-Payment-Tx-Hash": txh, "X-Payment-Signature": sig}
    for _ in range(40):
        try:
            # allow_redirects=False: requests only strips `Authorization` when a redirect changes
            # host — custom headers ride along. Following one would hand this payment signature
            # to whatever host the response names.
            rr = requests.get(ENDPOINT, headers=headers, timeout=30, allow_redirects=False)
            if rr.status_code == 200:
                body = _json(rr, what="unlocked data")
                return {"tx_hash": txh, "data": body.get("data"), "receipt": body.get("receipt")}
            if rr.status_code == 402:
                if _json(rr, what="402 retry").get("retryable"):
                    time.sleep(3)
                    continue
        except PaymentRefused as e:
            return {"error": str(e), "tx_hash": txh, "spent": True}
        except RequestException as e:
            return {"error": f"paid, but the unlock request failed: {type(e).__name__}",
                    "tx_hash": txh, "spent": True}
        return {"error": f"rejected: {rr.status_code}", "body": rr.text[:400], "tx_hash": txh,
                "spent": True}
    return {"error": "timed out waiting for verification", "tx_hash": txh, "spent": True}


@mcp.tool()
def verify_receipt(message: str, signature: str) -> dict:
    """Verify a proof-of-purchase receipt with the gateway (recovers the EIP-191 signer and
    checks it equals the server's receipt signer). Anyone can call this — no payment required."""
    try:
        r = requests.post(BASE + "/verify-receipt",
                          headers={**UA, "Content-Type": "application/json"},
                          json={"message": message, "signature": signature}, timeout=30)
        return _json(r, what="receipt verification")
    except PaymentRefused as e:
        return {"error": str(e)}
    except RequestException as e:
        return {"error": f"could not reach {BASE}: {type(e).__name__}"}


# ---- Prepaid gasless balance (for HABITUAL use: deposit once, then gasless calls) ----
def _agent_address() -> str:
    """The wallet's own address. Raises PaymentRefused (not KeyError) when no key is configured."""
    pk = os.environ.get("X402_AGENT_PRIVATE_KEY")
    if not pk:
        raise PaymentRefused("X402_AGENT_PRIVATE_KEY not set")
    from eth_account import Account
    try:
        return Account.from_key(pk).address.lower()
    except Exception as e:
        raise PaymentRefused(f"X402_AGENT_PRIVATE_KEY is not a usable key: {type(e).__name__}") from None


@mcp.tool()
def balance(address: str = "") -> dict:
    """Check a prepaid balance: remaining balance, calls_remaining, and next_nonce.
    Pass an address, or leave blank to use the wallet from X402_AGENT_PRIVATE_KEY."""
    addr = (address or "").strip().lower()
    try:
        if not addr:
            if not os.environ.get("X402_AGENT_PRIVATE_KEY"):
                return {"error": "provide address or set X402_AGENT_PRIVATE_KEY"}
            addr = _agent_address()
        else:
            # This value is pasted straight into the URL path, and an LLM chooses it. Without a
            # shape check, "../account/../admin" or an embedded "?"/"#" addresses a different
            # route on the gateway than the one this tool claims to call.
            addr = _checked_address(addr, field="address").lower()
        r = requests.get(BASE + "/account/" + addr, headers=UA, timeout=30)
        return _json(r, what="account balance")
    except PaymentRefused as e:
        return {"error": str(e)}
    except RequestException as e:
        return {"error": f"could not reach {BASE}: {type(e).__name__}"}


@mcp.tool()
def topup(tx_hash: str) -> dict:
    """Credit a prepaid balance from an on-chain USDC deposit to the recipient.
    First send USDC to the recipient on Base, then call this with the tx_hash."""
    tx_hash = (tx_hash or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
        return {"error": f"tx_hash is not a 0x-prefixed 32-byte hash: {tx_hash!r}"}
    try:
        r = requests.post(BASE + "/account/topup",
                          headers={**UA, "Content-Type": "application/json"},
                          json={"tx_hash": tx_hash}, timeout=60)
        return _json(r, what="topup")
    except PaymentRefused as e:
        return {"error": str(e)}
    except RequestException as e:
        return {"error": f"could not reach {BASE}: {type(e).__name__}"}


@mcp.tool()
def spend_gasless() -> dict:
    """Fetch the paid data by drawing from the prepaid balance — GASLESS, NO on-chain tx.
    This is the habitual-use path: after one topup you can call this many times for free (only gas-free
    balance debit). Requires X402_AGENT_PRIVATE_KEY with a funded balance."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    try:
        _require_https(ENDPOINT)
        pk = os.environ.get("X402_AGENT_PRIVATE_KEY")
        if not pk:
            return {"error": "X402_AGENT_PRIVATE_KEY not set; cannot spend."}
        addr = _agent_address()
        acc = _json(requests.get(BASE + "/account/" + addr, headers=UA, timeout=30),
                    what="account balance")
        if not isinstance(acc, dict):
            return {"error": "account endpoint did not return an object", "body": str(acc)[:400]}
        if acc.get("balance_units", 0) < acc.get("price_units", 1):
            return {"error": "insufficient_balance", "account": acc,
                    "hint": "call topup(tx_hash) after depositing USDC"}
        nonce = acc.get("next_nonce")
        if not isinstance(nonce, int) or isinstance(nonce, bool) or nonce < 0:
            return {"error": f"account endpoint gave no usable next_nonce: {nonce!r}"}
        msg = _checked_voucher(acc.get("voucher_message"), addr=addr, nonce=nonce)
        sig = Account.sign_message(encode_defunct(text=msg), pk).signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        headers = {**UA, "X-Account-Address": addr, "X-Account-Nonce": str(nonce),
                   "X-Account-Voucher": sig}
        # allow_redirects=False: this voucher signature authorizes a debit. It goes to the host
        # we chose, not to one a 302 picks for us.
        return _json(requests.get(ENDPOINT, headers=headers, timeout=30, allow_redirects=False),
                     what="gasless spend")
    except PaymentRefused as e:
        return {"error": str(e)}
    except RequestException as e:
        return {"error": f"could not reach {BASE}: {type(e).__name__}"}


if __name__ == "__main__":
    mcp.run()
