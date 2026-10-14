#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_v17_to_v18.py — v17 中間JSON を v18 形式（呼ぶ側／呼ばれる側 体系）に変換する（v30 で外部設定化）

【目的】
v17 まで（events / event_processes / api_spec / db_operations）の中間JSON 構造を、
v18 の呼ぶ側／呼ばれる側 体系（trigger_groups / front_processes / backend_processes）に変換する。

【意味合い】
- events[] → trigger_groups[].triggers[]（呼ぶ側）
- event_processes[] → front_processes[]（呼ばれるフロント、F001 から連番採番）
- api_spec[] → backend_processes[]（呼ばれるバックエンド、B001 から連番採番、1 endpoint = 1 process）
- db_operations[] → backend_processes[].processing[].db_op_detail に分配（api_endpoint.action マッチで紐付け）
- process_flows[] は中間JSON に保持しつつ docx 出力対象外（v18 では検討保留）
- v17 までのキーも保持する（変換元として残置、migrate を冪等にするため）
- v30 でハードコード辞書（PROCESS_NAMES_BY_EVENT_CODE / _derive_front_area の keyword_to_area）を
  screen_config.json に外出しし、本スクリプトを画面非依存（汎用化）にした。

【接続情報】
- 入力: target/intermediate/{機能名}.json + target/intermediate/{機能名}_screen_config.json
- 出力: 同ファイルを上書き（trigger_groups / front_processes / backend_processes を追加）
- 仕様根拠: 15_中間JSONスキーマ.md「v18 メインスキーマ」セクション、SKILL.md「Phase 3-4 外部設定ファイル化」（v30）
"""
import functools
import json
import sys

# v30: ハードコード辞書を screen_config.json に集約、_config_loader 経由で取得する
# v43: ID 体系を id_scheme.json (スキル付属) 経由化、ハードコード接頭辞 "F" を全廃
from _react_facts import add_fetch_order_step
from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    get_process_names_by_event_code,
    get_front_area_keywords,
    get_process_overview_overrides,
    get_initial_processes,
    # 2026-10-05 汎用化: _guess_step_kind が project_config の step_kind_keywords を読むために使う
    load_project_config,
    format_id,
)

# 2026-10-05 汎用化: 参照が無くなった SCRIPT_DIR / SKILL_DIR の定義と pathlib の import を削った。
# 意味合い: 中間 JSON の置き場は _config_loader の INTERMEDIATE_DIR に一本化済みで、本ファイルは自前でパスを組み立てない。
# 2026-10-05 汎用化: 既定の機能名を廃止し、機能名は第1引数か環境変数 FEATURE_NAME で必ず与える。
# 意味合い: _config_loader のアクセサは引数なしだと sys.argv を見ないため、ここで1回だけ解決し、
#          本ファイル内のアクセサ呼出すべてに FEATURE_NAME を明示で渡す（argv だけで渡しても動くように）。
# 接続情報: 未指定なら _config_loader.resolve_feature_name が SystemExit で止める。
#          下の PROCESS_NAMES_BY_EVENT_CODE が import 時にアクセサを呼ぶので、それより前に置く。
# WHY: --selftest は機能名を取らない(argv[1] を機能名にすると画面設定の読込で落ちる)。selftest のときだけ機能名を仮の値にする。
SELFTEST_MODE = "--selftest" in sys.argv
FEATURE_NAME = "selftest" if SELFTEST_MODE else get_feature_name_from_argv()
INTERMEDIATE_JSON = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"

# v30: PROCESS_NAMES_BY_EVENT_CODE は screen_config.json から動的取得（モジュールロード時に1回）
# 意味合い: 画面別ハードコードを画面別 JSON に外出しすることで、本スクリプトは画面非依存の汎用処理になる
PROCESS_NAMES_BY_EVENT_CODE = {} if SELFTEST_MODE else get_process_names_by_event_code(FEATURE_NAME)


def classify_trigger_kind(event: dict, default_screen_name: str = "") -> tuple:
    """events[] の各イベントを v18 trigger_groups の kind / category に分類する。

    分類ルール:
    - EV00 → kind='auto', category='画面表示時'
    - source_kind='internal' で trigger に「WebSocket」を含む → kind='external', category='WebSocket受信'
    - source_kind='internal' で trigger に「DevTools」「コンソール」を含む → kind='external', category='開発者操作'
    - source_kind='screen'（既定）→ kind='screen_operation', category=画面名
      - location 配列から「画面」を含む要素を選択、なければ default_screen_name にフォールバック
    """
    ev_code = event.get("event_code", "")
    source_kind = event.get("source_kind", "screen")
    trigger_text = event.get("trigger", "") or ""
    location = event.get("location", []) or []

    # EV00 は初期処理 = 自動トリガー
    if ev_code == "EV00":
        return ("auto", "画面表示時")

    # source_kind=internal の判別
    if source_kind == "internal":
        if "WebSocket" in trigger_text or "websocket" in trigger_text.lower():
            return ("external", "WebSocket受信")
        if "DevTools" in trigger_text or "コンソール" in trigger_text or "runIntegrationTests" in trigger_text or "runE2EScenarios" in trigger_text:
            return ("external", "開発者操作")
        return ("external", "その他")

    # screen_operation: 画面名を category に
    # 1. location 配列から「画面」を含む要素を選ぶ（業務担当者語彙で適切な画面名）
    for loc in location:
        if loc and "画面" in loc:
            return ("screen_operation", loc)
    # 2. フォールバック: default_screen_name（meta.feature_name + "画面"）
    if default_screen_name:
        return ("screen_operation", default_screen_name)
    # 3. 最終フォールバック: location[0] そのまま
    return ("screen_operation", location[0] if location else "（画面名不明）")


def build_trigger_groups(events: list, event_to_front_id: dict, default_screen_name: str = "") -> list:
    """events[] から v18 trigger_groups[] を生成。

    各イベントを (kind, category) で分類し、同じ群にまとめる。
    各トリガーの calls[] は event_to_front_id (EV01→F001 等のマップ) で解決。
    default_screen_name: screen_operation の category フォールバック用画面名（meta.feature_name + 「画面」）
    """
    # 分類: {(kind, category): [{trigger_no, location, action, description, calls}]}
    grouped = {}

    for ev in events:
        kind, category = classify_trigger_kind(ev, default_screen_name)
        key = (kind, category)
        if key not in grouped:
            grouped[key] = []

        ev_code = ev.get("event_code", "")
        calls = []
        if ev_code in event_to_front_id:
            calls.append(event_to_front_id[ev_code])

        grouped[key].append({
            "trigger_no": len(grouped[key]) + 1,
            "location": ev.get("location", []) or [],
            "action": ev.get("action", ""),
            "description": ev.get("trigger", "") or ev.get("content", ""),
            "calls": calls
        })

    # kind の出現順序を固定
    kind_order = ["screen_operation", "auto", "external"]
    kind_label = {
        "screen_operation": "画面操作トリガー",
        "auto": "自動トリガー",
        "external": "外部トリガー"
    }

    trigger_groups = []
    # screen_operation を先に出す（画面名で category を category 順に並べる）
    for kind in kind_order:
        # この kind の category 一覧
        categories = sorted({c for (k, c) in grouped.keys() if k == kind})
        for cat in categories:
            triggers = grouped.get((kind, cat), [])
            trigger_groups.append({
                "kind": kind,
                "kind_label": kind_label[kind],
                "category": cat,
                "triggers": triggers
            })

    return trigger_groups


def _derive_front_area(ep: dict, ev: dict) -> str:
    """フロント処理の area を業務操作カテゴリで推定する（v19 で追加）。

    判定優先順序:
    1. event_code='EV00' → "初期処理"
    2. source_kind='internal' + trigger に "WebSocket" → "編集権制御"
    3. source_kind='internal' + trigger に "DevTools|コンソール|runIntegrationTests|runE2EScenarios" → "動作検証"
    4. location / trigger / content / overview のキーワードマッチ:
       （キーワードは screen_config.json の front_area_keywords から読む。以下は例）
       - "ワークフローステッパー" "確定" → "ワークフロー操作"
       - "出力メニュー" "帳票" → "帳票出力"
       - "編集権" "閲覧モード" "編集中バナー" "リクエスト" → "編集権制御"
       - "サジェスト" "ドロップダウン" → "サジェスト"
       - "確認ダイアログ" "showConfirm" "閉じる" → "ダイアログ操作"
       - "タブ" "パターン切替" → "パターン切替"
       - "グリッド" "行を追加" "行削除" "削除" "入力値" "数量" "復元" → "データ編集"
       - "対象年月" "入力モード" → "ヘッダ操作"
       - "伝票" "戻る" "遷移" "スプレッドシート" "一覧" → "画面遷移"
    5. fallback → "その他操作"

    意味合い: docx §6.1 配下を H4 でエリア別にまとめるための分類軸。
              業務担当者が「確定処理は §6.1.X ワークフロー操作 にある」と探せるようにする。
    """
    ev_code = ev.get("event_code", "")
    source_kind = ev.get("source_kind", "screen")
    trigger_text = ev.get("trigger", "") or ""
    content = ev.get("content", "") or ""
    overview = (ep.get("overview", "") or "") if ep else ""
    location = ev.get("location", []) or []
    blob = " ".join([trigger_text, content, overview] + location)

    if ev_code == "EV00":
        return "初期処理"

    if source_kind == "internal":
        if "WebSocket" in trigger_text or "websocket" in trigger_text.lower():
            return "編集権制御"
        if any(kw in trigger_text for kw in ["DevTools", "コンソール", "runIntegrationTests", "runE2EScenarios"]):
            return "動作検証"

    # v30: キーワードマッチを screen_config.json (front_area_keywords) から動的取得。
    # 優先順序: 配列の出現順（業務的に特殊な領域 → 一般的な編集領域）
    # 注意: 「タブ」単独だと「同一タブ内」等の検証文脈にも誤マッチするため、具体フレーズに限定
    for entry in get_front_area_keywords(FEATURE_NAME):
        keywords = entry.get("keywords", [])
        area = entry.get("area", "")
        if area and any(kw in blob for kw in keywords):
            return area

    return "その他操作"


def build_front_processes(event_processes: list, events: list, initialization: dict, default_screen_name: str = "") -> tuple:
    """event_processes[] + initialization から v18 front_processes[] を生成。

    Returns:
        (front_processes, event_code_to_process_id)
    """
    event_map = {e.get("event_code"): e for e in events if e.get("event_code")}

    # v30: 概要上書きマップを取得（F001 含む長すぎ概要を業務目的1文に上書き）
    # 意味合い: 「概要」と「分岐・呼出」の重複防止のため、業務目的1文を screen_config.json で明示。
    #          override が無い場合は _derive_short_overview で自動短縮。
    overview_overrides = get_process_overview_overrides(FEATURE_NAME)

    front_processes = []
    event_to_id = {}  # EV01 → F001 のマップ
    # A3: EV00 の出典。event_processes に EV00 が無い案件(初期表示は initial_processes が書く)は events の EV00 の src を使う
    ev00_src = next((x.get("src") for x in list(event_processes) + list(events) if x.get("event_code") == "EV00" and x.get("src")), [])

    # v42 案B汎用化: initial_processes[] を screen_config.json から読んで N 個の独立処理を生成
    # 意味合い: URL パラメータ別の分岐パターンを「呼ばれる側」（独立 F001-* 処理）として表現。
    #          「呼ぶ側」(§5.2.1 [画面表示時] trigger_dispatch.EV00) で URL パラメータ判定 → 該当 F001-* を呼出。
    #          v18/v23 設計思想「呼ぶ側で判定、呼ばれる側は純粋処理」と整合。
    init_procs = get_initial_processes(FEATURE_NAME)
    if init_procs:
        # 各 URL パラメータパターン別に独立処理を生成
        for ip in init_procs:
            pid = ip.get("process_id", "")
            if not pid:
                continue
            front_processes.append({
                "process_id": pid,
                "name": ip.get("name", "（初期処理）"),
                "area": "初期処理",
                "overview": overview_overrides.get(pid) or _build_initial_process_overview(ip),
                "preconditions": ip.get("preconditions", []) or [],
                "postconditions": ip.get("postconditions", []) or [],
                "called_by_triggers": [
                    {"group_kind": "auto", "group_category": "画面表示時", "trigger_no": 1}
                ],
                # B9 c: 取得の開始時・失敗時の事実(EV00 の failure_effects)を backend_call の前後に入れてから番号を振る
                "steps": _renumber_init_steps(_with_init_fact_steps(_with_fetch_order(ip.get("steps", []) or []), _ev00_failure_effects(), _fetch_mode())),
                "validation_refs": [],
                "calculation_refs": [],
                "common_logic_refs": [],
                "parameter_refs": []
            })
        # A3: EV00 が呼ぶ最初の初期処理にだけ、EV00 の出典(初期表示の useEffect)を付ける。他の初期処理(URL パラメータ別)は根拠の行が別なので付けない
        first = next((fp for fp in front_processes if fp["process_id"] == init_procs[0].get("process_id")), None)
        if first and ev00_src:
            first["src"] = ev00_src
        # EV00 → 最初の初期処理（既定: F001、新規モード）に紐付ける
        # ※ trigger_dispatch.EV00 が apply_trigger_dispatch.py で URL パラメータ別に分岐させる
        event_to_id["EV00"] = init_procs[0].get("process_id", "F001")
    else:
        # 2026-10-05 汎用化: initial_processes 未定義時は F001 を steps 空で作る。
        # 意味合い: 旧 _build_init_steps は特定案件の初期処理（固定の backend_id・URL パラメータ名）を
        #          埋め込むフォールバックだったため廃止した。初期処理の手順が要る画面は
        #          screen_config.json の initial_processes[].steps で与える（上の if 側が読む）。
        # 接続情報: steps が空だと link_backend_to_front は F001 から backend への逆引きを登録しない。
        # 2026-10-05 汎用化: 既定の overview と postconditions を、編集ロックを前提にしない文へ直した。
        # 意味合い: 編集ロック・編集／閲覧モードを持たない画面でも、既定の文言が事実と食い違わないようにする。
        # 接続情報: この2つの文言は front_processes の F001（overview / postconditions）に入り、docx の処理詳細に出る。
        f001_overview = overview_overrides.get("F001") or _derive_short_overview({
            "overview": initialization.get("overview", "初期表示に必要なデータを取得し、画面項目へ展開する")
        })
        front_processes.append({
            "process_id": "F001",
            "name": "画面の初期処理",
            "area": "初期処理",
            "overview": f001_overview,
            "preconditions": ["業務担当者が画面URLを開く"],
            "postconditions": [
                "画面の全項目が初期値で表示される",
                "画面の初期表示が完了する"
            ],
            "called_by_triggers": [
                {"group_kind": "auto", "group_category": "画面表示時", "trigger_no": 1}
            ],
            "steps": [],
            "validation_refs": [],
            "calculation_refs": [],
            "common_logic_refs": [],
            "parameter_refs": [],
            "src": ev00_src  # ソース出典(A3)
        })
        event_to_id["EV00"] = "F001"

    # それ以外の event_processes を F002 以降に
    idx = 2
    for ep in event_processes:
        ev_code = ep.get("event_code", "")
        if ev_code == "EV00":
            continue  # 既に F001 に割当済

        # v43 fix: 旧体系 "F{idx:03d}" で出力する（screen_config.json の旧体系参照 "F003"/"F004"/"F005" と整合）。
        # 最終的に apply_id_scheme.py (Phase 5.8) が全 ID を新体系（FR#）に一括変換する設計。
        # migrate の id_scheme 経由化は当初実施したが、screen_config 側の参照が孤立する問題が発覚 → 旧体系出力に戻した。
        process_id = f"F{idx:03d}"
        idx += 1
        event_to_id[ev_code] = process_id

        ev = event_map.get(ev_code, {})
        steps = _build_event_steps(ep, ev)

        # v30: overview は override 優先、なければ _derive_short_overview で自動短縮
        # 意味合い: 業務目的1文に絞ることで「概要」と「steps」の重複を防ぐ
        overview = overview_overrides.get(process_id) or _derive_short_overview(ep)
        front_processes.append({
            "process_id": process_id,
            "name": _derive_process_name(ep, ev),
            "area": _derive_front_area(ep, ev),
            "overview": overview,
            "preconditions": [],
            "postconditions": [],
            "called_by_triggers": _derive_called_by_triggers(ev, default_screen_name),
            "steps": steps,
            "validation_refs": [],
            "calculation_refs": [],
            "common_logic_refs": [],
            "parameter_refs": ep.get("parameter_refs", []) or [],
            "src": ep.get("src") or ev.get("src") or []  # ソース出典(A3)。event_processes 優先、無ければ events の src
        })

    return front_processes, event_to_id


def _build_initial_process_overview(ip: dict) -> str:
    """initial_processes[] エントリから業務目的1文の overview を組み立てる（v42 案B汎用化）。

    意味合い: screen_config.json initial_processes[] の name + url_param_match から
              「{name}（{url_param_match}）」形式の業務目的1文を生成。
              手動で process_overview_overrides に書く必要は基本なし（自動生成で十分）。
    """
    name = ip.get("name", "初期処理")
    url_match = ip.get("url_param_match", "")
    if url_match:
        return f"{name}。URLパラメータ条件: {url_match}"
    return name


def _fetch_mode():
    """react 下書き events[0].fetch_plan.mode(parallel・serial・single)を返す。下書きが無い・読めないときは None。
    WHY: 並行の並び替え(_with_init_fact_steps)が読む。読み元は _with_fetch_order と同じ下書き1本。"""
    draft = INTERMEDIATE_DIR / f"{FEATURE_NAME}_react_draft.json"
    if not draft.exists():
        return None
    try:
        plan = ((json.loads(draft.read_text(encoding="utf-8")).get("events") or [{}])[0]).get("fetch_plan")
    except (OSError, ValueError, AttributeError):
        return None
    return (plan or {}).get("mode")


def _with_fetch_order(steps: list) -> list:
    """初期処理の steps に、取得の順(react 下書き events[0].fetch_plan.mode)の1行が無ければ足した写しを返す。

    WHY: 並行か順かは TSX から機械で決まる事実なので、S0(モデル)に書かせずここで入れる(verify_vs_source の [取得順] と同じ語)。
    接続: 下書き {機能名}_react_draft.json が無い・fetch_plan が無い・single なら何もしない。足す文は _react_facts.add_fetch_order_step。
    """
    draft = INTERMEDIATE_DIR / f"{FEATURE_NAME}_react_draft.json"
    if not draft.exists():
        return steps
    try:
        plan = ((json.loads(draft.read_text(encoding="utf-8")).get("events") or [{}])[0]).get("fetch_plan")
    except (OSError, ValueError, AttributeError):
        return steps
    out = [dict(s) for s in steps]
    add_fetch_order_step(out, plan)
    return out


def _renumber_init_steps(steps: list) -> list:
    """initial_processes[].steps の step_no を 1 から再採番する（v42 案B汎用化）。"""
    result = []
    for i, s in enumerate(steps, 1):
        s2 = dict(s)
        s2["step_no"] = i
        # kind 未指定時は description から推定
        if not s2.get("kind"):
            s2["kind"] = _guess_step_kind(s2.get("description", ""))
        result.append(s2)
    return result


def _ev00_failure_effects() -> list:
    """react 下書きの failure_effects のうち EV00(初期表示)の分を、ソース順で返す。下書きが無ければ空。
    WHY: 初期処理の取得の開始時・失敗時の事実を、EV00 の取得の並び(1件 = 1回の取得)から出すため(B9 c)。"""
    draft = INTERMEDIATE_DIR / f"{FEATURE_NAME}_react_draft.json"
    if not draft.exists():
        return []
    try:
        data = json.loads(draft.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [f for f in (data.get("failure_effects") or []) if isinstance(f, dict) and f.get("event") == "EV00"]


def _with_init_fact_steps(steps: list, ev00_failures: list, mode=None) -> list:
    """初期処理の steps の各 backend_call の前後に、EV00 の事実の行を state_update の step として足した写しを返す。

    WHY: EV00 は apply_react_draft(B6)が強制しない(EV00 以外が対象)ため、初期処理の取得の事実はここで入れる。
    対応: EV00 の failure_effects(1件 = 1回の取得、ソース順)と、steps の backend_call(出現順)を1対1で取る。
          件数が合わなければ何もしない(推測で対応させない)。
    位置: 開始時は呼出の前、成功時と失敗時は呼出の後(成功、失敗の順)。同じ趣旨の step が既にあれば足さない(extract_react_draft._FACT_MATCH の語で判定)。
          成功時の行は、後で migrate_v48_to_v49 が呼出の直後に入れる response_mapping の後ろに来る。
    並行(mode == "parallel")の並べ替えは、ここでは行わない。v49 の reorder_parallel_fetch が response_mapping の後に行う。
          WHY: 応答側の位置は、v49 が呼出の直後に response_mapping を入れた後でないと決まらないため。mode は呼出元の形を保つために受け取る。
    文面: extract_react_draft._event_facts と同じ文を使う(下書き側と文が割れないように)。state 名は下書きのまま(画面項目名への言い換えは無い)。
    """
    idx = [i for i, s in enumerate(steps) if s.get("kind") == "backend_call" or s.get("backend_call")]
    if not ev00_failures or len(idx) != len(ev00_failures):
        return steps
    from extract_react_draft import _event_facts, _FACT_MATCH  # 遅延 import: 下書きの文面を使うため

    texts = {}  # (取得の番号, key) → 文
    for k, fe in enumerate(ev00_failures):
        for f in _event_facts({"empty": []}, [fe], {}):
            if f["key"] in ("start", "success", "failure"):
                texts[(k, f["key"])] = f["text"]

    def _present(key: str) -> bool:
        return any(m in str(s.get("description") or "") for s in steps for m in _FACT_MATCH[key])

    def _fact(k: int, key: str):
        t = texts.get((k, key))
        return {"kind": "state_update", "description": t} if t and not _present(key) else None

    pos = {i: k for k, i in enumerate(idx)}
    pre = []     # 最初の呼出より前の step
    blocks = []  # [開始時の行(0か1件), 呼出の step, 応答側(成功・失敗の行と、後続の step)]
    for i, s in enumerate(steps):
        k = pos.get(i)
        if k is None:
            (blocks[-1][2] if blocks else pre).append(dict(s))
            continue
        head = [x for x in [_fact(k, "start")] if x]
        tail = [x for x in [_fact(k, "success"), _fact(k, "failure")] if x]
        blocks.append([head, dict(s), tail])

    out = list(pre)
    for head, call, tail in blocks:
        out += head + [call] + tail
    return out


# F1: 文面より react 下書きの事実(behavior の種別)を優先する。絞り込み・表示切替の behavior は入力検査ではない。
DISPLAY_BEHAVIOR_KINDS = {"data_filter", "display_toggle", "enable_toggle"}


@functools.lru_cache(maxsize=None)
def _react_behaviors() -> tuple:
    """react 下書きの behaviors[](処理の種別。_react_facts.behaviors が下書きに足す)を返す。下書きが無ければ空。
    WHY: 下書きの行範囲(src)で、文面が指す処理と同じ behavior を引くため。読み元は FEATURE_NAME の下書き1本(apply_react_draft と同じ)。"""
    draft = INTERMEDIATE_DIR / f"{FEATURE_NAME}_react_draft.json"
    if not draft.exists():
        return ()
    try:
        data = json.loads(draft.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    return tuple(b for b in data.get("behaviors") or [] if isinstance(b, dict))


def _behavior_kinds_for_src(src) -> frozenset:
    """src(行範囲の配列)と行範囲が重なる behavior の kind の集合を返す。
    WHY: verify_vs_source.check_kinds と同じ対の取り方(behavior の src と control_src の行範囲が、処理の src と重なる)にする。
         ここと検査で対の取り方が違うと、ここで通した kind を検査が ERROR にする。"""
    spans = [s for s in src or [] if isinstance(s, dict)]
    kinds = set()
    for b in _react_behaviors():
        bspans = [y for y in (b.get("src") or []) + (b.get("control_src") or []) if isinstance(y, dict)]
        if b.get("kind") and any(x.get("file") == y.get("file") and x["line_start"] <= y["line_end"] and y["line_start"] <= x["line_end"]
                                 for x in spans for y in bspans):
            kinds.add(b["kind"])
    return frozenset(kinds)


def _guess_step_kind(text: str, react_kinds: frozenset = frozenset()) -> str:
    """ステップ description から kind を推定する（v29 で追加）。
    react_kinds: 同じ処理の行範囲に重なる react behavior の kind（_behavior_kinds_for_src）。文面より優先して使う。

    キーワードマッチで業務分類:
    - API呼出 / fetch / POST / GET → backend_call
    - ダイアログ / 確認 → confirmation
    - リロード / 遷移 → transition
    - 更新 / 再描画 / 反映 / 計算セル → screen_update / calculation
    - チェック / 検証 / バリデーション → validation
    - 「ならば」「なら」「条件」「場合」 → branch
    - 「閲覧モード」「編集モード」 等 → branch
    - WebSocket 受信 / 通知 → event_emit
    - それ以外 → state_update

    calculation と branch の判定語には、project_config.json の step_kind_keywords
    （例: {"branch": ["案件固有のモード名"], "calculation": ["案件固有の金額項目名"]}）を足して判定する。
    """
    t = text or ""
    # 2026-10-05 汎用化: 案件固有の判定語を直書きせず、project_config の step_kind_keywords から足す。
    # 意味合い: 一般語だけを本体に残し、案件の業務語（モード名・金額項目名）は設定で与える。
    #          キーが無い案件は空扱いになり、一般語だけで判定する。
    # 接続情報: 読み元は _config_loader.load_project_config（target/intermediate/{機能名}_project_config.json。
    #          機能名ごとにキャッシュされ、ファイルが無ければ空 dict）。
    #          戻り値は _renumber_init_steps と _build_event_steps が front_processes[].steps[].kind に入れる。
    extra_keywords = load_project_config(FEATURE_NAME).get("step_kind_keywords", {})
    if any(kw in t for kw in ["API", "fetch", "POST", "GET ", "呼び出し", "呼出", "リクエスト"]):
        return "backend_call"
    if any(kw in t for kw in ["WebSocket", "通知を受信", "通知を送", "発火", "発信"]):
        return "event_emit"
    if any(kw in t for kw in ["確認ダイアログ", "ダイアログ表示", "ダイアログ「"]):
        return "confirmation"
    if any(kw in t for kw in ["リロード", "遷移", "画面へ", "別画面で起動"]):
        return "transition"
    if any(kw in t for kw in ["バリデーション", "チェック", "検証", "形式チェック", "重複チェック"]):
        # WHY: 「チェック」は表示の絞り込み・表示切替の文にも出る(例: 表示対象を選ぶチェックボックス)。
        #      同じ行範囲の behavior に入力検査(input_check)が無く、表示系(絞り込み・切替)だけなら入力検査にしない。
        #      入力検査にすると verify_vs_source.check_kinds が [種別] ERROR で止めるため。
        if react_kinds and "input_check" not in react_kinds:
            return "screen_update"
        return "validation"
    if any(kw in t for kw in ["計算セル", "計算式", "再計算"] + extra_keywords.get("calculation", [])):
        return "calculation"
    if any(kw in t for kw in ["再描画", "テーブル描画", "バナー表示", "バナーを消", "活性化", "非活性"]):
        return "screen_update"
    # B9: ファイル出力(ダウンロード・CSV・文字コード・見出し行・合計行・区切り)。branch より前に置く(「場合」を含むファイル出力の文を branch にしない)。
    # WHY: 「CSV出力ボタンが活性化する」等の画面の状態の文は file_output にしない(活性・押せの語を含む文は除く)。
    # WHY(U4): 引用符・BOM・Blob は CSV の書き方の語(二重引用符・合計行の引用符つきの空・BOM 付き UTF-8・Blob の作成)。手順の文を file_output にする。
    if any(kw in t for kw in ["ダウンロード", "ファイル", "CSV", "文字コード", "見出し行", "合計行", "区切り", "引用符", "BOM", "Blob"]) and not any(kw in t for kw in ["活性", "押せ"]):
        return "file_output"
    if any(kw in t for kw in ["なら", "ならば", "場合", "閲覧モード", "編集モード"] + extra_keywords.get("branch", [])):
        return "branch"
    return "state_update"


# WHY: ボタンの活性・押せる条件の語。verify_vs_source.py の画面規則の検査(U12)と同じ語で判定する。
#      この行が FR の steps に入ると U12 の検査で ERROR になって止まる。画面規則は VL と項目の備考に書く。
_SCREEN_RULE_WORDS = ("押せ", "押下でき", "活性", "無効", "disabled")


def _is_screen_rule_line(text: str) -> bool:
    """ボタンの活性・押せる条件の行か。案件の語では決め打ちせず、画面規則の語だけで判定する。"""
    return any(w in text for w in _SCREEN_RULE_WORDS)


def _build_event_steps(ep: dict, ev: dict) -> list:
    """event_processes[ev_code] の details / overview から front_processes[].steps[] を生成（v29 句点分解）。

    優先順序:
    1. ep.details があれば各行を1stepに（既存ロジック）
    2. ep.overview を句点（。）で分解して複数stepにする（v29 で追加）
    3. それも空なら「処理詳細未定義」1step

    各 step の kind は _guess_step_kind で内容から推定。
    """
    react_kinds = _behavior_kinds_for_src(ep.get("src") or ev.get("src") or [])
    details = ep.get("details", []) or []
    if details:
        steps = []
        for d in details:
            desc = str(d).lstrip("・-*•").strip()
            # WHY: モデルが画面規則を details に書いても、ここで機械的に外す(手順は処理本体だけにする)。step_no は詰め直す。
            if not desc or _is_screen_rule_line(desc):
                continue
            steps.append({
                "step_no": len(steps) + 1,
                "kind": _guess_step_kind(desc, react_kinds),
                "description": desc
            })
        if steps:
            return steps

    # overview を句点分解
    overview = (ep.get("overview", "") or "").strip()
    if not overview:
        return [{
            "step_no": 1,
            "kind": "screen_update",
            "description": "（処理詳細未定義）"
        }]

    # 「。」で分割、空行除去、各文を1step に
    sentences = [s.strip().lstrip("・-*•") for s in overview.split("。") if s.strip()]
    if not sentences:
        return [{
            "step_no": 1,
            "kind": "screen_update",
            "description": overview
        }]

    steps = []
    for i, sent in enumerate(sentences, 1):
        steps.append({
            "step_no": i,
            "kind": _guess_step_kind(sent, react_kinds),
            "description": sent
        })
    return steps


def _derive_short_overview(ep: dict) -> str:
    """overview を「業務目的の1文」に短縮（v29 で追加）。

    全文を steps に分解した結果、overview と steps[] の内容が完全重複する問題を解消するため、
    overview は「最初の句点まで」または「最大80文字」に絞る。
    steps[] が「何を実行するか」を担当、overview は「この処理の業務目的の概要」を担当。
    """
    text = (ep.get("overview", "") or "").strip()
    if not text:
        return ""
    # 最初の句点まで
    idx = text.find("。")
    if idx > 0:
        return text[:idx]
    if len(text) > 80:
        return text[:80] + "…"
    return text


# v30: PROCESS_NAMES_BY_EVENT_CODE はモジュール冒頭で screen_config.json から動的取得済み
# （旧版のハードコード辞書46件をここに展開していたが、外部設定化により削除）


def _derive_process_name(ep: dict, ev: dict) -> str:
    """process 名を業務担当者語彙で決める（v19 簡潔化対応、v29 手動マッピング優先）。

    優先順序:
    1. ev.content の最初の句点まで（または改行まで）、最大30文字
    2. location[最後] + action から組み立て（最大30文字）
    3. event_code

    意味合い: docx §6.1.X.N の見出しに表示される短い処理名を生成する。
              長い処理内容説明は overview に書き、name は識別しやすい短さに保つ。
              v17 まで content 先頭40文字をそのまま使っていた結果、句点で切れて意味不明な見出し
              （例: 「データを作成する。完了すると一覧に番号がリンク表示され、クリックで詳」）
              になっていた問題への対処。
    """
    MAX_LEN = 30

    # v29: event_code 手動マッピング優先（business-friendly な短縮名）
    ev_code = ev.get("event_code", "")
    if ev_code in PROCESS_NAMES_BY_EVENT_CODE:
        return PROCESS_NAMES_BY_EVENT_CODE[ev_code]

    def _trim_to_first_sentence(text: str) -> str:
        """最初の句点 or 改行までを抽出し、最大MAX_LEN 文字に切り詰める。"""
        if not text:
            return ""
        s = str(text).strip()
        # 「。」「\n」「、」（最後の手段）で文を区切る
        for sep in ("。", "\n"):
            idx = s.find(sep)
            if idx > 0:
                s = s[:idx]
                break
        # 文末の余分な記号を除去
        s = s.rstrip("、,；; ")
        # 最大長で切る
        if len(s) > MAX_LEN:
            s = s[:MAX_LEN] + "…"
        return s

    content = ev.get("content", "")
    if content:
        name = _trim_to_first_sentence(content)
        if name:
            return name

    action = ev.get("action", "")
    location = ev.get("location", []) or []
    if action and location:
        name = f"{location[-1]} を {action}"
        return name[:MAX_LEN] + ("…" if len(name) > MAX_LEN else "")
    return ep.get("event_code", "（名称未設定）")


def _derive_called_by_triggers(ev: dict, default_screen_name: str = "") -> list:
    """events[] のイベントから called_by_triggers の参照を導く。
    classify_trigger_kind と一致したロジックで kind/category を判定。
    trigger_no は build_trigger_groups の後で再付与するため、ここでは event_code を残してプレースホルダ的に使う。
    """
    kind, category = classify_trigger_kind(ev, default_screen_name)
    return [{"group_kind": kind, "group_category": category, "_event_code_hint": ev.get("event_code", "")}]


def resolve_trigger_no(front_processes: list, trigger_groups: list) -> None:
    """called_by_triggers[].trigger_no を build 後の trigger_groups から逆引きして補完。
    _event_code_hint を使って group 内の該当 trigger を見つける。
    """
    # (kind, category, event_description) → trigger_no のマップを作る
    # トリガー側は description に events[].trigger を入れているので、それで突合
    # ただし v17 events と v18 trigger_groups の description は同じテキストなので、対応イベントの trigger 文字列で照合する
    # しかし event_code_hint からは元イベント description が直接取れないので、シンプルに kind+category 内連番で割り当てる
    for fp in front_processes:
        for ref in fp.get("called_by_triggers", []) or []:
            kind = ref.get("group_kind")
            category = ref.get("group_category")
            hint = ref.pop("_event_code_hint", None)
            # 該当 group を探す
            target_group = None
            for g in trigger_groups:
                if g.get("kind") == kind and g.get("category") == category:
                    target_group = g
                    break
            if target_group is None:
                ref["trigger_no"] = None
                continue
            # group 内で hint と一致する trigger を探す
            # （hint は event_code、trigger の calls に F* が入っており、F* は event_code から一意なので逆引き可能）
            found_no = None
            if hint:
                # event_to_front_id を別に持ってないので、シンプルに「最初の trigger で calls に fp.process_id が含まれるもの」を返す
                for t in target_group.get("triggers", []):
                    if fp.get("process_id") in (t.get("calls") or []):
                        found_no = t.get("trigger_no")
                        break
            if found_no is None and target_group.get("triggers"):
                found_no = target_group["triggers"][0].get("trigger_no", 1)
            ref["trigger_no"] = found_no or 1


def _derive_backend_area(api: dict) -> str:
    """バックエンド処理の area を業務機能カテゴリで推定する（v19）。

    判定優先順序（action 名で1次判定、description は2次判定）:
    1. action 名（HTTP の動詞）で判定 — APIの実体に最も近い
       - "lock" → "排他制御"
       - "save" / "update" / "delete" / "create" / "insert" / "upsert" → "データ更新"
       - "suggest" → "マスタ参照"
       - "get" / "fetch" / "search" / "list" → "データ取得"(list は 2026-10-09 追加。list_* の一覧取得 API 用)
    2. description キーワードで判定（action 名が不明確な場合のフォールバック）
       - 「ロック」→ "排他制御"
       - 「更新/登録/削除/保存/確定/UPSERT」かつ「取得/参照/復元」を含まない → "データ更新"
       - 「サジェスト/マスタ全件」→ "マスタ参照"
       - 「取得/参照/復元/一覧」→ "データ取得"
    3. fallback → "その他"

    注意: description のキーワードは保存済みデータ等の「保存」「更新」が出現するため、
          action 名を優先することで誤判定を防ぐ（例: action が 'get_...' で description が
          "保存済みデータを返す" の処理は、action='get' のため "データ取得" になる）
    """
    action = (api.get("action", "") or "").lower()
    desc = api.get("description", "") or ""

    # 1次判定: action 名
    if "lock" in action:
        return "排他制御"
    if any(kw in action for kw in ["save", "update", "delete", "create", "insert", "upsert"]):
        return "データ更新"
    if "suggest" in action:
        return "マスタ参照"
    # 2026-10-09: "list" を追加。
    # 目的: list_sales_report のような list_* の action を「データ取得」に分類する。
    # 意味合い: 従来は get/fetch/search のどれにも当たらず description の2次判定に落ち、
    #   「その他」になっていた。更新系・suggest の判定より後に置くので、既存 action の分類順は変わらない。
    # 接続情報: 戻り値は build_backend_processes が backend_processes[].area に入れ、
    #   少なくとも generate_docx.js の BACKEND_AREA_ORDER(「データ取得」を含む)が並び順に使う。
    if any(kw in action for kw in ["get", "fetch", "search", "list"]):
        return "データ取得"

    # 2次判定: description
    if "ロック" in desc:
        return "排他制御"
    if any(kw in desc for kw in ["サジェスト", "マスタ全件"]):
        return "マスタ参照"
    if any(kw in desc for kw in ["取得", "参照", "復元", "一覧"]):
        return "データ取得"
    # 2026-10-05 汎用化: 特定案件の業務語（金額項目名）を判定語から外し、一般語だけで "データ更新" を判定する
    # 接続: この戻り値は build_backend_processes が backend_processes[].area に入れる
    if any(kw in desc for kw in ["更新", "登録", "削除", "確定", "UPSERT"]):
        return "データ更新"
    return "その他"


def _derive_backend_name(api: dict) -> str:
    """バックエンド処理の name を業務担当者語彙で簡潔に決める（v19 で追加）。

    優先順序:
    1. description の最初の句点まで（最大30文字、超えたら "…" 付加）
    2. action から動詞名詞化（"item_save" → "データを保存する"）
    3. fallback: action そのまま
    """
    MAX_LEN = 30
    desc = api.get("description", "") or ""
    if desc:
        s = desc.strip()
        # 最初の句点 or 改行で切る
        for sep in ("。", "\n", "（"):
            idx = s.find(sep)
            if idx > 0:
                s = s[:idx]
                break
        s = s.rstrip("、,；; ")
        if len(s) > MAX_LEN:
            s = s[:MAX_LEN] + "…"
        return s
    action = api.get("action", "")
    return action or "（名称未設定）"


def build_backend_processes(api_spec: list, db_operations: list) -> tuple:
    """api_spec[] と db_operations[] から v18 backend_processes[] を生成。

    1 API endpoint (method + path + action) = 1 backend_process の原則で B001 から採番。
    db_operations は api_endpoint.action マッチで該当 backend_process の processing[].db_op_detail に分配。
    """
    backend_processes = []
    api_to_id = {}  # action (or path) → B00X
    api_to_db_ops = {}  # action → [db_op...]

    # api_endpoint.action から db_operations を逆引きできるようにマップ作成
    for op in db_operations:
        ep = op.get("api_endpoint", {})
        action = ep.get("action", "")
        if action:
            api_to_db_ops.setdefault(action, []).append(op)

    idx = 1
    for api in api_spec:
        process_id = f"B{idx:03d}"
        idx += 1
        action = api.get("action", "")
        api_to_id[action] = process_id

        # processing[] を構築
        processing = [
            {"step_no": 1, "kind": "validation", "description": "リクエストパラメータバリデーション（必須/型）"}
        ]

        # cache 操作（lock 系等）
        cache_ops = api.get("related_cache_operations", []) or []
        for c in cache_ops:
            processing.append({
                "step_no": len(processing) + 1,
                "kind": "cache_op",
                "description": c.get("purpose", "キャッシュ操作"),
                "cache_op": {
                    "operation": c.get("operation", ""),
                    "key_pattern": c.get("key_pattern", ""),
                    "ttl_sec": c.get("ttl_sec"),
                    "purpose": c.get("purpose", "")
                }
            })

        # DB 操作
        related_dbs = api.get("related_db_operations", []) or []
        for db_id in related_dbs:
            # db_operations から該当 op を取得
            db_op = next((o for o in db_operations if o.get("id") == db_id), None)
            if db_op is None:
                continue
            processing.append({
                "step_no": len(processing) + 1,
                "kind": "db_operation",
                "description": f"{db_op.get('operation_type','')}: {db_op.get('dataset_name','')}",
                "db_op_detail": {
                    "id": db_id,
                    "operation_type": db_op.get("operation_type", ""),
                    "dataset_name": db_op.get("dataset_name", ""),
                    "transaction": db_op.get("transaction", False),
                    "dataset": db_op.get("dataset", []),
                    "joins": db_op.get("joins", []),
                    "where": db_op.get("where", []),
                    # E3: GROUP BY・集計(関数・対象・FILTER・OVER・別名・範囲)・HAVING・別名の式も落とさず届ける(generate_docx.js の renderDbOperationDetail が描く)
                    "group_by": db_op.get("group_by", []),
                    "aggregate": db_op.get("aggregate", []),
                    "having": db_op.get("having", []),
                    "aliases": db_op.get("aliases", []),
                    "order_by": db_op.get("order_by", []),
                    "insert_values": db_op.get("insert_values", []),
                    "update_values": db_op.get("update_values", []),
                    "cte_definitions": db_op.get("cte_definitions", []),
                    # B9: 共通表式(WITH)と副問合せを docx へ届ける(generate_docx.js の renderDbOperationDetail が描く)
                    # WHY: 届けないと WITH 節と副問合せが docx から落ちる(cte_definitions とは別の欄)
                    "ctes": db_op.get("ctes", []),
                    "subqueries": db_op.get("subqueries", []),
                    "remarks": db_op.get("remarks", ""),
                    "error_message_id": db_op.get("error_message_id", ""),
                    "src": db_op.get("src", [])  # ソース出典(A3)。合成の _fill_src が付けた値を落とさず届ける
                }
            })

        processing.append({
            "step_no": len(processing) + 1,
            "kind": "response_build",
            "description": "レスポンス組立"
        })

        # name と overview が同じ文にならないようにする（verify_intermediate.py が同文を WARN にする）。
        # api_spec[].name（任意）があればそれを name に使い、overview は description のまま。
        # 無ければ name は description の1文目から作るので、overview は2文目以降があればそちらを使う。
        api_name = str(api.get("name") or "").strip()
        desc = api.get("description", "") or ""
        if api_name:
            overview = desc
        else:
            _, sep, rest = desc.partition("。")
            overview = rest.strip() if sep and rest.strip() else desc
        bp_entry = {
            "process_id": process_id,
            "name": api_name or _derive_backend_name(api),
            "area": _derive_backend_area(api),
            "overview": overview,
            "called_by_front": [],  # 後で front_processes から逆引き
            "endpoint": {
                "method": api.get("method", ""),
                "path": api.get("path", ""),
                "action": action,
                "auth": api.get("auth", "")
            },
            "request_spec": {
                "params": api.get("request_params", []) or []
            },
            "processing": processing,
            "response_spec": {
                "pattern": api.get("response_pattern", ""),
                "schema": api.get("response_schema", {}),
                "items": api.get("response_items", []) or [],
                "response_to_screen_mapping": api.get("response_to_screen_mapping", []) or []
            },
            # api_spec[].error_patterns を写す。目的: 中間JSONのエラー定義を backend_processes に届ける。
            # 接続: 呼出元は build_backend_processes。api_spec に無い案件は空配列のまま。
            "error_patterns": api.get("error_patterns", []) or [],
            "src": api.get("src", [])  # ソース出典(A3)。api_spec の src を backend_processes へ届ける
        }
        # error_patterns を空にする理由（任意）。verify_intermediate.py は、空の error_patterns をこれがあるときだけ通す
        if api.get("error_patterns_none_reason"):
            bp_entry["error_patterns_none_reason"] = api["error_patterns_none_reason"]
        backend_processes.append(bp_entry)

    return backend_processes, api_to_id


def fill_event_backend_calls(front_processes: list, event_to_front_id: dict, db_operations: list, api_to_id: dict) -> int:
    """初期処理以外のイベントのフロント処理で、バックエンド呼出の step に backend_call.backend_id を補う。

    目的: _build_event_steps が作る step は kind と description だけで backend_call を持たない。
          db_operations[].api_endpoint の {trigger_event, action} と trigger_events（event_code の配列、多対一用。2026-10-09）を手掛かりに、
          「どのイベントがどの API を呼ぶか」を step.backend_call = {"backend_id": "B###"} として書き込む。
    意味合い: 初期処理は screen_config.json の initial_processes[].steps[].backend_call で backend_id を与えられるが、
              それ以外のイベントには設定にも Phase 1 の成果にも backend_id を書く場所が無い。
              ここで補わないと、画面側の処理とバックエンドの処理が結び付かない。
              step の形は initial_processes の step が持つ backend_call に合わせる（ここで入れるのは backend_id だけ）。
    規則:
    - 対象は kind == "backend_call" で backend_call が未設定の step だけ（既にある値は上書きしない）。
    - trigger_event が "EV00" のものは扱わない（初期処理は設定の記述をそのまま使う）。trigger_events の各要素も同じ。
    - 呼出元イベントは trigger_event と trigger_events の和。同じイベントに同じ B### は1回だけ入れる。
    - 同じイベントに複数の action があれば、db_operations の出現順（同じ action は1回）で
      backend_call の step へ前から割り当てる。step が足りなければ余りは補わず、action が足りなければ残りの step は空のまま。
    - api_endpoint が無い・null・action が api_spec に無い db_operations は何もしない。
    接続情報:
    - 呼出元: main（build_backend_processes の後、link_backend_to_front の前）。
    - 入力: event_to_front_id は build_front_processes の戻り値（event_code → F###）、
            api_to_id は build_backend_processes の戻り値（action → B###）。
            trigger_event と trigger_events の各要素は Phase 1 の events[].event_code と同じ値（merge_partials.py が db_operations_ref を作るときと同じキー。読み方も単数 ∪ 配列で揃える）。
    - 後段: link_backend_to_front がこの backend_id を見て backend_processes[].called_by_front に登録する。
            apply_id_scheme.py の update_cross_references は backend_id の値を新しい ID 体系へ置き換える。
            migrate_v48_to_v49.py は backend_call の step の backend_id を見て、その直後に response_mapping の step を挿入する。
    返り値: 補った step の数。
    """
    # 注: step と action の対応は「出現順」で決める単純な割当。kind は _guess_step_kind の語による推定なので、
    #     API を呼ばない step が backend_call と推定された場合は順番がずれる。
    #     ずれが問題になったら、event_processes[].details の書き方（API を呼ぶ行だけに呼出の語を書く）で合わせる。
    ids_by_front = {}  # F### → [B###, ...]（db_operations の出現順、重複なし）
    for op in db_operations:
        ep = op.get("api_endpoint") or {}
        action = ep.get("action", "")
        if not action or action not in api_to_id:
            continue
        # 2026-10-09 多対一対応:
        # 目的: 呼出元イベント集合を、単数 trigger_event と配列 trigger_events(event_code の配列)の和として作る。
        # 意味合い: 同じ action を複数イベントから呼ぶ形(例: 一覧取得 API を検索・並べ替え・ページ送りから呼ぶ)を
        #           Phase 1 の api_endpoint だけで書けるようにする。単数だけなら従来と同じ1要素で、結果も従来どおり。
        #           EV00・event_to_front_id に無い event_code の除外は各要素に適用し、重複は下の ids の判定で吸収する。
        # 接続情報: 少なくとも merge_partials.py の build_event_processes が同じ集合(単数 ∪ 配列)で db_operations_ref を作る。
        #           ここで入れた backend_id は、少なくとも link_backend_to_front が called_by_front へ逆引き登録する。
        ev_codes = [ep.get("trigger_event", "")] + list(ep.get("trigger_events") or [])
        for ev_code in ev_codes:
            if not ev_code or ev_code == "EV00" or ev_code not in event_to_front_id:
                continue
            ids = ids_by_front.setdefault(event_to_front_id[ev_code], [])
            if api_to_id[action] not in ids:
                ids.append(api_to_id[action])

    filled = 0
    for fp in front_processes:
        ids = iter(ids_by_front.get(fp.get("process_id"), []))
        for st in fp.get("steps", []) or []:
            if st.get("kind") != "backend_call" or st.get("backend_call"):
                continue
            bid = next(ids, None)
            if bid is None:
                break
            st["backend_call"] = {"backend_id": bid}
            filled += 1
    return filled


def link_backend_to_front(front_processes: list, backend_processes: list, api_to_id: dict) -> None:
    """front_processes[].steps[].backend_call.backend_id を api_to_id で解決し、
    backend_processes[].called_by_front[] に逆引き登録する。
    """
    # 2026-10-05 汎用化: backend_id は screen_config.json の initial_processes[].steps[].backend_call などで
    # 与えられたものを解決する（旧版が F001 に固定の backend_id を埋め込んでいた処理は廃止した）。
    # backend_call を持たない step が多い想定で、その場合は逆引きを登録しない。
    backend_id_to_process = {bp["process_id"]: bp for bp in backend_processes}

    for fp in front_processes:
        for st in fp.get("steps", []) or []:
            bc = st.get("backend_call")
            if not bc:
                continue
            bid = bc.get("backend_id")
            if bid and bid in backend_id_to_process:
                bp = backend_id_to_process[bid]
                if fp["process_id"] not in (bp.get("called_by_front") or []):
                    bp.setdefault("called_by_front", []).append(fp["process_id"])


def main():
    if not INTERMEDIATE_JSON.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {INTERMEDIATE_JSON}", file=sys.stderr)
        sys.exit(1)

    with INTERMEDIATE_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    events = data.get("events", []) or []
    event_processes = data.get("event_processes", []) or []
    initialization = data.get("initialization", {}) or {}
    api_spec = data.get("api_spec", []) or []
    db_operations = data.get("db_operations", []) or []

    # meta.feature_name から「画面」名を導出。screen_operation の category フォールバック用
    feature_name = data.get("meta", {}).get("feature_name", "")
    default_screen_name = f"{feature_name}画面" if feature_name and "画面" not in feature_name else (feature_name or "画面")

    # Step 1: front_processes 生成（event_code → F00X のマップも返す）
    front_processes, event_to_front_id = build_front_processes(event_processes, events, initialization, default_screen_name)

    # Step 2: trigger_groups 生成（event_to_front_id を使って calls を解決）
    trigger_groups = build_trigger_groups(events, event_to_front_id, default_screen_name)

    # Step 3: called_by_triggers の trigger_no を補完
    resolve_trigger_no(front_processes, trigger_groups)

    # Step 4: backend_processes 生成
    backend_processes, api_to_backend_id = build_backend_processes(api_spec, db_operations)

    # Step 4.5: 初期処理以外のイベントの backend_call step に backend_id を補う
    # 意味合い: Step 5 は step の backend_call.backend_id だけを見て結ぶので、その前に補っておく。
    # 接続情報: 入力は Step 1 の event_to_front_id と Step 4 の api_to_backend_id。件数は下のサマリ出力に出す。
    filled_backend_calls = fill_event_backend_calls(front_processes, event_to_front_id, db_operations, api_to_backend_id)

    # Step 5: front_processes ⇔ backend_processes の双方向リンク
    link_backend_to_front(front_processes, backend_processes, api_to_backend_id)

    # 中間JSONに v18 キーを追加（v17 キーは保持）
    data["trigger_groups"] = trigger_groups
    data["front_processes"] = front_processes
    data["backend_processes"] = backend_processes

    with INTERMEDIATE_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # サマリ出力
    print("[OK] v17 → v18 変換完了")
    print(f"  trigger_groups: {len(trigger_groups)} 群")
    for g in trigger_groups:
        print(f"    - [{g['kind']}] {g['category']}: {len(g['triggers'])} triggers")
    print(f"  front_processes: {len(front_processes)} 処理 (F001〜F{len(front_processes):03d})")
    print(f"  backend_processes: {len(backend_processes)} 処理 (B001〜B{len(backend_processes):03d})")
    print(f"  backend_call の補完（初期処理以外のイベント）: {filled_backend_calls} 件")
    print(f"[OK] 保存先: {INTERMEDIATE_JSON}")


def selftest() -> int:
    """F1 の検査: 文面の「チェック」を、react 下書きの behavior の事実で入力検査にするかを決める。
    behaviors は _react_behaviors を差し替えて与える(実ファイルに依らない)。WHY: 実ファイルで試すと案件ごとに結果が変わるため。"""
    global _react_behaviors
    real = _react_behaviors

    def span(a, b):
        return [{"file": "PosReport.tsx", "line_start": a, "line_end": b}]

    beh = ({"kind": "data_filter", "src": span(124, 124), "control_src": span(276, 276)},
           {"kind": "input_check", "src": span(200, 205), "control_src": []})
    try:
        _react_behaviors = lambda: beh
        # 下書きの事実が無ければ文面どおり(従来の動き)
        assert _guess_step_kind("表示対象のチェックを切り替える") == "validation"
        # 絞り込みの behavior だけなら validation にしない
        assert _guess_step_kind("表示対象のチェックを切り替える", frozenset({"data_filter"})) == "screen_update"
        assert _guess_step_kind("表示対象のチェックを切り替える", frozenset({"display_toggle"})) == "screen_update"
        # 入力検査が1つでもあれば文面どおり validation
        assert _guess_step_kind("入力値を検証する", frozenset({"input_check"})) == "validation"
        assert _guess_step_kind("入力値を検証する", frozenset({"data_filter", "input_check"})) == "validation"
        # 文面が別の kind を指すものは変えない
        assert _guess_step_kind("API を呼び出す", frozenset({"data_filter"})) == "backend_call"
        # 行範囲が重なる behavior を引く(control_src も対象)
        assert _behavior_kinds_for_src(span(270, 280)) == frozenset({"data_filter"})
        assert _behavior_kinds_for_src(span(200, 201)) == frozenset({"input_check"})
        assert _behavior_kinds_for_src(span(10, 20)) == frozenset()
        assert _behavior_kinds_for_src([]) == frozenset()
        # _build_event_steps: details と overview の両方で、ep の src から behavior を引く
        s1 = _build_event_steps({"event_code": "EV05", "details": ["・表示対象のチェックを切り替える"], "src": span(270, 280)}, {})
        assert [s["kind"] for s in s1] == ["screen_update"], s1
        s2 = _build_event_steps({"event_code": "EV06", "details": ["・入力値を検証する"], "src": span(200, 201)}, {})
        assert [s["kind"] for s in s2] == ["validation"], s2
        s3 = _build_event_steps({"event_code": "EV07", "details": ["・表示対象のチェックを切り替える"], "src": span(10, 20)}, {})
        assert [s["kind"] for s in s3] == ["validation"], s3
        s4 = _build_event_steps({"event_code": "EV05", "overview": "表示対象のチェックを切り替える。", "src": span(270, 280)}, {})
        assert [s["kind"] for s in s4] == ["screen_update"], s4
        # U12 後続: 押せる・活性・無効の行は手順に入れない(表示の行は残す)。step_no は詰め直す
        s5 = _build_event_steps({"event_code": "EV08", "details": ["・集計結果が0件のときは CSV 出力ボタンを押せないようにする", "・結果の一覧を表示する", "・出力ボタンは活性にする", "・無効な行は除く"], "src": span(10, 20)}, {})
        assert [s["description"] for s in s5] == ["結果の一覧を表示する"], s5
        assert [s["step_no"] for s in s5] == [1], s5
        s6 = _build_event_steps({"event_code": "EV09", "details": ["・ボタンを押せないようにする", "・取得を始める", "・結果を表示する"], "src": span(10, 20)}, {})
        assert [s["step_no"] for s in s6] == [1, 2] and [s["description"] for s in s6] == ["取得を始める", "結果を表示する"], s6
    finally:
        _react_behaviors = real

    # B9 b: file_output(ファイル出力)。活性・押せの文は file_output にしない。branch より前
    assert _guess_step_kind("結果をCSVファイルとしてダウンロードする") == "file_output"
    assert _guess_step_kind("ダウンロード先を選ぶ場合は区切りを指定する") == "file_output"
    assert _guess_step_kind("CSV出力ボタンを活性化する") == "screen_update"
    assert _guess_step_kind("CSV出力ボタンを押せない") != "file_output"
    assert _guess_step_kind("店舗が指定された場合は絞り込む") == "branch"
    # U4(新6後半): 引用符・BOM・Blob を含む CSV の手順は file_output
    assert _guess_step_kind("二重引用符は2つ重ねる") == "file_output"
    assert _guess_step_kind("文字コードは UTF-8 BOM 付きにする") == "file_output"
    assert _guess_step_kind("Blob を作ってダウンロードする") == "file_output"

    # B9 a: build_backend_processes が db_op_detail に ctes・subqueries を渡す
    bps, _ids = build_backend_processes(
        [{"action": "load", "method": "GET", "path": "/a", "description": "一覧を取得する。", "related_db_operations": ["DB1"]}],
        [{"id": "DB1", "api_endpoint": {"action": "load"}, "operation_type": "select",
          "ctes": [{"name": "c1"}], "subqueries": [{"clause": "where"}]}])
    det = next(p for p in bps[0]["processing"] if p["kind"] == "db_operation")["db_op_detail"]
    assert det["ctes"] == [{"name": "c1"}] and det["subqueries"] == [{"clause": "where"}], det

    # B9 c: 初期処理の backend_call の前後に EV00 の開始時・失敗時の step を入れる(件数が合うときだけ)
    fe = {"event": "EV00", "call": "load", "before_call": ["loading=true", "error=null"], "on_success": ["rows"],
          "on_failure": ["error"], "in_finally": ["loading=false"], "untouched_on_failure": ["rows"],
          "failure_text": "String(e)", "failure_state": "error", "src": [{"file": "x", "line_start": 1, "line_end": 1}]}
    base = [{"kind": "backend_call", "description": "店舗一覧を取得する", "backend_call": {"backend_id": "B001"}}]
    out = _with_init_fact_steps(base, [fe])
    assert [s["kind"] for s in out] == ["state_update", "backend_call", "state_update", "state_update"], out
    assert "取得の開始時に" in out[0]["description"] and "取得に成功したときは" in out[2]["description"] \
        and "取得に失敗したときは" in out[3]["description"], out
    assert _with_init_fact_steps(base, []) == base
    assert _with_init_fact_steps(base, [fe, fe]) == base
    # 既に同じ趣旨の文があれば足さない
    pre = [{"kind": "state_update", "description": "取得の開始時に、loading を true にする"}] + base
    assert len(_with_init_fact_steps(pre, [fe])) == 4
    # N7: 並行は「開始時・呼出」を先に全部出し、応答側(成功・失敗・後続の step)を後ろにまとめる。順(serial)は従来どおり
    fe2 = dict(fe, call="load2", on_success=["shops"], on_failure=["error2"])
    two = [{"kind": "backend_call", "description": "店舗一覧を取得する", "backend_call": {"backend_id": "B001"}},
           {"kind": "state_update", "description": "店舗の応答を画面に反映する"},
           {"kind": "backend_call", "description": "会計レポートを取得する", "backend_call": {"backend_id": "B002"}},
           {"kind": "state_update", "description": "会計レポートの応答を表示する"}]
    # U4(N7): 並行の並べ替えは v17 では行わない(mode に関係なく呼出ごとの順のまま。並べ替えは v49 の reorder_parallel_fetch)
    ser = _with_init_fact_steps(two, [fe, fe2], "serial")
    par = _with_init_fact_steps(two, [fe, fe2], "parallel")
    assert par == ser, par
    assert [i for i, s in enumerate(ser) if s["kind"] == "backend_call"] == [1, 6], ser
    assert ser[2]["description"].startswith("取得に成功") and ser[3]["description"].startswith("取得に失敗") \
        and ser[4]["description"] == "店舗の応答を画面に反映する", ser
    assert _with_init_fact_steps(two, [fe], "parallel") == two
    print("[selftest OK] migrate_v17_to_v18.step_kind")
    print("[selftest OK] migrate_v17_to_v18.db_op_detail_ctes")
    print("[selftest OK] migrate_v17_to_v18.init_fact_steps")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
