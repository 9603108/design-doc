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
            if m:
                actual.append(m.group(1))
            else:
                problems.append(f"[details] event_processes {ev}: 呼出の記法に合わない行 {line!r}")
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
    except AssertionError as e:
        print(f"[selftest NG] {e}")
        return 1
    print("[selftest OK] verify_partials")
    return 0


def main(argv=None):
    """CLI の入口。入力を検め、検査して終了コード 0/1/2 を返す。"""
    p = argparse.ArgumentParser(description="extract_react_draft.py の下書き JSON を merge 前に検査する")
    p.add_argument("partial", nargs="?", help="下書き JSON のパス")
    p.add_argument("--screen-name", help="画面名(trigger 文の先頭に来るべき語)")
    p.add_argument("--selftest", action="store_true", help="組込みの検査を走らせる")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
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
