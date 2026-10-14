#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_backend_draft.py — backend(Python) から API ごとの dataset / response_items / error_patterns を機械的に下書きする

【目的】
screen_config.backend_files に並ぶ Python を ast で静的解析し、ディスパッチ表(handler.py の
ACTIONS = {"action名": module.func, ...} のような「文字列 → 関数参照」の dict)から action ごとの処理関数を引き、
その関数(と、同じ backend_files 内で呼ばれる補助関数。from X import f の f は同じフォルダの X.py からも辿る)から次を JSON で出す。
  - dataset[]: {column, table}。SQL 文字列(SELECT で始まる f 文字列を含む)の最上位 SELECT 句の列(別名.列 AS x は物理列、式 AS x は別名)と、
    その列の出所テーブル(別名・WITH の名前は FROM / JOIN から解決)
  - response_items[]: {key}。action の関数が return する dict のキー(return が補助関数の呼出なら、その return を辿る)
  - error_patterns[]: {condition, status_code, message}。raise と _err(...) の返却。condition は業務語彙なので空文字(LLM が書く)
  - unresolved[]: {action, reason}。静的に取れなかったもの。推測で埋めずここにだけ出す
  - db_operations[]: {id, from, columns}。SQL 1 本ごと(id は H2 と同じ db_op_<action>、複数なら _<n>)。
    columns は最上位 SELECT 句の全列の骨組み {no, alias, expression, table, column, logical_name, logical_name_source}。
    SELECT * / t.* は DDL の列で展開する。from は FROM の表 {table, name, alias}
    logical_name は機械的に取れた論理名の候補。出所 logical_name_source は取れた順に
    comment(DDL のコメント)/ csv_header(backend の列名と日本語の見出しの対応)/ docstring(backend の docstring・# コメントの「日本語(識別子)」)/ ui_label(画面の定数配列の label。型のコメントの「側の列」から末尾の語を取り、取れなければ ui_label_guess)/ expr(式の別名。元列の論理名から SUM・COUNT・MIN・MAX の種類で組む)/ api_type(TS の型のコメント)/ guess(列名からの推定)。取れなければ空

【意味合い】
Phase 1 の backend 節は LLM の読解で列・キー・エラーを書いていたため、実行ごとに欠落とぶれが出た。
列名・テーブル名・キー・メッセージ(事実)を機械で出し、LLM は業務の文面だけを書く分担にする(extract_react_draft.py と同じ考え方)。

【入出力】
  入力: --screen-config <screen_config.json>(backend_files / source_files / project_root を読む。相対の backend_files は project_root 基準)
        論理名の候補には project_root 配下の *.sql(DDL)と source_files の .tsx/.jsx(定数配列)・.ts/.tsx(型)を読む
  出力: --out <JSON>。キーは { apis: {action: {dataset, response_items, error_patterns, joins, where, group_by, order_by, aliases, db_operations}}, unresolved, auth }
  auth は入口が表を引く前に呼ぶ認証(identify 等)と、その 401/403 の文面。error_patterns の id は MSE + 5 桁の機械採番
  終了コード: 0=未解決なし / 1=unresolved が1件以上(下書きは書く) / 2=入力不正
  backend_files に無いモジュールを指す action は対象外(出力に入れず、unresolved にも出さない)

【接続情報】
  - 出力は extract_react_draft.py の db_operations の action 名と突き合わせる前提(突合は後段)
  例: python3 target/scripts/extract_backend_draft.py --screen-config target/intermediate/<screen>_screen_config.json \
        --out target/intermediate/<screen>_backend_draft.json
"""
import argparse
import ast
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

# 集計・ウィンドウ関数の下書き(C4)。同じフォルダの _backend_facts.py を読む(実行時の sys.path は scripts/)
from _backend_facts import features as _features, merge_features as _merge_features, response_fields as _response_fields
# 境界値・共通エラーの事実(F2)
from _backend_facts import boundaries as _boundaries, common_errors as _common_errors, enclosing_if as _enclosing_if

# FROM / JOIN の直後の別名として読んではいけない語
_KW = {"WHERE", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "JOIN", "ON", "GROUP", "ORDER", "LIMIT",
       "UNION", "USING", "SET", "HAVING", "WINDOW", "OFFSET", "FOR", "RETURNING", "AS", "WITH"}
_SQL_HEAD = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(WITH|SELECT)\b", re.IGNORECASE)
_FROM_TBL = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)(?:\s+(?:AS\s+)?(?!(?:" + "|".join(_KW) + r")\b)([A-Za-z_]\w*))?", re.IGNORECASE)  # キーワードは別名に食わせない(FROM t JOIN u の JOIN を落とさない)
_CTE = re.compile(r"\b([A-Za-z_]\w*)\s+AS\s*\(", re.IGNORECASE)
# 深さ 0 の句(JOIN・WHERE・GROUP BY・ORDER BY・HAVING・LIMIT・UNION)。_clauses が使う
_CLAUSE_KW = re.compile(r"\b(?:(?:LEFT|RIGHT|FULL)\s+(?:OUTER\s+)?JOIN|(?:INNER|CROSS)\s+JOIN|JOIN|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION)\b", re.IGNORECASE)
_CLAUSE_KEYS = ("joins", "where", "group_by", "order_by", "aliases")
_AND_OR = re.compile(r"\b(?:AND|OR)\b", re.IGNORECASE)
_COMMA = re.compile(",")
_COND = re.compile(r"^([A-Za-z_][\w.]*)\s*(NOT\s+LIKE|NOT\s+IN|IS\s+NOT|LIKE|IN|IS|BETWEEN|<>|!=|<=|>=|=|<|>)\s*(.*)$", re.IGNORECASE)
_PARAM = re.compile(r"^(?:\?|%s|:(\w+)|%\((\w+)\)s)$")
# 認証の呼出の名前(identify 等)。入口がディスパッチ表を引く前に呼ぶものだけを auth にする
_AUTH_CALL = re.compile(r"identify|identity|auth|login|token|session|permission", re.IGNORECASE)


class Module:
    """1 つの backend ファイルの構文木と、解決に使う表(関数・定数・import)を持つ。"""

    def __init__(self, path):
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.funcs, self.consts, self.imports, self.from_imports = {}, {}, {}, {}
        for n in self.tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[n.name] = n
            elif isinstance(n, ast.Import):
                for a in n.names:
                    self.imports[a.asname or a.name] = a.name
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                for a in n.names:  # from queries import _require_date → 呼出 _require_date(...) を queries の関数へ辿る
                    self.from_imports[a.asname or a.name] = (n.module, a.name)
            elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                try:
                    self.consts[n.targets[0].id] = ast.literal_eval(n.value)
                except ValueError:
                    pass  # 定数でないものは評価しない(f 文字列の穴は下の _eval が失敗して X になる)

    def env(self):
        """f 文字列の穴を評価する名前空間。定数 + 引数なし・return 1 つの関数(例: SELECT 列を組む補助)。"""
        env = dict(self.consts)
        for name, f in self.funcs.items():
            rets = [n for n in ast.walk(f) if isinstance(n, ast.Return)]
            if not f.args.args and len(rets) == 1 and rets[0].value is not None:
                expr = ast.Expression(rets[0].value)
                ast.fix_missing_locations(expr)
                env[name] = lambda e=expr, s=self: eval(compile(e, "<f>", "eval"), {"__builtins__": {}}, s.consts)
        return env


def _render(node, env, sql):
    """文字列を表す式(定数・f 文字列・+ 連結)を文字列にする。表せなければ None。
    穴は評価できれば値、できなければ SQL では X(構造だけ保つ)、メッセージでは {式} を入れる。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _render(node.left, env, sql), _render(node.right, env, sql)
        return None if a is None or b is None else a + b
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
                continue
            try:
                expr = ast.Expression(v.value)
                ast.fix_missing_locations(expr)
                out.append(str(eval(compile(expr, "<f>", "eval"), {"__builtins__": {}}, env)))
            except Exception:  # noqa: BLE001 - 評価不能は placeholder にする(推測しない)
                out.append("X" if sql else "{" + ast.unparse(v.value) + "}")
        return "".join(out)
    return None


def _mask(sql):
    """-- コメントと '...' の中身を空白・x に置き換える(位置は変えない)。括弧・キーワードの走査を惑わせないため。"""
    sql = re.sub(r"--[^\n]*", lambda m: " " * len(m.group(0)), sql)
    return re.sub(r"'[^']*'", lambda m: "'" + "x" * (len(m.group(0)) - 2) + "'", sql)


def _depth0_selects(m):
    """マスク済み SQL から、括弧の深さ 0 の SELECT ごとに (select 句の開始, FROM の位置) を返す。"""
    depth, out, cur = 0, [], None
    for t in re.finditer(r"[()]|\bSELECT\b|\bFROM\b", m, re.IGNORECASE):
        w = t.group(0).upper()
        if w == "(":
            depth += 1
        elif w == ")":
            depth -= 1
        elif depth == 0 and w == "SELECT":
            cur = t.end()
        elif depth == 0 and w == "FROM" and cur is not None:
            out.append((cur, t.start()))
            cur = None
    return out


def _split0(m, s, e):
    """m[s:e] を深さ 0 のカンマで割った (開始, 終了) の並び。"""
    parts, depth, st = [], 0, s
    for i in range(s, e):
        c = m[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append((st, i))
            st = i + 1
    parts.append((st, e))
    return parts


def _tables(m):
    """マスク済み SQL の別名と CTE を実表へ解決する。(別名→表の dict, real(名前) -> 実表名) を返す。_dataset と _clauses が共有する。"""
    alias, ctes = {}, {}
    for t in _FROM_TBL.finditer(m):
        tbl, al = t.group(1), t.group(2)
        alias.setdefault(tbl, tbl)
        if al and al.upper() not in _KW:
            alias.setdefault(al, tbl)
    for c in _CTE.finditer(m):  # WITH d AS (SELECT ... FROM <実表>) の d を実表へ解決する
        end, depth = len(m), 1  # 本体は対応する ")" まで。外の FROM(次の CTE や本文)を拾わない
        for i in range(c.end(), len(m)):
            depth += (m[i] == "(") - (m[i] == ")")
            if depth == 0:
                end = i
                break
        f = _FROM_TBL.search(m, c.end(), end)
        ctes[c.group(1)] = f.group(1) if f else "X"  # 本体に実表が見えない(f 文字列の穴など)CTE は X = 不明

    def real(name):
        name = alias.get(name, name)
        seen = set()
        while name in ctes and name not in seen:  # CTE が別の CTE を FROM する連鎖(t → k → cur → 実表)を実表まで辿る
            seen.add(name)
            name = ctes[name]
        return name

    return alias, real


def _colref(text, real, main):
    """列の書き(t.col・col)を (論理表, 列) にする。別名は real で実表へ直す。式は表を空にして列の欄へそのまま入れる。"""
    if re.fullmatch(r"[A-Za-z_]\w*\.\w+", text):
        q, c = text.split(".")
        return real(q), c
    if re.fullmatch(r"[A-Za-z_]\w*", text):
        return main, text
    return "", text


def _split_top(m, depth, s, e, rx):
    """m[s:e] を深さ 0 で rx に当たる箇所で割る。[(直前の区切りの語, 開始, 終了)] を返す(先頭の区切りは空文字)。"""
    pieces, sep, st = [], "", s
    for t in rx.finditer(m, s, e):
        if depth[t.start()] == 0:
            pieces.append((sep, st, t.start()))
            sep, st = t.group(0), t.end()
    pieces.append((sep, st, e))
    return pieces


def _conds(sql, m, depth, s, e, real, main):
    """WHERE の区間を AND/OR で割って条件の行にする。BETWEEN の AND は割らない(ponytail: BETWEEN は語の有無だけで判定する。上限は構文の複雑さに比例して外れる)。"""
    items = []  # [連結語, 条件の文, BETWEEN の途中か]
    for sep, a, b in _split_top(m, depth, s, e, _AND_OR):
        txt = " ".join(sql[a:b].split())
        if not txt:
            continue
        if items and items[-1][2]:
            items[-1][1] += " AND " + txt
            items[-1][2] = False
            continue
        items.append([sep.upper(), txt, bool(re.search(r"\bBETWEEN\b", txt, re.IGNORECASE))])
    rows, n = [], 0
    for sep, txt, _ in items:
        cm = _COND.match(txt)
        if not cm:  # 形の分からない条件は列を空のまま、文だけ残す
            rows.append({"and_or": sep, "text": txt, "logical_table": "", "logical_column": "", "operator": "", "param_no": "", "param_name": "", "expression": txt})
            continue
        tbl, col = _colref(cm.group(1), real, main)
        # BETWEEN の右辺は「A AND B」の2つの値。それ以外は右辺の1つの値。プレースホルダは param# と名前にする
        op = " ".join(cm.group(2).upper().split())
        rhs = " ".join(cm.group(3).split())
        parts = re.split(r"\s+AND\s+", rhs, flags=re.IGNORECASE) if op == "BETWEEN" else [rhs]
        pnos, pnames, vals = [], [], []
        for p in parts:
            pm = _PARAM.match(p.strip())
            if pm:
                n += 1
                nm = pm.group(1) or pm.group(2) or ""
                pnos.append(str(n))
                pnames.append(nm)
                vals.append(nm or "?")
            else:
                vals.append(p.strip())
        rows.append({"and_or": sep, "text": txt, "logical_table": tbl, "logical_column": col, "operator": op,
                     "param_no": ",".join(pnos), "param_name": ", ".join(pnames),
                     "value": " AND ".join(vals) if op == "BETWEEN" else (vals[0] if vals else "")})
    return rows


def _clauses(sql):
    """本体 SELECT(深さ 0 の最初の SELECT)から JOIN・WHERE・GROUP BY・ORDER BY と式の別名を取る。
    generate_docx.js が読む joins / where の形に合わせ、group_by / order_by も同じく表と列の組で置く。解けない句は行を作らない。"""
    m = _mask(sql)
    out = {k: [] for k in _CLAUSE_KEYS}
    sels = _depth0_selects(m)
    if not sels:
        return out
    real = _tables(m)[1]
    s0, f0 = sels[0]
    ft = _FROM_TBL.match(m, f0)
    main = real(ft.group(1)) if ft else ""
    # 本体が CTE を FROM するとき(WITH d AS (... WHERE ...) SELECT ... FROM d)、絞り込みは CTE の中にある。その WHERE も取る
    cte = ft and re.search(r"\b" + re.escape(ft.group(1)) + r"\s+AS\s*\(", m[:s0], re.IGNORECASE)
    if cte:
        d, end = 1, cte.end()
        while end < s0 and d:
            d += (m[end] == "(") - (m[end] == ")")
            end += 1
        out["where"] += _clauses(sql[cte.end():end - 1])["where"]
    depth, d = [], 0  # depth[i] = 文字 i の前の括弧の深さ
    for c in m:
        depth.append(d)
        d += (c == "(") - (c == ")")
    kws = [(t.start(), t.end(), " ".join(t.group(0).upper().split())) for t in _CLAUSE_KW.finditer(m, f0) if depth[t.start()] == 0]
    for i, (pos, kend, kw) in enumerate(kws):
        end = kws[i + 1][0] if i + 1 < len(kws) else len(m)
        if kw.endswith("JOIN"):
            jt = _FROM_TBL.search(m, pos, end)
            on = re.search(r"\bON\b", m[pos:end], re.IGNORECASE)
            out["joins"].append({"type": kw, "logical_table": real(jt.group(1)) if jt else "",
                                 "condition": " ".join(sql[pos + on.end():end].split()) if on else ""})
        elif kw == "WHERE":
            out["where"] += _conds(sql, m, depth, kend, end, real, main)
        elif kw == "GROUP BY":
            for _, a, b in _split_top(m, depth, kend, end, _COMMA):
                txt = " ".join(sql[a:b].split())
                if txt:
                    tbl, col = _colref(txt, real, main)
                    out["group_by"].append({"logical_table": tbl, "logical_column": col})
        elif kw == "ORDER BY":
            for _, a, b in _split_top(m, depth, kend, end, _COMMA):
                txt = " ".join(sql[a:b].split())
                dm = re.match(r"^(.*?)\s+(ASC|DESC)$", txt, re.IGNORECASE)
                head, direction = (dm.group(1), dm.group(2).upper()) if dm else (txt, "")
                if head:
                    tbl, col = _colref(head, real, main)
                    out["order_by"].append({"logical_table": tbl, "logical_column": col, "direction": direction})
    for a, b in _split0(m, s0, f0):  # 式の別名(COUNT(DISTINCT ...) AS days 等)。物理列の別名 p.x AS id は別名に入れない
        item = re.sub(r"^\s*DISTINCT\s+", "", sql[a:b].strip(), flags=re.IGNORECASE)
        am = re.search(r"\bAS\s+([^\W\d]\w*)\s*$", item, re.IGNORECASE)
        if am:
            src = " ".join(re.sub(r"--[^\n]*", " ", item[:am.start()]).split())  # SQL のコメントは式に入れない
            if not re.fullmatch(r"[\w.]+", src):
                out["aliases"].append({"alias": am.group(1), "expression": src})
    return out


def _when_text(test):
    """if の条件式を、行に付ける when の文にする(B1)。X → 「X が指定されたとき」、not X → 「X が未指定のとき」、他は「条件: 式」。"""
    if isinstance(test, ast.Name):
        return f"{test.id} が指定されたとき"
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Name):
        return f"{test.operand.id} が未指定のとき"
    return "条件: " + ast.unparse(test)


def _local_lists(fn, env):
    """関数の中で組む文字列の並び(where = ["a", ...]; where.append("b"))を {名前: [(文字列, when か None)]} にする。
    f 文字列の {' AND '.join(where)} を評価できるようにするため。要素が表せなければ X(構造だけ保つ)。
    when: append が if の中(body)なら、囲む if の条件の文(_when_text)。else 側なら「条件: not (式)」。囲む if が無ければ None(無条件)。
    ponytail: 条件付きの append も全部足す(分岐は見ない)。WHERE の候補を全部出す側に倒す。"""
    def one(e):  # 文字列の式か、モジュール定数の名前(LIVE = "..." 等)
        v = env.get(e.id) if isinstance(e, ast.Name) else _render(e, env, True)
        return v if isinstance(v, str) else "X"

    def whens(n):  # n を囲む if の条件を「かつ」で連ねる。body に居れば条件そのまま、orelse に居れば否定
        out = []
        for i in ast.walk(fn):
            if not isinstance(i, ast.If):
                continue
            if any(x is n for b in i.body for x in ast.walk(b)):
                out.append(_when_text(i.test))
            elif any(x is n for b in i.orelse for x in ast.walk(b)):
                out.append("条件: not (" + ast.unparse(i.test) + ")")
        return " かつ ".join(out) or None

    lists = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.List):
            lists[n.targets[0].id] = [(one(e), None) for e in n.value.elts]
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "append"
                and isinstance(n.func.value, ast.Name) and n.func.value.id in lists and n.args):
            lists[n.func.value.id].append((one(n.args[0]), whens(n)))
    return lists


def _cond_map(lists):
    """_local_lists の結果から、条件付きの断片の {正規化した字面: when} を作る(_conds の行と突き合わせる)。"""
    return {" ".join(t.split()): w for v in lists.values() for t, w in v if w}


def _mark_conditional(cl, conds):
    """句の結果(_clauses の戻り)の where 行のうち、条件付きの断片と字面が同じ行に when を付ける(B1)。"""
    for r in cl["where"]:
        if r.get("text") in conds:
            r["when"] = conds[r["text"]]
    return cl


def _split_conditional(defn, conds):
    """CTE の定義文(正規化済み)から、条件付きの断片を取り除き、取り除いた断片を [{sql, when}] で返す(B1)。
    断片は直前の AND、無ければ直後の AND、それも無ければ WHERE と一緒に外す。残りの定義が無条件の部分になる。"""
    parts = []
    for text, when in (conds or {}).items():
        p = re.escape(text)
        for pat, rep in ((r"\s+AND\s+" + p + r"(?=\s|\)|$)", ""), (r"(WHERE\s+)" + p + r"\s+AND\s+", r"\1"),
                         (r"\s+WHERE\s+" + p + r"(?=\s|\)|$)", "")):
            new, n = re.subn(pat, rep, defn, count=1, flags=re.IGNORECASE)
            if n:
                defn = new
                parts.append({"sql": text, "when": when})
                break
    return defn, parts


def _table_aliases(sql):
    """本体の SQL の別名 → 実表({別名: 実表})。別名と実表が同じものは除く(B1。H2 の別名を実表へ解く材料)。"""
    alias, real = _tables(_mask(sql))
    return {a: real(a) for a in alias if real(a) != a}


def _find_nows(flat, call):
    """空白を無視して call を flat から探す。(開始, 終了) を flat の位置で返す。無ければ (-1, -1)(B1。改行・CASE・ORDER BY を含む集計の別名探索に使う)。"""
    keep = [i for i, ch in enumerate(flat) if not ch.isspace()]
    nows = "".join(flat[i] for i in keep)
    key = "".join(call.split())
    p = nows.find(key)
    if p < 0:
        return -1, -1
    return keep[p], keep[p + len(key) - 1] + 1


def _merge(acc, got):
    """同じ API の別の SQL から取れた句の行を重ねる。同じ行は 1 つだけ残す。"""
    for k in _CLAUSE_KEYS:
        acc[k] += [x for x in got[k] if x not in acc[k]]


def _scenarios(fn, env):
    """関数内の分岐で値が変わる変数を、引数の値ごとの env に展開する(C4)。[(ラベル, env)] を返す。
    対象は「名前 = 定数辞書[引数]」と「名前 = A if 引数 == 値 else B」。どちらも無ければ [("", env)] だけ。
    なぜ要るか: GROUP BY {', '.join(keys)} や ORDER BY {reji_col if ...} は単一の env では評価できず X になる。
    呼出元: extract()(SQL の組み直しと句の重ね合わせ)。最大 4 通り(ponytail: 超えた分は捨てる。分岐が増えたら絞り方を決める)。"""
    cands = {}  # 変数名 -> [(引数名, 値, 否定か, 評価値)]
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)):
            continue
        v, name = n.value, n.targets[0].id
        if isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name) and isinstance(env.get(v.value.id), dict) \
                and isinstance(v.slice, ast.Name):
            cands[name] = [(v.slice.id, k, False, val) for k, val in env[v.value.id].items()]
        elif isinstance(v, ast.IfExp) and isinstance(v.test, ast.Compare) and isinstance(v.test.left, ast.Name) \
                and len(v.test.ops) == 1 and isinstance(v.test.ops[0], ast.Eq) \
                and isinstance(v.test.comparators[0], ast.Constant):
            a, b = _render(v.body, env, True), _render(v.orelse, env, True)
            if a is not None and b is not None:
                var, k = v.test.left.id, v.test.comparators[0].value
                cands[name] = [(var, k, False, a), (var, k, True, b)]
    vals, from_dict = {}, set()  # 引数名 -> 取りうる値。辞書のキーが全候補、条件だけの引数には「それ以外」(None)を足す
    for c in cands.values():
        for var, k, neg, _ in c:
            vals.setdefault(var, [])
            if k not in vals[var] and not neg:
                vals[var].append(k)
            if not (len(c) == 2 and c[0][1] == c[1][1] and c[1][2]):  # 否定の候補を伴う = A if 引数 == 値 else B
                from_dict.add(var)
    for var in vals:
        if var not in from_dict:
            vals[var].append(None)
    out = [({}, {})]
    for var, ks in vals.items():
        out = [({**sc, var: k}, {}) for sc, _ in out for k in ks]
    res = []
    for sc, _ in out[:4]:
        e2 = {**env, **{v: k for v, k in sc.items() if k is not None}}  # f 文字列の中の {A if group == 'x' else B} も評価できるよう引数の値も入れる
        for name, c in cands.items():
            for var, k, neg, val in c:
                if (sc.get(var) == k) != neg:
                    e2[name] = val
                    break
        res.append((" かつ ".join(f"{v} = {k}" if k is not None else f"{v} がそれ以外" for v, k in sc.items()), e2))
    return res or [("", env)]


def _merge_scenarios(acc, per, labels):
    """通りごとの句(per)を重ねる。全ての通りにある行はそのまま、一部にしか無い行には when(その通りのラベル)を付ける。
    呼出元: extract()。接続: generate_docx.js 側は when を知らなくても行は出る(E1 が適用欄を足す)。"""
    if len(per) == 1:
        return _merge(acc, per[0])
    for k in _CLAUSE_KEYS:
        rows = [r for p in per for r in p[k]]
        for r in rows:
            where = [labels[i] for i, p in enumerate(per) if r in p[k]]
            scen = "" if len(where) == len(per) else " / ".join(where)  # 通りの when と、条件付き断片の when(_mark_conditional)を連ねる(B1)
            row = {**r, "when": " かつ ".join(x for x in (scen, r.get("when", "")) if x)} if (scen or r.get("when")) else r
            if row not in acc[k]:
                acc[k].append(row)


def _src(mod, node):
    """AST のノードを出典の {file, line_start, line_end}(1始まり・両端を含む)にする。
    呼出元: extract()・_errors()・_returns()・_auth()。意味合い: 下書きの要素が指すソースの行を機械で付ける(数字は LLM に書かせない)。
    接続: react 側の _span と同じ形。合成側(_units の _fill_src、A3)が同じ形で読む。"""
    return {"file": str(mod.path), "line_start": node.lineno, "line_end": node.end_lineno}


def _call_name(c):
    """呼出の関数名(f(...) なら f、obj.f(...) なら f)。取れなければ空。"""
    f = c.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _expand(errs, fn, clo):
    """エラー文面の {式} を、呼出側の実引数で展開して text_expanded に入れる(取れなければ空)。
    穴のある文面は、実引数が全部定数の呼出箇所ごとに1件ずつ返す(from・to・shop_id の別々の文面を落とさない。F1)。
    呼出元: extract(errors への積み込み)。同じ文の重複は呼出元が src だけ足してまとめる。
    ponytail: 呼出は関数名の一致で拾う(同名の別関数を取り違える余地あり)。実引数が定数の穴だけ展開する。"""
    params = [a.arg for a in fn.args.args]
    sites = [c for _, f in clo for c in ast.walk(f) if isinstance(c, ast.Call) and _call_name(c) == fn.name]
    out = []
    for e in errs:
        holes = re.findall(r"\{([^{}]+)\}", e["message"])
        if not holes:
            e["text_expanded"] = e["message"]
            first = e["message"].split(" ")[0]
            e["param"] = first if first.isascii() and first.isidentifier() else ""  # F2: 文面の先頭の語(例 group)
            out.append(e)
            continue
        texts = []
        for site in sites:
            amap = {params[i]: a for i, a in enumerate(site.args) if i < len(params)}
            amap.update({k.arg: k.value for k in site.keywords if k.arg})
            vals = [amap.get(h) for h in holes]
            if all(isinstance(v, ast.Constant) for v in vals):
                text = e["message"]
                for h, v in zip(holes, vals):
                    text = text.replace("{" + h + "}", str(v.value))
                if text not in [x[0] for x in texts]:
                    # F2: 文面が穴で始まるとき、その穴の実引数が検査の対象(param)
                    texts.append((text, str(vals[0].value) if e["message"].startswith("{" + holes[0] + "}") else ""))
        # 取れた展開ごとに別の行にする。1件も取れなければ従来どおり空の1件
        for t, prm in texts or [("", "")]:
            out.append({**e, "text_expanded": t, "param": prm, "src": list(e.get("src") or [])})
    return out


def _auth(mods, tables):
    """入口の関数(ディスパッチ表の名前を参照する関数)が、表を引く前に呼ぶ認証の関数を拾い、その応答の文面を返す(status は静的に取れなければ空)。
    ponytail: 入口は表の名前の参照で、認証は _AUTH_CALL の名前一致で近似する。外れたら推測で埋めず空のままにする。"""
    out, seen = [], set()
    for mod in [x for x in mods.values() if x is not None]:
        for entry in mod.funcs.values():
            # 入口 = ディスパッチ表の名前を参照するか、dispatch / lambda_handler を呼ぶ関数(web_app の _get は表を持たず lambda_handler を呼ぶ)
            refs = [n.lineno for n in ast.walk(entry) if (isinstance(n, ast.Name) and n.id in tables) or (isinstance(n, ast.Call) and _call_name(n) in ("dispatch", "lambda_handler"))]
            if not refs:
                continue
            for c in ast.walk(entry):
                name = _call_name(c) if isinstance(c, ast.Call) else ""
                if not name or name in seen or c.lineno >= min(refs) or not _AUTH_CALL.search(name):
                    continue
                seen.add(name)
                for m2, f2 in _callees(entry, mod, mods):
                    if f2.name != name:
                        continue
                    clo = _closure(m2, f2, mods)
                    errs = []
                    for m3, f3 in clo:
                        errs += _expand(_errors(f3, m3.env(), m3), f3, clo)  # 認証の関数の応答は全部を出す(status が取れなければ空)
                    out.append({"function": name, "applies_to": "全 API(各 API の処理に入る前に認証する)",
                                "fact": ("認証に失敗すると " + "、".join(f"{e['status_code']}（{e.get('text_expanded') or e['message']}）" for e in errs if e.get("status_code")) + " を返す") if any(e.get("status_code") for e in errs) else "", "error_patterns": errs, "src": [_src(mod, c)]})  # 関数名は function の欄にだけ残す
                    break
    return out


def _dataset(sql, unresolved, action):
    """SQL 1 本から [{column, table}] を作る。最上位 SELECT だけを見る(WITH の中身・副問合せの列は含めない)。"""
    m = _mask(sql)
    alias, real = _tables(m)

    rows = []
    for s, f in _depth0_selects(m):
        ft = _FROM_TBL.match(m, f)
        main = real(ft.group(1)) if ft else ""
        for a, b in _split0(m, s, f):
            item = re.sub(r"^\s*DISTINCT\s+", "", sql[a:b].strip(), flags=re.IGNORECASE)
            if not item or item.endswith("*"):
                continue
            am = re.search(r"\bAS\s+([^\W\d]\w*)\s*$", item, re.IGNORECASE)
            if am:
                # p.mise_no AS id は物理列 mise_no(別名 id を物理名にしない)。式の列(count(*) AS days 等)は別名のまま
                src = item[:am.start()].strip()
                col = src.split(".")[-1] if re.fullmatch(r"[A-Za-z_]\w*\.\w+", src) else am.group(1)
            elif re.fullmatch(r"[\w.]+", item):
                col = item.split(".")[-1]
            else:
                unresolved.append({"action": action, "reason": f"別名の無い式の列を取れない: {' '.join(item.split())[:60]}"})
                continue
            q = re.search(r"\b([A-Za-z_]\w*)\.\w+", _mask(item))
            tbl = real(q.group(1)) if q and q.group(1) in alias else main
            if tbl != "X":
                rows.append({"column": col, "table": tbl})
            else:
                unresolved.append({"action": action, "reason": f"列「{col}」の出所テーブルを静的に取れない(CTE の FROM が動的)"})
    return rows


def _close(t, i):
    """t[i] の開き括弧に対応する閉じ括弧の位置(無ければ末尾)。文字列の中の括弧は見ない(ponytail: 文字列に括弧があると外れる)。"""
    pair = {"(": ")", "[": "]", "{": "}"}[t[i]]
    depth = 0
    for j in range(i, len(t)):
        depth += (t[j] == t[i]) - (t[j] == pair)
        if depth == 0:
            return j
    return len(t)


_DDL_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)\s*\(", re.IGNORECASE)
_DDL_ALTER = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([\w.\"]+)([^;]*);", re.IGNORECASE)
_DDL_COMMENT = re.compile(r"COMMENT\s+ON\s+COLUMN\s+([\w.\"]+)\s+IS\s+'((?:[^']|'')*)'", re.IGNORECASE)
_DDL_NOT_COL = {"PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "CHECK", "EXCLUDE", "LIKE", "INDEX", "KEY"}


def _ddl(root):
    """root 配下の *.sql から {表: {列: コメント}} を作る(列は定義順)。CREATE TABLE の列と行末の -- コメント、
    ALTER TABLE ... ADD COLUMN、COMMENT ON COLUMN を読む。SELECT * の展開と論理名の候補(comment)に使う。
    ponytail: 列の定義は1行1列の前提で行ごとに読む。複数行にまたがる列定義・DB に直接付けたコメントは見ない。"""
    out = {}
    if not root or not Path(root).is_dir():
        return out
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("node_modules", ".git", "venv", ".venv", "__pycache__")]
        for n in names:
            if not n.endswith(".sql"):
                continue
            try:
                t = (Path(d) / n).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in _DDL_TABLE.finditer(t):
                cols = out.setdefault(m.group(1).replace('"', "").split(".")[-1], {})
                for line in t[m.end():_close(t, m.end() - 1)].splitlines():
                    cm = re.match(r'\s*"?([A-Za-z_]\w*)"?\s+\w', line)
                    if cm and cm.group(1).upper() not in _DDL_NOT_COL:
                        note = re.search(r"--\s*(.+?)\s*$", line)
                        cols.setdefault(cm.group(1), note.group(1) if note else "")
            for m in _DDL_ALTER.finditer(t):
                cols = out.setdefault(m.group(1).replace('"', "").split(".")[-1], {})
                for cm in re.finditer(r'\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"?([A-Za-z_]\w*)"?', m.group(2), re.IGNORECASE):
                    if cm.group(1).upper() not in _DDL_NOT_COL:
                        cols.setdefault(cm.group(1), "")
            for m in _DDL_COMMENT.finditer(t):
                parts = m.group(1).replace('"', "").split(".")
                if len(parts) >= 2:
                    out.setdefault(parts[-2], {})[parts[-1]] = m.group(2).replace("''", "'")
    return out


def _ddl_types(root):
    """root 配下の *.sql から {表: {列: (型, NOT NULL か)}} を作る(B3)。型は先頭の語を小文字にしたもの(varchar(20) → varchar)。
    意味合い: index signature の列(api.ts に欄名が無い)の型を、SQL の列の元の定義から確かめる材料にする。
    呼出元: main と selftest が hints["ddl_types"] に入れ、extract の _sql_type が読む。
    ponytail: _ddl と同じく1行1列の前提。ALTER ... ADD COLUMN の型は読むが NOT NULL は見ない。"""
    out = {}
    if not root or not Path(root).is_dir():
        return out
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("node_modules", ".git", "venv", ".venv", "__pycache__")]
        for n in names:
            if not n.endswith(".sql"):
                continue
            try:
                t = (Path(d) / n).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in _DDL_TABLE.finditer(t):
                cols = out.setdefault(m.group(1).replace('"', "").split(".")[-1], {})
                for line in t[m.end():_close(t, m.end() - 1)].splitlines():
                    cm = re.match(r'\s*"?([A-Za-z_]\w*)"?\s+([A-Za-z]\w*)(.*)', line)
                    if cm and cm.group(1).upper() not in _DDL_NOT_COL:
                        cols.setdefault(cm.group(1), (cm.group(2).lower(), bool(re.search(r"\bNOT\s+NULL\b", cm.group(3), re.IGNORECASE))))
            for m in _DDL_ALTER.finditer(t):
                cols = out.setdefault(m.group(1).replace('"', "").split(".")[-1], {})
                for cm in re.finditer(r'\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"?([A-Za-z_]\w*)"?\s+([A-Za-z]\w*)', m.group(2), re.IGNORECASE):
                    if cm.group(1).upper() not in _DDL_NOT_COL:
                        cols.setdefault(cm.group(1), (cm.group(2).lower(), False))
    return out


def _sql_type(c, dt):
    """SELECT 列 c({expression, table, column})の SQL 上の型の手掛かりを (db_type, db_nullable) で返す(B3)。
    取れる順に: 式の末尾の ::型(NULL::型 なら null 可)、COUNT(...) は bigint、1列だけの素の式は DDL(dt = _ddl_types)の型と NOT NULL。
    取れなければ (None, None)(SUM(x) の無印などは推測しない)。呼出元: extract(index signature の列の response_fields)。"""
    expr = c["expression"]
    m = re.search(r"::\s*(\w+)\s*$", expr)
    if m:
        return m.group(1).lower(), bool(re.match(r"NULL\s*::", expr, re.IGNORECASE))
    if re.match(r"COUNT\s*\(", expr, re.IGNORECASE):
        return "bigint", False
    t = (dt.get(c.get("table")) or {}).get(c.get("column")) if re.fullmatch(r"(?:\w+\.)?\w+", expr) else None
    return (t[0], not t[1]) if t else (None, None)


def _ui_specs(paths):
    """TSX の直下の定数配列(const X = [ {label: "..", kin: "列", ...}, ... ])の要素を (label, [他の文字列の値], {値: 属性}) で返す。
    値が SELECT の別名(応答の項目のキー)と一致すれば、その列の画面の表示名が label と分かる。属性(kin・cnt 等)は末尾の語の推定に使う。
    要素に csv(CSV 用の見出し)があれば label の代わりにそれを名前にする(呼出元は main と selftest。結果は _candidates が読む)。"""
    out = []
    for p in paths:
        if p.suffix not in (".tsx", ".jsx") or not p.is_file():
            continue
        t = p.read_text(encoding="utf-8")
        for m in re.finditer(r"^const\s+[A-Za-z_$][\w$]*\s*(?::[^=\n]+)?=\s*\[", t, re.M):
            i, end = m.end(), _close(t, m.end() - 1)
            while i < end:
                if t[i] == "{":
                    j = _close(t, i)
                    o = t[i:j]
                    # csv: (CSV 用の見出し)があれば label より優先する。画面の label は「小計」「合計」のように重なるが、
                    # csv は「小計1(値引前)」のように一意で意味まで持つ(PosReport.tsx の行定義)。_candidates の ui_label になる
                    lm = re.search(r"\bcsv\s*:\s*['\"]([^'\"]+)['\"]", o) or re.search(r"\blabel\s*:\s*['\"]([^'\"]+)['\"]", o)
                    if lm:
                        pairs = [(k, v) for k, v in re.findall(r"\b(\w+)\s*:\s*['\"]([^'\"]+)['\"]", o) if k not in ("label", "csv")]
                        out.append((lm.group(1), [v for _, v in pairs], {v: k for k, v in pairs}))
                    i = j
                i += 1
    return out


def _type_blocks(paths):
    """TS の interface / type の本体ごとに (欄名の集合, {欄: コメント}) を返す。コメントは /** */ か行末の //。"""
    out = []
    for p in paths:
        if p.suffix not in (".ts", ".tsx") or not p.is_file():
            continue
        t = p.read_text(encoding="utf-8")
        for m in re.finditer(r"\b(?:interface\s+\w+[^{\n]*|type\s+\w+\s*=\s*)\{", t):
            body = t[m.end():_close(t, m.end() - 1)]
            names = set(re.findall(r"(?:^|;|\*/)\s*(\w+)\??\s*:", body, re.M))
            notes = {f: c for c, f in re.findall(r"/\*\*\s*(.*?)\s*\*/\s*(\w+)\??\s*:", body, re.S)}
            notes.update({f: c for f, c in re.findall(r"^\s*(\w+)\??\s*:[^\n]*?//\s*(.+?)\s*$", body, re.M)})
            out.append((names, notes))
    return out


# 列名の語 → 業務の語。guess(列名からの推定)にだけ使う。全部の語が引けたときだけ候補にする
_WORDS = {"id": "ID", "no": "番号", "cnt": "件数", "count": "件数", "kin": "金額", "amount": "金額", "name": "名称",
          "date": "日", "dates": "日付", "days": "日数", "first": "初回", "last": "最終", "shop": "店舗", "mise": "店",
          "reji": "レジ", "registers": "レジ数", "warn": "警告", "kai": "会計", "total": "合計", "sum": "合計",
          "code": "コード", "type": "種別", "status": "状態", "qty": "数量", "price": "単価", "created": "作成",
          "updated": "更新", "at": "日時", "user": "利用者", "tax": "税", "rate": "率"}


def _guess(name):
    toks = (name or "").lower().split("_")
    if not name or not all(x in _WORDS or x.isdigit() for x in toks):
        return ""
    return "".join(_WORDS.get(x, x) for x in toks)


def _type_note(c):
    """型のコメントから論理名に使える頭の句を取る(括弧・句点の前、20 字以内、式や引用符を含まない)。使えなければ空。"""
    c = re.split(r"[（(。、,]", c or "")[0].strip()
    return c if c and len(c) <= 20 and not re.search(r"[=\"'`]", c) and not c.isascii() else ""


def _attr_suffix(note):
    """型のコメントが「点数側の列」「の列」の形なら、その頭の語(点数)を返す。それ以外は空(T7)。"""
    m = re.fullmatch(r"(.+?)(?:側の列|の列)", note or "")
    return m.group(1) if m else ""


def _attr_note(blocks, key):
    """TSX の行の属性名(cnt・kin)と同じ欄に付いた型のコメント(生の文)を、型ブロックの中から探す。無ければ空(T7)。
    ponytail: 同じ属性名の欄が複数の型にあれば最初の型のコメントを使う。"""
    for _, notes in blocks:
        if key and key in notes:
            return notes[key]
    return ""


def _doc_hints(paths):
    """backend の python の docstring と # コメントから「日本語(識別子)」を {識別子: 日本語} で取る(T7)。
    同じ識別子に別の語が付くときは取らない。呼出元: extract(hints["doc"] → _candidates の docstring の出所)。
    ponytail: 日本語の並びは最長 12 字で切る(「各店番号(mise_no)」は「各店番号」になる)。"""
    seen = {}
    for p in paths:
        if p.suffix != ".py" or not p.is_file():
            continue
        t = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(t)
        except SyntaxError:
            continue
        texts = [ast.get_docstring(tree) or ""]
        texts += [tok.string.lstrip("#") for tok in tokenize.generate_tokens(io.StringIO(t).readline) if tok.type == tokenize.COMMENT]
        for text in texts:
            for jp, ident in _DOC_HINT.findall(text):
                seen.setdefault(ident, set()).add(jp)
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


# 日本語(識別子) の書き方(全角・半角の括弧)。_doc_hints が読む
_DOC_HINT = re.compile(r"([々぀-ヿ㐀-鿿]{2,12})[（(]([A-Za-z_]\w*)[）)]")


def _pair_note(note, expr):
    """型のコメントが「AとB」の形なら、2列をまとめて説明しているので、式の種類で1つの意味に分ける(T18)。
    count 系(count(...))は A(「…数」で終わるとき)を、array_agg 系は B の「…の一覧」を名前にする。
    どちらの式でもない、または A が「数」で終わらない count は分けられないので空(候補を出さない)。「AとB」でない型のコメントはそのまま返す。
    WHY: 同じ「AとB」の名前を別々の別名に付けると、別の列が同じ論理名になる(取込欠損の疑いがある日数と日付)。出所は型の欄のグループ説明。"""
    parts = (note or "").split("と")  # fallback-ok: コメントが無い列は空のまま返す
    if len(parts) != 2 or not all(p.strip() for p in parts):
        return note
    a, b = parts[0].strip(), parts[1].strip()
    if re.search(r"\bcount\s*\(", expr or "", re.IGNORECASE):
        return a if a.endswith("数") else ""
    if re.search(r"\barray_agg\s*\(", expr or "", re.IGNORECASE):
        return b + "の一覧"
    return ""


def _candidates(cols, hints):
    """columns の各行に logical_name と logical_name_source を付ける。取れる順に comment / csv_header / docstring / ui_label / expr / api_type / guess。
    expr は式の別名(SUM・COUNT・MIN・MAX の1列の式)の元列の論理名から組む。CASE・四則演算は組まない。"""
    ddl, specs, blocks = hints.get("ddl") or {}, hints.get("ui") or [], hints.get("types") or []
    doc = hints.get("doc") or {}
    aliases = {c["alias"] for c in cols if c["alias"]}
    hits = [(lb, [v for v in vals if v in aliases], attr) for lb, vals, attr in specs]
    hits = [(lb, cs, attr) for lb, cs, attr in hits if cs]
    notes = {}
    # 別名を最も多く含む型を1つだけ見る。3つ以上(別名が3つ未満なら全部)含み、型の欄の半分以上が別名であること。
    # ponytail: id・name のような汎用の欄名で、別の型のコメントを取り違えないための目安。型の名前は突き合わせない
    best = max(blocks, key=lambda b: len(b[0] & aliases), default=(set(), {}))
    hit = len(best[0] & aliases)
    if hit >= min(3, len(aliases)) and hit * 2 >= len(best[0]):
        for f, c in best[1].items():
            if f in aliases and _type_note(c):
                notes[f] = _type_note(c)
    ui = {}  # 列の別名 -> (論理名, 出所)。型のコメントの「側の列」の語を末尾に付け、取れなければ語の推定(推定)
    for lb, cs, attr in hits:
        for c in cs:
            n, guessed = lb, False
            if sum(1 for x, _, _ in hits if x == lb) > 1:  # ponytail: 同じ表示名(小計・合計)が複数の列に付くときは列名の数字で分ける
                n += "".join(re.findall(r"\d+", c))
            if len(cs) > 1:  # 1つの表示行に件数と金額のように複数の列があるときは、末尾の語で分ける
                last = c.rsplit("_", 1)[-1]
                word = _attr_suffix(_type_note(_attr_note(blocks, attr.get(c, ""))))  # 属性(cnt・kin)の型のコメントが優先
                if not word:
                    key = attr.get(c) if attr.get(c) in ("kin", "cnt") else last
                    word, guessed = _WORDS.get(key, key), True
                n += word
            ui.setdefault(c, (n + ("(推定)" if guessed else ""), "ui_label_guess" if guessed else "ui_label"))
    csv = hints.get("csv") or {}
    # 1回目: 元列の論理名になるもの(comment・csv_header・ui_label・api_type)を列ごとに出す。式の別名はこれを材料に組む
    rows = []
    for c in cols:
        com = ddl.get(c["table"]) or {}
        plain = re.fullmatch(r"[\w.]+", c["expression"]) is not None  # 式の別名は元列のコメントを借りず、別名そのもので引く
        keys = (c["column"], c["alias"]) if plain else (c["alias"],)
        ui_name, ui_src = ui.get(c["alias"], (None, "ui_label"))
        rows.append([(next((com[k] for k in keys if com.get(k)), ""), "comment"),
                     (next((csv[k] for k in keys if csv.get(k)), ""), "csv_header"),
                     (next((doc[k] for k in keys if doc.get(k)), ""), "docstring"),
                     (ui_name, ui_src),
                     (_pair_note(notes.get(c["alias"]), c["expression"]), "api_type")])
    known = {}  # 元列の論理名の材料。別名ごとに取れた最初の名前(guess は材料にしない)
    for c, cs in zip(cols, rows):
        got = next((n for n, _ in cs if n), "")
        if got:
            known.setdefault(c["alias"], got)
    # 2回目: expr を ui_label の後・api_type の前に入れて、取れた順に決める
    for c, cs in zip(cols, rows):
        ex = ""
        em = re.fullmatch(r"(SUM|COUNT|MIN|MAX)\s*\(\s*[\w.]+\s*\)(?:::\w+)?", c["expression"], re.IGNORECASE)  # CASE・四則演算は種類が無いので組まない
        # 元列の名前の材料が comment/csv/ui/型に無いときは、列名の推定(guess)で組む(例: min(kai_date) → 最初の会計日)。guess のままより expr の出所のほうが式の意味が残る
        src = (ddl.get(c["table"]) or {}).get(c["column"]) or known.get(c["column"]) or _guess(c["column"])
        if em and c["column"] and src:
            ex = {"SUM": src + "の合計", "COUNT": src + "の件数", "MIN": "最初の" + src, "MAX": "最後の" + src}[em.group(1).upper()]
        order = cs[:4] + [(ex, "expr")] + cs[4:] + [(_guess(c["alias"]) or _guess(c["column"]), "guess")]
        name, how = next(((n, s) for n, s in order if n), ("", ""))
        c["logical_name"], c["logical_name_source"] = name, how
    return cols


def _columns(sql, ddl, unresolved, action):
    """最上位 SELECT(深さ 0 の最初の SELECT)の全列の骨組みと FROM の行を返す。(from, columns)。
    列は {alias, expression, table, column}。column は元テーブルの列(式が1つの列だけを参照するときだけ。count(*) 等は空)。
    SELECT * / t.* は DDL の列で展開し、DDL に無ければ * の1行を残して unresolved に出す。"""
    m = _mask(sql)
    alias, real = _tables(m)
    sels = _depth0_selects(m)
    if not sels:
        return {"table": "", "name": "", "alias": ""}, []
    s, f = sels[0]
    ft = _FROM_TBL.match(m, f)
    main = real(ft.group(1)) if ft else ""
    frm = {"table": main, "name": ft.group(1) if ft else "", "alias": (ft.group(2) or "") if ft else ""}
    nc = re.sub(r"--[^\n]*", lambda x: " " * len(x.group(0)), sql)  # コメントだけ消す(文字列は残す)
    cols = []
    for a, b in _split0(m, s, f):
        item = " ".join(re.sub(r"^\s*DISTINCT\s+", "", nc[a:b].strip(), flags=re.IGNORECASE).split())
        if not item:
            continue
        am = re.search(r"\bAS\s+([^\W\d]\w*)\s*$", item, re.IGNORECASE)
        expr = item[:am.start()].strip() if am else item
        if expr.endswith("*") and re.fullmatch(r"(?:[A-Za-z_]\w*\.)?\*", expr):
            q = expr[:-1].rstrip(".")
            tbl = real(q) if q else main
            names = list(ddl.get(tbl) or {})
            if not names:
                unresolved.append({"action": action, "reason": f"{expr} の列を DDL から引けない(表 {tbl})"})
                names = ["*"]
            cols += [{"alias": c, "expression": f"{q}.{c}" if q and c != "*" else expr if c == "*" else c, "table": tbl, "column": c,
                      "kind": "column", "real_column": f"{tbl}.{c}", "alias_name": "", "alias_expression": "", "source_columns": [f"{tbl}.{c}"]} for c in names]
            continue
        refs = list(dict.fromkeys(r for r in re.findall(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", _mask(expr)) if r[0] in alias))
        if expr == "X":  # f 文字列の穴を評価できなかった式(_render の X)。元の列は分からない
            tbl, col = main, ""
        elif re.fullmatch(r"[A-Za-z_]\w*", expr):
            tbl, col = main, expr
        elif refs:
            tbl, col = real(refs[0][0]), refs[0][1] if len(refs) == 1 else ""
        else:
            tbl, col = main, ""
        # 式の列は別名を物理列にする(式 min(kai_date) AS first_date の元列 kai_date を物理列にすると、別名の行が同じ物理列に曖昧に寄る)。
        # 式でない t.col は元の列名。H2 の dataset・logical_names はこの physical_column をそのまま使う(verify_partials.check_h2_columns が照合)
        alias_ = am.group(1) if am else col
        is_expr = not re.fullmatch(r"(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*", expr)
        phys = (alias_ if is_expr or not col else col) or expr
        # B1: 実列と別名の区別。kind=column は表.列そのもの(real_column が実列、alias_name は AS の名前)、
        # kind=alias_only は式(alias_name が別名、alias_expression が式の原文、実列は無い)。式が参照する実列は source_columns に全部出す
        # (COALESCE(s.name, ...) → shops.name 等。count(*) は空)。physical_column と違い、式の別名を物理列として書かない
        src = [f"{real(q)}.{c}" for q, c in refs if real(q) != "X"] or ([f"{main}.{expr}"] if re.fullmatch(r"[A-Za-z_]\w*", expr) and expr != "X" else [])
        cols.append({"alias": alias_, "expression": expr, "table": "" if tbl == "X" else tbl, "column": col,
                     "physical_column": phys, "is_expression": is_expr,
                     "kind": "alias_only" if is_expr else "column", "real_column": "" if is_expr or not col or tbl == "X" else f"{tbl}.{col}",
                     "alias_name": am.group(1) if am else "", "alias_expression": expr if is_expr else "", "source_columns": src})
    return frm, cols


def _body(m, open_):
    """マスク済み m の open_ にある "(" から対応する ")" までの (中身の開始, 終了)。"""
    d = 0
    for i in range(open_, len(m)):
        d += (m[i] == "(") - (m[i] == ")")
        if d == 0:
            return open_ + 1, i
    return open_ + 1, len(m)


def _top_has(m, kw):
    """m(括弧の中身を含む SQL 片)の深さ 0 に語 kw があるか。副問合せ・CTE の絞り込み(WHERE)の有無を見る。"""
    d = 0
    for t in re.finditer(r"[()]|\b" + kw + r"\b", m, re.IGNORECASE):
        if t.group(0) == "(":
            d += 1
        elif t.group(0) == ")":
            d -= 1
        elif d == 0:
            return True
    return False


def _subqueries(sql, conds=None):
    """SQL の WITH の CTE と、CTE 以外の括弧つき副問合せを返す。(ctes, subqueries)。
    ctes: {name, definition, from(最初の FROM の表の書き), filtered(深さ 0 に WHERE があるか)}
    subqueries: {location(SELECT/FROM/JOIN/WHERE/HAVING/ON のどの句の中か), definition, filtered}
    なぜ要るか: CTE が絞っているのか(WHERE の有無)・副問合せがどの句に居るかを、LLM が読み違えて「CTE 空」「絞り込み無し」と書くのを防ぐ。
    呼出元: extract()(db_operations の ctes / subqueries)。ponytail: 句の判定は直前の同じ深さのキーワード。UNION の枝は区別しない。"""
    m = _mask(sql)
    depth, d = [], 0
    for c in m:
        depth.append(d)
        d += (c == "(") - (c == ")")
    ctes, cte_opens = [], set()
    for c in _CTE.finditer(m):
        o = c.end() - 1
        a, b = _body(m, o)
        f = _FROM_TBL.search(m, a, b)
        cte_opens.add(o)
        defn, parts = _split_conditional(" ".join(sql[a:b].split()), conds)  # 条件付きの断片は定義から外し、conditional_parts に持つ(B1)
        ctes.append({"name": c.group(1), "definition": defn, "conditional_parts": parts, "from": f.group(1) if f else "",
                     "filtered": _top_has(m[a:b], "WHERE")})
    subs = []
    for t in re.finditer(r"\(\s*SELECT\b", m, re.IGNORECASE):
        if t.start() in cte_opens:
            continue
        kw = [k for k in re.finditer(r"\b(SELECT|FROM|JOIN|WHERE|HAVING|ON)\b", m[:t.start()], re.IGNORECASE) if depth[k.start()] == depth[t.start()]]
        a, b = _body(m, t.start())
        subs.append({"location": kw[-1].group(1).upper() if kw else "", "definition": " ".join(sql[a:b].split()), "filtered": _top_has(m[a:b], "WHERE")})
    return ctes, subs


def _agg_units(sql, feats):
    """集計関数ごとに数える単位(GROUP BY の列)を返す。[{func, arg, alias, scope, group_by:["表.列"…], unit}]。
    scope が main なら本体 SELECT の GROUP BY、cte <名前> ならその CTE の本体の GROUP BY。group_by が空なら結果全体で1つ(unit は "全体")。
    なぜ要るか: count(*) AS days が「何を1と数えるか」(例: 店舗×レジ単位の日数)の取り違えを防ぐ。呼出元: extract()(db_operations の aggregate_units)。
    ponytail: GROUP BY が列でない式・番号のときは式の原文をそのまま出す。"""
    cte_list, subs = _subqueries(sql)
    ctes = {c["name"]: c["definition"] for c in cte_list}
    flat = " ".join(sql.split())

    def gb(body):
        rows = _clauses(body)["group_by"] if body else []
        return [(r["logical_table"] + "." if r["logical_table"] else "") + r["logical_column"] for r in rows]

    out = []
    for a in feats["aggregates"]:
        scope, alias, call = a["scope"], a["alias"], a["func"] + "(" + a["arg"] + ")"
        body = ctes.get(scope[4:], "") if scope.startswith("cte ") else sql
        if scope == "main":
            # 括弧つき副問合せの中の集計は、外側の GROUP BY ではなくその副問合せ自身の GROUP BY で数える(features は CTE しか区別しない)
            sub = next((s for s in subs if re.search(re.escape(call), s["definition"], re.IGNORECASE)), None)
            if sub:
                scope, body = "subquery " + sub["location"], sub["definition"]
        if alias is None:
            # array_remove(array_agg(..)) AS x のように集計が式の中に入っているとき、外側の式の別名を補う。取れなければ空文字(別名なしを None と区別して単位の取り違えを防ぐ)
            i, call_end = _find_nows(flat, call)  # 空白の違い(改行・ORDER BY・CASE)を無視して集計の位置を取る(B1)
            alias, depth = "", 0
            for k in range(call_end, len(flat)) if i >= 0 and not scope.startswith("subquery") else ():
                depth += (flat[k] == "(") - (flat[k] == ")")
                if depth < 0 and flat[k] == ")":  # 外側の式を閉じる括弧の直後に AS があれば、それが式全体の別名
                    am = re.match(r"\s*(?:::\w+\s*)?AS\s+(\w+)", flat[k + 1:], re.IGNORECASE)
                    if am:
                        alias = am.group(1)
                        break
        g = gb(body)
        out.append({"func": a["func"], "arg": a["arg"], "alias": alias, "scope": scope, "group_by": g, "unit": ", ".join(g) or "全体"})
    return out


def _merge_aggs(per):
    """通りごとの集計の単位(_agg_units の戻り)を重ねる。全ての通りで同じ行はそのまま、一部の通りにしか無い行には when(その通りのラベル)を付ける。
    呼出元: extract()(db_operations の aggregate_units)。なぜ要るか: 分岐で GROUP BY が変わると count(*) の単位も通りで変わるため、通りごとに分ける。"""
    if len(per) == 1:
        return per[0][1]
    seen, out = [], []
    for _, rows in per:
        for r in rows:
            if r in seen:
                continue
            seen.append(r)
            where = [lab for lab, rs in per if r in rs]
            out.append(r if len(where) == len(per) else {**r, "when": " / ".join(where)})
    return out


def _error_origins(common):
    """common_errors の各 error に、code と message の出どころを足す(案 B1)。元の欄は変えない。
    code_from: literal(固定の文字列)/ exception_attr:<属性>(例外の属性。code_default が無いときの既定の文字列)/ expression
    message_from: literal / exception_message(str(e) 等、例外のメッセージそのもの)/ exception_type_and_message / expression
    なぜ要るか: 返す code が MSE0000x の採番でなく、ソースの文字列(validation_error 等)で、文言は例外のメッセージだと機械で示すため。
    呼出元: extract()(最上位 common_errors)。"""
    for ce in common:
        for e in ce["errors"]:
            try:
                cn = ast.parse(str(e["code"]), mode="eval").body
            except SyntaxError:
                cn = None
            lits = [n.value for n in ast.walk(cn) if isinstance(n, ast.Constant) and isinstance(n.value, str)] if cn else []
            attr = next((n.args[1].value for n in ast.walk(cn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "getattr"
                         and len(n.args) > 1 and isinstance(n.args[1], ast.Constant)), "") if cn else ""
            if not lits and re.fullmatch(r"\w+", str(e["code"])):  # _common_errors は定数の code を引用符なしで持つ(internal_error)。識別子だけなら固定の文字列
                lits = [str(e["code"])]
                cn = ast.Constant(value=lits[0])
            lits = [x for x in lits if x != attr]  # getattr(e, 'code', ...) の属性名 code は既定の文字列ではない
            e["code_default"] = lits[-1] if lits else ""
            e["code_from"] = "exception_attr:" + attr if attr else "literal" if isinstance(cn, ast.Constant) else "expression"
            msg = str(e["message"])
            e["message_from"] = ("exception_type_and_message" if re.fullmatch(r"f?['\"]\{type\(\w+\)\.__name__\}: \{\w+\}['\"]", msg)
                                 else "exception_message" if re.fullmatch(r"str\(\w+\)", msg)
                                 else "literal" if re.fullmatch(r"f?(['\"]).*\1", msg) and "{" not in msg else "expression")
    return common


def _callees(fn, mod, mods):
    """fn の本体が呼ぶ、backend_files 内の関数を (Module, FunctionDef) で返す(同モジュールの Name 呼出と module.func 呼出)。"""
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id in mod.funcs:
            yield mod, mod.funcs[f.id]
        elif isinstance(f, ast.Name) and f.id in mod.from_imports:
            # 入力検査の補助(_require_date 等)は backend_files に無い共通モジュールにあることが多い。
            # 同じフォルダの <module>.py があれば読んで辿る(ディスパッチ表の走査には加えない)
            mname, fname = mod.from_imports[f.id]
            if mname not in mods:
                p = mod.path.parent / f"{mname}.py"
                try:
                    mods[mname] = Module(p) if p.is_file() else None
                except (OSError, SyntaxError, UnicodeDecodeError):
                    mods[mname] = None
            m2 = mods.get(mname)
            if m2 and fname in m2.funcs:
                yield m2, m2.funcs[fname]
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            m2 = mods.get(mod.imports.get(f.value.id, ""))
            if m2 and f.attr in m2.funcs:
                yield m2, m2.funcs[f.attr]


def _closure(mod, fn, mods):
    """fn から辿れる関数の集合(自身を含む)。同じ関数は 1 度だけ。"""
    seen, todo, out = set(), [(mod, fn)], []
    while todo:
        m, f = todo.pop()
        if id(f) in seen:
            continue
        seen.add(id(f))
        out.append((m, f))
        todo.extend(_callees(f, m, mods))
    return out


def _returns(mod, fn, mods, seen=None):
    """fn の return する dict のキー。return が backend_files 内の関数の呼出なら、その return を辿る。"""
    seen = seen or set()
    if id(fn) in seen:
        return []
    seen.add(id(fn))
    keys = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Return) or n.value is None:
            continue
        v = n.value
        if isinstance(v, ast.Dict):
            keys += [(k.value, [_src(mod, k)]) for k in v.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        elif isinstance(v, ast.Call):
            for m2, f2 in _callees(ast.Module(body=[ast.Expr(v)], type_ignores=[]), mod, mods):
                keys += _returns(m2, f2, mods, seen)
    return keys


def _errors(fn, env, mod):
    """fn の raise X("msg") と _err(code, "msg", status) を {condition, status_code, message} にする。"""
    out = []
    for n in ast.walk(fn):
        call, status = None, ""
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call):
            call = n.exc
            # ValidationError は handler.dispatch が 400 を既定にする。他の例外の status は静的に決められないので空
            # AuthError(code, 文面, status) の形は文面と status を第2・第3引数から取る(D2)。他の形は従来どおり
            if len(call.args) >= 3 and isinstance(call.args[2], ast.Constant) and isinstance(call.args[2].value, int):
                status, msg = call.args[2].value, call.args[1]
            else:
                status = 400 if getattr(call.func, "id", "").endswith("ValidationError") else ""
                msg = call.args[0] if call.args else None
        elif isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_err" and len(n.args) >= 2:
            msg = n.args[1]
            st = n.args[2] if len(n.args) > 2 else next((k.value for k in n.keywords if k.arg == "status"), None)
            status = st.value if isinstance(st, ast.Constant) else 400
        elif isinstance(n, ast.Call) and getattr(n.func, "id", "") == "error_envelope" and len(n.args) >= 3:
            msg = n.args[2]  # error_envelope(status, code, message)。status が定数でなければ空のまま
            status = n.args[0].value if isinstance(n.args[0], ast.Constant) else ""
        else:
            continue
        text = _render(msg, env, False) if msg is not None else None
        if text is not None:
            e = {"condition": "", "status_code": status, "message": text, "src": [_src(mod, n)]}
            # F2: raise を囲む if の条件(拒否する条件)。boundaries の材料。囲む if が無ければ付けない
            ifn = _enclosing_if(fn, n) if isinstance(n, ast.Raise) else None
            if ifn is not None:
                e["check"] = ast.unparse(ifn.test)
            out.append(e)
    return out


_SQLISH = re.compile(r"\b(AND|OR|IS|NOT|NULL|CASE|WHEN|SUM|COUNT|MIN|MAX|BETWEEN)\b|[=<>]|\w\.\w", re.IGNORECASE)


def _logic_candidates(mods):
    """SQL に埋め込まれるモジュール定数(条件・列の並び・グループ表など)を共通ロジックの候補として返す(F4)。
    意味合い: 業務の規則(例: 取込欠損の条件)が SQL の中にあることを、S1 が runs_on(動作場所)を決める手掛かりとして見えるようにする。
    呼出元: extract(下書きの最上位 logic_candidates)。{name, kind(sql_condition=文字列 / sql_fragment=列・辞書), text, runs_on, src}。
    ponytail: 「関数の中で名前が使われる定数で、中身が SQL らしい(AND・IS・= ・t.col など)」ものを拾う。実際に SQL へ入ったかは見ない。"""
    out = []
    for mod in mods:
        for n in mod.tree.body:
            if not (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)):
                continue
            name = n.targets[0].id
            val = mod.consts.get(name)
            if val is None or not any(isinstance(x, ast.Name) and x.id == name for f in mod.funcs.values() for x in ast.walk(f)):
                continue
            # WHY: 辞書の値に set(例: 利用者の一覧)が入ると json.dumps が TypeError で止まる。set は並べ替えた list にして文字列化する
            text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, default=lambda o: sorted(o, key=str) if isinstance(o, (set, frozenset)) else str(o)) if isinstance(val, (list, dict)) else ""
            if text and _SQLISH.search(text):
                out.append({"name": name, "kind": "sql_condition" if isinstance(val, str) else "sql_fragment", "text": text,
                            "runs_on": "データベース", "src": [_src(mod, n)]})
    return out


def extract(files, hints=None):
    """backend ファイル(Path の並び)から {apis, unresolved} を作る。
    hints は論理名の候補の材料 {ddl: _ddl(), ui: _ui_specs(), types: _type_blocks()}。無ければ guess だけになる。"""
    hints = hints or {}
    mods = {p.stem: Module(p) for p in files}
    # csv_header: backend の定数の辞書、または (列名, 日本語の見出し) の組の並びから、列名→見出しを取る。位置だけで対応する配列は推測しない
    csv = {}
    for mod in mods.values():
        for val in mod.consts.values():
            if isinstance(val, dict):
                pairs = list(val.items())
            elif isinstance(val, (list, tuple)):
                pairs = [(x[0], x[1]) for x in val if isinstance(x, (list, tuple)) and len(x) == 2]
            else:
                continue
            for k, v in pairs:
                if isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[A-Za-z_]\w*", k) and not v.isascii():
                    csv.setdefault(k, v)
    hints = {**hints, "csv": csv, "doc": _doc_hints(files)}
    apis, unresolved, tables = {}, [], set()
    for mod in list(mods.values()):  # _callees が補助のモジュールを mods へ足すので、走査は最初の backend_files だけ
        for n in mod.tree.body:
            if not (isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict) and n.value.keys):
                continue
            tables.add(ast.unparse(n.targets[0]))
            for k, v in zip(n.value.keys, n.value.values):
                # ディスパッチ表 = 「文字列 → module.func / func」の dict。他の dict(文言表など)はここで弾かれる
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    continue
                if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name):
                    tm, name = mods.get(mod.imports.get(v.value.id, "")), v.attr
                elif isinstance(v, ast.Name):
                    tm, name = mod, v.id
                else:
                    continue
                if tm is None or name not in tm.funcs:
                    continue  # backend_files に無いモジュール(または関数でない値)は対象外
                action, fn = k.value, tm.funcs[name]
                dataset, errors, seen_col, ops = [], [], set(), []
                clauses = {k: [] for k in _CLAUSE_KEYS}
                clo = _closure(tm, fn, mods)
                for m2, f2 in clo:
                    lists = _local_lists(f2, m2.env())  # 条件付きの append は断片ごとの when を持つ(B1)
                    cond_map = _cond_map(lists)
                    scens = _scenarios(f2, {**m2.env(), **{k: [t for t, _ in v] for k, v in lists.items()}})
                    env = scens[0][1]  # 句以外(列・出典・エラー)は最初の通りで組む。句だけ全部の通りで取る
                    parts = set()  # f 文字列・+ 連結の部品。親の _render が一括で組むので単独では拾わない
                    for s in ast.walk(f2):
                        if isinstance(s, (ast.JoinedStr, ast.BinOp)):
                            parts.update(id(c) for c in ast.walk(s) if c is not s)
                    for s in ast.walk(f2):
                        if id(s) in parts:
                            continue
                        sql = _render(s, env, True) if isinstance(s, (ast.JoinedStr, ast.Constant, ast.BinOp)) else None
                        if sql and _SQL_HEAD.match(sql):
                            _merge_scenarios(clauses, [_mark_conditional(_clauses(_render(s, e, True)), cond_map) for _, e in scens], [l for l, _ in scens])
                            frm, cols = _columns(sql, hints.get("ddl") or {}, unresolved, action)
                            feats = _features(sql, _mask(sql))
                            ctes, subs = _subqueries(sql, cond_map)
                            # 集計の単位は通りごとに出して重ねる(分岐で GROUP BY が変わると単位も変わる。最初の通りだけでは取り違える)
                            per_aggs = []
                            for lab, e2 in scens:
                                q = _render(s, e2, True)
                                per_aggs.append((lab, _agg_units(q, _features(q, _mask(q)))))
                            ops.append({"from": frm, "columns": _candidates(cols, hints), "features": feats, "ctes": ctes, "subqueries": subs,
                                        "aggregate_units": _merge_aggs(per_aggs), "table_aliases": _table_aliases(sql), "src": [_src(m2, s)]})
                            for r in _dataset(sql, unresolved, action):
                                if (r["column"], r["table"]) not in seen_col:
                                    seen_col.add((r["column"], r["table"]))
                                    dataset.append(r)
                    for e in _expand(_errors(f2, env, m2), f2, clo):
                        same = [x for x in errors if (x["message"], x["status_code"], x["text_expanded"]) == (e["message"], e["status_code"], e["text_expanded"])]
                        # 同じ文面・status・展開後の文は1件にまとめ、出典(src)だけ足す(同じ文が別の行にあっても行を落とさない)
                        if same:
                            same[0]["src"] += [s for s in e["src"] if s not in same[0]["src"]]
                        if not same:
                            errors.append(e)
                keys = {}  # 同じキーは最初の return の出典を持つ(キーの並びは return の出現順)
                for key, s in _returns(tm, fn, mods):
                    keys.setdefault(key, s)
                if not keys:
                    unresolved.append({"action": action, "reason": "return の dict キーを静的に取れない"})
                if not dataset:
                    unresolved.append({"action": action, "reason": "SELECT 文を取れない(SQL を持たないか、動的に組んでいる)"})
                for i, op in enumerate(ops, 1):  # id は H2 の db_operations と同じ付け方(複数なら実行順に _<n>)
                    op["id"] = f"db_op_{action}" + (f"_{i}" if len(ops) > 1 else "")
                    for no, c in enumerate(op["columns"], 1):
                        c["no"] = no
                apis[action] = {"dataset": dataset, "src": [_src(tm, fn)],
                                "response_items": [{"key": key, "src": s} for key, s in keys.items()],
                                "error_patterns": errors, "boundaries": _boundaries(errors, clo, fn), **clauses,
                                "features": _merge_features([o["features"] for o in ops]),
                                "db_operations": [{"id": o["id"], "from": o["from"], "columns": o["columns"], "features": o["features"], "ctes": o["ctes"], "subqueries": o["subqueries"], "table_aliases": o["table_aliases"],
                                                     "aggregate_units": o["aggregate_units"], "src": o["src"]} for o in ops]}
    # B3: 応答項目の型(api.ts)。action ごとに response_fields を足す(型が取れない API は空)
    rf = _response_fields(hints.get("ts_files") or [])
    for action, a in apis.items():
        a["response_fields"] = rf.get(action, [])
        # index signature([col: string]: …)の行は欄名が api.ts に無いので、SQL の列(別名)ごとに型の手掛かり(sql_hint・db_type・db_nullable)つきの欄を足す。
        # 型が取れない列・api.ts に欄名がある列は足さない。照合は verify_vs_source.check_types(db_type で見る)
        for idx in [f for f in a["response_fields"] if f.get("index_signature")]:
            have = {f["path"] for f in a["response_fields"]}
            for o in a["db_operations"]:
                for c in o["columns"]:
                    path, (typ, nul) = idx["path"][:-3] + "." + c["alias"], _sql_type(c, hints.get("ddl_types") or {})
                    if typ and path not in have:
                        have.add(path)
                        a["response_fields"].append({**idx, "path": path, "sql_hint": c["expression"], "db_type": typ, "db_nullable": nul})
    auth = _auth(mods, tables)
    ids = {}  # メッセージ ID は下書き全体で機械採番する。同じ文面は同じ ID(別の文面に同じ ID を付けない)
    for e in [e for a in apis.values() for e in a["error_patterns"]] + [e for a in auth for e in a["error_patterns"]]:
        e["id"] = ids.setdefault(e.get("text_expanded") or e["message"], f"MSE{len(ids) + 1:05d}")
    return {"apis": apis, "unresolved": unresolved, "auth": auth, "logic_candidates": _logic_candidates([x for x in mods.values() if x is not None]),
            "common_errors": _error_origins(_common_errors([x for x in mods.values() if x is not None], tables))}


def selftest():
    """最小の 2 ファイルで、ディスパッチ解決・別名/CTE/テーブル・return キー・raise を assert する。"""
    import tempfile
    h = ('import q\nfrom common import identify\nA = {"get_x": q.get_x, "get_y": q.get_y}\n'
         'def _entry(p):\n    identify(p)\n    return A[p]\n')
    qs = (
        'from common import _req\n'  # backend_files に無い同じフォルダの補助モジュールの raise も拾う
        'COLS = ["a", "b"]\nCOND = "t.b IS NULL"\n'
        'def _sel():\n    return ", ".join(f"SUM(t.{c}) AS {c}" for c in COLS)\n'
        'def get_x(p):\n'
        '    if not p:\n        raise ValidationError("必須です")\n'
        '    _req(p)\n'
        '    w = ["t.a = %(a)s"]\n    w.append(COND)\n    shop = p.get("shop")\n    if shop:\n        w.append("t.c = %(shop)s")\n'  # CTE の中で組む WHERE も where に出す(局所の並び・モジュール定数を展開)
        '    sql = f"""WITH d AS (SELECT t.* FROM real_tbl t WHERE {\' AND \'.join(w)}) SELECT t.id AS x_id, {_sel()}, s.name FROM d t LEFT JOIN shops s ON s.id=t.id"""\n'
        '    return {"rows": sql, "n": 1}\n'
        'def get_y(p):\n    _req("日付")\n'  # CTE の連鎖(t → k → 実表)は実表へ、FROM が動的な CTE は unresolved へ
        '    sql = f"""WITH k AS (SELECT id FROM real_tbl), t AS (SELECT k.id FROM k), u AS (SELECT 1 AS z FROM {p()})\n'
        '    SELECT t.id AS y_id, u.z AS z FROM t JOIN u ON 1=1"""\n'
        '    return {"rows": sql}\n'
    )
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "h.py").write_text(h, encoding="utf-8")
        (Path(d) / "q.py").write_text(qs, encoding="utf-8")
        (Path(d) / "common.py").write_text('def _req(p):\n    raise ValidationError(f"{p} は必須")\ndef identify(p):\n    _err("E1", "認証に失敗", 401)\n', encoding="utf-8")
        (Path(d) / "s.sql").write_text("CREATE TABLE real_tbl (\n  id integer, -- 識別番号\n  a integer,\n  PRIMARY KEY (id)\n);\n"
                                       "ALTER TABLE real_tbl ADD COLUMN b integer;\nCOMMENT ON COLUMN public.real_tbl.a IS '金額A';\n", encoding="utf-8")
        (Path(d) / "V.tsx").write_text('const ROWS: X[] = [\n  { label: "名前", k: "name" },\n  { label: "対象外", k: "zzz" },\n];\n', encoding="utf-8")
        (Path(d) / "api.ts").write_text("export interface R {\n  x_id: number;\n  /** 合計B（参考値） */\n  b: number;\n  name: string;\n}\n", encoding="utf-8")
        hints = {"ddl": _ddl(d), "ddl_types": _ddl_types(d), "ui": _ui_specs([Path(d) / "V.tsx"]), "types": _type_blocks([Path(d) / "api.ts"]),
                 "ts_files": [Path(d) / "api.ts"]}
        (Path(d) / "api.ts").write_text("export interface R {\n  x_id: number;\n  /** 合計B */\n  b: number | null;\n  name: string;\n  [col: string]: number | string | null;\n}\n"
                                        "export function g() {\n  return call<{\n    rows: R[];\n    n: number;\n    tags: string[];\n  }>(\"get_x\", {});\n}\n", encoding="utf-8")
        r = extract([Path(d) / "h.py", Path(d) / "q.py"], hints)
    # 骨組み: 最上位 SELECT の全列(別名・元の式・元の表と列)と FROM の行。論理名の候補は出所つき
    op = r["apis"]["get_x"]["db_operations"][0]
    assert op["id"] == "db_op_get_x" and op["from"] == {"table": "real_tbl", "name": "d", "alias": "t"}, op["from"]
    got = [(c["no"], c["alias"], c["table"], c["column"], c["logical_name"], c["logical_name_source"]) for c in op["columns"]]
    assert got == [(1, "x_id", "real_tbl", "id", "識別番号", "comment"), (2, "a", "real_tbl", "a", "金額A", "comment"),
                   (3, "b", "real_tbl", "b", "合計Bの合計", "expr"), (4, "name", "shops", "name", "名前", "ui_label")], got
    assert op["columns"][1]["expression"] == "SUM(t.a)", op["columns"][1]
    assert [c["alias"] for c in _columns("SELECT r.* FROM real_tbl r", hints["ddl"], [], "t")[1]] == ["id", "a", "b"], "SELECT * を DDL で展開しない"
    assert _guess("shop_id") == "店舗ID" and _guess("sou_uriage") == "", "列名からの推定"
    a = r["apis"]["get_x"]
    cols = {(x["column"], x["table"]) for x in a["dataset"]}
    assert {("id", "real_tbl"), ("a", "real_tbl"), ("b", "real_tbl"), ("name", "shops")} <= cols, cols
    assert [x["key"] for x in a["response_items"]] == ["rows", "n"], a["response_items"]
    assert {e["message"] for e in a["error_patterns"]} == {"必須です", "{p} は必須"}, a["error_patterns"]
    # B3: api.ts の型。| null は nullable、interface の欄は rows[].x に展開、index signature は別持ち、型の無い API は空
    rf = {f["path"]: f for f in a["response_fields"]}
    assert rf["rows[].b"]["nullable"] is True and rf["rows[].b"]["ts_base"] == "number", rf
    assert rf["rows[].name"]["nullable"] is False and rf["tags"]["ts_base"] == "string[]" and rf["n"]["ts_base"] == "number", rf
    assert rf["rows[][*]"]["index_signature"] and rf["rows"]["ts_base"] == "R[]" and r["apis"]["get_y"]["response_fields"] == [], rf
    # F4: SQL に入る定数(COND)は共通ロジックの候補。中身が SQL らしくない定数(COLS)は出さない
    assert [(c["name"], c["kind"], c["text"], c["runs_on"]) for c in r["logic_candidates"]] == [("COND", "sql_condition", "t.b IS NULL", "データベース")], r["logic_candidates"]
    # B3 SQL の型: DDL の型と NOT NULL、::型・NULL::型・COUNT の手掛かり、取れない式(無印の SUM)は None
    dt = hints["ddl_types"]
    assert dt["real_tbl"]["id"] == ("integer", False) and dt["real_tbl"]["b"] == ("integer", False), dt
    st = lambda e, t="real_tbl", c="": _sql_type({"expression": e, "table": t, "column": c}, dt)
    assert st("t.id", c="id") == ("integer", True) and st("SUM(t.a)::bigint") == ("bigint", False) and st("NULL::varchar") == ("varchar", True)
    assert st("count(*)") == ("bigint", False) and st("SUM(t.a)", c="a") == (None, None), "SQL の型の手掛かり"
    assert "common" not in r["apis"] and set(r["apis"]) == {"get_x", "get_y"}, "補助モジュールを API として数えた"
    ycols = {(x["column"], x["table"]) for x in r["apis"]["get_y"]["dataset"]}
    assert ycols == {("id", "real_tbl")}, ycols
    gx = r["apis"]["get_x"]
    assert {"type": "LEFT JOIN", "logical_table": "shops", "condition": "s.id=t.id"} in gx["joins"], gx["joins"]
    assert {"alias": "a", "expression": "SUM(t.a)"} in gx["aliases"], gx["aliases"]
    assert [(w["logical_column"], w["operator"], w.get("when")) for w in gx["where"]] == [("a", "=", None), ("b", "IS", None), ("c", "=", "shop が指定されたとき")], gx["where"]
    # B1: 条件付きの断片は CTE の定義から外して conditional_parts に持つ。表の別名は実表へ解ける
    cte = gx["db_operations"][0]["ctes"][0]
    assert cte["conditional_parts"] == [{"sql": "t.c = %(shop)s", "when": "shop が指定されたとき"}] and cte["definition"] == "SELECT t.* FROM real_tbl t WHERE t.a = %(a)s AND t.b IS NULL", cte
    assert gx["db_operations"][0]["table_aliases"]["t"] == "real_tbl" and gx["db_operations"][0]["table_aliases"]["s"] == "shops", gx["db_operations"][0]["table_aliases"]
    assert _when_text(ast.parse("not shop", mode="eval").body) == "shop が未指定のとき" and _split_conditional("SELECT 1 WHERE x = 1", {"x = 1": "w"}) == ("SELECT 1", [{"sql": "x = 1", "when": "w"}])
    # B1: 改行・ORDER BY・CASE を含む array_agg でも、式の別名(warn_dates)を取る
    nl = "SELECT s.name, array_remove(array_agg(\n  CASE WHEN p.k > 0 THEN p.d END\n  ORDER BY p.d\n), NULL) AS warn_dates FROM p JOIN s ON s.id = p.m GROUP BY s.name"
    nu = _agg_units(nl, _features(nl, _mask(nl)))
    assert [(u["alias"], u["func"]) for u in nu if u["func"] == "array_agg"] == [("warn_dates", "array_agg")], nu
    # 集計・ウィンドウ(C4): FILTER と OVER と ::型 と CTE の範囲を、本体と CTE の両方で取る
    fsql = "WITH d AS (SELECT count(*) FILTER (WHERE t.x <> 0) OVER (PARTITION BY t.m, t.r) AS nz FROM real_tbl t) SELECT SUM(d.nz)::bigint AS total FROM d"
    ff = _features(fsql, _mask(fsql))
    assert ff["windows"] == [{"func": "count", "arg": "*", "filter": "t.x <> 0", "alias": "nz", "scope": "cte d", "cast": None,
                              "over": {"partition_by": ["t.m", "t.r"], "order_by": []}}], ff["windows"]
    assert ff["aggregates"] == [{"func": "SUM", "arg": "d.nz", "filter": None, "alias": "total", "scope": "main", "cast": "bigint"}], ff["aggregates"]
    assert {"func": "SUM", "arg": "t.a", "filter": None, "alias": "a", "scope": "main", "cast": None} in gx["features"]["aggregates"], gx["features"]
    # 分岐で変わる GROUP BY / ORDER BY(C4): 辞書[引数] と IfExp を通りごとに展開し、一部の通りにしか無い行に when を付ける
    sfn = ast.parse("def f(group):\n    keys = G[group]\n    rc = 'p.r' if group == 'reg' else 'NULL'\n").body[0]
    sc = _scenarios(sfn, {"G": {"reg": ["p.m", "p.r"], "shop": ["p.m"]}})
    assert [l for l, _ in sc] == ["group = reg", "group = shop"] and sc[1][1]["keys"] == ["p.m"] and sc[1][1]["rc"] == "NULL", sc
    acc = {k: [] for k in _CLAUSE_KEYS}
    _merge_scenarios(acc, [_clauses("SELECT 1 FROM real_tbl p GROUP BY " + ", ".join(e["keys"])) for _, e in sc], [l for l, _ in sc])
    assert [(g["logical_column"], g.get("when")) for g in acc["group_by"]] == [("m", None), ("r", "group = reg")], acc["group_by"]
    assert [e["status_code"] for e in r["auth"][0]["error_patterns"]] == [401], r["auth"]
    assert r["auth"][0]["function"] == "identify", r["auth"]
    assert not any(n in r["auth"][0][k] for n in ("identify", "lambda_handler", "h.py") for k in ("fact", "applies_to")), r["auth"]  # 物理名は function の欄にだけ
    assert any(e["text_expanded"] == "日付 は必須" and e["message"] == "{p} は必須" for e in r["apis"]["get_y"]["error_patterns"]), r["apis"]["get_y"]["error_patterns"]
    ids = [e["id"] for a in r["apis"].values() for e in a["error_patterns"]] + [e["id"] for a in r["auth"] for e in a["error_patterns"]]
    assert ids and len(ids) == len(set(ids)) and all(i.startswith("MSE") for i in ids), ids
    # expr は元列の論理名から式の種類で組む(MIN は最初の・SUM は合計)。csv_header は見出しの辞書から取る。式の別名はコメントを借りない
    cs = _candidates([{"alias": "f", "expression": "MIN(t.kai)", "table": "real_tbl", "column": "kai", "logical_name": ""},
                      {"alias": "g", "expression": "t.zz", "table": "real_tbl", "column": "zz"},
                      {"alias": "h", "expression": "CASE WHEN t.a THEN 1 END", "table": "real_tbl", "column": "a"}],
                     {"ddl": {"real_tbl": {"kai": "会計日"}}, "csv": {"zz": "店舗コード"}})
    assert [(c["logical_name"], c["logical_name_source"]) for c in cs] == [("最初の会計日", "expr"), ("店舗コード", "csv_header"), ("", "")], cs
    # csv: があれば label より優先する(同じ label「小計」が重なっても列名の数字を付けず、csv の見出しのまま)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "W.tsx").write_text('const R = [\n  { label: "小計", csv: "小計1(値引前)", kin: "s_1" },\n'
                                       '  { label: "小計", csv: "小計5(現金計)", kin: "s_5" },\n  { label: "総売上", kin: "u" },\n];\n', encoding="utf-8")
        ui = _ui_specs([Path(d) / "W.tsx"])
    assert ui == [("小計1(値引前)", ["s_1"], {"s_1": "kin"}), ("小計5(現金計)", ["s_5"], {"s_5": "kin"}), ("総売上", ["u"], {"u": "kin"})], ui
    # T7: 論理名の出所 docstring(backend の docstring・# の「日本語(識別子)」。同じ識別子に別の語が付けば取らない)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "m.py").write_text('"""店番号(mise_no) の表。"""\n# レジ番(reji_no) を見る\n# 支店番号(mise_no) も見る\n', encoding="utf-8")
        dh = _doc_hints([Path(d) / "m.py"])
    assert dh == {"reji_no": "レジ番"}, dh
    dcols = [{"alias": "shop_id", "expression": "p.mise_no", "table": "real_tbl", "column": "mise_no", "logical_name": ""}]
    dc = _candidates(dcols, {"doc": {"mise_no": "店番号"}})
    assert (dc[0]["logical_name"], dc[0]["logical_name_source"]) == ("店番号", "docstring"), dc
    dc = _candidates(dcols, {"doc": {"mise_no": "店番号"}, "ddl": {"real_tbl": {"mise_no": "店舗番号"}}})
    assert (dc[0]["logical_name"], dc[0]["logical_name_source"]) == ("店舗番号", "comment"), "DDL のコメントが docstring より先"
    # T7: ui_label の末尾の語は型のコメントの「点数側の列」から取る(取れなければ (推定) と ui_label_guess)
    scols = [{"alias": a, "expression": a, "table": "", "column": a} for a in ("uri_teisei_kin", "uri_teisei_cnt")]
    sfx = [("売上訂正", ["uri_teisei_kin", "uri_teisei_cnt"], {"uri_teisei_kin": "kin", "uri_teisei_cnt": "cnt"})]
    types = [({"label", "kin", "cnt"}, {"kin": "金額側の列。派生値は calc で与える", "cnt": "点数側の列（無い行は金額のみ）"})]  # 型の欄(属性)に付いたコメント
    sc = _candidates(scols, {"ui": sfx, "types": types})
    assert [(c["logical_name"], c["logical_name_source"]) for c in sc] == [("売上訂正金額", "ui_label"), ("売上訂正点数", "ui_label")], sc
    gs = _candidates(scols, {"ui": [("売上訂正", ["uri_teisei_kin", "uri_teisei_cnt"], {"uri_teisei_kin": "kin", "uri_teisei_cnt": "cnt"})]})
    assert [(c["logical_name"], c["logical_name_source"]) for c in gs] == [("売上訂正金額(推定)", "ui_label_guess"), ("売上訂正件数(推定)", "ui_label_guess")], gs
    cs = _candidates([{"alias": a, "expression": a, "table": "", "column": a} for a in ("s_1", "s_5", "u")], {"ui": ui})
    assert [(c["logical_name"], c["logical_name_source"]) for c in cs] == [("小計1(値引前)", "ui_label"), ("小計5(現金計)", "ui_label"), ("総売上", "ui_label")], cs
    # T18: 型のコメントが「AとB」(2列をまとめた説明)のときは式の種類で分ける。count は A(数で終わるとき)、array_agg は B の「の一覧」。分けられなければ空
    pc = [{"alias": "w_days", "expression": "count(*) FILTER (WHERE t.x > 0)::int", "table": "", "column": "", "logical_name": ""},
          {"alias": "w_dates", "expression": "array_remove(array_agg(t.d ORDER BY t.d), NULL)", "table": "", "column": "", "logical_name": ""},
          {"alias": "w_sum", "expression": "SUM(t.k)", "table": "", "column": "k", "logical_name": ""}]
    pnote = "取込欠損の疑いがある日数と日付（現行の取込バグ）"
    ptypes = [({"w_days", "w_dates", "w_sum"}, {"w_days": pnote, "w_dates": pnote, "w_sum": "金額と点数"})]
    pcs = _candidates(pc, {"types": ptypes})
    assert [(c["logical_name"], c["logical_name_source"]) for c in pcs] == [
        ("取込欠損の疑いがある日数", "api_type"), ("日付の一覧", "api_type"), ("", "")], pcs
    assert _pair_note("金額と点数", "SUM(t.k)") == "" and _pair_note("店舗名", "t.x") == "店舗名", "AとB の分け方"
    # F2: 日付の形式検査(from・to)の boundaries と、暦に無い日付を通すこと・大小比較が無いこと・共通エラー(except 節)
    src = ('import re\n_D = re.compile(r"^\\d{4}-\\d{2}-\\d{2}$")\nclass ValidationError(Exception):\n    pass\n'
           'def _req(p, k):\n    v = p.get(k)\n    if not isinstance(v, str) or not _D.match(v):\n        raise ValidationError(f"{k} は形式が違う")\n    return v\n'
           'def act(p):\n    a = _req(p, "from")\n    b = _req(p, "to")\n    return {"x": 1}\n'
           'T = {"act": act}\n'
           'def dispatch(body):\n    fn = T[body["a"]]\n    try:\n        return fn(body)\n    except ValidationError as e:\n        return _err("bad", str(e), status=400)\n'
           '    except Exception as e:\n        return _err("ng", "x", status=500)\n')
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "q.py").write_text(src, encoding="utf-8")
        r2 = extract([Path(d) / "q.py"])
    bs = r2["apis"]["act"]["boundaries"]
    assert [(b["param"], b.get("impossible_dates_accepted")) for b in bs if b.get("pattern")] == [("from", ["2026-02-30"]), ("to", ["2026-02-30"])], bs
    assert [b["check"] for b in bs if b.get("present") is False] == ["order", "span_limit"], bs
    assert [(c["exception"], [e["status_code"] for e in c["errors"]]) for c in r2["common_errors"]] == [("ValidationError", [400]), ("Exception", [500])], r2["common_errors"]
    # B1: 実列と別名の区別(s.name AS shop_name は実列 shops.name、count(*) AS days は実列なし)、CTE・副問合せ、集計の単位、例外の code の出どころ
    bsql = ("WITH d AS (SELECT p.m, p.r FROM tbl p WHERE p.k > 0), e AS (SELECT * FROM tbl) "
            "SELECT s.name AS shop_name, p.m, count(*) AS days, upper(s.name) AS nm, max(p.k) AS mk "
            "FROM d p JOIN shops s ON s.id = p.m WHERE p.m IN (SELECT m FROM tbl WHERE z = 1) AND p.r IN (SELECT r FROM tbl) GROUP BY p.m")
    _, bc = _columns(bsql, {}, [], "t")
    bk = {c["alias"]: (c["kind"], c["real_column"], c["alias_name"], c["source_columns"]) for c in bc}
    assert bk["shop_name"] == ("column", "shops.name", "shop_name", ["shops.name"]), bk
    assert bk["days"] == ("alias_only", "", "days", []) and bk["nm"] == ("alias_only", "", "nm", ["shops.name"]), bk
    assert bk["m"][0] == "column" and bk["m"][2] == "", bk
    bcte, bsub = _subqueries(bsql)
    assert [(c["name"], c["filtered"]) for c in bcte] == [("d", True), ("e", False)] and bcte[0]["from"] == "tbl", bcte
    assert [(s["location"], s["filtered"]) for s in bsub] == [("WHERE", True), ("WHERE", False)], bsub
    bu = {u["alias"]: u["group_by"] for u in _agg_units(bsql, _features(bsql, _mask(bsql)))}
    assert bu == {"days": ["tbl.m"], "mk": ["tbl.m"]}, bu
    n_sql = "SELECT count(*) AS n FROM tbl"
    bu2 = _agg_units(n_sql, _features(n_sql, n_sql))
    assert bu2[0]["unit"] == "全体" and bu2[0]["group_by"] == [], bu2
    mg = _merge_aggs([("group = register", _agg_units(bsql, _features(bsql, _mask(bsql)))), ("group = shop", bu2)])
    assert [(x["alias"], x.get("when")) for x in mg] == [("days", "group = register"), ("mk", "group = register"), ("n", "group = shop")], mg
    # 副問合せの中の集計は外側の GROUP BY を継がず(scope=subquery、単位は全体)、式に入った集計は外側の別名を補う
    qsql = ("SELECT s.name, array_remove(array_agg(p.d), NULL) AS warn_dates FROM p JOIN s ON s.id = p.m "
            "WHERE p.d = (SELECT max(d) FROM p) GROUP BY s.name")
    qu = _agg_units(qsql, _features(qsql, _mask(qsql)))
    assert [(u["func"], u["alias"], u["scope"], u["unit"]) for u in qu] == [
        ("array_agg", "warn_dates", "main", "s.name"), ("max", "", "subquery WHERE", "全体")], qu
    ce = _error_origins([{"errors": [{"code": "getattr(e, 'code', None) or 'validation_error'", "message": "str(e)"},
                                      {"code": "internal_error", "message": "f'{type(e).__name__}: {e}'"}]}])[0]["errors"]
    assert [(x["code_from"], x["code_default"], x["message_from"]) for x in ce] == [
        ("exception_attr:code", "validation_error", "exception_message"), ("literal", "internal_error", "exception_type_and_message")], ce
    print("[selftest OK] extract_backend_draft")
    return 0


def main(argv=None):
    """CLI の入口。入力を検め、解析 → 書出しを行い、終了コード 0/1/2 を返す。"""
    p = argparse.ArgumentParser(description="backend(Python) から API ごとの dataset / response_items / error_patterns の下書き JSON を作る")
    p.add_argument("--screen-config", help="screen_config.json のパス")
    p.add_argument("--out", help="出力する下書き JSON のパス")
    p.add_argument("--selftest", action="store_true", help="組込みの検査を走らせる(--screen-config/--out は不要)")
    args = p.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.screen_config or not args.out:
        print("[入力不正] --screen-config と --out が必要", file=sys.stderr)
        return 2
    try:
        cfg = json.loads(Path(args.screen_config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[入力不正] {args.screen_config}: {e}", file=sys.stderr)
        return 2
    # backend_files の展開は source_files と同じ取り決め(expandvars → expanduser)。相対は project_root 基準
    root = Path(os.path.expanduser(os.path.expandvars(cfg.get("project_root", "") or ".")))
    files = []
    for s in cfg.get("backend_files", []):
        if s:
            q = Path(os.path.expanduser(os.path.expandvars(s)))
            files.append(q if q.is_absolute() else root / q)
    missing = [str(f) for f in files if not f.is_file()]
    if not files or missing:
        print(f"[入力不正] backend_files が空か、無いファイルがある: {missing}", file=sys.stderr)
        return 2
    srcs = []
    for s in cfg.get("source_files", []):
        if s:
            q = Path(os.path.expanduser(os.path.expandvars(s)))
            srcs.append(q if q.is_absolute() else root / q)
    hints = {"ddl": _ddl(root), "ddl_types": _ddl_types(root), "ui": _ui_specs(srcs), "types": _type_blocks(srcs),
             "ts_files": [p for p in srcs if p.suffix in (".ts", ".tsx") and p.is_file()]}
    try:
        draft = extract(files, hints)
    except SyntaxError as e:
        print(f"[入力不正] 構文エラー: {e}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cols = [c for a in draft["apis"].values() for o in a["db_operations"] for c in o["columns"]]
    by_src = {}
    for c in cols:
        by_src[c["logical_name_source"] or "none"] = by_src.get(c["logical_name_source"] or "none", 0) + 1
    print(f"[OK] apis={len(draft['apis'])} columns={len(cols)} {by_src} unresolved={len(draft['unresolved'])} -> {args.out}")
    return 1 if draft["unresolved"] else 0


if __name__ == "__main__":
    sys.exit(main())
