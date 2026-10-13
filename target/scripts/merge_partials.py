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
# 2026-10-12 委譲の単位: --compose-units は、単位ごとの断片 _unit_<単位>.json（prompts/units.json の分担表）を
#           検査してから partial 4本へ合成して書き、統合はせずに終わる（この後に apply_react_draft --stage partial と
#           本スクリプトの通常の実行が続く）。断片の event_processes（details）は _partial_screen_events.json に入り、
#           通常の実行が event_processes[].details へ写す。断片が無い画面（HTML を1体で作る従来の流れ）は何も変わらない

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    load_project_config,
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
    event_code と api_endpoint.trigger_event(単数)/ trigger_events(event_code の配列)の
    一致で db_operations_ref を埋める(action では照合しない。2026-10-09 trigger_events 対応)。
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

        # 紐づく DB操作を探す（api_endpoint.trigger_event / trigger_events の event_code 一致でマッチ）
        # 2026-10-09 trigger_events 対応:
        #   目的: 同じ action を複数イベントから呼ぶ多対一を api_endpoint.trigger_events(event_code の配列)で表せるようにする。
        #   意味合い: 単数 trigger_event と併用でき、キーが無い・null なら従来どおり単数のみで判定する。
        #     op ごとに1回だけ判定するので、単数と配列に同じ event_code があっても db_operations_ref には1回だけ入る。
        #     見落とすと多対一の API が db_operations_ref に出ず、pattern が §9-G に落ちる。
        #   接続情報: 少なくとも migrate_v17_to_v18.fill_event_backend_calls が同じキー(trigger_event / trigger_events)を読む。
        related_db_ops = []
        for op in db_operations:
            api = op.get("api_endpoint", {}) or {}
            if api.get("trigger_event") == ev_code or ev_code in (api.get("trigger_events") or []):
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


def compose_units() -> int:
    """_unit_*.json を検査し、問題が無ければ partial 4本へ合成して書く。終了コード 0=書いた / 1=問題あり（書かない）/ 2=断片なし。
    既存の partial は .bak_units_<日時> に控えてから置き換える（合成は断片だけから作り直す。前回の partial の残りを混ぜない）"""
    import _units
    spec = _units.load_spec()
    frags, problems = _units.load_fragments(INTERMEDIATE_DIR)
    if not frags and not problems:
        print(f"[入力不正] {INTERMEDIATE_DIR} に _unit_*.json が無い", file=sys.stderr)
        return 2
    problems += _units.check_all(spec, frags)
    composed = _units.compose(spec, frags, load_project_config())
    parts, unknown = _units.split_partials(composed)
    problems += [f"[範囲外] 最上位キー {k}: partial に入口が無い" for k in unknown]
    if problems:
        for pr in problems:
            print(pr)
        print(f"[NG] 問題 {len(problems)} 件。partial は書いていない（差し戻し先の単位に直させてから再実行する）")
        return 1
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d-%H%M%S")
    for name, data in parts.items():
        path = PARTIALS[name]
        if path.exists():
            path.rename(path.with_name(f"{path.name}.bak_units_{stamp}"))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 断片 {len(frags)} 本（{', '.join(sorted(frags))}）を partial 4本へ合成した")
    return 0


def apply_unit_details(event_processes: list, unit_eps: list) -> None:
    """断片由来の event_processes（_partial_screen_events.json の event_processes）の details を、event_code で写す。
    details_none_reason と src も写す。断片に無いイベントは merge が作った空の details のまま"""
    by_code = {e.get("event_code"): e for e in unit_eps or [] if isinstance(e, dict)}
    for ep in event_processes:
        src = by_code.get(ep.get("event_code"))
        if not src:
            continue
        if src.get("details"):
            ep["details"] = list(src["details"])
        if src.get("details_none_reason"):
            ep["details_none_reason"] = src["details_none_reason"]
        if src.get("src"):
            ep["src"] = list(src["src"])


def main():
    # --help / -h は機能名ではなく使い方の要求。FEATURE_NAME に入ると screen_config 読込で落ちるため先に返す。
    #   接続: 第1引数の判定だけ。機能名の解決（モジュール先頭）は変えない。
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("usage: merge_partials.py [機能名] [--compose-units]  (機能名省略時は環境変数 FEATURE_NAME)")
        print("  --compose-units: _unit_*.json を検査して partial 4本へ合成するだけで終わる（委譲の単位の流れ）")
        return
    if "--compose-units" in sys.argv[1:]:
        sys.exit(compose_units())
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
    # 手順書の欄名（name/expression/used_in/zero_division/null_handling）を generate_docx.js の読み先へ読み替える。
    #   意味合い: sectionCommonLogic（generate_docx.js 3767 付近）は description/used_by_processes/processing_steps を読むため、
    #             欄名が違うと 5.5 章が空になる。既にある欄は上書きしない。
    #   接続: 読み替えの入力は cs/pe の common_logic。出力は merged["common_logic"]（下の merged 組立）。
    for item in common_logic:
        # description は概要(30字以内)。長い式は写さず processing_steps にだけ入れる(S1 が概要を必ず書く)
        if not item.get("description") and item.get("expression") and len(item["expression"]) <= 30:
            item["description"] = item["expression"]
        # F4: S1 が書く runs_on(動作場所)を generate_docx.js の読み先 runtime_location へ写す。書かれていなければ WARN(docx は「（未指定）」になる)
        if not item.get("runtime_location") and item.get("runs_on"):
            item["runtime_location"] = item["runs_on"]
        if not item.get("runtime_location"):
            print(f"[WARN] [動作場所] common_logic「{item.get('name')}」に runs_on が無い(ブラウザ / サーバー(API) / データベース) → 差し戻し先 S1", file=sys.stderr)
        if not item.get("used_by_processes") and item.get("used_in"):
            used = item["used_in"]
            item["used_by_processes"] = [used] if isinstance(used, str) else list(used)
        if not item.get("processing_steps"):
            # sectionCommonLogic は processing_steps[] を {no, kind, description} で読む(文字列だと空セルになる)
            vals = [(kind, item[key]) for key, kind in
                    (("expression", "演算"), ("zero_division", "0除算"), ("null_handling", "NULL"))
                    if item.get(key)]
            if vals:
                item["processing_steps"] = [{"no": i, "kind": k, "description": v}
                                            for i, (k, v) in enumerate(vals, 1)]

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
    apply_unit_details(event_processes, se.get("event_processes"))

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
