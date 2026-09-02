"""x402 MCP サーバのオフラインスモークテスト（ネットワーク/ウォレット不要）。

意図: 依存や公式SDKの破壊的変更でサーバが import すら出来なくなる回帰を CI で捕まえる。
（mcp 2.0 が FastMCP を mcp.server.fastmcp から削除し、無固定の mcp[cli] で
サーバが起動不能になった事故の再発防止。2026-09: mcp>=2.1.1 に移行し、
FastMCP→MCPServer（mcp.server.mcpserver）に追従済み。）
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 事故防止: 実鍵が環境にあってもテストでは絶対に使わない
os.environ.pop("X402_AGENT_PRIVATE_KEY", None)

import x402_mcp_server as srv  # noqa: E402

TOOLS = ["discover", "preview", "purchase", "verify_receipt",
         "balance", "topup", "spend_gasless"]


def test_server_imports_and_is_named():
    # import が通ること自体が最重要（FastMCP の所在破壊を検知）
    assert srv.mcp.name == "x402-obolpay"


def test_all_tools_present_and_callable():
    for name in TOOLS:
        assert callable(getattr(srv, name, None)), f"tool {name} が無い/呼べない"


def test_endpoints_are_https():
    assert srv.BASE.startswith("https://")
    assert srv.ENDPOINT.endswith("/api/v1/protected-data")


def test_balance_offline_requires_address_or_key(monkeypatch):
    monkeypatch.delenv("X402_AGENT_PRIVATE_KEY", raising=False)
    # アドレスも鍵も無ければ通信せずエラーを返す（オフライン分岐）
    assert srv.balance() == {"error": "provide address or set X402_AGENT_PRIVATE_KEY"}


def test_agent_address_derivation(monkeypatch):
    # 周知の公開テスト鍵(Hardhat #0) → 既知アドレス。eth_account 連携のオフライン検証
    monkeypatch.setenv(
        "X402_AGENT_PRIVATE_KEY",
        "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")
    assert srv._agent_address() == "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
