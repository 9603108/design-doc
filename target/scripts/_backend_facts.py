#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_backend_facts.py — backend の SQL から集計関数・ウィンドウ関数を機械で拾う(C4)

【目的】
extract_backend_draft.py の下書きに、SQL の集計(SUM/COUNT/MIN/MAX/AVG/array_agg/string_agg)と
ウィンドウ(OVER (PARTITION BY ... ORDER BY ...))を、関数名・引数・FILTER 条件・別名・範囲
(本体の "main" か、囲む CTE の "cte <名前>")つきで出す。
意味合い: 店舗別の日数が count(*) でレジ単位に数えている等の事実を、LLM に読ませず下書きから見えるようにする。

【入出力】
  入力: 元の SQL 文字列 sql と、そのマスク済み文字列 m(extract_backend_draft._mask(sql) の戻り。位置は sql と同じ)
  出力: features(sql, m) → {"aggregates": [...], "windows": [...]}
        aggregates の要素: {func, arg, filter, alias, scope, cast}
        windows の要素: 同じ形 + over: {partition_by: [...], order_by: [...]}
【接続情報】
  - 呼出元: extract_backend_draft.extract()(SQL 1 本ごとに db_operations[].features、API ごとに apis[].features)
  - 呼出元: extract_backend_draft.selftest()(形の検査)
  - B3: response_fields(ts_paths) は api.ts の型から apis[action].response_fields(path・ts_base・nullable・ts_src)を作る。
    呼出元は extract_backend_draft.extract()。照合は verify_vs_source.check_types
  - F2: boundaries(errors, clo, entry) は apis[action].boundaries(入力検査の境界値・暦に無い日付を通すか・大小比較と期間上限が無いこと)、
    common_errors(mods, tables) は下書き最上位の common_errors(入口の except 節)。enclosing_if は _errors が raise の拒否条件を取るのに使う
  - 呼出先: merge_features() は同じ extract() が API 単位の重ね合わせに使う。ほかに呼出先は無い(標準ライブラリの re・ast のみ)

ponytail: 括弧は位置で閉じを探すだけ。文字列の中の括弧は _mask で消してある前提。HAVING と ORDER BY の中の集計は見ない。
"""
import ast
import io
import re
import tokenize
from pathlib import Path

# 集計の関数名。count(*) のような引数も括弧の中をそのまま arg に入れる
_AGG = re.compile(r"\b(SUM|COUNT|MIN|MAX|AVG|array_agg|string_agg)\s*\(", re.IGNORECASE)
# CTE の開き("名前 AS (")。範囲(scope)を決めるのに使う
_CTE_OPEN = re.compile(r"\b([A-Za-z_]\w*)\s+AS\s*\(", re.IGNORECASE)
_FILTER = re.compile(r"\s*FILTER\s*\(", re.IGNORECASE)
_OVER = re.compile(r"\s*OVER\s*\(", re.IGNORECASE)
_CAST = re.compile(r"\s*::\s*(\w+)")
_ALIAS = re.compile(r"\s*AS\s+([^\W\d]\w*)", re.IGNORECASE)


def _close(t, i):
    """t[i] の開き括弧に対応する閉じ括弧の位置(無ければ末尾)。"""
    depth = 0
    for j in range(i, len(t)):
        if t[j] == "(":
            depth += 1
        elif t[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(t)


def _split_top_commas(s):
    """文字列を深さ 0 のカンマで割り、各項の空白を詰めて返す(空の項は除く)。呼出元: features()。"""
    out, depth, st = [], 0, 0
    for i, c in enumerate(s):
        depth += (c == "(") - (c == ")")
        if c == "," and depth == 0:
            out.append(s[st:i])
            st = i + 1
    out.append(s[st:])
    return [" ".join(x.split()) for x in out if x.strip()]


def _scope(m, pos):
    """pos を囲む CTE の名前("cte <名前>")。どの CTE の中でもなければ "main"。呼出元: features()。"""
    name = "main"
    for c in _CTE_OPEN.finditer(m):
        if c.end() - 1 < pos < _close(m, c.end() - 1):
            name = "cte " + c.group(1)
    return name


def features(sql, m):
    """SQL 1 本の集計とウィンドウを {aggregates, windows} で返す。
    OVER が付く関数はウィンドウ、付かないものは集計に入れる(FILTER は両方に付く)。呼出元: extract_backend_draft.extract()。"""
    agg, win = [], []
    for mt in _AGG.finditer(m):
        name = mt.group(1)
        open_ = mt.end() - 1
        close = _close(m, open_)
        arg = " ".join(sql[open_ + 1:close].split())
        at = close + 1
        flt = None
        fm = _FILTER.match(m, at)
        if fm:
            fclose = _close(m, fm.end() - 1)
            flt = re.sub(r"^WHERE\s+", "", " ".join(sql[fm.end():fclose].split()), flags=re.IGNORECASE)
            at = fclose + 1
        over = None
        om = _OVER.match(m, at)
        if om:
            oclose = _close(m, om.end() - 1)
            body = sql[om.end():oclose]
            pm = re.search(r"PARTITION\s+BY\s+(.*?)(?=\bORDER\s+BY\b|$)", body, re.IGNORECASE | re.DOTALL)
            om2 = re.search(r"ORDER\s+BY\s+(.*)$", body, re.IGNORECASE | re.DOTALL)
            over = {"partition_by": _split_top_commas(pm.group(1)) if pm else [],
                    "order_by": _split_top_commas(om2.group(1)) if om2 else []}
            at = oclose + 1
        cm = _CAST.match(m, at)
        cast = cm.group(1) if cm else None
        if cm:
            at = cm.end()
        am = _ALIAS.match(m, at)
        item = {"func": name, "arg": arg, "filter": flt, "alias": am.group(1) if am else None,
                "scope": _scope(m, mt.start()), "cast": cast}
        if over is None:
            agg.append(item)
        else:
            item["over"] = over
            win.append(item)
    return {"aggregates": agg, "windows": win}


# ---- B3: 応答項目の型(api.ts の interface と call<{…}>("action") の型引数) ----
_TS_IFACE = re.compile(r"\binterface\s+(\w+)[^{\n]*\{")
_TS_CALL = re.compile(r"\bcall\s*<\s*\{")
_TS_MEMBER = re.compile(r"^(?:\[\s*\w+\s*:\s*\w+\s*\]|(\w+)(\?)?)\s*:\s*(.+)$", re.S)


def _end(t, i):
    """t[i] の開き括弧({ [ ( のどれか)に対応する閉じの位置(無ければ末尾)。"""
    depth = 0
    for j in range(i, len(t)):
        depth += (t[j] in "({[") - (t[j] in ")}]")
        if depth == 0:
            return j
    return len(t)


def _members(t, b, e):
    """t[b:e](interface や型引数の本体)の欄を [(欄の文字列, 先頭の位置)] で返す。深さ 0 の ; と改行で割り、
    欄の形(名前: 型)でない行は直前の欄の続き(複数行の型)として足す。コメントは呼出元が空白に置換済み。"""
    segs, depth, st = [], 0, b
    for i in range(b, e):
        depth += (t[i] in "({[") - (t[i] in ")}]")
        if t[i] in ";\n" and depth == 0:
            segs.append((st, i))
            st = i + 1
    segs.append((st, e))
    out = []
    for s, f in segs:
        raw = t[s:f]
        txt = raw.strip()
        if not txt:
            continue
        if not _TS_MEMBER.match(txt) and out:
            out[-1][0] += " " + txt
        else:
            out.append([txt, s + len(raw) - len(raw.lstrip())])
    return out


def _union(typ):
    """型を深さ 0 の | で割り、(null を除いた型, null を含むか)を返す。undefined は欄の任意(?)と同じ扱いで除く。"""
    parts, depth, st = [], 0, 0
    for i, c in enumerate(typ + "|"):
        depth += (c in "({[") - (c in ")}]")
        if c == "|" and depth == 0:
            parts.append(typ[st:i].strip())
            st = i + 1
    rest = [p for p in parts if p not in ("null", "undefined")]
    return " | ".join(rest), "null" in parts


def _fields(t, file, members, prefix, path_of):
    """欄のリスト(本文 t・ファイル file の中)を response_fields の要素にする。path_of(名前)が欄の path。
    index signature([col: string]: …)は index_signature: true で path は prefix + "[*]"。"""
    out = []
    for txt, off in members:
        m = _TS_MEMBER.match(txt)
        typ = m.group(3).strip().rstrip(",")
        base, nullable = _union(typ)
        f = {"path": path_of(m.group(1)) if m.group(1) else prefix + "[*]", "ts_type": typ, "ts_base": base,
             "nullable": nullable, "optional": bool(m.group(2)),
             "ts_src": [{"file": file, "line_start": t.count("\n", 0, off) + 1, "line_end": t.count("\n", 0, off) + 1}]}
        if not m.group(1):
            f["index_signature"] = True
        out.append(f)
    return out


def response_fields(ts_paths):
    """TS ファイル群から {action: [response_fields]} を作る。fetch 関数の call<{ rows: Row[]; … }>("action") の
    トップのキーと、その型が interface なら中の欄(`rows[].reji_no` の形)を、null 可・任意・行番号つきで返す。
    ponytail: コメントは // 以降を行末まで空白にする(文字列内の // があると欄が欠ける)。型エイリアス・交差型 (A & {…})[] は展開しない。
    呼出元: extract_backend_draft.extract(hints["ts_files"])。"""
    texts, ifaces = {}, {}
    for p in ts_paths:
        try:
            raw = Path(p).read_text(encoding="utf-8")
        except OSError:
            continue
        t = re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: re.sub(r"[^\n]", " ", m.group()), raw, flags=re.S)
        texts[str(p)] = t
        for m in _TS_IFACE.finditer(t):
            ifaces[m.group(1)] = (str(p), _members(t, m.end(), _end(t, m.end() - 1)))
    out = {}
    for p, t in texts.items():
        for m in _TS_CALL.finditer(t):
            e = _end(t, m.end() - 1)
            am = re.match(r"\s*>\s*\(\s*[\"'](\w+)[\"']", t[e + 1:])
            if not am:
                continue
            fields = _fields(t, p, _members(t, m.end(), e), "", lambda n: n)
            for f in list(fields):
                im = re.fullmatch(r"(\w+)(\[\])?", f["ts_base"])
                if not f.get("index_signature") and im and im.group(1) in ifaces:
                    ip, mem = ifaces[im.group(1)]
                    pre = f["path"] + ("[]" if im.group(2) else "")
                    fields += _fields(texts[ip], ip, mem, pre, lambda n, k=pre + ".": k + n)
            out[am.group(1)] = fields
    return out


def enclosing_if(fn, node):
    """fn の中で node(raise)を body に含む最も内側の if。無ければ None。呼出元: extract_backend_draft._errors(F2)。"""
    best = None
    for n in ast.walk(fn):
        if isinstance(n, ast.If) and any(node is x for s in n.body for x in ast.walk(s)):
            if best is None or n.lineno >= best.lineno:
                best = n
    return best


def _regex_consts(mods):
    """モジュールの `NAME = re.compile("…")` を {NAME: パターン文字列} にする(literal_eval では取れないため別に拾う)。"""
    out = {}
    for m in mods:
        for n in m.tree.body:
            if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                    and isinstance(n.value, ast.Call) and ast.unparse(n.value.func).endswith("compile")
                    and n.value.args and isinstance(n.value.args[0], ast.Constant) and isinstance(n.value.args[0].value, str)):
                out[n.targets[0].id] = n.value.args[0].value
    return out


def boundaries(errors, clo, entry):
    """入力検査の境界値を、ソースで確かめた事実として返す(案 F2)。

    呼出元: extract_backend_draft.extract()(apis[action].boundaries)。errors は _errors → _expand の戻り(check・param を持つ行だけ使う)。
    - 検査1件: {param, check(拒否する条件)、compare(条件の比較)、message、src}。条件が `NAME.match(…)` で NAME が re.compile の定数なら
      pattern と、その形式の検査が暦に無い日付(2026-02-30)を通すか(impossible_dates_accepted。暦の関数 strptime/fromisoformat/date が closure に無いときだけ)。
    - 無いことの事実: 日付の検査が2つ以上あり、それらの値を使う比較が closure に無ければ order(開始と終了の大小)、
      引き算・timedelta が無ければ span_limit(期間の上限)。present:false で出す。
    ponytail: param は文面の穴の実引数(無ければ文面の先頭の語)で決める。暦の関数の有無は呼出名の字面で見る。"""
    regs = _regex_consts([m for m, _ in clo])
    calendar = any(isinstance(c, ast.Call) and re.search(r"strptime|fromisoformat|(^|\.)date(time)?$", ast.unparse(c.func))
                   for _, f in clo for c in ast.walk(f))
    out = []
    for e in errors:
        if not e.get("check"):
            continue
        b = {"param": e.get("param", ""), "check": e["check"], "message": e.get("text_expanded") or e["message"], "src": e["src"],
             "compare": [ast.unparse(c) for c in ast.walk(ast.parse(e["check"], mode="eval")) if isinstance(c, ast.Compare)]}
        rm = re.search(r"\b(\w+)\.(match|fullmatch|search)\(", e["check"])
        if rm and rm.group(1) in regs:
            pat, fn = regs[rm.group(1)], getattr(re, rm.group(2))
            b["pattern"], b["pattern_name"] = pat, rm.group(1)
            # 暦に無い日付(2月30日)が形式だけの検査を通るか。実際にパターンへ当てて確かめる(通常の日付が通るときだけ判定する)
            if fn(pat, "2026-01-30") and fn(pat, "2026-02-30") and not calendar:
                b["impossible_dates_accepted"] = ["2026-02-30"]
        if b not in out:
            out.append(b)
    names = {}
    for n in ast.walk(entry):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call):
            for a in n.value.args:
                if isinstance(a, ast.Constant) and any(b.get("pattern") and b["param"] == a.value for b in out):
                    names[n.targets[0].id] = a.value
    if len(names) >= 2:
        uses = lambda node: {x.id for x in ast.walk(node) if isinstance(x, ast.Name)} & set(names)
        params = list(dict.fromkeys(names.values()))
        nodes = [c for _, f in clo for c in ast.walk(f)]
        if not any(isinstance(c, ast.Compare) and len(uses(c)) >= 2 for c in nodes):
            out.append({"param": ",".join(params), "present": False, "check": "order",
                        "fact": f"{' と '.join(params)} の大小を比べる式がソースに無い"})
        if not any(uses(c) and ((isinstance(c, ast.BinOp) and isinstance(c.op, ast.Sub))
                                or (isinstance(c, ast.Call) and re.search(r"timedelta|relativedelta", ast.unparse(c.func)))) for c in nodes):
            out.append({"param": ",".join(params), "present": False, "check": "span_limit",
                        "fact": "期間の長さの上限を決める式(引き算・timedelta)がソースに無い"})
    return out


def _handler_fact(mod, h):
    """except 節1つの事実: 例外・呼ぶ補助関数(via)・_err の応答(code・status・message)・節内の代入(status などの式)。"""
    assigns = {n.targets[0].id: ast.unparse(n.value) for n in ast.walk(h)
               if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
    errs, via = [], []
    for c in ast.walk(h):
        if not isinstance(c, ast.Call):
            continue
        name = ast.unparse(c.func)
        if name == "_err" and len(c.args) >= 2:
            st = c.args[2] if len(c.args) > 2 else next((k.value for k in c.keywords if k.arg == "status"), None)
            errs.append({"code": c.args[0].value if isinstance(c.args[0], ast.Constant) else assigns.get(ast.unparse(c.args[0]), ast.unparse(c.args[0])),
                         "status_code": (st.value if isinstance(st, ast.Constant) else assigns.get(st.id, ast.unparse(st)) if isinstance(st, ast.Name)
                                         else ast.unparse(st) if st is not None else ""),
                         "message": ast.unparse(c.args[1])})
        elif name in mod.funcs and name not in via:
            via.append(name)
    return {"exception": ast.unparse(h.type) if h.type else "(すべて)", "errors": errs, "via": via, "assigns": assigns,
            "src": [{"file": str(mod.path), "line_start": h.lineno, "line_end": h.end_lineno}]}


def common_errors(mods, tables):
    """ディスパッチ表を引く処理を囲む try の except 節を、全 API 共通のエラーの事実として返す(案 F2)。

    呼出元: extract_backend_draft.extract()(下書きの最上位 common_errors)。意味合い: 形式検査を通った後の DB 例外(暦に無い日付など)が
    どの except 節でどの status になるかを、S2 の error_patterns・H4 の入力検査が足せるようにする。
    status が呼出先の関数(via。_busy など)の中で決まるものは空のまま(推測しない)。"""
    out = []
    for mod in mods:
        for f in mod.funcs.values():
            if not any(isinstance(n, ast.Name) and n.id in tables for n in ast.walk(f)):
                continue
            # 入口(表を引く関数)の最上位の try だけ。入れ子の try(個別の処理の中)は共通ではない
            out += [_handler_fact(mod, h) for t in f.body if isinstance(t, ast.Try) for h in t.handlers]
    return out


_DOC_TERM = re.compile(r"([々぀-ヿ㐀-鿿]{2,12})(?:[（(]([A-Za-z_]\w*)[）)]|\s*[:：]\s*([A-Za-z_]\w*))")


def docstring_terms(mods):
    """backend の docstring・モジュール定数の文字列・# コメントから「語(列名)」「語:列名」の対を {物理列名: [語,...]} で返す(P17)。
    WHY: 列名だけでは業務の語が取れない列(id・cnt など)に、コメントや docstring が付けた日本語の語を機械で渡すため。
    呼出元: extract_backend_draft.extract()(下書きの最上位 docstring_terms)。mods は Module の並び。対の形は _DOC_TERM が読む。
    ponytail: 同じ列に出る語は重複を除いて出現順に並べる。語は 2〜12 字の連続した日本語に限る。"""
    out = {}
    for mod in mods:
        texts = [ast.get_docstring(mod.tree) or ""]
        texts += [ast.get_docstring(f) or "" for f in mod.funcs.values()]
        texts += [v for v in mod.consts.values() if isinstance(v, str)]
        try:
            src = Path(mod.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            src = ""
        texts += [tok.string.lstrip("#") for tok in tokenize.generate_tokens(io.StringIO(src).readline) if tok.type == tokenize.COMMENT]
        for text in texts:
            for m in _DOC_TERM.finditer(text):
                terms = out.setdefault(m.group(2) or m.group(3), [])
                if m.group(1) not in terms:
                    terms.append(m.group(1))
    return out


def merge_features(feats):
    """SQL ごとの features を API 単位に重ねる。同じ項目は1つだけ残す(呼出元: extract_backend_draft.extract())。"""
    out = {"aggregates": [], "windows": []}
    for f in feats:
        for k in out:
            out[k] += [x for x in f.get(k, []) if x not in out[k]]
    return out
