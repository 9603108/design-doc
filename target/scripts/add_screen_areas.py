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


def sync_initialization_screen_no(data):
    """initialization.items[].screen_no を、同じ順の screen_layout.items[].screen_no から写す。写した件数を返す。

    目的: 初期表示後の画面項目表(docx)のエリア# 列が空にならないようにする。
          initialization.items は screen_layout.items とは別に作られ、screen_no を持たないため。
    意味合い: 件数が違うときは対応が決まらないので全体を触らない。項目名が違う行も触らない
              (推測で写さない)。既に screen_no がある行は上書きしない(冪等)。
    """
    init_items = (data.get("initialization") or {}).get("items") or []
    layout_items = (data.get("screen_layout") or {}).get("items") or []
    if not init_items:
        return 0
    if len(init_items) != len(layout_items):
        print(f"[INFO] initialization.items[] を写さない(件数 {len(init_items)} と screen_layout.items[] {len(layout_items)} が違う)")
        return 0
    copied = 0
    mismatched = 0
    for ini, lay in zip(init_items, layout_items):
        if ini.get("item_name") != lay.get("item_name"):
            mismatched += 1
            continue
        if ini.get("screen_no") or not lay.get("screen_no"):
            continue
        ini["screen_no"] = lay["screen_no"]
        copied += 1
    if mismatched:
        print(f"[WARN] initialization.items[] の項目名が screen_layout.items[] と違う行 {mismatched} 件は触らない")
    return copied


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

    # 2. items[] の area_ref と screen_no を揃える。area_ref が areas[] にあればそれを正とし screen_no を写す
    #    (area → screen_no は一意。複数 area が同じ screen_no を持てるので逆は一意でない。委譲の単位 S1 は area_ref を書く)。
    #    area_ref が無い・解決しない項目だけ、従来どおり screen_no から area_ref を付ける
    area_no_to_screen_no = {a["area_no"]: a["screen_no"] for a in screen_areas}
    assigned = 0
    for it in items:
        if it.get("area_ref") in area_no_to_screen_no:
            it["screen_no"] = area_no_to_screen_no[it["area_ref"]]
            assigned += 1
            continue
        sn = str(it.get("screen_no", ""))
        if sn in screen_no_to_area_no:
            it["area_ref"] = screen_no_to_area_no[sn]
            assigned += 1

    # 2b. 2026-10-13 api_spec[].response_to_screen_mapping[].screen_no を screen_item_name から写す
    #     目的: screen_no は項目名から機械で決まるので、委譲の単位 S2 に書かせない(書いてあっても上書きする)。
    #     意味合い: 2. で揃えた items の screen_no を正とする。引けない名前は触らない
    #               (verify_partials.py --units の [参照] が S2 へ差し戻す。verify_intermediate.py も WARN を出す)。
    item_screen_no = {it.get("item_name"): it.get("screen_no") for it in items if it.get("item_name")}
    for api in data.get("api_spec", []) or []:
        for m in api.get("response_to_screen_mapping") or []:
            if m.get("screen_item_name") in item_screen_no:
                m["screen_no"] = item_screen_no[m["screen_item_name"]]

    # 2c. 初期処理の項目表(initialization.items[])の screen_no を、2. で揃えた画面項目から写す(2026-10-14)
    #     意味合い: 2. の後に行う。写す元の screen_no が揃っている必要があるため。件数・名前が合わない行は触らない
    synced = sync_initialization_screen_no(data)
    print(f"[OK] initialization.items[] に screen_no を {synced} 件写した")

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
