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
import sys
from pathlib import Path

from _config_loader import INTERMEDIATE_DIR, resolve_feature_name
from verify_partials import DETAIL_RE

# 意味合い: details の1行が「呼出の行」かの判定語。migrate_v17_to_v18 の backend_call 判定(kind 推定)と同じ語にして、
#          ここで「呼出が n 行」と数えたものを、Phase 4 も同じ n 件の backend_call と数える。語が変わるなら両方を直す
CALL_WORDS = ["API", "fetch", "POST", "GET ", "呼び出し", "呼出", "リクエスト"]
INIT_EVENT = "EV00"


def is_call_line(line):
    return any(w in line for w in CALL_WORDS)


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
        have = ep.get("details") or []
        have_calls = [ln for ln in have if is_call_line(ln)]
        where = f"event_processes {code}"
        if not have:
            if want:
                ep["details"] = list(want)
                changed += 1
        elif len(have_calls) != len(want):
            conflicts.append(f"[details] {where}: 既存の呼出の行 {len(have_calls)} 行が下書きの {len(want)} 行と食い違う")
        else:
            got = [DETAIL_RE.match(ln) for ln in have_calls]
            if all(got) and [m.group(1) for m in got] != [DETAIL_RE.match(w).group(1) for w in want]:
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
    except AssertionError as e:
        print(f"[selftest NG] {e}")
        return 1
    print("[selftest OK] apply_react_draft")
    return 0


if __name__ == "__main__":
    sys.exit(main())
