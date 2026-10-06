#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_call_graph.py — 呼出グラフの整合性を機械検証する（v41 で新設、汎用）

【目的】
中間 JSON の呼出グラフ（front_processes ↔ backend_processes、観測点ログ呼出、監査ログ呼出）に対して
以下を機械検証し、見逃しゼロを保証する:
- 呼ばれていない F00X / B00X（ERROR / WARN）
- 呼んでるのに存在しない処理参照（ERROR）
- project_config に定義された trigger_function なのに抽出ゼロ（WARN）

【意味合い】
ソースに書いてある呼出を設計書へ取りこぼさないこと、どのプロジェクトでも同じ検証が使えることを意図している。
本スクリプトを Phase 6 と並行で実行することで、次画面以降は「呼出の見逃し」が機械的に検出される。

【接続情報】
- 入力: target/intermediate/{機能名}.json（build_call_graph.py 実行後の状態）
- 出力: 標準出力に検証結果（ERROR/WARN/INFO 別件数、詳細リスト）。終了コード = ERROR 件数
- 仕様根拠: SKILL.md Phase 1 改訂（v41）抽出網羅範囲表 / build_call_graph.py の補完結果

【設計原則】
- 汎用: 全プロジェクトで動作。project_config が空ならログ系検証はスキップ
- 終了コードで CI 連携可能（ERROR > 0 で 1、それ以外 0）
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

# 汎用化: sys.argv[1] / FEATURE_NAME 環境変数対応のため get_feature_name_from_argv 経由化
from _config_loader import get_feature_name_from_argv, get_logging_spec, get_audit_logging_spec, load_project_config

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
INTERMEDIATE_DIR = SKILL_DIR / "target" / "intermediate"


def verify_business_call_graph(data: dict, errors: list, warnings: list, infos: list):
    """業務処理の呼出グラフ検証（F00X ↔ trigger / B00X ↔ apiCall）"""
    front = data.get("front_processes", []) or []
    backs = data.get("backend_processes", []) or []
    all_fronts = {fp["process_id"]: fp for fp in front}
    all_backs = {bp["process_id"]: bp for bp in backs}

    # trigger_groups から呼ばれる F00X
    called_fronts = set()
    for g in data.get("trigger_groups", []) or []:
        for t in g.get("triggers", []) or []:
            for c in (t.get("calls", []) or []):
                called_fronts.add(c)
            for disp in (t.get("dispatch", []) or []):
                if disp.get("then_call"):
                    called_fronts.add(disp["then_call"])

    # FR#.steps から呼ばれる BE#
    # v50 Phase 2: backend_call.backend_ref（新名）優先、旧 backend_id フォールバック
    called_backs_from_steps = set()
    for fp in front:
        for s in fp.get("steps", []) or []:
            bc = s.get("backend_call")
            if bc:
                _bid = bc.get("backend_ref") or bc.get("backend_id")
                if _bid:
                    called_backs_from_steps.add(_bid)

    # build_call_graph.py が補完した called_by_front_processes（apiCall 経由）
    called_backs_from_apicalls = {bp["process_id"] for bp in backs if (bp.get("api_callsite_count", 0) > 0)}

    # 呼ばれていない F00X（trigger_groups から参照なし）
    # 目的: _dead_code_candidate マーク付き FR# は WARN 降格（業務的に説明可能、ソースの確認で対応 trigger 未検出と判断済）
    # 意味合い: 未使用と判断済みの処理を ERROR のまま残すと、本当の参照漏れが埋もれる。
    #          dead_code の識別を verify 出力に反映して、中間 JSON の印と検証結果を揃える（v51）
    # 接続情報: 呼出元 = verify_call_graph.py main / 中間 JSON .front_processes[].{_dead_code_candidate, _dead_code_note}
    unused_fronts = sorted(set(all_fronts.keys()) - called_fronts)
    for pid in unused_fronts:
        fp = all_fronts[pid]
        if fp.get("_dead_code_candidate") is True:
            # dead_code_candidate マーク済 → WARN 降格（実コード grep でも対応 trigger 未検出と判断済）
            note = fp.get("_dead_code_note", "")
            note_suffix = f" — note: {note}" if note else ""
            warnings.append(
                f"[WARN] 呼ばれていない F00X (dead_code_candidate マーク済): {pid} ({fp.get('name','')}) — 実コード grep でも未検出{note_suffix}"
            )
        else:
            errors.append(f"[ERROR] 呼ばれていない F00X: {pid} ({fp.get('name','')}) — trigger_groups から参照なし")

    # 呼ばれていない B00X（steps + apiCall 両方とも参照なし）→ WARN（共通モジュール経由の可能性あり）
    called_backs_any = called_backs_from_steps | called_backs_from_apicalls
    unused_backs = sorted(set(all_backs.keys()) - called_backs_any)
    for pid in unused_backs:
        bp = all_backs[pid]
        action = (bp.get("endpoint", {}) or {}).get("action", "")
        warnings.append(f"[WARN] 呼ばれていない B00X: {pid} ({bp.get('name','')[:30]} / action={action}) — front_processes.steps と apiCall の両方で参照なし。共通モジュール経由の呼出が抽出から漏れている可能性")

    # 呼んでるのに存在しない処理参照 → ERROR
    missing_fronts = sorted(called_fronts - set(all_fronts.keys()))
    for pid in missing_fronts:
        errors.append(f"[ERROR] 呼んでるのに存在しない F00X 参照: {pid}")
    missing_backs = sorted(called_backs_from_steps - set(all_backs.keys()))
    for pid in missing_backs:
        errors.append(f"[ERROR] 呼んでるのに存在しない B00X 参照: {pid}")

    # F00X.steps と apiCall の整合（apiCall で呼ばれているが steps に backend_call が無い B00X）→ WARN
    api_only_backs = called_backs_from_apicalls - called_backs_from_steps
    for pid in sorted(api_only_backs):
        bp = all_backs[pid]
        n = bp.get("api_callsite_count", 0)
        warnings.append(f"[WARN] {pid} は apiCall ({n}件) で呼出されているが front_processes.steps[].backend_call には未登録。steps への補完を検討")

    infos.append(f"[INFO] front_processes: 実在 {len(all_fronts)} 件、trigger 参照 {len(called_fronts)} 件、呼出網羅率 {len(called_fronts & set(all_fronts.keys()))}/{len(all_fronts)}")
    infos.append(f"[INFO] backend_processes: 実在 {len(all_backs)} 件、steps 参照 {len(called_backs_from_steps)} 件、apiCall 参照 {len(called_backs_from_apicalls)} 件、両方合わせた網羅率 {len(called_backs_any)}/{len(all_backs)}")


def verify_logging_callsites(data: dict, errors: list, warnings: list, infos: list, feature_name: str = None):
    """観測点ログ呼出の網羅性を検証（project_config の trigger_function vs ソース grep 結果）
    汎用化: feature_name 引数を追加（指定なしなら _config_loader 側の解決ロジックに委譲）
    """
    logging_spec = get_logging_spec(feature_name)
    if not logging_spec:
        infos.append("[INFO] project_config.logging なし、観測点ログ検証スキップ")
        return

    obs_points = logging_spec.get("observation_points", []) or []
    # v49: 純粋な関数名（識別子）のみ通すフィルタを追加
    # 意味合い: project_config.json の trigger_function には、複数の関数名を + でつないだ表記、
    #          関数名に実行基盤の種別名を添えた表記、括弧書きの補足のような複合文字列・説明語が
    #          混入することがあるため、分割後に ASCII 識別子のみ許容するフィルタを通して
    #          ゴミトークン（記号、ハイフン入りの名前、日本語の説明語など）を除外。
    # 接続情報: build_call_graph.py の extract_logging_calls にも同じフィルタを適用済
    import re as _re
    _IDENTIFIER_PATTERN = _re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
    # 識別子マッチするが実は関数名でない名詞語のブラックリスト
    # 意味合い: 関数名に種別を表す一般語を添えた複合表現を分割すると、その語（Function、Handler など）が
    #          関数名候補として混入する。これらは関数 grep の対象にしてはならない（必ず 0 件マッチで誤 WARN を出す）。
    # 2026-10-05 汎用化: 製品名にあたる語を基本語の集合から外し、案件ごとの語は設定から足す形にした。
    # 目的: 基本語（どの案件でも関数名にならない一般語）に、project_config の
    #       verify_overrides.noise_words（文字列の配列。無ければ空）を足した集合を作る。
    # 意味合い: 種別を表す語は案件の実行基盤によって違う（製品名など）ため、このファイルに直書きしない。
    #          設定に無い語は関数名として扱われ、呼出箇所が 0 件なら WARN になる。
    # 接続情報: 読み元 = project_config.json の verify_overrides（verify_intermediate.py の main() と同じ読み方）。
    #          build_call_graph.py の main() も同じ基本語と同じ設定キーで集合を作る
    #          （2つがずれると、期待する関数の集合と grep した関数の集合が食い違う）。
    _NOISE_WORDS = {"Function", "Module", "Handler", "Method", "Class", "Object"} | set(
        (load_project_config(feature_name).get("verify_overrides") or {}).get("noise_words", [])
    )
    # 抽出対象関数一覧（観測点 + サブ観測）
    expected_fns = set()
    def _add_parts(raw: str):
        if not raw:
            return
        # 空白 / / , + 等で分割し、括弧と空白を strip
        for part in _re.split(r"[\s/,+]+", raw):
            part = part.strip().strip("（）()")
            if part and _IDENTIFIER_PATTERN.match(part) and part not in _NOISE_WORDS:
                expected_fns.add(part)
    for op in obs_points:
        _add_parts(op.get("trigger_function", ""))
        for sub in (op.get("sub_observations", []) or []):
            _add_parts(sub.get("trigger_function", ""))

    callsites = data.get("logging_callsites", []) or []
    found_fns = {cs.get("function", "") for cs in callsites}

    # 期待されているが抽出ゼロの関数 → WARN
    missing = sorted(expected_fns - found_fns)
    for fn in missing:
        warnings.append(f"[WARN] project_config.logging で定義された観測点 trigger_function {fn}() の呼出箇所がソース内で 0 件。実装漏れ または 抽出パターン不適合の可能性")

    # ソースに存在するが project_config に未定義 → INFO（情報のみ）
    extra = sorted(found_fns - expected_fns)
    if extra:
        infos.append(f"[INFO] ソースで検出したが project_config 未定義のログ関数: {', '.join(extra[:5])}{'...' if len(extra) > 5 else ''}")

    infos.append(f"[INFO] 観測点ログ呼出: 期待 {len(expected_fns)} 種、実検出 {len(found_fns)} 種、合計 {len(callsites)} 箇所")


def verify_audit_callsites(data: dict, errors: list, warnings: list, infos: list, feature_name: str = None):
    """監査ログ呼出の網羅性を検証
    汎用化: feature_name 引数を追加（指定なしなら _config_loader 側の解決ロジックに委譲）
    """
    audit_spec = get_audit_logging_spec(feature_name)
    if not audit_spec:
        infos.append("[INFO] project_config.audit_logging なし、監査検証スキップ")
        return

    audit_fn = audit_spec.get("trigger_function", "")
    callsites = data.get("audit_callsites", []) or []
    if audit_fn and not callsites:
        warnings.append(f"[WARN] project_config.audit_logging.trigger_function ({audit_fn}) の呼出箇所がソース内で 0 件")

    applies_to = audit_spec.get("applies_to_processes", []) or []
    front = data.get("front_processes", []) or []
    front_ids = {fp["process_id"] for fp in front}
    for fp_id in applies_to:
        # process_id か業務名のいずれかでマッチ
        match = (fp_id in front_ids) or any(fp.get("name", "").find(fp_id) >= 0 for fp in front)
        if not match:
            warnings.append(f"[WARN] audit_logging.applies_to_processes で指定された '{fp_id}' に対応する front_processes が見当たらない")

    infos.append(f"[INFO] 監査ログ呼出: {len(callsites)} 箇所 / 対象指定: {len(applies_to)} 件")


def main():
    # 2026-10-05 汎用化: 機能名は第1引数(sys.argv[1])か FEATURE_NAME 環境変数で決まり、どちらも無ければ止まる
    # 意味合い: 既定の機能名を持たないことで、特定の画面に依存せずどの画面にも再利用できる
    # 接続情報: 呼出先 = _config_loader.get_feature_name_from_argv / 読込先 = INTERMEDIATE_DIR の {機能名}.json
    feature_name = get_feature_name_from_argv()
    intermediate_json = INTERMEDIATE_DIR / f"{feature_name}.json"
    print(f"[INFO] target feature: {feature_name}")
    print(f"[INFO] intermediate JSON: {intermediate_json}")

    if not intermediate_json.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {intermediate_json}", file=sys.stderr)
        sys.exit(1)

    with intermediate_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    errors, warnings, infos = [], [], []
    verify_business_call_graph(data, errors, warnings, infos)
    # 汎用化: feature_name を明示伝搬
    verify_logging_callsites(data, errors, warnings, infos, feature_name)
    verify_audit_callsites(data, errors, warnings, infos, feature_name)

    print(f"=== 呼出グラフ整合性検証結果 ERROR {len(errors)} 件 / WARN {len(warnings)} 件 / INFO {len(infos)} 件 ===")
    print()
    if errors:
        print("--- ERROR（要修正、見逃し検出） ---")
        for e in errors:
            print(" ", e)
        print()
    if warnings:
        print("--- WARN（要確認、共通モジュール経由等で許容可の場合あり） ---")
        for w in warnings:
            print(" ", w)
        print()
    if infos:
        print("--- INFO（参考情報） ---")
        for i in infos:
            print(" ", i)

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
