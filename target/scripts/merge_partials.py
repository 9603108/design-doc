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
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from _initial_eval import build_env, chain_value, disabled_value, linked_fixed_elements, literal_text_elements, visible_initially
# WHY: JSX の {式} の値を、初期処理の評価器で決める（式の評価を二重に持たない）
from _initial_eval import _value
# WHY: 画面項目の src（TSX の行範囲）から、表示条件・活性・文言を位置で結ぶ（round10 T15）
from _jsx_chain import resolve_for_lines, text_of_lines
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


def load_react_drafts() -> list:
    """react 下書き（<画面>_react_draft.json）を target/intermediate/ から全部読む。読めないファイルは飛ばす。
    呼出元: build_initialization。意味合い: 初期値（useState の初期値）と初期表示の API は下書きの事実が正。
    partial の events には action が無い形（run7 の形）があるため、初期表示の判定もここの下書きから取る。下書きが無ければ []。"""
    drafts = []
    for p in sorted(INTERMEDIATE_DIR.glob("*_react_draft.json")):
        try:
            drafts.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return drafts


def _state_by_label(drafts: list) -> dict:
    """画面項目の名前 → その項目に結ばれた state_initials の1件。同じ名前は最初の1件（_units._fill_initial_values と同じ辿り方）。"""
    out = {}
    for d in drafts:
        for si in d.get("state_initials") or []:
            for b in si.get("bound") or []:
                if isinstance(b, dict) and b.get("label"):
                    out.setdefault(str(b["label"]).strip(), si)
    return out


def _option_texts(drafts: list) -> dict:
    """選択欄の名前 → {option の value: 表示名}。下書きの jsx_elements が options を持つものだけ入る。
    意味合い: 選択欄の初期値（例 "register"）を業務の表示名（例 レジ別）へ写すための事実。無ければ空にして、推測で言い換えない。
    接続情報: 下書きの jsx_elements（_react_facts.jsx_elements の select の options）が入る。map だけで作る選択肢は options が空なので入らず、
              その場合は引用符を外した値を書く（表示名を推測で足さない）。"""
    out = {}
    for d in drafts:
        for el in d.get("jsx_elements") or []:
            if not isinstance(el, dict) or not el.get("label") or not el.get("options"):
                continue
            pairs = {str(o["value"]): str(o.get("text") or "") for o in el["options"] if isinstance(o, dict) and "value" in o}
            out.setdefault(str(el["label"]).strip(), pairs)
    return out


def _initial_text_from_state(si: dict, options: dict) -> str:
    """state_initials の1件を、初期処理の表の「初期値」欄の文にする。
    リテラル（引用符付き）: 選択肢の事実（value→表示名）があれば表示名、無ければ引用符を外した値。空文字は「空文字」。
    リテラル（その他）: そのまま（true・false など）。式: 読める説明（readable）、無ければ「式: 原文」。未設定: 「未設定(undefined)」。
    意味合い: 業務担当者が読む表なので、value の綴り（register）より表示名（レジ別）を優先する。"""
    kind, raw = si.get("kind"), si.get("initial") or ""
    if kind == "none":
        return "未設定(undefined)"
    if kind == "literal":
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            val = raw[1:-1]
            if options.get(val):
                return options[val]
            return val if val else "空文字"
        return {"[]": "空の一覧"}.get(raw, raw)
    return si.get("readable") or f"式: {raw}"


def _all_states(drafts: list) -> list:
    """全ての react 下書きの state_initials を1本の並びにする（評価の環境に使う）。"""
    return [si for d in drafts for si in d.get("state_initials") or [] if isinstance(si, dict)]


def _all_derived(drafts: list) -> list:
    """全ての react 下書きの derived_arrays を1本の並びにする。B5 の配線で入る欄。無い下書きは飛ばす（推測で埋めない）。"""
    return [da for d in drafts for da in d.get("derived_arrays") or [] if isinstance(da, dict)]


def _elements_by_name(drafts: list) -> dict:
    """画面項目の名前 → その名前の jsx 要素の一覧（label か text が一致）。表示条件（visible_chain）と活性（disabled_chain）を見るため。"""
    out = {}
    for d in drafts:
        for el in d.get("jsx_elements") or []:
            if not isinstance(el, dict):
                continue
            key = str(el.get("label") or el.get("text") or "").strip()
            if key:
                out.setdefault(key, []).append(el)
    return out


_VIS_LABEL = {True: "表示", False: "非表示", None: "条件付き(式)"}


def _visibility(elements: list, env: dict) -> tuple:
    """画面項目の（活性, 表示）。結ぶ jsx 要素が無ければ仮埋め（活性・表示）のまま。
    要素が複数あって結果が割れるときは「条件付き(式)」にする（どれが画面の項目かを推測しない）。
    WHY: 表示条件の鎖・disabled の式を state の初期値で評価し、評価できないものは「条件付き(式)」と書く。"""
    if not elements:
        return "活性", "表示"  # 仮埋め（結ぶ要素が無い項目。B16 が全項目一色を検出する）
    # WHY: 鎖が無い要素は visible_if（条件の式）で初期表示を決める。鎖だけを見ると「読込中…」が出ている扱いになる（run9）
    visible = {_VIS_LABEL[visible_initially(el, env)] for el in elements}
    active = {
        {True: "非活性", False: "活性", None: "条件付き(式)"}[disabled_value(el.get("disabled_chain"), env)]
        for el in elements
    }
    return (active.pop() if len(active) == 1 else "条件付き(式)",
            visible.pop() if len(visible) == 1 else "条件付き(式)")


def _tsx_src(sl_item: dict):
    """画面項目の src のうち、読める TSX/JSX の1件（file・line_start・line_end）。無ければ None。
    WHY: 項目の JSX の位置から表示条件を辿るため。src が無い・TSX でない・ファイルが無い項目は名前での結びに戻す。"""
    srcs = sl_item.get("src") or []
    if isinstance(srcs, dict):
        srcs = [srcs]
    for s in srcs:
        if not isinstance(s, dict) or not s.get("line_start"):
            continue
        f = str(s.get("file") or "")
        if f.endswith((".tsx", ".jsx")) and Path(f).is_file():
            return s
    return None


def _text_value(expr: str, env: dict):
    """JSX の {式} の値を文字列にする。評価できない式・配列・真偽は None（{式} のまま残す）。"""
    v = _value(expr, env)
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


_BR = re.compile(r"<br\s*/?>")
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")


def _brace_text(expr: str, env: dict) -> str:
    """文言の中の {式} を値に直す。評価できなければ {式} のまま残す（推測しない）。"""
    v = _text_value(expr, env)
    if v is None:
        return "{" + expr + "}"
    return v


def _range_text(file: str, start: int, end: int, env: dict) -> str:
    """範囲の文言を、タグを外して取る。範囲が要素の途中の文言だけ（囲む要素の開始タグが範囲に無い）のときに使う。
    WHY: 長い注記は行の途中から始まる。text_of_lines は範囲の最初の要素のタグから読むため、この形では文言を取れない。"""
    lines = Path(file).read_text(encoding="utf-8").split("\n")[start - 1:end]
    s = _BR.sub(" ", " ".join(lines))
    s = re.sub(r"\{([^{}]*)\}", lambda mm: _brace_text(mm.group(1), env), s)
    return re.sub(r"\s+", " ", _TAG.sub("", s)).strip()


def _is_array_use(r: dict) -> bool:
    """鎖が配列の使用箇所から来ているか（その行は配列の要素で、画面の文言ではない）。"""
    chain = r["visible_chain"]
    return bool(chain) and chain[0].get("kind") == "array_use"


def _src_fact(sl_item: dict, env: dict, elements: list):
    """src の JSX から、活性・表示・文言を事実で決める。TSX の src が使えないときは None（名前での結びに戻す）。
    WHY: 名前で結ぶと要素が見つからない項目が半数を超えた（round10 の 6）。位置で結べば、部品の呼出元・配列の使用箇所も辿れる。"""
    s = _tsx_src(sl_item)
    if s is None:
        return None
    # fallback-ok: 終了行が無い src は開始行だけの1行とみなす（行範囲は src が常に持つ想定）
    start, end = int(s["line_start"]), int(s.get("line_end") or s["line_start"])
    file = str(s["file"])
    # WHY: 結び方は _jsx_chain.resolve_for_lines に1か所化(検査 verify_intermediate と同じ規則で引く)。
    #   範囲に要素が無い属性の行は、囲む要素の開始タグの条件で表示が決まる(attr=True)
    res = resolve_for_lines(file, start, end)
    if res is None:
        return None
    r, attr = res
    kind = str(sl_item.get("item_type") or "")
    text = None
    if not attr and kind in ("Button", "Link"):
        # WHY: ボタンの名前は画面に出る文言（JSX の label）。label が無いときは範囲の文言で取る
        text = next((str(el["label"]) for el in elements if el.get("label")), None)
    if not attr and kind in ("Label", "Button", "Link") and not text:
        text = (text_of_lines(file, start, end, lambda e: _text_value(e, env)) or "").strip()
        # fallback-ok: 範囲の最初のタグが文言でないとき（<br /> から始まる等）は、タグを外して範囲の文言を取る
        if not text and not _is_array_use(r):
            text = _range_text(file, start, end, env)
    return {
        "active": {True: "非活性", False: "活性", None: "条件付き(式)"}[disabled_value(r["disabled_chain"], env)],
        "visible": chain_value(r["visible_chain"], env),
        # fallback-ok: 文言が空なら None（初期値として出さない）
        "text": text or None,
    }


def _initial_value(item: dict, states: dict, options: dict, elements: list = None, env: dict = None, fact: dict = None) -> str:
    """画面項目1件の初期値。結ぶ state があればその初期値（事実）。無ければ、表示が真の文言（src の JSX か、名前で結んだ固定文・ボタンの label）。
    どちらも無ければ合成欄の既存値、それも無ければ Blank。
    意味合い: 初期値が無い項目だけ Blank にする。全項目を Blank に固定すると、下書きの事実を捨ててしまう。
    WHY: 表示されている文言がそのまま初期の表示なので、Blank にすると事実を落とす。表示条件が偽・評価不能なら出さない。"""
    name = str(item.get("item_name") or "").strip()
    si = states.get(name)
    if si:
        return _initial_text_from_state(si, options.get(name, {}))
    if fact is not None:
        # WHY: src で結べた項目は、src の JSX の文言だけを見る（名前の結びと混ぜると、表示と文言が食い違うため）
        if fact["visible"] is True and fact["text"]:
            return fact["text"]
    else:
        for el in elements or []:
            if visible_initially(el, env or {}) is not True:
                continue
            if el.get("text") and not el.get("label"):
                return str(el["text"])
            if el.get("label") and item.get("item_type") in ("Button", "Link"):
                return str(el["label"])
    # fallback-ok: 結ぶ state も表示される文言も無い項目は、合成欄の既存値、無ければ Blank（事実が無いので決めない）
    return item.get("initial_value") or "Blank"



def _initial_db_refs(db_operations: list, drafts: list) -> list:
    """初期表示が呼ぶ API の DB 操作の id を、db_operations の並びのまま返す。
    辿り方は _units の _react_draft_facts / _fill_initial_db_triggers と同じ:
      1. 初期表示の行（下書きの events のうち action が「初期表示」）の event_code を集める
      2. その行の event_processes の calls の action と、初期表示の code を trigger_events に持つ API の action を集める
      3. 集めた action を持つ DB 操作の id を返す
    意味合い: 初期表示が引く DB の参照を、機能別処理の表と同じ id（db_operations_ref）で結ぶため。
    初期表示の行が無い・API を辿れないときは []（推測で結ばない）。"""
    init = {str(e["event_code"]) for d in drafts for e in d.get("events") or []
            if isinstance(e, dict) and e.get("action") == "初期表示" and e.get("event_code")}
    if not init:
        return []

    def api_of(op) -> dict:
        api = op.get("api_endpoint") if isinstance(op, dict) else None
        return api if isinstance(api, dict) else {}

    actions = set()
    for d in drafts:
        for ep in d.get("event_processes") or []:
            if isinstance(ep, dict) and ep.get("event_code") in init:
                actions |= {c["action"] for c in ep.get("calls") or [] if isinstance(c, dict) and c.get("action")}
    # 合成後の db_operations（trigger_events が初期表示の code を持つ）と、下書きの db_operations の両方を見る。
    # 理由: 合成済みの partial に初期表示の code が無い版（run7 の形）でも、下書きの側で API を辿れるようにするため
    for op in list(db_operations) + [o for d in drafts for o in d.get("db_operations") or []]:
        api = api_of(op)
        if api.get("action") and init & set(api.get("trigger_events") or []):
            actions.add(api["action"])
    return [op["id"] for op in db_operations if isinstance(op, dict) and op.get("id") and api_of(op).get("action") in actions]


def build_initialization(events: list, screen_layout: dict, db_operations: list = None, drafts: list = None) -> dict:
    """初期処理セクションを最小テンプレで生成。
    EV00 が events にあれば content を Overview に転載、なければデフォルト文を入れる。
    items は screen_layout の全項目（Button・Link を含む。画面項目と同数にする）。
    活性・表示・初期値は react 下書きの事実（state の初期値・表示条件の鎖・disabled の式）を評価して入れる（round9 B7）。
    WHY: Button や Link を除くと、CSV 出力などの非活性の事実が表から消える。全項目を仮埋めのままにすると、下書きの事実を捨てる。"""
    ev00 = next((e for e in events if e.get("event_code") == "EV00"), None)
    overview = ev00["content"] if ev00 else "画面初期表示処理（詳細はWave 3充実化で展開）"

    # 初期値と初期表示の API は react 下書きの事実から取る（下書きを省略時は読み直す。selftest は与える）
    if drafts is None:
        drafts = load_react_drafts()
    states = _state_by_label(drafts)
    options = _option_texts(drafts)
    env = build_env(_all_states(drafts), _all_derived(drafts))
    elements = _elements_by_name(drafts)
    literal = literal_text_elements(drafts)  # 固定文の要素（文字列リテラルだけで決まるもの）

    items = []
    for sl_item in screen_layout.get("items", []):
        name = str(sl_item.get("item_name") or "").strip()
        # WHY: 名前で結ぶ要素が無い Label（設計上の名前「画面見出し」と JSX の文言「POS会計レポート」が違う）は、画面に書かれた文言（example）で固定文に結ぶ
        els = elements.get(name) or linked_fixed_elements([name, str(sl_item.get("example") or "")], literal)
        # WHY: src の TSX で位置から結べた項目は、名前での結びより先に使う（round10 の 6）。結べない項目だけ名前で決める
        fact = _src_fact(sl_item, env, els)
        if fact is not None:
            active, visible = fact["active"], _VIS_LABEL[fact["visible"]]
        else:
            active, visible = _visibility(els, env)
        items.append({
            "no": len(items) + 1,
            "screen_no": sl_item.get("screen_no", ""),
            "item_name": sl_item.get("item_name", ""),
            "active": active,
            "visible": visible,
            "initial_value": _initial_value(sl_item, states, options, els, env, fact),  # 事実（下書き）が無い項目だけ Blank
        })

    return {
        "pattern": "DB取得",  # 初期表示はセッションデータ取得から始まる前提の DB取得パターン
        "overview": overview,
        # 初期表示が呼ぶ API の DB 操作。Wave 3 充実化ではなく、下書きの事実から今埋める（理由: 初期表示の DB 参照が空だと表に紐づかない）
        "db_operations_ref": _initial_db_refs(db_operations or [], drafts),
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


def _selftest_initialization() -> int:
    """初期処理の表の初期値（結ぶ state・選択肢の表示名・式・未設定・Blank・既存値の維持）、活性・表示（表示条件の鎖・disabled）、
    db_operations_ref（初期表示が呼ぶ API の id）を、一時の下書きと実ソースの TSX で確かめる。終了コード 0=OK / 1=NG。"""
    import _initial_eval
    if _initial_eval.selftest() != 0:
        return 1
    drafts = [{
        "events": [{"event_code": "EV00", "action": "初期表示"}, {"event_code": "EV01", "action": "クリック"}],
        "event_processes": [{"event_code": "EV00", "calls": [{"action": "list_a"}]}],
        "db_operations": [{"api_endpoint": {"action": "list_b", "trigger_events": ["EV00"]}}],
        "state_initials": [
            {"state": "from", "kind": "expression", "initial": "todayJst()", "readable": "当月1日(UTC+9時間基準)", "bound": [{"label": "集計開始日"}]},
            {"state": "shopId", "kind": "literal", "initial": '""', "readable": None, "bound": [{"label": "店舗"}]},
            {"state": "group", "kind": "literal", "initial": '"register"', "readable": None, "bound": [{"label": "集計単位"}]},
            {"state": "kind2", "kind": "literal", "initial": '"shop"', "readable": None, "bound": [{"label": "別の選択欄"}]},
            {"state": "showDead", "kind": "literal", "initial": "false", "readable": None, "bound": [{"label": "空欄項目も表示"}]},
            {"state": "rows", "kind": "literal", "initial": "[]", "readable": None, "bound": []},
            {"state": "loading", "kind": "literal", "initial": "false", "readable": None, "bound": []},
        ],
        "derived_arrays": [{"name": "w", "source": "rows", "op": "filter"}],
        "jsx_elements": [
            {"label": "集計単位", "options": [{"value": "register", "text": "レジ別（現行と同じ）"}, {"value": "shop", "text": "店舗別（レジ合算）"}]},
            {"label": "店舗", "options": [{"value": "", "text": "- 全店 -"}]},
            {"text": "合計", "visible_chain": [{"cond": "rows.length > 1", "kind": "and"}]},
            {"text": "読込中…", "visible_chain": [{"cond": "loading", "kind": "ternary_then"}]},
            {"text": "注記文", "visible_chain": []},
            {"tag": "h1", "text": "業務見出し", "visible_chain": []},
            {"text": "エラー文", "visible_chain": [], "visible_if": "loading"},
            {"text": "式の注記", "visible_chain": [{"cond": "f(from)", "kind": "and"}]},
            {"label": "CSV出力", "visible_chain": [], "disabled_chain": [{"cond": "!rows.length", "kind": "disabled"}]},
        ],
    }]
    screen = {"items": [
        {"item_name": "集計開始日", "item_type": "Calendar"},
        {"item_name": "店舗", "item_type": "ComboBox"},
        {"item_name": "集計単位", "item_type": "ComboBox"},
        {"item_name": "別の選択欄", "item_type": "ComboBox"},
        {"item_name": "空欄項目も表示", "item_type": "Checkbox"},
        {"item_name": "表示", "item_type": "Button"},
        {"item_name": "合計", "item_type": "Label"},
        {"item_name": "読込中…", "item_type": "Label"},
        {"item_name": "注記文", "item_type": "Label"},
        {"item_name": "式の注記", "item_type": "Label"},
        {"item_name": "CSV出力", "item_type": "Button"},
        {"item_name": "未設定の項目", "item_type": "Label"},
        {"item_name": "手書きの項目", "item_type": "Label", "initial_value": "手書き"},
        {"item_name": "画面見出し", "item_type": "Label", "example": "業務見出し"},
        {"item_name": "読込中表示", "item_type": "Label", "example": "読込中…"},
        {"item_name": "エラー表示", "item_type": "Label", "example": "エラー文"},
    ]}
    events = [{"event_code": "EV00", "content": "初期表示の概要"}, {"event_code": "EV01", "content": ""}]
    dbs = [{"id": "db_op_a", "api_endpoint": {"action": "list_a"}},
           {"id": "db_op_b", "api_endpoint": {"action": "list_b"}},
           {"id": "db_op_c", "api_endpoint": {"action": "list_c"}}]

    # 期待は (活性, 表示, 初期値)。合計・読込中…は偽の条件で隠れる。式の注記は評価できず「条件付き(式)」。CSV出力は rows が空なので非活性
    want = {
        "集計開始日": ("活性", "表示", "当月1日(UTC+9時間基準)"),
        "店舗": ("活性", "表示", "- 全店 -"),
        "集計単位": ("活性", "表示", "レジ別（現行と同じ）"),
        "別の選択欄": ("活性", "表示", "shop"),
        "空欄項目も表示": ("活性", "表示", "false"),
        "表示": ("活性", "表示", "Blank"),
        "合計": ("活性", "非表示", "Blank"),
        "読込中…": ("活性", "非表示", "Blank"),
        "注記文": ("活性", "表示", "注記文"),
        "式の注記": ("活性", "条件付き(式)", "Blank"),
        "CSV出力": ("非活性", "表示", "CSV出力"),  # ボタンの初期値は画面の文言（label）
        "未設定の項目": ("活性", "表示", "Blank"),
        "手書きの項目": ("活性", "表示", "手書き"),
        "画面見出し": ("活性", "表示", "業務見出し"),  # 名前は違うが example の文言で h1 に結ぶ（初期表示で見える）
        "読込中表示": ("活性", "非表示", "Blank"),  # visible_chain が空で visible_if=loading（初期は偽）
        "エラー表示": ("活性", "非表示", "Blank"),  # 同上
    }
    got = build_initialization(events, screen, dbs, drafts)
    vals = {i["item_name"]: (i["active"], i["visible"], i["initial_value"]) for i in got["items"]}
    if vals != want:
        print(f"[selftest NG] 初期処理の表 {vals}")
        return 1
    if len(got["items"]) != len(screen["items"]):
        print("[selftest NG] 件数が画面項目と一致しない（Button・Link を除いている）")
        return 1
    if got["db_operations_ref"] != ["db_op_a", "db_op_b"]:
        print(f"[selftest NG] db_operations_ref {got['db_operations_ref']}")
        return 1
    # 実ソースの TSX から作った事実（_react_facts の jsx_elements・state_initials・derived_arrays）でも同じ判定になること。
    # WHY: 手書きの下書きだけ確かめると、下書きの欄の名前がずれても気づけないため
    import tempfile
    import _react_facts
    tsx = (
        "export default function S() {\n"
        "  const [shopId, setShopId] = useState(\"\");\n"
        "  const [group, setGroup] = useState(\"register\");\n"
        "  const [rows, setRows] = useState([]);\n"
        "  return (\n"
        "    <div>\n"
        "      <label>店舗</label>\n"
        "      <select value={shopId} onChange={(e) => setShopId(e.target.value)}>\n"
        "        <option value=\"\">- 全店 -</option>\n"
        "      </select>\n"
        "      {rows.length > 1 && (\n"
        "        <span>合計</span>\n"
        "      )}\n"
        "      <label>集計単位</label>\n"
        "      <select value={group} onChange={(e) => setGroup(e.target.value)}>\n"
        "        <option value=\"register\">レジ別（現行と同じ）</option>\n"
        "        <option value=\"shop\">店舗別（レジ合算）</option>\n"
        "      </select>\n"
        "      <button disabled={!rows.length}>CSV出力</button>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td) / "S.tsx"
        tp.write_text(tsx, encoding="utf-8")
        real = [{"jsx_elements": _react_facts.jsx_elements([tp])["jsx_elements"],
                 "state_initials": _react_facts.state_initials([tp])["state_initials"],
                 "derived_arrays": _react_facts.derived_arrays([tp])["derived_arrays"]}]
    rb = build_initialization(events, {"items": [{"item_name": "店舗", "item_type": "ComboBox"},
                                                 {"item_name": "集計単位", "item_type": "ComboBox"},
                                                 {"item_name": "合計", "item_type": "Label"},
                                                 {"item_name": "CSV出力", "item_type": "Button"}]}, [], real)
    rv = {i["item_name"]: (i["active"], i["visible"], i["initial_value"]) for i in rb["items"]}
    if rv != {"店舗": ("活性", "表示", "- 全店 -"), "集計単位": ("活性", "表示", "レジ別（現行と同じ）"),
              "合計": ("活性", "非表示", "Blank"), "CSV出力": ("非活性", "表示", "CSV出力")}:
        print(f"[selftest NG] 実ソースの初期処理の表 {rv}")
        return 1

    # 下書きが無いときは仮埋め（活性・表示）と Blank、空の参照になる
    bare = build_initialization(events, screen, dbs, [])
    bv = {i["item_name"]: (i["active"], i["visible"], i["initial_value"]) for i in bare["items"]}
    if bv["集計単位"] != ("活性", "表示", "Blank") or bv["CSV出力"] != ("活性", "表示", "Blank") or bare["db_operations_ref"] != []:
        print("[selftest NG] 下書き無しの初期処理")
        return 1
    # src（TSX の行範囲）で位置から結ぶ項目。部品の呼出元（hidden）・配列の使用箇所・括弧なしの直接形・{式} の文言・ボタンの label を、実ファイルで確かめる。
    # WHY: 名前で結ぶ方式は要素の見つからない項目が多かった。位置で結んだ結果を、名前の結びと同じ表の形で確かめる
    import tempfile
    src_tsx = (
        "const LEFT = [\n"
        "  { label: \"店舗数\" },\n"
        "];\n"
        "function Card({ items }) {\n"
        "  return (\n"
        "    <section>\n"
        "      <p>件数</p>\n"
        "    </section>\n"
        "  );\n"
        "}\n"
        "export default function S() {\n"
        "  return (\n"
        "    <div>\n"
        "      {rows.length > 1 && <Card items={rows} />}\n"
        "      <span>店舗 {shops.length}店</span>\n"
        "      {loading && <span>読込中…</span>}\n"
        "      <button disabled={!rows.length}>CSV出力</button>\n"
        "      <table>{rows.length > 1 && LEFT.map((r) => <tr key={r.label} />)}</table>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )
    src_drafts = [{"state_initials": [
        {"state": "rows", "kind": "literal", "initial": "[]", "bound": []},
        {"state": "shops", "kind": "literal", "initial": "[]", "bound": []},
        {"state": "loading", "kind": "literal", "initial": "false", "bound": []},
    ], "jsx_elements": [], "derived_arrays": []}]
    with tempfile.TemporaryDirectory() as td:
        tsx_path = str(Path(td) / "S.tsx")
        Path(tsx_path).write_text(src_tsx, encoding="utf-8")

        def at(needle: str) -> dict:
            n = next(i for i, ln in enumerate(src_tsx.split("\n"), 1) if needle in ln)
            return {"file": tsx_path, "line_start": n, "line_end": n}

        src_screen = {"items": [
            {"item_name": "件数", "item_type": "Label", "src": [at("<p>件数")]},
            {"item_name": "店舗数表示", "item_type": "Label", "src": [at("<span>店舗 {shops")]},
            {"item_name": "読込中表示", "item_type": "Label", "src": [at("{loading && <span>")]},
            {"item_name": "CSV出力", "item_type": "Button", "src": [at("<button disabled")]},
            {"item_name": "店舗数", "item_type": "Label", "src": [at('{ label: "店舗数" }')]},
            # WHY(round11 U1): src が配列の宣言の全体(const LEFT = [ から ]; まで)を指す項目も、使用箇所で結ぶ
            {"item_name": "店舗数全体", "item_type": "Label", "src": [{
                "file": tsx_path,
                "line_start": at("const LEFT")["line_start"],
                "line_end": at("];")["line_end"],
            }]},
        ]}
        got_src = build_initialization(events, src_screen, [], src_drafts)
        sv = {i["item_name"]: (i["active"], i["visible"], i["initial_value"]) for i in got_src["items"]}
        want_src = {
            "件数": ("活性", "非表示", "Blank"),          # Card の呼出元が rows.length > 1 の中（rows は空）
            "店舗数表示": ("活性", "表示", "店舗 0店"),    # {shops.length} を初期値の shops（空の配列）で評価
            "読込中表示": ("活性", "非表示", "Blank"),    # {loading && …} は初期が偽
            "CSV出力": ("非活性", "表示", "CSV出力"),     # disabled={!rows.length} は非活性。ボタンの文言
            "店舗数": ("活性", "非表示", "Blank"),        # 配列の行は使用箇所（rows.length > 1 の中）で決まる
            "店舗数全体": ("活性", "非表示", "Blank"),    # 宣言の全体の範囲（定義行を含む）も使用箇所で決まる（round11 U1）
        }
        if sv != want_src:
            print(f"[selftest NG] src で結ぶ初期処理の表 {sv}")
            return 1
    print("[selftest OK] merge_partials.initialization")
    return 0


def main():
    # --help / -h は機能名ではなく使い方の要求。FEATURE_NAME に入ると screen_config 読込で落ちるため先に返す。
    #   接続: 第1引数の判定だけ。機能名の解決（モジュール先頭）は変えない。
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("usage: merge_partials.py [機能名] [--compose-units]  (機能名省略時は環境変数 FEATURE_NAME)")
        print("  --compose-units: _unit_*.json を検査して partial 4本へ合成するだけで終わる（委譲の単位の流れ）")
        return
    # --selftest: 組込みの検査だけ走らせる（partial・機能名は不要。機能名の解決は先頭で済んでいて、ここでは使わない）
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest_initialization())
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
    initialization = build_initialization(events, screen_layout, db_operations)
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
