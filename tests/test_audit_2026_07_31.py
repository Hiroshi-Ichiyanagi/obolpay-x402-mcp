"""2026-07-31 監査で見つかった問題の回帰テスト（完全オフライン）。

このリポジトリの特殊性: MCP ツールは「人間が見ていない状態で」実鍵を持つプロセスから
呼ばれる。したがってバグの帰結は例外ではなく **意図しない送金** になる。以下のテストは
ほぼ全てが「署名が生まれる前に止まる事」の証明で、`calls` に `raw_sent` が入っていない
＝ブロードキャストしていない＝1円も出ていない、を不変条件として使う。

既存 tests/test_client_units.py のフェイク（FakeRequests / install_fake_web3 /
make_challenge / run_purchase）をそのまま再利用する。テスト専用の二重実装を作ると、
本体ではなくフェイクを検証する事になりかねないため。
"""
from decimal import Decimal

import pytest

from test_client_units import (  # noqa: E402  (sys.path は test_client_units が通す)
    FakeRequests,
    FakeResp,
    TEST_ADDR,
    TEST_PK,
    install_fake_web3,
    make_challenge,
    run_purchase,
    srv,
)

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402


def _challenge(**over):
    """make_challenge() の payment ブロックを部分的に差し替える。"""
    ch = make_challenge()
    ch["payment"].update(over)
    return ch


# ══ 1. 402 の見積りは「価格」であって「承認」ではない ══════════════════════════════
# purchase() は amount / token_contract / recipient / chain_id を全てサーバ応答から
# 読み、そのまま署名していた。X402_BASE_URL は環境変数で、ゲートウェイは侵害されうる。
# 人間が見ていないツールでこれは「残高全部まで青天井」を意味する。

def test_a_quote_above_the_cap_is_refused_before_signing(monkeypatch):
    """既定 1.00 USDC 上限。上限超は署名前に止まり、チェーンには何も出ない。"""
    out, calls, fake = run_purchase(monkeypatch, [FakeResp(402, _challenge(amount="500.0"))])
    assert "spend cap" in out["error"]
    assert out["spent"] is False
    assert "raw_sent" not in calls, "上限超の見積りでトランザクションが送信された"
    assert "signed_tx" not in calls, "上限超の見積りに署名した"


def test_the_cap_is_configurable_but_never_taken_from_the_wire(monkeypatch):
    """対照実験（修正前ソースでも緑）。上限を上げれば同じ見積りが通る＝上の拒否群は
    「常に拒否する」実装ではない事を示す。raising=False は、修正前には
    MAX_SPEND_USDC 自体が存在せず、monkeypatch が AttributeError で赤くなって
    「定数が増えた事」を証明してしまうのを避けるため。"""
    monkeypatch.setattr(srv, "MAX_SPEND_USDC", Decimal("1000"), raising=False)
    out, calls, fake = run_purchase(monkeypatch,
                                    [FakeResp(402, _challenge(amount="500.0")),
                                     FakeResp(200, {"data": "d", "receipt": {}})])
    assert out["data"] == "d"
    assert calls["transfer_args"][1] == 500_000_000    # 500 USDC = 5e8 minor units


@pytest.mark.parametrize("amount,why", [
    ("0", "ゼロ請求は見積りとして成立しない"),
    ("-1", "負の請求"),
    ("abc", "数値ですらない"),
    ("", "空文字"),
    ("0.0000001", "USDC の 6 桁を超える精度 — 丸めずに拒否する"),
    ("1e9", "指数表記で上限を潜り抜けようとする形"),
], ids=lambda v: str(v)[:24])
def test_malformed_amounts_are_refused(monkeypatch, amount, why):
    out, calls, fake = run_purchase(monkeypatch, [FakeResp(402, _challenge(amount=amount))])
    # 送金していない事を先に見る。out["spent"] を先に読むと、修正前ソースには spent キーが
    # 無いので KeyError で赤くなり「キーが増えた事」を証明してしまう。実際に確かめたいのは
    # 「壊れた見積りでチェーンに何も出ていない」で、それは calls 側にしか現れない。
    assert "raw_sent" not in calls, f"{amount!r} ({why}) で送金してしまった"
    assert out.get("spent") is False, why


def test_a_float_amount_is_refused_rather_than_rounded(monkeypatch):
    """float は「金額」の型ではない。float('0.07') * 10**6 は 70000.00000000001 で、
    round() がたまたま救っているだけ。救えている事に依存しない。"""
    out, calls, fake = run_purchase(monkeypatch, [FakeResp(402, _challenge(amount=0.07))])
    assert "float" in out["error"]
    assert "raw_sent" not in calls


def test_an_unexpected_token_contract_is_refused(monkeypatch):
    """ABI は transfer(address,uint256) 固定なので任意関数は呼べないが、
    「どのトークンを」はサーバが決めていた。想定外の ERC-20 は想定外の資産である。"""
    out, calls, fake = run_purchase(
        monkeypatch,
        [FakeResp(402, _challenge(token_contract="0x" + "ab" * 20))])
    assert "not in X402_TOKEN_ALLOWLIST" in out["error"]
    assert "raw_sent" not in calls


def test_a_foreign_chain_id_is_refused(monkeypatch):
    """chainId をサーバに選ばせると、こちらが選んでいないチェーンに対して有効な署名が
    出来上がる。同じアドレスが実資産を持つチェーンは Base だけではない。"""
    out, calls, fake = run_purchase(monkeypatch, [FakeResp(402, _challenge(chain_id=1))])
    assert "chain" in out["error"]
    assert "raw_sent" not in calls


@pytest.mark.parametrize("field,value", [
    ("recipient", "0xnot-an-address"),
    ("recipient", None),
    ("recipient", "0x1111"),
    ("token_contract", "not-hex"),
])
def test_malformed_addresses_are_refused(monkeypatch, field, value):
    out, calls, fake = run_purchase(monkeypatch, [FakeResp(402, _challenge(**{field: value}))])
    assert "raw_sent" not in calls, f"{field}={value!r} のまま送金してしまった"
    assert out.get("spent") is False


def test_a_missing_invoice_or_domain_is_refused(monkeypatch):
    """invoice_id / signature_scheme.domain は EIP-191 束縛メッセージの構成要素。
    欠けたまま進むと KeyError で落ちるが、その時点では既に送金済みになりうる。"""
    for over in ({"invoice_id": None}, {"signature_scheme": {}}, {"signature_scheme": None}):
        out, calls, fake = run_purchase(monkeypatch, [FakeResp(402, _challenge(**over))])
        assert out["spent"] is False
        assert "raw_sent" not in calls


def test_the_ordinary_quote_still_goes_through(monkeypatch):
    """回帰ガード: 本番の実価格（0.01 USDC）はガードに引っかからない。
    上の拒否群が「常に拒否する」だけの実装で通ってしまわない事を示す対照実験。"""
    out, calls, fake = run_purchase(monkeypatch,
                                    [FakeResp(402, _challenge(amount="0.01")),
                                     FakeResp(200, {"data": "d", "receipt": {"r": 1}})])
    assert out["data"] == "d" and out["tx_hash"].startswith("0x")
    assert calls["transfer_args"][1] == 10_000        # 0.01 USDC = 10000 minor units


# ══ 2. 送金後は、何があっても tx hash を返す ═════════════════════════════════════
# wait_for_transaction_receipt が例外を出すと、USDC は既に出ているのに
# トレースバックの中に唯一の手掛かりが消えていた。

def test_receipt_timeout_returns_the_tx_hash_instead_of_raising(monkeypatch):
    calls = {}
    monkeypatch.setenv("X402_AGENT_PRIVATE_KEY", TEST_PK)
    install_fake_web3(monkeypatch, calls, receipt_raises=TimeoutError("not mined in time"))
    fake = FakeRequests(get_responses=[FakeResp(402, make_challenge())])
    monkeypatch.setattr(srv, "requests", fake)
    monkeypatch.setattr(srv.time, "sleep", lambda s: None)

    out = srv.purchase()
    assert calls.get("raw_sent"), "前提: 送金は実際に行われている"
    assert out["spent"] is True
    assert out["tx_hash"].startswith("0x"), "支払い済みなのに tx hash が失われた"
    assert "not confirmed" in out["error"]


def test_the_receipt_wait_is_bounded(monkeypatch):
    """既定 180 秒。無指定だと web3 の既定に暗黙依存し、待ち時間がコード上に現れない。

    最初に「timeout を渡している事」を見る。srv.RECEIPT_TIMEOUT_S と直接比べると、
    修正前ソースでは定数が存在せず AttributeError になり、「値が不足していた事」ではなく
    「定数が増えた事」を証明してしまう。
    """
    out, calls, fake = run_purchase(monkeypatch,
                                    [FakeResp(402, make_challenge()),
                                     FakeResp(200, {"data": "d"})])
    assert calls["receipt_timeout"] is not None, "wait_for_transaction_receipt に timeout 未指定"
    assert calls["receipt_timeout"] == getattr(srv, "RECEIPT_TIMEOUT_S", None)


def test_a_non_json_unlock_response_keeps_the_tx_hash(monkeypatch):
    """Cloudflare の HTML チャレンジページが返ると .json() は ValueError。
    支払い済みの経路でそれが素通りすると、やはり tx hash が失われる。"""
    # _NotJson を使う。FakeResp(200, text="<html>…") は json_data 未指定で {} を返してしまい、
    # 「JSON でない応答」を再現できていない（＝落ちないのは当然で、何も証明しない）。
    out, calls, fake = run_purchase(
        monkeypatch,
        [FakeResp(402, make_challenge()),
         _NotJson(200, text="<html>attention required</html>")])
    assert out["tx_hash"].startswith("0x")
    assert out["spent"] is True


# ══ 3. 支払い資格情報はリダイレクトについて行かない ═══════════════════════════════

def test_payment_headers_are_not_followed_across_a_redirect(monkeypatch):
    """requests がホスト跨ぎで剥がすのは Authorization だけ。X-Payment-Signature の
    ような独自ヘッダは 302 の行き先まで付いて行く。"""
    out, calls, fake = run_purchase(monkeypatch,
                                    [FakeResp(402, make_challenge()),
                                     FakeResp(200, {"data": "d"})])
    unlock = fake.get_calls[-1]
    assert unlock["headers"].get("X-Payment-Signature"), "前提: 署名ヘッダを送っている"
    assert fake.get_kwargs[-1].get("allow_redirects") is False


def test_the_gasless_voucher_is_not_followed_across_a_redirect(monkeypatch):
    monkeypatch.setenv("X402_AGENT_PRIVATE_KEY", TEST_PK)
    acc = {"balance_units": 10_000, "price_units": 5_000, "next_nonce": 3,
           "voucher_message": "x402-spend:x402.obolpay.xyz:%s:3:5000" % TEST_ADDR}
    fake = FakeRequests(get_responses=[FakeResp(200, acc), FakeResp(200, {"data": "d"})])
    monkeypatch.setattr(srv, "requests", fake)
    assert srv.spend_gasless() == {"data": "d"}
    assert fake.get_kwargs[-1].get("allow_redirects") is False


# ══ 4. サーバが渡してきた文字列に、支払い鍵で無条件に署名しない ══════════════════
# spend_gasless は voucher_message をそのまま署名していた。書式は
# x402-spend:{domain}:{address}:{nonce}:{units} なので、支払われる側が
# 別の spender・別の nonce・別の金額を名乗れて、返ってくる署名はそれに対して有効。

def _gasless(monkeypatch, voucher, *, nonce=3, balance=10_000, price=5_000):
    monkeypatch.setenv("X402_AGENT_PRIVATE_KEY", TEST_PK)
    acc = {"balance_units": balance, "price_units": price, "next_nonce": nonce,
           "voucher_message": voucher}
    fake = FakeRequests(get_responses=[FakeResp(200, acc), FakeResp(200, {"data": "d"})])
    monkeypatch.setattr(srv, "requests", fake)
    return srv.spend_gasless(), fake


def test_a_voucher_naming_another_spender_is_refused(monkeypatch):
    other = "0x" + "99" * 20
    out, fake = _gasless(monkeypatch, "x402-spend:x402.obolpay.xyz:%s:3:5000" % other)
    # 「署名して次の呼び出しに進んだかどうか」を先に見る。out["error"] を先に読むと、
    # 修正前ソースは成功応答 {"data": ...} を返すので KeyError になり、赤の理由が
    # 「他人名義の債務証書に署名した」ではなく「error キーが無い」になってしまう。
    assert len(fake.get_calls) == 1, "他人を spender とする債務証書に署名して先に進んだ"
    assert "names spender" in out["error"]


def test_a_voucher_with_a_different_nonce_is_refused(monkeypatch):
    out, fake = _gasless(monkeypatch, "x402-spend:x402.obolpay.xyz:%s:99:5000" % TEST_ADDR)
    assert len(fake.get_calls) == 1, "こちらが送る nonce と異なる債務証書に署名して先に進んだ"
    assert "nonce" in out["error"]


def test_a_voucher_debiting_more_than_the_cap_is_refused(monkeypatch):
    """1.00 USDC = 1_000_000 minor units。5 USDC 分の債務証書には署名しない。"""
    out, fake = _gasless(monkeypatch, "x402-spend:x402.obolpay.xyz:%s:3:5000000" % TEST_ADDR)
    assert len(fake.get_calls) == 1, "上限を超える債務証書に署名して先に進んだ"
    assert "cap" in out["error"]


@pytest.mark.parametrize("voucher", [
    "please sign this unrelated string",
    "x402-spend:only:three:parts",
    "",
    None,
    12345,
], ids=["arbitrary-text", "wrong-arity", "empty", "null", "not-a-string"])
def test_an_unrecognized_voucher_is_refused(monkeypatch, voucher):
    out, fake = _gasless(monkeypatch, voucher)
    assert len(fake.get_calls) == 1, f"解釈できない voucher {voucher!r} に署名して先に進んだ"
    assert "error" in out


def test_the_real_voucher_format_is_still_signed(monkeypatch):
    """対照実験。本番実測の書式（GET /account/… の voucher_message）は通り、
    復元した署名者がこのウォレット自身である事まで確かめる。
    実測値: "x402-spend:x402.obolpay.xyz:0xf39f…:0:10000"
    """
    voucher = "x402-spend:x402.obolpay.xyz:%s:3:5000" % TEST_ADDR
    out, fake = _gasless(monkeypatch, voucher)
    assert out == {"data": "d"}
    sig = fake.get_calls[-1]["headers"]["X-Account-Voucher"]
    signer = Account.recover_message(encode_defunct(text=voucher), signature=sig)
    assert signer.lower() == TEST_ADDR


# ══ 5. LLM が選ぶ値が URL のパスに入る ════════════════════════════════════════

@pytest.mark.parametrize("addr", [
    "../admin",
    "0x1111111111111111111111111111111111111111/../../admin",
    "0x1111111111111111111111111111111111111111?admin=1",
    "0x1111111111111111111111111111111111111111#x",
    "0x111",
    "0xZZ11111111111111111111111111111111111111",
])
def test_a_malformed_address_never_reaches_the_url(monkeypatch, addr):
    """balance(address) は値をそのままパスへ連結していた。'?' や '../' が入れば、
    このツールが名乗っているのとは別のルートを叩く事になる。"""
    fake = FakeRequests(get_responses=[FakeResp(200, {"balance_units": 0})])
    monkeypatch.setattr(srv, "requests", fake)
    out = srv.balance(addr)
    assert "error" in out, f"{addr!r} が受理された"
    assert fake.get_calls == [], f"{addr!r} で HTTP リクエストが発生した"


def test_a_wellformed_address_still_works(monkeypatch):
    fake = FakeRequests(get_responses=[FakeResp(200, {"balance_units": 7})])
    monkeypatch.setattr(srv, "requests", fake)
    assert srv.balance("0x" + "11" * 20) == {"balance_units": 7}
    assert fake.get_calls[0]["url"] == srv.BASE + "/account/0x" + "11" * 20


@pytest.mark.parametrize("txh", ["0xdeadbeef", "deadbeef", "", "0x" + "de" * 31, "0x" + "zz" * 32])
def test_a_malformed_tx_hash_never_reaches_the_url(monkeypatch, txh):
    fake = FakeRequests(post_responses=[FakeResp(200, {"credited": True})])
    monkeypatch.setattr(srv, "requests", fake)
    assert "error" in srv.topup(txh)
    assert fake.post_calls == []


# ══ 6. JSON でない応答で落ちない ═══════════════════════════════════════════════
# ゲートウェイは Cloudflare の裏に居るので、悪い1分は JSON ではなく HTML を返す。
# 素の .json() はそれを requests 内部由来の ValueError にし、MCP クライアントは
# エージェントに不透明なクラッシュとして見せる。

_TOOL_ARGS = {"verify_receipt": ("m", "s"), "topup": ("0x" + "de" * 32,),
              "balance": ("0x" + "11" * 20,)}
_FREE_TOOLS = ["discover", "preview", "balance", "verify_receipt", "topup"]

# ツールごとに「本文を読みに行く」状態コードが違う。preview だけは 402 が正常系で、
# 200 を返すと .json() に到達する前に「expected 402, got 200」で戻ってしまう。
# 全ツールを 200 で回すと preview だけがその短絡のおかげで緑になり、
# 「非 JSON を処理できている」ではなく「非 JSON を読みすらしなかった」を緑と誤読する。
_NOTJSON_STATUS = {"preview": 402}


class _NotJson(FakeResp):
    """Cloudflare の HTML チャレンジページ相当。requests と同じく json() が ValueError。"""

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


@pytest.mark.parametrize("tool", _FREE_TOOLS)
def test_no_tool_raises_on_an_html_error_page(monkeypatch, tool):
    resp = _NotJson(_NOTJSON_STATUS.get(tool, 200),
                    text="<html>Attention Required! | Cloudflare</html>")
    fake = FakeRequests(get_responses=[resp], post_responses=[resp])
    monkeypatch.setattr(srv, "requests", fake)
    out = getattr(srv, tool)(*_TOOL_ARGS.get(tool, ()))
    assert isinstance(out, dict) and "error" in out


@pytest.mark.parametrize("tool", _FREE_TOOLS)
def test_no_tool_raises_when_the_gateway_is_unreachable(monkeypatch, tool):
    """接続エラーもツールの戻り値であって、例外ではない。"""
    real_exc = srv.requests.RequestException

    class _Dead:
        RequestException = real_exc

        def get(self, *a, **kw):
            raise real_exc("connection refused")
        post = get

    monkeypatch.setattr(srv, "requests", _Dead())
    out = getattr(srv, tool)(*_TOOL_ARGS.get(tool, ()))
    assert isinstance(out, dict) and "error" in out


# ══ 7. 鍵が無い時に KeyError を出さない ═══════════════════════════════════════

def test_spend_gasless_without_a_key_returns_an_error(monkeypatch):
    """os.environ["X402_AGENT_PRIVATE_KEY"] を直接引いており、未設定だと KeyError。
    MCP ツールの戻り値としては、エージェントが読める文になっていなければならない。"""
    monkeypatch.delenv("X402_AGENT_PRIVATE_KEY", raising=False)
    out = srv.spend_gasless()
    assert "error" in out and "X402_AGENT_PRIVATE_KEY" in out["error"]


def test_agent_address_without_a_key_is_a_refusal_not_a_keyerror(monkeypatch):
    """KeyError で無い事を見る。`pytest.raises(srv.PaymentRefused)` と書くと、修正前ソースでは
    その例外クラス自体が無く AttributeError で赤くなり、「例外の種類が悪かった事」ではなく
    「クラスが増えた事」を証明してしまう。"""
    monkeypatch.delenv("X402_AGENT_PRIVATE_KEY", raising=False)
    with pytest.raises(Exception) as e:
        srv._agent_address()
    assert not isinstance(e.value, KeyError), \
        "鍵未設定が生の KeyError として飛んでいる（呼び出し側で扱えない）"
    assert "X402_AGENT_PRIVATE_KEY" in str(e.value)


def test_an_unusable_key_is_an_error_not_a_crash(monkeypatch):
    """壊れた鍵は eth_account 内部の例外ではなく、ツールの戻り値として返す。"""
    monkeypatch.setenv("X402_AGENT_PRIVATE_KEY", "not-a-key")
    out = srv.balance()
    assert isinstance(out, dict) and "error" in out


# ══ 8. 支払い指示は書き換えられる経路で受け取らない ═════════════════════════════

def test_plain_http_is_refused_for_payment(monkeypatch):
    """支払い指示（金額・宛先・トークン）を平文 http で受け取ると、経路上の誰でも
    書き換えられる。402 の中身をそのまま署名する以上、これは「盗聴」ではなく「送金先の
    差し替え」を許す事になる。

    フェイクを噛ませるのは意図的で、これが無いと修正前ソースは実際に
    http://x402.example へ DNS を引きに行き、赤の理由が「名前解決に失敗した」になる。
    それではオフラインでない上に、平文で通信した事自体を証明できていない。"""
    monkeypatch.setattr(srv, "ENDPOINT", "http://x402.example/api/v1/protected-data")
    out, calls, fake = run_purchase(monkeypatch,
                                    [FakeResp(402, make_challenge()),
                                     FakeResp(200, {"data": "d", "receipt": {}})])
    assert not fake.get_calls, \
        f"平文 http の支払い指示を要求しに行った: {fake.get_calls[0]['url']}"
    assert "raw_sent" not in calls, "平文 http で受け取った指示のまま送金した"
    assert "https required" in out["error"]


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:8402/api/v1/protected-data",
                                      "http://localhost:8402/api/v1/protected-data"])
def test_loopback_http_still_buys(monkeypatch, endpoint):
    """対照実験（修正前ソースでも緑）。上の拒否が「http を一律に蹴る」実装だと、
    ゲートウェイをローカルで動かしての開発が止まる。ループバックは第三者が
    割り込める経路ではないので通す。

    ここで `srv._require_https` を直接呼ばないのは意図的で、修正前ソースにはその関数が
    無く AttributeError で赤くなる ＝「平文 http で支払っていた事」ではなく
    「関数が増えた事」を証明してしまうため。purchase() 越しに見れば、修正前・修正後の
    どちらでも購入が成立する事を同じ振る舞いとして確認できる。"""
    monkeypatch.setattr(srv, "ENDPOINT", endpoint)
    out, calls, fake = run_purchase(monkeypatch,
                                    [FakeResp(402, make_challenge()),
                                     FakeResp(200, {"data": "d", "receipt": {}})])
    assert "error" not in out, out
    assert calls.get("raw_sent"), "ループバック相手の購入が署名まで到達していない"
