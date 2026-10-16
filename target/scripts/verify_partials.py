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
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
from pathlib import Path
import _config_loader  # 下書き・設定を機能名で1本に決める(draft_files / resolve_feature_name)

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

        # 下書きは機能名の1本だけ読む。試験の下書き(x_*_draft.json)に合わせて機能名を x にする(実の target/intermediate の下書きは読まない)
        import os
        os.environ["FEATURE_NAME"] = "x"
        selftest_units()
        selftest_feature()
        selftest_warn()
        import verify_vs_source
        assert verify_vs_source.selftest() == 0
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
    別名の行(alias_only)の書き方は physical_column 空・alias_name つき(dataset・logical_names とも)。
    別名を physical_column に書いた・logical_names が alias_name を落とした行は、合成と同じ機械の直し(_compose_db_facts.restore_alias_rows)を
    写しに当ててから数える(verify_vs_source の [別名] とは、直した形で食い違わない)。
    呼出元: check_unit_extras。"""
    import copy
    from collections import Counter
    import _compose_db_facts
    h2 = copy.deepcopy(h2)  # 検査は断片を書き換えない
    _compose_db_facts.restore_alias_rows(h2, [draft])

    def accepted(c):  # この列の照合名として H2 に書いてよい名前
        # WHY: alias_only の列は物理列が無く、手順書どおり H2 は別名を alias_name に書いて physical_column を空にする。
        #      下書き側の physical_column には別名が入っているので、alias_only は alias_name で照合する(physical_column で照合すると空の行と合わず [列の省略] になる)
        if c.get("kind") == "alias_only":
            return [c.get("alias_name") or c.get("alias")]
        if c.get("physical_column"):  # 物理列があればそれだけ(別名の取り違えを ERROR にする)
            return [c["physical_column"]]
        names = [n for n in (c.get("column"), c.get("alias")) if n]
        return names or [c.get("expression")]

    def h2_name(r):  # H2 の1行の照合名。物理列が空の行(別名だけの行)は alias_name で数える
        # WHY: 手順書は alias_only の行の physical_column を空にする。空のまま数えると、別名の列がすべて欠けたように見えてしまう
        return r.get("physical_column") or r.get("alias_name")

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
        pool = Counter(h2_name(r) for o in mine for r in o.get("dataset") or [] if isinstance(r, dict))
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
        have = {h2_name(n) for n in names}  # 別名だけの行(alias_only)も alias_name で数える(dataset と同じ照合名)
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
        for p in _config_loader.draft_files("react", directory):  # 機能名の下書き1本(別画面の下書きを読まない)
            try:
                rows = json.loads(p.read_text(encoding="utf-8")).get("display_rows") or []
            except (OSError, json.JSONDecodeError):
                continue
            # 目的: display_rows の照合は verify_vs_source.check_items に畳んだ(B2)。ここは jsx 無しで display_rows の分だけ呼ぶ
            import verify_vs_source
            problems += verify_vs_source.check_items(
                [it for it in (s1.get("screen_layout") or {}).get("items") or [] if isinstance(it, dict)], None, rows)
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
        for p in _config_loader.draft_files("backend", directory):  # 機能名の下書き1本
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
    # B14 a)(c): 例示値の括弧のプレースホルダと、「DB 例外」の断定は ERROR(差し戻し先は各行に書く)
    problems += check_example_placeholder(frags) + check_db_exception_wording(frags)
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


def _src_rows_at(node, key=None, generic=""):
    """_src_rows と同じ歩き方で、SRC_KEYS の欄の行を (汎用パス, 欄名, 行) で返す。
    WHY: 行がどの単位の欄かを _units.owner_of(spec, 汎用パス)で引くため、パスを持たせる(_src_rows は行だけを返す)。
    汎用パスは _units._walk と同じ形(a.b[].c)。呼出元: check_src_warn。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _src_rows_at(v, k, f"{generic}.{k}" if generic else k)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, dict) and key in SRC_KEYS:
                yield f"{generic}[]", key, v
            yield from _src_rows_at(v, key, generic + "[]" if isinstance(v, dict) else generic)


def _row_label(r):
    return str(r.get("event_code") or r.get("action") or r.get("item_name") or r.get("name") or r.get("message_id") or r.get("id") or "?")


def check_src_warn(composed, spec, skip=None):
    """合成の後に src(出典)が空の行を、単位ごとに1行の [WARN] で返す(終了コードには影響させない)。
    WHY(W1): 合成前の断片を数えると events・items など合成が src を付ける行まで「空」に数え、227 件が 9 件になる実測があった。
    _fill_src・_compose_*.py の後の形(= _units.compose の戻り)で数えるので、残るのは機械で付けられなかった行だけになる。
    単位は汎用パスから _units.owner_of(spec, パス)で引き(引けなければ creators の最上位キーの作り手)、差し戻し先を1単位に絞る。
    skip: {(欄名, 行の名前)}。check_cl_src・check_error_rows が ERROR にした行は WARN に重ねない。EV00 は除く。
    呼出元: main_units。"""
    import _units
    skip = skip or set()
    by_unit = {}
    for generic, key, r in _src_rows_at(composed):
        if r.get("event_code") == "EV00" or r.get("src"):
            continue
        name = _row_label(r)
        if (key, name) in skip or (key, str(r.get("condition"))) in skip:
            continue
        unit = _units.owner_of(spec, generic) or (spec.get("creators") or {}).get(re.split(r"[\[.]", generic)[0]) or "?"
        by_unit.setdefault((unit, key), []).append(name)
    out = []
    for (unit, key), names in sorted(by_unit.items()):
        sample = ", ".join(names[:5]) + (f" ほか {len(names) - 5} 件" if len(names) > 5 else "")
        out.append(f"[WARN] [出典] {unit}: {key} {len(names)} 件({sample})の src が空 → 差し戻し先 {unit}")
    return out


def check_cl_src(frags):
    """S1 の common_logic の行で src が無いものを ERROR にする(W1 b)。差し戻し先 S1。
    WHY: 合成は CL の src を付けない。src が無いと fill_common_logic_callers が呼出元の BE を絞れず、全 BE を足す退避に落ちる
    (run12 の新7')。S1 の手順書(W12)が書く決まりの検査。呼出元: main_units(戻りの行は skip にも使う)。"""
    out = []
    for r in ((frags.get("S1") or {}).get("common_logic") or []):
        if isinstance(r, dict) and not r.get("src"):
            out.append(f"[出典] common_logic[{_row_label(r)}]: src が無い。合成は付けない。DB で動くものは条件・定数の定義の行、"
                       "画面で動くものは関数の定義の行を {file,line_start,line_end} で書く → 差し戻し先 S1")
    return out


def _bad_error_rows(frags):
    """S2 の error_patterns で根拠の無い行を (action, 番号, 行) で返す。message_id が無く、(a) status_code が無い、
    または (b) 同じ API に同じ status_code で message_id のある行がある。呼出元: check_error_rows。"""
    for a in ((frags.get("S2") or {}).get("api_spec") or []):
        if not isinstance(a, dict):
            continue
        rows = [e for e in a.get("error_patterns") or [] if isinstance(e, dict)]
        with_mid = {str(e.get("status_code")) for e in rows if e.get("message_id") and e.get("status_code") is not None}
        for i, e in enumerate(rows):
            if e.get("message_id"):
                continue
            if e.get("status_code") is None or str(e.get("status_code")) in with_mid:
                yield a.get("action"), i, e


def check_error_rows(frags):
    """S2 の error_patterns の、出典(message_id)の無い注記の行を ERROR にする(W1 c)。差し戻し先 S2。
    WHY: S2 が処理の無いことの注記に status_code=400 を付けて行にし、合成が error_code を補って本物のエラー行に見えた(run12 の新6)。
    共通エラー(500 など)は合成が足すので S2 の断片に無く、対象外。呼出元: main_units。"""
    return [f"[根拠] api_spec[{act}].error_patterns[{i}]「{str(e.get('condition') or '')[:30]}」: 出典(message_id)が無い。"
            "起きる条件と返るステータスの行だけを書き、注記は api_spec の description へ → 差し戻し先 S2"
            for act, i, e in _bad_error_rows(frags)]

def check_composed_gaps(composed, frags):
    """合成した形の穴を、verify_intermediate(最後の検査)より前の単位の検査で拾う(ERROR)。呼出元: main_units。
    (a) [検査の漏れ] 400 の error_patterns(S2)の message_id が validations_frontend(H4)に無い。バック側の入力検査も validations に1件書く
        決まり(画面に検査が無ければ kind=API検査)。最後の検査と同じ判定を、S2・H4 の断片が揃った時点で出す → 差し戻し先 H4。
    (b) [画面への写し] S2 が書いた API に response_items があるのに response_to_screen_mapping も response_to_screen_mapping_none_reason も無い
        → 差し戻し先 S2。どちらも相手の断片が無い(並行の途中)ときは見ない。"""
    problems = []
    s2_actions = {a.get("action") for a in (frags.get("S2") or {}).get("api_spec") or [] if isinstance(a, dict)}
    if frags.get("S2") is not None:
        for a in composed.get("api_spec") or []:
            if a.get("action") in s2_actions and a.get("response_items") and not a.get("response_to_screen_mapping") \
                    and not a.get("response_to_screen_mapping_none_reason"):
                problems.append(f"[画面への写し] api_spec[{a.get('action')}].response_to_screen_mapping: response_items があるのに空。"
                                "画面のどの項目に出すかを書く(画面に出さない API だけ response_to_screen_mapping_none_reason に理由) → 差し戻し先 S2")
    if frags.get("S2") is not None and frags.get("H4") is not None:
        have = {v.get("message_id") for v in composed.get("validations_frontend") or [] if isinstance(v, dict)}
        for a in composed.get("api_spec") or []:
            for e in a.get("error_patterns") or []:
                mid = e.get("message_id") if isinstance(e, dict) else None
                if mid and str(e.get("status_code")) == "400" and mid not in have:
                    problems.append(f"[検査の漏れ] api_spec[{a.get('action')}].error_patterns: 400 のエラー {mid} が validations_frontend に無い。"
                                    "バック側の入力検査も1件書き message_id で結ぶ(画面に検査が無ければ kind は「API検査」) → 差し戻し先 H4")
    # (c) [表示名] 合成が補えなかった response_items の display_name の空。display_name は H3 の欄で、合成が先に補う(B3)。
    #     補えない行だけが残るので、業務の名前を H3 が書く。H3 の断片が未着(並行の途中)のときは見ない
    if frags.get("H3") is not None:
        for a in composed.get("api_spec") or []:
            empty = [str(r.get("key")) for r in a.get("response_items") or [] if isinstance(r, dict) and not r.get("display_name")]
            if empty:
                sample = ", ".join(empty[:5]) + (f" ほか {len(empty) - 5} 件" if len(empty) > 5 else "")
                problems.append(f"[表示名] api_spec[{a.get('action')}].response_items: display_name が空の行 {len(empty)} 件({sample})。"
                                "補えない行は業務の名前を書く → 差し戻し先 H3")
    return problems


# 識別子の抜き出しから外す「業務の文」の形。verify_intermediate.main がソースの識別子を作るときの正規表現と同じ
# WHY: 文字列・コメント・日本語を含む JSX の文は業務の文なので、識別子から外す(「POS会計」の POS を識別子と取り違えない)。
#      verify_intermediate.main の中に直書きされていて import できないので、同じものをここに持つ(変えるなら両方を直す)
_PROSE_RE = re.compile(r'/\*.*?\*/|"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|//[^\n]*|#[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`[^`]*`'
                       r'|[^<>{}\n]*[^\x00-\x7f][^<>{}\n]*', re.S)


def _verify_overrides(directory):
    """directory(断片のフォルダ)の project_config・screen_config から、合成後の概要の検査(check_description_granularity)に渡す
    verify_overrides を作る。無い設定は空にする(その分の判定は共通のものだけ)。呼出元: check_s2_overview。
    WHY: 部品の段でも合成後と同じ基準で見るため、同じ入力(案件の業界用語・参照 ID の接頭辞・ソースの識別子)を同じ作り方で渡す。
         ソースの識別子が空だと POS のような業務の略語を物理名と取り違えるので、source_files と backend_files から作る。"""
    d = Path(directory)
    vo, sc = {}, {}
    for suffix, slot in (("_project_config.json", "pc"), ("_screen_config.json", "sc")):
        # 機能名の設定1本(別画面の設定を拾わない)。無い設定は空のまま
        for p in [d / f"{_config_loader.resolve_feature_name()}{suffix}"]:
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if slot == "pc":
                vo = dict(data.get("verify_overrides") or {})
            else:
                sc = data
    import os
    root = Path(os.path.expanduser(os.path.expandvars(sc.get("project_root") or ".")))
    srcs = list(sc.get("source_files") or []) + [str(root / f) for f in sc.get("backend_files") or []]
    idents = set()
    for s in srcs:
        try:
            idents |= set(re.findall(r"[A-Za-z0-9_]+", _PROSE_RE.sub(" ", Path(os.path.expanduser(os.path.expandvars(s))).read_text(encoding="utf-8"))))
        except OSError:
            continue
    vo["source_identifiers"] = sorted(idents)
    return vo


def _bp_overview(api):
    """api_spec の1行から、合成後の backend_processes[].overview になる文を返す。migrate_v17_to_v18 の backend_processes の組み立てと同じ決め方。
    WHY: name が無いと name は description の1文目から作られるので、overview は2文目以降(あれば)になる。name があれば description そのまま。
         同じ決め方でないと、部品の段で 30 字内でも合成後に超える(または逆)食い違いが出る。決め方が変わるなら migrate_v17_to_v18 の側と両方を直す。"""
    name = str(api.get("name") or "").strip()
    desc = api.get("description") or ""
    if name:
        return desc
    _, sep, rest = desc.partition("。")
    return rest.strip() if sep and rest.strip() else desc


def check_s2_overview(composed, frags, directory):
    """S2 の api_spec[].description が、合成後の概要(backend_processes[].overview)の検査を満たすかを部品の段で見る(ERROR)。差し戻し先 S2。
    WHY(run14): description が 30 字を超える・register() のような関数名(物理名)を含むと、合成の後の verify_intermediate
    (check_description_granularity)でようやく ERROR になり、S2 への差し戻しが後の段まで遅れた。同じ関数を同じ入力(verify_overrides)で
    呼ぶので、字数の上限(30 字)・物理名の判定は合成後と一致する。S2 が書いた API(actions)だけを見る。
    接続: 呼出元 = main_units。合成後の行(name を含む)から overview を作る(_bp_overview)。"""
    s2_actions = {a.get("action") for a in (frags.get("S2") or {}).get("api_spec") or [] if isinstance(a, dict)}
    if not s2_actions:
        return []
    import verify_intermediate as vi
    vo = _verify_overrides(directory)
    out = []
    for a in composed.get("api_spec") or []:
        if not isinstance(a, dict) or a.get("action") not in s2_actions or not str(a.get("description") or "").strip():
            continue
        probe = {"backend_processes": [{"process_id": str(a.get("action")), "overview": _bp_overview(a)}]}
        for level, msg in vi.check_description_granularity(probe, vo):
            if level == "ERROR":
                out.append(f"[概要] api_spec[{a.get('action')}].description: {msg} → 差し戻し先 S2"
                           "(業務の言葉で30字以内の1文にする。関数名・物理名は書かない。注記は db_operations[].remarks に書く)")
    return out


# 説明の要の語の判定に使う英数字の識別子(3字以上)。日本語は見ない(ソース内の表記が揺れるため)
_KEY_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# WHY(2026-10-16 run19): 業務の言葉として説明に出る略語(「本日の日付(UTC+9時間基準)」の UTC)はソースの識別子ではなく、出典の本文(`+ 9 * 3600 * 1000`)に字面で無い。
#      これだけが「要の語」になった行が、run17〜19 で毎回 WARN の誤検出になった。識別子として使われない略語は要の語に数えない。
_GENERIC_ACRONYMS = {"UTC", "JST", "CSV", "API", "SQL", "URL", "HTTP", "HTTPS", "JSON", "XML", "HTML", "TSX", "UI", "DB", "ID", "PDF", "NULL", "OK", "NG"}
# 例示値の穴 {名前}。実行時に決まる値の書き方なので、例示値に残ると例として読めない
_EX_HOLE = re.compile(r"\{[^{}]+\}")
_DATE_EX = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")


def _src_text(src):
    """出典(file・line_start・line_end)の範囲の本文。読めなければ None。呼出元: check_explanation_warn。"""
    try:
        lines = Path(src["file"]).read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[src["line_start"] - 1:src["line_end"]])
    except (OSError, KeyError, TypeError, ValueError):
        return None


def check_data_type_warn(frags):
    """data_type が辞書(正規の語・別名)に無い値を、単位ごとに1行の [WARN] で返す(終了コードには影響させない)。
    理由: 型の表記揺れ(string と 文字列)は合成が直すが、辞書に無い語は直せないので担当へ戻す。
    別名('文字列' 等)と None や str 以外は WARN にしない。辞書は _compose_normalize が持つ。呼出元: main_units。"""
    from _compose_normalize import CANONICAL_DATA_TYPES, DATA_TYPE_ALIASES
    out = []
    for unit, frag in frags.items():
        bad = []
        stack = [frag]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                v = node.get("data_type")
                if isinstance(v, str):
                    key = v.strip().lower()
                    if key not in DATA_TYPE_ALIASES and key not in CANONICAL_DATA_TYPES:
                        bad.append(v)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        if bad:
            out.append(f"[WARN] [型] {unit}: data_type に辞書外の値 {', '.join(sorted(set(bad)))}({len(bad)} 件) → 差し戻し先 {unit}")
    return out


def check_explanation_warn(frags):
    """説明文の要の語が、出典の範囲のソース本文に1つも無い行を [WARN] で返す(終了コードには影響させない)。
    理由: 出典が付いていても範囲が別の処理を指すと、説明は根拠の無い文のまま出典の印だけ付く。
    英数字の識別子(3字以上・EV01 のような符号は除く)が説明に無い、または出典が読めないときは判定しない(誤検出を避ける)。
    呼出元: main_units。接続: _src_rows の SRC_KEYS の行を見る。"""
    found = []
    for unit, frag in frags.items():
        for r in _src_rows(frag):
            if r.get("event_code") == "EV00" or not r.get("src"):
                continue
            text = " ".join(str(r.get(k) or "") for k in ("content", "description", "overview"))
            terms = {t for t in _KEY_TERM.findall(text) if not re.fullmatch(r"[A-Z]+\d+", t) and t.upper() not in _GENERIC_ACRONYMS}
            bodies = [_src_text(s) for s in r["src"] if isinstance(s, dict)]
            if not terms or not bodies or any(b is None for b in bodies):
                continue
            body = "\n".join(bodies)
            if not any(t in body for t in terms):
                # WHY(2026-10-16 run19): 行の名前を event_code・action・item_name・message_id だけで引くと、他の欄(calculations・validations・common_logic など)の行は
                #      「?」になり、どの行・どの語か分からなかった(run17〜19 で毎回)。行の名前は _row_name で引き(無ければ最初の文字列の欄)、
                #      語(説明の識別子)と出典(file:行)も並べる。
                s0 = next((s for s in r["src"] if isinstance(s, dict)), {})
                import _vs_common
                where = _vs_common.span(s0)
                found.append((unit, f"{_row_name(r)}(語 {', '.join(sorted(terms)[:4])} / 出典 {where})"))
    if not found:
        return []
    names = ", ".join(dict.fromkeys(n for _, n in found))
    units = ", ".join(sorted({u for u, _ in found}))
    return [f"[WARN] [根拠] 行 {len(found)} 件: 説明の要の語が出典の範囲に無い: {names} → 差し戻し先 {units}"]


def _row_name(r):
    """行の名前。よくある識別の欄を順に引き、どれも無ければ最初の文字列の欄の先頭30字(「?」で特定できなくならないようにする)。呼出元: check_explanation_warn。"""
    for k in ("event_code", "action", "item_name", "name", "no", "field", "key", "message_id", "id", "process_id", "alias", "step_no"):
        if r.get(k):
            return str(r[k])
    return next((str(v)[:30] for v in r.values() if isinstance(v, str) and v), "?")  # fallback-ok: 文字列の欄が1つも無い行だけ「?」


def check_example_format(frags):
    """S1 の例示値の書式を見る。日付の項目(Calendar)の例示値が日付の書式(yyyy-mm-dd 等)に合わないものを ERROR で返す。
    戻りは問題の文字列 list。ERROR は main_units の problems に入り終了コード 1 になる。{名前} の穴は check_example_warn が WARN で出す。呼出元: main_units。"""
    bad_fmt = []
    for it in ((frags.get("S1") or {}).get("screen_layout") or {}).get("items") or []:
        if not isinstance(it, dict):
            continue
        ex, name = str(it.get("example") or ""), str(it.get("item_name") or "?")
        # 穴(例 {集計開始日})は書式の問題ではなく check_example_warn の WARN で扱う。ここで ERROR にすると WARN と二重になり、終了コードも 1 になる
        if it.get("item_type") == "Calendar" and ex and not _EX_HOLE.search(ex) and not _DATE_EX.search(ex):
            bad_fmt.append(f"{name}(例 {ex!r})")
    out = []
    if bad_fmt:
        out.append(f"[例示値] screen_layout.items: 日付の項目の例示値が日付の書式に合わない: {', '.join(bad_fmt)} → 差し戻し先 S1")
    return out


def check_example_warn(frags):
    """S1 の例示値に {名前} の穴が残るものを [WARN] で返す(終了コードには影響させない)。
    実行時に決まる値の書き方が例示値に残ると、例として読めないため S1 へ戻す。呼出元: main_units。"""
    holes = [str(it.get("item_name") or "?") for it in ((frags.get("S1") or {}).get("screen_layout") or {}).get("items") or []
             if isinstance(it, dict) and _EX_HOLE.search(str(it.get("example") or ""))]
    if not holes:
        return []
    return [f"[WARN] [例示値] screen_layout.items: 例示値に {{…}} の穴が残る: {', '.join(holes)} → 差し戻し先 S1"]


def check_composed_warn(frags):
    """単位が合成の欄(_units.COMPOSED: db_operations の ctes・subqueries)を書いたときの [WARN]。
    WHY: 書いた値は合成が下書きの事実で置き換えるので、終了コードにも差し戻しにもしない(差し戻し先を書くと再依頼されるため書かない)。"""
    import _units
    counts = {}
    for unit, f in frags.items():
        leaves, _ = _units.decompose(f)
        n = sum(1 for generic, _c in leaves if generic in _units.COMPOSED)
        if n:
            counts[unit] = n
    return [f"[WARN] [合成] {u}: db_operations の ctes・subqueries を {n} 欄書いた。合成が下書きの事実で置き換える(書いた値は捨てる)"
            for u, n in sorted(counts.items())]


# B14 a): S1 の例示値の括弧の中が、実値でなく説明の穴になる語(DB・データベース・実行時・入力値・任意)を含むものを拾う
PLACEHOLDER_WORDS = ("DB", "データベース", "実行時", "入力値", "任意")
PAREN_RE = re.compile(r"[（(]([^（）()]*)[）)]")
# B14 c): 「DB 例外」等の断定。ソースに根拠の無い暗黙の挙動を断定させないための語。「(推測)」を付けた文は通す
DB_EXC_RE = re.compile(r"DB 例外|データベースの例外|DB のエラー")
# 検査の対象の欄: validations(rule・applied_when)と error_patterns(condition)
_EXC_KEYS = ("validations", "validations_frontend", "error_patterns")


def check_example_placeholder(frags):
    """S1 の例示値の括弧の中が DB・データベース・実行時・入力値・任意 等の語なら ERROR(B14 a)。差し戻し先 S1。
    WHY: 「（DB の店舗名）」は例として読めず、画面に出る実値が業務担当者に分からないため。
    差し戻し文に「具体的な実値(例 横浜店)で書く」を添える。呼出元: check_unit_extras。"""
    problems = []
    for it in ((frags.get("S1") or {}).get("screen_layout") or {}).get("items") or []:
        if not isinstance(it, dict):
            continue
        ex = str(it.get("example") or "")
        for inner in PAREN_RE.findall(ex):
            if any(w in inner for w in PLACEHOLDER_WORDS):
                problems.append(f"[例示値] screen_layout.items[{it.get('item_name')}]: 例示値「{ex}」の括弧({inner})が実値でない。"
                                "具体的な実値(例 横浜店)で書く → 差し戻し先 S1")
                break
    return problems


def _keyed_rows(node, keys, key=None):
    """断片の中で keys の欄に入っている行(dict)を (欄名, 行) で順に返す。_src_rows と同じ歩き方。呼出元: check_db_exception_wording。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _keyed_rows(v, keys, k)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, dict) and key in keys:
                yield key, v
            yield from _keyed_rows(v, keys, key)


def check_db_exception_wording(frags):
    """H4 の validations_frontend(rule・applied_when)と S2 の error_patterns(condition)に「DB 例外」等の断定があれば ERROR(B14 c)。
    WHY: DB 例外(500)は下書きの事実として合成が足すので、説明文で断定させない。「(推測)」を付けた文は通す。
    差し戻し先は書いた断片(H4 か S2)。呼出元: check_unit_extras。"""
    problems = []
    for unit, frag in frags.items():
        if unit not in ("H4", "S2"):
            continue
        for key, row in _keyed_rows(frag, _EXC_KEYS):
            for f in (("rule", "applied_when") if key.startswith("validations") else ("condition",)):
                txt = str(row.get(f) or "")
                m = DB_EXC_RE.search(txt)
                if m and "(推測)" not in txt and "（推測）" not in txt:
                    problems.append(f"[文言] {unit} {key}[{f}]: 「{m.group(0)}」と断定している(ソースの根拠が無い)。"
                                    "ソースの処理の事実だけを書く → 差し戻し先 " + unit)
    return problems


def resolve_units_feature(feature=None):
    """--units の機能名を決める。--feature を優先し、無ければ環境変数 FEATURE_NAME。どちらも無ければ None(推測して選ばない)。
    決めた値は環境変数 FEATURE_NAME にも入れる(下書き・設定を読む側 _config_loader.draft_files が同じ値を使う)。"""
    import os
    name = feature or os.environ.get("FEATURE_NAME") or None
    if name:
        os.environ["FEATURE_NAME"] = name
    return name


def main_units(directory, only=None, feature=None):
    """--units の入口。分担表と断片を読み、_units.check_all の問題を出す。終了コード 0/1/2。
    機能名(--feature か環境変数 FEATURE_NAME)を必ず求める。無いと終了コード 2 で止める。
    下書き・設定はその機能名の1本だけ読み、断片の meta.feature_name が違う・無いと ERROR。"""
    import _units
    feature = resolve_units_feature(feature)
    if not feature:
        print("[入力不正] 機能名が決まらない。--feature <機能名> か環境変数 FEATURE_NAME で、どの画面かを必ず指定する"
              "(断片 _unit_*.json は機能名に依らない固定名なので、推測して別の画面の下書きを読まない)", file=sys.stderr)
        return 2
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
    problems += [p for p in _units.check_feature(frags, feature) if not only or f"差し戻し先 {only}" in p]
    extra = check_unit_extras(frags, directory) + check_example_format(frags)
    # 合成後の穴(画面への写しの欠け・400 の検査の漏れ)。project_config を渡して下書きの展開済み ID へ寄せた形で見る
    composed = _units.compose(spec, frags, {})  # check_composed_gaps と check_src_warn(合成の後に数える)で使い回す
    extra += check_composed_gaps(composed, frags)
    # S2 の description が合成後の概要の検査(30 字・物理名)を満たすか。合成後の verify_intermediate を待たずに S2 へ戻す
    extra += check_s2_overview(composed, frags, directory)
    # W1 b)(c): S1 の CL の src 無し・S2 の根拠の無いエラー行は ERROR。WARN には重ねない(skip に入れる)
    cl_errs, err_errs = check_cl_src(frags), check_error_rows(frags)
    extra += cl_errs + err_errs
    skip = {("common_logic", _row_label(r)) for r in ((frags.get("S1") or {}).get("common_logic") or []) if isinstance(r, dict) and not r.get("src")}
    skip |= {("error_patterns", str(e.get("condition"))) for _a, _i, e in _bad_error_rows(frags)}
    problems += [p for p in extra if not only or f"差し戻し先 {only}" in p]
    for pr in problems:
        print(pr)
    # WARN は終了コードに影響しない(合成で機械的に補うため)
    for w in check_h2_logical_names(frags.get("H2") or {}):
        print(w)
    for w in check_src_warn(composed, spec, skip) + check_explanation_warn(frags) + check_example_warn(frags) + check_composed_warn(frags) + check_data_type_warn(frags):
        print(w)
    # S1 の example がソースの文字列に無ければ [WARN](D1 後半)。機能名は --feature / FEATURE_NAME で決めたもの(下書きのファイル名からは取らない)
    s1_items = ((frags.get("S1") or {}).get("screen_layout") or {}).get("items") or []
    for _p in _config_loader.draft_files("react", directory):
        import verify_vs_source
        for w in verify_vs_source.check_examples(s1_items, verify_vs_source.source_strings(feature)):
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
                             "response_items": [{"key": "result", "no": 1, "data_type": "string", "display_name": "結果"}]}]},
        "H4": {"messages_backend": [{"message_id": "MSE00001", "code": "MSE00001", "severity": "error", "message": "保存に失敗しました"}]},
        "S1": {"screen_layout": {"items": [{"item_name": "確定", "area_ref": "A1", "item_type": "Button", "event_code": "EV01"}]}},
        "S2": {"api_spec": [{"action": "save", "description": "受注を保存する", "related_db_operations": ["db_op_save"],
                             "response_items": [{"key": "result", "description": "保存の結果"}],  # source・expression は担当が書かない(G20: 下書きから合成が入れる)
                             "response_to_screen_mapping": [{"api_field_path": "result", "screen_item_name": "確定"}],
                             "error_patterns": [{"condition": "保存できない", "status_code": 500, "message_id": "MSE00001"}]}],
               "db_operations": [{"id": "db_op_save", "dataset_name": "受注を登録する"}]},
        "S3": {"event_processes": [{"event_code": "EV01", "details": ["入力を確かめる", "・save をリクエストする"]}]},
    }
    assert _units.check_all(spec, good) == [], f"良い断片で問題が出た: {_units.check_all(spec, good)}"
    # G20: response_items の source はどの単位の担当でもない。S2 が書けば [範囲外] で S2 へ戻す
    own_src = copy.deepcopy(good)
    own_src["S2"]["api_spec"][0]["response_items"][0]["source"] = {"type": "const"}
    assert any(p.startswith("[範囲外]") and "source" in p and p.endswith("差し戻し先 S2") for p in _units.check_all(spec, own_src)), _units.check_all(spec, own_src)
    # 合成後の穴: 画面への写しが空・400 の検査が validations に無いと、断片の段階で S2・H4 へ戻す(最後の検査 verify_intermediate を待たない)
    assert check_composed_gaps(_units.compose(spec, good), good) == [], check_composed_gaps(_units.compose(spec, good), good)
    gap = copy.deepcopy(good)
    gap["S2"]["api_spec"][0]["response_to_screen_mapping"] = []
    gap["S2"]["api_spec"][0]["error_patterns"].append({"condition": "形式が違う", "status_code": 400, "message_id": "MSE00001"})
    out = check_composed_gaps(_units.compose(spec, gap), gap)
    assert [p.split(" ")[0] for p in out] == ["[画面への写し]", "[検査の漏れ]"] and out[0].endswith("差し戻し先 S2") and out[1].endswith("差し戻し先 H4"), out
    assert not check_composed_gaps(_units.compose(spec, {"S2": gap["S2"]}), {"S2": gap["S2"]})[1:], "H4 が未着でも [検査の漏れ] を出した"
    gap["S2"]["api_spec"][0]["response_to_screen_mapping_none_reason"] = "画面に出さない"
    gap["H4"]["validations_frontend"] = [{"field": "確定", "rule": "形式", "applied_when": "API が受けたとき", "kind": "API検査", "message_id": "MSE00001"}]
    assert check_composed_gaps(_units.compose(spec, gap), gap) == [], check_composed_gaps(_units.compose(spec, gap), gap)
    assert _units.check_all(spec, gap) == [], "none_reason・kind=API検査 が分担表・検査で通らない: " + str(_units.check_all(spec, gap))
    comp = _units.compose(spec, good)
    assert comp["api_spec"][0]["description"] == "受注を保存する" and comp["api_spec"][0]["method"] == "POST", "合成で欄が落ちた"
    assert comp["api_spec"][0]["response_items"][0] == {"key": "result", "no": 1, "data_type": "string", "display_name": "結果", "description": "保存の結果"}, "行の合成が違う"
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
    # alias_only の列(物理列が無い別名)は、下書きの physical_column に別名が入っていても H2 の alias_name で照合する。
    # H2 は手順書どおり physical_column を空にする。alias_name に無ければ dataset と logical_names の両方で欠けとして出す
    draft_alias = {"apis": {"save": {"db_operations": [{"id": "db_op_save", "columns": [
        {"alias": "shop_name", "kind": "alias_only", "physical_column": "shop_name", "real_column": "", "alias_name": "shop_name", "table": "shops", "column": ""}]}]}}}
    h2a = {"db_operations": [{"id": "db_op_save", "api_endpoint": {"action": "save"},
                              "dataset": [{"physical_column": "", "alias_name": "shop_name"}]}],
           "logical_names": [{"physical_column": "", "alias_name": "shop_name"}]}
    assert check_h2_columns(h2a, draft_alias) == [], check_h2_columns(h2a, draft_alias)
    h2a["db_operations"][0]["dataset"] = []
    h2a["logical_names"] = []
    out = check_h2_columns(h2a, draft_alias)
    assert len(out) == 2 and all("H2 に無い: shop_name" in p for p in out), out
    # R14: 別名の行の書き方は physical_column 空・alias_name つき。書き方の揺れは合成と同じ機械の直しで受ける。
    # (1) logical_names が alias_name を落とした(run13)→ dataset の同じ(物理表, 論理列)から補って通す
    row_a = {"physical_table": "shops", "logical_column": "店舗名称(推定)"}
    h2a["db_operations"][0]["dataset"] = [dict(row_a, physical_column="", alias_name="shop_name")]
    h2a["logical_names"] = [dict(row_a, physical_column="")]
    assert check_h2_columns(h2a, draft_alias) == [], check_h2_columns(h2a, draft_alias)
    assert "alias_name" not in h2a["logical_names"][0], "検査が断片を書き換えた"
    # (2) 別名を physical_column に書いた(古い書き方)→ 別名として数えて通す(verify_vs_source の [別名] に残らない形へ合成が直す)
    h2a["db_operations"][0]["dataset"] = [dict(row_a, physical_column="shop_name")]
    h2a["logical_names"] = [dict(row_a, physical_column="shop_name")]
    assert check_h2_columns(h2a, draft_alias) == [], check_h2_columns(h2a, draft_alias)
    # (3) logical_names の行が alias_name を落とし、dataset にも対応する行が無ければ、欠けとして出す(推測で補わない)
    h2a["db_operations"][0]["dataset"] = [dict(row_a, physical_column="", alias_name="shop_name")]
    h2a["logical_names"] = [{"physical_table": "shops", "logical_column": "別の名前", "physical_column": ""}]
    out = check_h2_columns(h2a, draft_alias)
    assert len(out) == 1 and out[0].startswith("[列の省略] logical_names") and "H2 に無い: shop_name" in out[0], out
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
    # F2: 下書きの common_errors のうち status が数値のもの(500)だけを各 api_spec の error_patterns へ足す。式の status(400)は足さない。既にある status は足さない
    import tempfile, _config_loader
    with tempfile.TemporaryDirectory() as td:
        Path(td, "x_backend_draft.json").write_text(json.dumps({"common_errors": [
            {"exception": "ValidationError", "errors": [{"code": "getattr(e) or 'v'", "status_code": "getattr(e, 'status', 400) or 400"}]},
            {"exception": "psycopg2.Error", "errors": [{"code_default": "internal_error", "status_code": 500}], "src": [{"file": "h.py", "line_start": 5, "line_end": 6}]},
            {"exception": "Exception", "errors": [{"code_default": "internal_error", "status_code": 500}]}]}), encoding="utf-8")
        old, _config_loader.INTERMEDIATE_DIR = _config_loader.INTERMEDIATE_DIR, Path(td)
        try:
            ce = _units._draft_common_errors()
            comp3 = _units.compose(spec, {"S2": {"api_spec": [{"action": "a", "error_patterns": [{"status_code": 400, "condition": "x"}]},
                                                               {"action": "b", "error_patterns": [{"status_code": 500, "condition": "y"}]}]}}, {})
        finally:
            _config_loader.INTERMEDIATE_DIR = old
    sr = [{"file": "h.py", "line_start": 5, "line_end": 6}]
    # WHY: code は condition に書かず error_code の欄に入れる(B0)。期待値の condition の「。code=」は B0 で消した形
    assert ce == [{"status_code": 500, "condition": "想定外の例外が起きたとき(psycopg2.Error・Exception)", "src": sr, "error_code": "internal_error"}], ce
    pa, pb = (a["error_patterns"] for a in comp3["api_spec"])
    assert [e["status_code"] for e in pa] == [400, 500] and pa[1]["src"] == sr and pb[0]["src"] == sr and len(pb) == 1, (pa, pb)
    assert pa[1]["error_code"] == "internal_error" and "code=" not in pa[1]["condition"], pa[1]
    # 作り手の断片がまだ無い(並行の途中)ときは、識別子の照合をしない
    partial_run = {"S2": good["S2"]}
    assert not [p for p in _units.check_fragments(spec, partial_run) if p.startswith("[参照]")], "作り手が未着でも [参照] を出した"
    # 型文の {key} は、下書きの呼出ごとの展開済みの文面(from・to・shop_id)へ、1呼出1件で置き換える(画面項目名への言い換えはしない)。
    # 展開が取れない型文は穴のまま。参照(error_patterns・validations_frontend・request_params.validation)の ID も付け替える
    out = {"api_spec": [{"request_params": [{"name": "from", "description": "集計開始日", "validation": "日付(MSE00003)"},
                                            {"name": "to", "description": "集計終了日", "validation": "日付(MSE00003)"}],
                         "error_patterns": [{"condition": "集計開始日が不正", "message_id": "MSE00003"},
                                            {"condition": "集計終了日が不正", "message_id": "MSE00003"}]}],
           "validations_frontend": [{"field": "集計終了日", "message_id": "MSE00003"}],
           "messages_backend": [{"message_id": "MSE00003", "message": "{key} は日付で指定してください", "severity": "error"},
                                {"message_id": "MSE00009", "message": "{x} は取れない", "severity": "error"}]}
    _units._expand_messages(out, {"{key} は日付で指定してください": [("MSE00003", "from は日付で指定してください", "from"),
                                                                 ("MSE00004", "to は日付で指定してください", "to")]})
    assert [(m["message_id"], m["message"]) for m in out["messages_backend"]] == [
        ("MSE00003", "from は日付で指定してください"), ("MSE00004", "to は日付で指定してください"), ("MSE00009", "{x} は取れない")], out
    assert [e["message_id"] for e in out["api_spec"][0]["error_patterns"]] == ["MSE00003", "MSE00004"], out
    assert [v["message_id"] for v in out["validations_frontend"]] == ["MSE00004"] and [q["validation"] for q in out["api_spec"][0]["request_params"]] == ["日付(MSE00003)", "日付(MSE00004)"], out
    # 英大文字だけの略語(POS)は、ソースの識別子に同じ形で無ければ物理名としない。同じ形で在れば物理名
    import verify_intermediate as vi
    def phys(src_idents):
        return [m for lv, m in vi.check_description_granularity(
            {"backend_processes": [{"process_id": "BE1", "overview": "POS会計を一覧にする"}]}, {"source_identifiers": src_idents}) if "物理名" in m]
    assert not phys([]) and phys(["POS"]), "略語の物理名判定が違う"
    # 画面項目の例示値は、実行時に決まる値の {名前} の穴を正式に認める(未展開の穴の ERROR にしない)。他の欄の {名前} と 〈 は従来どおり ERROR
    holes = {"screen_layout": {"items": [{"item_name": "総売上", "example": "{金額} 円"}, {"item_name": "件数", "example": "{件数} 点 {金額} 円"}]},
             "events": [{"content": "{金額} を出す"}], "notes": ["〈例〉"]}
    got = [m for lv, m in vi.check_intermediate_strict(holes) if "未展開の穴" in m]
    assert len(got) == 2 and all("screen_layout" not in m for m in got), got
    assert [m for lv, m in vi.check_intermediate_strict({"screen_layout": {"items": [{"example": "〈金額〉 円"}]}}) if "未展開の穴" in m], "example の 〈 は ERROR のまま"
    # W1: src の WARN は合成の後の形で、単位ごとに1行。合成が src を付ける行(events・items)は WARN にしない
    sp = {"units": {"H1": {"owns": ["events"]}, "H4": {"owns": ["calculations"]}, "S3": {"owns": ["event_processes"]}}, "creators": {}}
    src1 = [{"file": "x", "line_start": 1, "line_end": 2}]
    assert check_src_warn({"events": [{"event_code": "EV01", "src": src1}]}, sp) == [], "src が揃った行で WARN を出した"
    assert check_src_warn({"events": [{"event_code": "EV00"}]}, sp) == [], "EV00 を WARN にした"
    out = check_src_warn({"events": [{"event_code": "EV01", "src": src1}],
                          "calculations": [{"name": "額", "src": []}, {"name": "過不足"}]}, sp)
    assert len(out) == 1 and out[0].startswith("[WARN] [出典] H4: calculations 2 件") and out[0].endswith("差し戻し先 H4"), out
    assert check_src_warn({"calculations": [{"name": "額"}]}, sp, {("calculations", "額")}) == [], "skip の行を WARN にした"
    # W1 b): S1 の common_logic の src 無しは ERROR、src ありは通る
    cl = check_cl_src({"S1": {"common_logic": [{"name": "判定"}, {"name": "定数", "src": src1}]}})
    assert len(cl) == 1 and "common_logic[判定]" in cl[0] and cl[0].endswith("差し戻し先 S1"), cl
    # W1 c): message_id 無し・同じ status の行あり = ERROR、message_id 無し・status が一意 = 通す、status 無し = ERROR
    def s2(*rows):
        return {"S2": {"api_spec": [{"action": "a", "error_patterns": list(rows)}]}}
    ok_row = {"condition": "形式が違う", "status_code": 400, "message_id": "MSE00001"}
    out = check_error_rows(s2(ok_row, {"condition": "注記", "status_code": 400}))
    assert len(out) == 1 and "error_patterns[1]" in out[0] and out[0].endswith("差し戻し先 S2"), out
    assert check_error_rows(s2(ok_row, {"condition": "別の status", "status_code": 404})) == [], "status が一意の行を ERROR にした"
    assert len(check_error_rows(s2(ok_row, {"condition": "status 無し"}))) == 1, "status 無しを ERROR にしない"
    assert check_error_rows(s2(ok_row)) == [], "message_id のある行を ERROR にした"


def selftest_feature():
    """機能名の取り違え(棚分析の検査が POS会計レポートの下書きを読んだ事故)を防ぐ仕組みを assert する。
    a) 下書きは機能名の1本だけ読む(別画面の下書きを読まない)。機能名が無ければ止める
    b) 断片の meta.feature_name が無い・違うと ERROR。meta は欄の担当の検査と合成に入れない
    c) --units は機能名(--feature / FEATURE_NAME)が無ければ終了コード 2、断片の機能名が違えば 1。環境は戻す"""
    import contextlib
    import io
    import os
    import tempfile
    import _units
    spec = _units.load_spec()
    saved = os.environ.pop("FEATURE_NAME", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            for name in ("A", "B"):
                Path(td, f"{name}_react_draft.json").write_text("{}", encoding="utf-8")
            # a) 機能名 A のとき A の下書きだけ(B を読まない)。下書きが無い機能名は []。機能名が無いと SystemExit
            os.environ["FEATURE_NAME"] = "A"
            assert [p.name for p in _config_loader.draft_files("react", td)] == ["A_react_draft.json"], "別画面の下書きを読んだ"
            assert _config_loader.draft_files("backend", td) == [], "下書きが無いのに返った"
            os.environ["FEATURE_NAME"] = "C"
            assert _config_loader.draft_files("react", td) == [], "機能名 C に別画面の下書きを充てた"
            del os.environ["FEATURE_NAME"]
            try:
                _config_loader.draft_files("react", td)
                raise AssertionError("機能名が無いのに下書きを拾った(推測して選んだ)")
            except SystemExit:
                pass
            # 実際の読み手: H2 の列の省略の検査。B の下書きにだけ列があるとき、機能名 A は列の省略を出さない
            Path(td, "A_backend_draft.json").write_text(json.dumps({"apis": {"save": {"db_operations": [{"id": "db_op_save", "columns": []}]}}}), encoding="utf-8")
            Path(td, "B_backend_draft.json").write_text(json.dumps({"apis": {"save": {"db_operations": [{"id": "db_op_save", "columns": [
                {"alias": "shop_name", "kind": "alias_only", "physical_column": "shop_name", "real_column": "", "alias_name": "shop_name", "table": "shops", "column": ""}]}]}}}), encoding="utf-8")
            h2 = {"db_operations": [{"id": "db_op_save", "api_endpoint": {"action": "save"}, "dataset": []}], "logical_names": []}
            os.environ["FEATURE_NAME"] = "A"
            assert not [p for p in check_unit_extras({"H2": h2}, td) if p.startswith("[列の省略]")], "機能名 A の検査が B の下書きの列を読んだ"
            os.environ["FEATURE_NAME"] = "B"
            assert [p for p in check_unit_extras({"H2": h2}, td) if p.startswith("[列の省略]")], "機能名 B の下書きの列を読まなかった"
            # b) meta.feature_name: 違う(H2)・無い(H3)は ERROR、同じ(H1)は問題なし。行末の「→ 差し戻し先 <単位>」で単位へ戻す
            os.environ["FEATURE_NAME"] = "A"
            h1 = {"meta": {"feature_name": "A"}, "events": [{"event_code": "EV00", "trigger": "A画面の初期表示時", "content": "表示する", "classification": "サーバー"}]}
            frags = {"H1": h1, "H2": {"meta": {"feature_name": "B"}}, "H3": {}}
            out = _units.check_feature(frags, "A")
            assert len(out) == 2 and out[0].startswith("[機能名の食い違い]") and out[0].endswith("差し戻し先 H2") \
                and out[1].startswith("[機能名なし]") and out[1].endswith("差し戻し先 H3"), out
            assert _units.check_feature({"H1": h1}, "A") == [], "同じ機能名を ERROR にした"
            # meta は欄ではない: 担当外(範囲外)にも合成の最上位にも入れない
            assert _units.check_fragments(spec, {"H1": h1}) == [], _units.check_fragments(spec, {"H1": h1})
            assert "meta" not in _units.compose(spec, {"H1": h1}), "meta を合成の最上位に入れた"
            # c) --units: 機能名が無ければ 2(推測しない)、断片の機能名が違えば 1
            Path(td, "A_react_draft.json").unlink()  # S1 の example の WARN は screen_config(ソースの場所)を要するので、ここでは走らせない
            Path(td, "_unit_H1.json").write_text(json.dumps({"meta": {"feature_name": "B"}, "events": h1["events"]}), encoding="utf-8")
            del os.environ["FEATURE_NAME"]
            with contextlib.redirect_stderr(io.StringIO()) as err:
                assert main(["--units", td]) == 2, "機能名が無いのに --units が止まらない"
            assert "機能名が決まらない" in err.getvalue(), err.getvalue()
            assert "FEATURE_NAME" not in os.environ, "機能名が無いのに環境を書き換えた"
            with contextlib.redirect_stdout(io.StringIO()) as so:
                assert main(["--units", td, "--feature", "A"]) == 1, "断片の機能名が違うのに通した"
            assert "[機能名の食い違い] _unit_H1.json" in so.getvalue(), so.getvalue()
    finally:
        if saved is None:
            os.environ.pop("FEATURE_NAME", None)
        else:
            os.environ["FEATURE_NAME"] = saved


def selftest_warn():
    """説明の要の語が出典の範囲に無いときの [WARN]、例示値の {名前} の [WARN]、日付の例示値の書式の ERROR を assert する。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "h.py"
        src.write_text("def save_order(x):\n    return x\n\ndef other():\n    pass\n", encoding="utf-8")
        ok = {"H1": {"events": [{"event_code": "EV01", "content": "save_order で保存する",
                                 "src": [{"file": str(src), "line_start": 1, "line_end": 2}]}]}}
        assert check_explanation_warn(ok) == [], "根拠のある説明で WARN を出した"
        ng = {"H1": {"events": [{"event_code": "EV01", "content": "delete_customer を消す",
                                 "src": [{"file": str(src), "line_start": 4, "line_end": 5}]}]}}
        out = check_explanation_warn(ng)
        assert len(out) == 1 and out[0].startswith("[WARN] [根拠]") and out[0].endswith("差し戻し先 H1"), out
        # run19: 語と出典の場所が出る。event_code 等の無い行(validations の field・名前の欄すら無い行)でも「?」にならない
        assert "delete_customer" in out[0] and "h.py:4-5" in out[0] and "EV01" in out[0], out
        v_ng = {"H4": {"validations": [{"field": "エラーの表示", "description": "delete_customer を消す", "src": [{"file": str(src), "line_start": 4, "line_end": 5}]},
                                       {"description": "delete_customer を消す", "src": [{"file": str(src), "line_start": 4, "line_end": 5}]}]}}
        out_v = check_explanation_warn(v_ng)
        assert len(out_v) == 1 and "?(" not in out_v[0] and "エラーの表示(語 delete_customer" in out_v[0], out_v
        # 略語(UTC)だけが英数字の語の行は、出典の字面に無くても判定しない(誤検出)
        acr = {"S1": {"common_logic": [{"name": "本日の日付(UTC+9時間基準)", "description": "UTC に9時間足した日付", "src": [{"file": str(src), "line_start": 4, "line_end": 5}]}]}}
        assert check_explanation_warn(acr) == [], check_explanation_warn(acr)
        no_src = {"H1": {"events": [{"event_code": "EV01", "content": "save_order で保存する"}]}}
        assert check_explanation_warn(no_src) == [], "出典が無い行は判定しない(WARN は別の check_src_warn)"
    s1 = {"S1": {"screen_layout": {"items": [{"item_name": "総売上", "example": "{金額} 円", "item_type": "Label"},
                                            {"item_name": "期間", "example": "2026-10", "item_type": "Calendar"}]}}}
    assert [w for w in check_example_warn(s1) if "{…} の穴" in w and "総売上" in w], check_example_warn(s1)
    fmt = check_example_format(s1)
    assert len(fmt) == 1 and fmt[0].startswith("[例示値]") and "期間" in fmt[0], fmt
    assert not check_example_format({"S1": {"screen_layout": {"items": [{"example": "2026-10-01", "item_type": "Calendar"}]}}}), "正しい日付で ERROR"
    # 日付の項目の穴は ERROR にしない(WARN だけ出す。終了コードを変えない)
    hole_cal = {"S1": {"screen_layout": {"items": [{"item_name": "集計開始日", "example": "{集計開始日}", "item_type": "Calendar"}]}}}
    assert check_example_format(hole_cal) == [], "日付の穴を ERROR にした"
    assert [w for w in check_example_warn(hole_cal) if "集計開始日" in w], "日付の穴を WARN にしない"
    # F2: H2 が db_operations の subqueries・ctes を書いても範囲外・重複の ERROR にしない。WARN だけ出す(差し戻し先は書かない)
    import _units
    spec_h2 = {"units": {"H2": {"owns": ["db_operations[].operation_type"]}}}
    h2_sub = {"db_operations": [{"id": "db_op_x", "operation_type": "select",
                                 "subqueries": [{"location": "WHERE", "definition": "SELECT max(d) FROM t", "filtered": False}],
                                 "ctes": []}]}
    assert _units.check_fragments(spec_h2, {"H2": h2_sub}) == [], "合成が埋める欄を範囲外にした"
    w = check_composed_warn({"H2": h2_sub})
    assert len(w) == 1 and w[0].startswith("[WARN] [合成] H2") and "差し戻し先" not in w[0], w
    assert check_composed_warn({"H2": {"db_operations": [{"id": "db_op_x", "operation_type": "select"}]}}) == [], "書いていない H2 で WARN を出した"
    # 合成は下書きの事実で置き換える。H2 の書いた値は捨て、下書きに無い id の欄は外す
    rows = [{"id": "db_op_x", "subqueries": [{"definition": "作り話"}], "ctes": [{"name": "作り話"}]},
            {"id": "db_op_y", "subqueries": [{"definition": "作り話"}]}]
    facts = {"db_op_x": {"ctes": [{"name": "d", "definition": "SELECT 1", "from": "t", "filtered": True}],
                         "subqueries": [{"location": "WHERE", "definition": "SELECT max(d) FROM t", "filtered": False}]}}
    # 結び方は _units.draft_db_op(id・legacy_id・action・表)が一か所で決める。入力は下書きの形(apis[*].db_operations)で渡す
    _units._set_db_op_facts(rows, [{"apis": {"x": {"db_operations": [{"id": k, **v} for k, v in facts.items()]}}}])
    assert rows[0]["subqueries"] == facts["db_op_x"]["subqueries"] and rows[0]["ctes"] == facts["db_op_x"]["ctes"], rows[0]
    assert "subqueries" not in rows[1] and "ctes" not in rows[1], rows[1]
    # B14 a): 括弧の中が DB・実行時 等のプレースホルダの例示値は ERROR。実値(横浜店・1,234 円)は通す
    ph = {"S1": {"screen_layout": {"items": [{"item_name": "店舗", "example": "（DB の店舗名）（レジ2台）"},
                                            {"item_name": "店", "example": "横浜店（レジ2台）"}]}}}
    out = check_example_placeholder(ph)
    assert len(out) == 1 and "items[店舗]" in out[0] and out[0].endswith("差し戻し先 S1") and "items[店]" not in out[0], out
    assert check_example_placeholder({"S1": {"screen_layout": {"items": [{"item_name": "合計", "example": "1,234 円（税込）"}]}}}) == [], "実値の例で ERROR"
    # B14 b): 合成後も display_name が空なら H3 へ戻す。H3 の断片が未着(並行の途中)なら見ない
    empty_dn = {"api_spec": [{"action": "save", "response_items": [{"key": "result"}, {"key": "rows[].x", "display_name": "店"}]}]}
    out = check_composed_gaps(empty_dn, {"H3": {"api_spec": []}})
    assert len(out) == 1 and out[0].startswith("[表示名]") and out[0].endswith("差し戻し先 H3") and "result" in out[0], out
    assert check_composed_gaps(empty_dn, {"S2": {"api_spec": []}}) == [], "H3 が未着でも [表示名] を出した"
    # B14 c): 「DB 例外」の断定は H4 の validations と S2 の error_patterns で ERROR。「(推測)」を付けた文は通す
    dbx = {"H4": {"validations_frontend": [{"rule": "DB 例外で止まる", "applied_when": "保存時"}]},
           "S2": {"api_spec": [{"action": "a", "error_patterns": [{"condition": "データベースの例外が起きたとき"},
                                                              {"condition": "DB 例外が起きたとき(推測)"}]}]}}
    out = check_db_exception_wording(dbx)
    assert len(out) == 2 and out[0].endswith("差し戻し先 H4") and out[1].endswith("差し戻し先 S2"), out
    # S2 の description: 合成後の概要と同じ基準(30 字・物理名)。name が無ければ2文目以降が overview になる(migrate_v17_to_v18 と同じ)
    long_desc = "あ" * 31
    s2f = {"S2": {"api_spec": [{"action": "a"}]}}

    def s2_out(desc, name=None):
        row = {"action": "a", "description": desc}
        if name:
            row["name"] = name
        return check_s2_overview({"api_spec": [row]}, s2f, "/nonexistent")

    assert s2_out("店舗の一覧を返す") == [], s2_out("店舗の一覧を返す")
    out = s2_out(long_desc)
    assert len(out) == 1 and out[0].startswith("[概要] api_spec[a].description") and "30 字超" in out[0] and out[0].count("差し戻し先 S2") == 1, out
    out = s2_out("店舗を登録する register() を呼ぶ")
    assert len(out) == 1 and "物理名" in out[0] and "func:register()" in out[0], out
    # 1文目が長くても、name が無く2文目があれば overview は2文目(合成後の決め方)なので、2文目が短ければ通る
    assert s2_out(long_desc + "。店舗を返す") == [], s2_out(long_desc + "。店舗を返す")
    assert len(s2_out(long_desc + "。店舗を返す", name="一覧")) == 1, "name があれば description 全体が overview"
    assert check_s2_overview({"api_spec": [{"action": "z", "description": long_desc}]}, s2f, "/nonexistent") == [], "S2 が書いていない API を見た"
    assert check_s2_overview({"api_spec": [{"action": "a", "description": long_desc}]}, {"H3": {}}, "/nonexistent") == [], "S2 未着で見た"
    # data_type: 辞書外は単位ごとに WARN 1行、正規の語・別名・None だけなら WARN なし
    out = check_data_type_warn({"S1": {"screen_layout": {"items": [{"data_type": "foo"}, {"data_type": "string"}]}}})
    assert len(out) == 1 and out[0].startswith("[WARN] [型]") and out[0].endswith("差し戻し先 S1"), out
    assert check_data_type_warn({"S1": {"screen_layout": {"items": [{"data_type": "string"}, {"data_type": "文字列"}, {"data_type": None}]}}}) == []
    print("[selftest OK] verify_partials.warn")


def main(argv=None):
    """CLI の入口。入力を検め、検査して終了コード 0/1/2 を返す。"""
    p = argparse.ArgumentParser(description="extract_react_draft.py の下書き JSON を merge 前に検査する")
    p.add_argument("partial", nargs="?", help="下書き JSON のパス")
    p.add_argument("--screen-name", help="画面名(trigger 文の先頭に来るべき語)")
    p.add_argument("--selftest", action="store_true", help="組込みの検査を走らせる")
    p.add_argument("--units", nargs="?", const="", default=None,
                   help="委譲の単位ごとの断片 _unit_*.json のフォルダ(省略時 target/intermediate)を検査する")
    p.add_argument("--unit", help="--units と併用。その単位に関わる問題だけを出す")
    p.add_argument("--feature", help="--units と併用。検査する画面の機能名(省略時は環境変数 FEATURE_NAME。どちらも無いと止める)")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.units is not None:
        return main_units(args.units, args.unit, args.feature)
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
