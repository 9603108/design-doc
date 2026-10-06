#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_v48_to_v49.py — v48 中間 JSON を v49 構造に変換する移行スクリプト（汎用）

【目的】
v49 で確定した「呼ぶ側／呼ばれる側」設計徹底のため、以下の構造変更を行う。

(1) backend_processes[].response_spec.response_to_screen_mapping[] を front_processes[].steps[] へ移動
    - 旧: バック処理側に「JSON → 画面項目」マッピングが書かれていた（呼ぶ側／呼ばれる側 設計逸脱）
    - 新: フロント処理側の steps[] に「response_mapping」kind の新 step として挿入
    - 各 mapping entry に item_ref_no（IT#）を screen_layout.items[] から逆引き付与

(2) backend_processes[].response_spec.items[] に source / transform_backend フィールドを空のまま追加
    - 後段 Phase 1 Agent または手動編集で埋める前提
    - source: { type: 'request'|'db'|'calculated'|'merged'|'const', source_ref, source_column }
    - transform_backend: バック側 Python での加工内容（業務語彙）

(3) backend_processes[].processing[] の db_operation step に sub_no 採番
    - 同一バック処理内の db_operation 出現順に 1, 2, 3 を採番
    - generate_docx.js が表示時に「②-1 / ②-2 / ②-3」と組み立てる

(4) v42 設計逸脱の解消: 初期処理 FRX 内に残る「URL パラメータ判定 step」を strip
    - 「URLパラメータを解析し、mode・id が未指定であることを確認する」等
    - 判定は §5.2.1 [TR1] dispatch で行うため、呼ばれる側に書かない
    - screen_config.json の strip_step_keywords で挙動を拡張

【意味合い】
v18 「呼ぶ側／呼ばれる側」設計、v42 「初期処理の呼ぶ側／呼ばれる側分離」、v47 「処理仕様表 3 列構造」、
v48 「id_scheme 経由ヘッダ」と続いた一連の汎用化の集大成として、v49 では「画面項目マッピングは
フロント処理に書く」「DB SQL 詳細は backend 内で完結する（§6.11 廃止）」を実現する。

【接続情報】
- 入力: target/intermediate/{機能名}.json（v48 形式）
- 出力: 同 JSON を上書き（v49 形式）
- 後続: apply_id_scheme.py 再実行（IT 接頭辞は v48 までで採番済だが冪等性のため）
        generate_docx.js で v49 docx 生成
- 仕様根拠: SKILL.md v49 改修履歴 / 15_中間JSONスキーマ.md v49 拡張仕様

【設計原則】
- 冪等: 既に v49 化されたエントリは再変換しない（response_mapping kind step / source フィールド存在で判定）
- 汎用: 機能名・接頭辞・パターンに依存しない（screen_config.json の strip_step_keywords は拡張可能）
- 安全: backup を /tmp に保存（v48 形式が必要な場合の復元用）
"""
import json
import re
import sys
import shutil
from pathlib import Path
from datetime import datetime

from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    load_screen_config,
)

# 2026-10-05 汎用化: 機能名の既定値(_config_loader の旧 DEFAULT 定数)を廃止したため、
# 機能名はここで1回だけ解決する(第1引数 > 環境変数 FEATURE_NAME。どちらも無ければ
# _config_loader 側が SystemExit「feature_name 未指定」で止める)。
# 意味合い: _config_loader.resolve_feature_name() は sys.argv を見ないので、第1引数だけで
#           渡された機能名をアクセサへ届けるには、ここで解決した値を明示で渡す必要がある。
# 接続情報: INTERMEDIATE_JSON(main の読み書き先)と、main 内の load_screen_config(FEATURE_NAME) が使う。
FEATURE_NAME = get_feature_name_from_argv()
INTERMEDIATE_JSON = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"


def backup_v48(intermediate_json: Path) -> Path:
    """v48 形式の現状を /tmp にバックアップ"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = Path(f"/tmp/{intermediate_json.stem}_v48_backup_{ts}.json")
    shutil.copy(intermediate_json, backup_path)
    return backup_path


def build_item_lookup(data: dict) -> dict:
    """screen_layout.items[] から (area_no, item_name) → item_no 逆引き辞書を構築。

    意味合い: response_to_screen_mapping の (screen_area_no, screen_item_name) ペアから
              対応する画面項目の no（IT#）を逆引き付与するための辞書。
              area_no と item_name の組合せが業務的に一意である前提（同じエリアで同じ画面項目名は重複しない）。
    """
    items = data.get("screen_layout", {}).get("items", [])
    lookup = {}
    for it in items:
        area_no = it.get("area_ref") or it.get("area_no") or ""
        name = it.get("item_name", "")
        no = it.get("no", "")
        if name:
            lookup[(area_no, name)] = no
            # area 指定なしフォールバックも入れる（area 不明時の最後の手段）
            if name not in lookup:
                lookup[name] = no
    return lookup


def find_item_no(mapping_entry: dict, item_lookup: dict) -> str:
    """response_to_screen_mapping エントリから item_ref_no を逆引きする。

    意味合い: screen_area_no（または screen_no）+ screen_item_name から item_lookup を引く。
              一致が無ければ item_name だけで再検索。それでも無ければ空文字を返す（後段で手動補完）。
    """
    area_no = mapping_entry.get("screen_area_no") or mapping_entry.get("screen_no") or ""
    name = mapping_entry.get("screen_item_name") or ""
    if not name:
        return ""
    # (area_no, name) 完全一致
    if (area_no, name) in item_lookup:
        return item_lookup[(area_no, name)]
    # name のみ
    if name in item_lookup:
        return item_lookup[name]
    return ""


def migrate_response_to_screen_mapping(data: dict, item_lookup: dict) -> int:
    """各 backend_processes の response_to_screen_mapping を front_processes の steps[] に移動。

    意味合い: BE# ごとに response_to_screen_mapping を取得し、その BE# を呼ぶ全 FR# の steps[] を走査、
              該当の backend_call step を見つけてその直後に kind='response_mapping' の新 step を挿入。
              既に同 backend_id の response_mapping step がある FR# はスキップ（冪等性）。
              元の backend_processes[].response_spec.response_to_screen_mapping[] は削除（責務を front 側に移譲）。
    返り値: 挿入した response_mapping step の総数
    """
    inserted = 0
    backend_to_mapping = {}  # backend_id → mappings (with item_ref_no)
    for bp in data.get("backend_processes", []):
        bid = bp.get("process_id", "")
        rs = bp.get("response_spec", {}) or {}
        mappings = rs.get("response_to_screen_mapping") or []
        if not mappings or not bid:
            continue
        # item_ref_no 逆引き付与
        enriched = []
        for m in mappings:
            entry = {
                "api_field_path": m.get("api_field_path", ""),
                "screen_area_no": m.get("screen_area_no") or m.get("screen_no") or "",
                "item_ref_no": find_item_no(m, item_lookup),
                "screen_item_name": m.get("screen_item_name", ""),
                "transform": m.get("transform", "")
            }
            enriched.append(entry)
        backend_to_mapping[bid] = enriched
        # 元エントリを削除（責務移譲）
        if "response_to_screen_mapping" in rs:
            del rs["response_to_screen_mapping"]

    # front_processes の steps[] を走査して response_mapping step を挿入
    for fp in data.get("front_processes", []):
        steps = fp.get("steps", [])
        new_steps = []
        existing_mapping_bids = set()
        # 既に v49 化されている場合は backend_id を記録（冪等性）
        for s in steps:
            if s.get("kind") == "response_mapping":
                rm = s.get("response_mapping") or {}
                bid = rm.get("backend_id", "")
                if bid:
                    existing_mapping_bids.add(bid)

        for s in steps:
            new_steps.append(s)
            if s.get("kind") in ("backend_call", "api_call"):
                bc = s.get("backend_call") or {}
                bid = bc.get("backend_id", "")
                if not bid or bid not in backend_to_mapping:
                    continue
                if bid in existing_mapping_bids:
                    continue  # 既に v49 化済
                # 既に直後に response_mapping がある場合はスキップ（連続走査）
                next_idx = steps.index(s) + 1
                if next_idx < len(steps) and steps[next_idx].get("kind") == "response_mapping":
                    nrm = steps[next_idx].get("response_mapping") or {}
                    if nrm.get("backend_id") == bid:
                        continue
                # 新 step を挿入
                new_steps.append({
                    "step_no": None,  # 後段 renumber で再採番
                    "kind": "response_mapping",
                    "description": f"[{bid}] のレスポンスを画面項目にマッピング",
                    "response_mapping": {
                        "backend_id": bid,
                        "mappings": backend_to_mapping[bid]
                    }
                })
                existing_mapping_bids.add(bid)
                inserted += 1

        # step_no 再採番
        for i, s in enumerate(new_steps, start=1):
            s["step_no"] = i
        fp["steps"] = new_steps

    return inserted


def add_response_spec_source_fields(data: dict) -> int:
    """各 backend_processes.response_spec.items[] に source / transform_backend を空のまま追加。

    意味合い: v49 で「データ起源」「バック側の加工」列を docx 表示するため、items[] に新フィールドを追加。
              既存値は維持し、欠落フィールドのみ空のテンプレートで埋める（後段 Agent or 手動編集前提）。
    返り値: 追加した items 数
    """
    added = 0
    for bp in data.get("backend_processes", []):
        rs = bp.get("response_spec", {}) or {}
        items = rs.get("items", []) or []
        for it in items:
            if "source" not in it:
                it["source"] = {"type": "", "source_ref": "", "source_column": ""}
                added += 1
            if "transform_backend" not in it:
                it["transform_backend"] = ""
    return added


def add_db_op_sub_no(data: dict) -> int:
    """各 backend_processes.processing[] の db_operation step に sub_no を採番。

    意味合い: 同一バック処理内の db_operation 出現順に 1, 2, 3, ... を採番。
              generate_docx.js が表示時に「②-1」「②-2」「②-3」と組み立てる。
              既に sub_no が付与されている場合は維持（冪等性）。
    返り値: 採番した step 数
    """
    numbered = 0
    for bp in data.get("backend_processes", []):
        processing = bp.get("processing", []) or []
        sub_no = 0
        for s in processing:
            if s.get("kind") != "db_operation":
                continue
            sub_no += 1
            db_op = s.get("db_op_detail") or {}
            if db_op.get("sub_no") != sub_no:
                db_op["sub_no"] = sub_no
                s["db_op_detail"] = db_op
                numbered += 1
    return numbered


def strip_url_param_judgment_steps(data: dict, screen_config: dict) -> int:
    """初期処理 FRX に残る URL パラメータ判定 step を削除（v42 設計逸脱の解消）。

    意味合い: v42 設計（呼ぶ側で判定、呼ばれる側は処理本体）に整合させるため、
              FRX 内に「URLパラメータを解析し...確認する」等の判定 step が残っていれば削除。
              判定は §5.2.1 [TR1] dispatch (trigger_dispatch.EV00) で完結している。
              screen_config.initial_processes の steps 定義側で対応すべきだが、移行用に念のため削除。
    削除条件: kind='data_load' かつ description が URL パラメータ判定パターンを含む
    """
    patterns = [
        re.compile(r"URLパラメータ.*解析.*未指定"),
        re.compile(r"URLパラメータ.*解析.*取得する"),
        re.compile(r"URLパラメータ.*解析.*確認する"),
        re.compile(r"mode\s*=\s*['\"]edit['\"].*取得"),
        re.compile(r"mode\s*=\s*['\"]view['\"].*取得"),
    ]
    # screen_config に追加 strip パターンがあれば適用（汎用化）
    init_strip = (screen_config.get("initial_processes", {}) or {}).get("strip_step_keywords", [])
    for kw in init_strip:
        patterns.append(re.compile(re.escape(kw)))

    removed = 0
    for fp in data.get("front_processes", []):
        pid = fp.get("process_id", "")
        # 初期処理のみ対象（process_id が FR1 / FR1-EDIT / FR1-VIEW / FR1-MISC または area='初期処理'）
        is_init = pid in ("FR1", "FR1-EDIT", "FR1-VIEW", "FR1-MISC", "F001", "F001-EDIT", "F001-VIEW", "F001-MISC") \
                  or fp.get("area") == "初期処理"
        if not is_init:
            continue
        steps = fp.get("steps", []) or []
        new_steps = []
        for s in steps:
            if s.get("kind") == "data_load":
                desc = s.get("description") or ""
                if any(p.search(desc) for p in patterns):
                    removed += 1
                    continue  # 削除
            new_steps.append(s)
        # step_no 再採番
        for i, s in enumerate(new_steps, start=1):
            s["step_no"] = i
        fp["steps"] = new_steps
    return removed


def fill_meta_source_html_path(data: dict, screen_config: dict) -> bool:
    """meta.source_html_path が空なら screen_config.source_html_path から補完。

    意味合い: 中間 JSON の meta セクションに source_html_path がなくて verify_intermediate.py で
              ERROR になる件の解消。screen_config.json には source_html_path が定義されているので
              そこからコピーする。Phase 3 の extract_source_files.py が本来補完すべきだが、
              v49 で初期化漏れに対応。冪等（既に設定済なら何もしない）。
    返り値: 補完したら True、すでに値があれば False
    """
    meta = data.setdefault("meta", {})
    if meta.get("source_html_path"):
        return False
    sc_path = screen_config.get("source_html_path", "")
    if sc_path:
        meta["source_html_path"] = sc_path
        return True
    return False


def main():
    if not INTERMEDIATE_JSON.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {INTERMEDIATE_JSON}", file=sys.stderr)
        sys.exit(1)

    # バックアップ
    backup_path = backup_v48(INTERMEDIATE_JSON)
    print(f"[INFO] v48 バックアップ: {backup_path}")

    with INTERMEDIATE_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # 2026-10-05 汎用化: モジュール先頭で解決済みの FEATURE_NAME を明示で渡す
    # (引数なしだと環境変数しか見ず、第1引数で渡された機能名を拾えないため)。
    screen_config = load_screen_config(FEATURE_NAME)

    # 画面項目逆引き辞書
    item_lookup = build_item_lookup(data)
    print(f"[INFO] 画面項目逆引き辞書: {len(item_lookup)} エントリ")

    # (1) response_to_screen_mapping を front_processes へ移動
    inserted = migrate_response_to_screen_mapping(data, item_lookup)
    print(f"[OK] (1) response_mapping step 挿入: {inserted} 件（front_processes へ移動）")

    # (2) response_spec.items[] に source / transform_backend 追加
    added = add_response_spec_source_fields(data)
    print(f"[OK] (2) response_spec.items[].source / transform_backend 追加: {added} 件")

    # (3) db_op_detail に sub_no 採番
    numbered = add_db_op_sub_no(data)
    print(f"[OK] (3) db_op_detail.sub_no 採番: {numbered} 件")

    # (4) 初期処理 FRX の URL パラメータ判定 step 削除
    removed = strip_url_param_judgment_steps(data, screen_config)
    print(f"[OK] (4) URL パラメータ判定 step 削除: {removed} 件（v42 設計逸脱の解消）")

    # (5) meta.source_html_path を screen_config から補完（v49 で追加）
    filled = fill_meta_source_html_path(data, screen_config)
    print(f"[OK] (5) meta.source_html_path 補完: {'適用' if filled else 'すでに設定済'}")

    with INTERMEDIATE_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[OK] 保存先: {INTERMEDIATE_JSON}")


if __name__ == "__main__":
    main()
