#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_react_draft.py — extract_react_draft.py の下書きを、partial / 中間JSON へ機械的に当てはめる

【目的】
Phase 1 の LLM は業務語彙の文面だけを書く。events と API の対応の骨格(どのイベントがどの action を呼ぶか)は
下書きが持つので、それを手で写さずに機械で当てはめ、写し間違いと並びのぶれを無くす。
  --stage partial (merge_partials.py の前):
      _partial_backend.json の db_operations[].api_endpoint.trigger_events を、下書きの同じ action の値で埋める。
      同じ action を複数の db_operations に分けている案件(売上レポートは3件)では、同じ action を持つ全件に写す
  --stage merge   (merge_partials.py の後、Phase 4 の前):
      {機能名}.json の event_processes[].details の呼出の行を、下書きの並びと数で保証する。
      details が空なら下書きの呼出の行(「・<action> をリクエストする」)を入れる。LLM が書いた業務語彙の文は残し、
      呼出の行の数(「API」「呼出」「リクエスト」等を含む行)が下書きと同じことだけを確かめる

【意味合い】
- 既存の値と下書きが食い違うときは上書きしない。「[種別] 場所: 内容」を1行1件で出し、終了コード 1 にする
  (下書きを正とするか、既存を正とするかは人が決める)。食い違わない箇所だけは書く
- EV00(初期表示)の呼出は初期処理(screen_config の initial_processes)側で持つので、trigger_events には写さず、
  event_processes の details にも入れない(merge_partials.py も EV00 の event_processes は作らない)

【入出力】
  入力: 位置引数 feature(機能名。省略時は環境変数 FEATURE_NAME)、--stage {partial,merge}、
        --draft <下書きJSON>(省略時は target/intermediate/{機能名}_react_draft.json)
  終了コード: 0=食い違い無し / 1=食い違いが1件以上 / 2=入力不正
  --selftest: 組込みの検査(feature・--stage は不要)

【接続情報】
  - 下書きの作り手: extract_react_draft.py(キー: events / db_operations / event_processes)
  - 呼出の行の記法・判定語: verify_partials.DETAIL_RE / migrate_v17_to_v18 の backend_call 判定語と同じ
  - 後続: merge_partials.py(partial の trigger_events から db_operations_ref を作る)、migrate_v17_to_v18.py
  例: python3 target/scripts/apply_react_draft.py 売上レポート --stage partial
"""
import argparse
import copy
import json
import re
import sys
from pathlib import Path

from _config_loader import INTERMEDIATE_DIR, resolve_feature_name
# 呼出の行の判定語(CALL_WORDS / is_call_line)は verify_partials に置き、検査と当てはめで同じ語を使う
from verify_partials import DETAIL_RE, is_call_line

INIT_EVENT = "EV00"


def apply_partial(draft, backend, event_codes):
    """backend(_partial_backend の dict)の db_operations に、下書きの trigger_events を写す。

    戻り: (書いた db_operations の件数, 食い違いの list)。backend は破壊的に更新する。
    event_codes: partial の events の event_code の集合(無ければ None で、実在の確認を省く)。
    """
    want = {}
    for op in draft.get("db_operations") or []:
        api = op.get("api_endpoint") or {}
        want[api.get("action")] = [c for c in (api.get("trigger_events") or []) if c != INIT_EVENT]
    changed, conflicts, seen = 0, [], set()
    for i, op in enumerate(backend.get("db_operations") or []):
        api = op.get("api_endpoint") or {}
        act = api.get("action")
        if act not in want:
            continue  # 下書きに無い action は触らない(下書きが解析できなかった可能性がある)
        seen.add(act)
        new = want[act]
        have = list(api.get("trigger_events") or [])
        if api.get("trigger_event"):
            have.append(api["trigger_event"])
        have = [c for c in have if c != INIT_EVENT]
        where = f"db_operations[{i}].api_endpoint({act})"
        if have:
            if set(have) != set(new):
                conflicts.append(f"[trigger_events] {where}: 既存 {have} が下書き {new} と食い違う")
            continue
        if not new:
            continue
        absent = [c for c in new if event_codes is not None and c not in event_codes]
        if absent:
            conflicts.append(f"[参照] {where}: 下書きの {absent} が partial の events に無い")
            continue
        api["trigger_events"] = list(new)
        op["api_endpoint"] = api
        changed += 1
    for act in want:
        if act not in seen and want[act]:
            conflicts.append(f"[action] 下書きの {act} を持つ db_operations が partial に無い")
    return changed, conflicts


def apply_backend(bdraft, backend):
    """extract_backend_draft の出力(apis)を _partial_backend の api_spec と db_operations へ当てはめる。

    空の欄だけを埋める(response_items・error_patterns)。db_operations の dataset は、空か全行が括り表記の仮のときだけ埋める。LLM が書いた中身は上書きせず、
    論理名・業務の文は空のまま残す。api_spec は action、db_operations は api_endpoint.action で対応づける。
    戻り: 書いた欄の件数。backend は破壊的に更新する。
    """
    apis = bdraft.get("apis") or {}
    changed = 0
    for spec in backend.get("api_spec") or []:
        a = apis.get(spec.get("action"))
        if not a:
            continue
        if not spec.get("response_items") and a.get("response_items"):
            spec["response_items"] = [{"no": i, "key": r["key"], "display_name": "", "data_type": "", "source": ""}
                                      for i, r in enumerate(a["response_items"], 1)]
            changed += 1
        if not spec.get("error_patterns") and a.get("error_patterns"):
            spec["error_patterns"] = [dict(e) for e in a["error_patterns"]]
            changed += 1
    for op in backend.get("db_operations") or []:
        a = apis.get((op.get("api_endpoint") or {}).get("action"))
        if not a or not a.get("dataset"):
            continue
        rows = op.get("dataset") or []
        # WHY: LLM が括り表記(「[」「]」「（」を含む論理名・物理名が仮のまま)で書いた行だけの dataset は下書きで置き換える。
        # 正常な行が1つでもあれば LLM の記述を尊重して触らない(下書きとの食い違いは人が決める)
        provisional = [bool(re.search(r"[\[\]（]", f"{r.get('logical_table') or ''}{r.get('physical_table') or ''}"))
                       or not re.fullmatch(r"[A-Za-z0-9_]+", r.get("physical_table") or "") for r in rows]
        if rows and not all(provisional):
            continue
        op["dataset"] = [{"no": i, "logical_table": "", "logical_column": "", "physical_table": d["table"],
                          "physical_column": d["column"]} for i, d in enumerate(a["dataset"], 1)]
        changed += 1
    return changed


def apply_details(draft, merged):
    """merged(中間JSON)の event_processes[].details の呼出の行を、下書きの並びと数に揃える。

    戻り: (書いた event_processes の件数, 食い違いの list)。merged は破壊的に更新する。
    """
    want_by = {p.get("event_code"): p.get("details") or [] for p in draft.get("event_processes") or []}
    changed, conflicts = 0, []
    for ep in merged.get("event_processes") or []:
        code = ep.get("event_code")
        if code == INIT_EVENT or code not in want_by:
            continue
        want = want_by[code]
        # 下書きには業務の文(LLM が書いた行)も入るので、数と並びは呼出の行だけで比べる
        want_calls = [w for w in want if is_call_line(w)]
        have = ep.get("details") or []
        have_calls = [ln for ln in have if is_call_line(ln)]
        where = f"event_processes {code}"
        if not have:
            if want:
                ep["details"] = list(want)
                changed += 1
        elif len(have_calls) != len(want_calls):
            conflicts.append(f"[details] {where}: 既存の呼出の行 {len(have_calls)} 行が下書きの {len(want_calls)} 行と食い違う")
        else:
            got = [DETAIL_RE.match(ln) for ln in have_calls]
            if all(got) and [m.group(1) for m in got] != [DETAIL_RE.match(w).group(1) for w in want_calls]:
                conflicts.append(f"[details] {where}: 呼出の並び {[m.group(1) for m in got]} が下書きと食い違う")
    return changed, conflicts


def _load(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[入力不正] {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"[入力不正] {path}: 最上位がオブジェクトでない", file=sys.stderr)
        return None
    return data


def _save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None):
    """CLI の入口。入力を検め、stage に応じて当てはめ、食い違いを出して終了コード 0/1/2 を返す。"""
    p = argparse.ArgumentParser(description="react_draft を partial / 中間JSON へ機械的に当てはめる")
    p.add_argument("feature", nargs="?", help="機能名(省略時は環境変数 FEATURE_NAME)")
    p.add_argument("--stage", choices=["partial", "merge"], help="partial=merge_partials の前 / merge=merge_partials の後")
    p.add_argument("--draft", help="下書きJSONのパス(省略時は target/intermediate/{機能名}_react_draft.json)")
    p.add_argument("--backend-draft", help="extract_backend_draft の出力JSON(partial のみ・任意)。空欄の欄だけ埋める")
    p.add_argument("--selftest", action="store_true", help="組込みの検査を走らせる")
    args = p.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.stage:
        print("[入力不正] --stage が必要", file=sys.stderr)
        return 2
    feature = resolve_feature_name(args.feature)
    draft = _load(args.draft or INTERMEDIATE_DIR / f"{feature}_react_draft.json")
    if draft is None:
        return 2
    if args.stage == "partial":
        target = INTERMEDIATE_DIR / "_partial_backend.json"
        data = _load(target)
        if data is None:
            return 2
        ev_path = INTERMEDIATE_DIR / "_partial_screen_events.json"
        ev = _load(ev_path) if ev_path.exists() else None
        codes = {e.get("event_code") for e in (ev or {}).get("events", [])} if ev else None
        changed, conflicts = apply_partial(draft, data, codes)
        # backend 下書きは任意。与えられたときだけ api_spec・dataset の空欄を埋める(既存の値は触らない)
        if args.backend_draft:
            bd = _load(args.backend_draft)
            if bd is None:
                return 2
            changed += apply_backend(bd, data)
    else:
        target = INTERMEDIATE_DIR / f"{feature}.json"
        data = _load(target)
        if data is None:
            return 2
        changed, conflicts = apply_details(draft, data)
    if changed:
        _save(target, data)
    for c in conflicts:
        print(c)
    print(f"[{'NG' if conflicts else 'OK'}] stage={args.stage} 書いた={changed} 食い違い={len(conflicts)} -> {target}")
    return 1 if conflicts else 0


def selftest():
    """trigger_events の写し(同じ action の複数 db_operations・EV00 除外・食い違い)と details の保証を assert する。終了コード 0=OK / 1=NG。"""
    draft = {
        "db_operations": [
            {"api_endpoint": {"action": "load", "trigger_events": ["EV00", "EV01", "EV02"]}},
            {"api_endpoint": {"action": "save", "trigger_events": ["EV02"]}},
        ],
        "event_processes": [
            {"event_code": "EV00", "details": ["・load をリクエストする"]},
            {"event_code": "EV01", "details": ["・load をリクエストする"]},
            {"event_code": "EV02", "details": ["・load をリクエストする", "・save をリクエストする"]},
            {"event_code": "EV03", "details": []},
        ],
    }
    try:
        # (a) 同じ action の2件に写る。EV00 は写らない。実在しない event_code は食い違い
        be = {"db_operations": [{"api_endpoint": {"action": "load"}}, {"api_endpoint": {"action": "load"}},
                                {"api_endpoint": {"action": "save"}}]}
        n, bad = apply_partial(draft, be, {"EV00", "EV01", "EV02"})
        assert n == 3 and not bad, f"写し: {n} {bad}"
        assert [o["api_endpoint"]["trigger_events"] for o in be["db_operations"]] == [["EV01", "EV02"]] * 2 + [["EV02"]]
        n, bad = apply_partial(draft, copy.deepcopy(be), None)
        assert n == 0 and not bad, "同じ値の再実行は何もしない"
        be2 = {"db_operations": [{"api_endpoint": {"action": "load", "trigger_events": ["EV09"]}}]}
        n, bad = apply_partial(draft, be2, None)
        assert n == 0 and len(bad) == 2 and be2["db_operations"][0]["api_endpoint"]["trigger_events"] == ["EV09"], f"食い違い: {bad}"
        n, bad = apply_partial(draft, {"db_operations": [{"api_endpoint": {"action": "load"}}]}, {"EV00"})
        assert any(b.startswith("[参照]") for b in bad), f"実在確認: {bad}"
        # (b) 空なら下書きを入れる / 業務語彙の文は残す / 数が違えば食い違い
        mg = {"event_processes": [
            {"event_code": "EV01", "details": []},
            {"event_code": "EV02", "details": ["入力を確定する", "売上を取得する API を呼出す", "保存を呼出す"]},
            {"event_code": "EV03", "details": ["画面を閉じる", "API を呼出す"]},
        ]}
        n, bad = apply_details(draft, mg)
        assert n == 1 and mg["event_processes"][0]["details"] == ["・load をリクエストする"], f"空の details: {n}"
        assert mg["event_processes"][1]["details"][0] == "入力を確定する", "業務語彙の文は残す"
        assert len(bad) == 1 and "EV03" in bad[0], f"数の食い違い: {bad}"
        # (c) backend 下書き: 空欄だけ埋める / 再実行は何もしない / 既存の値は上書きしない
        bd = {"apis": {"a1": {"dataset": [{"column": "p.mise_no", "table": "pos"}],
                              "response_items": [{"key": "data.rows[].id"}],
                              "error_patterns": [{"condition": "x", "status_code": 500, "message": "m"}]}}}
        bk = {"api_spec": [{"action": "a1", "response_items": [], "error_patterns": []}, {"action": "a2"}],
              "db_operations": [{"api_endpoint": {"action": "a1"}, "dataset": []}]}
        n = apply_backend(bd, bk)
        assert n == 3 and bk["api_spec"][0]["response_items"][0]["key"] == "data.rows[].id", f"backend: {n}"
        assert bk["db_operations"][0]["dataset"][0]["physical_column"] == "p.mise_no", "dataset の写し"
        assert apply_backend(bd, bk) == 0, "再実行は何もしない"
        bk2 = {"api_spec": [{"action": "a1", "response_items": [{"key": "keep"}], "error_patterns": [{"x": 1}]}]}
        assert apply_backend(bd, bk2) == 0 and bk2["api_spec"][0]["response_items"][0]["key"] == "keep", "既存は上書きしない"
        # (d) dataset が括り表記だけの行なら下書きで置き換える / 正常な行が1つでもあれば触らない
        bk3 = {"api_spec": [], "db_operations": [{"api_endpoint": {"action": "a1"},
                                                  "dataset": [{"logical_table": "[店舗]", "physical_table": "[店舗テーブル]"}]}]}
        assert apply_backend(bd, bk3) == 1 and bk3["db_operations"][0]["dataset"][0]["physical_column"] == "p.mise_no", "括り表記は置換"
        bk4 = {"api_spec": [], "db_operations": [{"api_endpoint": {"action": "a1"},
                                                  "dataset": [{"logical_table": "店舗", "physical_table": "mise"},
                                                              {"logical_table": "[仮]", "physical_table": "x"}]}]}
        assert apply_backend(bd, bk4) == 0 and bk4["db_operations"][0]["dataset"][0]["physical_table"] == "mise", "正常な行があれば触らない"
    except AssertionError as e:
        print(f"[selftest NG] {e}")
        return 1
    print("[selftest OK] apply_react_draft")
    return 0


if __name__ == "__main__":
    sys.exit(main())
