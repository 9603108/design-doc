#!/usr/bin/env python3
# 目的: 4つの partial JSON（_partial_screen_events / _partial_calc_state / _partial_backend / _partial_peripheral）を統合し、
#       15_中間JSONスキーマ.md 準拠の完成版中間JSON を生成する。
# 意味合い: design-doc スキルの Wave 1-3 を 4 Agent 並列で分散実行した結果を、メインセッションがコンテキストを圧迫せずに
#           1ファイルに統合するための中継スクリプト。Wave 3 のうち initialization と event_processes は本スクリプトで
#           最小限のテンプレ埋めを行う（充実化は次フェーズ）。
# 接続情報: 入力 = target/intermediate/_partial_*.json 4本 / 出力 = target/intermediate/{機能名}.json
#           機能名 = 第1引数 または 環境変数 FEATURE_NAME（_config_loader.get_feature_name_from_argv）
#           設定 = target/intermediate/{機能名}_screen_config.json の source_html_path /
#                  backend_files / project_pattern / profile を main() が meta に転記する
#           参照仕様 = .claude/skills/design-doc/15_中間JSONスキーマ.md

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    get_source_html_path,
    load_screen_config,
)

# 2026-10-05 汎用化: 機能名はモジュール先頭で1回だけ解決する（第1引数 > 環境変数 FEATURE_NAME）。
#   意味合い: 特定案件の機能名・絶対パスの直書きを廃止。未指定時は _config_loader 側が SystemExit で止める。
#   接続情報: OUTPUT と main() の meta 組立（screen_config 読込）がこの値を使う。
FEATURE_NAME = get_feature_name_from_argv()

# 入出力パス（design-doc/target/intermediate/）。partial 4本は機能名に依存しない固定名
PARTIALS = {
    "screen_events": INTERMEDIATE_DIR / "_partial_screen_events.json",
    "calc_state":    INTERMEDIATE_DIR / "_partial_calc_state.json",
    "backend":       INTERMEDIATE_DIR / "_partial_backend.json",
    "peripheral":    INTERMEDIATE_DIR / "_partial_peripheral.json",
}
OUTPUT = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"


def load_partial(name: str) -> dict:
    """partial JSON を読み込み。存在しなければ空dictを返してビルドを継続する"""
    path = PARTIALS[name]
    if not path.exists():
        print(f"[warn] {path.name} が見つかりません。空dictで継続します", file=sys.stderr)
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def renumber_events(events: list) -> list:
    """イベントの no を 1 からの通し番号に振り直す（event_code は維持）"""
    for idx, ev in enumerate(events, start=1):
        ev["no"] = idx
    return events


def renumber_common_logic(items: list) -> list:
    """共通ロジックの no を 1 からの通し番号に振り直す"""
    for idx, item in enumerate(items, start=1):
        item["no"] = idx
    return items


def build_initialization(events: list, screen_layout: dict) -> dict:
    """初期処理セクションを最小テンプレで生成。
    EV00 が events にあれば content を Overview に転載、なければデフォルト文を入れる。
    items は screen_layout から Button 以外を抜粋（仮実装）"""
    ev00 = next((e for e in events if e.get("event_code") == "EV00"), None)
    overview = ev00["content"] if ev00 else "画面初期表示処理（詳細はWave 3充実化で展開）"

    items = []
    for sl_item in screen_layout.get("items", []):
        # Button 以外を初期表示テーブルの対象に（01_画面レイアウト.md の対象基準準用）
        if sl_item.get("item_type") in ("Button", "Link"):
            continue
        items.append({
            "no": len(items) + 1,
            "screen_no": sl_item.get("screen_no", ""),
            "item_name": sl_item.get("item_name", ""),
            "active": "活性",       # 仮埋め
            "visible": "表示",      # 仮埋め
            "initial_value": "Blank",  # 仮埋め
        })

    return {
        "pattern": "DB取得",  # 初期表示はセッションデータ取得から始まる前提の DB取得パターン
        "overview": overview,
        "db_operations_ref": [],  # Wave 3 充実化で db_op_get_session_data 等を紐付ける
        "items": items,
    }


def build_event_processes(events: list, db_operations: list) -> list:
    """機能別処理シートを最小テンプレで生成。
    event_code と api_endpoint.action / trigger_event のマッチングで db_operations_ref を埋める。
    pattern は GET/POST/INSERT/UPDATE/DELETE の operation_type から推定。
    Overview は events[].content をそのまま転載（業務担当者の語彙への変換は既に済んでいる前提）。"""
    pattern_map = {
        "SELECT": "§9-A",  # 検索処理
        "INSERT": "§9-B",  # 登録・更新
        "UPDATE": "§9-B",
        "DELETE": "§9-H",  # データ削除
    }
    processes = []
    for ev in events:
        if ev.get("event_code") == "EV00":
            continue  # 初期処理は initialization へ
        ev_code = ev.get("event_code", "")
        ev_content = ev.get("content", "")

        # 紐づく DB操作を探す（api_endpoint.action / trigger_event いずれかでマッチ）
        related_db_ops = []
        for op in db_operations:
            api = op.get("api_endpoint", {}) or {}
            if api.get("trigger_event") == ev_code:
                related_db_ops.append(op.get("id", ""))

        # pattern 推定
        if related_db_ops:
            # 紐づくDB操作のうち最初のもののoperation_typeから推定
            first_op = next((op for op in db_operations if op.get("id") == related_db_ops[0]), None)
            op_type = first_op.get("operation_type", "") if first_op else ""
            pattern = pattern_map.get(op_type, "§9-G")  # その他は値変更
        else:
            pattern = "§9-G"  # クライアント側の値変更

        processes.append({
            "event_code": ev_code,
            "pattern": pattern,
            "overview": ev_content,
            "details": [],  # Wave 3 充実化で機能別処理.md の §9-x テンプレに従い展開
            "db_operations_ref": related_db_ops,
            "parameter_refs": [],
        })
    return processes


def main():
    se = load_partial("screen_events")
    cs = load_partial("calc_state")
    be = load_partial("backend")
    pe = load_partial("peripheral")

    # events をマージ（Agent A + Agent D）。連番は merge 後に振り直す
    events = (se.get("events", []) or []) + (pe.get("events_peripheral", []) or [])
    events = renumber_events(events)

    # common_logic をマージ（Agent B + Agent D）
    common_logic = (cs.get("common_logic", []) or []) + (pe.get("common_logic_peripheral", []) or [])
    common_logic = renumber_common_logic(common_logic)

    # validations は Agent B のフロント側のみ採用（バック側は messages_backend に含まれるためここでは別建てしない）
    validations = cs.get("validations_frontend", []) or []

    # messages は Agent C のバック側を中心に
    messages = be.get("messages_backend", []) or []

    # logical_names は Agent C を採用（バック側 schema_registry が SSoT に近い）
    logical_names = be.get("logical_names", []) or []

    # db_operations / api_spec / parameters は Agent C
    db_operations = be.get("db_operations", []) or []
    api_spec = be.get("api_spec", []) or []
    parameters = be.get("parameters_backend", {}) or {}

    # screen_layout は Agent A
    screen_layout = se.get("screen_layout", {}) or {}

    # calculations は Agent B
    calculations = cs.get("calculations", []) or []

    # meta（生成日時はJST）
    jst = timezone(timedelta(hours=9))
    now_iso = datetime.now(jst).isoformat(timespec="seconds")
    # 2026-10-05 汎用化: meta の案件固有値を screen_config から読む（直書き廃止）。
    #   意味合い: 設定を与えれば従来と同じ meta になる。backend_files / project_pattern / profile は
    #             未設定でも動く（空で出す）。screen_config 自体が無ければ FileNotFoundError で止まる（握らない）。
    #   接続情報: source_html は get_source_html_path の .name、source_html_path は設定値をそのまま転記。
    screen_config = load_screen_config(FEATURE_NAME)
    meta = {
        "feature_name": FEATURE_NAME,
        "source_html": get_source_html_path(FEATURE_NAME).name,
        "source_html_path": screen_config.get("source_html_path", ""),
        "backend_files": screen_config.get("backend_files", []),
        "project_pattern": screen_config.get("project_pattern", ""),
        "profile": screen_config.get("profile", ""),
        "generated_at": now_iso,
    }

    # initialization と event_processes は本スクリプトで最小テンプレ生成
    initialization = build_initialization(events, screen_layout)
    event_processes = build_event_processes(events, db_operations)

    # 統合中間JSON
    merged = {
        "meta": meta,
        "logical_names": logical_names,
        "screen_layout": screen_layout,
        "events": events,
        "initialization": initialization,
        "event_processes": event_processes,
        "db_operations": db_operations,
        "parameters": parameters,
        "validations": validations,
        "messages": messages,
        "calculations": calculations,
        "api_spec": api_spec,
        "common_logic": common_logic,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    # 集計レポート（メインセッションへの戻り値として stdout に出す）
    print(json.dumps({
        "output_file": str(OUTPUT),
        "stats": {
            "logical_names": len(logical_names),
            "screen_items": len(screen_layout.get("items", [])),
            "events": len(events),
            "event_processes": len(event_processes),
            "db_operations": len(db_operations),
            "api_spec": len(api_spec),
            "validations": len(validations),
            "messages": len(messages),
            "calculations": len(calculations),
            "common_logic": len(common_logic),
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
