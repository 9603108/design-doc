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
from pathlib import Path

# 2026-10-05 汎用化: 機能名・中間 JSON の置き場・案件ごとの設定は _config_loader から取る
# （このファイルに案件固有の値や絶対パスを持たないため）。使うのは main()。
from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    get_forbidden_terms_map,
    load_project_config,
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
                issues.append(("WARN", f"db_operations[{op.get('id')}].dataset の logical_column={lc} が logical_names[] に未登録"))
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
                          f"events[{e.get('event_code','?')}].location[{i}]='{elem}' が screen_layout の権威語彙にない"))

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
                              f"trigger_groups[kind={kind}].triggers[no={t.get('trigger_no')}].location[{i}]='{elem}' が screen_layout の権威語彙にない"))

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


def check_item_name_duplication(merged: dict) -> list:
    """A8 新設（2026-07-18）: 同一エリア内での item_name 重複を検出する。
    意味合い: 業務担当者が §4.3 画面項目一覧を読む際、同一エリア内に同名項目が複数あると
              どちらの項目を指しているか区別できず可読性が落ちる（14_チェックリスト.md「孤立処理・記述精度チェック」対応）。
    """
    issues = []
    items = (merged.get("screen_layout", {}) or {}).get("items", []) or []
    by_area = {}
    for it in items:
        name = it.get("item_name")
        if not name:
            continue
        area = it.get("area_ref") or "(未割当)"
        by_area.setdefault(area, {}).setdefault(name, []).append(it.get("no"))
    for area, name_map in by_area.items():
        for name, nos in name_map.items():
            if len(nos) > 1:
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
      - error_patterns 空 → WARN（業務エラー記述なしも設計上あり得るため WARN）
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
        # 4. error_patterns 空 — 業務エラー記述なしも設計上あり得るが WARN として注意喚起
        if not (bp.get("error_patterns") or []):
            issues.append(("WARN", f"backend_processes[{pid}] {name}: error_patterns が空（業務エラー・システムエラーの記述なし）"))
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
            if w in BUSINESS_OK or BUSINESS_REF_PAT.match(w):
                continue
            found.append(f"PascalCase:{w}")
        # SNAKE_CASE / 大文字略語 (2 文字以上)
        for m in _re.finditer(r'(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]+)(?![A-Za-z0-9_])', text):
            w = m.group(1)
            if w in BUSINESS_OK or BUSINESS_REF_PAT.match(w):
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
    if not meta.get("source_html") and not meta.get("source_html_path"):
        issues.append(("ERROR", "meta.source_html / meta.source_html_path のいずれも空（HTML 解析起点が不明）"))
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
    verify_overrides = project_config.get("verify_overrides") or {}
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
    # v50.5 phase 2.5.1: 「未割当」trigger 検出（screen_operation kind の area_ref 必須）
    all_issues.extend(check_screen_operation_trigger_area_ref(merged))
    # v51 §19 記述粒度ルール (1 文 1 概念原則): 説明系フィールドの「。」連結マルチステートメント検出
    # 意味合い: 共通ビジネスロジックなどの説明が 1 行に複数のロジックを詰め込んだ状態（マルチステートメント）になるのを防ぐ、
    #           への検証側対応。description_details[] 配列があれば OK 判定 (詳細は配列で 1 要素 1 文)。
    all_issues.extend(check_description_granularity(merged, verify_overrides))
    # A8（2026-07-18）: 同一エリア内 item_name 重複 / overview 文言使い回し検出（可読性の底上げ）
    all_issues.extend(check_item_name_duplication(merged))
    all_issues.extend(check_overview_duplication(merged))

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

    if not errors and not warns:
        print("整合性 OK。docx 生成に進めます")

    # 終了コード: ERROR があれば 1、なければ 0
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
