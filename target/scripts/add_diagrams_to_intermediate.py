#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_diagrams_to_intermediate.py — 中間JSON に diagrams[] セクションを追加する（v3.5 で新設、v30 で外部設定化）

【目的】
中間JSON（target/intermediate/{機能名}.json）の diagrams キーに、各構成図の spec を集約する。
17_構成図生成.md / 15_中間JSONスキーマ.md「diagrams」の契約に従う。

【意味合い】
v29 までは build_process_structure_spec() / build_screen_structure_spec() / build_ipo_flowchart_spec()
の3関数が画面別 spec をハードコードしていた。
v30 で screen_config.json の diagrams セクションに 3 spec を全て集約し、本スクリプトは
「screen_config から各 diagram_id の spec を読み、ファイルパス（html/svg/png）を補完して
 中間JSON に書き戻すだけ」の汎用処理になった。次画面追加時は screen_config.json に
 diagrams.* を追加するだけで OK。

【接続情報】
- 入力: target/intermediate/{機能名}_screen_config.json
- 出力: target/intermediate/{機能名}.json に diagrams キーを追加（上書き保存）
- 次工程: diagram-design スキル or build_ipo_flowchart_svg.py 経由で target/diagrams/*.html を生成
- 仕様根拠: 15_中間JSONスキーマ.md「diagrams」セクション、SKILL.md「Phase 3-4 外部設定ファイル化」（v30）
"""
import json
from pathlib import Path

# v30: ハードコード辞書を screen_config.json に集約、_config_loader 経由で取得する
# 2026-07-19 多画面対応: get_feature_name_from_argv を追加し、sys.argv[1] / FEATURE_NAME 環境変数
#            の順で画面名を解決できるようにする（apply_trigger_dispatch.py と同一パターン）
# 2026-10-05 汎用化: 既定の機能名（_config_loader の旧定数）は廃止されたため import しない。
#            機能名が第1引数にも環境変数にも無ければ、_config_loader 側が SystemExit で止める。
# 2026-10-05 汎用化: 中間JSON の置き場は _config_loader の INTERMEDIATE_DIR を使う。
#            目的: main() が中間JSON のパスを自前で組み立てるのをやめる。
#            意味合い: 置き場の定義を _config_loader の1か所に寄せ、screen_config / project_config を
#                      読む側（_config_loader）と、中間JSON を読み書きする側（本スクリプト）の食い違いを防ぐ。
#            接続情報: 定義元は _config_loader.py の INTERMEDIATE_DIR（SKILL_DIR / "target" / "intermediate"）。
#                      使うのは本ファイルの main()。
from _config_loader import get_diagram_spec, get_all_diagram_ids, get_feature_name_from_argv, INTERMEDIATE_DIR

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
DIAGRAMS_DIR = SKILL_DIR / "target" / "diagrams"
DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)


def build_diagram_spec(diagram_id: str, feature_name: str) -> dict:
    """screen_config.json の diagrams[diagram_id] を読み、ファイルパスを補完して中間JSON 用の spec を生成する。

    意味合い: screen_config の diagrams セクションは「論理構造（nodes/edges/structure/spec）」のみを持ち、
              中間JSON に書き出す際に必要な html_path / svg_path / png_path はファイル命名規約から組み立てる。
              これにより screen_config を環境非依存（パス依存なし）に保てる。
              2026-07-19 多画面対応: 複数画面の diagrams が同一 DIAGRAMS_DIR に出力されるため、
              全画面で "{feature_name}_{diagram_id}" プレフィックスを付けてファイル名衝突を防ぐ。
              2026-10-05 汎用化: 「既定画面だけ無プレフィックス」の分岐は廃止した（既定の機能名を持たないため）。
              ここで組み立てる stem は、build_generic_diagram_svg.py / build_ipo_flowchart_svg.py が
              実際に出力するファイル名（{機能名}_{diagram_id}.html、IPO 図は {機能名}_ipo_flowchart.html）と
              同じ規則に揃える。食い違うと中間JSON のパスが実在しないファイルを指す。
              機能名そのものは main() が get_feature_name_from_argv() で解決して渡す。

    引数:
        diagram_id: "process_structure" / "screen_structure" / "ipo_flowchart" 等
        feature_name: 画面の機能名（例: "{機能名}"）。ファイル名プレフィックスに使用。

    返り値:
        中間JSON の diagrams[] 1要素分の dict（id, type, title, doc_section, spec, html_path, svg_path, png_path, embed_size）
    """
    src = get_diagram_spec(diagram_id, feature_name)
    file_stem = f"{feature_name}_{diagram_id}"
    return {
        "id": diagram_id,
        "type": src["type"],
        "title": src["title"],
        "doc_section": src["doc_section"],
        "spec": src["spec"],
        "html_path": str(DIAGRAMS_DIR / f"{file_stem}.html"),
        "svg_path":  str(DIAGRAMS_DIR / f"{file_stem}.svg"),
        "png_path":  str(DIAGRAMS_DIR / f"{file_stem}.png"),
        "embed_size": src.get("embed_size", {"width_px": 600, "height_px": 480})
    }


def main():
    # 2026-07-19 多画面対応: sys.argv[1] / FEATURE_NAME 環境変数の順で画面名を解決
    # 2026-10-05 汎用化: 既定の機能名は廃止。どちらも無ければ _config_loader が SystemExit で止める
    # （apply_trigger_dispatch.py の main() と同一パターン）。旧モジュール直下定数 INTERMEDIATE をローカル変数化。
    feature_name = get_feature_name_from_argv()
    # 2026-10-05 汎用化: パスの組み立てを _config_loader の INTERMEDIATE_DIR に揃えた（指す場所は従来と同じ）。
    # 読み込みと上書き保存の両方がこの INTERMEDIATE を使う。
    INTERMEDIATE = INTERMEDIATE_DIR / f"{feature_name}.json"
    if not INTERMEDIATE.exists():
        raise FileNotFoundError(f"中間JSONが見つからない: {INTERMEDIATE}")
    with INTERMEDIATE.open(encoding="utf-8") as f:
        d = json.load(f)

    diagrams = d.get("diagrams") or []
    # v30: screen_config.json の diagrams キー一覧から全 diagram を構築（旧版の build_*_spec 個別呼出廃止）
    # v26 で Swimlane / Sequence は廃止、Flowchart 採用に確定（ユーザー比較結果）
    # 2026-07-19 多画面対応: get_all_diagram_ids/build_diagram_spec に feature_name を明示伝搬
    new_diagrams = [build_diagram_spec(did, feature_name) for did in get_all_diagram_ids(feature_name)]
    new_ids = {x["id"] for x in new_diagrams}
    diagrams = [x for x in diagrams if x.get("id") not in new_ids]
    diagrams.extend(new_diagrams)
    d["diagrams"] = diagrams

    with INTERMEDIATE.open("w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "intermediate": str(INTERMEDIATE),
        "diagrams_added": [x["id"] for x in diagrams],
        "diagrams_detail": [
            {
                "id": x["id"],
                "type": x["type"],
                "html_path": x["html_path"],
                "svg_path": x["svg_path"],
            } for x in new_diagrams
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
