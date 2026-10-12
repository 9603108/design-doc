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

【意味合い】
Phase 1 の backend 節は LLM の読解で列・キー・エラーを書いていたため、実行ごとに欠落とぶれが出た。
列名・テーブル名・キー・メッセージ(事実)を機械で出し、LLM は業務の文面だけを書く分担にする(extract_react_draft.py と同じ考え方)。

【入出力】
  入力: --screen-config <screen_config.json>(backend_files / project_root を読む。相対の backend_files は project_root 基準)
  出力: --out <JSON>。キーは { apis: {action: {dataset, response_items, error_patterns}}, unresolved }
  終了コード: 0=未解決なし / 1=unresolved が1件以上(下書きは書く) / 2=入力不正
  backend_files に無いモジュールを指す action は対象外(出力に入れず、unresolved にも出さない)

【接続情報】
  - 出力は extract_react_draft.py の db_operations の action 名と突き合わせる前提(突合は後段)
  例: python3 target/scripts/extract_backend_draft.py --screen-config target/intermediate/<screen>_screen_config.json \
        --out target/intermediate/<screen>_backend_draft.json
"""
import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

# FROM / JOIN の直後の別名として読んではいけない語
_KW = {"WHERE", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "JOIN", "ON", "GROUP", "ORDER", "LIMIT",
       "UNION", "USING", "SET", "HAVING", "WINDOW", "OFFSET", "FOR", "RETURNING", "AS", "WITH"}
_SQL_HEAD = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(WITH|SELECT)\b", re.IGNORECASE)
_FROM_TBL = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)(?:\s+(?:AS\s+)?(?!(?:" + "|".join(_KW) + r")\b)([A-Za-z_]\w*))?", re.IGNORECASE)  # キーワードは別名に食わせない(FROM t JOIN u の JOIN を落とさない)
_CTE = re.compile(r"\b([A-Za-z_]\w*)\s+AS\s*\(", re.IGNORECASE)


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


def _dataset(sql, unresolved, action):
    """SQL 1 本から [{column, table}] を作る。最上位 SELECT だけを見る(WITH の中身・副問合せの列は含めない)。"""
    m = _mask(sql)
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
            keys += [k.value for k in v.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        elif isinstance(v, ast.Call):
            for m2, f2 in _callees(ast.Module(body=[ast.Expr(v)], type_ignores=[]), mod, mods):
                keys += _returns(m2, f2, mods, seen)
    return keys


def _errors(fn, env):
    """fn の raise X("msg") と _err(code, "msg", status) を {condition, status_code, message} にする。"""
    out = []
    for n in ast.walk(fn):
        call, status = None, ""
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call):
            call = n.exc
            # ValidationError は handler.dispatch が 400 を既定にする。他の例外の status は静的に決められないので空
            status = 400 if getattr(call.func, "id", "").endswith("ValidationError") else ""
            msg = call.args[0] if call.args else None
        elif isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_err" and len(n.args) >= 2:
            msg = n.args[1]
            st = n.args[2] if len(n.args) > 2 else next((k.value for k in n.keywords if k.arg == "status"), None)
            status = st.value if isinstance(st, ast.Constant) else 400
        else:
            continue
        text = _render(msg, env, False) if msg is not None else None
        if text is not None:
            out.append({"condition": "", "status_code": status, "message": text})
    return out


def extract(files):
    """backend ファイル(Path の並び)から {apis, unresolved} を作る。"""
    mods = {p.stem: Module(p) for p in files}
    apis, unresolved = {}, []
    for mod in list(mods.values()):  # _callees が補助のモジュールを mods へ足すので、走査は最初の backend_files だけ
        for n in mod.tree.body:
            if not (isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict) and n.value.keys):
                continue
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
                dataset, errors, seen_col = [], [], set()
                for m2, f2 in _closure(tm, fn, mods):
                    env = m2.env()
                    parts = set()  # f 文字列・+ 連結の部品。親の _render が一括で組むので単独では拾わない
                    for s in ast.walk(f2):
                        if isinstance(s, (ast.JoinedStr, ast.BinOp)):
                            parts.update(id(c) for c in ast.walk(s) if c is not s)
                    for s in ast.walk(f2):
                        if id(s) in parts:
                            continue
                        sql = _render(s, env, True) if isinstance(s, (ast.JoinedStr, ast.Constant, ast.BinOp)) else None
                        if sql and _SQL_HEAD.match(sql):
                            for r in _dataset(sql, unresolved, action):
                                if (r["column"], r["table"]) not in seen_col:
                                    seen_col.add((r["column"], r["table"]))
                                    dataset.append(r)
                    errors += _errors(f2, env)
                keys = list(dict.fromkeys(_returns(tm, fn, mods)))
                if not keys:
                    unresolved.append({"action": action, "reason": "return の dict キーを静的に取れない"})
                if not dataset:
                    unresolved.append({"action": action, "reason": "SELECT 文を取れない(SQL を持たないか、動的に組んでいる)"})
                apis[action] = {"dataset": dataset, "response_items": [{"key": x} for x in keys],
                                "error_patterns": errors}
    return {"apis": apis, "unresolved": unresolved}


def selftest():
    """最小の 2 ファイルで、ディスパッチ解決・別名/CTE/テーブル・return キー・raise を assert する。"""
    import tempfile
    h = 'import q\nA = {"get_x": q.get_x, "get_y": q.get_y}\n'
    qs = (
        'from common import _req\n'  # backend_files に無い同じフォルダの補助モジュールの raise も拾う
        'COLS = ["a", "b"]\n'
        'def _sel():\n    return ", ".join(f"SUM(t.{c}) AS {c}" for c in COLS)\n'
        'def get_x(p):\n'
        '    if not p:\n        raise ValidationError("必須です")\n'
        '    _req(p)\n'
        '    sql = f"""WITH d AS (SELECT t.* FROM real_tbl t) SELECT t.id AS x_id, {_sel()}, s.name FROM d t LEFT JOIN shops s ON s.id=t.id"""\n'
        '    return {"rows": sql, "n": 1}\n'
        'def get_y(p):\n'  # CTE の連鎖(t → k → 実表)は実表へ、FROM が動的な CTE は unresolved へ
        '    sql = f"""WITH k AS (SELECT id FROM real_tbl), t AS (SELECT k.id FROM k), u AS (SELECT 1 AS z FROM {p()})\n'
        '    SELECT t.id AS y_id, u.z AS z FROM t JOIN u ON 1=1"""\n'
        '    return {"rows": sql}\n'
    )
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "h.py").write_text(h, encoding="utf-8")
        (Path(d) / "q.py").write_text(qs, encoding="utf-8")
        (Path(d) / "common.py").write_text('def _req(p):\n    raise ValidationError(f"{p} は必須")\n', encoding="utf-8")
        r = extract([Path(d) / "h.py", Path(d) / "q.py"])
    a = r["apis"]["get_x"]
    cols = {(x["column"], x["table"]) for x in a["dataset"]}
    assert {("id", "real_tbl"), ("a", "real_tbl"), ("b", "real_tbl"), ("name", "shops")} <= cols, cols
    assert [x["key"] for x in a["response_items"]] == ["rows", "n"], a["response_items"]
    assert {e["message"] for e in a["error_patterns"]} == {"必須です", "{p} は必須"}, a["error_patterns"]
    assert "common" not in r["apis"] and set(r["apis"]) == {"get_x", "get_y"}, "補助モジュールを API として数えた"
    ycols = {(x["column"], x["table"]) for x in r["apis"]["get_y"]["dataset"]}
    assert ycols == {("id", "real_tbl")}, ycols
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
    try:
        draft = extract(files)
    except SyntaxError as e:
        print(f"[入力不正] 構文エラー: {e}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] apis={len(draft['apis'])} unresolved={len(draft['unresolved'])} -> {args.out}")
    return 1 if draft["unresolved"] else 0


if __name__ == "__main__":
    sys.exit(main())
