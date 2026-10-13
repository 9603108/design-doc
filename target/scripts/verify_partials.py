#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_partials.py — extract_react_draft.py が出す下書き JSON(partial)を merge 前に検査する

【目的】
events / db_operations / event_processes が互いに矛盾していないかを機械で確かめる。
LLM が書いた語彙の文面より前に、骨格(連番・参照先・呼出の並び)の食い違いを止めるための検査。
検査項目:
  1. events[].event_code が EV01 から出現順に連番
  2. db_operations[].api_endpoint.trigger_events の参照先が events に実在
  3. overview は 30 字以内(overview キーがある箇所だけ。top・events[]・api_endpoint を見る)
  4. events[].trigger が --screen-name で始まる
  5. event_processes[].details の呼出の行の数と並びが、db_operations の並びで
     そのイベントを trigger_events に持つ action と一致

【入出力】
  入力: 位置引数 partial(下書き JSON のパス)、--screen-name <画面名>
  出力: stdout に問題を '[種別] 場所: 内容' で1行1件。問題が無ければ [OK] の1行
  終了コード: 0=問題なし / 1=問題が1件以上 / 2=入力不正
  --selftest: 組込みの検査を走らせる(partial と --screen-name は不要)
  --units [DIR]: 委譲の単位ごとの断片 DIR/_unit_<単位>.json(既定 target/intermediate)を検査する。
      分担表 prompts/units.json に照らして [範囲外] 担当外の欄 / [重複] 2単位が同じ欄 / [参照] 行の識別子や
      action・event_code・message_id 等が他の単位の断片と食い違う、を1行1件で出す(行末に「→ 差し戻し先 <単位>」)。
      --unit <単位> を付けると、その単位に関わる行だけを出す(単位が自分の断片を検査するとき)
      [列の省略] H2 の dataset・logical_names の行数が <画面>_backend_draft.json の columns より少ない(欠けた列名を挙げる)

【接続情報】
  - 入力の作り手: extract_react_draft.py の build_draft()(キー: events / db_operations / event_processes)
  - details の記法: build_draft() が出す「・<action> をリクエストする」。DETAIL_RE がこの記法を読む
  例: python3 target/scripts/verify_partials.py target/intermediate/<screen>_react_draft.json --screen-name <画面名>
"""
import argparse
import copy
import json
import re
import sys
from pathlib import Path

# 意味合い: 概要は業務担当者が一目で読む長さに抑える取り決め(30 字)。値が変わるならここだけを直す
OVERVIEW_MAX = 30
# details の呼出の行。build_draft() の「・<action> をリクエストする」と同じ記法
DETAIL_RE = re.compile(r"^・(.+?) をリクエストする$")
# details の未記入の印。build_draft() が EV00 以外の各イベントの先頭に出し、LLM が業務の文に置き換える。
# 残っていれば [未記入] にする(verify_intermediate.py も同じ印を空の details として ERROR にする)
DETAIL_TODO = "（未記入）この行を処理ステップの業務の文に置き換える"
# 意味合い: details の1行が「呼出の行」かの判定語。migrate_v17_to_v18 の backend_call 判定(kind 推定)と同じ語にして、
#          ここで「呼出が n 行」と数えたものを、Phase 4 も同じ n 件の backend_call と数える。語が変わるなら両方を直す
CALL_WORDS = ["API", "fetch", "POST", "GET ", "呼び出し", "呼出", "リクエスト"]


def is_call_line(line):
    return any(w in line for w in CALL_WORDS)


def check_partial(draft, screen_name):
    """下書き(dict)を検査し、問題の文字列 list を返す。問題が無ければ空 list。

    呼出元: main() / selftest()。順序は連番・参照・overview・trigger・details の順。
    """
    problems = []
    events = draft.get("events") or []
    ops = draft.get("db_operations") or []
    procs = draft.get("event_processes") or []

    # 1. event_code の連番(build_draft は EV01 から出現順に振る)
    # 目的: LLM が書く Phase 1 の partial は先頭に初期表示の EV00 を持つ(process_names_by_event_code の取り決め)。
    #       EV00 始まりは正常として、その場合は EV00 から連番を見る。誤検出で実 partial が全件 ERROR になるのを防ぐ。
    codes = [e.get("event_code") for e in events]
    base = 0 if codes[:1] == ["EV00"] else 1
    for i, code in enumerate(codes):
        if code != f"EV{i + base:02d}":
            problems.append(f"[連番] events[{i}]: {code!r} は EV{i + base:02d} であるべき")

    # 2. trigger_events の参照先が events に実在
    known = set(codes)
    for i, op in enumerate(ops):
        api = op.get("api_endpoint") or {}
        for c in api.get("trigger_events") or []:
            if c not in known:
                problems.append(f"[参照] db_operations[{i}].api_endpoint.trigger_events: {c} が events に無い")

    # 3. overview は 30 字以内。overview を持つ箇所(top・events[]・api_endpoint)だけ見る
    holders = [("top", draft)]
    holders += [(f"events[{i}]", e) for i, e in enumerate(events)]
    holders += [(f"db_operations[{i}].api_endpoint", op.get("api_endpoint") or {}) for i, op in enumerate(ops)]
    for where, obj in holders:
        ov = obj.get("overview")
        if ov is not None and len(str(ov)) > OVERVIEW_MAX:
            problems.append(f"[overview] {where}: {len(str(ov))} 字(上限 {OVERVIEW_MAX} 字)")

    # 4. trigger 文は画面名で始まる
    for i, e in enumerate(events):
        trig = str(e.get("trigger", ""))
        if not trig.startswith(screen_name):
            problems.append(f"[trigger] events[{i}]: 画面名 {screen_name} で始まらない")

    # 5. details の呼出の行は、db_operations の並びでそのイベントを持つ action と数・並びが一致
    # 目的: event_processes は下書き(extract_react_draft の出力)にだけある。実 partial(_partial_screen_events 等)には
    #       無いので、キーが無いときは検査しない(無いのを「呼出が空」と誤検出しない)。
    by_code = {p.get("event_code"): p for p in procs}
    for p in procs:
        if p.get("event_code") not in known:
            problems.append(f"[参照] event_processes: {p.get('event_code')} が events に無い")
    for ev in (codes if "event_processes" in draft else []):
        # 目的: 同じ action を複数の db_operations に分ける案件(売上レポートは3件)でも、呼出の行は action ごとに1行。
        #       並びは db_operations の初出順のまま、重複だけ除く(dict.fromkeys が出現順を保つ)。
        expected = list(dict.fromkeys(
            (op.get("api_endpoint") or {}).get("action") for op in ops
            if ev in ((op.get("api_endpoint") or {}).get("trigger_events") or [])))
        proc = by_code.get(ev) or {}
        lines = proc.get("details") or []
        actual = []
        for line in lines:
            m = DETAIL_RE.match(line)
            if line == DETAIL_TODO:
                problems.append(f"[未記入] event_processes {ev}: details の未記入の印が残っている(業務の文に置き換える)")
            elif m:
                actual.append(m.group(1))
            elif is_call_line(line):
                problems.append(f"[details] event_processes {ev}: 呼出の記法に合わない行 {line!r}")
            # それ以外は LLM が書いた業務の文(呼出の語を含まない行)なので通す
        if actual != expected:
            problems.append(f"[details] event_processes {ev}: 呼出 {actual} が api_endpoint の並び {expected} と一致しない")
    return problems


def selftest():
    """良い下書きは問題 0 件、壊した下書きは検査ごとの種別が出ることを assert する。終了コード 0=OK / 1=NG。"""
    good = {
        "events": [
            {"event_code": "EV01", "trigger": "受注入力の確定ボタンをクリックする。"},
            {"event_code": "EV02", "trigger": "受注入力の一覧をクリックする。"},
        ],
        "db_operations": [
            {"api_endpoint": {"action": "load", "trigger_events": ["EV01", "EV02"]}},
            {"api_endpoint": {"action": "save", "trigger_events": ["EV01"]}},
        ],
        "event_processes": [
            {"event_code": "EV01", "details": ["・load をリクエストする", "・save をリクエストする"]},
            {"event_code": "EV02", "details": ["・load をリクエストする"]},
        ],
    }
    try:
        assert check_partial(good, "受注入力") == [], "良い下書きで問題が出た"

        # 実 partial の形: EV00 始まり・event_processes 無しは誤検出しない
        real = copy.deepcopy(good)
        real["events"].insert(0, {"event_code": "EV00", "trigger": "受注入力の初期表示時"})
        del real["event_processes"]
        assert check_partial(real, "受注入力") == [], f"実 partial の形で誤検出: {check_partial(real, '受注入力')}"

        bad = copy.deepcopy(good)
        bad["events"][1]["event_code"] = "EV03"                       # 連番
        bad["db_operations"][1]["api_endpoint"]["trigger_events"] = ["EV09"]  # 参照先
        bad["db_operations"][0]["api_endpoint"]["overview"] = "あ" * 31      # overview
        bad["events"][0]["trigger"] = "別の画面の確定ボタンをクリックする。"  # trigger
        bad["event_processes"][0]["details"] = ["・save をリクエストする", "・load をリクエストする"]  # details
        kinds = {p[:p.index("]") + 1] for p in check_partial(bad, "受注入力")}
        assert {"[連番]", "[参照]", "[overview]", "[trigger]", "[details]"} <= kinds, f"検出不足: {kinds}"

        # 業務の文は通し、未記入の印は [未記入] にする
        todo = copy.deepcopy(good)
        todo["event_processes"][1]["details"] = ["入力を確かめる", "・load をリクエストする"]
        assert check_partial(todo, "受注入力") == [], f"業務の文で誤検出: {check_partial(todo, '受注入力')}"
        todo["event_processes"][1]["details"] = [DETAIL_TODO, "・load をリクエストする"]
        assert [p for p in check_partial(todo, "受注入力") if p.startswith("[未記入]")], "未記入の印を検出しない"

        selftest_units()
    except AssertionError as e:
        print(f"[selftest NG] {e}")
        return 1
    print("[selftest OK] verify_partials")
    return 0


# 穴あき文の印: 〈 と {…}(省略記号入りの波括弧)。{key} のような下書きの置換語は穴ではないので拾わない
HOLE_RE = re.compile(r"〈|\{\s*(?:…|\.\.\.)[^{}]*\}")


def _strings(node):
    """断片の中の文字列を (値) で順に返す"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def check_h2_columns(h2, draft):
    """H2 が下書きの列を省いていないか(2026-10-13 混成版で 63 列中 5 列しか書かれなかった)。
    件数ではなく名前で比べる。下書きの各列の物理名は、手順書 H2 のとおり columns の column(空なら別名)。
    別名のまま書いた行も認める(どちらの書き方でも H2 に1行あれば足りる)。
    同じ元列に別名が複数つく列(first_date・last_date がどちらも kai_date)は、別名の数だけ dataset の行が要る。
    1行にまとめれば、足りない別名を挙げて ERROR にする(run4 で first_date・last_date が欠けた)。
    logical_names は「同じ表・同じ列は1行にまとめてよい」ので、物理名が1つあれば足りる。
    呼出元: check_unit_extras。"""
    from collections import Counter

    def accepted(c):  # この列の物理名として書いてよい名前。下書きに physical_column があればそれだけ(別名の取り違えを ERROR にする)
        if c.get("physical_column"):
            return [c["physical_column"]]
        names = [n for n in (c.get("column"), c.get("alias")) if n]
        return names or [c.get("expression")]

    def label(c):
        return c.get("alias") or c.get("column") or c.get("expression")

    problems, keys = [], {}
    ops = [o for o in (h2.get("db_operations") or []) if isinstance(o, dict)]
    for action, api in (draft.get("apis") or {}).items():
        cols = [c for o in api.get("db_operations") or [] for c in o.get("columns") or []]
        if not cols:
            continue
        for c in cols:
            keys.setdefault((c.get("table"), label(c)), c)
        # api_endpoint が辞書でない行は _units.check_refs が [形] で報告する。ここでは数えない
        mine = [o for o in ops if isinstance(o.get("api_endpoint"), dict) and o["api_endpoint"].get("action") == action]
        pool = Counter(r.get("physical_column") for o in mine for r in o.get("dataset") or [] if isinstance(r, dict))
        lack = []
        for c in cols:
            hit = next((n for n in accepted(c) if pool[n] > 0), None)
            if hit is None:
                lack.append(label(c))
            else:
                pool[hit] -= 1
        if lack:
            where = ",".join(o.get("id") or "?" for o in mine) or f"action={action} の行が無い"
            problems.append(f"[列の省略] db_operations[{where}].dataset: 下書きの列 {len(cols)} 列のうち H2 に無い: "
                            f"{', '.join(lack)} → 差し戻し先 H2")
    names = [n for n in h2.get("logical_names") or [] if isinstance(n, dict)]
    if keys:
        have = {n.get("physical_column") for n in names}
        lack = [name for (_table, name), c in keys.items() if not any(n in have for n in accepted(c))]
        if lack:
            problems.append(f"[列の省略] logical_names: 下書きの列 {len(keys)} 種のうち H2 に無い: "
                            f"{', '.join(lack)} → 差し戻し先 H2")
    return problems


def check_h2_logical_names(h2):
    """H2 の dataset の logical_column が H2 の logical_names に無ければ WARN(合成 _units.compose が自動で足す)。"""
    import _units
    return [f"[WARN] [論理名] db_operations.dataset の logical_column={lc}(物理列 {pc}) が logical_names に無い(合成で自動補完) → 差し戻し先 H2"
            for _lt, lc, _pt, pc in _units.missing_logical_names(h2)]


def check_h2_features(h2, draft):
    """下書きの集計・ウィンドウ(apis[].features の aggregates・windows)の別名が、H2 の db_operations[].aggregate に全部あるか(C4)。
    別名のある項目だけ名前で比べる(別名の無い集計は照合の手掛かりが無い)。無ければ ERROR にして H2 へ差し戻す。
    呼出元: check_unit_extras。意味合い: 店舗別の日数が count(*) でレジ単位に数えている等の事実を、H2 が落とさないため。"""
    ops = [o for o in (h2.get("db_operations") or []) if isinstance(o, dict)]
    problems = []
    for action, api in (draft.get("apis") or {}).items():
        f = api.get("features") or {}
        want = [x["alias"] for k in ("aggregates", "windows") for x in f.get(k) or [] if x.get("alias")]
        if not want:
            continue
        mine = [o for o in ops if isinstance(o.get("api_endpoint"), dict) and o["api_endpoint"].get("action") == action]
        have = {a.get("alias") for o in mine for a in (o.get("aggregate") if isinstance(o.get("aggregate"), list) else []) if isinstance(a, dict)}
        lack = [a for a in want if a not in have]
        if lack:
            where = ",".join(o.get("id") or "?" for o in mine) or f"action={action} の行が無い"
            problems.append(f"[集計の省略] db_operations[{where}].aggregate: 下書きの集計・ウィンドウ {len(want)} 件のうち H2 に無い別名: "
                            f"{', '.join(lack)} → 差し戻し先 H2")
    return problems


def check_unit_extras(frags, directory):
    """--units の追加検査(ERROR)。(a) S1 の画面項目に react 下書きの display_rows の行名が全部ある /
    (b) H2 の joins・where が英語キー / (c) S2 と H4 で同じ message_id の文面が同じ / (d) 〈 や {…} の穴が残っていない /
    (e) H2 の dataset・logical_names が backend 下書きの columns を省いていない(check_h2_columns)/
    (f) S1 の example が半数を超えて空でない。
    問題は「[種別] 場所: 内容 → 差し戻し先 <単位>」。呼出元: main_units。"""
    problems = []
    s1 = frags.get("S1")
    if s1 is not None:
        for p in sorted(Path(directory).glob("*_react_draft.json")):
            try:
                rows = json.loads(p.read_text(encoding="utf-8")).get("display_rows") or []
            except (OSError, json.JSONDecodeError):
                continue
            have = {it.get("item_name") for it in (s1.get("screen_layout") or {}).get("items") or [] if isinstance(it, dict)}
            lack = [lb for r in rows for lb in r.get("labels") or [] if lb not in have]
            if lack:
                problems.append(f"[画面項目] screen_layout.items: react 下書きの display_rows の行名 {', '.join(lack)} が無い → 差し戻し先 S1")
    s1_items = [it for it in ((s1 or {}).get("screen_layout") or {}).get("items") or [] if isinstance(it, dict)]
    no_example = [it.get("item_name") for it in s1_items if not it.get("example")]
    if s1_items and len(no_example) * 2 > len(s1_items):
        problems.append(f"[例示値] screen_layout.items: example が空の項目が {len(no_example)}/{len(s1_items)} 件(半数超)。example は全項目に書く → 差し戻し先 S1")
    for op in (frags.get("H2") or {}).get("db_operations") or []:
        if not isinstance(op, dict):
            continue
        for fld in ("joins", "where"):
            bad = list(dict.fromkeys(k for row in op.get(fld) or [] if isinstance(row, dict) for k in row if not k.isascii()))
            if bad:
                problems.append(f"[英語キー] db_operations[{op.get('id')}].{fld}: 日本語のキー {', '.join(bad)}(英語のキー名で書く) → 差し戻し先 H2")
    if frags.get("H2") is not None:
        for p in sorted(Path(directory).glob("*_backend_draft.json")):
            try:
                draft = json.loads(p.read_text(encoding="utf-8"))
                problems += check_h2_columns(frags["H2"], draft) + check_h2_features(frags["H2"], draft)
            except (OSError, json.JSONDecodeError):
                continue
    h4 = {m.get("message_id"): m.get("message") for m in (frags.get("H4") or {}).get("messages_backend") or [] if isinstance(m, dict)}
    for api in (frags.get("S2") or {}).get("api_spec") or []:
        for e in api.get("error_patterns") or []:
            mid, txt = e.get("message_id"), e.get("message")
            if mid in h4 and txt and h4[mid] and txt != h4[mid]:
                problems.append(f"[文面] api_spec[{api.get('action')}].error_patterns: {mid} の文面が S2 と H4 で違う → 差し戻し先 S2 か H4")
    for unit, frag in frags.items():
        if any(HOLE_RE.search(s) for s in _strings(frag)):
            problems.append(f"[穴あき] _unit_{unit}.json: 〈 または {{…}} の穴が残っている → 差し戻し先 {unit}")
    return problems


# 出典(src)を持つべき欄の名前。根拠の行を数える対象(A4 の一覧)
SRC_KEYS = {"events", "event_processes", "api_spec", "response_items", "error_patterns", "db_operations",
            "calculations", "validations", "messages", "messages_backend", "items", "common_logic"}


def _src_rows(node, key=None):
    """断片の中で SRC_KEYS の欄に入っている行(dict)を順に返す。呼出元: check_src_warn。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _src_rows(v, k)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, dict) and key in SRC_KEYS:
                yield v
            yield from _src_rows(v, key)


def check_src_warn(frags):
    """src(出典)が空の行を、件数を1行にまとめた [WARN] で返す(終了コード には影響させない)。
    理由: 出典の無い記述は作り話の疑いがあるため目印を付けるが、A3 の合成前の断片にはまだ src が無く、
    行ごとに出すと欄が多い断片で出力が埋まるので、1件の要約にする。EV00 は初期表示で src を持たない欄として除く。
    呼出元: main_units。"""
    lack, units = [], set()
    for unit, frag in frags.items():
        for r in _src_rows(frag):
            if r.get("event_code") == "EV00":
                continue
            if not r.get("src"):
                lack.append(r.get("event_code") or r.get("action") or r.get("item_name") or r.get("message_id") or r.get("id") or "?")
                units.add(unit)
    if not lack:
        return []
    sample = ", ".join(str(x) for x in lack[:5]) + (f" ほか {len(lack) - 5} 件" if len(lack) > 5 else "")
    return [f"[WARN] [出典] 行 {len(lack)} 件の src が空(ソースの根拠が不明): {sample} → 差し戻し先 {', '.join(sorted(units))}"]


def main_units(directory, only=None):
    """--units の入口。分担表と断片を読み、_units.check_all の問題を出す。終了コード 0/1/2。"""
    import _units
    if not directory:
        from _config_loader import INTERMEDIATE_DIR
        directory = INTERMEDIATE_DIR
    try:
        spec = _units.load_spec()
    except (OSError, json.JSONDecodeError) as e:
        print(f"[入力不正] 分担表 {_units.UNITS_SPEC_PATH}: {e}", file=sys.stderr)
        return 2
    frags, problems = _units.load_fragments(directory)
    if not frags and not problems:
        print(f"[入力不正] {directory} に _unit_*.json が無い", file=sys.stderr)
        return 2
    problems += _units.check_all(spec, frags, only)
    extra = check_unit_extras(frags, directory)
    problems += [p for p in extra if not only or f"差し戻し先 {only}" in p]
    for pr in problems:
        print(pr)
    # WARN は終了コードに影響しない(合成で機械的に補うため)
    for w in check_h2_logical_names(frags.get("H2") or {}):
        print(w)
    for w in check_src_warn(frags):
        print(w)
    if problems:
        return 1
    print(f"[OK] 断片 {len(frags)} 本({', '.join(sorted(frags))})の検査に問題なし")
    return 0


def selftest_units():
    """分担表どおりの断片は問題 0 件、担当外・重複・識別子の食い違い・参照切れは種別が出ることを確かめる"""
    import _units
    spec = _units.load_spec()
    assert _units.check_spec(spec) == [], f"分担表に重なり: {_units.check_spec(spec)}"
    good = {
        "H1": {"events": [{"event_code": "EV00", "trigger": "受注入力画面の初期表示時", "content": "一覧を表示する", "classification": "サーバー"},
                          {"event_code": "EV01", "trigger": "受注入力画面の確定ボタンをクリックする。", "content": "保存する", "classification": "サーバー"}]},
        "H2": {"db_operations": [{"id": "db_op_save", "api_endpoint": {"action": "save", "trigger_events": ["EV01"]},
                                  "operation_type": "INSERT"}]},
        "H3": {"api_spec": [{"action": "save", "method": "POST", "path": "/api/order",
                             "response_items": [{"key": "result", "no": 1, "data_type": "string"}]}]},
        "H4": {"messages_backend": [{"message_id": "MSE00001", "code": "MSE00001", "severity": "error", "message": "保存に失敗しました"}]},
        "S1": {"screen_layout": {"items": [{"item_name": "確定", "area_ref": "A1", "item_type": "Button", "event_code": "EV01"}]}},
        "S2": {"api_spec": [{"action": "save", "description": "受注を保存する", "related_db_operations": ["db_op_save"],
                             "response_items": [{"key": "result", "source": {"type": "const"}}],
                             "response_to_screen_mapping": [{"api_field_path": "result", "screen_item_name": "確定"}],
                             "error_patterns": [{"condition": "保存できない", "status_code": 500, "message_id": "MSE00001"}]}],
               "db_operations": [{"id": "db_op_save", "dataset_name": "受注を登録する"}]},
        "S3": {"event_processes": [{"event_code": "EV01", "details": ["入力を確かめる", "・save をリクエストする"]}]},
    }
    assert _units.check_all(spec, good) == [], f"良い断片で問題が出た: {_units.check_all(spec, good)}"
    comp = _units.compose(spec, good)
    assert comp["api_spec"][0]["description"] == "受注を保存する" and comp["api_spec"][0]["method"] == "POST", "合成で欄が落ちた"
    assert comp["api_spec"][0]["response_items"][0] == {"key": "result", "no": 1, "data_type": "string", "source": {"type": "const"}}, "行の合成が違う"
    parts, unknown = _units.split_partials(comp)
    assert "event_processes" in parts["screen_events"] and not unknown, "partial への振り分けが違う"
    assert comp["screen_layout"]["items"][0]["no"] == 1, "合成で items の no を振っていない"
    # 合成後に必須のキーの欠けは、そのキーを持つ単位へ戻す。担当の断片が未着なら照合しない
    lack = copy.deepcopy(good)
    del lack["S1"]["screen_layout"]["items"][0]["item_type"]
    del lack["S2"]["api_spec"][0]["description"]
    out = _units.check_all(spec, lack)
    assert any(p.startswith("[必須]") and p.endswith("差し戻し先 S1") for p in out), f"S1 の欠けを拾わない: {out}"
    assert any(p.startswith("[必須]") and p.endswith("差し戻し先 S2") for p in out), f"S2 の欠けを拾わない: {out}"
    assert not [p for p in _units.check_all(spec, {"H3": good["H3"]}) if p.startswith("[必須]")], "担当が未着でも [必須] を出した"

    # H1 の trigger の項目名が S1 の item_name と食い違えば H1 へ戻す
    typo = copy.deepcopy(good)
    typo["H1"]["events"][1]["trigger"] = "受注入力画面の確認ボタンをクリックする。"
    out = _units.check_all(spec, typo)
    assert any(p.startswith("[項目名]") and p.endswith("差し戻し先 H1") for p in out), f"trigger の項目名の食い違いを拾わない: {out}"
    assert not [p for p in _units.check_all(spec, {"H1": good["H1"]}) if p.startswith("[項目名]")], "S1 が未着でも [項目名] を出した"

    bad = copy.deepcopy(good)
    bad["H3"]["api_spec"][0]["description"] = "勝手に書いた"                    # 範囲外 + 重複
    bad["S2"]["api_spec"][0]["response_items"][0]["key"] = "results"            # 識別子の食い違い
    bad["S2"]["api_spec"][0]["error_patterns"][0]["message_id"] = "MSE09999"   # 参照切れ
    out = _units.check_all(spec, bad)
    kinds = {p[:p.index("]") + 1] for p in out}
    assert {"[範囲外]", "[重複]", "[参照]"} <= kinds, f"検出不足: {out}"
    assert any("差し戻し先 H3" in p for p in out) and any("差し戻し先 S2" in p for p in out), f"差し戻し先が出ない: {out}"
    assert all("S2" in p for p in _units.check_all(spec, bad, only="S2")), "--unit の絞り込みが効かない"
    # H2 が下書きの columns を省けば、欠けた列名つきで H2 へ戻す。全列を書けば出さない
    draft = {"apis": {"save": {"db_operations": [{"id": "db_op_save", "columns": [
        {"alias": "shop_id", "expression": "p.mise_no", "table": "pos", "column": "mise_no"},
        {"alias": "days", "expression": "count(*)", "table": "pos", "column": ""}]}]}}}
    h2 = copy.deepcopy(good["H2"])
    h2["db_operations"][0]["dataset"] = [{"physical_column": "shop_id"}]
    h2["logical_names"] = [{"physical_column": "shop_id"}]
    out = check_h2_columns(h2, draft)
    assert len(out) == 2 and all("H2 に無い: days" in p and p.endswith("差し戻し先 H2") for p in out), out
    h2["db_operations"][0]["dataset"].append({"physical_column": "days"})
    h2["logical_names"].append({"physical_column": "days"})
    assert check_h2_columns(h2, draft) == [], check_h2_columns(h2, draft)
    # 手順書どおり元の列名(mise_no)で書いても通る。同じ元列の別名が複数あるとき、1行にまとめれば足りない別名を挙げる
    draft2 = {"apis": {"save": {"db_operations": [{"id": "db_op_save", "columns": [
        {"alias": "shop_id", "expression": "p.mise_no", "table": "pos", "column": "mise_no"},
        {"alias": "first_date", "expression": "min(p.kai_date)", "table": "pos", "column": "kai_date"},
        {"alias": "last_date", "expression": "max(p.kai_date)", "table": "pos", "column": "kai_date"}]}]}}}
    h2b = copy.deepcopy(good["H2"])
    h2b["db_operations"][0]["dataset"] = [{"physical_column": "mise_no"}, {"physical_column": "kai_date"}]
    h2b["logical_names"] = [{"physical_column": "mise_no"}, {"physical_column": "kai_date"}]
    out = check_h2_columns(h2b, draft2)
    assert len(out) == 1 and "H2 に無い: last_date " in out[0], f"まとめた別名を拾わない: {out}"
    h2b["db_operations"][0]["dataset"].append({"physical_column": "kai_date"})
    assert check_h2_columns(h2b, draft2) == [], check_h2_columns(h2b, draft2)
    # 下書きの集計・ウィンドウの別名が H2 の aggregate に無ければ ERROR(C4)
    draft3 = {"apis": {"save": {"features": {"aggregates": [{"alias": "total"}], "windows": [{"alias": "nz"}]}}}}
    h2f = {"db_operations": [{"id": "db_op_save", "api_endpoint": {"action": "save"}, "aggregate": [{"alias": "total"}]}]}
    out = check_h2_features(h2f, draft3)
    assert len(out) == 1 and "H2 に無い別名: nz" in out[0] and out[0].endswith("差し戻し先 H2"), out
    h2f["db_operations"][0]["aggregate"].append({"alias": "nz"})
    assert check_h2_features(h2f, draft3) == [], check_h2_features(h2f, draft3)
    # dataset の論理列が logical_names に無ければ WARN、合成で補う
    h2w = {"db_operations": [{"dataset": [{"logical_column": "店番号", "physical_column": "mise_no"}]}], "logical_names": []}
    assert len(check_h2_logical_names(h2w)) == 1, "logical_names の欠けを WARN にしない"
    comp2 = _units.compose(spec, {"H2": h2w})
    assert comp2["logical_names"][0]["physical_column"] == "mise_no" and not _units.missing_logical_names(comp2), "合成で補わない"
    # 作り手の断片がまだ無い(並行の途中)ときは、識別子の照合をしない
    partial_run = {"S2": good["S2"]}
    assert not [p for p in _units.check_fragments(spec, partial_run) if p.startswith("[参照]")], "作り手が未着でも [参照] を出した"
    # 型文の {key} は、下書きの展開済みの値(shop_id)の説明が画面項目名で始まるとき、その画面項目名で展開する。対応が無ければ穴のまま
    out = {"screen_layout": {"items": [{"item_name": "店舗"}, {"item_name": "店舗名"}]},
           "api_spec": [{"request_params": [{"name": "shop_id", "description": "店舗番号。省略時は全店"}]}],
           "messages_backend": [{"message_id": "MSE00002", "message": "{key} は正の整数で指定してください"},
                                {"message_id": "MSE00003", "message": "{key} は日付で指定してください"}]}
    _units._expand_message_holes(out, {"{key} は正の整数で指定してください": "MSE00002", "shop_id は正の整数で指定してください": "MSE00002",
                                       "{key} は日付で指定してください": "MSE00003", "from は日付で指定してください": "MSE00003"})
    assert [m["message"] for m in out["messages_backend"]] == ["店舗 は正の整数で指定してください", "{key} は日付で指定してください"], out
    # 英大文字だけの略語(POS)は、ソースの識別子に同じ形で無ければ物理名としない。同じ形で在れば物理名
    import verify_intermediate as vi
    def phys(src_idents):
        return [m for lv, m in vi.check_description_granularity(
            {"backend_processes": [{"process_id": "BE1", "overview": "POS会計を一覧にする"}]}, {"source_identifiers": src_idents}) if "物理名" in m]
    assert not phys([]) and phys(["POS"]), "略語の物理名判定が違う"
    # src が空の行は件数を1行の [WARN] にまとめる。src が全部ある断片、EV00 だけの断片は出さない
    src_ok = {"H1": {"events": [{"event_code": "EV01", "src": [{"file": "x", "line_start": 1, "line_end": 2}]}]}}
    assert check_src_warn(src_ok) == [], "src が揃った断片で WARN を出した"
    src_lack = {"H1": {"events": [{"event_code": "EV02"}]}, "S3": {"event_processes": [{"event_code": "EV00"}, {"event_code": "EV01", "src": []}]}}
    out = check_src_warn(src_lack)
    assert len(out) == 1 and out[0].startswith("[WARN] [出典]") and "行 2 件" in out[0] and "差し戻し先 H1, S3" in out[0], out


def main(argv=None):
    """CLI の入口。入力を検め、検査して終了コード 0/1/2 を返す。"""
    p = argparse.ArgumentParser(description="extract_react_draft.py の下書き JSON を merge 前に検査する")
    p.add_argument("partial", nargs="?", help="下書き JSON のパス")
    p.add_argument("--screen-name", help="画面名(trigger 文の先頭に来るべき語)")
    p.add_argument("--selftest", action="store_true", help="組込みの検査を走らせる")
    p.add_argument("--units", nargs="?", const="", default=None,
                   help="委譲の単位ごとの断片 _unit_*.json のフォルダ(省略時 target/intermediate)を検査する")
    p.add_argument("--unit", help="--units と併用。その単位に関わる問題だけを出す")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.units is not None:
        return main_units(args.units, args.unit)
    if not args.partial or not args.screen_name:
        print("[入力不正] partial と --screen-name が必要", file=sys.stderr)
        return 2
    try:
        draft = json.loads(Path(args.partial).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[入力不正] {args.partial}: {e}", file=sys.stderr)
        return 2
    if not isinstance(draft, dict):
        print(f"[入力不正] {args.partial}: 最上位がオブジェクトでない", file=sys.stderr)
        return 2

    problems = check_partial(draft, args.screen_name)
    for pr in problems:
        print(pr)
    if problems:
        return 1
    print("[OK] partial の検査に問題なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
