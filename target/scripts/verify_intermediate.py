#!/usr/bin/env python3
# 目的: 中間JSON（target/intermediate/{機能名}.json）に対し 15_中間JSONスキーマ.md「整合性ルール」と 14_チェックリスト.md の
#       業務語彙・日時フォーマットチェックを機械検証する。
# 意味合い: design-doc スキルの Wave 4。問題を全件レポートし、致命度（ERROR / WARN）を分けて出力する。
#           ERROR は docx 生成前に修正必須、WARN は許容範囲（業務担当者の確認は推奨）。
# 接続情報: 入力 = target/intermediate/{機能名}.json / 出力 = stdout の検証レポート
#           設定 = target/intermediate/{機能名}_project_config.json（無くても動く）。main() が読み、
#             forbidden_terms_map.terms のキー → check_forbidden_words の禁止語、
#             allowed_project_patterns / allowed_profiles → check_structure の許容値 として渡す。
#           参照仕様 = .claude/skills/design-doc/15_中間JSONスキーマ.md §整合性ルール / 14_チェックリスト.md
#
# フォーマット憲法 v55.2 準拠
# v55.2: §10 §19 強制: 全セクション業務語彙化辞書適用 + 概要 25 字以内 + 物理名残存 0 (ERROR 50→0)
# 適用憲法: 16_docx出力ハンドオフ.md §20 バージョン管理規約
#   - 中間 JSON `_meta.format_version` フィールドの値が EXPECTED_FORMAT_VERSION と一致するか
#     `check_format_version_consistency()` で WARN レベル検証する
#   - 不一致時は WARN として出力し、開発者に憲法バージョン更新の見落としを通知する

import json
import re
import sys
import tempfile
from pathlib import Path

# 2026-10-05 汎用化: 機能名・中間 JSON の置き場・案件ごとの設定は _config_loader から取る
# （このファイルに案件固有の値や絶対パスを持たないため）。使うのは main()。
from verify_partials import DETAIL_TODO
from _initial_eval import build_env, chain_value, disabled_value, linked_fixed_elements, literal_text_elements, visible_initially
from _jsx_chain import chain_for_lines, resolve_for_lines
from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    get_forbidden_terms_map,
    load_project_config,
    load_screen_config,
)

# v55.2: 期待する憲法バージョン
# 16_docx出力ハンドオフ.md §20 「バージョン管理規約」適用範囲 #4 に基づく定数表記
EXPECTED_FORMAT_VERSION = "v55.2"

# 2026-10-05 汎用化: 業務担当者に出してはならない旧物理名（禁止語）の直書き定数は廃止した。
# 意味合い: 禁止語は案件ごとに違うため、project_config の forbidden_terms_map.terms のキーから読む。
# 接続情報: main() が get_forbidden_terms_map(feature) で読み、check_forbidden_words() へ渡す。

# 不正な日付時刻フォーマット（共通の規則）
DATE_FORBIDDEN_PATTERNS = [
    (r"\b\d{4}-\d{2}-\d{2}\b",        "YYYY-MM-DD 形式（ISO日付）"),
    (r"\b\d{1,2}/\d{1,2}/\d{4}\b",    "MM/DD/YYYY または DD/MM/YYYY 形式"),
    (r"\b\d{1,2}:\d{2}\s*[AP]M\b",    "AM/PM 12時間表記"),
]


def check_format_version_consistency(merged: dict) -> list:
    """v55.0 §20 バージョン管理規約: 中間 JSON `_meta.format_version` と EXPECTED_FORMAT_VERSION の一致確認。

    目的: 中間 JSON に記録された憲法バージョンと、本 verify スクリプトが想定する憲法バージョン (EXPECTED_FORMAT_VERSION) が
          一致しているかを検証する。不一致は「中間 JSON が古い憲法のまま」「憲法だけ bump されてスクリプト追従漏れ」のいずれか。
    意味合い: 16_docx出力ハンドオフ.md §20「バージョン管理規約」適用範囲 #3 (中間 JSON `_meta.format_version`) と
              #4 (verify_intermediate.py 先頭バージョン表記) の整合を機械検証する。
    レベル: WARN (致命ではないが憲法バージョン更新漏れを開発者に通知)。
    接続情報: 呼出元 = main() / 参照 = _meta.format_version (Python dict キー) / 期待値 = EXPECTED_FORMAT_VERSION 定数
    """
    issues = []
    meta_obj = merged.get("_meta") or merged.get("meta") or {}
    # _meta が dict でない (旧フォーマット等) 場合はスキップせず WARN 化
    if not isinstance(meta_obj, dict):
        issues.append(("WARN", f"v55.0 §20 _meta フィールドが dict 型でない (type={type(meta_obj).__name__}) - format_version 検証不能"))
        return issues
    actual = meta_obj.get("format_version")
    # 未設定（None）は WARN にしない。値があって期待値と違うときだけ WARN。
    # 意味合い: このスキルのパイプラインには format_version を書くスクリプトが無く、利用者は設定できない。
    #           出せない値の未設定を毎回警告しても対処のしようがないため、値がある場合の不一致だけを知らせる。
    # 接続情報: 呼出元 = main() / 期待値 = EXPECTED_FORMAT_VERSION（変えていない）
    if actual is not None and actual != EXPECTED_FORMAT_VERSION:
        issues.append(("WARN", f"v55.0 §20 中間 JSON format_version={actual} が verify 期待値 {EXPECTED_FORMAT_VERSION} と不一致 - 憲法 bump 追従漏れの可能性"))
    return issues


def check_event_code_consistency(merged: dict) -> list:
    """整合性ルール 1, 2: イベント参照の整合（v50: event_code → event_ref 移行に対応）。

    意味合い: v50 で event_code フィールドが廃止され、events[].no が ID として使われる。
              items[].event_ref / event_processes[].event_ref / trigger_groups[].triggers[].event_ref 等が
              events[].no を参照しているか検証。後方互換として event_code 参照も検出。
    """
    issues = []
    # v50: events[].no が ID（EV1, EV2, ...）
    events_nos = {e.get("no") for e in merged.get("events", []) if e.get("no")}
    # event_processes の参照（v50 では event_ref、v49 までは event_code）
    process_refs = set()
    for p in merged.get("event_processes", []):
        ref = p.get("event_ref") or p.get("event_code")
        if ref:
            process_refs.add(ref)
    # 初期表示イベント（EV00 / EV1 等）の参照は initialization 担当として許容
    # v50: events[].legacy_event_code === 'EV00' のエントリの no も process_refs に追加
    for e in merged.get("events", []):
        if e.get("legacy_event_code") == "EV00":
            process_refs.add(e.get("no"))
    process_refs.add("EV1")  # 後方互換: EV1 (新体系の初期表示)
    process_refs.add("EV00")  # 後方互換: legacy

    # screen_layout.items[].event_ref ⊆ events[].no
    # v50 で event_code → event_ref に rename、event_code も残置していたら旧形式残存として ERROR
    for item in merged.get("screen_layout", {}).get("items", []):
        # v50: event_ref を優先、event_code が残っていたら旧形式残存として ERROR
        if item.get("event_code"):
            issues.append(("ERROR", f"screen_layout.items[no={item.get('no')}].event_code が残存（v50 で event_ref に rename 必須）: {item.get('event_code')}"))
        ref = item.get("event_ref")
        if ref and ref not in events_nos:
            issues.append(("ERROR", f"screen_layout.items[no={item.get('no')}].event_ref={ref} が events[].no に存在しない"))

    # events[] の全 no に対して event_processes[] または initialization のいずれかが対応
    for no in events_nos:
        if no and no not in process_refs:
            issues.append(("WARN", f"events[].no={no} に対応する event_processes が存在しない（仮埋めが未生成）"))

    return issues


def check_legacy_event_code_residue(merged: dict, verify_overrides: dict) -> list:
    """v50 新設: 中間 JSON 内に legacy event_code 形式が残存していないか検証。

    意味合い: v50 で event_code は events[].legacy_event_code に保存されるべきで、
              他のフィールドに旧形式が残っていたら移行漏れ。
              legacy_event_code フィールド自体は OK（明示的に旧コードを保持する設計）。
    判定方法: events[].no のセットに含まれない、かつ legacy event_code パターンに合致する値を警告。
              （EV17 は新体系 events[].no に存在すれば OK、存在しなければ旧形式残存と判定）

    2026-10-05 汎用化(3): 「明白な旧形式」の正規表現は引数 verify_overrides の
              legacy_event_code_patterns（正規表現の文字列の配列。無ければ空）から読む。
    意味合い: 旧イベントコードの書式は案件ごとに違うため、このファイルに直書きしない。
              配列が空なら、明白な旧形式の判定は何も検出しない（数値形式 EV\\d+ の判定は常に行う）。
    接続情報: 呼出元 = main()（project_config の verify_overrides を渡す）。
    """
    import re as _re
    issues = []
    # 案件で定義した、明確に旧形式とわかるパターン（project_config の verify_overrides.legacy_event_code_patterns）
    OBVIOUS_LEGACY = [_re.compile(p) for p in verify_overrides.get("legacy_event_code_patterns", [])]
    # 2 桁ゼロ埋め形式（EV00, EV01, ...）は events[].no と区別がつきにくいので、events[].no と照合
    NUMERIC_FORM = _re.compile(r"^EV\d+$")
    events_nos = {e.get("no") for e in merged.get("events", []) if e.get("no")}

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                # legacy_event_code フィールド自体は OK
                if k == "legacy_event_code":
                    continue
                if isinstance(v, str):
                    if any(pat.match(v) for pat in OBVIOUS_LEGACY):
                        # 案件で定義した旧形式に一致 = 明白な旧形式
                        issues.append(("WARN", f"legacy event_code 残存: {path}.{k}={v}（v50 で events[].no への参照に移行すべき）"))
                    elif NUMERIC_FORM.match(v) and v not in events_nos and len(v) >= 4:
                        # EV### 形式だが events[].no に存在しない → 旧 EV00 系の可能性
                        # ただし events[].no に存在する場合は新体系 OK（誤検出抑止）
                        # len(v) >= 4 (EVnn, EVnnn) で 1-2 桁の新体系 (EV1, EV9) を除外
                        # この条件はやや緩い。実際は events[].no と照合できれば十分
                        issues.append(("WARN", f"legacy event_code 残存の可能性: {path}.{k}={v}（events[].no に存在しない）"))
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(merged)
    return issues


def check_db_op_refs(merged: dict) -> list:
    """整合性ルール 3: event_processes[].db_operations_ref ⊆ db_operations[].id"""
    issues = []
    db_op_ids = {op.get("id") for op in merged.get("db_operations", [])}
    for p in merged.get("event_processes", []):
        for ref in p.get("db_operations_ref", []) or []:
            if ref and ref not in db_op_ids:
                issues.append(("ERROR", f"event_processes[event_code={p.get('event_code')}].db_operations_ref={ref} が db_operations[] に存在しない"))
    return issues


def check_logical_names(merged: dict) -> list:
    """整合性ルール 5: 全ての logical_column / logical_table が logical_names[] に存在"""
    issues = []
    known_logical_columns = {ln.get("logical_column") for ln in merged.get("logical_names", [])}
    known_logical_tables = {ln.get("logical_table") for ln in merged.get("logical_names", [])}

    for op in merged.get("db_operations", []):
        for ds_item in op.get("dataset", []) or []:
            lc = ds_item.get("logical_column")
            lt = ds_item.get("logical_table")
            if lc and lc not in known_logical_columns:
                # 2026-10-12 WARN から格上げ: H3 で logical_names が 67→7 件に減っても通っていた(8章の論理名対応表が欠ける)
                issues.append(("ERROR", f"db_operations[{op.get('id')}].dataset の logical_column={lc} が logical_names[] に未登録（logical_names に物理名と対で書く）"))
            if lt and lt not in known_logical_tables:
                issues.append(("WARN", f"db_operations[{op.get('id')}].dataset の logical_table={lt} が logical_names[] に未登録"))
    return issues


def check_message_refs(merged: dict) -> list:
    """整合性ルール 6: validations[].message_id ⊆ messages[].message_id

    意味合い: messages の識別子は message_id が正（generate_docx.js が読むキー）。旧い形の id も受ける。
              validations 側の参照キー（message_id）は変えていない。
    接続情報: 呼出元 = main() / 入力 = merged['messages'], merged['validations']
    """
    issues = []
    msg_ids = {m.get("message_id") or m.get("id") for m in merged.get("messages", [])}
    for v in merged.get("validations", []):
        mid = v.get("message_id")
        if mid and mid not in msg_ids:
            issues.append(("WARN", f"validations[no={v.get('no')}].message_id={mid} が messages[] に未登録"))
    return issues


def check_forbidden_words(merged: dict, forbidden_words: set) -> list:
    """業務語彙チェック: 旧システムの物理名が含まれていないこと（共通の規則）

    2026-10-05 汎用化(2): 関数名を check_forbidden_words に改め、文言を「旧システムの物理名」にした。
    意味合い: 禁止語は project_config の forbidden_terms_map で与える形で、特定の言語・旧システムに限らないため。

    2026-10-05 汎用化: 禁止語は引数 forbidden_words で受ける（小文字化済みの集合。空なら何も検出しない）。
    接続情報: 呼出元 = main()。値の出どころは project_config の forbidden_terms_map.terms のキー。
    """
    issues = []

    def scan(label: str, text):
        if not text:
            return
        s = str(text).lower()
        for word in forbidden_words:
            if word in s:
                issues.append(("ERROR", f"{label} に 旧システムの物理名 '{word}' が含まれている: '{text[:80]}'"))

    # meta.feature_name
    scan("meta.feature_name", merged.get("meta", {}).get("feature_name"))

    # events[].trigger, content
    for e in merged.get("events", []):
        scan(f"events[no={e.get('no')}].trigger", e.get("trigger"))
        scan(f"events[no={e.get('no')}].content", e.get("content"))

    # event_processes[].overview
    for p in merged.get("event_processes", []):
        scan(f"event_processes[{p.get('event_code')}].overview", p.get("overview"))

    # v50.5 phase 2.5.1 新規: logical_names[].physical_table / logical_table も旧システムの物理名の検出対象に追加
    # 意味合い: 「logical_names[] に旧システム由来のテーブル名が
    #          紛れ込み、§8 論理名対応表 docx に旧システムの名前がそのまま出力されてしまった」事故の再発防止。
    #          旧 scan() 対象は events / event_processes / meta だけで、logical_names は盲点だった。
    for ln in merged.get("logical_names", []):
        scan(f"logical_names[no={ln.get('no')}].physical_table", ln.get("physical_table"))
        scan(f"logical_names[no={ln.get('no')}].logical_table", ln.get("logical_table"))
        scan(f"logical_names[no={ln.get('no')}].physical_column", ln.get("physical_column"))
        scan(f"logical_names[no={ln.get('no')}].logical_column", ln.get("logical_column"))

    return issues


def check_date_format(merged: dict) -> list:
    """日付時刻フォーマット: 文字列内の日時表現が YYYY/MM/DD HH:MM 形式（meta.generated_at は ISO 8601 として除外）"""
    issues = []

    def scan(label: str, text):
        if not text:
            return
        s = str(text)
        for pattern, desc in DATE_FORBIDDEN_PATTERNS:
            if re.search(pattern, s):
                issues.append(("WARN", f"{label} に不正な日付フォーマット ({desc}) が含まれている: '{s[:80]}'"))

    for e in merged.get("events", []):
        scan(f"events[no={e.get('no')}].trigger", e.get("trigger"))
        scan(f"events[no={e.get('no')}].content", e.get("content"))
    for p in merged.get("event_processes", []):
        scan(f"event_processes[{p.get('event_code')}].overview", p.get("overview"))
    for m in merged.get("messages", []):
        # ラベルの識別子は message_id 優先・旧い形の id も受ける（check_message_refs と同じ作法）。
        # 走査する本文のキー（content）は変えていない。
        scan(f"messages[{m.get('message_id') or m.get('id')}].content", m.get("content"))
    return issues


def check_process_flows(merged: dict) -> list:
    """v16 で新設: process_flows[] の参照整合性と phase_no 連番をチェック。

    対応する整合性ルール（15_中間JSONスキーマ.md §整合性ルール）:
    - ルール9: process_flows[].trigger_events[] ⊆ events[].event_code
              process_flows[].steps[].event_ref ⊆ events[].event_code
              process_flows[].steps[].db_op_ref[] ⊆ db_operations[].id
    - ルール10: phase_no が 6.1, 6.2, 6.3, ... の連番で重複なし

    意味合い: 業務フェーズ縦糸 (process_flows) が、横糸 (events / event_processes / db_operations) と
              矛盾なく参照されることを保証する。参照先が存在しないリンクは設計書として致命的。
    """
    issues = []
    flows = merged.get("process_flows", [])
    if not flows:
        return issues  # process_flows 未設定はOK（旧バージョン互換のフォールバック動作）

    # v50: events[].event_code 廃止 → events[].no が ID。trigger_events / event_ref は events[].no を参照。
    events_codes = {e.get("no") for e in merged.get("events", []) if e.get("no")}
    db_op_ids = {op.get("id") for op in merged.get("db_operations", []) if op.get("id")}

    # phase_no 連番チェック（"6.1", "6.2", ...の順）
    expected_nos = [f"6.{i+1}" for i in range(len(flows))]
    actual_nos = [f.get("phase_no") for f in flows]
    if actual_nos != expected_nos:
        issues.append(("ERROR", f"process_flows[].phase_no が想定の連番（{expected_nos}）と一致しない: 実際={actual_nos}"))

    # 各フェーズの参照整合
    for i, flow in enumerate(flows):
        phase_label = f"process_flows[{i}]({flow.get('phase_no')} {flow.get('phase_name')})"

        # trigger_events 整合
        for ec in flow.get("trigger_events", []) or []:
            if ec and ec not in events_codes:
                issues.append(("ERROR", f"{phase_label}.trigger_events に未登録イベント '{ec}'"))

        # steps[] 整合
        for j, step in enumerate(flow.get("steps", []) or []):
            step_label = f"{phase_label}.steps[{j}](step_no={step.get('step_no')})"

            # event_ref 整合（あれば）
            evref = step.get("event_ref")
            if evref and evref not in events_codes:
                issues.append(("ERROR", f"{step_label}.event_ref='{evref}' が events[] に存在しない"))

            # db_op_refs 整合（配列）
            # v50 Phase 2: db_op_refs（新名、複数参照配列）優先、旧 db_op_ref フォールバック
            db_op_list = step.get("db_op_refs") if step.get("db_op_refs") is not None else step.get("db_op_ref", [])
            for op_ref in db_op_list or []:
                if op_ref and op_ref not in db_op_ids:
                    issues.append(("ERROR", f"{step_label}.db_op_refs='{op_ref}' が db_operations[] に存在しない"))

            # 必須フィールド step_no / kind / description
            for k in ("step_no", "kind", "description"):
                if not step.get(k):
                    issues.append(("WARN", f"{step_label} の必須フィールド {k} が空"))

    return issues


def check_response_to_screen_mapping(merged: dict) -> list:
    """v16 で新設: api_spec[].response_to_screen_mapping の画面参照整合性をチェック。

    対応する整合性ルール（15_中間JSONスキーマ.md §整合性ルール ルール11）:
    - response_to_screen_mapping[].screen_no ⊆ screen_layout.items[].screen_no
    - screen_item_name が当該画面の items[].item_name に存在

    例外: screen_no=null は内部処理用途（DOM項目ではない、例: トーストメッセージ）として許容
    """
    issues = []
    sl_items = (merged.get("screen_layout") or {}).get("items", []) or []

    # screen_no → 当該画面のitem_name set
    screen_items_map = {}
    for it in sl_items:
        sn = it.get("screen_no")
        if not sn:
            continue
        screen_items_map.setdefault(sn, set()).add(it.get("item_name"))

    for i, api in enumerate(merged.get("api_spec", []) or []):
        mapping = api.get("response_to_screen_mapping", []) or []
        # 2026-10-12: 返す項目があるのに画面への写しが無いと、migrate_v48_to_v49 が response_mapping の step を作れず
        #             5.1 の「画面表示変換」と「レスポンス→画面項目マッピング」表が丸ごと消える
        if api.get("response_items") and not mapping and not api.get("response_to_screen_mapping_none_reason"):
            # 欄の道筋を api_spec[i].response_to_screen_mapping の形で先頭に置く(run_units.owner_of が書いた単位 S2 を引けるように。action は括弧で後ろ)
            issues.append(("ERROR", f"api_spec[{i}].response_to_screen_mapping({api.get('action', '')}): response_items があるのに response_to_screen_mapping が空"
                                    "（{api_field_path, screen_area_no, screen_item_name, transform} を書く。画面に出さない API なら response_to_screen_mapping_none_reason に理由を書く）"))
        # 2026-10-12: パスに「api.ts の apiConfig.salesPath」のようなソース上の在り処を書くと、エンドポイント欄が URL にならない
        path = str(api.get("path") or "")
        if path and not path.startswith("/") and "://" not in path:
            issues.append(("ERROR", f"api_spec[{i}]({api.get('action', '')}): path「{path}」が URL のパスでない（/api/sales のように実際のパスを書く）"))
        for k, prm in enumerate(api.get("request_params") or []):
            if not str(prm.get("location") or "").strip():
                issues.append(("ERROR", f"api_spec[{i}]({api.get('action', '')}).request_params[{k}] {prm.get('name')}: location が空（body / query / path / header を書く）"))
        for j, m in enumerate(mapping):
            label = f"api_spec[{i}]({api.get('description','')[:30]}).response_to_screen_mapping[{j}]"
            sn = m.get("screen_no")
            sin = m.get("screen_item_name")

            if sn is None and sin is None:
                # 内部処理用途として許容（トースト・WebSocket送信等）
                continue

            if sn and sn not in screen_items_map:
                issues.append(("WARN", f"{label}.screen_no='{sn}' が screen_layout.items[] に存在しない"))
                continue

            if sn and sin and sin not in screen_items_map.get(sn, set()):
                # 「グリッド全体」のような擬似項目は許容（特定の単一画面項目に対応しない）
                # 厳格チェックを希望する場合は WARN を ERROR に
                issues.append(("WARN", f"{label}.screen_item_name='{sin}' が画面{sn}の items[].item_name に存在しない（疑似項目は許容）"))

    return issues


def check_screen_terminology(merged: dict) -> list:
    """v20 整合性ルール 18-20: 画面用語の cross-reference 整合性チェック。

    ルール18: events[].location / trigger_groups[].triggers[].location の各要素は
              screen_layout.areas[].area_name ∪ items[].item_name ∪ 「{feature_name}画面」と厳密一致
              （ただし trigger_groups[kind=auto/external] の location は権威語彙不問、WARN のみ）
    ルール19: screen_layout.items[].area_ref ⊆ screen_layout.areas[].area_no
    ルール20: backend_processes[].response_to_screen_mapping[].screen_item_name の厳密一致
    """
    issues = []
    sl = merged.get("screen_layout", {}) or {}
    areas = sl.get("areas", []) or []
    items = sl.get("items", []) or []
    feature_name = merged.get("meta", {}).get("feature_name", "")
    screen_name_default = f"{feature_name}画面" if feature_name and "画面" not in feature_name else (feature_name or "画面")

    # 権威集合
    area_names = {a.get("area_name") for a in areas if a.get("area_name")}
    area_nos = {a.get("area_no") for a in areas if a.get("area_no")}
    item_names = {it.get("item_name") for it in items if it.get("item_name")}
    # external 用の特殊カテゴリラベル
    external_labels = {"WebSocket受信", "DevToolsコンソール", "外部呼出"}
    authority = area_names | item_names | {screen_name_default} | external_labels

    # ルール19: items.area_ref ⊆ areas.area_no
    for it in items:
        aref = it.get("area_ref")
        if aref and aref not in area_nos:
            issues.append(("ERROR", f"screen_layout.items[no={it.get('no')}].area_ref='{aref}' が areas[].area_no に存在しない"))

    # ルール18: events[].location 厳密一致
    for e in merged.get("events", []) or []:
        source_kind = e.get("source_kind", "screen")
        # internal/external 系は area_name + 特殊ラベル(WebSocket受信/DevToolsコンソール)が許容、それ以降は自由
        is_strict = source_kind == "screen"
        for i, elem in enumerate(e.get("location", []) or []):
            if elem in authority:
                continue
            if not is_strict and i >= 1:
                # external/internal の2階層目以降は自由（特殊トリガーの function 名等）
                continue
            issues.append(("WARN" if not is_strict else "ERROR",
                          f"events[{e.get('event_code') or e.get('legacy_event_code') or e.get('no') or '?'}].location[{i}]='{elem}' が screen_layout の権威語彙にない"
                          "（H1 の trigger の項目名を S1 の item_name の字面のまま書く。種別の語を付けない）"))

    # ルール18: trigger_groups[].triggers[].location 厳密一致
    for g in merged.get("trigger_groups", []) or []:
        kind = g.get("kind")
        is_strict = kind == "screen_operation"
        for t in g.get("triggers", []) or []:
            for i, elem in enumerate(t.get("location", []) or []):
                if elem in authority:
                    continue
                if not is_strict and i >= 1:
                    continue
                issues.append(("WARN" if not is_strict else "ERROR",
                              f"trigger_groups[kind={kind}].triggers[no={t.get('trigger_no')}].location[{i}]='{elem}' が screen_layout の権威語彙にない"
                              + ("（画面操作の trigger は events の trigger から作る。H1 の trigger の項目名を直す）" if is_strict else "")))

    # ルール20: response_to_screen_mapping.screen_item_name 厳密一致
    for bp in merged.get("backend_processes", []) or []:
        respspec = bp.get("response_spec", {}) or {}
        for m in respspec.get("response_to_screen_mapping", []) or []:
            sin = m.get("screen_item_name")
            sn = m.get("screen_no")
            if sin is None and sn is None:
                continue  # 内部処理用途として許容
            if sin and sin not in item_names:
                # 「グリッド全体」等の疑似項目は WARN（既存ルール踏襲）
                issues.append(("WARN", f"backend_processes[{bp.get('process_id')}].response_to_screen_mapping.screen_item_name='{sin}' が screen_layout.items[] に存在しない（疑似項目は許容）"))

    return issues


def check_item_name_duplication(merged: dict, drafts: list = None) -> list:
    """A8 新設（2026-07-18）: 同一エリア内での item_name 重複を検出する。
    意味合い: 業務担当者が §4.3 画面項目一覧を読む際、同一エリア内に同名項目が複数あると
              どちらの項目を指しているか区別できず可読性が落ちる（14_チェックリスト.md「孤立処理・記述精度チェック」対応）。
    W2(2026-10-14 run12): drafts（react 下書き）の display_rows が同名の行を描く数以下なら WARN にしない（小計・合計など、描く行の反復）。
    接続情報: 入力 = merged['screen_layout']['items'] / drafts（_load_react_drafts() の結果。無ければ従来どおり全件 WARN）/ 呼出元 = main()
    """
    issues = []
    items = (merged.get("screen_layout", {}) or {}).get("items", []) or []
    # WHY(W2): 下書きの display_rows[].specs[].label を全行で数える。同名の項目が画面項目で複数あっても、
    #      描く行が同じ名前を同数出すなら、画面の反復として正しく、重複の WARN は誤検出になるため
    draft_counts = {}
    for d in drafts or []:
        for row in (d.get("display_rows") or []):
            for sp in (row.get("specs") or []):
                lb = str(sp.get("label") or "").strip() if isinstance(sp, dict) else ""
                if lb:
                    draft_counts[lb] = draft_counts.get(lb, 0) + 1
    by_area = {}
    for it in items:
        name = it.get("item_name")
        if not name:
            continue
        area = it.get("area_ref") or "(未割当)"
        by_area.setdefault(area, {}).setdefault(name, []).append(it.get("no"))
    for area, name_map in by_area.items():
        for name, nos in name_map.items():
            # WHY(W2): 画面項目の同名の数が、下書きの同名の行の数を上回るときだけ WARN にする（下書きが無いときは draft_counts が空で従来どおり）
            if len(nos) > 1 and len(nos) > draft_counts.get(str(name).strip(), 0):
                issues.append(("WARN", f"screen_layout.items[] area_ref={area} 内で item_name='{name}' が重複（no={nos}）"))
    return issues


def check_overview_duplication(merged: dict) -> list:
    """A8 新設（2026-07-18）: front_processes / backend_processes 間での overview 文言使い回しを検出する。
    意味合い: 同じ説明文が複数処理に付いていると、業務担当者が処理一覧を流し読みした際に
              各処理の違いを区別できない（14_チェックリスト.md「孤立処理・記述精度チェック」対応）。
    """
    issues = []
    seen = {}
    for section_key in ("front_processes", "backend_processes"):
        for proc in merged.get(section_key, []) or []:
            overview = (proc.get("overview") or "").strip()
            if not overview:
                continue
            pid = proc.get("process_id", "?")
            if overview in seen:
                issues.append(("WARN", f"{section_key}[{pid}].overview が {seen[overview]} と同一文言（使い回し）: 「{overview[:40]}」"))
            else:
                seen[overview] = f"{section_key}[{pid}]"
    return issues


def check_screen_item_remarks_dynamic_behavior(merged: dict, verify_overrides: dict) -> list:
    """v49 で追加、v51 で WARN → ERROR に格上げ。screen_layout.items[].remarks に動的振る舞いが混入していないか検証。

    目的: §3.3 画面項目一覧の備考列 (remarks) に動的振る舞い記述 / 撤廃用語が混入していないかを ERROR で検出する。
    意味合い:
      - v51 §18「画面項目一覧（§3.3）備考欄の責務分離」 + §15 責務分離原則の機械強制点。
      - 備考列には項目の「静的属性」のみを書くべきで、「フォーカス外しで〜する」「クリックで〜する」
        「サジェスト機能あり」等の動的振る舞いは §4 トリガー / §5 フロント処理に書くべき。
        動的振る舞いは項目の event_ref で §4/§5 にリンクされているため、備考列での重複記述は禁止。
      - 「動的生成」「JavaScript で」「JS で」は v51 で撤廃用語に指定 (業務担当者語彙ではない実装詳細語)。
        v49 までは STATIC_ALLOWED_KEYWORDS で例外扱いだったが、v51 で撤廃検出対象に格上げ。
    接続情報:
      - 呼出元: verify_intermediate.py main → all_issues に集約
      - 仕様: 16_docx出力ハンドオフ.md §18 / 01_画面レイアウト.md の備考欄の記述
      - 対象データ: merged['screen_layout']['items'][].remarks

    検出パターン (全て ERROR):
      - 英語動作系: blur / focus / change / click / keydown / mouseover 等
      - 日本語動作系: フォーカス外し / フォーカス時 / 変更時 / クリック時 / 押下時 / 入力時
      - 自動振る舞い: 自動取得 / 自動照合 / 自動展開 / 自動計算 / 自動反映
      - サジェスト関連: サジェスト機能 / サジェスト発火 / サジェスト表示
      - マスタ照合等の動的処理
      - 動的遷移: に切替 / に遷移 / を呼出 / へ移行 / を実行
      - 撤廃用語 (v51): 動的生成 / 動的に生成 / JavaScript で / JS で
    例外 (静的属性として許容):
      - 「未入力時」「無入力時」(状態属性、活性条件)
      - 「省略時」「NULL時」「0時」(デフォルト値・表示形式)
      - 「編集モード時のみ表示」「閲覧モード時のみ表示」(静的可視性条件)

    返り値: [(レベル, メッセージ), ...]
    """
    issues = []
    # v51 動的振る舞い検出パターン (コンパイル済み正規表現の (パターン, 説明) ペア)
    # 意味合い: ERROR 1 件 = 1 検出。同一 remarks 内に複数該当しても代表 1 件のみ出す (情報量過多回避)
    DYNAMIC_BEHAVIOR_PATTERNS = [
        (re.compile(r"\b(blur|focus|change|click|keydown|keyup|keypress|input|mouseover|mouseout|dblclick|submit)\b", re.IGNORECASE),
         "JS イベント名 (blur/focus/change/click 等)"),
        (re.compile(r"フォーカス外し|フォーカス時|フォーカスを?(外す|失う|当て|当てる)"),
         "フォーカス系の振る舞い記述"),
        # 「未入力時」「無入力時」は状態属性として除外 (否定先読み)
        # 意味合い: 「商品CD未入力時は非活性」は項目の活性条件 (静的属性) であり動的振る舞いではない
        (re.compile(r"変更時|クリック時|押下時|(?<![未無])入力時|選択時|チェック時"),
         "イベント発火タイミング記述"),
        (re.compile(r"自動(取得|照合|展開|計算|反映|表示|更新|削除|登録|保存|入力|描画|選択)"),
         "自動振る舞い記述"),
        (re.compile(r"サジェスト(機能|発火|表示|候補表示|候補ドロップダウン表示)(あり|を)?|入力中.*サジェスト"),
         "サジェスト関連振る舞い"),
        (re.compile(r"(マスタ|データ|値)照合"),
         "マスタ照合等の動的処理"),
        (re.compile(r"に切替|に遷移|に切り替|へ移行|へ遷移|を呼出|を呼び出し"),
         "動的遷移・呼出記述"),
        # v51 撤廃用語 (業務担当者語彙ではない実装詳細語)
        # v49 までは STATIC_ALLOWED_KEYWORDS で例外扱いだったが、v51 §18 で撤廃検出対象に格上げ
        (re.compile(r"動的生成|動的に生成|JavaScript で|JS で"),
         "v51 撤廃用語 (業務担当者語彙ではない実装詳細語、§18 参照)"),
    ]

    # §5/§6/§7 への参照リンク (括弧内、「参照」を含む) は誤検出から除外
    # 意味合い: 「(変更時挙動は §5 EV01 参照)」のような業務担当者向けナビゲーションは
    #          動的振る舞い記述ではなく「§5 のどこに書いてあるか」の道標。検出対象外。
    # 接続情報: v49 で remarks クリーンアップ後、参照リンク文を含む remarks が増えるため
    #          誤検出を抑止する必要がある (実装パターン: 括弧内に §X を含む全テキストを除去)
    REF_LINK_PATTERN = re.compile(r"[（(][^（）()]*(?:§\d|§ ?\d|参照)[^（）()]*[）)]")

    # 静的「時/モード」条件 (動的トリガーではない静的可視性条件) をマスクするパターン
    # 意味合い: 「編集モード時のみ表示」「省略時は 2999-12-31」「NULL時:「-」表示」のような
    #          静的条件節は動的判定から除外する。マスクして残ったテキストに動的キーワードがあるか確認
    STATIC_TIME_CONDITION_PATTERNS = [
        re.compile(r"(編集|閲覧|新規|削除|変更|入力|表示)モード時のみ[^、。]*"),
        re.compile(r"(編集|閲覧|新規|削除|変更|入力|表示)モード(?:では|で)(?:非?活性|非?表示|入力可|読取専用|読み取り専用)"),
        re.compile(r"省略時(?:は|の)[^、。]*"),
        re.compile(r"NULL\s*/?\s*0?\s*時[^、。]*"),
        re.compile(r"0\s*時(?:は|の)?[^、。]*"),
        re.compile(r"未入力時[^、。]*"),
        re.compile(r"無入力時[^、。]*"),
        re.compile(r"確定済時[^、。]*"),
        re.compile(r"完了時刻[^、。]*"),
        re.compile(r"参考値の場合は[^、。]*"),
        re.compile(r"現在(年|月|日|有効)[^、。]*"),
        re.compile(r"作成済の場合は[^、。]*"),
        re.compile(r"確定済の場合は[^、。]*"),
        # 2026-10-05 汎用化(3): 案件固有の静的な言い回しは project_config の
        # verify_overrides.extra_phrase_patterns（正規表現の文字列の配列。無ければ空）から足す。
        # 意味合い: 案件の業務語をこのファイルに直書きしないため。マスクは上から順に掛かるので、
        #           直書きだった頃と同じ位置に展開する（順序を変えると結果が変わりうる）。
        # 接続情報: verify_overrides は main() が project_config から取り出して渡す。
        *[re.compile(p) for p in verify_overrides.get("extra_phrase_patterns", [])],
        re.compile(r"当月該当時[^、。]*"),
        re.compile(r"処理月（[^）]+）が当月の時[^、。]*"),
        re.compile(r"の場合(?:のみ)?表示"),
    ]
    # 静的サジェスト表現 (「サジェスト候補ドロップダウン項目」のような項目自体の存在説明) は除外
    STATIC_SAJEST_PATTERN = re.compile(r"サジェスト候補ドロップダウン項目")
    # 静的ダイアログ表現 (「確認ダイアログ #confirm-overlay 内のタイトル」のような要素説明) は除外
    STATIC_DIALOG_PATTERN = re.compile(r"確認ダイアログ\s*[#＃「『][^、。]*?(内|ボタン|タイトル|本文)")

    items = (merged.get("screen_layout") or {}).get("items", []) or []
    for it in items:
        no = it.get("no", "?")
        name = it.get("item_name", "")
        remarks = it.get("remarks", "") or ""
        if not remarks:
            continue

        # 静的条件節をマスクしたテキストで検査
        # 意味合い: 「編集モード時のみ表示」「省略時は 2999-12-31」のような静的可視性条件節は
        #          動的判定から除外する。マスクして残ったテキストに動的キーワードがあるか確認
        scan_text = remarks
        for pat in STATIC_TIME_CONDITION_PATTERNS:
            scan_text = pat.sub("", scan_text)
        # 静的サジェスト・ダイアログ表現は除外
        scan_text = STATIC_SAJEST_PATTERN.sub("", scan_text)
        scan_text = STATIC_DIALOG_PATTERN.sub("", scan_text)
        # §5/§6/§7 参照リンク (業務担当者向け道標) も検出対象外
        scan_text = REF_LINK_PATTERN.sub("", scan_text)

        # 各パターンを順にチェックし、最初にヒットしたものを ERROR として報告
        for pattern, descr in DYNAMIC_BEHAVIOR_PATTERNS:
            m = pattern.search(scan_text)
            if m:
                hit = m.group(0)
                issues.append((
                    "ERROR",
                    f"screen_layout.items[no={no}][{name}].remarks に動的振る舞いキーワード '{hit}' を検出（{descr}）。"
                    f"v51 §18: 備考列には項目の静的属性のみ書き、動的振る舞いは §4 トリガー / §5 フロント処理に移してください "
                    f"(項目の event_ref で §4/§5 にリンク済)。"
                    f"検出箇所: '{remarks[:80]}{'...' if len(remarks) > 80 else ''}'"
                ))
                break  # 同一 remarks 内では代表 1 件のみ報告

    return issues


def check_screen_operation_trigger_area_ref(merged: dict) -> list:
    """v50.5 phase 2.5.1 新規: screen_operation kind の trigger に area_ref 必須を検証。

    意味合い: 業務的に screen_operation の trigger は必ず画面エリアに属するべき。area_ref 未設定の
              trigger があると generate_docx.js が「§5.1.7 [未割当]」グループを作って docx に出してしまい、
              設計書の品質が低く見える。
              本チェックで area_ref の付け漏れを ERROR で検出し、未割当が残ったまま docx 生成に
              至らないよう機械保証する。
              trigger_groups は migrate_v17_to_v18.py が作り、area_ref は normalize_screen_terminology.py が
              events[] の area_ref から写す。空のまま残るのは、対応する event に area_ref が付かなかった場合
              （トリガー文が「{画面名}の…」の型でない、またはエリア名を解決できない）なので、
              ERROR 文言でもその2点の確認を案内する（Phase 1 の担当は trigger_groups を書けない）。

    判定ルール: trigger_groups[kind='screen_operation'].triggers[].area_ref が空 → ERROR

    接続情報: 入力 = merged['trigger_groups'] / 出力 = [(level, message), ...]
              呼出元 = main() の all_issues.extend(...)
              再発防止仕様 = generate_docx.js sectionTriggerGroups 「未割当」グループ廃止 (v50.5 phase 2.5.1)
    """
    issues = []
    for g in merged.get("trigger_groups", []) or []:
        if g.get("kind") != "screen_operation":
            continue
        for t in g.get("triggers", []) or []:
            if not t.get("area_ref"):
                issues.append((
                    "ERROR",
                    f"trigger_groups[kind=screen_operation].triggers[no={t.get('trigger_no')}](action='{t.get('action', '')[:30]}'): "
                    f"area_ref が空（画面操作 trigger は必ず画面エリアに属するべき。"
                    f"normalize_screen_terminology.py が events[] から area_ref を写せなかった。"
                    f"events[].trigger が「{{画面名}}の…」の型か、エリア名が screen_config の画面エリアで解決できるかを確認）"
                ))
    return issues


# 複数の項目を1行に括った表記。「等」は後ろに漢字が続くもの（等級 など）を除く
BUNDLED_KEY_RE = re.compile(r"各列|各\S{1,10}の列|など|…|等(?![一-鿿])")  # 各…の列: 「各金額・点数の列」(H3)

# 入力欄とみなす画面項目の種類（小文字で比べる）。check_coverage_and_details が使う
INPUT_ITEM_TYPES = {
    "textbox", "text", "textarea", "input", "numberbox", "number", "combobox", "select",
    "calendar", "date", "checkbox", "radio", "radiobutton",
}


def check_coverage_and_details(merged: dict) -> list:
    """2026-10-12 新設: 中身の抜けを件数・空欄で検出する。

    判定ルール:
      - validations / messages が0件 → 画面に入力欄があれば ERROR、無ければ WARN。
        本当に無いなら トップレベルの validations_none_reason / messages_none_reason に理由を書けば通す
      - db_operations[].dataset[].logical_column が括り表記（各列/等/など/…）→ ERROR
      - event_processes[].details が空 → ERROR（初期表示などで本当に不要なら details_none_reason に理由を書けば通す）

    接続情報: 入力 = merged / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    items = (merged.get("screen_layout") or {}).get("items") or []
    has_input = any(str(i.get("item_type") or "").lower() in INPUT_ITEM_TYPES for i in items)
    for key, label in (("validations", "バリデーション"), ("messages", "メッセージ")):
        if not (merged.get(key) or []) and not merged.get(f"{key}_none_reason"):
            lvl = "ERROR" if has_input else "WARN"
            why = "画面に入力欄があるのに" if has_input else ""
            issues.append((lvl, f"{key}: {why}{label}が0件（実コードから抽出する。本当に無いなら {key}_none_reason に理由を書く）"))
    for op in merged.get("db_operations", []) or []:
        for ds in op.get("dataset") or []:
            col = str(ds.get("logical_column") or "")
            if BUNDLED_KEY_RE.search(col):
                issues.append(("ERROR", f"db_operations[{op.get('id') or '?'}].dataset: 列「{col}」が括り表記（1列1行で書く）"))
            # 2026-10-12: backend 下書き(apply_react_draft --backend-draft)は論理名を空で入れる。空のまま docx へ出ると 5.2 の表が空欄になる
            if not col.strip() or not str(ds.get("logical_table") or "").strip():
                issues.append(("ERROR", f"db_operations[{op.get('id') or '?'}].dataset[no={ds.get('no')}]: 論理テーブル名・論理列名が空"
                                        f"（物理名 {ds.get('physical_table') or ''}.{ds.get('physical_column') or ''} の業務の名前を書く）"))
    # 2026-10-12: 入力の誤りで返す 400 のエラーは、バリデーション一覧にも載るはず。載っていなければ書き漏れ
    vl_msgs = {v.get("message_id") for v in merged.get("validations", []) or []}
    for api in merged.get("api_spec", []) or []:
        for e in api.get("error_patterns") or []:
            mid = e.get("message_id")
            if str(e.get("status_code")) == "400" and mid and mid not in vl_msgs:
                issues.append(("ERROR", f"api_spec[{api.get('action', '')}].error_patterns: 400 のエラー {mid} が validations に無い"
                                        "（バック側の入力検査も validations に1件書き、message_id で結ぶ。画面に検査が無ければ kind を「API検査」にする）"))
    for ep in merged.get("event_processes", []) or []:
        ref = ep.get("event_ref") or ep.get("event_code") or "?"
        raw = ep.get("details") or []
        if DETAIL_TODO in raw:
            issues.append(("ERROR", f"event_processes[{ref}]: details に未記入の印が残っている（extract_react_draft.py の印を業務の文に置き換える）"))
        details = [d for d in raw if str(d).strip() and d != DETAIL_TODO]
        if not details and not ep.get("details_none_reason"):
            issues.append(("ERROR", f"event_processes[{ref}]: details が空（処理ステップを1行1 step で書く。本当に不要なら details_none_reason に理由を書く）"))
    return issues


def check_backend_process_substance(merged: dict) -> list:
    """v50.5 新設: backend_processes の各エントリが「key だけ存在して値が空テンプレ」になっていないかを検証。

    意味合い: 中身の無い docx が出る現象（10 件全てが logging_call/validation/response_build の
              5 step テンプレ + overview/request_spec/error_patterns/response_spec が全件空）を再発防止する
              実質チェック。「形（キーの存在）」だけ見ていた旧 verify を「中身（業務固有処理の有無）」まで
              踏み込ませる。

    判定ルール:
      - overview 空 → ERROR（業務的役割の説明が必須）
      - processing 内に kind in BUSINESS_KINDS の step が 0 件 → ERROR（業務固有処理が一切ない＝Agent
        がスケルトンだけ返した状態）
      - request_spec.params 空 → WARN（パラメータなし API もあり得るため WARN 止まり）
      - error_patterns 空 → ERROR（error_patterns_none_reason に理由があれば通す）
      - name と overview が同文 → WARN
      - response_spec.items の key が括り表記（各列/等/など/…/（）/ " / "）→ ERROR
      - response_spec.items/schema 両方空 → WARN（POST 系で何も返さない API もあるため WARN）

    BUSINESS_KINDS: apply_logging_steps.py の business_kinds と整合させる。logging_call / validation /
              response_build はスケルトン用 kind なので含めない（これらだけでは「業務処理」とみなさない）。

    接続情報: 入力 = merged['backend_processes'] / 出力 = [(level, message), ...]
              呼出元 = main() の all_issues.extend(...)
              再発防止仕様 = 共通の規則（業務シナリオ実装後の全件確認）
    """
    issues = []
    BUSINESS_KINDS = {
        "db_operation", "calculation", "cache_op", "transaction",
        "audit", "send_audit", "authorization", "auth_check",
        "external", "s3_op", "ses_op", "sqs_op", "exec_logic",
    }
    none_reason_by_action = {a.get("action"): a.get("error_patterns_none_reason")
                             for a in merged.get("api_spec", []) or [] if a.get("error_patterns_none_reason")}
    for bp in merged.get("backend_processes", []) or []:
        pid = bp.get("process_id") or bp.get("id") or "?"
        name = bp.get("name") or "?"
        # 1. overview の空チェック — 業務的役割の説明は必須
        if not str(bp.get("overview") or "").strip():
            issues.append(("ERROR", f"backend_processes[{pid}] {name}: overview が空（業務的役割の説明が必須）"))
        # 2. processing 内に業務固有 step ≥1 件 — スケルトンのみは Agent 出力不足
        proc = bp.get("processing", []) or []
        biz_steps = [s for s in proc if s.get("kind") in BUSINESS_KINDS]
        if not biz_steps:
            issues.append(("ERROR", f"backend_processes[{pid}] {name}: processing 内に業務固有 step（{', '.join(sorted(BUSINESS_KINDS))} のいずれか）が 1 件もない（logging_call/validation/response_build スケルトンのみ＝中身が空。SKILL.md Step 3 の「Phase 1 の成果物の渡し方」を確認）"))
        # 3. request_spec.params 空 — パラメータなし API もあり得るので WARN
        rs = bp.get("request_spec", {}) or {}
        if not rs.get("params"):
            issues.append(("WARN", f"backend_processes[{pid}] {name}: request_spec.params が空（リクエストに何も受け取らない API？）"))
        # 4. error_patterns 空 — ERROR（2026-10-12 WARN から格上げ）。本当にエラーを返さない API は
        #    error_patterns_none_reason（api_spec[] か backend_processes[] に書く）で理由を書けば通す。
        #    バックエンドのソースに raise / エラー返却があるかまでは見ない（API ごとの切り分けが難しいため）
        action = (bp.get("endpoint") or {}).get("action")
        if not (bp.get("error_patterns") or []) and not (bp.get("error_patterns_none_reason") or none_reason_by_action.get(action)):
            issues.append(("ERROR", f"backend_processes[{pid}] {name}: error_patterns が空（api_spec[].error_patterns に {{condition, status_code, message_id}} を書く。本当にエラーを返さないなら api_spec[].error_patterns_none_reason に理由を書く）"))
        # 4b. name と overview が同じ文 — 見出しと概要で同じ文を2回読ませることになる
        if str(bp.get("name") or "").strip() and str(bp.get("name") or "").strip() == str(bp.get("overview") or "").strip():
            issues.append(("WARN", f"backend_processes[{pid}] {name}: name と overview が同じ文（api_spec[].name に短い処理名を書くか、description を2文にする）"))
        # 4c. response_spec.items の key に括り表記 — 1行で複数のキーを括ると、出力の項目が数えられない
        for it in (bp.get("response_spec") or {}).get("items") or []:
            key = str(it.get("key") or "")
            if BUNDLED_KEY_RE.search(key) or "（" in key or " / " in key:
                issues.append(("ERROR", f"backend_processes[{pid}] {name}: response_spec.items の key「{key}」が括り表記（「各列」「等」「など」「…」や（）・/ で複数をまとめず、1キー1行で書く）"))
        # 5. response_spec.items/schema 両方空 — POST 系で何も返さない API もあるが WARN
        respspec = bp.get("response_spec", {}) or {}
        if not respspec.get("items") and not respspec.get("schema"):
            issues.append(("WARN", f"backend_processes[{pid}] {name}: response_spec.items / schema が両方空（レスポンス定義なし）"))
    return issues


def check_section_substance(merged: dict) -> list:
    """v50.5 新設: validations / calculations / common_logic / messages / parameters の各エントリが
              「業務的に意味ある値」を持つかを検証。

    意味合い: 旧 verify は「キーの存在」しか見なかったため、Agent が key だけ作って value 空文字を返しても
              ERROR 0 件で通っていた。本関数は各セクションの「業務的に最低限必要なフィールド」が空でないこと
              を検証し、再発防止する。

    判定ルール:
      - validations[].{field, rule, applied_when} が全て空 → ERROR（業務的に何の検証ルールか不明）
      - calculations[].{name, formula} が両方空 → ERROR（業務的に何の計算式か不明）
      - common_logic[].{name, description} が両方空 → ERROR
      - messages[].message 空 → ERROR（メッセージ本文は業務担当者が画面で見る文字、必須）
      - parameters[].{name, description} が両方空 → ERROR

    接続情報: 入力 = merged[各セクション] / 出力 = [(level, message), ...]
              呼出元 = main() の all_issues.extend(...)
              再発防止仕様 = 共通の規則（ハルシネーション防止 / 過剰実装・業務誤解の防止）
    """
    issues = []

    # validations: field / rule / applied_when のいずれかが必須
    for v in merged.get("validations", []) or []:
        no = v.get("no") or "?"
        if not any(str(v.get(k) or "").strip() for k in ("field", "rule", "applied_when")):
            issues.append(("ERROR", f"validations[{no}]: field / rule / applied_when が全て空（業務的に何の検証ルールか不明）"))

    # calculations: name と formula が両方空 → ERROR
    for c in merged.get("calculations", []) or []:
        no = c.get("no") or "?"
        if not str(c.get("name") or "").strip() and not str(c.get("formula") or "").strip():
            issues.append(("ERROR", f"calculations[{no}]: name と formula が両方空（業務的に何の計算式か不明）"))

    # common_logic: name と description が両方空 → ERROR
    for cl in merged.get("common_logic", []) or []:
        no = cl.get("no") or "?"
        if not str(cl.get("name") or "").strip() and not str(cl.get("description") or "").strip():
            issues.append(("ERROR", f"common_logic[{no}]: name と description が両方空"))

    # messages: message 本文が空 → ERROR
    for m in merged.get("messages", []) or []:
        # ラベルの識別子は message_id 優先。旧い形の msg_id / id も受ける（check_message_refs と同じ作法）
        mid = m.get("message_id") or m.get("msg_id") or m.get("id") or "?"
        if not str(m.get("message") or "").strip():
            issues.append(("ERROR", f"messages[{mid}]: message 本文が空"))

    # parameters: name と description が両方空 → ERROR
    for prm in merged.get("parameters", []) or []:
        no = prm.get("no") or "?"
        if not str(prm.get("name") or "").strip() and not str(prm.get("description") or "").strip():
            issues.append(("ERROR", f"parameters[{no}]: name と description が両方空"))

    return issues



def check_description_granularity(d, verify_overrides):
    """
    目的: §5 全カテゴリ (FR / BE / VL / CA / CL / DB / LG) の説明系フィールドで
          (1) 「。」連結マルチステートメント (1 文 1 概念違反) を WARN 検出する。
          (2) v55.1 §19 補遺: 概要 30 字超を ERROR (旧 WARN から格上げ) で検出する。
          (3) v55.1 §10 補遺: 概要本文に物理名 / 関数名 / camelCase / SNAKE_CASE /
              data-XXX 属性 / 関数呼出 xxx(...) を含む場合 ERROR 検出する。
    意味合い: v51 §19 「記述粒度ルール (1 文 1 概念原則)」の機械強制点。
              v55.1 で物理名残存 ERROR 化と概要 25 字超 ERROR 化を追加。
              共通ビジネスロジックの説明が 1 行に複数のロジックを詰め込んで読みにくくなるのを防ぎ、
              説明を配列へ退避するだけで本文を直さない対応も許さない。
              `description_details[]` 配列が存在する場合は概要本体は OK 判定とする
              (詳細は配列なので 1 文 1 概念ルール準拠の表現)。
    接続情報: 入力 = 中間 JSON (dict) /
              出力 = list[(level, msg)] level ∈ {"WARN", "ERROR"} /
              呼出元 = verify_intermediate.py main() /
              関連 = 16_docx出力ハンドオフ.md §10 / §19 / §19.6 v55.1 補遺 /
                    generate_docx.js renderDescriptionWithDetails
    """
    import re as _re
    issues = []

    # 業務的参照 ID は OK (CL003 等)
    # 2026-10-05 汎用化(3): 案件独自の接頭辞と業界用語は project_config の verify_overrides から足す
    #   business_ref_prefixes = 参照 ID の接頭辞（文字列の配列。無ければ空）
    #   business_ok_words     = 物理名として検出しない語（文字列の配列。無ければ空）
    # 意味合い: 接頭辞・業界用語は案件ごとに違うため、このファイルには共通のものだけを持つ。
    #           接頭辞は正規表現ではなく文字列として扱うので re.escape を通す。
    # 接続情報: verify_overrides は main() が project_config から取り出して渡す。
    #           下の _detect_phys_names がこの2つを使う。
    BUSINESS_REF_PAT = _re.compile(
        '^(' + '|'.join(
            ["CL", "FR", "BE", "VL", "CA", "DB", "LG", "MS"]
            + [_re.escape(p) for p in verify_overrides.get("business_ref_prefixes", [])]
        ) + ')[0-9_]+$'
    )
    # 業界用語ホワイトリスト (業務担当者の語彙に通用するもの)
    # 一般の頭字語（URL / API / ID / CSV / PDF / HTTP / JSON）も物理名として検出しない。
    # 意味合い: 業務担当者にも通じる一般語で、概要に書いても物理名の残存ではないため
    #           （初期処理の概要が「…URLパラメータ条件…」の形で自動生成され、誤検出になっていた）。
    # 接続情報: 下の _detect_phys_names が camelCase / PascalCase / UPPER の各判定でこの集合を見る。
    BUSINESS_OK = {
        "Google", "Excel", "Enter", "Shift", "JWT",
        "URL", "API", "ID", "CSV", "PDF", "HTTP", "JSON",
    } | set(verify_overrides.get("business_ok_words", []))

    # 英大文字だけの短い略語(POS・CSV など)は、ソースの識別子に大小文字まで同じ形で在るときだけ物理名とみなす。
    # 意味合い: 「POS会計…」のような業務用語を物理名と誤検出しないため。ソースの識別子は main() が source_identifiers で渡す。
    src_idents = set(verify_overrides.get("source_identifiers", []))

    def _is_abbrev(w):
        return w.isalpha() and w.isupper() and len(w) <= 5 and w not in src_idents

    def _detect_phys_names(text):
        """物理名 / 関数名 / camelCase / SNAKE_CASE / data-XXX / 関数呼出 を検出。
        日本語と英字の境界では \\b が機能しないため独自境界を使う。
        """
        if not text:
            return []
        found = []
        # camelCase (小文字始まり、内部大文字)
        for m in _re.finditer(r'(?<![A-Za-z0-9_])([a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*)(?![A-Za-z0-9_])', text):
            w = m.group(1)
            if w not in BUSINESS_OK and not BUSINESS_REF_PAT.match(w):
                found.append(f"camelCase:{w}")
        # PascalCase (大文字始まり、3 字以上、業務用語以外)
        for m in _re.finditer(r'(?<![A-Za-z0-9_])([A-Z][a-zA-Z]{2,})(?![A-Za-z0-9_])', text):
            w = m.group(1)
            if w in BUSINESS_OK or BUSINESS_REF_PAT.match(w) or _is_abbrev(w):
                continue
            found.append(f"PascalCase:{w}")
        # SNAKE_CASE / 大文字略語 (2 文字以上)
        for m in _re.finditer(r'(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]+)(?![A-Za-z0-9_])', text):
            w = m.group(1)
            if w in BUSINESS_OK or BUSINESS_REF_PAT.match(w) or _is_abbrev(w):
                continue
            found.append(f"UPPER:{w}")
        # data-XXX 属性
        for m in _re.finditer(r'data-[a-z][a-z-]*', text):
            found.append(f"data-attr:{m.group(0)}")
        # 関数呼出 xxx(...)
        for m in _re.finditer(r'(?<![A-Za-z0-9_])([a-z][a-zA-Z0-9_]+)\s*\(', text):
            found.append(f"func:{m.group(1)}()")
        return found

    # 対象セクション + フィールド + ID取得関数のマッピング
    # 意味合い: §19.1 ルール表の対象フィールドを機械的に走査
    targets = [
        ("front_processes", "overview", lambda x: x.get("process_id") or x.get("legacy_id") or ""),
        ("backend_processes", "overview", lambda x: x.get("process_id") or x.get("legacy_id") or ""),
        ("validations", "rule", lambda x: x.get("validation_id") or x.get("no") or ""),
        ("validations", "applied_when", lambda x: x.get("validation_id") or x.get("no") or ""),
        ("calculations", "remarks", lambda x: x.get("calc_id") or x.get("no") or ""),
        ("common_logic", "description", lambda x: x.get("logic_id") or x.get("no") or ""),
        ("db_operations", "summary", lambda x: x.get("op_id") or x.get("id") or ""),
    ]
    # 概要長閾値 (v55.1 §19 補遺: 30 字超は ERROR、80 字超 + 句点なしは長すぎ ERROR)
    max_summary_len_error = 30   # v55.1 補遺: 25 字目安、30 字超は ERROR
    max_summary_len_warn = 80    # 既存閾値 (句点なし長文)

    for section_key, field, id_fn in targets:
        for item in d.get(section_key, []):
            v = item.get(field) or ''
            if not isinstance(v, str):
                continue
            item_id = id_fn(item)
            details_field = field + "_details"
            has_details = isinstance(item.get(details_field), list) and len(item[details_field]) > 0
            period_count = v.count("。")
            # 句点 2+ 検出 (詳細配列があれば概要本体 = 先頭 1 文として OK 想定だが念のため検証)
            if period_count >= 2 and not has_details:
                issues.append((
                    "WARN",
                    f"v51 §19 1 文 1 概念ルール違反疑い: {section_key}[{item_id}].{field} に句点 {period_count} 個 ({v[:60]}...)"
                ))
            # v55.1 §19 補遺: 概要 30 字超を ERROR で検出
            # 概要 = description / overview / summary / business_title が対象
            if field in ("overview", "description", "summary", "business_title"):
                if len(v) > max_summary_len_error:
                    issues.append((
                        "ERROR",
                        f"v55.1 §19 補遺 概要 30 字超: {section_key}[{item_id}].{field} が {len(v)} 文字 (25 字以内推奨、30 字超 ERROR): {v[:80]}"
                    ))
            # 概要長すぎ検出 (句点なしで 80 文字超)
            if period_count == 0 and len(v) > max_summary_len_warn:
                issues.append((
                    "WARN",
                    f"v51 §19 概要長すぎ疑い: {section_key}[{item_id}].{field} が {len(v)} 文字 + 句点なし ({v[:60]}...)"
                ))
            # v55.1 §10 補遺: 概要本文に物理名残存を ERROR 検出
            if field in ("overview", "description", "summary", "business_title"):
                phys_names = _detect_phys_names(v)
                if phys_names:
                    issues.append((
                        "ERROR",
                        f"v55.1 §10 補遺 概要本文に物理名残存: {section_key}[{item_id}].{field} 検出={phys_names[:5]} 本文={v[:80]}"
                    ))

    # ステップ系 (front_processes.steps / backend_processes.processing / common_logic.processing_steps)
    # 意味合い: ステップ内の description も同じく 1 文 1 概念ルールを適用
    step_targets = [
        ("front_processes", "steps", lambda x: x.get("process_id") or ""),
        ("backend_processes", "processing", lambda x: x.get("process_id") or ""),
        ("common_logic", "processing_steps", lambda x: x.get("logic_id") or ""),
    ]
    for section_key, step_key, parent_id_fn in step_targets:
        for parent in d.get(section_key, []):
            parent_id = parent_id_fn(parent)
            for step in parent.get(step_key, []) or []:
                # WHY(2026-10-15 run16): common_logic の processing_steps が文字列の並びで来て、step.get が例外になり検査が止まった。
                # 例外にせず、形の誤りとして ERROR で出す(合成 merge_partials.normalize_processing_steps が common_logic では dict にそろえる)
                if not isinstance(step, dict):
                    issues.append(("ERROR", f"形: {section_key}[{parent_id}].{step_key} の要素が dict でない({str(step)[:40]})。{{no, kind, description}} の形で書く"))
                    continue
                v = step.get("description") or ''
                if not isinstance(v, str):
                    continue
                step_no = step.get("step_no") or step.get("no") or "?"
                has_details = isinstance(step.get("description_details"), list) and len(step["description_details"]) > 0
                period_count = v.count("。")
                if period_count >= 2 and not has_details:
                    issues.append((
                        "WARN",
                        f"v51 §19 1 文 1 概念ルール違反疑い (step): {section_key}[{parent_id}].{step_key}[step_no={step_no}].description に句点 {period_count} 個 ({v[:60]}...)"
                    ))

    # error_patterns.business_reason
    for bp in d.get("backend_processes", []):
        bp_id = bp.get("process_id") or ""
        for ep in bp.get("error_patterns", []) or []:
            v = ep.get("business_reason") or ''
            if not isinstance(v, str):
                continue
            has_details = isinstance(ep.get("business_reason_details"), list) and len(ep["business_reason_details"]) > 0
            period_count = v.count("。")
            if period_count >= 2 and not has_details:
                issues.append((
                    "WARN",
                    f"v51 §19 1 文 1 概念ルール違反疑い (error_pattern): backend_processes[{bp_id}].error_patterns[].business_reason に句点 {period_count} 個 ({v[:60]}...)"
                ))

    return issues


def check_v18_schema(merged: dict) -> list:
    """v18 整合性ルール（15_中間JSONスキーマ.md §整合性ルール 12-17）

    対応ルール:
    - 12: trigger_groups[].triggers[].calls[] ⊆ front_processes[].process_id
    - 13: front_processes[].steps[].backend_call.backend_id ⊆ backend_processes[].process_id
    - 14: backend_processes[].called_by_front[] ⊆ front_processes[].process_id、かつ双方向一致
    - 15: backend_processes[].endpoint の (method, path, action) が一意（1 endpoint = 1 process）
    - 16: process_id 採番規約（F001 / B001 形式の連番）
    - 17: validation/calculation/common_logic 参照整合
    """
    issues = []
    triggers_groups = merged.get("trigger_groups", []) or []
    front_processes = merged.get("front_processes", []) or []
    backend_processes = merged.get("backend_processes", []) or []

    if not triggers_groups and not front_processes and not backend_processes:
        # v18 形式が未生成（v17 以前の中間JSON）
        return issues

    front_ids = {fp.get("process_id") for fp in front_processes if fp.get("process_id")}
    backend_ids = {bp.get("process_id") for bp in backend_processes if bp.get("process_id")}

    # ルール12: trigger → front_process 整合
    # v50 Phase 2: triggers[].front_refs（新名、複数参照配列）優先、旧 triggers[].calls フォールバック
    for g in triggers_groups:
        for t in g.get("triggers", []) or []:
            front_refs_list = t.get("front_refs") if t.get("front_refs") is not None else t.get("calls", [])
            for c in front_refs_list or []:
                if c and c not in front_ids:
                    issues.append(("ERROR", f"trigger_groups[kind={g.get('kind')}].triggers[no={t.get('trigger_no')}].front_refs={c} が front_processes[] に存在しない"))

    # ルール13: front_process → backend_process 整合
    # v50 Phase 2: backend_call.backend_ref（新名、単数参照）優先、旧 backend_id フォールバック
    for fp in front_processes:
        for s in fp.get("steps", []) or []:
            bc = s.get("backend_call")
            if not bc:
                continue
            bid = bc.get("backend_ref") or bc.get("backend_id")
            if bid and bid not in backend_ids:
                issues.append(("ERROR", f"front_processes[{fp.get('process_id')}].steps[{s.get('step_no')}].backend_call.backend_ref={bid} が backend_processes[] に存在しない"))

    # ルール14: backend_process 逆参照整合（called_by_front[] ⊆ front_ids、かつ双方向一致）
    # 双方向一致: F が B を steps で呼んでいるなら、B の called_by_front に F が含まれる
    fp_calls_bp = {}  # process_id -> set(backend_ids)
    for fp in front_processes:
        called = set()
        for s in fp.get("steps", []) or []:
            bc = s.get("backend_call")
            # v50 Phase 2: backend_ref（新名）優先、backend_id（旧名）フォールバック
            if bc:
                _bid = bc.get("backend_ref") or bc.get("backend_id")
                if _bid:
                    called.add(_bid)
        fp_calls_bp[fp.get("process_id")] = called

    for bp in backend_processes:
        cbf = bp.get("called_by_front", []) or []
        for fid in cbf:
            if fid not in front_ids:
                issues.append(("ERROR", f"backend_processes[{bp.get('process_id')}].called_by_front={fid} が front_processes[] に存在しない"))
            else:
                if bp.get("process_id") not in fp_calls_bp.get(fid, set()):
                    issues.append(("WARN", f"backend_processes[{bp.get('process_id')}].called_by_front に {fid} があるが、{fid}.steps[].backend_call で {bp.get('process_id')} を呼んでいない（双方向不一致）"))

    # ルール15: エンドポイント一意性
    endpoint_keys = {}
    for bp in backend_processes:
        ep = bp.get("endpoint", {}) or {}
        key = (ep.get("method"), ep.get("path"), ep.get("action"))
        if key in endpoint_keys:
            issues.append(("ERROR", f"backend_processes[{bp.get('process_id')}] のエンドポイント {key} が既に {endpoint_keys[key]} で使われている（1 endpoint = 1 process 違反）"))
        else:
            endpoint_keys[key] = bp.get("process_id")

    # ルール16: process_id 採番規約
    # v49: v43 で旧体系（F###/B###）→ 新体系（FR#/BE#、サブラベル可: FR1-EDIT）に移行済。
    #      どちらの体系も許容するパターンで検証（後方互換 + 新体系対応）。
    import re
    f_pattern = re.compile(r"^(FR\d+(-[\w]+)?|F\d{3,})$")
    b_pattern = re.compile(r"^(BE\d+(-[\w]+)?|B\d{3,})$")
    for fp in front_processes:
        pid = fp.get("process_id", "")
        if not f_pattern.match(pid):
            issues.append(("ERROR", f"front_processes[].process_id='{pid}' は FR/F + 数字 の形式に従っていない"))
    for bp in backend_processes:
        pid = bp.get("process_id", "")
        if not b_pattern.match(pid):
            issues.append(("ERROR", f"backend_processes[].process_id='{pid}' は BE/B + 数字 の形式に従っていない"))

    # 採番が連番か（FR1/FR2/... or F001/F002/... のように飛びがないか）
    # v49: 新体系では prefix が 1-2 文字、サブラベル付き（FR1-EDIT）は親と同番号を共有するため set で重複除去
    def _extract_num(pid: str) -> int:
        m = re.match(r"^(?:FR|F|BE|B)(\d+)", pid)
        return int(m.group(1)) if m else 0
    front_nums_set = {_extract_num(fp.get("process_id", "")) for fp in front_processes if f_pattern.match(fp.get("process_id", ""))}
    front_nums = sorted(n for n in front_nums_set if n > 0)
    if front_nums and front_nums != list(range(1, len(front_nums) + 1)):
        issues.append(("WARN", f"front_processes の process_id 連番に飛びがある: {front_nums}"))
    backend_nums_set = {_extract_num(bp.get("process_id", "")) for bp in backend_processes if b_pattern.match(bp.get("process_id", ""))}
    backend_nums = sorted(n for n in backend_nums_set if n > 0)
    if backend_nums and backend_nums != list(range(1, len(backend_nums) + 1)):
        issues.append(("WARN", f"backend_processes の process_id 連番に飛びがある: {backend_nums}"))

    return issues


def check_structure(merged: dict, project_config: dict) -> list:
    """中間JSON 構造チェック: meta の必須フィールド + 主要セクションの存在

    2026-10-05 汎用化: project_pattern / profile の許容値は引数 project_config から読む。
    接続情報: 呼出元 = main()（load_project_config(feature) の戻り値を渡す）。
    """
    issues = []
    meta = merged.get("meta", {}) or {}
    # v49: meta 必須フィールドを業務的に必須な 2 件に絞る。
    # 意味合い: §1.1 文書情報サマリが廃止されたため、project_pattern / profile / generated_at は docx 出力で使われない。
    #          内部判定用に残置されているが、空でも ERROR ではなく WARN に格下げ。
    #          feature_name と source_html (or source_html_path) のみが業務的に必須。
    for k in ("feature_name",):
        if not meta.get(k):
            issues.append(("ERROR", f"meta.{k} が空"))
    # source_html / source_html_path のいずれかは必須（HTML 解析の起点として使われる）
    # 2026-10-10 React(TSX) 対応: meta.frontend_files（画面を構成するソース）があれば起点ありとみなす。
    # 目的: HTML を持たず screen_config.source_files だけで書く React の画面が、必ず ERROR になるのを防ぐ。
    # 意味合い: そうした画面は merge_partials.py が source_html / source_html_path を空で書き、extract_source_files.py が
    #          source_html_path を消すため、従来の2つだけを見る検査では起点が無いと誤判定される。
    #          HTML の画面は従来どおり source_html で通るので、結果は変わらない。
    # 接続情報: frontend_files は extract_source_files.py が source_files から作る（Phase 3）。この検査は Phase 6 なので届いている。
    if not meta.get("source_html") and not meta.get("source_html_path") and not meta.get("frontend_files"):
        issues.append(("ERROR", "meta.source_html / meta.source_html_path / meta.frontend_files のいずれも空（解析起点が不明）"))
    # v49: project_pattern / profile / generated_at / source_html_path 単独は WARN に格下げ
    for k in ("source_html_path", "project_pattern", "profile", "generated_at"):
        if not meta.get(k):
            issues.append(("WARN", f"meta.{k} が空（v49 で docx 出力からは廃止、内部判定用のみ）"))

    # 2026-10-05 汎用化: 許容値は project_config の allowed_project_patterns / allowed_profiles（文字列の配列）。
    # 意味合い: 型の分類は案件ごとに違うので直書きしない。キーが無ければ、その検査をしない。
    if "allowed_project_patterns" in project_config and meta.get("project_pattern") and meta.get("project_pattern") not in project_config["allowed_project_patterns"]:
        issues.append(("WARN", f"meta.project_pattern が想定外: {meta.get('project_pattern')}"))
    if "allowed_profiles" in project_config and meta.get("profile") and meta.get("profile") not in project_config["allowed_profiles"]:
        issues.append(("WARN", f"meta.profile が想定外: {meta.get('profile')}"))

    for k in ("logical_names", "screen_layout", "events", "initialization", "event_processes"):
        if k not in merged:
            issues.append(("ERROR", f"必須セクション '{k}' が中間JSONに存在しない"))

    return issues


# 2026-10-12 新設: 論理・物理テーブル名の仮表記と物理名の形式（検査 (1)）
# ponytail: 仮表記は角括弧と「集計結果の列」だけを見る。他の仮表記は見逃すので、増えたら語を足す
PLACEHOLDER_NAME_RE = re.compile(r"[\[\]［］]|集計結果の列")
PHYSICAL_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
# messages[].code のメッセージID形式（例 MSE00001 / MSG_POS_001）（検査 (3)）
MESSAGE_CODE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]*\d+$")


def check_table_name_placeholders(merged: dict) -> list:
    """2026-10-12 新設: 論理テーブル名・物理テーブル名の仮表記と物理名の形式を ERROR で検出する。

    判定ルール:
      - db_operations[].dataset[] と logical_names[] の logical_table / physical_table に
        角括弧や「集計結果の列」等の仮表記が残る → ERROR
      - 物理テーブル名が英数字と _ だけでない → ERROR
    意味合い: 仮の名前のまま docx に出る事故を止める。値は直さず、業務の名前への置き換えを促す。
    接続情報: 入力 = merged / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    rows = []
    for op in merged.get("db_operations", []) or []:
        for ds in op.get("dataset") or []:
            rows.append((f"db_operations[{op.get('id') or '?'}].dataset", ds.get("logical_table"), ds.get("physical_table")))
    for ln in merged.get("logical_names", []) or []:
        rows.append((f"logical_names[{ln.get('no') or '?'}]", ln.get("logical_table"), ln.get("physical_table")))
    for label, logical, physical in rows:
        for kind, name in (("論理", logical), ("物理", physical)):
            if name and PLACEHOLDER_NAME_RE.search(str(name)):
                issues.append(("ERROR", f"{label}: {kind}テーブル名「{name}」が仮表記のまま（業務の名前に置き換える）"))
        if physical and not PHYSICAL_NAME_RE.match(str(physical)):
            issues.append(("ERROR", f"{label}: 物理テーブル名「{physical}」が英数字と _ だけでない"))
    return issues


def check_file_descriptions(merged: dict) -> list:
    """2026-10-12 新設: 1.1/1.2 のファイル構成（meta.frontend_files / meta.backend_files）の説明空・種別 other を WARN で検出する。

    意味合い: 表に何のためのファイルかが空欄のまま出るのを防ぐ。種別 other は分類が決まっていない合図。
    接続情報: 入力 = merged['meta'] の frontend_files / backend_files / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    meta = merged.get("meta") or {}
    for key in ("frontend_files", "backend_files"):
        for f in meta.get(key) or []:
            path = f.get("path") or "?"
            if not str(f.get("description") or "").strip():
                issues.append(("WARN", f"meta.{key}[{path}]: 説明が空（1.1/1.2 のファイル構成に何のためのファイルかを書く）"))
            if str(f.get("kind") or "").lower() == "other":
                issues.append(("WARN", f"meta.{key}[{path}]: 種別が other（js / css / backend 等に分類する）"))
    return issues


def check_message_code_format(merged: dict) -> list:
    """2026-10-12 新設: messages[].code がメッセージID形式でないものを ERROR で検出する(同日 WARN から格上げ)。

    意味合い: code 列に validation_error のような種別名が入ると、業務担当者がメッセージを特定できない。
    判定: 空の code は対象外。
    接続情報: 入力 = merged['messages'] / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    for m in merged.get("messages", []) or []:
        code = str(m.get("code") or "").strip()
        if code and not MESSAGE_CODE_ID_RE.match(code):
            mid = m.get("message_id") or m.get("msg_id") or m.get("id") or "?"
            # 2026-10-12 WARN から格上げ: WARN のままだと Haiku が直さずに docx へ出す
            issues.append(("ERROR", f"messages[{mid}].code={code} がメッセージID形式（例 MSE00001）でない（code には message_id と同じ ID を書く）"))
    return issues


# 2026-10-13 新設: 合成後の中身の穴を ERROR で検出する（既存の検査では通ってしまう穴の機械強制）
# 意味合い: 名前だけで処理の中身が空の共通ロジック・DB 物理名の残存・未展開の穴・メッセージ ID の形式や
#          同じ ID に別の文面が付く不整合・DB 操作の結合/条件/集計/並び順の英語キーの空欄を止める。
# 接続情報: 入力 = merged（中間 JSON 全体）/ 出力 = [(level, message), ...] / 呼出元 = main()
MESSAGE_ID_STRICT_RE = re.compile(r"^MS[EWI]\d{5}$")
BRACE_HOLE_RE = re.compile(r"\{[^{}\s]+\}")  # 空白を含まない {名前} だけを穴とみなす（JSON 風の記述を誤検出しない）
# 応答の形を書く欄(api_spec[].response_pattern・backend_processes[].response_spec.pattern)は、応答の JSON の形で書く。
# 「成功時 {success: true, data: {rows}}」の {rows} は data の値の置き方(キー名)で、実行時に決まる穴ではない。
# WHY: 値の位置(「: {名前}」)と、内側に「:」のある波括弧({k: v} の形)だけ JSON の形として外す。それ以外は従来どおり穴。
RESPONSE_SHAPE_PATH_RE = re.compile(r"^(api_spec\[\d+\]\.response_pattern|backend_processes\[\d+\]\.response_spec\.pattern)$")
JSON_VALUE_BRACE_RE = re.compile(r":\s*\{[^{}]*\}")
JSON_OBJ_BRACE_RE = re.compile(r"\{[^{}]*:[^{}]*\}")


def _has_brace_hole(path: str, s: str) -> bool:
    """欄の文字列に、未展開の {名前} の穴があるか。応答の形の欄だけ JSON の形を穴にしない。"""
    if RESPONSE_SHAPE_PATH_RE.match(path):
        s = JSON_OBJ_BRACE_RE.sub("{}", JSON_VALUE_BRACE_RE.sub(": {}", s))
    return bool(BRACE_HOLE_RE.search(s))
# 2026-10-13: 画面項目の例示値は、実行時に決まる値(金額・件数・日付など)を {金額} 円 のように書式の例として書く(S1 手順書)。
# 具体的な値はソースに無く verify_vs_source が止めるため。この道筋の {名前} は未展開の穴とみなさない
EXAMPLE_PATH_RE = re.compile(r"^screen_layout\.items\[\d+\]\.example$")
# WHY(2026-10-15 run15 差6): messages の可変部は {項目名} で書くのがスキーマの形(15_中間JSONスキーマ.md の例「{項目名}は必須項目です。」)で、
#   ソースが `（HTTP ${r2.status}）` のようにテンプレートリテラルで組む文面の ${x} に対応する。これを未展開の穴として ERROR にすると、
#   H4 が正しく書いた「（HTTP {status}）」を語(「HTTP ステータスコード」)に置き換え、画面に出る形と区別できない文面になった。
#   対象は画面のメッセージ(messages[].message)だけ。messages_backend は下書きの text_expanded(展開済み)を一字一句写す欄なので {名前} を許さない
MESSAGE_PATH_RE = re.compile(r"^messages\[\d+\]\.message$")
# 例示値に残った {名前} は、書式の例として書かれるべき値が穴のまま残った状態。ERROR にせず WARN で S1 へ差し戻す。
# 理由: verify_partials.py の _EX_HOLE(0402803)と同じ判定にそろえ、2つの検査で同じ穴を別の重さで扱わないため。
# 空白を含む {…} も穴として拾う(BRACE_HOLE_RE は例示値には使わない)
EXAMPLE_HOLE_RE = re.compile(r"\{[^{}]+\}")
# DB 操作の日本語キーと、それに対応する英語キー（英語キーが空なら ERROR）
DB_OP_JP_TO_EN = (
    ("結合", "joins"),
    ("抽出条件", "where"),
    ("条件", "where"),
    ("集計", "group_by"),
    ("並び順", "order_by"),
)


def _iter_strings(node, path=""):
    """中間 JSON の全ての文字列値を (欄の道筋, 値) で順に取り出す。check_intermediate_strict が使う。
    道筋(api_spec[0].description 等)は run_units.py の owner_of が units.json の owns と照らして差し戻し先を引く。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _iter_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def check_intermediate_strict(merged: dict) -> list:
    """2026-10-13 新設: 中身の穴を ERROR で検出する（合成後の検査）。

    判定ルール:
      (1) common_logic の要素で processing_steps も expression も空 → ERROR
      (2) 全文字列に 'db_op_' が残る → ERROR
      (3) 全文字列に '〈' または波括弧の穴 '{名前}' が残る → ERROR
          （例外: screen_layout.items[].example の {…} の穴だけは WARN で S1 へ差し戻す。
            messages[].message の {名前} は可変部の正しい書き方(ソースの ${x} に対応)なので穴にしない)
      (4) messages の ID が ^MS[EWI]\\d{5}$ に合わない → ERROR / 同じ ID に別の文面が付く → ERROR
      (5) db_operations の要素に日本語キー（結合・抽出条件・条件・集計・並び順）の値があり、
          対応する英語キー（joins / where / group_by / order_by）が空 → ERROR
    接続情報: 呼出元 = main() / 入力 = merged / 出力 = [(level, message), ...]
    """
    issues = []
    # (1) 名前だけの共通ロジックを通さない
    for cl in merged.get("common_logic", []) or []:
        cl_no = cl.get("no") or cl.get("logic_id") or "?"
        if not (cl.get("processing_steps") or []) and not str(cl.get("expression") or "").strip():
            issues.append(("ERROR", f"common_logic[{cl_no}]: processing_steps も expression も空（処理の中身が無い。名前だけで通さない）"))
    # (2)(3) 物理名の残存と未展開の穴
    for path, s in _iter_strings(merged):
        if "db_op_" in s and not path.endswith("legacy_id"):  # legacy_id は旧 ID の控え(docx に出ない)
            issues.append(("ERROR", f"{path}: 物理名 'db_op_' が文字列に残存: '{s[:80]}'"))
        # 画面項目の例示値(screen_layout.items[].example)の {名前} は下の elif で WARN にする(〈 は従来どおり ERROR)。
        # 理由: 例示値は書式の例として実値で書くのが正(S1 手順書)。穴は S1 へ戻して実値に直させる
        # messages[].message の {名前} は、ソースのテンプレートリテラルの ${x} に対応する可変部の正しい書き方なので穴にしない(MESSAGE_PATH_RE の WHY)
        if "〈" in s or (_has_brace_hole(path, s) and not EXAMPLE_PATH_RE.match(path) and not MESSAGE_PATH_RE.match(path)):
            issues.append(("ERROR", f"{path}: 未展開の穴（〈 または {{名前}}）が残存: '{s[:80]}'"))
        # 例示値の {名前} は WARN に留める(最終段で残れば S1 へ差し戻す)。ERROR にすると docx 生成前に止まり、S1 の手戻りを機械的に強制しすぎるため
        elif EXAMPLE_PATH_RE.match(path) and EXAMPLE_HOLE_RE.search(s):
            issues.append(("WARN", f"{path}: 例示値に {{…}} の穴が残る（書式の例を実値で書き直す）: '{s[:80]}' → 差し戻し先 S1"))
    # (4) メッセージ ID の形式と、同じ ID の文面の一致
    seen_msg = {}
    for m in merged.get("messages", []) or []:
        mid = m.get("message_id") or m.get("id") or "?"
        text = str(m.get("message") or "")
        if not MESSAGE_ID_STRICT_RE.match(str(mid)):
            issues.append(("ERROR", f"messages[{mid}]: メッセージ ID が ^MS[EWI]\\d{{5}}$ に合わない（例 MSE00001）"))
        if mid in seen_msg and seen_msg[mid] != text:
            issues.append(("ERROR", f"messages[{mid}]: 同じ ID に別の文面が付いている（「{seen_msg[mid][:40]}」と「{text[:40]}」）"))
        seen_msg.setdefault(mid, text)
    # (5) DB 操作の日本語キーに対応する英語キーの空欄
    for op in merged.get("db_operations", []) or []:
        op_id = op.get("id") or "?"
        for jp, en in DB_OP_JP_TO_EN:
            if str(op.get(jp) or "").strip() and not str(op.get(en) or "").strip():
                issues.append(("ERROR", f"db_operations[{op_id}]: 日本語キー「{jp}」に値があるのに英語キー「{en}」が空（対応する英語キーに書く）"))
    return issues


# 2026-10-13 新設: メッセージの種別（severity）と ID 接頭辞（MSE/MSW/MSI）の食い違いと、
# 情報なのに警告の文面がある疑いを検出する（種別の誤りを業務担当者に渡さないため）。
# 意味合い: 接頭辞は種別を表す。error→MSE / warning→MSW / info→MSI。MS1 のような新体系 ID は接頭辞で判定できないので対象外。
# 接続情報: 入力 = merged['messages'] の message_id と severity（apply_id_scheme.py は ID を採番し直すが severity には触らない）
#           / 出力 = [(level, message), ...] / 呼出元 = main()
MESSAGE_SEVERITY_PREFIX = {"error": "MSE", "warning": "MSW", "info": "MSI"}
INFO_ALARM_WORDS_RE = re.compile(r"警告|注意|疑い|欠損|確認してください|失敗")


def check_message_severity_prefix(merged: dict) -> list:
    """メッセージの種別と ID 接頭辞の整合を検査する。

    判定ルール:
      (1) severity と ID の接頭辞（MSE/MSW/MSI）が食い違う → ERROR
      (2) severity が info なのに文面に警告語（警告・注意・疑い・欠損・確認してください・失敗）がある → WARN（種別の誤りの疑い）
    接続情報: 入力 = merged['messages'] / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    for m in merged.get("messages", []) or []:
        mid = str(m.get("message_id") or m.get("id") or "?")
        sev = str(m.get("severity") or "").lower()
        text = str(m.get("message") or "")
        prefix = next((p for p in MESSAGE_SEVERITY_PREFIX.values() if mid.startswith(p)), None)
        if prefix is not None and MESSAGE_SEVERITY_PREFIX.get(sev) != prefix:
            issues.append(("ERROR", f"messages[{mid}]: 種別 severity={sev or '(空)'} と ID の接頭辞 {prefix} が食い違う"))
        if sev == "info" and INFO_ALARM_WORDS_RE.search(text):
            issues.append(("WARN", f"messages[{mid}]: 種別が情報だが文面に警告語がある（種別の誤りの疑い）: '{text[:80]}'"))
    return issues


def check_src(merged: dict, sc: dict) -> list:
    """2026-10-13 新設(案 A4): 行の出典 src({file, line_start, line_end}) を検査する。
    (a) src の無い行は WARN。src が1行も無い中間 JSON(A3 の合成より前)では WARN を1件の要約にまとめる。
    (b) src.file が screen_config の source_files・backend_files(同じフォルダの補助モジュールを含む)に無い、
        line_start > line_end、line_end がファイルの行数を超える → ERROR(作り話の行番号を止める)。
    接続情報: 呼出元 = main() / 入力 = merged・sc / 出力 = [(level, message), ...]"""
    issues = []
    lists = {
        "events": merged.get("events") or [],
        "event_processes": [e for e in merged.get("event_processes") or [] if isinstance(e, dict) and e.get("event_code") != "EV00"],
        "api_spec": merged.get("api_spec") or [],
        "db_operations": merged.get("db_operations") or [],
        "calculations": merged.get("calculations") or [],
        "validations": merged.get("validations") or [],
        "common_logic": merged.get("common_logic") or [],
        "screen_layout.items": (merged.get("screen_layout") or {}).get("items") or [],
    }
    rows = [(name, r) for name, rs in lists.items() for r in rs if isinstance(r, dict)]
    if rows and not any(r.get("src") for _, r in rows):
        issues.append(("WARN", f"[出典] src が1行も無い({len(rows)} 行)。合成より前の中間 JSON と思われるため出典は未検査"))
    else:
        for name, r in rows:
            if not r.get("src"):
                issues.append(("WARN", f"[出典] {name}: src が無い(ソースの根拠が不明)"))
    srcs = list(sc.get("source_files") or []) + [str(Path(sc.get("project_root") or ".") / f) for f in sc.get("backend_files") or []]
    allowed_files = {Path(f).resolve() for f in srcs}
    allowed_dirs = {p.parent for p in allowed_files}
    line_counts = {}
    for name, r in rows:
        for s in r.get("src") or []:
            if not isinstance(s, dict):
                continue
            p = Path(s["file"]).resolve() if s.get("file") else None
            if p is None or not (p in allowed_files or p.parent in allowed_dirs):
                issues.append(("ERROR", f"[出典] {name}: src.file が対象のソースに無い: {s.get('file')}"))
                continue
            ls, le = s.get("line_start"), s.get("line_end")
            if isinstance(ls, int) and isinstance(le, int) and ls > le:
                issues.append(("ERROR", f"[出典] {name}: line_start {ls} が line_end {le} より大きい"))
            if isinstance(le, int) and p not in line_counts:
                try:
                    line_counts[p] = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    line_counts[p] = None
            if isinstance(le, int) and line_counts.get(p) is not None and le > line_counts[p]:
                issues.append(("ERROR", f"[出典] {name}: line_end {le} がファイルの行数 {line_counts[p]} を超える: {p.name}"))
    return issues


def _same_place(a: dict, b: dict) -> bool:
    """2つの src({file, line_start, line_end})が同じファイルで行範囲が重なるか。"""
    if str(a.get("file") or "") != str(b.get("file") or ""):
        return False
    # fallback-ok: 行範囲の終わりが無い src は開始行だけの1行とみなす(src は行範囲を常に持つ想定)。開始行が無いものは 0 で重ならない
    a0, a1 = int(a.get("line_start") or 0), int(a.get("line_end") or a.get("line_start") or 0)
    # fallback-ok: 同上(比較の相手側)
    b0, b1 = int(b.get("line_start") or 0), int(b.get("line_end") or b.get("line_start") or 0)
    return a0 <= b1 and b0 <= a1


def _fixed_for_item(s: dict, literal: list) -> list:
    """画面項目1件に結ぶ固定文の要素。項目に src(TSX の行範囲)があれば、その範囲と重なる要素だけを使う。無ければ名前・example で結ぶ。
    WHY: merge_partials の初期値・表示は src の位置で結ぶ(_src_fact)。名前だけで結ぶと、同じ文言が別の場所にあるとき
         位置の違う要素の表示で判定してしまう。POS会計レポートの項目「空」は行の見出しの印(Card の中。初期表示では出ない)だが、
         凡例の「空」(初期表示で見える)で結ぶと誤って ERROR になった。
    """
    named = linked_fixed_elements([s.get("item_name"), s.get("example")], literal)
    srcs = s.get("src") or []
    spans = [x for x in ([srcs] if isinstance(srcs, dict) else srcs) if isinstance(x, dict) and x.get("line_start")]
    if not spans:
        return named
    # WHY(P20): 名前が一致しない文字だけの要素（ボタンの文言など）も、項目の出典の行と重なれば同じ位置の固定文として結ぶ。
    #      名前の一致だけで結ぶと、見える固定文のボタン文言の Blank を取りこぼすため。名前の一致する要素も、位置が重なるときだけ残る（named は literal の部分集合）。
    #      呼出元 = check_initialization_items の (c)。
    out = []
    for el in literal:
        els = el.get("src") or []
        for es in ([els] if isinstance(els, dict) else els):
            if isinstance(es, dict) and any(_same_place(es, sp) for sp in spans):
                out.append(el)
                break
    return out


def check_initialization_items(merged: dict, drafts: list = None) -> list:
    """B16 (round9): 初期処理の表の中身を検査する。

    判定ルール:
      (a) initialization.items の件数が screen_layout.items の件数と違う → ERROR
      (b) 全項目が「活性/表示」の一色 → ERROR（表示条件・disabled を評価できた項目が1つも無い）
      (c) 固定文の項目で、初期表示で見えるものの初期値が Blank → ERROR。
          固定文 = react 下書きの jsx_elements のうち文字列リテラルだけの要素（text があり label が無い）に、
          項目の名前か example の文言が一致する項目。見えるか = visible_chain（無ければ visible_if）を初期値で評価して真。
          初期表示で見えない固定文（読込中・エラーなど）の Blank は正しいので対象外。
          見えるかは画面項目の位置ごとに持つ（同じ名前の別の項目、例えば隠れた行の印「空」と凡例の「空」を混ぜない）。
    WHY: merge_partials.build_initialization は評価できない項目を仮埋めの「活性/表示」で出す。
         後段の検査が無いと、その仮埋めのまま docx に出ても止まらないため。
         固定文の判定は example の形（{…} の有無）では行わない。実行時の値（総売上など）を固定文と誤認するため。
    接続情報: 入力 = merged['initialization']['items'] / merged['screen_layout']['items'] / drafts（react 下書きの一覧。省略時は (c) を判定しない）
              / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    init_items = (merged.get("initialization") or {}).get("items") or []
    sl_items = (merged.get("screen_layout") or {}).get("items") or []
    if len(init_items) != len(sl_items):
        issues.append(("ERROR", f"initialization.items が {len(init_items)} 件で screen_layout.items の {len(sl_items)} 件と一致しない（画面項目と同数にする）"))
    if init_items and all(str(i.get("active") or "") == "活性" and str(i.get("visible") or "") == "表示" for i in init_items):
        issues.append(("ERROR", "initialization.items が全項目「活性/表示」の一色（表示条件・disabled を評価できた項目が1つも無い。仮埋めのまま）"))
    # (c) 固定文の判定。state の初期値で表示条件を評価する（merge_partials と同じ評価器・同じ下書きの事実）
    drafts = drafts or []
    env = build_env([si for d in drafts for si in d.get("state_initials") or [] if isinstance(si, dict)],
                    [da for d in drafts for da in d.get("derived_arrays") or [] if isinstance(da, dict)])
    literal = literal_text_elements(drafts)
    # P20: ボタンの文言（tag が button で label を持つ要素）も固定文の候補に入れる。literal_text_elements は label を持つ要素を外すため、ここで足す
    literal = literal + [el for d in drafts for el in d.get("jsx_elements") or [] if isinstance(el, dict) and el.get("tag") == "button" and str(el.get("label") or "").strip()]
    # WHY(2026-10-14 run12): 見えるかは項目の位置（画面項目の並び）で持つ。名前の集合で持つと、同じ名前の別の項目
    #      （Card の中の隠れた印「空」と、凡例の「空」）の一方が見えるだけで、隠れた方まで Blank を ERROR にした。
    visible_fixed_pos = set()
    visible_fixed_name = set()
    for k, s in enumerate(sl_items):
        linked = _fixed_for_item(s, literal)
        if any(visible_initially(el, env) is True for el in linked):
            visible_fixed_pos.add(k)
            visible_fixed_name.add(str(s.get("item_name") or "").strip())
    for k, i in enumerate(init_items):
        name = str(i.get("item_name") or "").strip()
        # 同じ位置の画面項目と名前が合えば、その位置の判定。合わない（並びがずれた）ときだけ名前で見る
        paired = k < len(sl_items) and str(sl_items[k].get("item_name") or "").strip() == name
        fixed = (k in visible_fixed_pos) if paired else (name in visible_fixed_name)
        if fixed and str(i.get("initial_value") or "") == "Blank":
            issues.append(("ERROR", f"initialization.items[{i.get('no') or '?'}]({name}): 初期表示で見える固定文の初期値が Blank（画面に書かれた文言を初期値に入れる）→ 差し戻し先 S1（項目の出典 src を、その文言を描く JSX の行範囲に直す）"))
    return issues


def _resolve_src_file(file: str, project_root: str):
    """src.file の実在するパスを返す。絶対パスはそのまま、相対パスは project_root 基準も見る。無ければ None。
    WHY: 出典の file は案件ごとに絶対パスと project_root 基準の相対が混ざるため、両方を見る。"""
    for cand in (Path(file), Path(project_root or ".") / file):
        if cand.is_file():
            return cand
    return None


def _tsx_src_of(item: dict) -> list:
    """項目の出典のうち TSX/JSX の行範囲だけを返す。
    WHY(round12 V2): 「画面が TSX か」と「項目が位置で結べる出典を持つか」を同じ読み方で判定するため、1か所にまとめる。"""
    return [s for s in item.get("src") or [] if isinstance(s, dict) and str(s.get("file") or "").endswith((".tsx", ".jsx"))]


def check_initialization_eval(merged: dict, sc: dict, drafts: list = None) -> list:
    """B16 差異6 (2026-10-14): 初期処理の活性・表示を、画面項目の出典（src の TSX の行範囲）から再評価して検査する。

    判定ルール:
      initialization.items[k] と screen_layout.items[k] を名前で対にする（件数・名前が違えば対にしない）。
      項目の src のうち最初の TSX/JSX の行範囲を resolve_for_lines で出典の位置に結び、表示条件の鎖・disabled の鎖を取る。
      位置で結べない項目（JSX の要素も配列の使用箇所も無い行範囲）は ERROR（差し戻し先 S1）。推測して読み飛ばさない。
      state の初期値で評価し（merge_partials と同じ関数）、評価できた値（None でない）と記載の活性・表示が違えば ERROR。
      評価が None の項目は見ない。「全項目が一色か」は見ない（正しく一色の画面で誤検出になるため）。
      TSX が読めないファイルは検査せず、ファイルごとに INFO を1件出す。
    WHY: merge_partials は名前で結べない項目を仮埋めの「活性/表示」で出す。後段で止めないと、誤った表示条件が docx まで出るため。
    接続情報: 入力 = merged['initialization']['items'] / merged['screen_layout']['items'] / sc（project_root）/ drafts（state の初期値）
              / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    init_items = (merged.get("initialization") or {}).get("items") or []
    sl_items = (merged.get("screen_layout") or {}).get("items") or []
    if len(init_items) != len(sl_items):
        return issues  # 件数の不一致は check_initialization_items が ERROR にする
    drafts = drafts or []
    env = build_env([si for d in drafts for si in d.get("state_initials") or [] if isinstance(si, dict)],
                    [da for d in drafts for da in d.get("derived_arrays") or [] if isinstance(da, dict)])
    root = sc.get("project_root") or "."
    missing = []
    # V2 (round12): 画面が TSX か（どれかの項目が TSX/JSX の出典を持つか）。HTML・サンプルの画面は従来どおり出典の無い項目を読み飛ばす
    tsx_screen = any(_tsx_src_of(sl) for sl in sl_items)
    for ini, sl in zip(init_items, sl_items):
        name = str(sl.get("item_name") or "").strip()
        srcs = _tsx_src_of(sl)
        # WHY(round12 V2): TSX 画面の項目で出典が無いものは、位置で結べず仮埋め（活性・表示）になる。読み飛ばすと誤った表示条件が docx まで出るため、名前の一致に関係なく止める
        if tsx_screen and not srcs:
            issues.append(("ERROR", f"screen_layout.items[{sl.get('no') or '?'}]({name}): 出典(src)が無く、初期表示の活性・表示を位置で結べない（仮埋めの表示になる）。読んだ範囲 {{file,line_start,line_end}} を書く → 差し戻し先 S1"))
            continue
        if str(ini.get("item_name") or "").strip() != name:
            continue
        if not srcs:
            continue
        s = srcs[0]
        ls, le = s.get("line_start"), s.get("line_end")
        if not (isinstance(ls, int) and isinstance(le, int)):
            continue
        path = _resolve_src_file(str(s["file"]), root)
        if path is None:
            if str(s["file"]) not in missing:
                missing.append(str(s["file"]))
            continue
        res = resolve_for_lines(str(path), ls, le)
        if res is None:
            # WHY(round11 U2): 結べない項目を仮埋め（活性・表示）のまま通すと、誤った表示条件が docx まで出る（round10 の 6）。止めて S1 へ差し戻す
            issues.append(("ERROR", f"initialization.items[{ini.get('no') or '?'}]({name}): 表示条件を出典の位置で結べない（出典 {path.name}:{ls}-{le}）。出典の行範囲を、その項目を描く JSX の要素か、データ配列の宣言に直す → 差し戻し先 S1"))
            continue
        r = res[0]
        dis = disabled_value(r.get("disabled_chain"), env)
        vis = chain_value(r.get("visible_chain") or [], env)
        no = ini.get("no") or "?"
        where = f"出典 {path.name}:{ls}-{le}"
        if dis is not None and str(ini.get("active") or "") != ("非活性" if dis else "活性"):
            issues.append(("ERROR", f"initialization.items[{no}]({name}): 活性の記載「{ini.get('active')}」が表示条件の評価（{'非活性' if dis else '活性'}）と違う。{where} の disabled に合わせる → 差し戻し先 S1（項目の出典 src を、その項目を描く JSX の行範囲に直す）"))
        if vis is not None and str(ini.get("visible") or "") != ("表示" if vis else "非表示"):
            issues.append(("ERROR", f"initialization.items[{no}]({name}): 表示の記載「{ini.get('visible')}」が表示条件の評価（{'表示' if vis else '非表示'}）と違う。{where} の表示条件に合わせる → 差し戻し先 S1（項目の出典 src を、その項目を描く JSX の行範囲に直す）"))
    for f in missing:
        issues.append(("INFO", f"initialization.items の表示条件を検査できない（TSX が読めない）: {f}"))
    return issues


def check_initialization_dup(merged: dict) -> list:
    """B16 round12 V2 (新11 の検査): 初期値に別の項目の初期値が含まれる項目を ERROR にする。

    判定ルール:
      initialization.items の i≠j で、j の初期値が 12 字以上・Blank でなく、i の初期値に含まれ、
      かつ i の初期値の方が長いとき、i を ERROR（差し戻し先 S1）。同じ文言の2項目は重複とみなさない。
      ただし j の出典（TSX の行範囲）が i の出典の行範囲の中に収まる入れ子（段落の中の強調など）は、画面に出る文のとおりなので見ない。
    WHY: 出典の行範囲が兄弟の項目の文言まで取ると、初期値に別の項目の文言が重複して入る（round12 の IT61・IT62 で再現）。
         12 字未満の語は別の文言に偶然含まれやすいので見ない。
    接続情報: 入力 = merged['initialization']['items'] / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    items = (merged.get("initialization") or {}).get("items") or []
    vals = [str(i.get("initial_value") or "").strip() for i in items]
    # WHY(2026-10-14 run12): j の出典の行範囲が i の出典の行範囲の中に収まるときは、段落の中の強調（<strong> 等）のような入れ子で、
    #      i の文言に j の文言が入るのは画面に出る文のとおり。範囲の外の文言まで取った重複（round12 の IT61・IT62）だけを止める。
    sl_items = (merged.get("screen_layout") or {}).get("items") or []

    def _span(k: int):
        """initialization.items[k] と同じ位置・同じ名前の画面項目の、最初の TSX/JSX の出典 (file, 開始行, 終了行)。無ければ None。"""
        if k >= len(sl_items) or len(sl_items) != len(items):
            return None
        if str(sl_items[k].get("item_name") or "").strip() != str(items[k].get("item_name") or "").strip():
            return None
        for s in _tsx_src_of(sl_items[k]):
            ls, le = s.get("line_start"), s.get("line_end")
            if isinstance(ls, int) and isinstance(le, int):
                return (str(s.get("file") or ""), ls, le)
        return None

    for a, ia in enumerate(items):
        va = vals[a]
        for b, ib in enumerate(items):
            vb = vals[b]
            if a == b or len(vb) < 12 or vb == "Blank" or len(vb) >= len(va) or vb not in va:
                continue
            sa, sb = _span(a), _span(b)
            if sa and sb and sa[0] == sb[0] and sa[1] <= sb[1] and sb[2] <= sa[2]:
                continue
            issues.append(("ERROR", f"initialization.items[{ia.get('no') or '?'}]({str(ia.get('item_name') or '').strip()}): 初期値に項目 {ib.get('no') or '?'}({str(ib.get('item_name') or '').strip()}) の初期値が含まれる（出典の範囲が兄弟の項目の文言まで取っている）→ 差し戻し先 S1（出典の行範囲を、その項目の文言だけに直す）"))
    return issues


def _load_react_drafts() -> list:
    """機能の react 下書き（<画面>_react_draft.json）を target/intermediate/ から全部読む。読めないファイルは飛ばす。
    WHY: merge_partials.load_react_drafts と同じ集まりを見て、初期表示の判定の事実を揃える（merge_partials は argv を読むため import しない）。"""
    drafts = []
    for p in sorted(INTERMEDIATE_DIR.glob("*_react_draft.json")):
        try:
            drafts.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return drafts


def check_event_action(merged: dict) -> list:
    """B16 (round9): events[].action が空の行を ERROR で検出する。

    WHY: 動作語（押下・選択など）が無いと、トリガーの文が「どの操作で動くか」を業務担当者に伝えられない。
         convert_events_location_action.py が取れなかった行を後段で止めるための検査。
    接続情報: 入力 = merged['events'] / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    for e in merged.get("events") or []:
        if not str(e.get("action") or "").strip():
            issues.append(("ERROR", f"events[no={e.get('no') or '?'}].action が空（動作語を入れる。取れなければ react 下書きの動作語で補う）"))
    return issues


def check_diagram_ids(merged: dict) -> list:
    """B16 (round9): 構成図（diagrams.screen_structure）のラベル先頭 ID が、画面のエリア番号と一致するか検査する。

    判定ルール: ラベルの先頭が「英字+数字」の ID で、screen_layout.areas[].area_no の集合に無い → ERROR。
                areas が空なら比べる相手が無いので検査しない。
    WHY: 構成図だけ旧番号（A1）のまま出ると、画面一覧の AR 番号と結べず、同じ画面の番号が2通りになるため。
    接続情報: 入力 = merged['diagrams']['screen_structure'] と merged['screen_layout']['areas'] / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    areas = (merged.get("screen_layout") or {}).get("areas") or []
    area_nos = {str(a.get("area_no")) for a in areas if a.get("area_no")}
    if not area_nos:
        return issues
    labels = []

    def walk(node):
        # ラベルは入れ子（spec.structure[].children[]）のどこにあっても拾う
        if isinstance(node, dict):
            if isinstance(node.get("label"), str):
                labels.append(node["label"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    # WHY: 中間 JSON の diagrams は2形がある（dict の {id: spec} と、id を持つ要素の list）。両方から screen_structure を取る
    diagrams = merged.get("diagrams") or {}
    if isinstance(diagrams, dict):
        walk(diagrams.get("screen_structure") or {})
    else:
        walk([d for d in diagrams if isinstance(d, dict) and d.get("id") == "screen_structure"])
    for lab in labels:
        m = re.match(r"^([A-Za-z]+\d+)(?=\s|$)", lab.strip())
        if m and m.group(1) not in area_nos:
            issues.append(("ERROR", f"diagrams.screen_structure のラベル「{lab}」の先頭 ID {m.group(1)} が screen_layout.areas[].area_no に無い（AR 形の番号に直す）"))
    return issues


def check_common_logic_callers(merged: dict) -> list:
    """B16 (round9): common_logic[].used_by_processes_ids が空の行を ERROR で検出する。

    WHY: 呼出元が空だと docx の共通ロジックの「呼出元」が「（該当なし）」になり、どこから呼ばれるか分からない。
         fill_common_logic_callers.py が結べなかった行を後段で止めるための検査。
    接続情報: 入力 = merged['common_logic'] / 出力 = [(level, message), ...] / 呼出元 = main()
    """
    issues = []
    for cl in merged.get("common_logic") or []:
        if not (cl.get("used_by_processes_ids") or []):
            no = cl.get("no") or cl.get("logic_id") or "?"
            issues.append(("ERROR", f"common_logic[{no}]({cl.get('name') or ''}): used_by_processes_ids が空（呼出元の FR・BE を結ぶ）"))
    issues.extend(_check_cl_covers_be_callers(merged))
    return issues


def _check_cl_covers_be_callers(merged: dict) -> list:
    """run18 差2: サーバー・データベースで動く CL の呼出元の FR は、その CL を使う BE の呼出元の FR を全部含む(CL の呼出元の FR ⊇ BE の呼出元の FR)。

    WHY: DB の共通ロジック(BE が使う SQL 条件)の呼出元が S1 の used_in の FR だけだと、同じ BE を呼ぶ他の FR(初期表示・店舗の変更・表示)が抜け、
         BE の called_by_front と JSON の中で食い違う(run18 の CL1〜CL3 は FR1・FR3・FR6 が抜けた)。BE の呼出元は backend_processes[].called_by_front と、
         FR の steps の backend_call の両方から取る。差し戻し先は S1(呼出元の根拠は used_in)。fill_common_logic_callers.py が機械で足すので、
         ここで出るのは、足す前の JSON か、BE が結ばれていない CL。
    接続情報: 入力 = merged['common_logic'] / ['backend_processes'] / ['front_processes'] / 出力 = [(level, message), ...] / 呼出元 = check_common_logic_callers"""
    callers = {}
    for bp in merged.get("backend_processes") or []:
        bid = bp.get("process_id")
        if bid:
            callers.setdefault(bid, set()).update(f for f in (bp.get("called_by_front") or []) if isinstance(f, str))
    for fp in merged.get("front_processes") or []:
        fid = fp.get("process_id") or fp.get("no")
        for s in fp.get("steps") or []:
            bid = (s.get("backend_call") or {}).get("backend_id") if isinstance(s, dict) else None
            if fid and bid:
                callers.setdefault(bid, set()).add(fid)
    out = []
    for cl in merged.get("common_logic") or []:
        ro = cl.get("runs_on") or cl.get("runtime_location") or ""  # fallback-ok: 動作場所が無い CL は対象外(fill_common_logic_callers と同じ判定)
        if "サーバー" not in ro and "データベース" not in ro:
            continue
        ids = set(cl.get("used_by_processes_ids") or [])
        for bid in sorted(i for i in ids if i in callers):
            lack = sorted(callers[bid] - ids)
            if lack:
                no = cl.get("no") or cl.get("logic_id") or "?"
                out.append(("ERROR", f"common_logic[{no}]({cl.get('name') or ''}): 呼出元に {'・'.join(lack)} が無い"
                                     f"(使う {bid} の呼出元の FR。CL の呼出元の FR は BE の呼出元の FR を全部含む) → 差し戻し先 S1"))
    return out


def _selftest() -> int:
    """B16 の4検査の最小自己検査。ERROR が出る形と出ない形を1つずつ置く。
    WHY: 実データ（run8 の複製）が無くても、検査の判定そのものが壊れていないことを確かめるため。"""
    sl_ok = {"areas": [{"area_no": "AR1"}],
             "items": [{"item_name": "店舗", "item_type": "Select"},
                       {"item_name": "注記", "item_type": "Label", "example": "※ 注記文"}]}
    good = {
        "initialization": {"items": [
            {"no": 1, "item_name": "店舗", "active": "活性", "visible": "表示", "initial_value": "- 全店 -"},
            {"no": 2, "item_name": "注記", "active": "非活性", "visible": "表示", "initial_value": "※ 注記文"},
        ]},
        "screen_layout": sl_ok,
        "events": [{"no": "EV1", "action": "押下"}],
        "diagrams": {"screen_structure": {"structure": [{"label": "POS会計レポート画面", "children": [{"label": "AR1 ヘッダ"}]}]}},
        "common_logic": [{"no": "CL1", "used_by_processes_ids": ["FR1"]}],
    }
    checks = (check_initialization_items, check_event_action, check_diagram_ids, check_common_logic_callers)

    def all_issues(d):
        return [i for c in checks for i in c(d)]

    if all_issues(good) != []:
        print(f"[selftest NG] 正しい形で ERROR が出た: {all_issues(good)}")
        return 1
    bad_init = {"initialization": {"items": [
        {"no": 1, "item_name": "店舗", "active": "活性", "visible": "表示", "initial_value": "Blank"},
        {"no": 2, "item_name": "注記", "active": "活性", "visible": "表示", "initial_value": "Blank"},
    ]}, "screen_layout": sl_ok}
    # 注記は文字列リテラル「注記」の要素に名前で結ばれ、初期表示で見える。初期値が Blank なので固定文の ERROR
    lit = [{"jsx_elements": [{"text": "注記", "visible_chain": []}]}]
    msgs = [m for _, m in check_initialization_items(bad_init, lit)]
    if not (any("一色" in m for m in msgs) and any("固定文" in m for m in msgs)):
        print(f"[selftest NG] 初期処理の一色・固定文の ERROR が出ない: {msgs}")
        return 1
    # 固定文でも初期表示で見えないもの（読込中など）の Blank は正しい。見える固定文の値が入っていれば ERROR は出ない
    hidden = [{"jsx_elements": [{"text": "注記", "visible_chain": [{"cond": "loading", "kind": "ternary_then"}]}],
               "state_initials": [{"state": "loading", "kind": "literal", "initial": "false"}]}]
    if any("固定文" in m for _, m in check_initialization_items(bad_init, hidden)):
        print("[selftest NG] 初期表示で見えない固定文の Blank を ERROR にした")
        return 1
    if check_initialization_items(good, lit):
        print("[selftest NG] 見える固定文に文言が入っているのに ERROR が出た")
        return 1
    # 例が文字列（{…} なし）の Label でも、下書きの文字列リテラルに結ばれないもの（総売上など）は固定文にしない
    calc = {"initialization": {"items": [{"no": 1, "item_name": "総売上", "active": "非活性", "visible": "表示", "initial_value": "Blank"}]},
            "screen_layout": {"items": [{"item_name": "総売上", "item_type": "Label", "example": "1,250,000 円"}]}}
    if check_initialization_items(calc, lit):
        print("[selftest NG] 実行時の値の Label を固定文にした")
        return 1
    if not any("一致しない" in m for _, m in check_initialization_items({"initialization": {"items": good["initialization"]["items"][:1]}, "screen_layout": sl_ok})):
        print("[selftest NG] 初期処理の件数の不一致が出ない")
        return 1
    # 2026-10-14: 項目の src(位置)で結ぶ。同じ文言の凡例が初期表示で見えても、項目の位置(隠れた行の印)で判定する
    card_src = {"file": "/x/S.tsx", "line_start": 10, "line_end": 10}
    legend_src = {"file": "/x/S.tsx", "line_start": 50, "line_end": 50}
    lit_pos = [{"jsx_elements": [
        {"text": "空", "visible_chain": [{"cond": "rows.length > 1", "kind": "and"}], "src": [card_src]},
        {"text": "空", "visible_chain": [], "src": [legend_src]}]}]
    pos_init = {"initialization": {"items": [{"no": 1, "item_name": "空", "active": "活性", "visible": "非表示", "initial_value": "Blank"}]},
                "screen_layout": {"items": [{"item_name": "空", "item_type": "Label", "example": "空", "src": [card_src]}]}}
    if any("固定文" in m for _, m in check_initialization_items(pos_init, lit_pos)):
        print("[selftest NG] 項目の位置で隠れた印を、別の位置の凡例で固定文の ERROR にした")
        return 1
    # 2026-10-14 run12: 同じ名前の2項目(隠れた行の印「空」・見える凡例「空」)。隠れた方の Blank は正しく、凡例の Blank だけを ERROR にする
    two_sl = {"items": [{"item_name": "空", "item_type": "Label", "example": "空", "src": [card_src]},
                        {"item_name": "空", "item_type": "Label", "example": "空", "src": [legend_src]}]}
    two_ok = {"initialization": {"items": [
        {"no": 1, "item_name": "空", "active": "活性", "visible": "非表示", "initial_value": "Blank"},
        {"no": 2, "item_name": "空", "active": "活性", "visible": "表示", "initial_value": "空"}]}, "screen_layout": two_sl}
    if any("固定文" in m for _, m in check_initialization_items(two_ok, lit_pos)):
        print("[selftest NG] 同じ名前の凡例が見えるだけで、隠れた印の Blank を固定文の ERROR にした")
        return 1
    two_bad = {"initialization": {"items": [
        {"no": 1, "item_name": "空", "active": "活性", "visible": "非表示", "initial_value": "Blank"},
        {"no": 2, "item_name": "空", "active": "活性", "visible": "表示", "initial_value": "Blank"}]}, "screen_layout": two_sl}
    two_msgs = [m for _, m in check_initialization_items(two_bad, lit_pos) if "固定文" in m]
    if not (len(two_msgs) == 1 and "items[2]" in two_msgs[0] and "→ 差し戻し先 S1" in two_msgs[0]):
        print(f"[selftest NG] 見える凡例の Blank だけを固定文の ERROR にしない: {two_msgs}")
        return 1
    # P20: 名前が一致しないボタンの文言も、項目の出典の行が重なれば固定文として結ぶ（出典の行が重ならなければ結ばない）
    btn_src = {"file": "/x/S.tsx", "line_start": 20, "line_end": 20}
    lit_btn = [{"text": "保存する", "visible_chain": [], "src": [btn_src]}]
    btn_item = {"initialization": {"items": [{"no": 1, "item_name": "保存ボタン", "active": "活性", "visible": "表示", "initial_value": "Blank"}]},
                "screen_layout": {"items": [{"item_name": "保存ボタン", "item_type": "Button", "src": [btn_src]}]}}
    if not any("固定文" in m for _, m in check_initialization_items(btn_item, [{"jsx_elements": lit_btn}])):
        print("[selftest NG] 出典の行が重なるボタンの文言の Blank を固定文の ERROR にしない")
        return 1
    btn_far = {"initialization": btn_item["initialization"],
               "screen_layout": {"items": [{"item_name": "保存ボタン", "item_type": "Button", "src": [{"file": "/x/S.tsx", "line_start": 30, "line_end": 30}]}]}}
    if any("固定文" in m for _, m in check_initialization_items(btn_far, [{"jsx_elements": lit_btn}])):
        print("[selftest NG] 出典の行が重ならないボタンの文言を固定文として結んだ")
        return 1
    # P20: 下書きのボタン（tag button・label が文言）も、出典の行が重なれば固定文として結ぶ
    btn_el = [{"tag": "button", "label": "保存する", "visible_chain": [], "src": [btn_src]}]
    if not any("固定文" in m for _, m in check_initialization_items(btn_item, [{"jsx_elements": btn_el}])):
        print("[selftest NG] 下書きのボタン(label)の Blank を固定文の ERROR にしない")
        return 1
    # src が無い項目は従来どおり名前で結ぶ(凡例の「空」が初期表示で見える → ERROR)
    no_src = {"initialization": pos_init["initialization"],
              "screen_layout": {"items": [{"item_name": "空", "item_type": "Label", "example": "空"}]}}
    if not any("固定文" in m for _, m in check_initialization_items(no_src, lit_pos)):
        print("[selftest NG] src の無い項目の名前での固定文の判定が外れた")
        return 1
    # 応答の形の欄: JSON の形({success: true, data: {rows}})は穴にしない。応答の形の中の素の {名前} は穴のまま
    shape = {"api_spec": [{"response_pattern": "成功時 {success: true, data: {rows}}"}],
             "backend_processes": [{"response_spec": {"pattern": "成功時 {success:true,data:{rows}}"}}]}
    if any("未展開の穴" in m for _, m in check_intermediate_strict(shape)):
        print("[selftest NG] 応答の形の JSON を未展開の穴にした")
        return 1
    bare = {"api_spec": [{"response_pattern": "成功時 {件数} 件を返す"}]}
    if not any("未展開の穴" in m for _, m in check_intermediate_strict(bare)):
        print("[selftest NG] 応答の形の中の素の {名前} の穴を見逃した")
        return 1
    # run15 差6: messages[].message の可変部 {status} は未展開の穴にしない。messages_backend の {名前}・他の欄の素の {名前} は穴のまま
    msg_var = {"messages": [{"message_id": "MSE00005", "message": "大きい応答の受け取りに失敗しました（HTTP {status}）。もう一度操作してください"},
                            {"message_id": "MSE00007", "message": "{code}: {message}"}]}
    if any("未展開の穴" in m for _, m in check_intermediate_strict(msg_var)):
        print("[selftest NG] messages の可変部 {名前} を未展開の穴にした")
        return 1
    msg_back = {"messages_backend": [{"message_id": "MSE00001", "message": "{key} は正の整数で指定してください"}],
                "api_spec": [{"description": "{件数} 件を返す"}]}
    if sum("未展開の穴" in m for _, m in check_intermediate_strict(msg_back)) != 2:
        print("[selftest NG] messages_backend・他の欄の {名前} を穴として見逃した")
        return 1
    if not check_event_action({"events": [{"no": "EV1", "action": ""}]}):
        print("[selftest NG] 動作語の空が出ない")
        return 1
    if not check_diagram_ids({"screen_layout": {"areas": [{"area_no": "AR1"}]},
                              "diagrams": {"screen_structure": {"children": [{"label": "A1 ヘッダ"}]}}}):
        print("[selftest NG] 旧番号の構成図ラベルが出ない")
        return 1
    if not check_common_logic_callers({"common_logic": [{"no": "CL1", "used_by_processes_ids": []}]}):
        print("[selftest NG] 呼出元の空が出ない")
        return 1
    # run18 差2: DB の CL の呼出元の FR ⊇ CL を使う BE の呼出元の FR。足りなければ ERROR(S1)、揃えば出ない。ブラウザの CL・BE を含まない CL は見ない
    cl_be = {"backend_processes": [{"process_id": "BE2", "called_by_front": ["FR1", "FR2"]}],
             "front_processes": [{"process_id": "FR3", "steps": [{"kind": "backend_call", "backend_call": {"backend_id": "BE2"}}]}],
             "common_logic": [{"no": "CL1", "name": "条件", "runs_on": "データベース", "used_by_processes_ids": ["FR2", "BE2"]}]}
    got_cl = [m for lv, m in check_common_logic_callers(cl_be) if lv == "ERROR"]
    if len(got_cl) != 1 or "FR1・FR3" not in got_cl[0] or "S1" not in got_cl[0]:
        print(f"[selftest NG] CL の呼出元が BE の呼出元の FR を欠く ERROR が出ない: {got_cl}")
        return 1
    cl_be["common_logic"][0]["used_by_processes_ids"] = ["FR1", "FR2", "FR3", "BE2"]
    cl_be["common_logic"].append({"no": "CL2", "runs_on": "ブラウザ", "used_by_processes_ids": ["FR2", "BE2"]})
    cl_be["common_logic"].append({"no": "CL3", "runs_on": "データベース", "used_by_processes_ids": ["FR2"]})
    if check_common_logic_callers(cl_be):
        print(f"[selftest NG] 揃った呼出元・ブラウザの CL・BE を結ばない CL で ERROR が出た: {check_common_logic_callers(cl_be)}")
        return 1
    # B16 差異6 (2026-10-14): 初期表示の活性・表示を出典の TSX の行から再評価する。
    # 記載が評価と違えば ERROR、評価が決まらない・TSX が無い項目は見ない（INFO のみ）
    with tempfile.TemporaryDirectory() as tmp:
        tsx = Path(tmp) / "init.tsx"
        tsx.write_text("export default function P() {\n  return (\n    <div>\n"
                       "      {loading && <p>読込中</p>}\n"
                       "      <button disabled={loading}>保存</button>\n"
                       "    </div>\n  );\n}\n", encoding="utf-8")
        drafts_loading = [{"state_initials": [{"state": "loading", "kind": "literal", "initial": "false"}]}]
        sl_src = {"items": [
            {"item_name": "読込中", "src": [{"file": str(tsx), "line_start": 4, "line_end": 4}]},
            {"item_name": "保存", "src": [{"file": str(tsx), "line_start": 5, "line_end": 5}]},
        ]}

        def merged_of(vis1: str, act2: str) -> dict:
            return {"screen_layout": sl_src, "initialization": {"items": [
                {"no": 1, "item_name": "読込中", "active": "活性", "visible": vis1},
                {"no": 2, "item_name": "保存", "active": act2, "visible": "表示"},
            ]}}

        if check_initialization_eval(merged_of("非表示", "活性"), {}, drafts_loading):
            print("[selftest NG] 評価と記載が一致しているのに ERROR が出た")
            return 1
        bad = [m for lvl, m in check_initialization_eval(merged_of("表示", "非活性"), {}, drafts_loading) if lvl == "ERROR"]
        if not (len(bad) == 2 and any("表示の記載" in m for m in bad) and any("活性の記載" in m for m in bad)):
            print(f"[selftest NG] 表示・活性の食い違いが出ない: {bad}")
            return 1
        missing = {"screen_layout": {"items": [{"item_name": "読込中", "src": [{"file": "/nonexistent/x.tsx", "line_start": 1, "line_end": 1}]}]},
                   "initialization": {"items": [{"no": 1, "item_name": "読込中", "active": "活性", "visible": "表示"}]}}
        if [lvl for lvl, _ in check_initialization_eval(missing, {}, [])] != ["INFO"]:
            print("[selftest NG] TSX が読めないときに INFO で飛ばさない")
            return 1
        # WHY(round11 U2): 出典の行範囲が JSX の要素も配列の使用箇所も指さない（関数の宣言行）項目は、仮埋めで通さず ERROR（S1 へ差し戻し）
        unlinked = {"screen_layout": {"items": [{"item_name": "読込中", "src": [{"file": str(tsx), "line_start": 1, "line_end": 1}]}]},
                    "initialization": {"items": [{"no": 1, "item_name": "読込中", "active": "活性", "visible": "表示"}]}}
        if [lvl for lvl, _ in check_initialization_eval(unlinked, {}, drafts_loading)] != ["ERROR"]:
            print("[selftest NG] 出典の位置で結べない項目を ERROR にしない（仮埋めのまま通した）")
            return 1
    # B16 round12 V2: TSX 画面で出典(TSX/JSX)の無い項目は位置で結べないので、読み飛ばさず ERROR（差し戻し先 S1）。
    # TSX の出典が1つも無い画面（HTML・サンプル）は従来どおり読み飛ばす
    with tempfile.TemporaryDirectory() as tmp:
        tsx2 = Path(tmp) / "v2.tsx"
        tsx2.write_text("export default function P() {\n  return (\n    <div>\n"
                        "      {loading && <p>読込中</p>}\n"
                        "    </div>\n  );\n}\n", encoding="utf-8")
        drafts_v2 = [{"state_initials": [{"state": "loading", "kind": "literal", "initial": "false"}]}]
        mixed = {"screen_layout": {"items": [
            {"no": "IT1", "item_name": "読込中", "src": [{"file": str(tsx2), "line_start": 4, "line_end": 4}]},
            {"no": "IT2", "item_name": "総売上", "item_type": "Label"}]},
            "initialization": {"items": [
                {"no": 1, "item_name": "読込中", "active": "活性", "visible": "非表示"},
                {"no": 2, "item_name": "総売上", "active": "活性", "visible": "表示"}]}}
        v2_errs = [m for lvl, m in check_initialization_eval(mixed, {}, drafts_v2) if lvl == "ERROR"]
        if not (len(v2_errs) == 1 and "総売上" in v2_errs[0] and "出典(src)が無く" in v2_errs[0]):
            print(f"[selftest NG] TSX 画面で出典の無い項目を ERROR にしない: {v2_errs}")
            return 1
        tsx_free = {"screen_layout": {"items": [{"item_name": "総売上", "item_type": "Label"}]},
                    "initialization": {"items": [{"no": 1, "item_name": "総売上", "active": "活性", "visible": "表示"}]}}
        if check_initialization_eval(tsx_free, {}, drafts_v2):
            print("[selftest NG] TSX の出典が1つも無い画面（HTML・サンプル）を ERROR にした")
            return 1
    # B16 round12 V2 (新11): 初期値に別の項目の文言が含まれるものは ERROR（含む側だけ）。同じ文言の2項目と、12字未満の語は見ない
    dup_long = "この期間・店舗のPOS会計データはありません。0店だけなので表示できません。"
    dup_bad = {"initialization": {"items": [
        {"no": 61, "item_name": "注記", "initial_value": dup_long},
        {"no": 62, "item_name": "説明", "initial_value": "0店だけなので表示できません。"}]}}
    dup_errs = [m for lvl, m in check_initialization_dup(dup_bad) if lvl == "ERROR"]
    if not (len(dup_errs) == 1 and "注記" in dup_errs[0] and "説明" in dup_errs[0]):
        print(f"[selftest NG] 初期値に兄弟の文言が含まれるのを ERROR にしない: {dup_errs}")
        return 1
    dup_same = {"initialization": {"items": [
        {"no": 1, "item_name": "注記A", "initial_value": dup_long},
        {"no": 2, "item_name": "注記B", "initial_value": dup_long}]}}
    dup_short = {"initialization": {"items": [
        {"no": 1, "item_name": "注記", "initial_value": dup_long},
        {"no": 2, "item_name": "語", "initial_value": "表示できません。"}]}}
    if check_initialization_dup(dup_same) or check_initialization_dup(dup_short):
        print("[selftest NG] 同じ文言の2項目、または12字未満の語を重複として ERROR にした")
        return 1
    # 2026-10-14 run12: 段落の中の強調(出典の範囲が段落の範囲の中に収まる入れ子)は、画面に出る文のとおりなので ERROR にしない。
    # 範囲の外(次の行以降)の文言まで取った重複は ERROR のまま
    em = "強調して書く大事な文言そのもの"
    para = f"前の文 {em} 後ろの文。"
    nest_sl = [{"item_name": "注記", "src": [{"file": "/x/S.tsx", "line_start": 10, "line_end": 14}]},
               {"item_name": "強調", "src": [{"file": "/x/S.tsx", "line_start": 12, "line_end": 12}]}]
    nest_in = [{"no": 1, "item_name": "注記", "initial_value": para}, {"no": 2, "item_name": "強調", "initial_value": em}]
    if check_initialization_dup({"initialization": {"items": nest_in}, "screen_layout": {"items": nest_sl}}):
        print("[selftest NG] 段落の中の強調(出典が段落の範囲の中)を重複として ERROR にした")
        return 1
    out_sl = [{"item_name": "注記", "src": [{"file": "/x/S.tsx", "line_start": 10, "line_end": 11}]},
              {"item_name": "強調", "src": [{"file": "/x/S.tsx", "line_start": 12, "line_end": 12}]}]
    if len([1 for lvl, _ in check_initialization_dup({"initialization": {"items": nest_in}, "screen_layout": {"items": out_sl}}) if lvl == "ERROR"]) != 1:
        print("[selftest NG] 出典の範囲の外の文言まで取った重複を ERROR にしない")
        return 1
    # W2(2026-10-14 run12): 活性・表示の不一致の ERROR は差し戻し先 S1 を文末に持ち、run_units.parse_sendbacks で S1 に入る。
    # 下書きの display_rows で同名の行が画面項目の数以上にあれば、同名の項目は WARN にしない（小計・合計の反復）
    from run_units import parse_sendbacks
    with tempfile.TemporaryDirectory() as tmp:
        tsx_w2 = Path(tmp) / "w2.tsx"
        tsx_w2.write_text("export default function P() {\n  return (\n    <div>\n"
                          "      {loading && <p>読込中</p>}\n"
                          "      <button disabled={loading}>保存</button>\n"
                          "    </div>\n  );\n}\n", encoding="utf-8")
        drafts_w2 = [{"state_initials": [{"state": "loading", "kind": "literal", "initial": "true"}]}]
        mis_w2 = {"screen_layout": {"items": [
            {"item_name": "保存", "src": [{"file": str(tsx_w2), "line_start": 5, "line_end": 5}]}]},
            "initialization": {"items": [{"no": 2, "item_name": "保存", "active": "活性", "visible": "表示"}]}}
        mis_errs = [m for lvl, m in check_initialization_eval(mis_w2, {}, drafts_w2) if lvl == "ERROR"]
        if not (len(mis_errs) == 1 and "→ 差し戻し先 S1" in mis_errs[0] and parse_sendbacks(mis_errs[0]) == {"S1": [mis_errs[0]]}):
            print(f"[selftest NG] 活性の不一致の ERROR に差し戻し先 S1 が付かない: {mis_errs}")
            return 1
    dup3 = {"screen_layout": {"items": [{"no": i + 1, "item_name": "小計", "area_ref": "AR1"} for i in range(3)]}}
    dup4 = {"screen_layout": {"items": [{"no": i + 1, "item_name": "小計", "area_ref": "AR1"} for i in range(4)]}}
    w2_drafts = [{"display_rows": [{"specs": [{"label": "小計"}, {"label": "小計"}]}, {"specs": [{"label": "小計"}]}]}]
    if check_item_name_duplication(dup3, w2_drafts):
        print("[selftest NG] 下書きの同名の行の数までの画面項目の同名を WARN にした")
        return 1
    if len(check_item_name_duplication(dup4, w2_drafts)) != 1:
        print("[selftest NG] 下書きの同名の行の数を上回る画面項目の同名を WARN にしない")
        return 1
    if len(check_item_name_duplication(dup3)) != 1:
        print("[selftest NG] 下書きが無いときに同名の WARN を出さない")
        return 1
    print("[OK] selftest: B16 の初期処理・動作語・構成図の ID・共通ロジックの呼出元・初期表示の活性と表示(出典から再評価)・出典の無い項目・初期値の重複")
    return 0


def main():
    # 目的: 引数で指定された機能名の中間 JSON を検証する。引数なしなら環境変数 FEATURE_NAME、
    #       どちらも無ければ _config_loader 側が「feature_name 未指定」で止める（既定の機能名は持たない）。
    # 意味合い: どの画面・どの案件でも再利用可能にする（汎用性確保）。機能名はここで1回だけ解決し、
    #           設定の読み込みにも同じ値を明示で渡す（_config_loader のアクセサは sys.argv を見ないため）。
    # 接続情報: sys.argv[1] または FEATURE_NAME = 機能名 / 中間 JSON パス = target/intermediate/{機能名}.json
    #           project_config = target/intermediate/{機能名}_project_config.json（無ければ {}）
    feature = get_feature_name_from_argv()
    target = INTERMEDIATE_DIR / f"{feature}.json"
    project_config = load_project_config(feature)
    # 2026-10-05 汎用化(3): 案件固有の判定の上書き（旧イベントコードの書式・参照 ID の接頭辞・
    # 業界用語・静的な言い回し）。project_config に無ければ空で、その分の判定は共通のものだけになる。
    # 接続情報: check_legacy_event_code_residue / check_screen_item_remarks_dynamic_behavior /
    #           check_description_granularity へ渡す。
    verify_overrides = dict(project_config.get("verify_overrides") or {})
    # ソースの識別子(英数字と _ の語)。check_description_granularity が、POS など英大文字だけの略語を
    # 物理名と誤検出しないために使う。ソースは screen_config の source_files と project_root + backend_files。
    try:
        sc = load_screen_config(feature)
    except FileNotFoundError:
        sc = {}
    srcs = list(sc.get("source_files") or []) + [str(Path(sc.get("project_root") or ".") / f) for f in sc.get("backend_files") or []]
    # コメント・文字列・日本語を含む JSX の文は業務の文なので、識別子から外す(「POS会計」「新POS の…」の POS を識別子と取り違えない)。
    # ponytail: 正規表現での粗い除去(言語ごとの字句解析はしない)。取りこぼすと略語が物理名として残るだけで、ERROR 側へ倒れる
    prose = re.compile(r'/\*.*?\*/|"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|//[^\n]*|#[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`[^`]*`'
                       r'|[^<>{}\n]*[^\x00-\x7f][^<>{}\n]*', re.S)
    idents = set()
    for s in srcs:
        try:
            idents |= set(re.findall(r"[A-Za-z0-9_]+", prose.sub(" ", Path(s).read_text(encoding="utf-8"))))
        except OSError:
            pass
    verify_overrides["source_identifiers"] = sorted(idents)
    # 禁止語は小文字化する: check_forbidden_words の scan が小文字化した本文と突き合わせるため
    # （大文字混じりのキーをそのまま使うと一致しなくなる）。
    forbidden_words = {w.lower() for w in get_forbidden_terms_map(feature)}
    if not target.exists():
        print(f"FATAL: 中間JSON {target} が存在しない", file=sys.stderr)
        sys.exit(1)
    with target.open(encoding="utf-8") as f:
        merged = json.load(f)

    all_issues = []
    # v55.0 §20 バージョン管理規約: 中間 JSON _meta.format_version と本スクリプトの期待値の整合確認 (WARN レベル)
    all_issues.extend(check_format_version_consistency(merged))
    all_issues.extend(check_structure(merged, project_config))
    all_issues.extend(check_event_code_consistency(merged))
    # v50: legacy event_code 形式 (案件で定義した旧形式, EV00) の残存検出
    all_issues.extend(check_legacy_event_code_residue(merged, verify_overrides))
    all_issues.extend(check_db_op_refs(merged))
    all_issues.extend(check_logical_names(merged))
    all_issues.extend(check_message_refs(merged))
    all_issues.extend(check_forbidden_words(merged, forbidden_words))
    all_issues.extend(check_date_format(merged))
    # v16: process_flows[] / response_to_screen_mapping 整合性
    all_issues.extend(check_process_flows(merged))
    all_issues.extend(check_response_to_screen_mapping(merged))
    # v18: trigger_groups / front_processes / backend_processes 整合性
    all_issues.extend(check_v18_schema(merged))
    # v20: 画面用語 cross-reference 整合性（screen_layout 権威語彙との厳密一致）
    all_issues.extend(check_screen_terminology(merged))
    # v49: 画面項目 remarks に動的振る舞いが混入していないかチェック（§4.3 vs §5 の責務分離原則）
    all_issues.extend(check_screen_item_remarks_dynamic_behavior(merged, verify_overrides))
    # v50.5: 中間 JSON の中身が空テンプレ（key だけ存在）でないかを検証（再発防止）
    # 意味合い: 中身の無い docx が出る現象（backend_processes 10 件全部スケルトンのみ +
    #          validations/calculations/common_logic/messages/parameters のフィールド大半空）の再発防止。
    #          Agent 出力が「形だけ」で通ってしまう旧 verify の致命的盲点を埋める。
    all_issues.extend(check_backend_process_substance(merged))
    all_issues.extend(check_section_substance(merged))
    # 2026-10-12: validations/messages の0件・dataset の括り表記・event_processes の空 details
    all_issues.extend(check_coverage_and_details(merged))
    # v50.5 phase 2.5.1: 「未割当」trigger 検出（screen_operation kind の area_ref 必須）
    all_issues.extend(check_screen_operation_trigger_area_ref(merged))
    # v51 §19 記述粒度ルール (1 文 1 概念原則): 説明系フィールドの「。」連結マルチステートメント検出
    # 意味合い: 共通ビジネスロジックなどの説明が 1 行に複数のロジックを詰め込んだ状態（マルチステートメント）になるのを防ぐ、
    #           への検証側対応。description_details[] 配列があれば OK 判定 (詳細は配列で 1 要素 1 文)。
    all_issues.extend(check_description_granularity(merged, verify_overrides))
    # A8（2026-07-18）: 同一エリア内 item_name 重複 / overview 文言使い回し検出（可読性の底上げ）
    # W2(2026-10-14): 下書きの描く行の数を渡し、同名の反復を重複として止めない
    all_issues.extend(check_item_name_duplication(merged, _load_react_drafts()))
    all_issues.extend(check_overview_duplication(merged))

    # 2026-10-12: 論理・物理テーブル名の仮表記 / 1.1・1.2 のファイル説明・種別 / messages.code の ID 形式
    all_issues.extend(check_table_name_placeholders(merged))
    all_issues.extend(check_file_descriptions(merged))
    all_issues.extend(check_message_code_format(merged))
    # 2026-10-13: メッセージの種別（severity）と ID 接頭辞の食い違い・情報なのに警告文言
    all_issues.extend(check_message_severity_prefix(merged))

    # 2026-10-13: 中身の穴（名前だけの共通ロジック・物理名残存・未展開の穴・メッセージ ID・DB 操作の英語キー空欄）
    all_issues.extend(check_intermediate_strict(merged))
    # 2026-10-13 案 A4: 出典(src)の無い記述は WARN、対象外のファイル・作り話の行番号は ERROR
    all_issues.extend(check_src(merged, sc))
    # B16 (round9): 初期処理の表（件数・一色・固定文）、動作語の空、構成図の ID、共通ロジックの呼出元
    all_issues.extend(check_initialization_items(merged, _load_react_drafts()))
    all_issues.extend(check_event_action(merged))
    all_issues.extend(check_diagram_ids(merged))
    all_issues.extend(check_common_logic_callers(merged))
    # B16 差異6 (2026-10-14): 初期表示の活性・表示を出典の TSX の行から再評価して照合する
    all_issues.extend(check_initialization_eval(merged, sc, _load_react_drafts()))
    # B16 round12 V2: 初期値に別の項目の文言が重複して入るもの（出典の範囲が兄弟の文言まで取った）を止める
    all_issues.extend(check_initialization_dup(merged))

    errors = [i for i in all_issues if i[0] == "ERROR"]
    warns = [i for i in all_issues if i[0] == "WARN"]

    print(f"=== 検証結果: ERROR {len(errors)} 件 / WARN {len(warns)} 件 ===")
    print()
    if errors:
        print("--- ERROR（docx生成前に要修正） ---")
        for lvl, msg in errors[:50]:  # 50件まで表示
            print(f"  [{lvl}] {msg}")
        if len(errors) > 50:
            print(f"  ...他 {len(errors) - 50} 件のERROR省略")
        print()
    if warns:
        print("--- WARN（許容範囲、業務担当者の確認推奨） ---")
        for lvl, msg in warns[:30]:  # 30件まで表示
            print(f"  [{lvl}] {msg}")
        if len(warns) > 30:
            print(f"  ...他 {len(warns) - 30} 件のWARN省略")
        print()

    infos = [i for i in all_issues if i[0] == "INFO"]
    if infos:
        print("--- INFO（検査を飛ばした理由。判定には使わない） ---")
        for lvl, msg in infos:
            print(f"  [{lvl}] {msg}")
        print()

    if not errors and not warns:
        print("整合性 OK。docx 生成に進めます")

    # 終了コード: ERROR があれば 1、なければ 0
    sys.exit(1 if errors else 0)


# WHY: --selftest は機能名や中間 JSON を要らずに検査の判定だけを確かめる入口
if __name__ == "__main__" and "--selftest" in sys.argv:
    sys.exit(_selftest())
if __name__ == "__main__":
    main()
