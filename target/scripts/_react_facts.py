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
        "failure_state": fails[0][0] if fails else "",
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
        "failure_state": fails[0][0] if fails else "",
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
                      "untouched_on_failure", "failure_text", "failure_state"):
                entry[k] = f[k]
            entry["src"] = src
            out.append(entry)
    return {"failure_effects": out}


_EFFECT = re.compile(r"\buseEffect\s*\(")
_EFFECT_ARGS = re.compile(r"([\s\S]*?)\s*,\s*\[([^\]]*)\]\s*,?\s*$")
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
            out.append({"calls": calls, "body": body, "deps": am.group(2).strip(), "src": _line_span(p, text, body)})
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


def add_fetch_order_step(steps, plan):
    """初期処理の steps に、取得の順(fetch_plan.mode)の書き方が無ければ、スクリプトが1行足す。足したら True。

    WHY: 初期表示の取得が並行か順かは TSX から機械で決まる事実(fetch_plan)なので、モデルに書かせず
    スクリプトが入れる(verify_vs_source の [取得順] が見る語と同じ: parallel=「並行」、serial=「待って」)。
    呼出元: migrate_v17_to_v18.build_front_processes(initial_processes[].steps を再採番する前に呼ぶ)。
    足す位置は最初の backend_call の直前。backend_call が2件未満・mode が single/None・既に語がある steps は触らない。
    文は、各 backend_call の description の末尾「…を取得する」を落とした名前から作る(API 名・「リクエスト」は入れない)。
    """
    mode = (plan or {}).get("mode")
    if mode not in ("parallel", "serial"):
        return False
    idx = [i for i, s in enumerate(steps) if s.get("kind") == "backend_call" or s.get("backend_call")]
    if len(idx) < 2:
        return False
    text = " ".join(str(s.get("description")) for s in steps)
    if (mode == "parallel" and re.search(r"並行|同時", text)) or (mode == "serial" and "待って" in text):
        return False
    names = [re.sub(r"(を取得|を呼び出)(する|します)?$", "", str(steps[i].get("description") or "").strip()) for i in idx]
    if not all(names):
        return False
    if mode == "parallel":
        desc = "、".join(names[:-1]) + "と" + names[-1] + "の取得は並行して進める(先の応答を待たない)"
    else:
        desc = names[0] + "の応答を待って" + "、その応答を待って".join(n + "を取得する" for n in names[1:])
    steps.insert(idx[0], {"kind": "state_update", "description": desc})
    return True


def initial_fetches(sources, api_names):
    """初期表示で起動する useEffect ごとの API 呼出を {"initial_fetches": [...]} で返す。

    呼出元: extract_react_draft.main()。意味合い: 「初期表示で何の API を呼ぶか」を一覧にして、EV00 の発火(mount)と
    backend_call の漏れを verify 側が突き合わせる土台にする。effect は出現順の番号、deps は useEffect の依存配列の原文。
    api_names に無い呼出は出さない(fetch_plan と同じ。推測で足さない)。
    """
    out = []
    for p in sources:
        if Path(p).suffix in (".tsx", ".jsx"):
            for gi, g in enumerate(_effect_groups(p, Path(p).read_text(encoding="utf-8"), set(api_names)), 1):
                out += [{"call": c, "effect": gi, "deps": g["deps"], "src": g["src"]} for c in g["calls"]]
    return {"initial_fetches": out}


_USESTATE = re.compile(r"\bconst\s*\[\s*([A-Za-z_$][\w$]*)\s*,\s*([A-Za-z_$][\w$]*)\s*\]\s*=\s*useState\b")
_LITERAL_INIT = re.compile(r"""true|false|null|undefined|-?\d+(?:\.\d+)?|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\[\s*\]""")
_FN_DEF = re.compile(r"\b(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\()")


def _type_end(text, i):
    """text[i] の `<`(useState の型引数)に対応する `>` の位置を返す。`=>` の `>` は数えない。無ければ i。"""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "<":
            depth += 1
        elif text[j] == ">" and text[j - 1] != "=":
            depth -= 1
            if depth == 0:
                return j
    return i


def _today_fns(text):
    """本体が new Date( と toISOString を使う関数名 → 基準の時差の説明を返す(日付を作る関数の見分け)。"""
    out = {}
    for m in _FN_DEF.finditer(text):
        bo = text.find("{", m.end())
        if bo < 0:
            continue
        body = text[bo:_end(text, bo) + 1]
        if "new Date(" in body and "toISOString" in body:
            om = re.search(r"Date\.now\(\)\s*\+\s*(\d+)\s*\*\s*3600\s*\*\s*1000", body)
            out[m.group(1) or m.group(2)] = f"UTC+{om.group(1)}時間" if om else "UTC"
    return out


def _readable_init(expr, today):
    """初期値の式を、日付を作る関数の呼出だけ「当月1日」「本日」の言葉に置き換えた文にする。置換が無ければ None。"""
    r = expr
    for n, tz in today.items():
        e = re.escape(n)
        r = re.sub(e + r"\(\)\.slice\(\s*0\s*,\s*8\s*\)\s*\+\s*\"01\"", f"当月1日({tz}基準)", r)
        r = re.sub(e + r"\(\)\.slice\(\s*0\s*,\s*7\s*\)", f"当月(yyyy-mm、{tz}基準)", r)
        r = re.sub(e + r"\(\)", f"本日({tz}基準)", r)
    return r if r != expr else None


def state_initials(sources):
    """useState ごとの初期値を {"state_initials": [...]} で返す(状態名・setter・原文・リテラルか式か・読める説明・結ぶ画面項目)。

    呼出元: extract_react_draft.main()。意味合い: 「初期表示の項目が全部 Blank」と書く誤りを止めるため、
    画面項目(bind=state)の初期値をソースの useState から機械で出す。kind: literal(true/false/数値/文字列/null/[])/ expression(それ以外)/ none(引数なし)。
    readable は expression の中の日付関数の呼出を「当月1日」「本日」に言い換えた文(言い換えが無ければ None)。
    bound は value/checked でその state を束ねた入力要素(jsx_elements)の id・label・item_type_hint。
    """
    items = {}
    for g in jsx_elements(sources)["jsx_elements"]:
        if g.get("bind"):
            items.setdefault(g["bind"], []).append({k: g[k] for k in ("id", "label", "item_type_hint")})
    out = []
    for p in sources:
        if Path(p).suffix not in (".tsx", ".jsx"):
            continue
        text = Path(p).read_text(encoding="utf-8")
        today = _today_fns(text)
        for m in _USESTATE.finditer(text):
            i = m.end()
            if text[i:i + 1] == "<":
                i = _type_end(text, i) + 1
            if text[i:i + 1] != "(":
                continue
            e = _end(text, i)
            raw = text[i + 1:e].strip()
            kind = "none" if not raw else "literal" if _LITERAL_INIT.fullmatch(raw) else "expression"
            out.append({"state": m.group(1), "setter": m.group(2), "initial": raw, "kind": kind,
                        "readable": _readable_init(raw, today) if kind == "expression" else None,
                        "bound": items.get(m.group(1), []), "src": [_line_span(p, text, text[m.start():e + 1])]})
    return {"state_initials": out}


_RACE_GUARD = re.compile(r"\bAbortController\b|\w*(?:reqId|requestId|stale)\w*|\+\+\s*\w+\.current\b", re.IGNORECASE)


def outcomes(link, sources):
    """連続操作1件(link)について、後から届く応答・読込中表示の事実を返す(案 C6)。

    呼出元: extract_react_draft._continuous_ops()。意味合い:
    - stale_overwrite: 成功側が state を書き、要求の識別(連番・AbortController)が本体に無い → 古い応答が後から上書きしうる。
    - loading_cleared_early: 呼出の前に state=true、finally で state=false、件数を数えない → 先に返った応答で表示が消える。
    判定は文字の一致だけ。取れない形は何も出さない(推測で足さない)。接続: extract() の failure_effects を土台にする。
    """
    guarded = bool(_RACE_GUARD.search("\n".join(link.get("texts", []))))
    out = []
    for e in extract([link], sources)["failure_effects"]:
        if not guarded:
            for st in e["on_success"]:
                out.append({"kind": "stale_overwrite", "state": st,
                            "fact": f"先に出した要求の応答が後から届くと、{st} を古い応答の内容で上書きする",
                            "basis": "成功側が応答の順序と無関係に state を書き、要求の識別(連番・AbortController)が無い",
                            "src": e["src"]})
        for fin in e["in_finally"]:
            st, _, val = fin.partition("=")
            if val == "false" and any(x.endswith("=true") for x in e["before_call"]):
                out.append({"kind": "loading_cleared_early", "state": st,
                            "fact": f"最初に返った応答の finally で {st} が消え、後続の要求が処理中でも表示が戻る",
                            "basis": f"{st}=true は要求ごとに立てるが、{st}=false は finally で件数を数えずに実行する",
                            "src": e["src"]})
    return out


_CSV_FN = re.compile(r"const\s+(\w+)\s*=\s*\(\)\s*=>\s*\{")


def _split_top(s):
    """s を深さ0のカンマで割る(文字列の中のカンマは数えない)。空の部分は捨てる。"""
    parts, depth, quote, cur = [], 0, None, []
    for c in s:
        if quote:
            cur.append(c)
            if c == quote:
                quote = None
            continue
        if c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(c)
    last = "".join(cur).strip()
    if last:
        parts.append(last)
    return parts


_JOIN = re.compile(r'(\w+|\])\s*\.join\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
_LINE_FN = re.compile(r"const\s+(\w+)\s*=\s*\((\w+)\s*(?::[^,)]*)?,[^)]*\)\s*=>\s*\[")


def _top_colon(s):
    """s の深さ0・文字列の外にある最初の `:`(三項の else 側の手前)の位置。無ければ -1。"""
    depth, quote = 0, None
    for i, c in enumerate(s):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ":" and depth == 0:
            return i
    return -1


def _csv_col(el, rp, qn):
    """行の配列の要素 el を1列の事実にする。rp は行の仮引数名(null なら合計行)、qn は引用符で包む関数名。

    kind: ternary(`rp ? 通常行の値 : 合計行の値`)/ same(両行で同じ式)/ spread(`...` で複数列を展開。合計行は agg に合計を渡して同じ式)。
    """
    quoted = bool(qn) and el.startswith(qn + "(")
    if el.startswith("..."):
        return {"kind": "spread", "expr": el[:160], "quoted": False}
    # WHY: 合計行は同じ行の関数に null を渡して呼ぶため、列の式は「行の値」と「合計の値」の2枝に分かれる。両方を出さないと合計行の列が読めない
    m = re.search(rf"\b{re.escape(rp)}\s*\?\s*", el)
    if not m:
        return {"kind": "same", "expr": el[:160], "quoted": quoted}
    rest = el[m.end():]
    c = _top_colon(rest)
    if c < 0:
        return {"kind": "same", "expr": el[:160], "quoted": quoted}
    tail = rest[c + 1:]
    return {"kind": "ternary", "expr": el[:160], "quoted": quoted, "row_value": rest[:c].strip(),
            "total_value": tail[:_expr_end(tail, 0)].strip()}


def _csv_rows(text, body, qn):
    """行を作る関数(`const line = (r, agg) => [ … ].join(sep)`)と、それを呼ぶ lines の元の配列・合計行の呼出を読む。無ければ None。"""
    lm = _LINE_FN.search(body)
    if not lm:
        return None
    ln, rp = lm.group(1), lm.group(2)
    ao = lm.end() - 1
    ae = _end(body, ao)
    jm = re.match(r'\s*\.join\(\s*"((?:[^"\\]|\\.)*)"\s*\)', body[ae + 1:])
    # WHY: 行の元の配列が state そのものか絞り込み後の派生値かで、CSV の行数と中身が変わる。画面の表示切替(showDead 等)が CSV に効くかを読めるよう、派生の有無を出す
    src = re.search(rf"const\s+(\w+)\s*=\s*(\w+)\.map\(\s*\(\w+(?:\s*,\s*\w+)?\)\s*=>\s*{re.escape(ln)}\(", body)
    out = {"function": ln, "row_param": rp, "value_separator": jm.group(1) if jm else None,
           "columns": [_csv_col(el, rp, qn) for el in _split_top(body[ao + 1:ae])]}
    if src:
        name = src.group(2)
        # WHY: 派生は useMemo を介さない素の `const x = y.filter(…)` も見る(useMemo は任意)。派生が見つからず、元配列が state でも props でもなければ、何で作られたか不明なので filtered は False と断定せず None にする
        sm = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*(?:useMemo\(\s*\(\)\s*=>\s*)?(\w+)\.(filter|slice|sort)\b", text)
        is_state = any(m.group(1) == name for m in _USESTATE.finditer(text))
        is_prop = bool(re.search(rf"\bprops\.{re.escape(name)}\b|\(\s*\{{[^()]*\b{re.escape(name)}\b[^()]*\}}", text))
        filtered = bool(sm and sm.group(2) == "filter")
        if not sm and not (is_state or is_prop):
            filtered = None
        out["rows_array"] = {"name": name, "derived_from": sm.group(1) if sm else None,
                             "op": sm.group(2) if sm else None, "filtered": filtered}
    pm = re.search(rf"(\w+)\.push\(\s*{re.escape(ln)}\(\s*null\s*,\s*(\w+)", body)
    if pm:
        tm = re.search(rf"\bconst\s+{re.escape(pm.group(2))}\s*=\s*useMemo\(\s*\(\)\s*=>\s*(.+?),\s*\[", text)
        out["total_call"] = {"lines": pm.group(1), "agg_arg": pm.group(2), "agg_expr": tm.group(1) if tm else None}
    return out


def _csv_one(p, text, name_idx_body, codes):
    """exportCsv 形の関数1件(本体 body)から CSV の書式の事実を読む。拾えない欄は空・None のまま出す。"""
    name, body, src = name_idx_body
    fn = {"function": name, "trigger_events": codes}
    fm = re.search(r"\.download\s*=\s*`([^`]*)`", body)
    fn["file_name_template"] = fm.group(1) if fm else None
    mm = re.search(r'type:\s*"([^"]+)"', body)
    fn["mime"] = mm.group(1) if mm else None
    fn["bom"] = "﻿" in body
    seps = re.findall(r'header\.join\("([^"]*)"\)', body)
    fn["separator"] = seps[0] if seps else None
    fn["line_break"] = "CRLF" if "\\r\\n" in body else None
    qm = re.search(r"const\s+(\w+)\s*=\s*\(v", body)
    if qm:
        qn = qm.group(1)
        fn["quote"] = {"fn": qn, "escape": '"→""' if 'replaceAll(\'"\', \'""\')' in body else None,
                       "uses_count": len(re.findall(rf"\b{re.escape(qn)}\(", body)) - 1}
    else:
        fn["quote"] = None
    hs = body.find("const header = [")
    fixed, spread = [], []
    if hs >= 0:
        ho = hs + len("const header = ")
        for item in _split_top(body[ho + 1:_end(body, ho)]):
            if item.startswith("..."):
                spread.append({"raw": item[:160], "cnt_suffixes": _uniq(re.findall(r"\(点数\)|\(金額\)", item))})
            elif item[:1] in "\"`":
                fixed.append(item[1:-1])
    fn["header"] = {"fixed": fixed, "spread": spread}
    tm = re.search(r"if\s*\(([^\n]*?)\)\s*lines\.push\(", body)
    fn["total_row"] = {"when": tm.group(1).strip()} if tm else None
    # WHY: join の区切りは CSV の見た目(値の中の区切り・列・行)を決める。モデルに書かせると取り違えるので、join の対象と区切りを機械で出す
    # 追加の事実: 各 join の区切り・値の区切り・行の元の配列(表示の絞り込み前か後か)・合計行の各列の値
    fn["joins"] = [{"target": "[…]" if m.group(1) == "]" else m.group(1), "separator": m.group(2)}
                   for m in _JOIN.finditer(body)]
    fn["row_builder"] = _csv_rows(text, body, qm.group(1) if qm else None)
    fn["src"] = [src] if src else []
    return fn


def csv_exports(links, sources):
    """画面の TSX から CSV 出力の書式を {"csv_exports": [...]} で返す(案 C1)。

    呼出元: extract_react_draft.main()。new Blob( を含む関数を対象にする。
    trigger_events は、その関数の名前が本体に出るイベントの event_code(build_draft と同じ振り方)。
    取れない欄は None・空配列で出す(推測で埋めない)。接続: S3 の EV の details が BOM・CRLF・合計行を書く元。
    """
    out = []
    for p in sources:
        if Path(p).suffix not in (".tsx", ".jsx"):
            continue
        text = Path(p).read_text(encoding="utf-8")
        if "new Blob(" not in text:
            continue
        b = text.find("new Blob(")
        cands = [m for m in _CSV_FN.finditer(text) if m.start() < b]
        if not cands:
            continue
        m = cands[-1]
        bo = m.end() - 1
        body = text[bo:_end(text, bo) + 1]
        src = _line_span(p, text, body)
        codes = []
        n = 0
        for link in links:
            code = "EV00" if link["attr"] == "mount" else f"EV{n + 1:02d}"
            if link["attr"] != "mount":
                n += 1
            if any(re.search(rf"\b{re.escape(m.group(1))}\b", t) for t in link.get("texts", [])):
                codes.append(code)
        out.append(_csv_one(p, text, (m.group(1), body, src), codes))
    return {"csv_exports": out}


_CALC = re.compile(r"\bcalc\s*:\s*\((\w+)\)\s*=>\s*")
_SPEC_OBJ = re.compile(r"\{\s*label:\s*\"([^\"]*)\"([^{}]*)\}")


def _expr_end(text, i):
    """text[i] から始まる式の終わり(深さ0のカンマ、または対の無い閉じ括弧の位置)を返す。文字列の中は数えない。"""
    depth, quote = 0, None
    for j in range(i, len(text)):
        c = text[j]
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return j
            depth -= 1
        elif c == "," and depth == 0:
            return j
    return len(text)


def _col_names(text):
    """定数配列の {label, csv, kin, cnt} から 列名 → [画面での名前(label・csv)] を作る。呼出元: calc_candidates。"""
    names = {}
    for m in _SPEC_OBJ.finditer(text):
        csv = re.search(r"csv:\s*\"([^\"]*)\"", m.group(2))
        for col in re.findall(r"\b(?:kin|cnt):\s*\"(\w+)\"", m.group(2)):
            names.setdefault(col, _uniq([m.group(1)] + ([csv.group(1)] if csv else [])))
    return names


def calc_candidates(sources):
    """定数配列の `calc: (r) => 式` を {"calc_candidates": [...]} で返す(案 B1)。

    呼出元: extract_react_draft.main()。意味合い: 計算式の「式そのもの」をソースから取り、H4 の calculations と verify_vs_source.check_calcs が突き合わせる。
    operands は式の中の `<引数>.<列>`、operators は + - * /(深さは見ない)、funcs は Math.xxx、guards は `列 ? … : 値` の三項。
    operand_labels は列の画面での名前(label・csv)。取れない欄は空で出す。
    ponytail: 算術を持つ useMemo / 関数は拾わない(calc: の定数配列だけ)。要れば _CALC と同じ形で足す。
    """
    out = []
    for p in sources:
        if Path(p).suffix not in (".tsx", ".jsx"):
            continue
        text = Path(p).read_text(encoding="utf-8")
        names = _col_names(text)
        for m in _CALC.finditer(text):
            s, e = m.end(), _expr_end(text, m.end())
            expr = text[s:e].strip()
            if expr.startswith("(") and _end(expr, 0) == len(expr) - 1:
                expr = expr[1:-1].strip()
            head = text[text.rfind("{", 0, m.start()):m.start()]
            lm = re.search(r"label:\s*\"([^\"]*)\"", head)
            ops = _uniq(re.findall(rf"\b{re.escape(m.group(1))}\.(\w+)", expr))
            tm = re.match(rf"{re.escape(m.group(1))}\.(\w+)\s*\?[\s\S]*:\s*([^:]+)$", expr)
            out.append({
                "id": f"CC{len(out) + 1:02d}", "label": lm.group(1) if lm else "", "expr": expr,
                "operands": ops, "operators": _uniq(re.findall(r"[+\-*/]", expr)),
                "funcs": _uniq(re.findall(r"Math\.\w+", expr)),
                "literals": _uniq(re.findall(r"(?<![\w.])\d+(?:\.\d+)?", expr)),
                "guards": [f"{tm.group(1)} が 0 のとき {tm.group(2).strip()}"] if tm else [],
                "operand_labels": {o: names[o] for o in ops if o in names},
                "in": "フロント",
                "src": [{"file": str(p), "line_start": text.count("\n", 0, s) + 1, "line_end": text.count("\n", 0, e) + 1}]})
    return {"calc_candidates": out}


_JSX_OPEN = re.compile(r"<(input|select|textarea|button|span|strong|h1|h2|a|table)\b")
# 表示専用の要素 → item_type_hint(案 B2)。text を持ち、label は持たない。verify_vs_source._DISPLAY_TAGS と同じ組
_DISPLAY_HINT = {"span": "Label", "strong": "Label", "h1": "Label", "h2": "Label", "a": "Link", "table": "Grid"}
_VISIBLE_IF = re.compile(r"\{\s*([^{}?]+?)\s*(?:&&|\?(?!\.))\s*\(?\s*$")
# 入力・操作系の要素 → 画面項目の item_type(案 B2 の表)。input は type で決まる
_HINT = {"select": "ComboBox", "textarea": "TextArea", "button": "Button"}
_INPUT_HINT = {"date": "Calendar", "checkbox": "Checkbox", "submit": "Button", "button": "Button"}
_OPTION_OPEN = re.compile(r"<option\b")
_MAP_OPEN = re.compile(r"\{\s*([\w$.]+)\s*\.map\(")
_VALUE_ATTR = re.compile(r"\bvalue=(?:\"([^\"]*)\"|\{([^{}]*)\})")


def _select_options(p, text, e):
    """select の開きタグの `>` の位置 e から、選択肢の事実を {"options": [...], "map": {…} または None} で返す。

    呼出元: jsx_elements()。options は value が引用符の定数の option → {"value", "text"}。value 属性の無い option は HTML の規則どおり文言を値にする。
    map は `{配列.map(…)}` の中の option(value が式)の事実 → {"array", "array_origin", "value_expr", "label_expr", "src"}。
    意味合い: 初期値 "register" を業務の表示名へ写すには value と表示名の対が事実として要る。map の値は配列の中身を見ないと決まらないので、
    推測で埋めず、配列名と式の原文だけ出す(array_origin は配列が useState の state のときだけ "state")。
    ponytail: map は最初の1件だけ見る。map の外の式の value は拾わない。
    """
    close = text.find("</select>", e)
    body = text[e + 1:close if close >= 0 else len(text)]
    mm = _MAP_OPEN.search(body)
    mend = _end(body, mm.start()) if mm else -1
    options, dyn = [], None
    for om in _OPTION_OPEN.finditer(body):
        oe = _tag_end(body, om.start())
        oc = body.find("</option>", oe)
        inner = "" if body[oe - 1] == "/" or oc < 0 else body[oe + 1:oc]
        vm = _VALUE_ATTR.search(body[om.end():oe])
        in_map = mm is not None and mm.start() < om.start() < mend
        if in_map:
            if dyn is None:
                dyn = {"value_expr": vm.group(2).strip() if vm and vm.group(2) is not None else None,
                       "label_expr": re.sub(r"\s+", " ", inner).strip()}
            continue
        plain = re.sub(r"<[^>]*>|\{[^{}]*\}", "", inner).strip()
        if vm and vm.group(1) is not None:
            options.append({"value": vm.group(1), "text": plain})
        elif vm is None and "{" not in inner:
            # WHY: HTML では value 属性が無い option の値は文言そのもの。推測ではなく仕様どおりに写す
            options.append({"value": plain, "text": plain})
    mp = None
    if dyn:
        arr = mm.group(1)
        s, en = e + 1 + mm.start(), e + 1 + mend
        mp = {"array": arr,
              "array_origin": "state" if any(g.group(1) == arr for g in _USESTATE.finditer(text)) else None,
              **dyn,
              "src": {"file": str(p), "line_start": text.count("\n", 0, s) + 1, "line_end": text.count("\n", 0, en) + 1}}
    return {"options": options, "map": mp}


def _tag_end(text, i):
    """text[i] の `<` から始まる開きタグの終わりの `>` の位置。属性値の {…} と引用符の中の `>`(=> など)は数えない。"""
    j = i
    while j < len(text):
        c = text[j]
        if c == "{":
            j = _end(text, j)
        elif c in "\"'":
            j = text.find(c, j + 1)
            if j < 0:
                break
        elif c == ">":
            return j
        j += 1
    return len(text) - 1


def jsx_elements(sources):
    """TSX の入力・操作系の要素(input/select/textarea/button)を {"jsx_elements": [...]} で返す(案 B2)。

    呼出元: extract_react_draft.main()。意味合い: 画面項目(S1)がソースの画面部品と過不足なく対応するかを verify_vs_source.check_items が見る元。
    label: button は中の文言、checkbox は包む <label> の文言、それ以外は直前の <label>…</label>。item_type_hint は上の表。
    select は options(value→表示名の定数)と map(配列から作る選択肢の式。_select_options を参照)を持つ。
    表示専用の要素(span・strong・h1・h2・a・table。文言が空のものは table 以外出さない)は text・visible_if(直前の `{cond && (`・三項)を持つ。
    ponytail: visible_if は直前の1段だけ(入れ子の外側の cond は見ない)。div・ul は拾わない。要れば _DISPLAY_HINT に足す。
    """
    out = []
    for p in sources:
        if Path(p).suffix not in (".tsx", ".jsx"):
            continue
        text = Path(p).read_text(encoding="utf-8")
        for m in _JSX_OPEN.finditer(text):
            tag, e = m.group(1), _tag_end(text, m.start())
            attrs = text[m.end():e]
            if tag in _DISPLAY_HINT:
                # 表示専用: 中の文言(自分の閉じタグまで。{…}・タグは除く)と、直前の `{cond && (` の cond(visible_if)
                close = text.find(f"</{tag}>", e)
                inner = "" if text[e - 1] == "/" or close < 0 else re.sub(r"<[^>]*>|\{[^{}]*\}", "", text[e + 1:close]).strip()
                if inner or tag == "table":
                    vm = _VISIBLE_IF.search(text[max(0, m.start() - 200):m.start()])
                    out.append({
                        "id": f"JX{len(out) + 1:02d}", "tag": tag, "type": None, "label": "", "text": inner,
                        "visible_if": vm.group(1) if vm else None, "item_type_hint": _DISPLAY_HINT[tag],
                        "src": [{"file": str(p), "line_start": text.count("\n", 0, m.start()) + 1, "line_end": text.count("\n", 0, e) + 1}]})
                continue
            ty = re.search(r"\btype=\"(\w+)\"", attrs)
            ty = ty.group(1) if ty else None
            if tag == "input":
                hint = _INPUT_HINT.get(ty, "Input")
            else:
                hint = _HINT[tag]
            label = ""
            if tag == "button":
                close = text.find("</button>", e)
                label = re.sub(r"<[^>]*>|\{[^{}]*\}", "", text[e + 1:close]).strip() if close >= 0 else ""
            elif ty == "checkbox":
                close = text.find("</label>", e)
                label = re.sub(r"<[^>]*>|\{[^{}]*\}", "", text[e + 1:close]).strip() if close >= 0 else ""
            else:
                lm = re.search(r"<label[^>]*>([^<]*)</label>\s*$", text[:m.start()])
                label = lm.group(1).strip() if lm else ""
            bind = re.search(r"\b(?:value|checked)=\{(\w+)\}", attrs)
            dis = re.search(r"\bdisabled=\{([^{}]*)\}", attrs)
            # WHY: select だけ選択肢の事実(options・map)を持つ。value と表示名の対が無いと、初期値 "register" を業務の表示名へ写せないため
            opts = _select_options(p, text, e) if tag == "select" else {}
            out.append({
                "id": f"JX{len(out) + 1:02d}", "tag": tag, "type": ty, "label": label,
                "bind": bind.group(1) if bind else None, "on": _uniq(re.findall(r"\b(on[A-Z]\w*)=", attrs)),
                "disabled": dis.group(1) if dis else None, "item_type_hint": hint, **opts,
                "src": [{"file": str(p), "line_start": text.count("\n", 0, m.start()) + 1, "line_end": text.count("\n", 0, e) + 1}]})
    return {"jsx_elements": out}


_UI_JSX_TEXT = re.compile(r"(?<!=)>([^<>{}]*[^\s<>{}][^<>{}]*)<")
_UI_LITERAL = re.compile(r"\"((?:[^\"\\\n]|\\.)*[぀-ヿ一-鿿](?:[^\"\\\n]|\\.)*)\"|`([^`$\n]*[぀-ヿ一-鿿][^`$\n]*)`")


def ui_strings(sources):
    """TSX の画面の文言を {"ui_strings": [...]} で返す(案 B4)。

    呼出元: extract_react_draft.main()。意味合い: H4・S3 の「画面にこの文言を出す」が作り話でないかを verify_vs_source.main が見る元
    (照合の文字列の集合に足す)。kind=jsx_text は `>…<` の間の文言(visible_if は囲む要素の直前の `{cond && (`・三項)、
    kind=literal は日本語を含む引用符・テンプレートの文字列。空白は1つにまとめる。
    ponytail: `${…}` を含むテンプレートは拾わない・visible_if は直前の1段だけ。
    """
    out = []
    for p in sources:
        if Path(p).suffix not in (".tsx", ".jsx"):
            continue
        text = Path(p).read_text(encoding="utf-8")
        for m in _UI_JSX_TEXT.finditer(text):
            tg = text.rfind("<", 0, m.start())
            vm = _VISIBLE_IF.search(text[max(0, tg - 200):tg]) if tg >= 0 else None
            out.append({"text": re.sub(r"\s+", " ", m.group(1)).strip(), "kind": "jsx_text",
                        "visible_if": vm.group(1) if vm else None,
                        "src": [{"file": str(p), "line_start": text.count("\n", 0, m.start(1)) + 1,
                                 "line_end": text.count("\n", 0, m.end(1)) + 1}]})
        for m in _UI_LITERAL.finditer(text):
            out.append({"text": m.group(1) or m.group(2), "kind": "literal", "visible_if": None,
                        "src": [{"file": str(p), "line_start": text.count("\n", 0, m.start()) + 1,
                                 "line_end": text.count("\n", 0, m.end()) + 1}]})
    return {"ui_strings": out}


def selftest_ui_strings():
    """jsx_text(visible_if つき・複数行)と literal(日本語のみ)を最小の TSX で assert する。"""
    tsx = (
        "const a = \"abc\";\n"
        "const m = \"取込欠損\";\n"
        "const f = () => <div>\n"
        "  {!rows.length && (\n"
        "    <div className=\"n\">この期間の\n"
        "      データはありません。</div>\n"
        "  )}\n"
        "</div>;\n"
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            got = ui_strings([p])["ui_strings"]
        j = [g for g in got if g["kind"] == "jsx_text"]
        assert [g["text"] for g in j] == ["この期間の データはありません。"], got
        assert j[0]["visible_if"] == "!rows.length" and j[0]["src"][0]["line_start"] == 5, j
        assert [g["text"] for g in got if g["kind"] == "literal"] == ["取込欠損"], got
    except AssertionError as e:
        print(f"[selftest NG] ui_strings {e}")
        return 1
    print("[selftest OK] _react_facts.ui_strings")
    return 0


_TOGGLE = re.compile(r"\{\s*([^{}?]+?)\s*(?:&&|\?(?!\.))\s*\(?\s*<(\w+)")
_DISABLED = re.compile(r"\bdisabled=\{([^{}]*)\}")
_FILTER = re.compile(r"\.filter\(\s*\(?\w+\)?\s*=>\s*")
_IF_OPEN = re.compile(r"\bif\s*\(")
_ATTR_CHECK = re.compile(r"<(?:input|select|textarea)\b[^>]*?\b(?:required|min|max|pattern|maxLength)\b")


def behaviors(sources):
    """画面の処理の種別をソースの書き方から {"behaviors": [...]} で返す(案 B5)。

    呼出元: extract_react_draft.main()。意味合い: 「表示の出し分け」を「入力検査」と書く誤りを verify_vs_source.check_kinds が止める元。
    kind: display_toggle(`{cond && <…>}`・三項)/ enable_toggle(`disabled={…}`)/ data_filter(`.filter(`)/
    input_check(`if (…) { setError(…) | throw … }` と input の required|min|max|pattern|maxLength)。
    control_src は、cond の state を value/checked に束ねた入力要素の位置(その操作の処理 FR と src で対にする)。
    ponytail: format(toLocaleString 等)は拾わない。要れば _FILTER と同じ形で足す。
    """
    out = []
    for p in sources:
        if Path(p).suffix not in (".tsx", ".jsx"):
            continue
        text = Path(p).read_text(encoding="utf-8")

        def span(s, e):
            return {"file": str(p), "line_start": text.count("\n", 0, s) + 1, "line_end": text.count("\n", 0, e) + 1}

        def add(kind, cond, target, s, e):
            ctl = []
            for ident in _uniq(re.findall(r"[A-Za-z_]\w*", cond)):
                bm = re.search(rf"\b(?:value|checked)=\{{{re.escape(ident)}\}}", text)
                if bm:
                    ctl.append(span(bm.start(), bm.end()))
            out.append({"id": f"BH{len(out) + 1:02d}", "kind": kind, "cond": cond, "target": target,
                        "src": [span(s, e)], "control_src": ctl})

        for m in _TOGGLE.finditer(text):
            t = text.find("<", m.end() - len(m.group(2)) - 1)
            te = _tag_end(text, t)
            lbl = re.match(r"[^<>{}]+", text[te + 1:])
            add("display_toggle", m.group(1).strip(), (lbl.group(0).strip() if lbl else "") or m.group(2), m.start(), te)
        for m in _DISABLED.finditer(text):
            t = text.rfind("<", 0, m.start())
            te = _tag_end(text, t)
            lbl = re.match(r"[^<>{}]+", text[te + 1:])
            add("enable_toggle", m.group(1).strip(), lbl.group(0).strip() if lbl else "", t, te)
        for m in _FILTER.finditer(text):
            s = m.end()
            add("data_filter", text[s:_expr_end(text, s)].strip(), "", m.start(), s)
        for m in _IF_OPEN.finditer(text):
            c = _end(text, m.end() - 1)
            nxt = re.match(r"\s*", text[c + 1:]).end() + c + 1
            body = text[nxt:_end(text, nxt) + 1] if text[nxt:nxt + 1] == "{" else text[nxt:text.find(";", nxt) + 1]
            if re.search(r"\bsetError\(|\bthrow\b", body):
                add("input_check", text[m.end():c].strip(), "", m.start(), nxt + len(body))
        for m in _ATTR_CHECK.finditer(text):
            add("input_check", "", "", m.start(), _tag_end(text, m.start()))
    return {"behaviors": out}


_ROWS_STATE = re.compile(
    r"\bconst\s*\[\s*(\w+)\s*,\s*\w+\s*\]\s*=\s*useState<([^\n]*?)\[\]>\(\s*\[\s*\]\s*\)")
_DERIVED = re.compile(r"\bconst\s+(\w+)\s*=\s*useMemo\(\s*\(\)\s*=>\s*(\w+)\.(?:filter|slice|sort)\b")
_ITER = r"\.(?:map|filter|forEach|find|reduce|some|every)\("
_SPEC_COL = re.compile(r"\b(?:kin|cnt)\s*:\s*\"(\w+)\"")
_NEAR_TAG = re.compile(r"<([A-Za-z]\w*)")
_FUNC_NAME = re.compile(r"\b(?:const|function)\s+(\w+)\s*(?:=|\()")


def _scope_after_params(text, po):
    """text[po] の `(` で始まる仮引数の直後の本体(`=> {…}`・`=> 式`・`{…}`)の範囲 (s, e) を返す。無ければ None。"""
    pe = _end(text, po)
    sm = re.match(r"\s*(?::\s*[\w<>\[\]|, ]+?)?\s*(=>|\{)\s*", text[pe + 1:pe + 200])
    if not sm:
        return None
    if sm.group(1) == "{":
        s = pe + 1 + sm.end(1) - 1
        return s, _end(text, s)
    s = pe + 1 + sm.end()
    e = _expr_end(text, s)
    semi = text.find(";", s)
    return s, (min(e, semi) if semi >= 0 else e)


def _element_scopes(text, base, types, typed_root):
    """行の配列(base: state名・派生名 → 元の state 名)の要素変数が使える範囲を [(変数名, 開始, 終了, 元の state 名)] で返す。

    要素変数は、`配列.map((x…` などの引数、`for (const x of 配列)`、型 types の仮引数(`(r: PosReportRow …)`。元は typed_root)。
    ponytail: 変数の入れ子・同名の遮蔽は見ない。
    """
    out = []
    for v, owner in base.items():
        for m in re.finditer(rf"\b{re.escape(v)}{_ITER}", text):
            po = m.end() - 1
            pm = re.match(r"\s*\(?\s*(\w+)\s*(?:,\s*(\w+))?", text[po + 1:])
            if pm:
                name = pm.group(2) if m.group(0).endswith(".reduce(") and pm.group(2) else pm.group(1)
                out.append((name, po, _end(text, po), owner))
        for m in re.finditer(rf"\bfor\s*\(\s*const\s+(\w+)\s+of\s+{re.escape(v)}\s*\)", text):
            s = m.end()
            nxt = re.match(r"\s*", text[s:]).end() + s
            e = _end(text, nxt) if text[nxt:nxt + 1] == "{" else max(text.find(";", nxt), nxt)
            out.append((m.group(1), s, e, owner))
    for t in types:
        for m in re.finditer(rf"\(\s*(\w+)\s*:\s*{re.escape(t)}\b", text):
            sc = _scope_after_params(text, m.start())
            if sc:
                out.append((m.group(1), sc[0], sc[1], typed_root))
    return out


def _use_element(text, s, pos):
    """範囲の開始 s から pos までの最寄りの開きタグ(属性の中なら「タグ の 属性」)、無ければ囲む関数名を返す。"""
    tags = list(_NEAR_TAG.finditer(text, s, pos))
    if tags:
        t = tags[-1]
        if _tag_end(text, t.start()) >= pos:
            am = list(re.finditer(r"\b(\w+)=\{", text[t.start():pos]))
            if am:
                return f"{t.group(1)} の {am[-1].group(1)}"
        return t.group(1)
    fm = list(_FUNC_NAME.finditer(text, 0, pos))
    return f"関数 {fm[-1].group(1)}" if fm else ""


def field_usage(sources):
    """画面部品の中の応答項目の使われ方を {"field_usage": [...]} で返す(案 C5)。

    呼出元: extract_react_draft.main()。意味合い: S2 の response_to_screen_mapping が「どの項目が画面のどこに出るか」を、
    推測でなくソースの `x.項目` から書く土台。path は「state名[].項目」(例 rows[].shop_name)。
    how: member(`x.項目`)/ spec_array(定数配列の `kin: "項目"`・`cnt: "項目"`)/ dynamic(Object.entries(x) の全項目走査)。
    null_guard は `x.項目` の直後に `?`・`&&` が続くとき true(`??`・`?.` も含む)。
    ponytail: 応答の型は読まないので、どの画面部品でも参照されない項目(unused)は出さない。要れば api.ts の型と突き合わせる。
    """
    paths = {}

    def add(path, how, use, guard=False):
        e = paths.setdefault(path, {"path": path, "how": how, "null_guard": False, "uses": []})
        e["null_guard"] = e["null_guard"] or guard
        if use not in e["uses"]:
            e["uses"].append(use)

    for p in sources:
        if Path(p).suffix not in (".tsx", ".jsx"):
            continue
        text = Path(p).read_text(encoding="utf-8")
        states = _ROWS_STATE.findall(text)
        if not states:
            continue
        base = {n: n for n, _ in states}
        for m in _DERIVED.finditer(text):
            if m.group(2) in base:
                base[m.group(1)] = base[m.group(2)]
        types = [t for _, t in states if re.fullmatch(r"\w+", t)]
        # 型付き仮引数の要素は、型名で宣言した state(最初の1つ)に結ぶ
        typed_root = next((n for n, t in states if re.fullmatch(r"\w+", t)), states[0][0])

        def span(s, e):
            return [{"file": str(p), "line_start": text.count("\n", 0, s) + 1, "line_end": text.count("\n", 0, e) + 1}]

        for v, s, e, owner in _element_scopes(text, base, types, typed_root):
            for fm in re.finditer(rf"\b{re.escape(v)}\.(\w+)((?:\.\w+\([^()]*\))?)", text[s:e + 1]):
                pos = s + fm.start()
                guard = bool(re.match(r"\s*(?:\?|&&)", text[s + fm.end():s + fm.end() + 6]))
                use = {"expr": fm.group(0), "element": _use_element(text, s, pos), "null_guard": guard,
                       "src": span(pos, s + fm.end())}
                add(f"{owner}[].{fm.group(1)}", "member", use, guard)
            for dm in re.finditer(rf"Object\.entries\(\s*{re.escape(v)}\s*\)", text[s:e + 1]):
                pos = s + dm.start()
                add(f"{owner}[].*", "dynamic", {"expr": dm.group(0), "element": _use_element(text, s, pos),
                                                "note": "全項目を走査する", "src": span(pos, s + dm.end())})
        for m in _SPEC_COL.finditer(text):
            head = text[text.rfind("{", 0, m.start()):m.start()]
            lm = re.search(r"label:\s*\"([^\"]*)\"", head)
            add(f"{typed_root}[].{m.group(1)}", "spec_array",
                {"expr": m.group(0), "element": f"行「{lm.group(1)}」" if lm else "", "src": span(m.start(), m.end())})
    return {"field_usage": list(paths.values())}


def selftest_field_usage():
    """member(null_guard・要素)・spec_array・dynamic を最小の TSX で assert する。"""
    tsx = (
        "const S = [{ label: \"総\", kin: \"sou\" }];\n"
        "function toAgg(r: Row) {\n"
        "  for (const [k, v] of Object.entries(r)) a[k] = v;\n"
        "}\n"
        "export default function P() {\n"
        "  const [rows, setRows] = useState<Row[]>([]);\n"
        "  const w = useMemo(() => rows.filter((r) => r.days > 0), [rows]);\n"
        "  const csv = () => rows.map((r) => q(r.name));\n"
        "  return (\n"
        "    <ul>\n"
        "      {w.map((r) => (\n"
        "        <li>\n"
        "          {r.name}{r.no ? r.no : \"\"}\n"
        "        </li>\n"
        "      ))}\n"
        "    </ul>\n"
        "  );\n"
        "}\n"
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            got = {g["path"]: g for g in field_usage([p])["field_usage"]}
        assert got["rows[].name"]["how"] == "member" and len(got["rows[].name"]["uses"]) == 2, got["rows[].name"]
        assert {u["element"] for u in got["rows[].name"]["uses"]} == {"li", "関数 csv"}, got["rows[].name"]
        assert got["rows[].no"]["null_guard"] is True and got["rows[].days"]["null_guard"] is False, got
        assert got["rows[].sou"]["how"] == "spec_array" and got["rows[].*"]["how"] == "dynamic", got
    except AssertionError as e:
        print(f"[selftest NG] field_usage {e}")
        return 1
    print("[selftest OK] _react_facts.field_usage")
    return 0


def selftest_behaviors():
    """display_toggle・enable_toggle・data_filter・input_check と control_src を最小の TSX で assert する。"""
    tsx = (
        "const f = (rows) => rows.filter((s) => showDead || !s.dead);\n"
        "function S() {\n"
        "  const go = () => {\n"
        "    if (!from) { setError(\"x\"); return; }\n"
        "  };\n"
        "  return (\n"
        "    <div>\n"
        "      <input type=\"checkbox\" checked={showDead} onChange={a} />\n"
        "      <button type=\"button\" disabled={!rows.length}>CSV</button>\n"
        "      {rows.length > 1 && (\n"
        "        <Card>合計</Card>\n"
        "      )}\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            got = behaviors([p])["behaviors"]
        k = {g["kind"]: g for g in got}
        assert sorted(k) == ["data_filter", "display_toggle", "enable_toggle", "input_check"], got
        assert k["display_toggle"]["cond"] == "rows.length > 1" and k["display_toggle"]["target"] == "合計", k["display_toggle"]
        assert k["enable_toggle"]["cond"] == "!rows.length" and k["enable_toggle"]["target"] == "CSV", k["enable_toggle"]
        assert k["data_filter"]["control_src"][0]["line_start"] == 8, k["data_filter"]
        assert k["input_check"]["cond"] == "!from", k["input_check"]
    except AssertionError as e:
        print(f"[selftest NG] behaviors {e}")
        return 1
    print("[selftest OK] _react_facts.behaviors")
    return 0


def selftest_jsx():
    """select・date・checkbox・button の label・hint・bind・disabled を最小の TSX で assert する。"""
    tsx = (
        "<div>\n"
        "  <label>店舗</label>\n"
        "  <select value={s} onChange={(e) => f(e.target.value === \"\" ? 1 : 2)}>\n"
        "    <option>x</option>\n"
        "  </select>\n"
        "  <label>開始</label>\n"
        "  <input type=\"date\" value={from} onChange={(e) => g(e)} />\n"
        "  <label className=\"c\">\n"
        "    <input type=\"checkbox\" checked={dead} />\n"
        "    空欄も表示\n"
        "  </label>\n"
        "  <button type=\"button\" onClick={x} disabled={!rows.length}>\n"
        "    CSV出力\n"
        "  </button>\n"
        "  {loading && <span className=\"l\">読込中…</span>}\n"
        "</div>\n"
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            got = jsx_elements([p])["jsx_elements"]
        assert [(g["label"], g["item_type_hint"]) for g in got] == [
            ("店舗", "ComboBox"), ("開始", "Calendar"), ("空欄も表示", "Checkbox"), ("CSV出力", "Button"), ("", "Label")], got
        assert got[4]["text"] == "読込中…" and got[4]["visible_if"] == "loading", got[4]
        assert got[0]["bind"] == "s" and got[0]["on"] == ["onChange"] and got[3]["disabled"] == "!rows.length", got
        assert got[1]["src"][0]["line_start"] == 7, got[1]
    except AssertionError as e:
        print(f"[selftest NG] jsx_elements {e}")
        return 1
    print("[selftest OK] _react_facts.jsx_elements")
    return 0


def selftest_select_options():
    """select の定数 option(value="" を含む)・map の option(配列名・value の式・表示の式)・value 属性の無い option を最小の TSX で assert する。"""
    tsx = (
        "export default function S() {\n"
        "  const [shops, setShops] = useState<Shop[]>([]);\n"
        "  const [shopId, setShopId] = useState<number | \"\">(\"\");\n"
        "  return (\n"
        "    <div>\n"
        "      <label>店舗</label>\n"
        "      <select value={shopId} onChange={(e) => setShopId(Number(e.target.value))}>\n"
        "        <option value=\"\">- 全店 -</option>\n"
        "        {shops.map((s) => (\n"
        "          <option key={s.id} value={s.id}>\n"
        "            {s.name}{s.registers > 1 ? `（レジ${s.registers}台）` : \"\"}\n"
        "          </option>\n"
        "        ))}\n"
        "      </select>\n"
        "      <label>集計単位</label>\n"
        "      <select value={g}>\n"
        "        <option value=\"register\">レジ別（現行と同じ）</option>\n"
        "        <option>店舗別</option>\n"
        "      </select>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            got = jsx_elements([p])["jsx_elements"]
        a, b = got[0], got[1]
        assert a["label"] == "店舗" and a["options"] == [{"value": "", "text": "- 全店 -"}], a
        assert a["map"]["array"] == "shops" and a["map"]["array_origin"] == "state", a["map"]
        assert a["map"]["value_expr"] == "s.id" and a["map"]["label_expr"].startswith("{s.name}"), a["map"]
        assert a["map"]["src"]["line_start"] == 9, a["map"]
        assert b["label"] == "集計単位" and b["bind"] == "g" and b["map"] is None, b
        assert b["options"] == [{"value": "register", "text": "レジ別（現行と同じ）"}, {"value": "店舗別", "text": "店舗別"}], b
    except AssertionError as e:
        print(f"[selftest NG] select_options {e}")
        return 1
    print("[selftest OK] _react_facts.select_options")
    return 0


def selftest_calc():
    """calc の式・operands・funcs・guards・label・operand_labels・行番号を最小の TSX で assert する。"""
    tsx = (
        "const L = [\n"
        "  { label: \"小計\", csv: \"小計3(税込)\", kin: \"s3\" },\n"
        "  { label: \"点数\", kin: \"n\" },\n"
        "  { label: \"差\", calc: (r) => r.s3 - r.n, added: true },\n"
        "  { label: \"平均\", calc: (r) => (r.n ? Math.floor(r.s3 / r.n) : 0) },\n"
        "];\n"
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            got = calc_candidates([p])["calc_candidates"]
        assert [c["label"] for c in got] == ["差", "平均"], got
        a, b = got
        assert a["expr"] == "r.s3 - r.n" and a["operands"] == ["s3", "n"] and a["operators"] == ["-"], a
        assert b["expr"] == "r.n ? Math.floor(r.s3 / r.n) : 0" and b["funcs"] == ["Math.floor"], b
        assert b["literals"] == ["0"] and b["guards"] == ["n が 0 のとき 0"], b
        assert a["operand_labels"]["s3"] == ["小計", "小計3(税込)"] and a["src"][0]["line_start"] == 4, a
    except AssertionError as e:
        print(f"[selftest NG] calc_candidates {e}")
        return 1
    print("[selftest OK] _react_facts.calc_candidates")
    return 0


def selftest_csv():
    """exportCsv 形の最小 TSX で BOM・区切り・改行・固定見出し・合計行・ファイル名・トリガーを assert する。"""
    tsx = (
        "export default function S() {\n"
        "  const exportCsv = () => {\n"
        "    const q = (v: unknown) => `\"${String(v ?? \"\").replaceAll('\"', '\"\"')}\"`;\n"
        "    const header = [\"開始日\", \"店舗\", ...CSV_SPECS.flatMap((s) => [s.label])];\n"
        "    const lines = rows.map((r) => q(r.shop_name));\n"
        "    if (rows.length > 1) lines.push(\"x\");\n"
        "    const blob = new Blob([\"﻿\" + [header.join(\",\"), ...lines].join(\"\\r\\n\")], {\n"
        "      type: \"text/csv;charset=utf-8\",\n"
        "    });\n"
        "    a.download = `pos_${applied.from}.csv`;\n"
        "  };\n"
        "  return <button onClick={exportCsv}>CSV出力</button>;\n"
        "}\n"
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            links = [{"file": str(p), "attr": "mount", "texts": []},
                     {"file": str(p), "attr": "onClick", "texts": ["exportCsv"]}]
            got = csv_exports(links, [p])["csv_exports"]
        assert len(got) == 1, f"件数: {got}"
        c = got[0]
        assert c["function"] == "exportCsv" and c["trigger_events"] == ["EV01"], f"fn/trigger: {c}"
        assert c["bom"] is True and c["separator"] == "," and c["line_break"] == "CRLF", f"bom/sep/crlf: {c}"
        assert c["mime"] == "text/csv;charset=utf-8", f"mime: {c['mime']}"
        assert c["header"]["fixed"] == ["開始日", "店舗"] and len(c["header"]["spread"]) == 1, f"header: {c['header']}"
        assert c["total_row"] == {"when": "rows.length > 1"}, f"total: {c['total_row']}"
        assert c["file_name_template"] == "pos_${applied.from}.csv", f"file: {c['file_name_template']}"
        assert c["quote"]["fn"] == "q" and c["quote"]["escape"] is not None, f"quote: {c['quote']}"
        assert c["src"][0]["line_start"] == 2, f"src: {c['src']}"
    except AssertionError as e:
        print(f"[selftest NG] csv_exports {e}")
        return 1
    print("[selftest OK] _react_facts.csv_exports")
    return 0


def selftest_outcomes():
    """stale_overwrite と loading_cleared_early の2件が出ること、要求の識別があれば stale が消えることを assert する。"""
    tsx = (
        "export default function S() {\n"
        "  const [rows, setRows] = useState([]);\n"
        "  const [loading, setLoading] = useState(false);\n"
        "  const load = () => {\n"
        "    setLoading(true);\n"
        "    fetchX().then((d) => setRows(d.rows)).catch((e) => setError(String(e))).finally(() => setLoading(false));\n"
        "  };\n"
        "}\n"
    )
    b1 = ("setLoading(true);\n"
          "    fetchX().then((d) => setRows(d.rows)).catch((e) => setError(String(e))).finally(() => setLoading(false));")
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            plain = outcomes({"file": str(p), "attr": "onChange", "actions": ["x"], "texts": [b1]}, [p])
            guarded = outcomes({"file": str(p), "attr": "onChange", "actions": ["x"],
                                "texts": [b1, "const ac = new AbortController();"]}, [p])
        assert [o["kind"] for o in plain] == ["stale_overwrite", "loading_cleared_early"], f"plain: {plain}"
        assert plain[0]["state"] == "rows" and plain[1]["state"] == "loading", f"state: {plain}"
        assert [o["kind"] for o in guarded] == ["loading_cleared_early"], f"guarded: {guarded}"
    except AssertionError as e:
        print(f"[selftest NG] outcomes {e}")
        return 1
    print("[selftest OK] _react_facts.outcomes")
    return 0


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
    # add_fetch_order_step: parallel/serial の語が無ければ最初の backend_call の前に1行足す。あれば・single・1件なら足さない
    def mk():
        return [{"kind": "backend_call", "description": "店舗の一覧を取得する", "backend_call": {"backend_id": "B001"}},
                {"kind": "screen_update", "description": "店舗の選択肢を用意する"},
                {"kind": "backend_call", "description": "会計レポートを取得する", "backend_call": {"backend_id": "B002"}}]
    try:
        st = mk()
        assert add_fetch_order_step(st, {"mode": "parallel"}) and len(st) == 4 and st[0]["description"] == "店舗の一覧と会計レポートの取得は並行して進める(先の応答を待たない)", st
        assert not add_fetch_order_step(st, {"mode": "parallel"}) and len(st) == 4, "足した後は足さない"
        st = mk()
        assert add_fetch_order_step(st, {"mode": "serial"}) and st[0]["description"] == "店舗の一覧の応答を待って会計レポートを取得する", st
        st = mk()
        assert not add_fetch_order_step(st, {"mode": "single"}) and not add_fetch_order_step(st, None) and len(st) == 3
        assert not add_fetch_order_step(mk()[:2], {"mode": "parallel"}), "backend_call が1件なら足さない"
        st = mk()
        st[1]["description"] = "取得は同時に進める"
        assert not add_fetch_order_step(st, {"mode": "parallel"}), "語が既にあれば足さない"
    except AssertionError as e:
        print(f"[selftest NG] add_fetch_order_step {e}")
        return 1
    print("[selftest OK] _react_facts.fetch_plan")
    return 0


def selftest_initials():
    """state_initials(リテラル・式・読める説明・結ぶ項目)、initial_fetches、csv の row_builder(区切り・元の配列・合計行)を最小の TSX で assert する。"""
    tsx = (
        "function todayJst() {\n"
        "  return new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);\n"
        "}\n"
        "export default function S() {\n"
        "  const [from, setFrom] = useState(todayJst().slice(0, 8) + \"01\");\n"
        "  const [to, setTo] = useState(todayJst());\n"
        "  const [sid, setSid] = useState<number | \"\">(\"\");\n"
        "  const [rows, setRows] = useState<Row[]>([]);\n"
        "  const w = useMemo(() => rows.filter((r) => r.days > 0), [rows]);\n"
        "  const total = useMemo(() => sumAggs(rows), [rows]);\n"
        "  useEffect(() => {\n"
        "    fetchX().then((d) => setRows(d.rows)).catch((e) => setError(String(e)));\n"
        "  }, []);\n"
        "  const exportCsv = () => {\n"
        "    const q = (v: unknown) => `\"${String(v ?? \"\")}\"`;\n"
        "    const line = (r: Row | null, agg: Agg) =>\n"
        "      [\n"
        "        applied.from,\n"
        "        q(r ? r.name : \"合計\"),\n"
        "        r ? r.days : \"\",\n"
        "        q(r ? r.dates.join(\" \") : \"\"),\n"
        "      ].join(\",\");\n"
        "    const lines = w.map((r, i) => line(r, aggs[i]));\n"
        "    if (w.length > 1) lines.push(line(null, total));\n"
        "    const blob = new Blob([[\"a\", ...lines].join(\"\\r\\n\")], { type: \"text/csv\" });\n"
        "  };\n"
        "  return <input type=\"date\" value={from} onChange={a} />;\n"
        "}\n"
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "S.tsx"
            p.write_text(tsx, encoding="utf-8")
            si = {s["state"]: s for s in state_initials([p])["state_initials"]}
            fe = initial_fetches([p], ["fetchX"])["initial_fetches"]
            cv = csv_exports([], [p])["csv_exports"][0]
        assert si["from"]["kind"] == "expression" and si["from"]["readable"] == "当月1日(UTC+9時間基準)", si["from"]
        assert si["to"]["readable"] == "本日(UTC+9時間基準)" and si["from"]["bound"][0]["item_type_hint"] == "Calendar", si
        assert si["sid"]["kind"] == "literal" and si["sid"]["initial"] == '""' and si["rows"]["initial"] == "[]", si
        assert [(f["call"], f["deps"]) for f in fe] == [("fetchX", "")], fe
        rb = cv["row_builder"]
        assert rb["value_separator"] == "," and rb["rows_array"] == {"name": "w", "derived_from": "rows", "op": "filter", "filtered": True}, rb
        assert rb["total_call"]["agg_expr"] == "sumAggs(rows)", rb
        c = rb["columns"]
        assert [x["kind"] for x in c] == ["same", "ternary", "ternary", "ternary"], c
        assert c[1]["total_value"] == '"合計"' and c[1]["quoted"] and c[2]["total_value"] == '""' and not c[2]["quoted"], c
        assert {"target": "dates", "separator": " "} in cv["joins"] and {"target": "[…]", "separator": ","} in cv["joins"], cv["joins"]
        # useMemo を介さない素の filter は派生として拾い、filtered は True
        vis = ("const [rows, setRows] = useState<Row[]>([]);\n"
               "const exportCsv = () => {\n"
               "  const visible = rows.filter((r) => r.days > 0);\n"
               "  const line = (r: Row | null, agg: Agg) => [r ? r.a : \"x\"].join(\",\");\n"
               "  const lines = visible.map((r, i) => line(r, aggs[i]));\n"
               "};\n")
        assert _csv_rows(vis, vis[vis.find("{", vis.find("exportCsv")):], None)["rows_array"]["filtered"] is True
        # 元配列が state でも props でもなく、派生も無い(makeRows() で作られた)なら filtered は None(不明)
        nd = vis.replace("const visible = rows.filter((r) => r.days > 0);", "const data = makeRows();").replace("visible.map", "data.map")
        assert _csv_rows(nd, nd[nd.find("{", nd.find("exportCsv")):], None)["rows_array"]["filtered"] is None
        # 元配列が state そのものなら派生なし・filtered は False
        sd = nd.replace("const data = makeRows();", "").replace("data.map", "rows.map")
        assert _csv_rows(sd, sd[sd.find("{", sd.find("exportCsv")):], None)["rows_array"] == \
            {"name": "rows", "derived_from": None, "op": None, "filtered": False}
    except AssertionError as e:
        print(f"[selftest NG] initials/csv_rows {e}")
        return 1
    print("[selftest OK] _react_facts.state_initials/initial_fetches/csv_rows")
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
        assert a["failure_text"] == "String(e)" and a["failure_state"] == "error", f"failure_text: {a}"
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
        sys.exit(rc or selftest_fetch_plan() or selftest_outcomes() or selftest_csv() or selftest_calc() or selftest_jsx() or selftest_behaviors() or selftest_field_usage() or selftest_ui_strings() or selftest_initials() or selftest_select_options())
    print("[入力不正] 単独では使わない。--selftest のみ", file=sys.stderr)
    sys.exit(2)
