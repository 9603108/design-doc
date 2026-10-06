#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_id_scheme.py — 中間 JSON 全体の ID を id_scheme.json に従って新体系に一括変換する（v43 で新設、汎用）

【目的】
ID 体系を「2 文字接頭辞で統一 + 1 始まりの可変桁」にし、新しいカテゴリを追加しても同じ仕組みで採番できるようにする。スキル付属の id_scheme.json を読み、中間 JSON 全体の ID を新体系に一括変換 + クロスリファレンス
（called_by_triggers / dispatch.then_call / backend_call.backend_id / event_code 参照 等）も整合的に更新する。

【意味合い】
スキル本体（migrate / apply_logging_steps / generate_docx 等）からハードコード接頭辞（"F", "B", "L", "A"）
を全廃する単一拘束点。本スクリプトを通すことで、screen_config に "FR1" と書こうが "F001" と書こうが、
最終的な中間 JSON は id_scheme.json 準拠の新体系（FR1 / BE1 / LG1 / AR1 等）に統一される。

【接続情報】
- 入力: target/intermediate/{機能名}.json（機能名は第1引数か環境変数 FEATURE_NAME）
- 入力: .claude/skills/design-doc/id_scheme.json（スキル付属、全プロジェクト共通）
- 出力: 同 JSON を上書き（各エントリに新 ID + 旧 ID を legacy_id として保持、クロスリファレンスも更新）
- 仕様根拠: id_scheme.json _design_principles / SKILL.md「v43 ID 体系全面再設計」

【設計原則】
- 汎用: id_scheme.json の categories[] に登録された全カテゴリを自動処理。新カテゴリ追加時はスクリプト改修不要
- 冪等: 既に新体系 ID（先頭2文字が prefix）になっているエントリは再変換しない
- クロスリファレンス整合性: 旧 ID → 新 ID マップを構築し、すべての参照を一括更新
- legacy_id 保持: 旧 ID を legacy_id フィールドに残し、業務的トレーサビリティを維持
"""
import json
import re
import sys

from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    load_id_scheme,
    get_id_scheme_categories,
    format_id,
    get_id_prefix,
)

# 2026-10-05 汎用化: このファイル内でパスを組み立てる定数（スクリプト置き場・スキル直下）と pathlib の import を削った。
# 目的: 参照が0件になった定義を残さない。
# 意味合い: 中間 JSON の置き場をこのファイルで二重に持たず、_config_loader.INTERMEDIATE_DIR に一本化する。
# 接続情報: 置き場は下の INTERMEDIATE_JSON が _config_loader.INTERMEDIATE_DIR から組み立てる。
# 2026-10-05 汎用化: 既定の機能名（_config_loader が持っていた固定値）への依存をやめた。
# 目的: 特定画面に固定せず、指定された機能の中間 JSON を処理する。
# 意味合い: 機能名はここで1回だけ解決する（第1引数 > 環境変数 FEATURE_NAME）。
#           どちらも無ければ _config_loader 側が SystemExit("feature_name 未指定: ...") で止める。
# 接続情報: INTERMEDIATE_JSON は main() が読み書きする。置き場 target/intermediate/{機能名}.json は
#           _config_loader.INTERMEDIATE_DIR と同じ（他スクリプト・basic-design 側も同じ置き場を読む）。
FEATURE_NAME = get_feature_name_from_argv()
INTERMEDIATE_JSON = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"


def _is_new_format(value: str, prefix: str) -> bool:
    """既に新体系 ID か判定（冪等性確保）"""
    if not isinstance(value, str) or not value or not prefix:
        return False
    # サブラベル付き対応: <prefix>#-<sub>
    return value.startswith(prefix) and len(value) >= len(prefix) + 1


def build_id_mapping(data: dict) -> dict:
    """中間 JSON の各カテゴリの旧 ID → 新 ID マップを構築する。

    意味合い: id_scheme.json categories[] を駆動して、各セクションを自動走査。
              新カテゴリ追加時もこの関数は無改修で動く（categories[] にエントリ追加するだけ）。
    返り値: {category_key: {old_id: new_id}} のネスト辞書 + flat マップ
    """
    mapping = {}
    flat_map = {}  # 旧 ID -> 新 ID（クロスリファレンス更新用）

    # カテゴリごとの source_field を走査して ID マップを構築
    for cat in get_id_scheme_categories():
        key = cat.get("key", "")
        prefix = cat.get("prefix", "")
        source = cat.get("source_field", "")
        if not key or not prefix or not source:
            continue
        mapping[key] = {}
        # source_field の例: "screen_layout.areas[].area_no" / "front_processes[].process_id"
        new_id_map = _walk_and_assign_ids(data, source, key, prefix)
        mapping[key] = new_id_map
        # flat に集約
        for old, new in new_id_map.items():
            if old and new:
                flat_map[old] = new

    return mapping, flat_map


def _walk_and_assign_ids(data: dict, source_field: str, category_key: str, prefix: str) -> dict:
    """source_field のパスに従って配列を走査し、各エントリに新 ID を採番。

    意味合い: source_field 仕様（"a.b[].c" or "a[].b[].c" の多段ネスト）をパースして該当エントリ群を取得、
              各エントリの c フィールドを新体系 ID に変換 + legacy_id に旧値を保持。
    v50.5 phase 2.5.1: 多段ネスト配列対応（trigger_groups[].triggers[].trigger_no 等）。
              旧版は最初の `[]` で break して 1 段配列までしか対応していなかったため、trigger_no が
              生数値（1, 2, 3...）のまま接頭辞付与されない不具合があった。
    """
    # パースしてターゲットエントリ群を取得（多段ネスト対応）
    # 意味合い: source_field の各 segment を順に掘り下げて、最終的に id_field を持つ dict 群を集める。
    parts = source_field.split(".")
    id_field = parts[-1]
    path_parts = parts[:-1]

    # 開始点は data 自体。path_parts を順に処理して targets リストを成長させる。
    targets = [data]
    for p in path_parts:
        next_targets = []
        if p.endswith("[]"):
            key = p[:-2]
            if key:
                # "key[]" 形式: 現在の targets 各々から key 配列を取り出し、その要素を次の targets に
                for t in targets:
                    sub = t.get(key) if isinstance(t, dict) else None
                    if isinstance(sub, list):
                        next_targets.extend(sub)
            else:
                # "[]" 単体: 現在 targets がリストなら、その要素を次の targets に
                for t in targets:
                    if isinstance(t, list):
                        next_targets.extend(t)
        else:
            # 通常 key: 現在 targets 各々から key を取り出し
            for t in targets:
                sub = t.get(p) if isinstance(t, dict) else None
                if sub is not None:
                    next_targets.append(sub)
        targets = next_targets
        if not targets:
            return {}

    if not targets:
        return {}
    # targets は id_field を持つ dict のリスト
    target = targets  # 後段ロジック互換のため target 名を維持

    new_id_map = {}
    # 1パス目: 親 ID（サブラベルなし）の base_old を抽出して連番マップ構築
    # 意味合い: サブラベル付き ID（"L4-a" 等）は親 ID（"L4"）と同じ番号を共有させるため、
    #          先に親エントリの順序を確定する。空 ID エントリは 2 パス目で個別採番。
    base_to_seq = {}
    base_seq = 1
    for entry in target:
        if not isinstance(entry, dict):
            continue
        old_val = entry.get(id_field)
        old_str = str(old_val) if old_val is not None else ""
        if not old_str:
            continue
        if "-" in old_str:
            continue  # サブラベル付きはスキップ
        if _is_new_format(old_str, prefix):
            continue  # 既に新体系
        # v50.5 phase 2.5.1: 同じ旧 ID が複数エントリに重複している場合（trigger_no が group 内連番のため
        # group をまたぐと "1", "1", "2", "1" のように重複）、最初の出現位置だけ base_to_seq に登録。
        # 親 ID 探索はサブラベル付き ID 用なので、重複の最初を採用すれば足る。
        if old_str in base_to_seq:
            continue
        base_to_seq[old_str] = base_seq
        base_seq += 1

    # 2パス目: 各エントリに新 ID を付与（v50.5 phase 2.5.1 で「個別連番」モードに変更）
    # 意味合い: 旧版は「同じ旧 ID は同じ新 ID」だったため、trigger_no のような group 内連番では
    #          重複新 ID が発生してユニーク性が崩れた。新版は entry_seq で各エントリに個別連番採番。
    #          サブラベル付き ID（"L4-a" 等）は親 ID（"L4"）と同番号を共有するため base_to_seq から取得。
    # entry_seq の起点: 既存の新形式番号があれば、その最大値+1 から開始（衝突回避）。
    entry_seq = 1
    for entry in target:
        if not isinstance(entry, dict):
            continue
        old_val = entry.get(id_field)
        if old_val and isinstance(old_val, str) and _is_new_format(old_val, prefix):
            m = re.match(rf"^{re.escape(prefix)}(\d+)", old_val)
            if m:
                num = int(m.group(1))
                if num >= entry_seq:
                    entry_seq = num + 1

    for entry in target:
        if not isinstance(entry, dict):
            continue
        old_val = entry.get(id_field)
        old_str = str(old_val) if old_val is not None else ""
        # 既に新体系（冪等性）
        if old_str and _is_new_format(old_str, prefix):
            new_id_map[old_str] = old_str
            legacy_id = entry.get("legacy_id")
            if legacy_id and isinstance(legacy_id, str) and legacy_id != old_str:
                new_id_map[legacy_id] = old_str
            continue
        # サブラベル付き旧 ID（例: "FR1-EDIT" / "L4-a"）: 親 ID と同番号 + サブラベル
        if old_str and "-" in old_str:
            base_old, sub_label = old_str.split("-", 1)
            if base_old in base_to_seq:
                assigned_num = base_to_seq[base_old]
            else:
                assigned_num = entry_seq
                entry_seq += 1
            new_id = format_id(category_key, assigned_num, sub_label=sub_label)
            if "legacy_id" not in entry:
                entry["legacy_id"] = old_str
            entry[id_field] = new_id
            new_id_map[old_str] = new_id
            continue
        # サブラベルなし（空 ID or 旧連番）: entry_seq で個別連番採番
        # 重複旧 ID（"1" が複数エントリにある等）でも各エントリにユニーク新 ID が振られる
        new_id = format_id(category_key, entry_seq)
        entry_seq += 1
        if "legacy_id" not in entry:
            entry["legacy_id"] = old_str if old_str else None
        entry[id_field] = new_id
        if old_str:
            new_id_map[old_str] = new_id  # 重複旧 ID は last 採番で上書き（参照解決は last 採番優先）

    # v50.5: id フィールドが entry に既に存在し（typically None）、id_field が 'id' でない場合、
    #         process_id 等の新 ID を id にも同期する。
    # 意味合い: backend_processes / front_processes / common_logging_processes 等は id と process_id
    #          の二重キーを持つ（Phase 1 Agent が id=null で雛形を作り、process_id に新 ID を採番する設計）。
    #          generate_docx.js / 後段スクリプトが bp.id 経由で参照しても None にならないよう同期する。
    # 接続情報: 再発防止対象 = backend_processes[i].id = None のまま docx が壊れて出力される問題
    if id_field != "id":
        for entry in target:
            if isinstance(entry, dict) and "id" in entry and entry.get("id") is None:
                synced = entry.get(id_field)
                if synced:
                    entry["id"] = synced
    return new_id_map


def update_cross_references(data: dict, flat_map: dict):
    """中間 JSON 内のクロスリファレンスを旧 ID → 新 ID で一括更新。

    意味合い: front_processes.called_by_triggers / steps[].backend_call.backend_id /
              trigger_groups.dispatch[].then_call / dispatch.calls / common_logging_processes.parent_call_id 等を
              flat_map で置換。各カテゴリの参照フィールドを自動走査。
    """
    # 一般的な ID 参照フィールド名（業務的に「参照」を意味するもの）
    # v49 拡張: called_by_front / db_op_ref / db_operations_ref / event_ref / api_ref /
    #          validation_refs / calculation_refs / common_logic_refs / message_ref も追加。
    # v50 Phase 2: 命名揺れ統一。新名 {category}_ref / {category}_refs を主とし、旧名は
    #              legacy 中間 JSON 互換のため残置（旧名のデータでも
    #              ID 解決が継続できるようにする）。新規生成は新名のみ。
    REF_FIELDS = {
        # v50 Phase 2 新名（主、{category}_ref / {category}_refs 統一）
        "backend_ref",           # front_processes[].steps[].backend_call.backend_ref: BE# (旧 backend_id)
        "front_refs",            # trigger_groups[].triggers[].front_refs[]: FR# (旧 calls)
        "logging_ref",           # *_processes[].steps/processing[].logging_ref: LG# (旧 call_id, kind='logging_call' 内)
        "item_ref",              # response_mapping.mappings[].item_ref: IT# (旧 item_ref_no)
        "db_op_refs",            # process_flows[].steps[].db_op_refs[]: DB# (旧 db_op_ref)
        # v50 Phase 2 旧名（legacy 互換維持、旧名と新名の両方を受ける）
        "backend_id",            # legacy → backend_ref
        "calls",                 # legacy → front_refs
        "call_id",               # legacy → logging_ref
        "item_ref_no",           # legacy → item_ref
        "db_op_ref",             # legacy → db_op_refs
        # その他（rename 対象外、命名が既に統一済 or 単一キー）
        "process_id", "then_call",
        "parent_call_id", "area_ref",
        "subdivision_id",
        "called_by_front",       # backend_processes[].called_by_front: [FR1, ...]
        "db_operations_ref",     # event_processes[].db_operations_ref: [DB1, ...]
        "event_ref",             # events[].no への参照 (EV1, EV2, ...)
        "api_ref",               # process_flows[].steps[].api_ref / event_processes[].api_refs[]
        "api_refs",              # event_processes[].api_refs[]: API#
        "validation_ref",        # process_flows[].steps[].validation_ref
        "validation_refs",       # front_processes[].validation_refs[]: VL#
        "calculation_refs",      # front_processes[].calculation_refs[]: CA#
        "common_logic_refs",     # front_processes[].common_logic_refs[]: CL#
        "message_ref",           # process_flows[].steps[].message_ref: MS#
        "trigger_events",        # process_flows[].trigger_events[]: EV#
        # 注: v50 で event_code は廃止（events[].legacy_event_code に保存）、参照は全て event_ref に統一済。
        #     REF_FIELDS から event_code を削除（migrate_v49_event_unify.py で全 rename 済）
    }

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in REF_FIELDS:
                    if isinstance(v, str) and v in flat_map:
                        node[k] = flat_map[v]
                    elif isinstance(v, list):
                        node[k] = [flat_map.get(x, x) if isinstance(x, str) else x for x in v]
                elif k == "db_op_detail" and isinstance(v, dict):
                    # v45 fix: db_op_detail.id は ID 参照だが REF_FIELDS の "id" だけだと他の id とも衝突
                    # 親キーが db_op_detail のものに限定して id を flat_map で変換
                    if isinstance(v.get("id"), str) and v["id"] in flat_map:
                        v["id"] = flat_map[v["id"]]
                    walk(v)  # サブフィールドも再帰
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)

    # 特殊: area_pattern_subdivisions の dict キー（A5 等）も flat_map で更新
    # 意味合い: area_no をキーとする辞書は walk で検出できないため個別対応
    sl = data.get("screen_layout", {})
    aps = sl.get("area_pattern_subdivisions")
    if isinstance(aps, dict):
        new_aps = {}
        for k, v in aps.items():
            new_k = flat_map.get(k, k)
            new_aps[new_k] = v
        sl["area_pattern_subdivisions"] = new_aps


def main():
    if not INTERMEDIATE_JSON.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {INTERMEDIATE_JSON}", file=sys.stderr)
        sys.exit(1)

    scheme = load_id_scheme()
    if not scheme or not scheme.get("categories"):
        print("[INFO] id_scheme.json が空 or 未配置、スキップ（後方互換維持）")
        return

    with INTERMEDIATE_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"[INFO] id_scheme.json categories: {len(scheme['categories'])} カテゴリ")

    # 1. 各カテゴリの旧 ID → 新 ID マップを構築 + 中間 JSON の ID フィールドを更新
    mapping, flat_map = build_id_mapping(data)
    print(f"[INFO] ID 変換マップ構築: {len(flat_map)} エントリ")
    for cat_key, m in mapping.items():
        if m:
            sample = list(m.items())[:2]
            print(f"  {cat_key}: {len(m)} 件 (例: {sample})")

    # 2. クロスリファレンス（参照フィールド）を一括更新
    update_cross_references(data, flat_map)
    print(f"[OK] クロスリファレンス更新完了")

    with INTERMEDIATE_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[OK] 保存先: {INTERMEDIATE_JSON}")


if __name__ == "__main__":
    main()
