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
ただし加えるのは、次のどちらかで BE が共通ロジックを使うと判る場合だけ。
  (a) BE のソース範囲(下書き apis.<action>.src の行)に、共通ロジックと行範囲が重なる下書き候補
      (backend 下書きの logic_candidates)の name か text が実在する
  (b) BE のソースが python で、共通ロジックの出典の行に重なるモジュール直下の名前(定数・関数・クラス)を、
      BE の関数から同じモジュール内の参照を推移で辿って使っている(_module_reach)
どちらも判らないとき(下書き候補も出典の定義も無い)だけ、従来どおり FR の BE を全部足し、WARN を1行出す。
WHY: 候補が無くても、定数や補助関数の参照で BE を絞れる。候補と行が重ならない共通ロジックで「全部足す」に
倒れると、別の BE まで呼出元に入る(run10 の CL4・CL5 は BE1 が誤って入っていた)。
WHY: サーバー・DB で動くロジックは、呼ぶ FR が呼ぶ BE の中で動くため、呼出元の鎖として BE も示す。
ただし FR が呼ぶ BE の全部では動かない(同じ FR が別の BE も呼ぶ)ため、本文で絞る。
used_by_processes の API 名(action)は、その BE の name に置き換える(呼出元の表示を名前で揃える)。

冪等: 既存の used_by_processes_ids に足すだけで、重複は入れない。2回流しても結果は変わらない。
入出力: target/intermediate/{機能名}.json を読み、同じファイルへ書く(他の欄は触らない)。
下書き: target/intermediate/{機能名}_backend_draft.json を読む(無ければ候補なし扱い)。

呼出元: run_units.py の AFTER(migrate_v48_to_v49.py の直後)。
"""
import ast
import json
import os
import re
import sys
from pathlib import Path

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
    # WHY: 下書き(apis)は action 名で引くため、BE の ID から action と name を引けるようにする。
    be_action, be_name, bid_by_action = {}, {}, {}
    for bp in data.get("backend_processes", []) or []:
        bid = bp.get("process_id") or ""
        if not bid:
            continue
        be_ids.add(bid)
        action = (bp.get("endpoint") or {}).get("action")
        be_action[bid] = action
        be_name[bid] = bp.get("name")
        if action:
            bid_by_action.setdefault(action, bid)
        keys = [action, bp.get("name")]
        for k in keys:
            if k and k not in be_by_key:
                be_by_key[k] = [bid] + [f for f in callers_of_be.get(bid, []) if f != bid]
    return {"fr_by_id": fr_by_id, "be_ids": be_ids, "fr_by_name": fr_by_name,
            "ev_to_name": ev_to_name, "be_by_key": be_by_key,
            "be_action": be_action, "be_name": be_name, "bid_by_action": bid_by_action}


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


def _same_file(a, b):
    """出典のファイルが同じか。パスは正規化して比べる。"""
    return bool(a) and bool(b) and os.path.normpath(a) == os.path.normpath(b)


def _overlaps(srcs_a, srcs_b):
    """2つの出典の並びのどれかが、同じファイルで行範囲が重なるか。"""
    for a in srcs_a or []:
        for b in srcs_b or []:
            # fallback-ok: 行番号が欠けた出典は、行を比べられないので重ならない扱いにする(0 で比較)
            if (_same_file(a.get("file"), b.get("file"))
                    and a.get("line_start", 0) <= b.get("line_end", 0)
                    and b.get("line_start", 0) <= a.get("line_end", 0)):
                return True
    return False


def _keys_of_common(cl, cands):
    """共通ロジックと出典の行が重なる下書き候補から、BE の本文を探す手掛かり(name / text)を取る。"""
    keys = []
    for c in cands:
        if not _overlaps(cl.get("src"), c.get("src")):
            continue
        if c.get("name"):
            keys.append(("name", c["name"]))
        if c.get("text"):
            keys.append(("text", c["text"]))
    return keys


def _range_text(srcs, cache):
    """出典の並び(file・line_start・line_end)が指す行を、改行でつないだ文字列で返す。読めないファイルは空。"""
    parts = []
    for s in srcs or []:
        # fallback-ok: file が無い出典は本文を読めないので飛ばす(空文字は下で読めない扱い)
        f = s.get("file") or ""
        if not f:
            continue
        if f not in cache:
            try:
                cache[f] = Path(f).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                cache[f] = []
        lines = cache[f]
        # fallback-ok: 行番号が欠けた出典は範囲を取れないので、先頭から0行目までとして空にする
        parts.append("\n".join(lines[s.get("line_start", 1) - 1: s.get("line_end", 0)]))
    return "\n".join(parts)


def _squash(s):
    """空白(改行・全角空白を含む)を全部取った文字列。text の照合に使う。"""
    return "".join(s.split())


def _mentions(body, kind, value):
    """本文に手掛かりがあるか。name は識別子として単語で探し、text は空白を無視して部分一致で探す。"""
    if kind == "name":
        return re.search("(?<![A-Za-z0-9_])" + re.escape(value) + "(?![A-Za-z0-9_])", body) is not None
    return bool(_squash(value)) and _squash(value) in _squash(body)


def _be_fits(bid, keys, idx, apis, cache):
    """BE のソース範囲(下書き apis.<action>.src)に、共通ロジックの手掛かりのどれかがあるか。"""
    action = idx["be_action"].get(bid)
    body = _range_text((apis.get(action) or {}).get("src"), cache)  # fallback-ok: action が無い BE は本文なし(下の any が偽になる)
    return any(_mentions(body, kind, value) for kind, value in keys)


def _target_names(t):
    """代入の左辺から直下の名前(Name)を取る。属性・添字への代入は名前を作らないので取らない。"""
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, (ast.Tuple, ast.List)):
        return [n for e in t.elts for n in _target_names(e)]
    return []


def _top_names(st):
    """モジュール直下の文が定義する名前(関数・クラス・代入の左辺)。"""
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [st.name]
    if isinstance(st, ast.Assign):
        return [n for t in st.targets for n in _target_names(t)]
    if isinstance(st, ast.AnnAssign):
        return _target_names(st.target)
    return []


def _index_module(tree):
    """モジュール直下の文ごとに、行範囲(start・end)・定義する名前・本文で参照する直下の名前を並べる。"""
    items = []
    for st in tree.body:
        names = _top_names(st)
        if not names:
            continue
        # WHY: デコレータの行も定義の一部として扱う(関数の行範囲の頭に含める)
        start = min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", [])])
        items.append((start, st.end_lineno, names, st))
    top = {n for _, _, names, _ in items for n in names}
    return [{"start": s, "end": e, "names": set(names),
             "refs": {node.id for node in ast.walk(st) if isinstance(node, ast.Name)} & top}
            for s, e, names, st in items]


def _module_items(path, cache):
    """python モジュールの直下の文の索引。python でない・読めない・構文エラーのファイルは None(判定なし)。"""
    if path in cache:
        return cache[path]
    items = None
    # fallback-ok: python でないファイル・読めないファイル・構文エラーは判定なし(None)にして、呼出元の従来判定に任せる
    if path.endswith(".py"):
        try:
            items = _index_module(ast.parse(Path(path).read_text(encoding="utf-8"), filename=path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            items = None
    cache[path] = items
    return items


def _overlaps_lines(item, start, end):
    """索引の1文の行範囲と、行範囲 start〜end が重なるか。"""
    return item["start"] <= end and start <= item["end"]


def _module_reach(cl, bid, idx, apis, cache):
    """共通ロジックの出典の行に重なる直下の名前(defined)を、BE の関数から参照で辿れるか。
    BE のソース範囲(apis.<action>.src)ごとに判定し、どれかが辿れれば True。
    True: 辿れる / False: どれも辿れない / None: 判定できない(出典の定義が無い・python でない・BE の出典が無い)。"""
    action = idx["be_action"].get(bid)
    verdicts = []
    for s in (apis.get(action) or {}).get("src") or []:  # fallback-ok: action が無い BE は出典なし(None)
        f = s.get("file") or ""
        items = _module_items(f, cache) if f else None
        if items is None:
            continue
        defined = set()
        for c in cl.get("src") or []:
            if not _same_file(c.get("file"), f):
                continue
            for it in items:
                if _overlaps_lines(it, c.get("line_start", 0), c.get("line_end", 0)):  # fallback-ok: 行番号が欠けた出典は 0 で比べ、どの文とも重ならない
                    defined |= it["names"]
        seeds = [it for it in items if _overlaps_lines(it, s.get("line_start", 0), s.get("line_end", 0))]  # fallback-ok: 同上
        if not defined or not seeds:
            verdicts.append(None)
            continue
        by_name = {}
        for it in items:
            for n in it["names"]:
                by_name.setdefault(n, []).append(it)
        reached, todo = set(), [n for it in seeds for n in it["names"]]
        while todo:
            n = todo.pop()
            if n in reached:
                continue
            reached.add(n)
            for it in by_name.get(n, []):
                todo.extend(it["refs"] - reached)
        verdicts.append(bool(defined & reached))
    if True in verdicts:
        return True
    if False in verdicts:
        return False
    return None


def _name_of_api(tok, idx):
    """used_by_processes の語が API 名(action)なら、その BE の name に置き換える。処理の ID・名前はそのまま。"""
    if tok in idx["fr_by_id"] or tok in idx["be_ids"] or tok in idx["fr_by_name"]:
        return tok
    bid = idx["bid_by_action"].get(tok)
    if not bid:
        return tok
    # fallback-ok: name が空の BE は名前が無いので、API 名のまま残す
    return idx["be_name"].get(bid) or tok


def fill(data, pnames, log, draft=None):
    """common_logic[] の used_by_processes_ids を埋める。draft は backend 下書き(logic_candidates・apis)。
    結べなかった語は log に WARN を積む。"""
    idx = build_index(data, pnames)
    # fallback-ok: 下書きが無いときは候補も本文も無い扱い(呼ぶ BE を絞れないので WARN が出る)
    draft = draft or {}
    cands = draft.get("logic_candidates") or []  # fallback-ok: 同上
    apis = draft.get("apis") or {}  # fallback-ok: 同上
    cache = {}
    mod_cache = {}
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
        # ただし FR の BE の全部ではなく、BE の本文に共通ロジックの手掛かりがあるものだけ入れる。
        ro = cl.get("runs_on") or cl.get("runtime_location") or ""  # fallback-ok: 既存の判定と同じ(動作場所が無ければ対象外)
        if "サーバー" in ro or "データベース" in ro:
            keys = _keys_of_common(cl, cands)
            fallback_used = False
            for rid in list(resolved):
                if rid not in idx["fr_by_id"]:
                    continue
                for bid in _backend_ids_of(idx["fr_by_id"][rid]):
                    if keys and _be_fits(bid, keys, idx, apis, cache):
                        resolved.append(bid)
                        continue
                    reach = _module_reach(cl, bid, idx, apis, mod_cache)
                    if reach is True:
                        resolved.append(bid)
                    elif reach is None and not keys:
                        # WHY: 候補も出典の定義も無く、どの BE を呼ぶかを絞れないときだけ従来どおり全部足す
                        resolved.append(bid)
                        fallback_used = True
            if fallback_used:
                log.append(f"[WARN] common_logic「{cl.get('name')}」は下書きの候補と行が重ならず、呼ぶ BE を絞れない(FR の BE を全部足す)")
        for rid in resolved:
            if rid not in ids:
                ids.append(rid)
        cl["used_by_processes_ids"] = ids
        # WHY: used_by_processes に API 名(action)で書かれた語は、呼出元の表示を揃えるため BE の name に直す。
        if isinstance(cl.get("used_by_processes"), list):
            cl["used_by_processes"] = [_name_of_api(t, idx) for t in cl["used_by_processes"]]


def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        be1, be2, be3 = t / "be1.py", t / "be2.py", t / "be3.py"
        be1.write_text('def list_shops(p):\n    w = "t.c >  0"\n    return sql(w + " AND t.z  =  9")\n', encoding="utf-8")
        be2.write_text('def list_report(p):\n    return sql(COND_B2 + " AND t.c>0")\nCOND_B2 = "t.b IS NULL"\n', encoding="utf-8")
        # 候補が無い形: CONST_X は list_x だけが参照する定数、list_y は参照しない
        be3.write_text('def list_x(p):\n    return sql(CONST_X)\nCONST_X = "t.q = 1"\ndef list_y(p):\n    return sql("t.r = 2")\n', encoding="utf-8")
        # 下書き: BE1・BE2 の行範囲と、共通ロジックの候補(名前あり・名前なし・出典の行が無いもの)
        draft = {
            "apis": {
                "list_shops": {"src": [{"file": str(be1), "line_start": 1, "line_end": 3}]},
                "list_report": {"src": [{"file": str(be2), "line_start": 1, "line_end": 3}]},
                "list_x": {"src": [{"file": str(be3), "line_start": 1, "line_end": 2}]},
                "list_y": {"src": [{"file": str(be3), "line_start": 4, "line_end": 5}]},
            },
            "logic_candidates": [
                {"name": "COND_B2", "text": "t.b IS NULL", "src": [{"file": str(be2), "line_start": 3, "line_end": 3}]},
                {"name": "LIMIT_BOTH", "text": "t.c > 0", "src": [{"file": str(be1), "line_start": 2, "line_end": 2}]},
                {"name": "ZONE_NOT_IN_SRC", "text": "t.z = 9", "src": [{"file": str(be1), "line_start": 3, "line_end": 3}]},
            ],
        }
        data = {
            "events": [{"no": "EV1", "event_code": "EV00"}, {"no": "EV2", "event_code": "EV01"}],
            "front_processes": [
                {"process_id": "FR1", "name": "初期表示", "steps": [{"kind": "backend_call", "backend_call": {"backend_id": "BE1"}}]},
                {"process_id": "FR2", "name": "集計", "steps": [{"kind": "backend_call", "backend_call": {"backend_id": "BE2"}}]},
                {"process_id": "FR3", "name": "別の画面", "steps": [{"kind": "backend_call", "backend_call": {"backend_id": "BE3"}}, {"kind": "backend_call", "backend_call": {"backend_id": "BE4"}}]},
            ],
            "backend_processes": [
                {"process_id": "BE1", "name": "店舗一覧", "endpoint": {"action": "list_shops"}},
                {"process_id": "BE2", "name": "集計処理", "endpoint": {"action": "list_report"}},
                {"process_id": "BE3", "name": "別画面A", "endpoint": {"action": "list_x"}},
                {"process_id": "BE4", "name": "別画面B", "endpoint": {"action": "list_y"}},
            ],
            "common_logic": [
                {"name": "A", "used_in": ["EV1", "初期表示"], "runs_on": "ブラウザ"},
                {"name": "B", "used_in": ["list_report"], "runs_on": "データベース"},
                {"name": "C", "used_in": ["どこにも無い"]},
                {"name": "D", "used_in": ["集計"], "used_by_processes_ids": ["FR9"]},
                {"name": "E", "used_in": ["初期表示"], "runs_on": "サーバー(API)"},
                {"name": "F", "used_by_processes": ["list_shops", "初期表示"], "runs_on": "ブラウザ"},
                # BE2 だけが使う条件(COND_B2 は BE2 の行にだけ在る)
                {"name": "CL_A", "used_in": ["集計", "初期表示"], "runs_on": "データベース",
                 "src": [{"file": str(be2), "line_start": 3, "line_end": 3}]},
                # 両方が使う条件(LIMIT_BOTH の text は両方の BE に空白違いで在る)
                {"name": "CL_B", "used_in": ["初期表示", "集計"], "runs_on": "データベース",
                 "src": [{"file": str(be1), "line_start": 2, "line_end": 2}]},
                # 名前が無く本文だけ一致(text は BE1 にだけ空白違いで在る)
                {"name": "CL_C", "used_in": ["初期表示", "集計"], "runs_on": "データベース",
                 "src": [{"file": str(be1), "line_start": 3, "line_end": 3}]},
                # 候補は無いが、出典の定数を list_x だけが参照する(BE3 だけ入り、BE4 は入らない・WARN は出ない)
                {"name": "CL_E", "used_in": ["別の画面"], "runs_on": "データベース",
                 "src": [{"file": str(be3), "line_start": 3, "line_end": 3}]},
                # 候補と行が重ならない(絞れない → 従来どおり全部足して WARN)
                {"name": "CL_D", "used_in": ["集計"], "runs_on": "データベース",
                 "src": [{"file": str(t / "none.py"), "line_start": 1, "line_end": 1}]},
            ],
        }
        pn = {"EV00": "初期表示", "EV01": "集計"}
        log = []
        fill(data, pn, log, draft)
        cl = {c["name"]: c for c in data["common_logic"]}
        assert cl["A"]["used_by_processes_ids"] == ["FR1"], cl["A"]
        assert cl["B"]["used_by_processes_ids"] == ["BE2", "FR2"], cl["B"]
        assert cl["C"]["used_by_processes_ids"] == [], cl["C"]
        assert cl["D"]["used_by_processes_ids"] == ["FR9", "FR2"], cl["D"]
        assert cl["E"]["used_by_processes_ids"] == ["FR1", "BE1"], cl["E"]
        assert cl["F"]["used_by_processes_ids"] == ["BE1", "FR1"], cl["F"]
        assert cl["F"]["used_by_processes"] == ["店舗一覧", "初期表示"], "API 名が BE の name に直っていない"
        assert cl["CL_A"]["used_by_processes_ids"] == ["FR2", "FR1", "BE2"], cl["CL_A"]
        assert cl["CL_B"]["used_by_processes_ids"] == ["FR1", "FR2", "BE1", "BE2"], cl["CL_B"]
        assert cl["CL_C"]["used_by_processes_ids"] == ["FR1", "FR2", "BE1"], cl["CL_C"]
        assert cl["CL_D"]["used_by_processes_ids"] == ["FR2", "BE2"], cl["CL_D"]
        assert cl["CL_E"]["used_by_processes_ids"] == ["FR3", "BE3"], cl["CL_E"]
        assert not any("CL_E" in m or "別の画面" in m for m in log), log
        # WARN: C は結べない語(1件)。B・E・CL_D は候補と行が重ならず絞れない(各1件)。計4件
        assert len(log) == 4, log
        assert sum("どこにも無い" in m for m in log) == 1, log
        assert sum("絞れない" in m for m in log) == 3, log
        # 冪等: 2回目で結果が変わらず、WARN も増えない
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        log2 = []
        fill(data, pn, log2, draft)
        assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before and len(log2) == 4, log2
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
    draft_path = INTERMEDIATE_DIR / f"{feat}_backend_draft.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else {}
    data = json.loads(path.read_text(encoding="utf-8"))
    log = []
    fill(data, get_process_names_by_event_code(feat), log, draft)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for line in log:
        print(line, file=sys.stderr)
    print(f"[done] common_logic {len(data.get('common_logic', []) or [])} 件の呼出元を補った(WARN {len(log)} 件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
