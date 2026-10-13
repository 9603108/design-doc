#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_react_facts.py — React(TSX) の下書きに足す事実の抽出(案 C2: 取得失敗時に変える状態・残る状態)

【目的】
API を呼ぶ操作の .then / .catch / .finally、または try / catch / finally から、
取得に失敗したときにどの state を書き、どの state を書かないか(前の値が残るか)を機械で出す。
「失敗時は一覧を更新しない」のような文を、推測でなく TSX の書き込みから作れるようにする。

【接続】
呼出元: extract_react_draft.main() の draft.update(extract(links, sources)) の1行。
入力の links は analyze_sources() の戻り(file / attr / texts / actions を使う)。
出力は {"failure_effects": [...]} だけ。既存キーは変えない(足すだけ)。
selftest: python3 _react_facts.py --selftest

【限界】
- 1つの本体(texts の1要素)につき、最初の失敗経路(.catch 付きの chain か try/catch)だけを見る。
- 呼出は直前の識別子を API 関数名とする(call)。action 文字列は写さない(analyze_sources の calls を参照)。
- 拾えない形(別の関数に分かれた chain 等)は出さない。推測で埋めない。
"""
import re
import sys
import tempfile
from pathlib import Path

# useState の状態名と setter 名の対。setter → state の写像を作る(呼出元: _setter_map)
_STATE = re.compile(r"\bconst\s*\[\s*([A-Za-z_$][\w$]*)\s*,\s*([A-Za-z_$][\w$]*)\s*\]\s*=\s*useState\b")
# 直前が . でない識別子の呼出(.then( などのメソッド呼出は除く)
_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
_CATCH_BLOCK = re.compile(r"\s*catch\s*(?:\([^)]*\))?\s*\{")
_FINALLY_BLOCK = re.compile(r"\s*finally\s*\{")
_CHAIN = re.compile(r"\.(then|catch|finally)\s*\(")


def _end(text, i):
    """text[i] の開き括弧に対応する閉じ括弧の位置を返す。文字列リテラル内の括弧は数えない。見つからなければ末尾。"""
    depth, quote, esc = 0, None, False
    for j in range(i, len(text)):
        c = text[j]
        if quote is not None:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = None
            continue
        if c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return j
    return len(text) - 1


def _uniq(xs):
    return list(dict.fromkeys(xs))


def _setter_map(sources):
    """画面の TSX/JSX から setter 名 → state 名の写像を作る。呼出元: extract()。"""
    m = {}
    for p in sources:
        if Path(p).suffix in (".tsx", ".jsx"):
            for s in _STATE.finditer(Path(p).read_text(encoding="utf-8")):
                m[s.group(2)] = s.group(1)
    return m


def _set_calls(seg, setters):
    """seg の中の setX(式) を [(state, 式の文字列), ...] の出現順で返す。"""
    out = []
    for m in _CALL.finditer(seg):
        if m.group(1) in setters:
            e = _end(seg, m.end() - 1)
            out.append((setters[m.group(1)], seg[m.end():e].strip()))
    return out


def _promise_chain(b, setters):
    """本体 b の最初の .then/.catch/.finally の並びを読む。.catch が無ければ None。

    意味合い: 失敗時に書く state(on_failure)と、成功時に書く state(on_success)の差が「前の値が残る」事実になる。
    """
    m = _CHAIN.search(b)
    if not m:
        return None
    head = b[:m.start()]
    names = [n for n in _CALL.findall(head) if n not in setters]
    stop = b.find(";", m.start())
    stop = len(b) if stop < 0 else stop
    seg = b[m.start():stop]
    parts = {"then": [], "catch": [], "finally": []}
    for pm in _CHAIN.finditer(seg):
        e = _end(seg, pm.end() - 1)
        parts[pm.group(1)].append(seg[pm.end():e])
    if not parts["catch"]:
        return None
    succ = _uniq([st for a in parts["then"] for st, _ in _set_calls(a, setters)])
    fails = [sc for a in parts["catch"] for sc in _set_calls(a, setters)]
    fail = _uniq([st for st, _ in fails])
    return {
        "call": names[-1] if names else "",
        "before_call": [f"{st}={v}" for st, v in _set_calls(head, setters)],
        "on_success": succ,
        "on_failure": fail,
        "in_finally": [f"{st}={v}" for a in parts["finally"] for st, v in _set_calls(a, setters)],
        "untouched_on_failure": [s for s in succ if s not in fail],
        "failure_text": fails[0][1] if fails else "",
        "start": m.start(),
        "end": stop,
    }


def _try_chain(b, setters):
    """本体 b の try / catch / finally を読む。try が無い、または catch が無ければ None。"""
    tm = re.search(r"\btry\s*\{", b)
    if not tm:
        return None
    tb = tm.end() - 1
    te = _end(b, tb)
    cm = _CATCH_BLOCK.match(b, te + 1)
    if not cm:
        return None
    cb = cm.end() - 1
    ce = _end(b, cb)
    fm = _FINALLY_BLOCK.match(b, ce + 1)
    fin_body = b[fm.end():_end(b, fm.end() - 1)] if fm else ""
    end = _end(b, fm.end() - 1) + 1 if fm else ce + 1
    awaits = re.findall(r"await\s+([A-Za-z_$][\w$]*)\s*\(", b[tb:te])
    fails = _set_calls(b[cb:ce], setters)
    fail = _uniq([st for st, _ in fails])
    succ = _uniq([st for st, _ in _set_calls(b[tb:te], setters)])
    return {
        "call": awaits[-1] if awaits else "",
        "before_call": [f"{st}={v}" for st, v in _set_calls(b[:tm.start()], setters)],
        "on_success": succ,
        "on_failure": fail,
        "in_finally": [f"{st}={v}" for st, v in _set_calls(fin_body, setters)],
        "untouched_on_failure": [s for s in succ if s not in fail],
        "failure_text": fails[0][1] if fails else "",
        "start": tm.start(),
        "end": end,
    }


def _failure_of(b, setters):
    """本体 b から失敗経路を1つ読む。promise の chain を先に見て、無ければ try/catch を見る。"""
    return _promise_chain(b, setters) or _try_chain(b, setters)


def extract(links, sources):
    """links(analyze_sources の戻り)と画面の TSX から {"failure_effects": [...]} を返す。

    呼出元: extract_react_draft.main()。意味合い: API を呼ぶイベントごとに、失敗時の state の動きを出す。
    event_code は extract_react_draft.build_draft と同じ振り方(attr=mount が EV00、以降 EV01…)。
    src は本体の文字列の出現位置から行を出す(本体がファイルに無い・一意に決まらない場合は付けない)。
    """
    setters = _setter_map(sources)
    out = []
    n = 0
    for link in links:
        if link["attr"] == "mount":
            code = "EV00"
        else:
            n += 1
            code = f"EV{n:02d}"
        if not link.get("file") or not link.get("texts"):
            continue
        # 目的: API 呼出の有無で絞らない。失敗経路(.catch・try/catch)は本体に在る時点で取得の経路なので、そのまま読む
        ftext = Path(link["file"]).read_text(encoding="utf-8")
        seen = set()
        for b in link["texts"]:
            f = _failure_of(b, setters)
            if not f:
                continue
            pos = ftext.find(b)
            src = []
            if pos >= 0:
                s, e = pos + f["start"], pos + f["end"]
                src = [{"file": link["file"], "line_start": ftext.count("\n", 0, s) + 1,
                        "line_end": ftext.count("\n", 0, max(s, e - 1)) + 1}]
            key = (code, f["call"], tuple(f["on_failure"]), tuple(f["before_call"]), tuple(f["in_finally"]))
            if key in seen:
                continue
            seen.add(key)
            entry = {"event": code}
            for k in ("call", "before_call", "on_success", "on_failure", "in_finally",
                      "untouched_on_failure", "failure_text"):
                entry[k] = f[k]
            entry["src"] = src
            out.append(entry)
    return {"failure_effects": out}


_EFFECT = re.compile(r"\buseEffect\s*\(")
_EFFECT_ARGS = re.compile(r"([\s\S]*?)\s*,\s*\[[^\]]*\]\s*,?\s*$")
_IDENT_ONLY = re.compile(r"[A-Za-z_$][\w$]*")


def _line_span(p, text, body):
    """本体の文字列の出現位置を {file, line_start, line_end} にする。見つからなければ None。"""
    s = text.find(body)
    if s < 0 or not body:
        return None
    return {"file": str(p), "line_start": text.count("\n", 0, s) + 1,
            "line_end": text.count("\n", 0, s + len(body) - 1) + 1}


def _effect_groups(p, text, api_names):
    """TSX の useEffect ごとに、本体(引数が関数名ならその定義)が呼ぶ API 関数名を [{calls, body, src}] で返す。

    意味合い: 別々の useEffect は React が待たずに起動する(並行)。1つの本体の中の並びは mode の判定に使う。
    api_names に無い呼出は数えない(推測で足さない)。
    """
    out = []
    for m in _EFFECT.finditer(text):
        po = m.end() - 1
        am = _EFFECT_ARGS.match(text[po + 1:_end(text, po)])
        if not am:
            continue
        arg = am.group(1).strip()
        body = arg
        if _IDENT_ONLY.fullmatch(arg):
            dm = re.search(r"\b(?:const|function)\s+" + re.escape(arg) + r"\b[^{]*\{", text)
            if not dm:
                continue
            bo = dm.end() - 1
            body = text[bo:_end(text, bo) + 1]
        calls = _uniq([n for n in _CALL.findall(body) if n in api_names])
        if calls:
            out.append({"calls": calls, "body": body, "src": _line_span(p, text, body)})
    return out


def fetch_plan(sources, api_names):
    """初期表示(EV00)の取得が並行か順かを返す。取得が無ければ None。

    呼出元: extract_react_draft.main()(events[0].fetch_plan に入れる)。
    mode: parallel(別々の useEffect / Promise.all / 待たずに続けて呼ぶ)、serial(await や .then で待ってから次を呼ぶ)、single(1件)。
    groups: 呼出の並び(parallel は同じ群が同時、serial は1件ずつ)。
    """
    groups = []
    for p in sources:
        if Path(p).suffix in (".tsx", ".jsx"):
            groups += _effect_groups(p, Path(p).read_text(encoding="utf-8"), set(api_names))
    if not groups:
        return None
    srcs = [g["src"] for g in groups if g["src"]]
    if len(groups) >= 2:
        return {"mode": "parallel", "groups": [g["calls"] for g in groups],
                "basis": "別々の useEffect で起動し、先の完了を待たない", "src": srcs}
    g = groups[0]
    calls, body = g["calls"], g["body"]
    if len(calls) < 2:
        return {"mode": "single", "groups": [calls], "basis": "取得は1件", "src": srcs}
    if "Promise.all" in body:
        return {"mode": "parallel", "groups": [calls], "basis": "Promise.all で並行に待つ", "src": srcs}
    if re.search(r"\bawait\b|\.then\s*\(", body):
        return {"mode": "serial", "groups": [[c] for c in calls],
                "basis": "同じ本体で応答を待ってから次を呼ぶ", "src": srcs}
    return {"mode": "parallel", "groups": [calls], "basis": "同じ本体で待たずに続けて呼ぶ", "src": srcs}


def selftest_fetch_plan():
    """fetch_plan の3形(別々の useEffect / Promise.all / await の直列)を最小の TSX で assert する。終了コード 0=OK / 1=NG。"""
    tsx_a = (
        "export default function S() {\n"
        "  const load = useCallback(() => {\n"
        "    fetchY().then((d) => setRows(d.rows));\n"
        "  }, []);\n"
        "  useEffect(() => { fetchX().then((d) => setShops(d.rows)); }, []);\n"
        "  useEffect(load, [load]);\n"
        "}\n"
    )
    tsx_b = "export default function S() {\n  useEffect(() => { Promise.all([fetchX(), fetchY()]); }, []);\n}\n"
    tsx_c = "export default function S() {\n  useEffect(() => { fetchX().then(() => fetchY()); }, []);\n}\n"
    api = ["fetchX", "fetchY"]
    try:
        with tempfile.TemporaryDirectory() as d:
            res = []
            for name, src in (("A.tsx", tsx_a), ("B.tsx", tsx_b), ("C.tsx", tsx_c)):
                p = Path(d) / name
                p.write_text(src, encoding="utf-8")
                res.append(fetch_plan([p], api))
        a, b, c = res
        assert a["mode"] == "parallel" and a["groups"] == [["fetchX"], ["fetchY"]], f"A: {a}"
        assert a["src"][0]["line_start"] == 5, f"A の src: {a['src']}"
        assert b["mode"] == "parallel" and b["groups"] == [["fetchX", "fetchY"]], f"B: {b}"
        assert c["mode"] == "serial" and c["groups"] == [["fetchX"], ["fetchY"]], f"C: {c}"
    except AssertionError as e:
        print(f"[selftest NG] fetch_plan {e}")
        return 1
    print("[selftest OK] _react_facts.fetch_plan")
    return 0


def selftest():
    """promise chain と try/catch の2形を最小の TSX で試し、失敗時の state と行番号を assert する。終了コード 0=OK / 1=NG。"""
    tsx = (
        "export default function S() {\n"
        "  const [rows, setRows] = useState([]);\n"
        "  const [loading, setLoading] = useState(false);\n"
        "  const [error, setError] = useState(null);\n"
        "  const load = () => {\n"
        "    setLoading(true);\n"
        "    setError(null);\n"
        "    fetchX().then((d) => setRows(d.rows)).catch((e) => setError(String(e))).finally(() => setLoading(false));\n"
        "  };\n"
        "  const load2 = async () => {\n"
        "    try {\n"
        "      const d = await fetchY();\n"
        "      setRows(d.rows);\n"
        "    } catch (e) {\n"
        "      setError(String(e));\n"
        "    } finally {\n"
        "      setLoading(false);\n"
        "    }\n"
        "  };\n"
        "}\n"
    )
    b1 = ("setLoading(true);\n    setError(null);\n"
          "    fetchX().then((d) => setRows(d.rows)).catch((e) => setError(String(e))).finally(() => setLoading(false));")
    b2 = ("try {\n      const d = await fetchY();\n      setRows(d.rows);\n    } catch (e) {\n"
          "      setError(String(e));\n    } finally {\n      setLoading(false);\n    }")
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            links = [{"file": str(p), "attr": "onSubmit", "actions": ["x"], "fetch": True, "texts": [b1, b2]}]
            got = extract(links, [p])["failure_effects"]
        assert len(got) == 2, f"件数: {got}"
        a, b = got
        assert a["call"] == "fetchX" and a["before_call"] == ["loading=true", "error=null"], f"before/call: {a}"
        assert a["on_success"] == ["rows"] and a["on_failure"] == ["error"], f"on_success/on_failure: {a}"
        assert a["in_finally"] == ["loading=false"] and a["untouched_on_failure"] == ["rows"], f"finally/untouched: {a}"
        assert a["failure_text"] == "String(e)", f"failure_text: {a}"
        assert a["src"] == [{"file": str(p), "line_start": 8, "line_end": 8}], f"src(chain): {a['src']}"
        assert b["call"] == "fetchY" and b["on_failure"] == ["error"] and b["untouched_on_failure"] == ["rows"], f"try: {b}"
        assert b["in_finally"] == ["loading=false"], f"try finally: {b}"
        assert b["src"] == [{"file": str(p), "line_start": 11, "line_end": 18}], f"src(try): {b['src']}"
    except AssertionError as e:
        print(f"[selftest NG] {e}")
        return 1
    print("[selftest OK] _react_facts")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        rc = selftest()
        sys.exit(rc or selftest_fetch_plan())
    print("[入力不正] 単独では使わない。--selftest のみ", file=sys.stderr)
    sys.exit(2)
