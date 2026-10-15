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
どちらも判らないとき(下書き候補も出典の定義も無い。ただし src はある)だけ、従来どおり FR の BE を全部足し、WARN を1行出す。
src が1件も無い CL には BE を足さず WARN を出す(round13 W3。src は S1 が必ず書き、verify_partials が止める)。
runs_on が「ブラウザ」の CL は、src の関数・定数を TSX で使う箇所(useMemo の依存・state を変えるイベント・CSV の
起点のイベント)から呼出元の FR を引く(_front_callers。react 下書き <機能名>_react_draft.json を読む)。
  出典の項目が「旗: true」(dead: true 等)を持ち、描画側が旗で項目を落とす条件(showDead || !s.dead)を持つときは、
  その条件の state を変えるイベント(空欄項目の表示切替)も呼出元に入れる(round14 R3。dead の項目の計算は条件が真のときだけ走る)。
  関数の呼出が、表示用の state(showDead 等)を参照する描画の中にあるときも、その state を変えるイベントを呼出元に入れる
  (2026-10-15 run16。基準は検査 _vs_flow.check_cl_rerender と同じ。描画に使わず送信時に読むだけの入力欄の state は足さない)。
  state の宣言の初期値の中で呼ぶ関数(`useState(todayJst())` の todayJst)は、初期表示の処理(EV00 の FR)を呼出元に入れる
  (2026-10-15 run17。画面を開いたときに走る。宣言の行は参照に数えないため、これが無いと呼出元が空になり verify_intermediate が止まる)。
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
            "be_action": be_action, "be_name": be_name, "bid_by_action": bid_by_action,
            "callers_of_be": callers_of_be}


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


def _const_text_keys(cl, mod_cache):
    """共通ロジックの出典の行に重なる、python のモジュール直下の `名前 = "文字列"` の文字列(空白を除いて12字以上)を、BE の本文を探す手掛かり(text)にする。
    WHY(2026-10-15 run18 差2): queries_pos.py の LIVE(有効な行の条件)は list_pos_account_report が名前で使い、list_pos_shops は同じ式を
    SQL に直書きしている(:164)。名前の参照(_module_reach)では直書きの BE を拾えないので、定数の文字列そのものが BE の本文に在るかで引く。
    辞書・リストの要素(_GROUPS の "p.mise_no" など、どの SQL にも出る短い断片)は取らない: 直下の代入で値が文字列そのものの定数だけにする。"""
    keys = []
    for c in cl.get("src") or []:
        f = c.get("file") or ""  # fallback-ok: file が無い出典は読めないので飛ばす
        if not f.endswith(".py") or not os.path.isfile(f):
            continue
        if ("tree", f) not in mod_cache:
            try:
                mod_cache[("tree", f)] = ast.parse(Path(f).read_text(encoding="utf-8"), filename=f)
            except (OSError, UnicodeDecodeError, SyntaxError):
                mod_cache[("tree", f)] = None
        tree = mod_cache[("tree", f)]
        for st in (tree.body if tree else []):
            if not (isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)):
                continue
            if st.lineno <= c.get("line_end", 0) and c.get("line_start", 0) <= st.end_lineno and len(_squash(st.value.value)) >= 12:  # fallback-ok: 行番号が欠けた出典は 0 で比べ、どの文とも重ならない
                keys.append(("text", st.value.value))
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


# ---- round13 W3: 画面(ブラウザ)で動く共通ロジックの呼出元を、TSX の使用箇所から機械で引く ----
# WHY: S1 が書く used_in は画面で動く関数の呼出元を取りこぼす(関数が useMemo の中で使われ、その結果が
# 別の state の変化で変わる、という鎖を S1 は辿らない)。事実は react 下書き(events・failure_effects・
# derived_arrays・csv_exports・state_initials)と TSX の行だけで、画面名・関数名は決め打ちしない。
_FRONT_EXT = (".tsx", ".jsx", ".ts", ".js")
_RE_TOP_FUNC = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)")
# WHY(round14 R3): `const RIGHT: Spec[] = [` のように型注釈が付く直下の定数も宣言として拾う。拾えないと、CL の出典が
# 型注釈つきの配列の中の1行(PosReport.tsx の LEFT / RIGHT の spec)のとき、囲む宣言の名前が引けず、使用箇所の鎖が
# 1歩も進まなかった(run13 の CL1〜CL4 は _front_callers が空を返し、呼出元が S1 の語だけになっていた)。
# 型注釈は「:」から最初の「=」まで(型の中に「=」が入る関数型は直下の定数では稀なので扱わない)。
_RE_TOP_CONST = re.compile(r"^(?:export\s+)?const\s+(\w+)\s*(?::[^=]*)?=")
_RE_JS_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_RE_TRUE_FLAG = re.compile(r"(?<![A-Za-z0-9_$])(\w+)\s*:\s*true\b")
_RE_MEMO_DECL = re.compile(r"^\s*(?:const|let)\s+(\w+)\s*=\s*use(?:Memo|Callback)\(")
_RE_MEMO_DEPS = re.compile(r"\[([^\[\]]*)\]\s*\)\s*;?\s*$")


def _word_in(name, line):
    """行に識別子 name が単語として現れるか。"""
    return re.search(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])", line) is not None


def _used_in_initial(name, init):
    """state の初期値の式 init に、識別子 name の使用(呼出・参照)があるか。オブジェクトのキー(`{ from: ...` の from)は使用に数えない。
    WHY(2026-10-15 run17): `useState({ from: todayJst() })` のキー from が、同名の関数 from の使用に取り違えられないようにする
    (_render_ref_states が宣言行のキー名を参照と取り違えない扱いと同じ趣旨)。キーは「{ か , の直後で、名前の直後が : 」の形だけ。
    三項演算子の `c ? FOO : 1` は、FOO の前が ? なのでキーではなく使用に数える。"""
    for m in re.finditer(r"(?<![A-Za-z0-9_$.])" + re.escape(name) + r"(?![A-Za-z0-9_$])", init):
        is_key = re.match(r"\s*:", init[m.end():]) and re.search(r"[{,]\s*$", init[:m.start()])
        if not is_key:
            return True
    return False


def _top_blocks(lines):
    """モジュール直下の宣言ごとの行範囲(1始まり)と宣言名。次の「行頭が英字・/ の行」までを1ブロックとする。"""
    starts = [i for i, ln in enumerate(lines) if ln[:1].isalpha() or ln.startswith("//")]
    blocks = []
    for k, s in enumerate(starts):
        e = (starts[k + 1] - 1) if k + 1 < len(starts) else len(lines) - 1
        m = _RE_TOP_FUNC.match(lines[s]) or _RE_TOP_CONST.match(lines[s])
        blocks.append({"start": s + 1, "end": e + 1, "name": m.group(1) if m else None})
    return blocks


def _memo_regions(lines):
    """useMemo / useCallback の宣言ごとに、行範囲(1始まり)・変数名・依存の名前を取る。
    依存の配列は、宣言の行から30行先までで「, [deps])」で終わる最初の行から読む。"""
    out = []
    for i, ln in enumerate(lines):
        m = _RE_MEMO_DECL.match(ln)
        if not m:
            continue
        for j in range(i, min(i + 30, len(lines))):
            d = _RE_MEMO_DEPS.search(lines[j])
            if d:
                deps = [x.strip() for x in d.group(1).split(",") if x.strip()]
                out.append({"start": i + 1, "end": j + 1, "name": m.group(1), "deps": deps})
                break
    return out


def _spec_flags(lines, ls, le):
    """CL の出典の行(1始まり ls〜le)にある「<名前>: true」の旗の名前を返す(例: dead: true)。
    出典が1項目(オブジェクトの { ...)の先頭の行だけで、閉じる } が次の行以降にあるときは、波括弧が釣り合うまで
    10行を上限に読み足す。"""
    end = min(max(le, ls), len(lines))
    text = "\n".join(lines[max(ls, 1) - 1: end])
    while text.count("{") > text.count("}") and end < min(len(lines), max(ls, 1) + 10):
        end += 1
        text += "\n" + lines[end - 1]
    return _RE_TRUE_FLAG.findall(text)


def _gate_names(lines, flags, chain_lines, blocks):
    """旗で項目を落とす絞り込み(`!<変数>.<旗>` を含む行。例: .filter((s) => showDead || !s.dead))があれば、
    その行に現れる識別子(絞り込みの変数と旗の名前を除く)と、その識別子を JSX の属性 `名前={式}` で渡している式の識別子を返す。
    絞り込みの行は、CL の使用箇所(chain_lines)を含む直下の宣言(コンポーネント)の中にあるものだけ採る。
    WHY: 項目の絞り込みが別の宣言にあるなら、その CL の項目が絞り込まれる描画とは関係が無い。"""
    out = []
    for flag in flags:
        pat = re.compile(r"!\s*(\w+)\s*\.\s*" + re.escape(flag) + r"(?![A-Za-z0-9_$])")
        for n, ln in enumerate(lines, 1):
            m = pat.search(ln)
            if not m:
                continue
            blk = [b for b in blocks if b["start"] <= n <= b["end"] and b["name"]]
            if not blk or not any(blk[0]["start"] <= c <= blk[0]["end"] for c in chain_lines):
                continue
            for t in _RE_JS_IDENT.findall(ln):
                if t in (m.group(1), flag) or t in out:
                    continue
                out.append(t)
                # 属性で渡される名前(コンポーネントの props)なら、渡している式の側の識別子も足す
                for ln2 in lines:
                    for am in re.finditer(r"(?<![A-Za-z0-9_$])" + re.escape(t) + r"\s*=\s*\{([^{}]*)\}", ln2):
                        out.extend(x for x in _RE_JS_IDENT.findall(am.group(1)) if x not in out)
    return out


def _jsx_use_names(lines, n, comp):
    """コンポーネント comp を描く行 n(1始まり。`<comp` を含む行)で、描く値の元になる識別子を返す(重複なし)。
    1. 要素の属性(`<comp` の行から `/>` か閉じの `>` の行まで。30行を上限)に現れる識別子(agg={total} の total など)
    2. その要素を囲む JS の式の行(`{rows.map((r, i) => (` / `{rows.length > 1 && (` のように、要素の手前で閉じていない括弧を開く行)の識別子
    WHY(run15 差3): num()・fmt() を呼ぶのは Card の描画の中で、Card は rows.map で行ごとに描かれる。rows は取得・再取得の FR(店舗の絞り込み・
    集計単位の切替)で置き換わるが、useMemo の依存にも CSV にも現れないため、鎖が Card で止まり FR3・FR6 が呼出元から抜けた。
    識別子が何の state かは呼出側が resolve_state(派生配列・useMemo を辿る)で決める。ここでは名前を拾うだけで、画面名は決め打ちしない。"""
    names = []

    def take(text):
        for t in _RE_JS_IDENT.findall(text):
            if t not in names:
                names.append(t)

    for k in range(n - 1, min(n + 30, len(lines))):
        take(lines[k])
        if "/>" in lines[k] or re.search(r"(?<![=-])>\s*$", lines[k]):
            break
    depth = 0
    for k in range(n - 2, max(n - 12, -1), -1):
        ln = lines[k]
        depth += (ln.count("}") + ln.count(")")) - (ln.count("{") + ln.count("("))
        if depth < 0:
            take(ln)
            depth = 0
    return names


_RE_HEADER_END = re.compile(r"(?:\)|=>)\s*(?::\s*[^{=]+)?[{(]?\s*$")


def _header_end(lines, b):
    """直下のブロック b(宣言の行範囲)の見出し(function Card({ ... }: { ... }) { の引数と型の部分)が終わる行(1始まり)を返す。
    見出しの終わりは、ブロックの先頭から40行までで、行末が `) {`・`=> {`・`=> (` の最初の行。見つからなければ先頭の行。
    WHY(2026-10-15 run16): コンポーネントの引数の宣言(`showDead,` `showDead: boolean;`)は state の参照ではない。
    これを参照に数えると、見出しの直後の8行以内にある関数の呼出が、描画に使わない props の state まで拾ってしまう。"""
    for k in range(b["start"] - 1, min(b["end"], b["start"] + 40)):
        if _RE_HEADER_END.search(lines[k]):
            return k + 1
    return b["start"]


def _fed_props(lines, comp, names):
    """コンポーネント comp を描く JSX の要素(`<comp` の行から最初の `/>` まで。15行を上限)で、names のどれかを含む式を渡している属性名の集合。
    例: `<Card agg={aggs[i]} showDead={show} />` で names が {show} なら {showDead}。呼出元: _render_ref_states。"""
    rx = re.compile(r"(?<![A-Za-z0-9_$.])(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![A-Za-z0-9_$])")
    out = set()
    for i, ln in enumerate(lines):
        if not re.search(r"<\s*" + re.escape(comp) + r"(?![A-Za-z0-9_$])", ln):
            continue
        block = " ".join(lines[i:i + 15])
        end = block.find("/>")
        for m in re.finditer(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*\{([^{}]*)\}", block if end < 0 else block[:end]):
            if rx.search(m.group(2)):
                out.add(m.group(1))
    return out


def _render_ref_states(lines, f, names, own_lines, states, blocks):
    """CL の関数(names)の呼出が、その state を参照する描画の中にある state の名前の集合。
    _vs_flow.check_cl_rerender の「走る条件」と同じ基準(読み方の関数は _vs_flow のものを使う):
      (1) 呼出行が、その state の参照行(宣言・setter の行・コンポーネントの引数の宣言を除く)の直後 8 行以内にある
          (フィルタ条件の state の下で関数を呼ぶ形。例: `.filter((s) => showDead || !s.dead).map((s) => num(...))`)
      (2) 呼出を囲むコンポーネントが、その state(か、そこから作った値)を属性で渡され、その属性を本体で使っている
    クリックで走る関数(handler)の中の呼出は、再描画で走らないので数えない。
    WHY(2026-10-15 run16): _front_callers は取得で置き換わる state(rows)しか依存に足さず、表示用の state(showDead)を変える FR(空欄項目の
    表示切替)が num()・fmt() の呼出元から抜けた。検査(check_cl_rerender)は同じ呼出を「showDead の変化による再描画でも走る」と指摘していた。
    WHY: (2) で「属性を本体で使っている」を求めるのは、props に渡すだけで描画に使わない state(入力欄の state)を足さないため。
    入力欄だけの state(描画に使わず送信時に読むもの)は、(1)(2)のどちらにも当たらない。"""
    from _vs_flow import _derived_names, _enclosing_component, _in_event_handler

    calls = []
    for n, ln in enumerate(lines, 1):
        if n in own_lines or not any(re.search(r"(?<![\w$.])" + re.escape(nm) + r"\s*\(", ln) for nm in names):
            continue
        if not _in_event_handler(lines, n):
            calls.append(n)
    if not calls:
        return set()
    # WHY: 引数の宣言を持つのは function の宣言とコンポーネント(先頭が大文字の const)だけ。`const rows = list.filter(...)` のような
    # 直下の値の宣言は見出しを持たず、行末が `)` の最初の行までを見出しと取り違えると、フィルタの行(参照)まで除いてしまう。
    heads = [(b["start"], _header_end(lines, b)) for b in blocks
             if b["name"] and (_RE_TOP_FUNC.match(lines[b["start"] - 1]) or b["name"][:1].isupper())]
    out = set()
    # WHY(2026-10-15 run16): 宣言の行は、どの state の宣言の行も参照に数えない(検査の decl_lines と同じ)。自分の宣言だけを除くと、
    # `useState({ from: todayJst() })` のようにオブジェクトのキー(from)が別の state(from)の名前と同じ行で、todayJst() の呼出が
    # 「from を参照する描画の中」と取り違えられ、日付の入力欄の FR(集計開始日の入力)が呼出元に入った。
    decl = set()
    for info in states.values():
        for sp in info.get("src") or []:  # fallback-ok: 宣言の出典が無い state は宣言行を除けない(参照に数えるだけ)
            if _same_file(sp.get("file"), f):
                decl.update(range(sp.get("line_start", 0), sp.get("line_end", 0) + 1))  # fallback-ok: 行欠けは範囲が空
    for st, info in states.items():
        setter = info.get("setter")
        word = re.compile(r"(?<![\w$.])" + re.escape(st) + r"(?![\w$])")
        refs = [n for n, ln in enumerate(lines, 1)
                if word.search(ln) and n not in decl
                and not (setter and re.search(r"\b" + re.escape(setter) + r"\b", ln))
                and not any(a <= n <= e for a, e in heads)]
        if any(r <= c <= r + 8 for r in refs for c in calls):
            out.add(st)
            continue
        dn = _derived_names(lines, {st})
        for c in calls:
            comp = _enclosing_component(lines, c)
            if not comp:
                continue
            props = _fed_props(lines, comp, dn)
            for b in blocks:
                if b["name"] == comp and any(_word_in(p, ln) for p in props for ln in lines[_header_end(lines, b):b["end"]]):
                    out.add(st)
            if st in out:
                break
    return out


def _front_callers(cl, data, react_draft, idx, cache=None):
    """ブラウザで動く CL の呼出元の FR の ID を返す(新しく足せるものだけでなく、辿れた全部。重複の除去は呼出側)。
    1. CL の src の行にあるモジュール直下の宣言(function / const)と useMemo の変数を defs にする
    2. 同じファイルで defs を参照する行を探し、useMemo の中なら変数を defs に足し(推移)、依存を state に引き直す
    3. 参照の行がイベントの範囲(events[].src・csv_exports[].src)に入れば、そのイベントを呼出元にする
    3b. 出典の項目が「旗: true」(dead: true 等)を持ち、描画側が `showDead || !s.dead` で旗の項目を落とすなら、
        表示の条件の state(props で渡されるなら渡す元の state)も依存の state にする(round14 R3)
    3c. 参照の行が state の宣言の行で、その state の初期値(state_initials[].initial)に名前があるとき(`useState(todayJst())`)は、
        初期表示のイベント(EV00。マウント時)を呼出元にする(2026-10-15 run17。初期値の呼出は画面を開いたときに走る)
    4. 依存の state を変えるイベント(failure_effects の on_success・ハンドラの set<State>( )も呼出元にする
    5. event_code → idx の ev_to_name → fr_by_name で FR の ID に写す
    react 下書きが無い・src に画面のファイルが無いときは空。"""
    if not react_draft:
        return []
    cache = {} if cache is None else cache
    events = react_draft.get("events") or []  # fallback-ok: 下書きに欄が無ければ何も辿らない
    states = {s.get("state"): s for s in (react_draft.get("state_initials") or []) if s.get("state")}  # fallback-ok: 同上
    derived = {d.get("name"): d.get("source") for d in (react_draft.get("derived_arrays") or []) if d.get("name")}  # fallback-ok: 同上
    ev_codes = []

    def add_ev(code):
        if code and code not in ev_codes:
            ev_codes.append(code)

    # 初期表示(マウント時)のイベントの event_code。パイプラインの決まりで EV00(handler は「useEffect(マウント時)」)。
    # WHY: 宣言の初期値の中で呼ばれる関数の呼出元は、このイベントの FR にする。名前は決め打ちせず、下書きの events から拾う。
    mount_codes = [ev.get("event_code") for ev in events
                   if ev.get("event_code") == "EV00" or "マウント" in ((ev.get("source") or {}).get("handler") or "")]  # fallback-ok: handler が無いイベントはマウント時ではない扱い

    dep_states = set()

    render_states = set()  # 描画の中で走る関数(コンポーネント)に渡る state。取得・再取得で変わるものだけ、下の 4. で依存に足す

    def resolve_state(name, memos, seen, into=None):
        """依存の名前を state に引き直す。派生配列は source へ、別の useMemo はその依存へ辿る。into が無ければ dep_states へ足す。"""
        if name in seen:
            return
        seen.add(name)
        if name in states:
            (dep_states if into is None else into).add(name)
        elif name in derived:
            resolve_state(derived[name], memos, seen, into)
        elif name in memos:
            for d in memos[name]["deps"]:
                resolve_state(d, memos, seen, into)

    for s in cl.get("src") or []:
        f = s.get("file") or ""  # fallback-ok: file が無い出典は読めないので飛ばす
        if not f.endswith(_FRONT_EXT):
            continue
        if f not in cache:
            try:
                cache[f] = Path(f).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                cache[f] = None
        lines = cache[f]
        if not lines:
            continue
        blocks = _top_blocks(lines)
        memo_list = _memo_regions(lines)
        memos = {m["name"]: m for m in memo_list}
        ls, le = s.get("line_start", 0), s.get("line_end", 0)  # fallback-ok: 行番号が欠けた出典は範囲が空
        defs = []
        for n in range(max(ls, 1), min(le, len(lines)) + 1):
            m = _RE_TOP_FUNC.match(lines[n - 1]) or _RE_TOP_CONST.match(lines[n - 1]) or _RE_MEMO_DECL.match(lines[n - 1])
            if m and m.group(1) not in defs:
                defs.append(m.group(1))
        # 出典が宣言の途中の行だけのときは、その行を含むブロックの名前も defs にする
        for b in blocks:
            if b["name"] and b["start"] <= le and ls <= b["end"] and b["name"] not in defs:
                defs.append(b["name"])
        # 出典の行を含む useMemo の変数も defs
        for m in memo_list:
            if m["start"] <= le and ls <= m["end"] and m["name"] not in defs:
                defs.append(m["name"])
        # イベントの範囲(行 → event_code)
        ev_ranges = []
        for ev in events:
            for r in ev.get("src") or []:
                if _same_file(r.get("file"), f):
                    ev_ranges.append((r.get("line_start", 0), r.get("line_end", 0), ev.get("event_code")))  # fallback-ok: 行欠けは範囲が空
        for ce in react_draft.get("csv_exports") or []:  # fallback-ok: 欄が無ければ辿らない
            for r in ce.get("src") or []:
                if _same_file(r.get("file"), f):
                    for code in ce.get("trigger_events") or []:  # fallback-ok: 起点のイベントが無ければ足さない
                        ev_ranges.append((r.get("line_start", 0), r.get("line_end", 0), code))  # fallback-ok: 同上
        # state の宣言(state_initials[].src)ごとの行範囲と初期値の式。宣言の初期値の中の呼出を、下の鎖で初期表示のイベントに結ぶ
        init_ranges = []
        for st_info in states.values():
            for sp in st_info.get("src") or []:  # fallback-ok: 宣言の出典が無い state は初期値の呼出を拾えない(拾わないだけ)
                if _same_file(sp.get("file"), f) and st_info.get("initial"):
                    init_ranges.append((sp.get("line_start", 0), sp.get("line_end", 0), str(st_info["initial"])))  # fallback-ok: 行欠けは範囲が空
        done, todo = set(), list(defs)
        chain_lines = []  # 鎖の中の名前を参照している行(1始まり)。下の「表示の条件」の判定で、描画する宣言を特定する
        while todo:
            name = todo.pop()
            if name in done:
                continue
            done.add(name)
            own = {(b["start"], b["end"]) for b in blocks if b["name"] == name}
            own |= {(m["start"], m["end"]) for m in memo_list if m["name"] == name}
            if name in memos:
                for d in memos[name]["deps"]:
                    resolve_state(d, memos, set())
            for n, ln in enumerate(lines, 1):
                if not _word_in(name, ln) or any(a <= n <= b for a, b in own):
                    continue
                chain_lines.append(n)
                # WHY(2026-10-15 run17): state の宣言の初期値(`useState(todayJst())`)の中で呼ばれる関数は、画面を開いて最初に描くときに走る。
                # 初期表示の処理(EV00。マウント時のイベント)が呼出元になる。ここを拾わないと、todayJst のように宣言の初期値でしか呼ばれない
                # 共通関数は、どのイベントの範囲・useMemo・描画にも当たらず呼出元が空のままになり、verify_intermediate が止まる。
                # 宣言の行そのものは参照として数えない(_render_ref_states)が、初期値の中の呼出は「初期表示で走る」事実なので別に拾う。
                # 名前が初期値の式(initial)に現れるときだけ採る。キー名・state 名が同じ名前でも、初期値に無ければ呼出ではない。
                for a, b, init in init_ranges:
                    if a <= n <= b and _used_in_initial(name, init):
                        for code in mount_codes:
                            add_ev(code)
                # WHY(run15 差3): name がコンポーネントで、この行が `<name` で描いているなら、描く値の元の state を集める。
                # 描画の中で走る関数(num・fmt など)は、その state が変わるたびに走る。useMemo の依存にも CSV にも出ない経路なので、
                # ここで拾わないと鎖が止まる。集めた state のうち取得・再取得で変わるものだけを、下の 4. で依存に足す
                # (入力欄の state まで足すと、旗の無い項目の呼出元が増える。round14 R3 の「旗が無い項目には何も足さない」を保つ)。
                if re.search(r"<\s*" + re.escape(name) + r"(?![A-Za-z0-9_$])", ln):
                    for t in _jsx_use_names(lines, n, name):
                        resolve_state(t, memos, set(), render_states)
                for a, b, code in ev_ranges:
                    if a <= n <= b:
                        add_ev(code)
                inner = [m for m in memo_list if m["start"] <= n <= m["end"]]
                if inner:
                    m = inner[-1]
                    todo.append(m["name"])
                    for d in m["deps"]:
                        resolve_state(d, memos, set())
                else:
                    blk = [b for b in blocks if b["start"] <= n <= b["end"] and b["name"]]
                    if blk and blk[0]["name"] not in done:
                        # 別の直下の関数・定数の中で使われる(補助関数の連鎖)。コンポーネントなら参照先が無く止まる
                        todo.append(blk[0]["name"])
        # WHY(2026-10-15 run16): 関数(num・fmt)の呼出が、表示用の state(showDead)を参照する描画の中にあるなら、その state の変化で
        # 呼出が走り直す(空欄項目が増え、num・fmt が初めて走る)。その state も依存に入れ、下の 4. で変える FR を呼出元にする。
        # 基準は検査(_vs_flow.check_cl_rerender)と同じ。取得で置き換わる state だけを足していた上の render_states の判定を、表示用の state へ広げる。
        # 入力欄だけの state(描画に使わず送信時に読むもの)は _render_ref_states が足さない。
        own_lines = {n for b in blocks if b["name"] in defs for n in range(b["start"], b["end"] + 1)}
        own_lines |= {n for m in memo_list if m["name"] in defs for n in range(m["start"], m["end"] + 1)}
        dep_states |= _render_ref_states(lines, f, defs, own_lines, states, blocks)
        # WHY(round14 R3): 出典の項目が「旗: true」(例: dead: true)を持ち、描画側が `showDead || !s.dead` のように旗で項目を
        # 落としているなら、その項目の計算(calc)は表示の条件の state が真のときだけ描かれる(走る)。条件の state は
        # 依存の state に入れ、下の 4. でその state を変えるイベント(例: 「空欄項目も表示」のチェック)を呼出元にする。
        # 旗が無い項目(常に描かれる)には何も足さない。CL1〜CL3(dead でない項目)の呼出元は変わらない。
        for g in _gate_names(lines, _spec_flags(lines, ls, le), chain_lines, blocks):
            resolve_state(g, memos, set())
    # 4. 依存の state を変えるイベント
    # WHY(run15 差3): 描画に渡る state のうち、API の取得・再取得の成功で置き換わるもの(failure_effects の on_success)は依存に入れる。
    # 取得を起こす FR(最初の取得・再取得)が呼出元になる。
    fetched = {st for fe in react_draft.get("failure_effects") or [] for st in fe.get("on_success") or []}  # fallback-ok: 欄が無ければ足さない
    dep_states |= render_states & fetched
    for fe in react_draft.get("failure_effects") or []:  # fallback-ok: 欄が無ければ足さない
        if any(st in (fe.get("on_success") or []) for st in dep_states):
            add_ev(fe.get("event"))
    for ev in events:
        handler = (ev.get("source") or {}).get("handler") or ""  # fallback-ok: handler が無いイベントは setter を呼ばない扱い
        for st in dep_states:
            setter = (states.get(st) or {}).get("setter")
            if setter and re.search(r"(?<![A-Za-z0-9_$])" + re.escape(setter) + r"\(", handler):
                add_ev(ev.get("event_code"))
    out = []
    for code in ev_codes:
        pname = idx["ev_to_name"].get(code)
        for fid in idx["fr_by_name"].get(pname, []) if pname else []:
            if fid not in out:
                out.append(fid)
    return out


def fill(data, pnames, log, draft=None, react_draft=None):
    """common_logic[] の used_by_processes_ids を埋める。draft は backend 下書き(logic_candidates・apis)、
    react_draft は react 下書き(ブラウザで動く CL の呼出元を引く。無ければ引かない)。
    結べなかった語は log に WARN を積む。"""
    idx = build_index(data, pnames)
    front_cache = {}
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
        if "ブラウザ" in ro:
            # WHY(round13 W3): 画面で動く CL は、TSX の使用箇所・useMemo の依存・state を変えるイベントから FR を引く。
            # S1 の語(used_in)は消さず、辿れた FR を足すだけ。
            # WHY(round13 W3): src が無いと画面の呼出元を引けず、_front_callers は黙って空を返す(呼出元が S1 の語だけになる)。
            # サーバー・DB の CL と同じく WARN で知らせる。呼出元の値は変えない。
            if not cl.get("src"):
                log.append(f"[WARN] common_logic「{cl.get('name')}」は src が無いので画面の呼出元を引けない(呼出元の値は S1 の語のまま)")
            resolved += _front_callers(cl, data, react_draft, idx, front_cache)
        if ("サーバー" in ro or "データベース" in ro) and not cl.get("src"):
            # WHY(round13 W3): src が無いと呼ぶ BE を絞れず、全部足す退避で別の BE まで入る(run12 の新7')。
            # src は S1 が必ず書く(verify_partials が止める)ので、無いものには BE を足さない。
            log.append(f"[WARN] common_logic「{cl.get('name')}」は src が無いので呼ぶ BE を絞れない(BE を足さない)")
        elif "サーバー" in ro or "データベース" in ro:
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
            # WHY(2026-10-15 run18 差2): 上の鎖は S1 の used_in が結んだ FR から BE へ辿るだけで、その FR 以外が呼ぶ BE や、別の BE が
            # 同じ条件を使う(queries_pos.py の CL2 は list_pos_account_report の LIVE と list_pos_shops の直書き)ことを拾えなかった。
            # 全 BE を、この CL の出典を使うと判る場合(上の(a)(b)か、CL の出典の行が BE のソース範囲の中にある)だけ足す。
            # 判らないときの「全部足す」退避はここでは使わない(別の BE まで入る誤りを増やさない)。
            gkeys = keys + _const_text_keys(cl, mod_cache)
            for bid in sorted(idx["be_ids"], key=lambda b: (len(b), b)):
                if bid in resolved:
                    continue
                action = idx["be_action"].get(bid)
                brange = (apis.get(action) or {}).get("src")  # fallback-ok: action が無い BE は範囲なし(重なりなしと扱う)
                if (gkeys and _be_fits(bid, gkeys, idx, apis, cache)) or _overlaps(cl.get("src"), brange) \
                        or _module_reach(cl, bid, idx, apis, mod_cache) is True:
                    resolved.append(bid)
            # WHY(run18 差2): CL を使う BE は、その BE を呼ぶ FR も呼出元。S1 の used_in が一部の FR の名前しか書かなくても、
            # BE の呼出元(FR の steps の backend_call)と同じ集合を機械で入れる(検査 verify_intermediate.check_cl_callers_cover_be が照合する)
            for rid in list(resolved):
                if rid in idx["be_ids"]:
                    resolved.extend(f for f in idx["callers_of_be"].get(rid, []) if f not in resolved)
        for rid in resolved:
            if rid not in ids:
                ids.append(rid)
        cl["used_by_processes_ids"] = ids
        # WHY: used_by_processes に API 名(action)で書かれた語は、呼出元の表示を揃えるため BE の name に直す。
        if isinstance(cl.get("used_by_processes"), list):
            cl["used_by_processes"] = [_name_of_api(t, idx) for t in cl["used_by_processes"]]
        # WHY(2026-10-16 run19 差4): ids へ機械で足した FR(店舗の絞り込みの FR3)の名前は、S1 の used_in・used_by_processes に無いまま
        # 取り残され、同じ CL の中で ids と名前が食い違った。名前の欄は ids から機械で作り直す(S1 の語は消さず、足りない FR の名前だけを足す)。
        # 足すのは FR だけ(BE は S1 が名前で書く語のままにする。CL1・CL2 の used_in に BE の名前を混ぜない)。冪等。
        # 検査 verify_intermediate.check_cl_names_match_ids が、足し漏れ(別経路で ids だけ増えた場合)を ERROR で照合する。
        for key in ("used_in", "used_by_processes"):
            lst = cl.get(key)
            if not isinstance(lst, list):
                continue
            for rid in ids:
                fp = idx["fr_by_id"].get(rid)
                nm = (fp or {}).get("name")
                if nm and nm not in lst and rid not in lst:
                    lst.append(nm)


def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        be1, be2, be3 = t / "be1.py", t / "be2.py", t / "be3.py"
        be1.write_text('def list_shops(p):\n    w = "t.c >  0"\n    return sql(w + " AND t.z  =  9")\n', encoding="utf-8")
        be2.write_text('def list_report(p):\n    return sql(COND_B2 + " AND t.c>0")\nCOND_B2 = "t.b IS NULL"\n', encoding="utf-8")
        # 候補が無い形: CONST_X は list_x だけが参照する定数、list_y は参照しない
        be3.write_text('def list_x(p):\n    return sql(CONST_X)\nCONST_X = "t.q = 1"\ndef list_y(p):\n    return sql("t.r = 2")\n', encoding="utf-8")
        # run18 差2: 定数 COND_LIVE を名前で使う list_a と、同じ式を直書きする list_b
        be5 = t / "be5.py"
        be5.write_text('COND_LIVE = "t.superseded_by IS NULL AND t.x = 1"\ndef list_a(p):\n    return sql(COND_LIVE)\n'
                       'def list_b(p):\n    return sql("SELECT 1 WHERE t.superseded_by IS NULL AND  t.x = 1")\n', encoding="utf-8")
        # 下書き: BE1・BE2 の行範囲と、共通ロジックの候補(名前あり・名前なし・出典の行が無いもの)
        draft = {
            "apis": {
                "list_a": {"src": [{"file": str(be5), "line_start": 2, "line_end": 3}]},
                "list_b": {"src": [{"file": str(be5), "line_start": 4, "line_end": 5}]},
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
                {"process_id": "FR4", "name": "一覧A", "steps": [{"kind": "backend_call", "backend_call": {"backend_id": "BE5"}}]},
                {"process_id": "FR5", "name": "一覧B", "steps": [{"kind": "backend_call", "backend_call": {"backend_id": "BE6"}}]},
            ],
            "backend_processes": [
                {"process_id": "BE1", "name": "店舗一覧", "endpoint": {"action": "list_shops"}},
                {"process_id": "BE2", "name": "集計処理", "endpoint": {"action": "list_report"}},
                {"process_id": "BE3", "name": "別画面A", "endpoint": {"action": "list_x"}},
                {"process_id": "BE4", "name": "別画面B", "endpoint": {"action": "list_y"}},
                {"process_id": "BE5", "name": "一覧処理A", "endpoint": {"action": "list_a"}},
                {"process_id": "BE6", "name": "一覧処理B", "endpoint": {"action": "list_b"}},
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
                # run18 差2: used_in は FR1 だけだが、条件(COND_B2)を使う BE2 と、BE2 を呼ぶ FR2 も呼出元に入る(BE1 は使わない)
                {"name": "CL_G", "used_in": ["初期表示"], "runs_on": "データベース",
                 "src": [{"file": str(be2), "line_start": 3, "line_end": 3}]},
                # run18 差2: 名前で使う BE5(list_a)と、同じ式を直書きする BE6(list_b)の両方と、それぞれを呼ぶ FR4・FR5 が入る
                {"name": "CL_J", "used_in": [], "runs_on": "データベース",
                 "src": [{"file": str(be5), "line_start": 1, "line_end": 1}]},
                # run18 差2: 候補も used_in も無くても、出典の行が BE4(list_y)のソース範囲の中なら、BE4 と BE4 を呼ぶ FR3 が入る
                {"name": "CL_I", "used_in": [], "runs_on": "データベース",
                 "src": [{"file": str(be3), "line_start": 5, "line_end": 5}]},
                # run19 差4: ids に FR2 があるのに used_in・used_by_processes に「集計」が無い。名前を ids から足す(FR1 の語は残す)
                {"name": "CL_K", "used_in": ["初期表示"], "used_by_processes": ["初期表示"], "used_by_processes_ids": ["FR2"]},
            ],
        }
        pn = {"EV00": "初期表示", "EV01": "集計"}
        log = []
        fill(data, pn, log, draft)
        cl = {c["name"]: c for c in data["common_logic"]}
        assert cl["A"]["used_by_processes_ids"] == ["FR1"], cl["A"]
        # round13 W3: src が無いサーバー・DB の CL には BE を足さない(E)。B は語 list_report が BE2 に直接結ぶので従来どおり
        assert cl["B"]["used_by_processes_ids"] == ["BE2", "FR2"], cl["B"]
        assert cl["C"]["used_by_processes_ids"] == [], cl["C"]
        assert cl["D"]["used_by_processes_ids"] == ["FR9", "FR2"], cl["D"]
        assert cl["E"]["used_by_processes_ids"] == ["FR1"], cl["E"]
        assert cl["F"]["used_by_processes_ids"] == ["BE1", "FR1"], cl["F"]
        assert cl["F"]["used_by_processes"] == ["店舗一覧", "初期表示"], "API 名が BE の name に直っていない"
        assert cl["CL_A"]["used_by_processes_ids"] == ["FR2", "FR1", "BE2"], cl["CL_A"]
        assert cl["CL_B"]["used_by_processes_ids"] == ["FR1", "FR2", "BE1", "BE2"], cl["CL_B"]
        assert cl["CL_C"]["used_by_processes_ids"] == ["FR1", "FR2", "BE1"], cl["CL_C"]
        assert cl["CL_D"]["used_by_processes_ids"] == ["FR2", "BE2"], cl["CL_D"]
        assert cl["CL_E"]["used_by_processes_ids"] == ["FR3", "BE3"], cl["CL_E"]
        assert not any("CL_E" in m or "別の画面" in m for m in log), log
        assert cl["CL_G"]["used_by_processes_ids"] == ["FR1", "BE2", "FR2"], cl["CL_G"]
        assert cl["CL_I"]["used_by_processes_ids"] == ["BE4", "FR3"], cl["CL_I"]
        assert cl["CL_J"]["used_by_processes_ids"] == ["BE5", "BE6", "FR4", "FR5"], cl["CL_J"]
        assert cl["CL_K"]["used_by_processes_ids"] == ["FR2", "FR1"], cl["CL_K"]
        assert cl["CL_K"]["used_in"] == ["初期表示", "集計"] and cl["CL_K"]["used_by_processes"] == ["初期表示", "集計"], cl["CL_K"]
        # FR の名前だけ足す。BE(BE5・BE6)の名前は used_in に混ぜない
        assert cl["CL_J"]["used_in"] == ["一覧A", "一覧B"], cl["CL_J"]
        # WARN: C は結べない語(1件)。B・E は src が無く BE を足さない(各1件)。CL_D は候補と行が重ならず絞れない(1件)。
        # round(clwarn): ブラウザの CL で src が無い A・F は画面の呼出元を引けない WARN(各1件)。計6件
        assert len(log) == 6, log
        assert sum("どこにも無い" in m for m in log) == 1, log
        assert sum("絞れない" in m for m in log) == 3, log
        assert sum("画面の呼出元を引けない" in m for m in log) == 2, log
        # WARN は画面の CL の A・F(src が無い方)にだけ出る
        assert all("「A」" in m or "「F」" in m for m in log if "画面の呼出元" in m), log
        # 冪等: 2回目で結果が変わらず、WARN も増えない
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        log2 = []
        fill(data, pn, log2, draft)
        assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before and len(log2) == 6, log2
    _selftest_front()
    print("[selftest OK] fill_common_logic_callers")


def _selftest_front():
    """round13 W3: ブラウザで動く CL の呼出元(TSX の useMemo の依存と state を変えるイベント)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tsx = Path(tmp) / "Page.tsx"
        tsx.write_text("\n".join([
            "function sumX(list) {",                                   # 1
            "  return list.length;",                                   # 2
            "}",                                                       # 3
            "",                                                        # 4
            "export default function Page() {",                        # 5
            "  const aggs = useMemo(() => rows.map(toAgg), [rows]);",  # 6
            "  const total = useMemo(() => sumX(aggs), [aggs]);",      # 7
            "  const exportCsv = () => {",                             # 8
            "    return total;",                                       # 9
            "  };",                                                    # 10
            "  const x = 1;",                                          # 11
            "}",                                                       # 12
        ]) + "\n", encoding="utf-8")
        f = str(tsx)
        rd = {
            "state_initials": [{"state": "rows", "setter": "setRows"}, {"state": "show", "setter": "setShow"}],
            "derived_arrays": [{"name": "aggs", "source": "rows"}],
            "events": [
                {"event_code": "EV00", "source": {"handler": "useEffect"}, "src": [{"file": f, "line_start": 20, "line_end": 20}]},
                {"event_code": "EV01", "source": {"handler": "(e) => setRows(x)"}, "src": [{"file": f, "line_start": 21, "line_end": 21}]},
                {"event_code": "EV05", "source": {"handler": "(e) => setRows(y)"}, "src": [{"file": f, "line_start": 22, "line_end": 22}]},
                {"event_code": "EV06", "source": {"handler": "(e) => setShow(!x)"}, "src": [{"file": f, "line_start": 23, "line_end": 23}]},
                {"event_code": "EV07", "source": {"handler": "exportCsv"}, "src": [{"file": f, "line_start": 24, "line_end": 24}]},
            ],
            "failure_effects": [{"event": "EV00", "call": "fetchX", "on_success": ["rows"]}],
            "csv_exports": [{"function": "exportCsv", "trigger_events": ["EV07"], "src": [{"file": f, "line_start": 8, "line_end": 10}]}],
        }
        names = ["初期", "送信", "切替", "表示切替", "CSV"]
        data = {
            "events": [{"no": c, "event_code": c} for c in ("EV00", "EV01", "EV05", "EV06", "EV07")],
            "front_processes": [{"process_id": f"FR{i + 1}", "name": n, "steps": []} for i, n in enumerate(names)],
            "backend_processes": [],
            "common_logic": [
                {"name": "T", "used_in": [], "runs_on": "ブラウザ", "src": [{"file": f, "line_start": 1, "line_end": 3}]},
            ],
        }
        pn = {"EV00": "初期", "EV01": "送信", "EV05": "切替", "EV06": "表示切替", "EV07": "CSV"}
        log = []
        fill(data, pn, log, {}, rd)
        got = data["common_logic"][0]["used_by_processes_ids"]
        # rows を変える EV00(on_success)・EV01・EV05(setRows)と CSV の EV07。無関係の setShow の EV06 は入らない
        assert sorted(got) == ["FR1", "FR2", "FR3", "FR5"], got
        assert "FR4" not in got, got
        # react 下書きが無いときは何も足さない
        data2 = json.loads(json.dumps(data))
        data2["common_logic"][0]["used_by_processes_ids"] = []
        # 1回目の fill が ids から足した FR の名前(used_in)も戻す(残すと、2回目は名前から ids を結び直してしまう)
        data2["common_logic"][0]["used_in"] = []
        fill(data2, pn, [], {}, None)
        assert data2["common_logic"][0]["used_by_processes_ids"] == [], data2["common_logic"][0]
    print("[selftest OK] fill_common_logic_callers front")
    _selftest_front_gate()


def _selftest_front_gate():
    """round14 R3: 型注釈つきの配列の中の項目(dead: true)の calc は、旗で項目を落とす表示の条件の state を変えるイベントも呼出元にする。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tsx = Path(tmp) / "Page.tsx"
        tsx.write_text("\n".join([
            "const SPECS: Spec[] = [",                                              # 1
            '  { label: "a", kin: "a" },',                                          # 2
            '  { label: "b", calc: (r) => r.x - r.y, dead: true,',                  # 3
            '    note: "n" },',                                                     # 4
            "];",                                                                   # 5
            "",                                                                     # 6
            "function Card({ showDead }: { showDead: boolean }) {",                 # 7
            "  const rows = SPECS.filter((s) => showDead || !s.dead).map((s) => s.label);",  # 8
            "  return rows;",                                                       # 9
            "}",                                                                    # 10
            "",                                                                     # 11
            "export default function Page() {",                                     # 12
            "  const [show, setShow] = useState(false);",                           # 13
            "  const [other, setOther] = useState(0);",                             # 14
            "  return <Card showDead={show} />;",                                   # 15
            "}",                                                                    # 16
        ]) + "\n", encoding="utf-8")
        f = str(tsx)
        rd = {
            "state_initials": [{"state": "show", "setter": "setShow"}, {"state": "other", "setter": "setOther"}],
            "events": [
                {"event_code": "EV10", "source": {"handler": "(e) => setShow(e.target.checked)"}, "src": [{"file": f, "line_start": 30, "line_end": 30}]},
                {"event_code": "EV11", "source": {"handler": "(e) => setOther(1)"}, "src": [{"file": f, "line_start": 31, "line_end": 31}]},
            ],
        }
        data = {
            "events": [{"no": c, "event_code": c} for c in ("EV10", "EV11")],
            "front_processes": [{"process_id": "FR1", "name": "表示切替", "steps": []},
                                {"process_id": "FR2", "name": "別の入力", "steps": []}],
            "backend_processes": [],
            "common_logic": [
                # 項目の1行目だけが出典(閉じる } は次の行): 旗 dead: true を読み取れる
                {"name": "DEAD", "used_in": [], "runs_on": "ブラウザ", "src": [{"file": f, "line_start": 3, "line_end": 3}]},
                # 旗の無い項目: 常に描かれるので、表示の条件のイベントは入らない
                {"name": "PLAIN", "used_in": [], "runs_on": "ブラウザ", "src": [{"file": f, "line_start": 2, "line_end": 2}]},
            ],
        }
        pn = {"EV10": "表示切替", "EV11": "別の入力"}
        fill(data, pn, [], {}, rd)
        cl = {c["name"]: c for c in data["common_logic"]}
        assert cl["DEAD"]["used_by_processes_ids"] == ["FR1"], cl["DEAD"]
        assert cl["PLAIN"]["used_by_processes_ids"] == [], cl["PLAIN"]
        # 旗で絞り込む行が、CL の項目を描く宣言の外(SPECS を使わない Card)にあるなら、条件の state は拾わない
        tsx.write_text(tsx.read_text(encoding="utf-8").replace("SPECS.filter(", "OTHERS.filter(").replace(
            "  return rows;\n}\n", "  return rows;\n}\n\nfunction Other() {\n  return SPECS.map((s) => s.label);\n}\n"), encoding="utf-8")
        data3 = json.loads(json.dumps(data))
        for c in data3["common_logic"]:
            c["used_by_processes_ids"] = []
            c["used_in"] = []  # 1回目の fill が ids から足した FR の名前も戻す(残すと名前から ids を結び直す)
        fill(data3, pn, [], {}, rd)
        assert {c["name"]: c["used_by_processes_ids"] for c in data3["common_logic"]}["DEAD"] == [], data3["common_logic"]
    print("[selftest OK] fill_common_logic_callers front gate")
    _selftest_front_render()


def _selftest_front_render():
    """run15 差3: 描画の中で走る関数(num)は、Card を描く rows(派生の aggs 経由)を取得・再取得する FR も呼出元にする。
    入力欄の state(show)だけを変える FR と、描かれない関数(他の宣言から呼ばれない)は足さない。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tsx = Path(tmp) / "Page.tsx"
        tsx.write_text("\n".join([
            "const num = (v: unknown) => Number(v);",                             # 1
            "const unused = (v: unknown) => v;",                                  # 2
            "",                                                                   # 3
            "function Card({ agg, showDead }: Props) {",                          # 4
            "  return <td>{num(agg.x)}</td>;",                                    # 5
            "}",                                                                  # 6
            "",                                                                   # 7
            "export default function Page() {",                                   # 8
            "  const [rows, setRows] = useState([]);",                            # 9
            "  const [show, setShow] = useState(false);",                         # 10
            "  const aggs = useMemo(() => rows.map(toAgg), [rows]);",             # 11
            "  return (",                                                         # 12
            "    <div>",                                                          # 13
            "      {rows.map((r, i) => (",                                        # 14
            "        <Card",                                                      # 15
            "          key={i}",                                                  # 16
            "          agg={aggs[i]}",                                            # 17
            "          showDead={show}",                                          # 18
            "        />",                                                         # 19
            "      ))}",                                                          # 20
            "    </div>",                                                         # 21
            "  );",                                                               # 22
            "}",                                                                  # 23
        ]) + "\n", encoding="utf-8")
        f = str(tsx)
        rd = {
            "state_initials": [{"state": "rows", "setter": "setRows"}, {"state": "show", "setter": "setShow"}],
            "derived_arrays": [{"name": "aggs", "source": "rows"}],
            "events": [
                {"event_code": "EV00", "source": {"handler": "useEffect"}, "src": [{"file": f, "line_start": 30, "line_end": 30}]},
                {"event_code": "EV02", "source": {"handler": "(e) => setGroup(e.target.value)"}, "src": [{"file": f, "line_start": 31, "line_end": 31}]},
                {"event_code": "EV06", "source": {"handler": "(e) => setShow(e.target.checked)"}, "src": [{"file": f, "line_start": 32, "line_end": 32}]},
            ],
            # 取得・再取得は rows を置き換える。EV02(集計単位の切替)は handler に setRows が無く、再取得(load)経由で rows を変える
            "failure_effects": [
                {"event": "EV00", "call": "fetchX", "on_success": ["rows"]},
                {"event": "EV02", "call": "fetchX", "on_success": ["rows"]}],
        }
        data = {
            "events": [{"no": c, "event_code": c} for c in ("EV00", "EV02", "EV06")],
            "front_processes": [{"process_id": "FR1", "name": "初期", "steps": []},
                                {"process_id": "FR2", "name": "切替", "steps": []},
                                {"process_id": "FR3", "name": "表示切替", "steps": []}],
            "backend_processes": [],
            "common_logic": [
                {"name": "NUM", "used_in": [], "runs_on": "ブラウザ", "src": [{"file": f, "line_start": 1, "line_end": 1}]},
                {"name": "UNUSED", "used_in": [], "runs_on": "ブラウザ", "src": [{"file": f, "line_start": 2, "line_end": 2}]},
            ],
        }
        pn = {"EV00": "初期", "EV02": "切替", "EV06": "表示切替"}
        fill(data, pn, [], {}, rd)
        cl = {c["name"]: c for c in data["common_logic"]}
        # rows を取得する EV00・EV02 は入り、入力欄だけの EV06 は入らない
        assert cl["NUM"]["used_by_processes_ids"] == ["FR1", "FR2"], cl["NUM"]
        assert cl["UNUSED"]["used_by_processes_ids"] == [], cl["UNUSED"]
        # 冪等
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        fill(data, pn, [], {}, rd)
        assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before
    print("[selftest OK] fill_common_logic_callers front render")
    _selftest_front_state()


def _selftest_front_state():
    """run16: 描画の中で走る関数(num)の呼出が、表示用の state(showDead)を参照する描画の中にあるなら、その state を変える FR も呼出元にする。
    props に渡すだけで本体で使わない state(hide)・クリックの handler の中だけで読む state(note)を変える FR は足さない。
    コンポーネントの外(フィルタの直後の呼出)でも、検査と同じく直後 8 行以内なら足す。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tsx = Path(tmp) / "Page.tsx"
        tsx.write_text("\n".join([
            "const num = (v: unknown) => Number(v);",                           # 1
            "",                                                                 # 2
            "function Card({",                                                  # 3
            "  agg,",                                                           # 4
            "  showDead,",                                                      # 5
            "  hide,",                                                          # 6
            "}: {",                                                             # 7
            "  agg: Agg;",                                                      # 8
            "  showDead: boolean;",                                             # 9
            "  hide: boolean;",                                                 # 10
            "}) {",                                                             # 11
            "  const render = (specs: Spec[]) =>",                              # 12
            "    specs",                                                        # 13
            "      .filter((s) => showDead || !s.dead)",                        # 14
            "      .map((s) => num(agg[s.kin!]));",                             # 15
            "  return <td>{render(SPECS)}</td>;",                               # 16
            "}",                                                                # 17
            "",                                                                 # 18
            "export default function Page() {",                                 # 19
            "  const [showDead, setShowDead] = useState(false);",               # 20
            "  const [hide, setHide] = useState(false);",                       # 21
            "  const [note, setNote] = useState(\"\");",                        # 22
            "  const exportCsv = () => {",                                      # 23
            "    const v = num(note);",                                         # 24
            "    return v;",                                                    # 25
            "  };",                                                             # 26
            "  return (",                                                       # 27
            "    <main>",                                                       # 28
            "      <Card agg={a} showDead={showDead} hide={hide} />",           # 29
            "      <button onClick={exportCsv} />",                             # 30
            "    </main>",                                                      # 31
            "  );",                                                             # 32
            "}",                                                                # 33
        ]) + "\n", encoding="utf-8")
        f = str(tsx)

        def ev(code, line, handler):
            return {"event_code": code, "source": {"handler": handler}, "src": [{"file": f, "line_start": line, "line_end": line}]}
        rd = {
            "state_initials": [
                {"state": "showDead", "setter": "setShowDead", "src": [{"file": f, "line_start": 20, "line_end": 20}]},
                {"state": "hide", "setter": "setHide", "src": [{"file": f, "line_start": 21, "line_end": 21}]},
                {"state": "note", "setter": "setNote", "src": [{"file": f, "line_start": 22, "line_end": 22}]}],
            "events": [ev("EV10", 40, "(e) => setShowDead(e.target.checked)"), ev("EV11", 41, "(e) => setHide(e.target.checked)"),
                       ev("EV12", 42, "(e) => setNote(e.target.value)")],
        }
        data = {
            "events": [{"no": c, "event_code": c} for c in ("EV10", "EV11", "EV12")],
            "front_processes": [{"process_id": "FR1", "name": "表示切替", "steps": []}, {"process_id": "FR2", "name": "隠す", "steps": []},
                                {"process_id": "FR3", "name": "メモ", "steps": []}],
            "backend_processes": [],
            "common_logic": [{"name": "NUM", "used_in": [], "runs_on": "ブラウザ", "src": [{"file": f, "line_start": 1, "line_end": 1}]}],
        }
        pn = {"EV10": "表示切替", "EV11": "隠す", "EV12": "メモ"}
        fill(data, pn, [], {}, rd)
        got = data["common_logic"][0]["used_by_processes_ids"]
        # showDead(filter の下で num を呼ぶ描画)を変える FR1 だけ。hide は Card の引数に渡すだけ・note は handler の中だけ
        assert got == ["FR1"], got
        # 冪等
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        fill(data, pn, [], {}, rd)
        assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before
        # コンポーネントの外: フィルタの直後 8 行以内の呼出(検査の selftest と同じ形)
        t2 = Path(tmp) / "Flat.tsx"
        t2.write_text("\n".join([
            "const conv = (v: unknown) => Number(v);",                          # 1
            "const [showDead, setShowDead] = useState(false);",                 # 2
            "const [from, setFrom] = useState(\"\");",                          # 3
            "const [applied, setApplied] = useState({ from: conv(0) });",       # 4
            "const rows = list.filter((s) => showDead || !s.dead)",             # 5
            "  .map((s) => conv(s.v));",                                        # 6
        ]) + "\n", encoding="utf-8")
        g = str(t2)
        # from は別の state(applied)の宣言の行にキー名として出る。その行の conv(0) を「from を参照する描画の中」と取り違えない
        rd2 = {
            "state_initials": [{"state": "showDead", "setter": "setShowDead", "src": [{"file": g, "line_start": 2, "line_end": 2}]},
                               {"state": "from", "setter": "setFrom", "src": [{"file": g, "line_start": 3, "line_end": 3}]},
                               {"state": "applied", "setter": "setApplied", "src": [{"file": g, "line_start": 4, "line_end": 4}]}],
            "events": [{"event_code": "EV10", "source": {"handler": "(e) => setShowDead(e.target.checked)"},
                        "src": [{"file": g, "line_start": 9, "line_end": 9}]},
                       {"event_code": "EV11", "source": {"handler": "(e) => setFrom(e.target.value)"},
                        "src": [{"file": g, "line_start": 10, "line_end": 10}]}],
        }
        data2 = {
            "events": [{"no": "EV10", "event_code": "EV10"}, {"no": "EV11", "event_code": "EV11"}],
            "front_processes": [{"process_id": "FR1", "name": "表示切替", "steps": []}, {"process_id": "FR2", "name": "開始日", "steps": []}],
            "backend_processes": [],
            "common_logic": [{"name": "CONV", "used_in": [], "runs_on": "ブラウザ", "src": [{"file": g, "line_start": 1, "line_end": 1}]}],
        }
        fill(data2, {"EV10": "表示切替", "EV11": "開始日"}, [], {}, rd2)
        assert data2["common_logic"][0]["used_by_processes_ids"] == ["FR1"], data2["common_logic"][0]
    print("[selftest OK] fill_common_logic_callers front state")
    _selftest_front_init()


def _selftest_front_init():
    """run17: state の宣言の初期値の中で呼ぶ関数(todayJst)は、初期表示(EV00)の FR を呼出元にする。
    初期値に呼出が無い関数(キー名・state 名が同じ名前でも)・宣言の外で呼ぶだけの関数には足さない。冪等。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tsx = Path(tmp) / "Page.tsx"
        tsx.write_text("\n".join([
            "function todayJst() {",                                              # 1
            "  return 'x';",                                                      # 2
            "}",                                                                  # 3
            "function from() {",                                                  # 4
            "  return 'y';",                                                      # 5
            "}",                                                                  # 6
            "",                                                                   # 7
            "export default function Page() {",                                   # 8
            "  const [from, setFrom] = useState(todayJst().slice(0, 8));",       # 9
            "  const [applied, setApplied] = useState({ from: todayJst() });",   # 10
            "  const [note, setNote] = useState(\"\");",                         # 11
            "  return <input onChange={(e) => setNote(e.target.value)} />;",     # 12
            "}",                                                                  # 13
        ]) + "\n", encoding="utf-8")
        f = str(tsx)

        def decl(state, setter, initial, line):
            return {"state": state, "setter": setter, "initial": initial, "src": [{"file": f, "line_start": line, "line_end": line}]}
        rd = {
            "state_initials": [decl("from", "setFrom", "todayJst().slice(0, 8)", 9),
                               decl("applied", "setApplied", "{ from: todayJst() }", 10),
                               decl("note", "setNote", "\"\"", 11)],
            "events": [
                {"event_code": "EV00", "source": {"handler": "useEffect(マウント時)"}, "src": [{"file": f, "line_start": 20, "line_end": 20}]},
                {"event_code": "EV01", "source": {"handler": "(e) => setFrom(e.target.value)"}, "src": [{"file": f, "line_start": 21, "line_end": 21}]},
            ],
        }
        data = {
            "events": [{"no": c, "event_code": c} for c in ("EV00", "EV01")],
            "front_processes": [{"process_id": "FR1", "name": "初期表示", "steps": []}, {"process_id": "FR2", "name": "開始日の入力", "steps": []}],
            "backend_processes": [],
            "common_logic": [
                # 初期値の中で呼ぶ(2つの state の宣言): 初期表示の FR1 だけ。from を変える FR2(入力欄)は入らない
                {"name": "TODAY", "used_in": [], "runs_on": "ブラウザ", "src": [{"file": f, "line_start": 1, "line_end": 3}]},
                # 同じ名前の state(from)が宣言されるが、初期値に呼出は無い: 何も足さない
                {"name": "FROM_FN", "used_in": [], "runs_on": "ブラウザ", "src": [{"file": f, "line_start": 4, "line_end": 6}]},
            ],
        }
        pn = {"EV00": "初期表示", "EV01": "開始日の入力"}
        fill(data, pn, [], {}, rd)
        cl = {c["name"]: c for c in data["common_logic"]}
        assert cl["TODAY"]["used_by_processes_ids"] == ["FR1"], cl["TODAY"]
        assert cl["FROM_FN"]["used_by_processes_ids"] == [], cl["FROM_FN"]
        # 冪等
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        fill(data, pn, [], {}, rd)
        assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before
    print("[selftest OK] fill_common_logic_callers front init")


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
    # WHY(round13 W3): ブラウザで動く CL の呼出元は react 下書き(events・derived_arrays 等)から引く。無ければ引かない。
    react_path = INTERMEDIATE_DIR / f"{feat}_react_draft.json"
    react_draft = json.loads(react_path.read_text(encoding="utf-8")) if react_path.exists() else {}
    data = json.loads(path.read_text(encoding="utf-8"))
    log = []
    fill(data, get_process_names_by_event_code(feat), log, draft, react_draft)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for line in log:
        print(line, file=sys.stderr)
    print(f"[done] common_logic {len(data.get('common_logic', []) or [])} 件の呼出元を補った(WARN {len(log)} 件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
