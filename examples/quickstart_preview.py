#!/usr/bin/env python3
"""
x402.obolpay.xyz — 60-second quickstart. ZERO setup: stdlib only, no wallet, no pip install.

Shows the killer feature: an agent evaluates the data QUALITY *before* paying
(trustless — no blind spend). Run it:

    python3 quickstart_preview.py

Then look at buy_openunit.py for the paid path (one signed USDC micropayment).
"""
import json
import urllib.request

BASE = "https://x402.obolpay.xyz"
UA = {"User-Agent": "x402-quickstart/1.0"}


def get(path):
    req = urllib.request.Request(BASE + path, headers=UA)
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:  # 402 Payment Required carries the challenge + free preview
        return e.code, json.loads(e.read())


def main():
    # 1) What does the market pay for most right now?
    _, stats = get("/stats")
    print(f"→ selected_product (market-chosen): {stats['selected_product']}  "
          f"| total paid calls: {stats['total_calls']}")

    # 2) Ask for the paid data → get 402 with a FREE preview (evaluate before paying)
    code, body = get("/api/v1/protected-data?types=openunit")
    print(f"\n→ GET /api/v1/protected-data?types=openunit  ->  HTTP {code}")
    pay = body["payment"]
    print(f"   price: {pay['amount']} {pay['token']} on {pay['network']}  ->  {pay['recipient']}")

    prev = pay["preview"]
    print(f"\n=== FREE PREVIEW (no payment) ===")
    print(f"   product : {prev['product']}")
    print(f"   items   : {prev['item_count']}  (categories buyable via ?types=)")
    fr = prev.get("freshness") or {}
    if fr:
        print(f"   freshness: signed, data age {fr.get('age_seconds')}s  (server-attested, EIP-191)")
    print(f"   sample  : {json.dumps(prev['sample_item'], ensure_ascii=False)[:120]}...")

    print(f"\n✓ You just evaluated the dataset WITHOUT paying. That's the point: trustless preview.")
    print(f"  To unlock the full data, pay the quoted {pay['amount']} {pay['token']} "
          f"and re-send with an EIP-191 signature (see buy_openunit.py).")


if __name__ == "__main__":
    import urllib.error  # noqa
    main()
