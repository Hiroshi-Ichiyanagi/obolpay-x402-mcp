"""x402 MCP クライアントのユニットテスト（完全オフライン・全てモック）。

対象の性質:
  - 402 応答の解析(preview / purchase の challenge 読み取り)
  - 支払いヘッダ構築(X-Payment-* / X-Account-*)と EIP-191 メッセージ束縛
  - 金額の単位変換(USDC 1e-6, int(round(...)) の丸め)
  - エラー経路(鍵なし・非402・非リトライ拒否・検証タイムアウト)
  - URL/入力検証(BASE の rstrip, アドレスの strip+lower, UA 必須)

ネットワークには一切出ない: requests は FakeRequests に差し替え、web3 は
偽モジュールを sys.modules に注入。eth_account の署名だけは本物(純ローカル計算)を
使い、復元(recover)で署名者を実証する。有料エンドポイントへの実決済は行わない。
"""
import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 事故防止: 実鍵が環境にあってもテストでは絶対に使わない
os.environ.pop("X402_AGENT_PRIVATE_KEY", None)

import x402_mcp_server as srv  # noqa: E402

from eth_account import Account  # noqa: E402  (署名/復元は純ローカル計算)
from eth_account.messages import encode_defunct  # noqa: E402

# 周知の公開テスト鍵(Hardhat #0)。実資産ゼロ・実決済には使えない
TEST_PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDR = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"


# ---------------------------------------------------------------- fakes

class FakeResp:
    def __init__(self, status_code=200, json_data=None, text=None, headers=None):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data
        self.text = json.dumps(self._json) if text is None else text
        # 本物の requests.Response は必ず headers を持つ。偽物が持たないと、
        # 「本番では必ず在る属性」を読むコードがテストでだけ AttributeError になる。
        self.headers = {"content-type": "application/json"} if headers is None else headers

    def json(self):
        return self._json


class FakeRequests:
    """srv.requests の差し替え。応答列を順に返し、最後の1つは繰り返す。"""

    def __init__(self, get_responses=(), post_responses=()):
        self._gets = list(get_responses)
        self._posts = list(post_responses)
        self.get_calls = []
        self.post_calls = []
        # 残りのキーワード引数も残す。allow_redirects のように「渡していない」事自体が
        # 脆弱性であるものを検査するため（**kw で捨てると検査できない）。
        self.get_kwargs = []
        self.post_kwargs = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        self.get_kwargs.append(kw)
        return self._gets.pop(0) if len(self._gets) > 1 else self._gets[0]

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        self.post_calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        self.post_kwargs.append(kw)
        return self._posts.pop(0) if len(self._posts) > 1 else self._posts[0]


class ExplodingRequests:
    """呼ばれた時点で失敗 = ネットワークに出ない事の証明。"""

    def get(self, *a, **kw):
        raise AssertionError("network call attempted")

    post = get


def make_challenge(amount="0.005"):
    return {"payment": {
        "token_contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "recipient": "0x1111111111111111111111111111111111111111",
        "amount": amount,
        "chain_id": 8453,
        "invoice_id": "inv-123",
        "signature_scheme": {"domain": "x402.obolpay.xyz"},
    }}


def install_fake_web3(monkeypatch, calls, tx_hash_hex="abc123def456", receipt_raises=None):
    """web3 モジュールを偽物に差し替え(RPC に出ない)。eth_account は本物のまま、
    sign_transaction だけ偽 acct 経由で無害化する。"""
    mod = types.ModuleType("web3")

    class _Fn:
        def __init__(self, to, value):
            calls["transfer_args"] = (to, value)

        def build_transaction(self, params):
            calls["tx_params"] = params
            return {"faketx": True}

    class _Functions:
        @staticmethod
        def transfer(to, value):
            return _Fn(to, value)

    class _Contract:
        functions = _Functions()

    class _Eth:
        gas_price = 100

        def contract(self, address=None, abi=None):
            calls["contract_address"] = address
            return _Contract()

        def get_transaction_count(self, addr):
            return 7

        def send_raw_transaction(self, raw):
            calls["raw_sent"] = raw

            class _H:
                def hex(_self):
                    return tx_hash_hex
            return _H()

        def wait_for_transaction_receipt(self, txh, timeout=None):
            # 旧呼び出し形(位置引数1つ)と新形(timeout 付き)の両方を受ける。timeout だけを
            # 受ける偽物にすると、修正前ソースに対して TypeError で赤くなり
            # 「タイムアウトが無い事」ではなく「呼び出し形が変わった事」を証明してしまう。
            calls["waited_for"] = txh
            calls["receipt_timeout"] = timeout
            if receipt_raises is not None:
                raise receipt_raises

    class FakeWeb3:
        def __init__(self, provider):
            self.eth = _Eth()

        @staticmethod
        def HTTPProvider(url):
            calls["rpc_url"] = url
            return ("provider", url)

        @staticmethod
        def to_checksum_address(a):
            return a

        def to_wei(self, v, unit):
            return int(v * 10 ** 9)

    mod.Web3 = FakeWeb3
    monkeypatch.setitem(sys.modules, "web3", mod)

    # eth_account: from_key/sign_message は本物、sign_transaction のみ偽
    acct_mod = types.ModuleType("eth_account")

    class FakeAccount:
        @staticmethod
        def from_key(pk):
            real = Account.from_key(pk)

            class _A:
                address = real.address

                def sign_transaction(self, tx):
                    calls["signed_tx"] = tx

                    class _S:
                        raw_transaction = b"rawtx"
                    return _S()
            return _A()

        @staticmethod
        def sign_message(signable, pk):
            calls["signed_message"] = signable
            return Account.sign_message(signable, pk)

    acct_mod.Account = FakeAccount
    msg_mod = types.ModuleType("eth_account.messages")
    msg_mod.encode_defunct = encode_defunct
    acct_mod.messages = msg_mod
    monkeypatch.setitem(sys.modules, "eth_account", acct_mod)
    monkeypatch.setitem(sys.modules, "eth_account.messages", msg_mod)


def run_purchase(monkeypatch, get_responses, tx_hash_hex="abc123def456"):
    calls = {}
    monkeypatch.setenv("X402_AGENT_PRIVATE_KEY", TEST_PK)
    install_fake_web3(monkeypatch, calls, tx_hash_hex=tx_hash_hex)
    fake = FakeRequests(get_responses=get_responses)
    monkeypatch.setattr(srv, "requests", fake)
    monkeypatch.setattr(srv.time, "sleep", lambda s: calls.setdefault("sleeps", []).append(s))
    return srv.purchase(), calls, fake


# ---------------------------------------------------------------- discover

def test_discover_hits_wellknown_with_ua(monkeypatch):
    fake = FakeRequests(get_responses=[FakeResp(200, {"x402_version": 1})])
    monkeypatch.setattr(srv, "requests", fake)
    assert srv.discover() == {"x402_version": 1}
    call = fake.get_calls[0]
    assert call["url"] == srv.BASE + "/.well-known/x402"
    assert call["headers"]["User-Agent"]  # Cloudflare は UA 無しを弾く
    assert call["timeout"] == 30


# ---------------------------------------------------------------- preview (402 解析)

def test_preview_parses_402_challenge(monkeypatch):
    fake = FakeRequests(get_responses=[FakeResp(402, make_challenge())])
    monkeypatch.setattr(srv, "requests", fake)
    out = srv.preview()
    assert out["invoice_id"] == "inv-123"
    assert out["recipient"] == "0x1111111111111111111111111111111111111111"
    assert out["price"] == {"amount": "0.005", "token": None, "network": None}
    assert fake.get_calls[0]["url"] == srv.ENDPOINT


def test_preview_non_402_returns_error_with_truncated_body(monkeypatch):
    fake = FakeRequests(get_responses=[FakeResp(200, text="x" * 1000)])
    monkeypatch.setattr(srv, "requests", fake)
    out = srv.preview()
    assert out["error"] == "expected 402, got 200"
    assert len(out["body"]) == 400  # 本文は 400 文字で切り詰め


def test_preview_missing_payment_block_is_graceful(monkeypatch):
    """payment ブロックが無い 402 は「エラー」として返す。

    以前は None 埋めの正常形（preview=None, price 全 None）を返していた。KeyError にならない
    点は良かったが、エージェントから見ると「プレビューが提供されていない」のか
    「ゲートウェイが壊れている」のか区別できない。区別できない返答は判断材料にならないので、
    明示的な error に変更した（無料経路なので金銭的影響はない）。
    """
    fake = FakeRequests(get_responses=[FakeResp(402, {"unexpected": True})])
    monkeypatch.setattr(srv, "requests", fake)
    out = srv.preview()
    assert "error" in out and "payment" in out["error"]


# ---------------------------------------------------------------- purchase (支払い)

def test_purchase_without_key_errors_before_any_network(monkeypatch):
    monkeypatch.delenv("X402_AGENT_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(srv, "requests", ExplodingRequests())
    assert srv.purchase() == {"error": "X402_AGENT_PRIVATE_KEY not set; cannot pay."}


def test_purchase_non_402_challenge_errors(monkeypatch):
    out, calls, fake = run_purchase(monkeypatch, [FakeResp(503, text="down")])
    assert out["error"] == "expected 402, got 503"
    assert out["spent"] is False        # 「まだ1円も出ていない」事を戻り値で明言する
    assert "raw_sent" not in calls      # 送金には進まない


def test_purchase_amount_is_usdc_6_decimals(monkeypatch):
    out, calls, fake = run_purchase(
        monkeypatch,
        [FakeResp(402, make_challenge(amount="0.005")),
         FakeResp(200, {"data": {"d": 1}, "receipt": {"r": 1}})])
    to, value = calls["transfer_args"]
    assert value == 5000  # 0.005 USDC = 5000 units (1e-6)
    assert to == "0x1111111111111111111111111111111111111111"
    assert calls["contract_address"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert out == {"tx_hash": "0xabc123def456", "data": {"d": 1}, "receipt": {"r": 1}}


def test_purchase_amount_rounding_avoids_float_truncation(monkeypatch):
    # 0.29 * 1e6 = 289999.99999... — int(round(...)) で 290000 になること(切り捨て回帰の防止)
    out, calls, fake = run_purchase(
        monkeypatch,
        [FakeResp(402, make_challenge(amount="0.29")),
         FakeResp(200, {"data": None, "receipt": None})])
    assert calls["transfer_args"][1] == 290_000


def test_purchase_tx_hash_gets_0x_prefix(monkeypatch):
    out, calls, fake = run_purchase(
        monkeypatch,
        [FakeResp(402, make_challenge()),
         FakeResp(200, {"data": 1, "receipt": 2})],
        tx_hash_hex="ABC123")  # hex() が 0x 無しを返す実装でも動く事
    assert out["tx_hash"] == "0xABC123"
    assert calls["waited_for"] == "0xABC123"


def test_purchase_payment_headers_and_signature_binding(monkeypatch):
    out, calls, fake = run_purchase(
        monkeypatch,
        [FakeResp(402, make_challenge()),
         FakeResp(200, {"data": 1, "receipt": 2})])
    paid = fake.get_calls[1]  # 2回目の GET が支払いヘッダ付き
    h = paid["headers"]
    assert paid["url"] == srv.ENDPOINT
    assert h["X-Payment-Invoice-ID"] == "inv-123"
    assert h["X-Payment-Tx-Hash"] == "0xabc123def456"
    assert h["User-Agent"]
    sig = h["X-Payment-Signature"]
    assert sig.startswith("0x")
    # EIP-191 メッセージが domain:invoice:txhash(小文字) に束縛されている事を復元で実証
    expected_msg = "x402:x402.obolpay.xyz:inv-123:0xabc123def456"
    signer = Account.recover_message(encode_defunct(text=expected_msg), signature=sig)
    assert signer.lower() == TEST_ADDR


def test_purchase_retries_on_retryable_402_then_succeeds(monkeypatch):
    out, calls, fake = run_purchase(
        monkeypatch,
        [FakeResp(402, make_challenge()),
         FakeResp(402, {"retryable": True}),
         FakeResp(402, {"retryable": True}),
         FakeResp(200, {"data": "unlocked", "receipt": {"ok": 1}})])
    assert out["data"] == "unlocked"
    assert calls["sleeps"] == [3, 3]
    assert len(fake.get_calls) == 4  # challenge + 3 attempts


def test_purchase_non_retryable_rejection_errors_with_tx_hash(monkeypatch):
    out, calls, fake = run_purchase(
        monkeypatch,
        [FakeResp(402, make_challenge()),
         FakeResp(403, {"reason": "bad sig"})])
    assert out["error"] == "rejected: 403"
    assert out["tx_hash"] == "0xabc123def456"  # 失敗しても送金txは追跡可能
    assert "sleeps" not in calls  # 非リトライは即終了


def test_purchase_verification_timeout_after_40_attempts(monkeypatch):
    out, calls, fake = run_purchase(
        monkeypatch,
        [FakeResp(402, make_challenge()),
         FakeResp(402, {"retryable": True})])  # 永遠に retryable
    assert out == {"error": "timed out waiting for verification",
                   "tx_hash": "0xabc123def456", "spent": True}
    assert len(fake.get_calls) == 1 + 40  # challenge + 上限40回で必ず打ち切り


# ---------------------------------------------------------------- verify_receipt

def test_verify_receipt_posts_message_and_signature(monkeypatch):
    fake = FakeRequests(post_responses=[FakeResp(200, {"valid": True})])
    monkeypatch.setattr(srv, "requests", fake)
    assert srv.verify_receipt("msg", "0xsig") == {"valid": True}
    call = fake.post_calls[0]
    assert call["url"] == srv.BASE + "/verify-receipt"
    assert call["json"] == {"message": "msg", "signature": "0xsig"}
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["User-Agent"]


# ---------------------------------------------------------------- balance / topup

def test_balance_normalizes_address_input(monkeypatch):
    fake = FakeRequests(get_responses=[FakeResp(200, {"balance_units": 1})])
    monkeypatch.setattr(srv, "requests", fake)
    assert srv.balance("  0xF39Fd6e51aad88F6F4ce6aB8827279cffFb92266  ") == {"balance_units": 1}
    assert fake.get_calls[0]["url"] == srv.BASE + "/account/" + TEST_ADDR


def test_balance_derives_address_from_env_key(monkeypatch):
    monkeypatch.setenv("X402_AGENT_PRIVATE_KEY", TEST_PK)
    fake = FakeRequests(get_responses=[FakeResp(200, {"balance_units": 2})])
    monkeypatch.setattr(srv, "requests", fake)
    assert srv.balance() == {"balance_units": 2}
    assert fake.get_calls[0]["url"] == srv.BASE + "/account/" + TEST_ADDR


def test_topup_posts_tx_hash(monkeypatch):
    fake = FakeRequests(post_responses=[FakeResp(200, {"credited": True})])
    monkeypatch.setattr(srv, "requests", fake)
    # 実在の形（0x + 32バイト）を使う。'0xdeadbeef' は tx hash として成立しないので、
    # 短縮値を使い続けると「サーバへ素通しする」挙動をテストが追認してしまう。
    txh = "0x" + "de" * 32
    assert srv.topup(txh) == {"credited": True}
    call = fake.post_calls[0]
    assert call["url"] == srv.BASE + "/account/topup"
    assert call["json"] == {"tx_hash": txh}
    assert call["timeout"] == 60


# ---------------------------------------------------------------- spend_gasless

def test_spend_gasless_insufficient_balance_stops_before_spend(monkeypatch):
    monkeypatch.setenv("X402_AGENT_PRIVATE_KEY", TEST_PK)
    acc = {"balance_units": 100, "price_units": 5000}
    fake = FakeRequests(get_responses=[FakeResp(200, acc)])
    monkeypatch.setattr(srv, "requests", fake)
    out = srv.spend_gasless()
    assert out["error"] == "insufficient_balance"
    assert out["account"] == acc
    assert len(fake.get_calls) == 1  # ENDPOINT には行かない(残高照会のみ)


def test_spend_gasless_empty_account_treated_as_insufficient(monkeypatch):
    monkeypatch.setenv("X402_AGENT_PRIVATE_KEY", TEST_PK)
    fake = FakeRequests(get_responses=[FakeResp(200, {})])
    monkeypatch.setattr(srv, "requests", fake)
    out = srv.spend_gasless()  # フィールド欠落でも KeyError にならない
    assert out["error"] == "insufficient_balance"


def test_spend_gasless_signs_voucher_and_builds_headers(monkeypatch):
    monkeypatch.setenv("X402_AGENT_PRIVATE_KEY", TEST_PK)
    # 本番が実際に返す書式。旧フィクスチャは "x402-account:<domain>:<addr>:3"（4要素）
    # だったが、これはサーバが一度も送っていない形式で、
    # 実測: GET https://x402.obolpay.xyz/account/0xf39f… ->
    #   "voucher_message":"x402-spend:x402.obolpay.xyz:0xf39f…:0:10000"
    # 「サーバ提示の文字列に署名する」事を、サーバが出さない文字列で実証していた。
    voucher_msg = "x402-spend:x402.obolpay.xyz:%s:3:5000" % TEST_ADDR
    acc = {"balance_units": 10_000, "price_units": 5_000,
           "next_nonce": 3, "voucher_message": voucher_msg}
    fake = FakeRequests(get_responses=[FakeResp(200, acc),
                                       FakeResp(200, {"data": "paid", "receipt": {}})])
    monkeypatch.setattr(srv, "requests", fake)
    out = srv.spend_gasless()
    assert out == {"data": "paid", "receipt": {}}
    assert fake.get_calls[0]["url"] == srv.BASE + "/account/" + TEST_ADDR
    spend = fake.get_calls[1]
    assert spend["url"] == srv.ENDPOINT
    h = spend["headers"]
    assert h["X-Account-Address"] == TEST_ADDR
    assert h["X-Account-Nonce"] == "3"  # ヘッダは必ず文字列
    sig = h["X-Account-Voucher"]
    assert sig.startswith("0x")
    # サーバ提示の voucher_message にのみ署名している事を復元で実証
    signer = Account.recover_message(encode_defunct(text=voucher_msg), signature=sig)
    assert signer.lower() == TEST_ADDR


# ---------------------------------------------------------------- URL/設定検証

def _load_fresh_module():
    spec = importlib.util.spec_from_file_location(
        "x402_mcp_server_fresh", str(ROOT / "x402_mcp_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_base_url_env_override_strips_trailing_slashes(monkeypatch):
    monkeypatch.setenv("X402_BASE_URL", "https://example.test///")
    mod = _load_fresh_module()
    assert mod.BASE == "https://example.test"
    assert mod.ENDPOINT == "https://example.test/api/v1/protected-data"


def test_default_base_url_when_env_unset(monkeypatch):
    monkeypatch.delenv("X402_BASE_URL", raising=False)
    mod = _load_fresh_module()
    assert mod.BASE == "https://x402.obolpay.xyz"
    assert mod.ENDPOINT == "https://x402.obolpay.xyz/api/v1/protected-data"
