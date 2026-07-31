"""撤去済みカテゴリを、このリポジトリが二度と宣伝しないための固定。

2026-07-31、x402-gateway は J-Quants (JPX) 由来の3カテゴリを撤去した。契約は
有料 Standard プランだが**個人利用ライセンス**で、分析結果の継続反復した第三者
提供を認めていないためである（詳細は x402-gateway の docs/WITHDRAWAL_2026-07-31.md）。

このリポジトリは**公開**であり、`examples/` は「こう呼べば買える」と読者に教える
面である。撤去した名前がここに残っていると、データ本体を配っていなくても
「無許諾データを再販している」と公に名乗り続けることになる。実際 2026-07-31 時点で
raw.githubusercontent.com から HTTP 200 で読める状態だった（実測）。

なぜ grep のテストか: examples は import して実行できない（ネットワークと鍵が要る）。
だが「文字列として名乗っていないこと」は実行せずに測れる。測れるものを測る。
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# x402-gateway/withdrawn.py と同じ台帳。あちらが単一の真実の源で、ここはその写し。
WITHDRAWN = ("jp-equity-convergence", "jp-disclosure-events", "jp-earnings-surprise")

# この記録自身は名前を書かないと説明できない。ここだけは除外する。
ALLOWED = {"tests/test_no_withdrawn_categories.py"}

SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".txt", ".cfg"}


def _tracked_files() -> list[pathlib.Path]:
    out = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in SUFFIXES:
            continue
        rel = p.relative_to(ROOT)
        if any(part in {".git", ".venv", "__pycache__", "node_modules"} for part in rel.parts):
            continue
        out.append(p)
    return out


def test_the_repository_scan_actually_sees_files():
    """対照群。走査が0件なら、下の「見つからない」は何も証明していない。"""
    files = _tracked_files()
    assert len(files) > 5, f"走査対象が {len(files)} 件しかない — 下の検査は無意味"
    assert any(f.name == "agent_tool.py" for f in files), \
        "examples/agent_tool.py を走査できていない（かつて撤去名が載っていた実ファイル）"


@pytest.mark.parametrize("slug", WITHDRAWN)
def test_no_file_advertises_a_withdrawn_category(slug: str):
    hits = []
    for p in _tracked_files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        try:
            if slug in p.read_text(encoding="utf-8", errors="ignore"):
                hits.append(rel)
        except OSError:  # pragma: no cover - 読めないファイルは対象外
            continue
    assert not hits, (
        f"{slug} は 2026-07-31 にデータライセンス上の理由で撤去された。"
        f"まだ名乗っているファイル: {hits}")
