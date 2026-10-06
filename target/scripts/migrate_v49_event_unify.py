#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_v49_event_unify.py — events[].event_code → events[].no で統一する移行スクリプト（v50、汎用）

【目的】
v43 で「接頭辞付き項番」を ID 体系として採用したが、events[].event_code フィールドのみ legacy 形式
（EV00, EV01, EV02 のような連番形式や、案件独自の旧形式）が更新対象外として残っていた。
v50 で events[].event_code を廃止し、events[].no（EV1, EV2, ...）に統一する。

【意味合い】
業務担当者向け仕様書では「ID は接頭辞付き項番（AR/IT/EV/.../FL）のみ」という設計原則。
event_code 旧形式が残ると、業務記述（remarks 等）に旧コードが滲み出し、§4.3 表で
表示変換しても中間 JSON 上にズレが残り続ける。本スクリプトで完全統一する。

【接続情報】
- 入力: target/intermediate/{機能名}.json（event_code が legacy 形式を含む）
- 出力: 同 JSON を上書き
  - events[].event_code は廃止、events[].legacy_event_code に旧値を保存
  - screen_layout.items[].event_code → items[].event_ref（events[].no を参照する形式に置換）
  - trigger_groups[].triggers[].event_code → triggers[].event_ref（同上）
  - その他、event_code を含む全フィールドを event_ref に rename + 値置換
- 仕様根拠: SKILL.md v50 改修履歴 / 15_中間JSONスキーマ.md v50 拡張仕様

【設計原則】
- 汎用: 機能名・event_code 形式（EV00 のような連番形式 / 案件独自の旧形式）に依存しない、events[].event_code → events[].no で動的にマップ構築
- 冪等: 既に event_ref に rename 済みのフィールドは再変換しない（events[].event_code が存在しなければ skip）
- 安全: バックアップを /tmp に保存
- legacy_id 保持: events[].legacy_event_code に旧値を保存し、業務トレーサビリティ維持
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from _config_loader import INTERMEDIATE_DIR, get_feature_name_from_argv

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
# 2026-10-05 汎用化: 既定の機能名を廃止し、対象画面を実行時に決める。
#   目的: 特定の画面に固定せず、どの機能の中間 JSON でも移行できるようにする。
#   意味合い: 機能名は第1引数か環境変数 FEATURE_NAME で与える。どちらも無ければ
#             _config_loader 側が SystemExit で止める（このファイルでは既定値を持たない）。
#   接続情報: 機能名の解決は _config_loader.get_feature_name_from_argv()、
#             置き場は _config_loader.INTERMEDIATE_DIR（target/intermediate）。
#             INTERMEDIATE_JSON は本ファイルの main() が読み書きする。
FEATURE_NAME = get_feature_name_from_argv()
INTERMEDIATE_JSON = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"


def backup(intermediate_json: Path) -> Path:
    """現状を /tmp にバックアップ。意味合い: v50 移行前の状態を復元できるよう保存"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = Path(f"/tmp/{intermediate_json.stem}_pre-v50_{ts}.json")
    shutil.copy(intermediate_json, backup_path)
    return backup_path


def build_event_code_to_no_map(data: dict) -> dict:
    """events[].event_code → events[].no のマップを構築。

    意味合い: items / triggers 等が events[].event_code を参照していたため、
              新体系 events[].no に変換するための逆引き辞書。
    """
    mapping = {}
    for e in data.get("events", []):
        ec = e.get("event_code", "")
        no = e.get("no", "")
        if ec and no:
            mapping[ec] = no
    return mapping


def unify_events_section(data: dict) -> int:
    """events[].event_code を legacy_event_code にコピー、event_code は no で上書き or 削除。

    意味合い: events[] 自身の構造変更。旧 event_code を legacy_event_code に保存し、
              event_code フィールド自体は廃止（events[].no が ID として残る）。
    返り値: 変換した events エントリ数
    """
    count = 0
    for e in data.get("events", []):
        ec = e.get("event_code")
        if ec is None:
            continue  # 既に v50 化済
        no = e.get("no", "")
        # legacy として保存
        if e.get("legacy_event_code") is None:
            e["legacy_event_code"] = ec
        # event_code フィールド廃止
        del e["event_code"]
        count += 1
    return count


def rename_event_code_references(data: dict, ec_to_no: dict) -> tuple:
    """中間 JSON 内の event_code 参照を event_ref に rename + 値変換。

    意味合い: screen_layout.items[].event_code / trigger_groups[].triggers[].event_code /
              process_flows[].steps[].event_ref / event_processes[].event_code 等を
              event_ref に rename し、値を新体系 events[].no に変換。
              event_ref はすでに新体系の場合もあるため、変換できないものはそのまま残す。
    返り値: (rename した箇所数, 値変換した箇所数)
    """
    renamed = 0
    converted = 0

    def walk(node, parent_key=None):
        nonlocal renamed, converted
        if isinstance(node, dict):
            # What: この node で event_code → event_ref の rename(値変換込み)を済ませたかの印。
            # Why: rename で入れた新番号(例 EV10)が、未変換の旧コード(例 EV10)と同じ文字列だと、
            #      直後の「既存 event_ref の legacy 値変換」が再度 ec_to_no を引いて EV11 へずらす
            #      (二重変換。画面項目・event_processes の参照が別イベントを指し、重なりも生じた)。
            #      1つの node の値変換は1回だけにするため、rename 済みの node では次の分岐を飛ばす。
            # Where: 対応表 ec_to_no は main → build_event_code_to_no_map が作る。
            just_renamed = False
            # キー event_code を event_ref に rename
            if "event_code" in node:
                old_val = node.pop("event_code")
                # 既存 event_ref があれば優先、なければ event_code から rename
                if "event_ref" not in node:
                    # 値変換: 旧 event_code → 新 events[].no
                    new_val = ec_to_no.get(old_val, old_val)
                    node["event_ref"] = new_val
                    renamed += 1
                    just_renamed = True  # 上の just_renamed 宣言のコメント参照(再変換の防止)
                    if new_val != old_val:
                        converted += 1
            # 既存 event_ref も legacy 値が残っていれば変換(rename 直後の新値は対象外)
            if "event_ref" in node and not just_renamed:
                cur = node["event_ref"]
                if isinstance(cur, str) and cur in ec_to_no:
                    new_val = ec_to_no[cur]
                    if new_val != cur:
                        node["event_ref"] = new_val
                        converted += 1
                elif isinstance(cur, list):
                    new_list = []
                    for v in cur:
                        if isinstance(v, str) and v in ec_to_no:
                            mapped = ec_to_no[v]
                            if mapped != v:
                                converted += 1
                            new_list.append(mapped)
                        else:
                            new_list.append(v)
                    node["event_ref"] = new_list
            # trigger_events も配列で event_code 参照を持つ
            if "trigger_events" in node and isinstance(node["trigger_events"], list):
                new_list = []
                for v in node["trigger_events"]:
                    if isinstance(v, str) and v in ec_to_no:
                        mapped = ec_to_no[v]
                        if mapped != v:
                            converted += 1
                        new_list.append(mapped)
                    else:
                        new_list.append(v)
                node["trigger_events"] = new_list
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, parent_key)

    walk(data)
    return renamed, converted


def main():
    if not INTERMEDIATE_JSON.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {INTERMEDIATE_JSON}", file=sys.stderr)
        sys.exit(1)

    backup_path = backup(INTERMEDIATE_JSON)
    print(f"[INFO] バックアップ: {backup_path}")

    with INTERMEDIATE_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. event_code → no のマップ構築
    ec_to_no = build_event_code_to_no_map(data)
    print(f"[INFO] event_code → no マップ構築: {len(ec_to_no)} エントリ（例: {list(ec_to_no.items())[:3]}）")

    # 2. events[] 自身の event_code → legacy_event_code に移動、event_code 廃止
    unified = unify_events_section(data)
    print(f"[OK] events[] event_code → legacy_event_code 移動 + event_code 廃止: {unified} 件")

    # 3. 参照フィールド event_code → event_ref に rename + 値変換
    renamed, converted = rename_event_code_references(data, ec_to_no)
    print(f"[OK] event_code 参照 → event_ref に rename: {renamed} 件 / 値変換: {converted} 件")

    with INTERMEDIATE_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[OK] 保存先: {INTERMEDIATE_JSON}")


if __name__ == "__main__":
    main()
