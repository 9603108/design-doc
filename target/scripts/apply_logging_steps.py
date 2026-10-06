#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_logging_steps.py — 観測点ログ処理を独立 [L1]-[L8] + サブ [L4a]-[L4f] として定義し、
                          各 F00X / B00X の steps からは呼出のみ記述する（v40 で構造改訂）

【目的】
project_config.json の logging.observation_points を読み、観測点ログ処理を独立した「共通ログ処理」として
中間 JSON に `common_logging_processes[]` を新設。各 F00X / B00X の steps からは
「[LN] 観測点 N 記録 を呼ぶ」の1行（kind='logging_call', call_id='L1' 等）で参照する。

【意味合い】
v39 で各 F00X / B00X の steps に観測点ログ step を直書きしたため、
各処理から [FXX0] をコールする形になっておらず、同じ記述が複数箇所に重複していた点への対応。
ロギング処理は業務処理とは別の独立した責務であり、本来は共通モジュール処理として一度定義し、
各業務処理からは呼出するべき構造（[F00X] [B00X] と並ぶ [L1]-[L8] の独立 ID 体系）。

【接続情報】
- 入力: target/intermediate/{機能名}.json + target/intermediate/{機能名}_project_config.json
- 出力: 同 JSON を上書き
  - 新規セクション: `common_logging_processes[]` (観測点ログ処理 14個 = [L1]-[L8] + [L4a]-[L4f])
  - 各 F00X.steps / B00X.processing に kind='logging_call' step を挿入（call_id 参照）
- 後段: generate_docx.js が §6.3「観測点ログ処理」として表示、各 F00X/B00X の steps では呼出のみ
- 仕様根拠: project_config.json logging / SKILL.md「Phase 5.7 観測点ログ処理」（v40 で改訂）

【設計原則】
- 観測点ログ処理本体は独立した [L1]-[L8] + サブ [L4a]-[L4f] として一度だけ定義（DRY）
- 各 F00X / B00X の steps では「[LN] を呼ぶ」の1行のみ（業務処理と並列）
- 冪等性: 既に kind='logging_call' な step がある場合は重複挿入しない
- project_config.json が空ならスキル汎用性確保のためスキップ
"""
import json
import sys
from pathlib import Path

# v43: ID 体系を id_scheme.json (スキル付属) 経由化、ハードコード接頭辞 "L" を全廃
# 汎用化: sys.argv[1] / FEATURE_NAME 環境変数対応のため get_feature_name_from_argv 経由化
from _config_loader import get_feature_name_from_argv, get_logging_spec, format_id, get_id_prefix

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
INTERMEDIATE_DIR = SKILL_DIR / "target" / "intermediate"


def _build_common_logging_processes(op_map: dict, op_4_subs: list) -> list:
    """観測点ログ処理本体を [L1]-[L8] + サブ [L4a]-[L4f] として構築する。

    意味合い: 各観測点を独立した「共通処理」として一度だけ定義。各 F00X / B00X の steps からは
              [LN] を呼ぶ（kind='logging_call', call_id='L1' 等）形式で参照する。物理実装名
              （trigger_function / module_path）は本処理の内部にのみ記載され、業務処理側 §6.1/§6.2 には
              一切露出しない。
    """
    processes = []
    # 観測点 [1]-[8] 本体
    for op_no in sorted(op_map.keys()):
        op = op_map[op_no]
        # v43: id_scheme.json 経由（旧ハードコード "L{op_no}" 廃止）
        call_id = format_id("logging", op_no)
        label = op.get("label", "")
        layer = op.get("layer", "")
        triggered_when = op.get("triggered_when", "")
        business_meaning = op.get("business_meaning", "")
        required_fields = op.get("required_fields", []) or []
        captures = op.get("captures", []) or []
        processes.append({
            "process_id": call_id,
            "name": f"観測点[{op_no}] {label}記録",
            "area": "観測点ログ処理",
            "layer": layer,
            "overview": f"観測点[{op_no}] {label}の記録を行う共通処理。" + (f"業務的役割: {business_meaning}" if business_meaning else ""),
            "triggered_when": triggered_when,
            "recorded_fields": required_fields + (op.get("optional_fields", []) or []),
            "captures": captures,
            # v50.5: 旧形式 F00X / B00X ハードコードを id_scheme.json 経由で動的化（generate_docx.js getCategoryPrefix と同方針）
            "called_by": f"各 {get_id_prefix('front_process')}X / {get_id_prefix('backend_process')}X の steps から logging_call kind で呼出される",
            "_meta": {
                "trigger_function": op.get("trigger_function", ""),
                "module_path": op.get("module_path", ""),
                "v2_1_route": op.get("v2_1_route", "")
            }
        })

    # 観測点 [4] サブ観測 [L4a]-[L4f]
    sub_letters = ['a', 'b', 'c', 'd', 'e', 'f']
    for i, sub in enumerate(op_4_subs or []):
        if i >= len(sub_letters):
            break
        letter = sub_letters[i]
        # v43: id_scheme.json 経由（旧ハードコード "L4{letter}" 廃止、サブラベル付き ID として親と同じ番号 4 を共有）
        call_id = format_id("logging", 4, sub_label=letter)
        parent_call_id = format_id("logging", 4)  # サブの親 ID（LG4 等）
        sub_label = sub.get("label", "")
        processes.append({
            "process_id": call_id,
            "name": f"観測点[4]-サブ「{sub_label}」記録",
            "area": "観測点ログ処理",
            "layer": "backend",
            "overview": f"観測点[4]のサブ観測「{sub_label}」の記録を行う共通処理（[{parent_call_id}] の配下）。",
            "parent_call_id": parent_call_id,
            "recorded_fields": sub.get("required_fields", []) or [],
            "captures": sub.get("captures", []) or [],
            # v50.5: 旧形式 B00X ハードコードを id_scheme.json 経由で動的化
            "called_by": f"{get_id_prefix('backend_process')}X の processing から、対応する業務処理 step（DB-TX 境界 / SQL実行 / キャッシュ操作 / 監査イベント / 外部サービス呼出 / 認可チェック）の前後で呼出される",
            "_meta": {
                "trigger_function": sub.get("trigger_function", ""),
                "module_path": sub.get("module_path", ""),
                "implementation_status": sub.get("implementation_status", "")
            }
        })
    return processes


def _build_logging_call_step(call_id: str, observation_no: int, label: str, sub_label: str = "") -> dict:
    """各 FR# / BE# の steps に挿入する「[LN] 観測点 N 記録呼出」step を構築する。

    意味合い: description は業務担当者語彙のみで「[LN] を呼ぶ」と簡潔に。実装詳細は [LN] 本体（§6.3）にあるため
              ここでは重複させない。
    v50 Phase 2: step 内の参照キー call_id → logging_ref に rename（{category}_ref 命名統一）。
              旧 call_id は廃止、新規生成は logging_ref のみ。
    """
    sub_suffix = f"／サブ観測「{sub_label}」" if sub_label else ""
    description = f"[{call_id}] 観測点[{observation_no}] {label}{sub_suffix}記録 を呼ぶ"
    return {
        "kind": "logging_call",
        "description": description,
        "logging_ref": call_id,  # v50 Phase 2: 旧 call_id → logging_ref（{category}_ref 命名統一）
        "observation_no": observation_no,
        "observation_sub": sub_label if sub_label else None
    }


def _renumber(steps: list) -> list:
    """step_no を 1 から再採番"""
    for i, s in enumerate(steps, 1):
        s["step_no"] = i
    return steps


def _strip_old_logging_steps(steps: list) -> list:
    """v39 で挿入した kind='logging' / kind='logging_call' な step を全削除（冪等性確保）"""
    return [s for s in (steps or []) if s.get("kind") not in ("logging", "logging_call")]


def insert_logging_calls_into_front_processes(data: dict, op_map: dict):
    """front_processes の各 F00X.steps から既存ログ step を除去し、観測点呼出 step を挿入。

    挿入ルール:
    - 先頭に [L1] フロント操作記録 呼出
    - backend_call kind の各 step 前に [L2] フロント→送信記録、後に [L6] フロント←受信記録
    - screen_update kind の最後の step 後に [L7] フロント処理記録
    - 末尾は何も挿入しない（[L8] は audit-logger 側、F00X からは直接呼ばない）
    """
    count = 0
    for fp in data.get("front_processes", []) or []:
        # 既存 logging step を全削除（冪等性）
        steps = _strip_old_logging_steps(fp.get("steps", []) or [])
        new_steps = []
        # v43: id_scheme.json 経由で呼出 ID 生成（旧ハードコード "L1"/"L2"/"L6"/"L7" 廃止）
        # [LN] フロント操作記録 呼出（先頭）
        if 1 in op_map:
            new_steps.append(_build_logging_call_step(format_id("logging", 1), 1, op_map[1].get("label", "")))
        last_screen_update_idx = None
        for s in steps:
            kind = s.get("kind", "")
            if kind == "backend_call" and 2 in op_map:
                new_steps.append(_build_logging_call_step(format_id("logging", 2), 2, op_map[2].get("label", "")))
            new_steps.append(s)
            if kind == "backend_call" and 6 in op_map:
                new_steps.append(_build_logging_call_step(format_id("logging", 6), 6, op_map[6].get("label", "")))
            if kind == "screen_update":
                last_screen_update_idx = len(new_steps) - 1
        if last_screen_update_idx is not None and 7 in op_map:
            new_steps.insert(last_screen_update_idx + 1, _build_logging_call_step(format_id("logging", 7), 7, op_map[7].get("label", "")))
        fp["steps"] = _renumber(new_steps)
        count += 1
    return count


def insert_logging_calls_into_backend_processes(data: dict, op_map: dict, op_4_subs: list, sub_label_by_kind: dict = None):
    """backend_processes の各 B00X.processing から既存ログ step を除去し、観測点呼出 step を挿入。

    挿入ルール:
    - 先頭に [L3] バック←受信記録 呼出
    - 最初の業務処理 step 前に [L4] バック処理記録 呼出
    - kind=transaction の前後に [L4a] DB-TX 境界記録 呼出
    - kind=db_operation の後に [L4b] SQL 実行記録 呼出
    - kind=cache_op の後に [L4c] キャッシュ操作記録 呼出
    - kind=audit の後に [L4d] 監査イベント記録 呼出（同時に [L8] も audit-logger 側で発火）
    - kind=external 系の後に [L4e] 外部サービス呼出記録 呼出
    - kind=authorization の後に [L4f] 認可チェック記録 呼出
    - 末尾に [L5] バック→送信記録 呼出
    """
    # v43: サブ観測ラベル → call_id のマップ（id_scheme.json 経由）
    sub_label_to_id = {}
    sub_letters = ['a', 'b', 'c', 'd', 'e', 'f']
    for i, sub in enumerate(op_4_subs or []):
        if i >= len(sub_letters):
            break
        # サブラベル付き ID は親観測点 4 と同じ番号を共有（例: LG4-a）
        sub_call_id = format_id("logging", 4, sub_label=sub_letters[i])
        sub_label_to_id[sub.get("label", "")] = (sub_call_id, sub.get("label", ""))

    kind_to_sub = {
        "transaction": "DB-TX 境界",
        "db_operation": "SQL 実行",
        "cache_op": "キャッシュ操作",
        "audit": "監査イベント",
        "send_audit": "監査イベント",
        "external": "外部サービス呼出",
        "s3_op": "外部サービス呼出",
        "ses_op": "外部サービス呼出",
        "sqs_op": "外部サービス呼出",
        "authorization": "認可チェック",
        "auth_check": "認可チェック"
    }
    # project_config の logging.sub_label_by_kind（kind → ラベル）で既定値を上書きする
    kind_to_sub.update(sub_label_by_kind or {})

    count = 0
    for bp in data.get("backend_processes", []) or []:
        proc = _strip_old_logging_steps(bp.get("processing", []) or [])
        new_proc = []
        # v43: id_scheme.json 経由で呼出 ID 生成（旧ハードコード "L3"/"L4"/"L5" 廃止）
        # [LN] バック←受信記録 呼出（先頭）
        if 3 in op_map:
            new_proc.append(_build_logging_call_step(format_id("logging", 3), 3, op_map[3].get("label", "")))
        # [LN] バック処理記録 呼出（最初の業務処理 step の前）
        l4_inserted = False
        business_kinds = ("validation", "db_operation", "calculation", "cache_op", "response_build", "transaction", "audit", "external", "authorization")
        for s in proc:
            kind = s.get("kind", "")
            if (not l4_inserted) and kind in business_kinds:
                if 4 in op_map:
                    new_proc.append(_build_logging_call_step(format_id("logging", 4), 4, op_map[4].get("label", "")))
                l4_inserted = True
            new_proc.append(s)
            # サブ観測呼出
            sub_label = kind_to_sub.get(kind)
            if sub_label and sub_label in sub_label_to_id:
                call_id, label = sub_label_to_id[sub_label]
                new_proc.append(_build_logging_call_step(call_id, 4, op_map.get(4, {}).get("label", ""), sub_label=label))
        # [LN] バック→送信記録 呼出（末尾）
        if 5 in op_map:
            new_proc.append(_build_logging_call_step(format_id("logging", 5), 5, op_map[5].get("label", "")))
        bp["processing"] = _renumber(new_proc)
        count += 1
    return count


def main():
    # 2026-10-05 汎用化: 機能名は第1引数（sys.argv[1]）か FEATURE_NAME 環境変数で決まり、どちらも無ければ止まる
    # 意味合い: 機能名をスクリプトに直書きしないので、どの画面でも同じスクリプトを再利用できる
    # 接続情報: 解決は _config_loader.get_feature_name_from_argv()。結果は下の中間 JSON のパスと get_logging_spec() に渡す
    feature_name = get_feature_name_from_argv()
    intermediate_json = INTERMEDIATE_DIR / f"{feature_name}.json"
    print(f"[INFO] target feature: {feature_name}")
    print(f"[INFO] intermediate JSON: {intermediate_json}")

    if not intermediate_json.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {intermediate_json}", file=sys.stderr)
        sys.exit(1)

    # 汎用化: feature_name を明示的に伝搬
    logging_spec = get_logging_spec(feature_name)
    if not logging_spec:
        print("[INFO] project_config.json にロギング規約なし、スキップ")
        return

    obs_points = logging_spec.get("observation_points", []) or []
    op_map = {int(p["no"]): p for p in obs_points if p.get("no") is not None}
    op_4 = op_map.get(4, {}) or {}
    op_4_subs = op_4.get("sub_observations", []) or []

    print(f"[INFO] 観測点数: {len(op_map)}、観測点 [4] サブ: {len(op_4_subs)}")

    with intermediate_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. 観測点ログ処理本体を common_logging_processes として新設
    data["common_logging_processes"] = _build_common_logging_processes(op_map, op_4_subs)
    print(f"[OK] common_logging_processes に観測点ログ処理 {len(data['common_logging_processes'])} 件定義（[L1]-[L8] + サブ [L4a]-[L4f]）")

    # 2. 各 F00X / B00X の steps に呼出 step を挿入
    front_count = insert_logging_calls_into_front_processes(data, op_map)
    back_count = insert_logging_calls_into_backend_processes(data, op_map, op_4_subs, logging_spec.get("sub_label_by_kind"))

    with intermediate_json.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[OK] front_processes の steps に観測点呼出 step を挿入: {front_count} 件")
    print(f"[OK] backend_processes の processing に観測点呼出 step を挿入: {back_count} 件")
    print(f"[OK] 保存先: {intermediate_json}")


if __name__ == "__main__":
    main()
