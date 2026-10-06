#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_logical_name_substitution.py — 中間JSON 内テキストの DB 物理名を「論理名[物理名]」に置換する（v30 で新設）

【目的】
front_processes / trigger_groups / backend_processes の overview / description / request_summary 等の
業務担当者向けテキストに DB 物理名（order_date / customer_code / item_code 等）が露出している箇所を、
logical_names テーブルを引いて「論理名[物理名]」表記（例: 注文日[order_date]）に置換する。

【意味合い】
本文に物理名が頻出して業務担当者が読みにくくなるのを防ぎ、論理名[物理名]表記を全体で徹底する。
共通の規則の「業務担当者の語彙を優先」「論理名[物理名]統一」を front_processes / trigger_groups にも拡張。
旧版（v29 まで）は §12 DB操作セクションだけが「論理名[物理名]」併記済で、それ以外のテキスト本文は
物理名のまま露出していた。

【接続情報】
- 入力: target/intermediate/{機能名}.json（logical_names を参照、置換対象セクションも上書き）
        機能名は第1引数か環境変数 FEATURE_NAME で与える（どちらも無ければ _config_loader が SystemExit）
- 出力: 同 JSON を上書き
- 仕様根拠: 共通の規則「論理名[物理名]統一」、SKILL.md「Phase 5.5 物理名置換」（v30 で新設）
- 実行順序: Phase 5 (構成図生成) の後、Phase 6 (整合性検証) の前
"""
import json
import re
import sys

# v30: ハードコード辞書を screen_config.json に集約、_config_loader 経由で取得する
# v51: project_config.json の forbidden_terms_map を読んで旧システムの名前等を業務語彙に置換
# 2026-10-05 汎用化: 自前の SCRIPT_DIR / SKILL_DIR と pathlib の import を削除した。
# 意味合い: 中間 JSON の置き場は _config_loader の INTERMEDIATE_DIR に一本化済みで、
#          本ファイル内に SCRIPT_DIR / SKILL_DIR / Path の参照が残っていなかったため。
# 接続情報: 置き場は下の INTERMEDIATE_JSON（INTERMEDIATE_DIR / "{機能名}.json"）で決まる。
from _config_loader import INTERMEDIATE_DIR, get_feature_name_from_argv, get_forbidden_terms_map

# 2026-10-05 汎用化: 既定の機能名（特定案件の画面名）を廃止し、機能名をここで1回だけ解決する。
# 意味合い: 第1引数 > 環境変数 FEATURE_NAME。どちらも無ければ _config_loader 側が
#          SystemExit("feature_name 未指定: ...") で止める（本ファイルでは既定値・独自の止め方を持たない）。
# 接続情報: FEATURE_NAME は下の INTERMEDIATE_JSON と、main() の get_forbidden_terms_map(FEATURE_NAME) が使う。
FEATURE_NAME = get_feature_name_from_argv()
INTERMEDIATE_JSON = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"

# 置換対象フィールド名（業務担当者向けテキストが入る場所のみ。バックエンド固有の sql 文等は対象外）
# 意味合い: 全フィールドを置換するとパフォーマンス低下 + API 名等まで誤変換するため、
#          業務担当者が読む可能性のあるフィールドだけに限定する。
TARGET_FIELDS = {
    "overview",        # front_processes/backend_processes の概要
    "description",     # steps の説明文
    "request_summary", # dispatch/calls のリクエスト要約
    "then",            # branches の遷移先
    "then_message",    # dispatch の終了メッセージ
    "condition",       # dispatch/branches の条件
    "trigger",         # events.trigger（業務担当者語彙の操作記述）
    "content",         # events.content（操作内容）
    "action",          # events.action
}

# 置換対象外フィールド（同名でも置換しない）
# 意味合い: API パスや SQL 等の機械可読テキストは物理名そのものに業務的意味があるため変換しない
SKIP_FIELDS = {
    "sql",             # 物理 SQL はそのまま
    "endpoint",        # API パス
    "path",            # ファイルパス
    "process_id",      # F001 等の識別子
    "physical_table",  # logical_names 自身の物理名フィールド
    "physical_column", # 同上
}


def build_substitution_map(logical_names: list) -> tuple:
    """物理名→論理名 のマップを構築する（カラム名/テーブル名 別々に返す）。

    意味合い: logical_names は同じ物理名が複数テーブルで使われることがある（例: order_date）。
              同一物理名で論理名が衝突する場合は最初に登場したものを採用（業務担当者向けには大差なし）。

    返り値: (col_map, table_map) — どちらも {物理名: 論理名} の dict
    """
    col_map = {}
    table_map = {}
    for entry in logical_names or []:
        pc = entry.get("physical_column")
        lc = entry.get("logical_column")
        if pc and lc and pc not in col_map:
            col_map[pc] = lc
        pt = entry.get("physical_table")
        lt = entry.get("logical_table")
        if pt and lt and pt not in table_map:
            table_map[pt] = lt
    return col_map, table_map


def _build_regex(physical_names: list) -> re.Pattern:
    """物理名リストを単語境界で検出する正規表現を構築。

    意味合い: \\b は ASCII の単語境界（[a-zA-Z0-9_] 境界）。
              「order_date_extra」のような連結された別単語にはマッチしない。
              長い物理名から優先マッチさせるため reverse sorted。
    """
    if not physical_names:
        return None
    escaped = [re.escape(p) for p in sorted(physical_names, key=len, reverse=True)]
    pattern = r'\b(' + '|'.join(escaped) + r')\b'
    return re.compile(pattern)


def substitute_text(text: str, col_pattern, col_map: dict, table_pattern, table_map: dict) -> str:
    """テキスト内の物理名を 論理名[物理名] に置換する（冪等性あり）。

    意味合い: 既に「論理名[物理名]」になっている箇所は再変換しない（プレースホルダで保護）。
              これにより本スクリプトを複数回実行しても安全（冪等）。
    """
    if not isinstance(text, str) or not text:
        return text

    result = text

    # 1. 既存の「論理名[物理名]」表記をプレースホルダで保護
    protected = {}
    placeholder_counter = [0]

    def protect(match_text: str) -> str:
        placeholder_counter[0] += 1
        key = f"@@P{placeholder_counter[0]}@@"
        protected[key] = match_text
        return key

    # カラム: 既存の「論理名[物理名]」を保護
    for pc, lc in col_map.items():
        existing = f"{lc}[{pc}]"
        if existing in result:
            result = result.replace(existing, protect(existing))
    # テーブル: 同様
    for pt, lt in table_map.items():
        existing = f"{lt}[{pt}]"
        if existing in result:
            result = result.replace(existing, protect(existing))

    # 2. カラム物理名を「論理名[物理名]」に置換
    if col_pattern is not None:
        def repl_col(m):
            pc = m.group(1)
            lc = col_map.get(pc, pc)
            return f"{lc}[{pc}]"
        result = col_pattern.sub(repl_col, result)

    # 3. テーブル物理名を「論理名[物理名]」に置換
    if table_pattern is not None:
        def repl_table(m):
            pt = m.group(1)
            lt = table_map.get(pt, pt)
            return f"{lt}[{pt}]"
        result = table_pattern.sub(repl_table, result)

    # 4. プレースホルダを復元
    for key, original in protected.items():
        result = result.replace(key, original)

    return result


def walk_replace(node, col_pattern, col_map, table_pattern, table_map, parent_key: str = ""):
    """JSON ツリー全体を走査し、TARGET_FIELDS のテキストのみ置換する。

    意味合い: 全テキストを盲目的に置換するとAPI名・URL・パス等まで誤変換するため、
              業務担当者向け説明テキストの入る既知フィールドだけに限定する。
    """
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if k in SKIP_FIELDS:
                continue
            if isinstance(v, str) and k in TARGET_FIELDS:
                node[k] = substitute_text(v, col_pattern, col_map, table_pattern, table_map)
            elif isinstance(v, (dict, list)):
                walk_replace(v, col_pattern, col_map, table_pattern, table_map, k)
    elif isinstance(node, list):
        for v in node:
            walk_replace(v, col_pattern, col_map, table_pattern, table_map, parent_key)


def apply_forbidden_terms(data: dict, forbidden_map: dict, target_sections: list) -> int:
    """禁止語彙（旧システムの名前等）を業務語彙に置換する（v51 で新設）。

    意味合い: 論理名[物理名] 置換とは別経路。logical_names テーブルに載らない
              旧システム固有の用語（旧システムの略号や、それを含む JS 関数名等）を、
              project_config.json forbidden_terms_map.terms{} の辞書で置換する。
              論理名置換より先に走らせる（旧名が物理名と混在しても影響しないため）。

    引数:
      data: 中間 JSON のトップレベル dict
      forbidden_map: {旧名: 業務語彙} の dict
      target_sections: 置換対象セクション名のリスト
    返り値: 置換実施回数（情報表示用）
    """
    if not forbidden_map:
        return 0
    # 長い旧名から先に置換（旧名を含む長い語を、その一部である短い旧名より先に処理）
    sorted_terms = sorted(forbidden_map.items(), key=lambda kv: len(kv[0]), reverse=True)
    count = 0

    def replace_in_str(s: str) -> str:
        nonlocal count
        if not isinstance(s, str) or not s:
            return s
        original = s
        for old, new in sorted_terms:
            if old and old in s:
                s = s.replace(old, new)
        if s != original:
            count += 1
        return s

    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, str):
                    node[k] = replace_in_str(v)
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str):
                    node[i] = replace_in_str(v)
                elif isinstance(v, (dict, list)):
                    walk(v)

    for section in target_sections:
        if section in data:
            walk(data[section])
    # screen_layout.items の remarks も対象（業務担当者語彙の重要箇所）
    if "screen_layout" in data:
        walk(data["screen_layout"])
    return count


def main():
    if not INTERMEDIATE_JSON.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {INTERMEDIATE_JSON}", file=sys.stderr)
        sys.exit(1)

    with INTERMEDIATE_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    logical_names = data.get("logical_names", []) or []
    col_map, table_map = build_substitution_map(logical_names)
    col_pattern = _build_regex(list(col_map.keys()))
    table_pattern = _build_regex(list(table_map.keys()))

    print(f"[INFO] 物理名マップ構築: カラム {len(col_map)} 件、テーブル {len(table_map)} 件")
    print(f"  サンプル カラム: {', '.join(f'{k}→{v}' for k, v in list(col_map.items())[:3])}")
    print(f"  サンプル テーブル: {', '.join(f'{k}→{v}' for k, v in list(table_map.items())[:3])}")

    # 置換対象セクション: 業務担当者向けテキストを持つ箇所
    # events / event_processes / front_processes / backend_processes / trigger_groups / api_spec / db_operations / validations
    target_sections = [
        "events", "event_processes",
        "front_processes", "backend_processes",
        "trigger_groups", "api_spec", "db_operations",
        "validations", "calculations", "common_logic",
        "initialization",
    ]

    # v51: 禁止語彙（旧システムの名前等）置換を先に実行 — project_config.json の forbidden_terms_map から
    # 意味合い: Agent A1 が旧名を含む JS 関数名や旧システムの名前を引用しがち。
    #          ここで案件固有の置換辞書を適用し、「旧システムの名前を設計書に出さない」決まりを機械担保。
    # 2026-10-05 汎用化: 機能名を明示で渡す（引数なしだと _config_loader は sys.argv を見ず、
    #          第1引数だけで機能名を渡した実行が「feature_name 未指定」で止まるため）。
    # 接続情報: {機能名}_project_config.json の forbidden_terms_map.terms（{旧名: 業務語彙}）を読む。
    forbidden_map = get_forbidden_terms_map(FEATURE_NAME)
    if forbidden_map:
        replaced_count = apply_forbidden_terms(data, forbidden_map, target_sections)
        print(f"[OK] 禁止語彙置換: {len(forbidden_map)} パターン、{replaced_count} 箇所変更")
    else:
        print(f"[INFO] forbidden_terms_map 未定義（project_config.json に設定なし、スキップ）")

    replaced_sections = []
    for section in target_sections:
        if section in data:
            walk_replace(data[section], col_pattern, col_map, table_pattern, table_map, section)
            replaced_sections.append(section)

    with INTERMEDIATE_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[OK] 物理名置換完了 — 対象セクション: {', '.join(replaced_sections)}")
    print(f"[OK] 保存先: {INTERMEDIATE_JSON}")


if __name__ == "__main__":
    main()
