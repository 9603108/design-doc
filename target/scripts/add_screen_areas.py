#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_screen_areas.py — screen_layout.areas[] を追加し、items[].area_ref を screen_no から自動付与する（v20 で新設、v30 で外部設定化）

【目的】
画面用語の権威ソースとして screen_layout.areas[] を定義する。
events[]/trigger_groups[]/front_processes[]/backend_processes[].response_to_screen_mapping 等から
画面項目に言及する際の語彙は、ここで定義された area_name / item_name と厳密一致させる必要がある（v20 整合性ルール 18-20）。

【意味合い】
v19 まで screen_layout.items[] だけが画面用語の定義源だった結果、events[].location に
「ワークフローステッパー[5]」「テーブル[43]」のような items に存在しない名称が出現していた。
v20 で areas[] を導入し、業務担当者語彙の画面エリア名を明示することで cross-reference を成立させる。
v30 でハードコード辞書 SCREEN_AREAS を screen_config.json (areas[]) に外出しし、
本スクリプトを画面非依存（汎用化）にした。

【接続情報】
- 入力: target/intermediate/{機能名}.json + target/intermediate/{機能名}_screen_config.json
        （機能名は第1引数か環境変数 FEATURE_NAME で与える）
- 出力: 同 JSON を上書き
- 仕様根拠: 15_中間JSONスキーマ.md「screen_layout」セクション（v20 拡張）、SKILL.md「Phase 3-4 外部設定ファイル化」（v30）
"""
import json
import sys
from pathlib import Path

# v30: ハードコード辞書を screen_config.json に集約、_config_loader 経由で取得する
from _config_loader import (
    get_areas,
    get_area_pattern_subdivisions,
    get_feature_name_from_argv,
    INTERMEDIATE_DIR,
)

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
# 2026-10-05 汎用化: 機能名の既定値(_config_loader が持っていた既定の機能名)を廃止した。
# 目的: 特定画面の中間JSONを固定で処理せず、第1引数か環境変数 FEATURE_NAME で与えた画面を処理する。
# 意味合い: モジュール先頭で1回だけ解決する。どちらも無ければ _config_loader 側が
#          SystemExit("feature_name 未指定: ...") で止める(本ファイルで独自の既定値・止め方を持たない)。
# 接続情報: FEATURE_NAME は main() の get_areas / get_area_pattern_subdivisions に明示で渡す
#          (アクセサは引数なしだと sys.argv を見ないため、第1引数だけで渡した機能名を拾えない)。
#          INTERMEDIATE_JSON は main() が読み書きする target/intermediate/{機能名}.json。
FEATURE_NAME = get_feature_name_from_argv()
INTERMEDIATE_JSON = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"


def main():
    # v30: screen_config.json から画面エリア定義をロード（ハードコード SCREEN_AREAS 廃止）
    # 意味合い: 画面別ハードコードを画面別 JSON に外出しすることで、本スクリプトは画面非依存の汎用処理になる
    screen_areas = get_areas(FEATURE_NAME)
    # screen_no → area_no の単純マップ（areas[] から動的に生成、v30 で SCREEN_NO_TO_AREA_NO ハードコード廃止）
    screen_no_to_area_no = {a["screen_no"]: a["area_no"] for a in screen_areas}

    if not INTERMEDIATE_JSON.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {INTERMEDIATE_JSON}", file=sys.stderr)
        sys.exit(1)

    with INTERMEDIATE_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    sl = data.get("screen_layout", {})
    items = sl.get("items", []) or []

    # 1. areas[] を追加（既存があれば上書き）— v30: screen_config.json 経由
    sl["areas"] = screen_areas

    # 2. items[] に area_ref を screen_no ベースで付与
    assigned = 0
    for it in items:
        sn = str(it.get("screen_no", ""))
        if sn in screen_no_to_area_no:
            it["area_ref"] = screen_no_to_area_no[sn]
            assigned += 1

    # 3. v32: area_pattern_subdivisions[] を screen_layout に伝播
    # 意味合い: generate_docx.js は中間JSON のみを読むため、screen_config.json の subdivision 定義を
    #          中間JSON 経由で渡す。明細グリッドのエリアをパターン別に分割表示する場合などに使われる。
    subdivisions = get_area_pattern_subdivisions(FEATURE_NAME)
    if subdivisions:
        sl["area_pattern_subdivisions"] = subdivisions

    data["screen_layout"] = sl

    with INTERMEDIATE_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[OK] screen_layout.areas[] を {len(screen_areas)} エリア定義")
    for a in screen_areas:
        item_count = sum(1 for it in items if str(it.get("screen_no")) == a["screen_no"])
        print(f"  {a['area_no']} [{a['screen_no']}] {a['area_name']} ({item_count} items)")
    print(f"[OK] items[] に area_ref を {assigned}/{len(items)} 件付与")
    print(f"[OK] 保存先: {INTERMEDIATE_JSON}")


if __name__ == "__main__":
    main()
