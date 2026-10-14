#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fill_common_logic_callers.py — common_logic[].used_by_processes_ids を埋める(round9 B13)

WHY: 共通ロジックの呼出元は S1 が名前(used_in / used_by_processes)で書く。名前のままでは docx の
「呼出元」が ID を出せず「（該当なし）」になる。IDs が確定した後(migrate_v48_to_v49.py の後)に、
名前を FR・BE の ID へ結ぶ。結び先は中間 JSON と screen_config の事実だけで、画面名・処理名は決め打ちしない。

語ごとの結び順(先に当たったものを採る):
  1. 語が処理の ID(front_processes[].process_id / backend_processes[].process_id)→ そのまま
  2. 語が front_processes[].name に完全一致 → その FR
  3. 語が events の番号・旧コード(no / event_code / legacy_event_code)→ process_names_by_event_code の名前 → FR
  4. 語が backend_processes[].endpoint.action か name に一致 → その BE と、その BE を呼ぶ FR
結べない語は used_by_processes に残し、標準エラーに WARN を出す(終了コードは 0。空のまま残す判定は検査の担当)。

runs_on が「サーバー」「データベース」を含む行は、結んだ FR が steps の backend_call で呼ぶ BE も加える。
WHY: サーバー・DB で動くロジックは、呼ぶ FR が呼ぶ BE の中で動くため、呼出元の鎖として BE も示す。

冪等: 既存の used_by_processes_ids に足すだけで、重複は入れない。2回流しても結果は変わらない。
入出力: target/intermediate/{機能名}.json を読み、同じファイルへ書く(他の欄は触らない)。

呼出元: run_units.py の AFTER(migrate_v48_to_v49.py の直後)。
"""
import json
import sys

from _config_loader import INTERMEDIATE_DIR, get_feature_name_from_argv, get_process_names_by_event_code


def _fr_id(fp):
    return fp.get("process_id") or fp.get("no") or ""


def _backend_ids_of(fp):
    """FR の steps が backend_call で呼ぶ BE の ID を、出現順・重複なしで返す。"""
    out = []
    for s in fp.get("steps", []) or []:
        if s.get("kind") in ("backend_call", "api_call"):
            bid = (s.get("backend_call") or {}).get("backend_id", "")
            if bid and bid not in out:
                out.append(bid)
    return out


def build_index(data, pnames):
    fr_by_id, fr_by_name = {}, {}
    for fp in data.get("front_processes", []) or []:
        fid = _fr_id(fp)
        if not fid:
            continue
        fr_by_id[fid] = fp
        if fp.get("name"):
            fr_by_name.setdefault(fp["name"], []).append(fid)

    # WHY: 呼出元の語は events の番号か旧コードで書かれる。処理名への写しは screen_config の事実を使う。
    ev_to_name = {}
    for ev in data.get("events", []) or []:
        code = ev.get("event_code") or ev.get("legacy_event_code") or ""
        pname = pnames.get(code) if code else None
        if not pname:
            continue
        for t in (ev.get("no"), code):
            if t not in (None, ""):
                ev_to_name.setdefault(str(t), pname)

    # WHY: BE を呼ぶ FR を逆引きする。API の action で書かれた語は、その BE と呼出元の FR の両方を示す。
    callers_of_be = {}
    for fid, fp in fr_by_id.items():
        for bid in _backend_ids_of(fp):
            callers_of_be.setdefault(bid, []).append(fid)
    be_ids, be_by_key = set(), {}
    for bp in data.get("backend_processes", []) or []:
        bid = bp.get("process_id") or ""
        if not bid:
            continue
        be_ids.add(bid)
        keys = [(bp.get("endpoint") or {}).get("action"), bp.get("name")]
        for k in keys:
            if k and k not in be_by_key:
                be_by_key[k] = [bid] + [f for f in callers_of_be.get(bid, []) if f != bid]
    return {"fr_by_id": fr_by_id, "be_ids": be_ids, "fr_by_name": fr_by_name,
            "ev_to_name": ev_to_name, "be_by_key": be_by_key}


def resolve(tok, idx):
    """語を処理の ID の並びに結ぶ。結べなければ空の並び。"""
    if tok in idx["fr_by_id"] or tok in idx["be_ids"]:
        return [tok]
    if tok in idx["fr_by_name"]:
        return list(idx["fr_by_name"][tok])
    if tok in idx["ev_to_name"]:
        return list(idx["fr_by_name"].get(idx["ev_to_name"][tok], []))
    if tok in idx["be_by_key"]:
        return list(idx["be_by_key"][tok])
    return []


def fill(data, pnames, log):
    """common_logic[] の used_by_processes_ids を埋める。結べなかった語は log に WARN を積む。"""
    idx = build_index(data, pnames)
    for cl in data.get("common_logic", []) or []:
        names = list(cl.get("used_by_processes") or [])
        used_in = cl.get("used_in") or []
        for u in ([used_in] if isinstance(used_in, str) else used_in):
            if u not in names:
                names.append(u)
        ids = list(cl.get("used_by_processes_ids") or [])
        resolved = []
        for tok in names:
            got = resolve(tok, idx)
            if not got:
                log.append(f"[WARN] common_logic「{cl.get('name')}」の呼出元「{tok}」を処理の ID に結べない")
                continue
            resolved += got
        # WHY: サーバー・DB で動くロジックは、結んだ FR が呼ぶ BE の中でも動く。BE も呼出元の鎖に入れる。
        ro = cl.get("runs_on") or cl.get("runtime_location") or ""
        if "サーバー" in ro or "データベース" in ro:
            for rid in list(resolved):
                if rid in idx["fr_by_id"]:
                    resolved += _backend_ids_of(idx["fr_by_id"][rid])
        for rid in resolved:
            if rid not in ids:
                ids.append(rid)
        cl["used_by_processes_ids"] = ids


def selftest():
    data = {
        "events": [{"no": "EV1", "event_code": "EV00"}, {"no": "EV2", "event_code": "EV01"}],
        "front_processes": [
            {"process_id": "FR1", "name": "初期表示", "steps": [{"kind": "backend_call", "backend_call": {"backend_id": "BE1"}}]},
            {"process_id": "FR2", "name": "集計", "steps": [{"kind": "backend_call", "backend_call": {"backend_id": "BE2"}}]},
        ],
        "backend_processes": [
            {"process_id": "BE1", "name": "店舗一覧", "endpoint": {"action": "list_shops"}},
            {"process_id": "BE2", "name": "集計処理", "endpoint": {"action": "list_report"}},
        ],
        "common_logic": [
            {"name": "A", "used_in": ["EV1", "初期表示"], "runs_on": "ブラウザ"},
            {"name": "B", "used_in": ["list_report"], "runs_on": "データベース"},
            {"name": "C", "used_in": ["どこにも無い"]},
            {"name": "D", "used_in": ["集計"], "used_by_processes_ids": ["FR9"]},
            {"name": "E", "used_in": ["初期表示"], "runs_on": "サーバー(API)"},
        ],
    }
    pn = {"EV00": "初期表示", "EV01": "集計"}
    log = []
    fill(data, pn, log)
    cl = {c["name"]: c for c in data["common_logic"]}
    assert cl["A"]["used_by_processes_ids"] == ["FR1"], cl["A"]
    assert cl["B"]["used_by_processes_ids"] == ["BE2", "FR2"], cl["B"]
    assert cl["C"]["used_by_processes_ids"] == [], cl["C"]
    assert cl["D"]["used_by_processes_ids"] == ["FR9", "FR2"], cl["D"]
    assert cl["E"]["used_by_processes_ids"] == ["FR1", "BE1"], cl["E"]
    assert len(log) == 1 and "どこにも無い" in log[0], log
    # 冪等: 2回目で結果が変わらず、WARN も増えない
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    log2 = []
    fill(data, pn, log2)
    assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before and len(log2) == 1, log2
    print("[selftest OK] fill_common_logic_callers")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    feat = get_feature_name_from_argv()
    path = INTERMEDIATE_DIR / f"{feat}.json"
    if not path.exists():
        print(f"[ERROR] 中間 JSON が見つかりません: {path}", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    log = []
    fill(data, get_process_names_by_event_code(feat), log)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for line in log:
        print(line, file=sys.stderr)
    print(f"[done] common_logic {len(data.get('common_logic', []) or [])} 件の呼出元を補った(WARN {len(log)} 件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
