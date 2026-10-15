#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_vs_db.py: verify_vs_source の追加検査: 論理名・データ起源・ファイル一覧・日付表記の照合(stdlib のみ)

【受け持ち】
論理名・データ起源・ファイル一覧・日付表記。logical_names・db_operations の論理名、api_spec[].response_items[].source(データの起源)、
meta のファイル一覧(backend_files・frontend_files)、日付の書き方が、backend 下書き(columns・response_fields)や
ソースの事実と合うかを見る。d(_vs_common.load の戻り)の読み方は _vs_common の docstring。

【検査の足し方】
  1. check(d) -> list[str] の関数を書く。指摘が無ければ []。必要な JSON が d に無いときも [] を返す(何も言わない)。
  2. 行は _vs_common.warn(tag, where, msg) で作る(新しい検査は [WARN]。終了コードに影響しない)。
  3. 関数を CHECKS に足す。
  4. 検査の正例・負例を、引数なしで bool を返す selftest 用の関数に書き、SELFTESTS に足す(selftest() が全部呼ぶ)。

【接続情報】
  呼出元: _vs_checks_ext.run_all / selftest(verify_vs_source.main と --selftest から)
"""
from pathlib import Path

import _vs_common

# 検査の本体(check(d) -> list[str])。まだ空。後で1検査ずつ足す
CHECKS: list = []
# 検査の自己テスト(引数なし・bool を返す)。まだ空
SELFTESTS: list = []


def run(inter_dir, feature) -> list:
    """d = _vs_common.load(...) を各 check に渡し、指摘の行を連結して返す。呼出元: _vs_checks_ext.run_all。"""
    d = _vs_common.load(Path(inter_dir), feature)
    out = []
    for check in CHECKS:
        out += check(d)
    return out


def selftest() -> bool:
    """SELFTESTS の各関数を全部呼び、全部 True なら True。WHY: 1つ落ちても残りを呼び、どれが落ちたかを1回の実走で全部出せるようにする。"""
    results = [bool(t()) for t in SELFTESTS]
    return all(results)


def _top_level_names(tree) -> set:
    """モジュールの先頭で定義される名前(def・class・代入)。import は含めない。WHY: import 先が、画面の API の関数の使う名前を本当に定義しているかを判る材料にするため。"""
    import ast
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def check_backend_files(d) -> list:
    """画面の API の関数の本体が使う名前の定義元(同じフォルダの .py)が meta.backend_files に無いものを指摘する。
    WHY: 一覧に無いモジュールは設計書の対象外になり、画面の処理がその定義(検査・変換など)を追えなくなるため。
    ただし入口(handler など)が全画面ぶん import する大量のモジュールは、画面の API が使わないので指摘しない
    (extract_source_files._imported_shared_modules と同じ「API の関数が使う名前の定義元だけ・1段」の基準)。
    Where: 起点は d["main"]["api_spec"][*]["src"](関数の行範囲)。ソースは d["screen_config"]["project_root"] を展開して解く。
    差し戻し先は extract_source_files.py(一覧を作る段)。"""
    import ast
    import os
    import posixpath

    main = d.get("main") or {}
    root_raw = (d.get("screen_config") or {}).get("project_root", "")
    if not root_raw:
        return []
    root = Path(os.path.expanduser(os.path.expandvars(root_raw)))
    # WHY: 一覧の要素は {path, kind, description} の object だが、旧版は string のままなので両方受ける
    listed = set()
    for e in (main.get("meta") or {}).get("backend_files") or []:
        p = e.get("path") if isinstance(e, dict) else e
        if p:
            listed.add(posixpath.normpath(p))
    # 起点: 画面の API の関数の置き場(ファイル → 行範囲の並び)
    ranges: dict = {}
    for api in main.get("api_spec") or []:
        for s in api.get("src") or []:
            try:
                rel = posixpath.normpath(Path(s["file"]).resolve().relative_to(root.resolve()).as_posix())
                ranges.setdefault(rel, []).append((int(s["line_start"]), int(s["line_end"])))
            except (KeyError, TypeError, ValueError):
                continue
    out = []
    for rel, spans in sorted(ranges.items()):
        f = root / rel
        if not rel.endswith(".py") or not f.is_file():
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        in_span = lambda n: any(a <= getattr(n, "lineno", -1) <= b for a, b in spans)
        bare, dotted = set(), set()
        for node in ast.walk(tree):
            if not in_span(node):
                continue
            if isinstance(node, ast.Name):
                bare.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                dotted.add((node.value.id, node.attr))
        folder = posixpath.dirname(rel)
        reported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pairs = [(a.name, a.asname or a.name, None) for a in node.names if "." not in a.name]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level <= 1 and "." not in node.module:
                pairs = [(node.module, a.asname or a.name, a.name) for a in node.names]
            else:
                continue
            for mod, local, orig in pairs:
                target = posixpath.normpath(posixpath.join(folder, mod + ".py"))
                if target == rel or target in listed or target in reported or not (root / target).is_file():
                    continue
                try:
                    defined = _top_level_names(ast.parse((root / target).read_text(encoding="utf-8", errors="ignore")))
                except SyntaxError:
                    continue
                if orig is None:
                    used = any(x == local and t in defined for x, t in dotted)
                else:
                    used = local in bare and orig in defined
                if used:
                    reported.add(target)
                    out.append(_vs_common.warn(
                        "ファイル一覧", rel,
                        f"画面の API の関数が使う名前の定義元 {target} が meta.backend_files に無い。差し戻し先: extract_source_files.py"))
    return out


def _check_backend_files_case(files: dict, listed: list) -> list:
    """selftest 用: 一時フォルダに files を書き、listed を backend_files として check_backend_files を呼ぶ。
    画面の API の関数は h/q.py の 4〜5 行目(下の files の def api)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in files.items():
            fp = Path(tmp) / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(text, encoding="utf-8")
        d = {"main": {"meta": {"backend_files": [{"path": p, "kind": "backend", "description": ""} for p in listed]},
                      "api_spec": [{"action": "a", "src": [{"file": str(Path(tmp) / "h/q.py"), "line_start": 4, "line_end": 5}]}]},
             "screen_config": {"project_root": tmp}}
        return check_backend_files(d)


def selftest_check_backend_files() -> bool:
    """当たり: API の関数が helper.f を使い一覧に helper.py が無い → 1件。外れ: 一覧にある → 0件。
    外れ: 同じファイルが import しても API の関数が使わない unrelated.py・API の範囲外の関数が使う other.py → 0件。"""
    files = {"h/q.py": "import helper\nimport unrelated\nfrom other import g\n"
                       "def api():\n    return helper.f()\ndef x():\n    return g()\n",
             "h/helper.py": "def f():\n    return 1\n", "h/unrelated.py": "Y = 1\n", "h/other.py": "def g():\n    return 2\n"}
    hit = _check_backend_files_case(files, ["h/q.py"])
    miss = _check_backend_files_case(files, ["h/q.py", "h/helper.py"])
    return len(hit) == 1 and "h/helper.py" in hit[0] and miss == []


CHECKS.append(check_backend_files)
SELFTESTS.append(selftest_check_backend_files)


def _column_name(arg: str) -> str:
    """SQL の引数 'alias.col' から列名 col を取る。WHY: 引数の表の別名を外し、dataset の physical_column と同じ字面で比べるため。"""
    return arg.strip().split(".")[-1].strip().strip('"`[]')


def check_logical_primary_column(d) -> list:
    """COALESCE(第1引数, 第2引数…) の別名について、元列は第1引数の列だけと認め、dataset が第2引数以降の列を元列として持つものを指摘する。
    WHY: COALESCE の後ろの列は第1引数が空のときの代わりであって、論理名の元列にはならないため。
    Where: 下書きは d["backend_draft"]["apis"][*]["db_operations"][*]["columns"]、照合先は d["main"]["db_operations"][*]["dataset"]。"""
    import re
    coalesce = re.compile(r"^\s*COALESCE\s*\((.*)\)\s*$", re.I | re.S)
    apis = ((d.get("backend_draft") or {}).get("apis") or {}).values()
    # 別名 → (第1引数の列の集合, 第2引数以降の列の集合)
    roles: dict = {}
    for api in apis:
        for op in api.get("db_operations") or []:
            for col in op.get("columns") or []:
                m = coalesce.match(col.get("expression") or "")
                alias = col.get("alias") or col.get("alias_name")
                if not m or not alias:
                    continue
                cols = [_column_name(a) for a in m.group(1).split(",")]
                primary, others = roles.setdefault(alias, (set(), set()))
                primary.add(cols[0])
                others.update(cols[1:])
    out = []
    for op in (d.get("main") or {}).get("db_operations") or []:
        for row in op.get("dataset") or []:
            alias = row.get("alias_name")
            if alias not in roles:
                continue
            primary, others = roles[alias]
            pc = row.get("physical_column")
            if pc in others and pc not in primary:
                out.append(_vs_common.warn(
                    "論理名の元列", f"{op.get('id', '?')} dataset no {row.get('no', '?')}",
                    f"別名 {alias} は COALESCE の第1引数 {'・'.join(sorted(primary))} が元列だが、"
                    f"physical_column が第2引数以降の {pc} になっている"))
    return out


def check_logical_name_consistency(d) -> list:
    """同じ physical_table・physical_column の行に、操作ごとに違う logical_column が付いているものを指摘する。
    WHY: 1つの物理列には1つの論理名で揃っているべきで、違うと画面の項目名が操作によって食い違うため。
    Where: 照合先は d["main"]["db_operations"][*]["dataset"]。"""
    # 物理列 → 操作 → その操作で付いた論理列名の集合
    groups: dict = {}
    for op in (d.get("main") or {}).get("db_operations") or []:
        # WHY: 集計(aggregate)の別名の行は列そのものでなく集計の結果の名前なので、列の論理名として比べない
        agg_aliases = {a.get("alias") for a in op.get("aggregate") or [] if a.get("alias")}
        for row in op.get("dataset") or []:
            if row.get("alias_name") in agg_aliases:
                continue
            pt, pc, lc = row.get("physical_table"), row.get("physical_column"), row.get("logical_column")
            if pt and pc and lc:
                groups.setdefault((pt, pc), {}).setdefault(op.get("id", "?"), set()).add(lc)
    out = []
    for (pt, pc), per_op in groups.items():
        # WHY: 同じ操作の中で同じ列に2つの名が付くのは MIN と MAX のような正当な使い分けなので、
        #      各操作が1つの論理名だけを持ち、その名が操作どうしで違う場合だけを指摘する
        if any(len(names) != 1 for names in per_op.values()):
            continue
        names = {next(iter(v)) for v in per_op.values()}
        if len(names) > 1:
            detail = "・".join(f"{next(iter(v))}({op})" for op, v in sorted(per_op.items()))
            out.append(_vs_common.warn(
                "論理名の揃い", f"{pt}.{pc}", f"操作ごとに論理列名が違う: {detail}"))
    return out


def selftest_logical_primary_column() -> bool:
    """当たり: 第2引数の col_b が dataset の元列になっている → 1件。外れ: 第1引数の col_a → 0件。"""
    def case(pc):
        return {"backend_draft": {"apis": {"a": {"db_operations": [{"columns": [
                    {"alias": "x", "expression": "COALESCE(t1.col_a, t2.col_b)"}]}]}}},
                "main": {"db_operations": [{"id": "DB1", "dataset": [
                    {"no": 1, "alias_name": "x", "physical_column": pc}]}]}}
    return len(check_logical_primary_column(case("col_b"))) == 1 and check_logical_primary_column(case("col_a")) == []


def selftest_logical_name_consistency() -> bool:
    """当たり: 同じ物理列に操作ごとに L1・L2 → 1件。外れ: 両方 L1 → 0件。同じ操作内で MIN・MAX の2名 → 0件。"""
    def case(rows):
        return {"main": {"db_operations": [{"id": oid, "dataset": [
            {"physical_table": "t", "physical_column": "c", "logical_column": lc} for lc in lcs]}
            for oid, lcs in rows]}}
    hit = check_logical_name_consistency(case([("DB1", ["L1"]), ("DB2", ["L2"])]))
    same = check_logical_name_consistency(case([("DB1", ["L1"]), ("DB2", ["L1"])]))
    minmax = check_logical_name_consistency(case([("DB1", ["最初", "最後"]), ("DB2", ["最後"])]))
    # 外れ: DB2 の行は集計の別名 cnt(aggregate に有る)なので、DB1 の L1 だけになり指摘しない
    agg_case = {"main": {"db_operations": [
        {"id": "DB1", "dataset": [{"physical_table": "t", "physical_column": "c", "logical_column": "L1", "alias_name": None}]},
        {"id": "DB2", "aggregate": [{"alias": "cnt"}],
         "dataset": [{"physical_table": "t", "physical_column": "c", "logical_column": "件数", "alias_name": "cnt"}]}]}}
    agg = check_logical_name_consistency(agg_case) == []
    return len(hit) == 1 and same == [] and minmax == [] and agg


CHECKS.append(check_logical_primary_column)
CHECKS.append(check_logical_name_consistency)
SELFTESTS.append(selftest_logical_primary_column)
SELFTESTS.append(selftest_logical_name_consistency)


# 単位語。論理名の末尾と画面項目の名前の末尾で、同じ単位かを比べる
_UNIT_WORDS = ("件数", "日数", "点", "円", "回")


def _unit_of(text: str):
    """末尾が単位語なら、その単位語を返す。無ければ None。"""
    return next((u for u in _UNIT_WORDS if (text or "").endswith(u)), None)


def check_docstring_terms(d) -> list:
    """backend のコメント・docstring が列に付けた語があるのに、その列の論理名が『(推定)』のままのものを指摘する。
    WHY: 語が根拠として手元にあるなら、推定の印を外して出典を語に寄せるべきため。
    Where: 語は d["backend_draft"]["docstring_terms"]({物理列名: [語]}。_backend_facts.docstring_terms が作る)、論理名は d["main"]["logical_names"]。"""
    terms = (d.get("backend_draft") or {}).get("docstring_terms") or {}
    rows = (d.get("main") or {}).get("logical_names") or []
    # WHY: 同じ列に推定でない論理名(語どおりの名)が既にあるなら列の名は揃っている。その列の推定の行は
    #      集計の別名などの派生の名なので、列の出典の指摘の対象にしない
    settled = {(r.get("physical_table"), r.get("physical_column")) for r in rows
               if r.get("logical_column") and not r.get("logical_column").endswith("(推定)")}
    # WHY(第20回 run17): 集計の別名(min(kai_date) AS first_date など)の行は、列そのものの名前でなく「関数 + 列」から付けた名前
    #      (最初の会計日(推定)・最後の会計日(推定))。H2 の手順書は式と別名から付けた名に「(推定)」を付けると決めており、列の語(会計日)が
    #      docstring にあっても「最初の」「最後の」という集計の意味までは語が決めない。列の出典の指摘の対象にすると、手順書どおりの名に毎回 WARN が出る
    agg_alias = {a.get("alias") for op in (d.get("main") or {}).get("db_operations") or [] if isinstance(op, dict)
                 for a in op.get("aggregate") or [] if isinstance(a, dict) and a.get("alias")}
    out = []
    for row in rows:
        col = row.get("physical_column")
        lc = row.get("logical_column") or ""
        if (row.get("physical_table"), col) in settled:
            continue
        if row.get("alias_name") in agg_alias:
            continue
        if col in terms and lc.endswith("(推定)"):
            out.append(_vs_common.warn(
                "論理名の出典", f"{row.get('physical_table', '?')}.{col}",
                f"コメント・docstring の語 {'・'.join(terms[col])} があるのに論理名 {lc} が推定のまま"))
    return out


def check_logical_unit_word(d) -> list:
    """論理名の末尾の単位語と、同じ列を写しで結ぶ画面項目の名前の末尾の単位語が違うものを指摘する。
    WHY: 件数と点のように同じ列を単位の違う語で呼ぶと、画面と設計書で数量の意味が食い違うため。
    Where: 写しは d["main"]["api_spec"][*]["response_to_screen_mapping"]、画面項目は d["main"]["screen_layout"]["items"]、
    論理名は d["main"]["logical_names"]。結びは応答項目の key と api_field_path の末尾が一致するものだけ見る。"""
    main = d.get("main") or {}
    unit_by_col: dict = {}
    for row in main.get("logical_names") or []:
        u = _unit_of(row.get("logical_column"))
        if u:
            unit_by_col.setdefault(row.get("physical_column"), u)
    items = {it.get("item_name"): it for it in (main.get("screen_layout") or {}).get("items") or []}
    out = []
    for api in main.get("api_spec") or []:
        key_to_col = {ri.get("key"): (ri.get("source") or {}).get("source_column")
                      for ri in api.get("response_items") or []}
        for mp in api.get("response_to_screen_mapping") or []:
            key = (mp.get("api_field_path") or "").rsplit(".", 1)[-1]
            col = key_to_col.get(key)
            name = mp.get("screen_item_name")
            if col not in unit_by_col or name is None:
                continue
            item = items.get(name, {})
            u_screen = _unit_of(name) or _unit_of(item.get("format"))
            if u_screen and u_screen != unit_by_col[col]:
                out.append(_vs_common.warn(
                    "論理名の単位", f"{api.get('action', '?')} {key}",
                    f"論理名の単位 {unit_by_col[col]} と画面項目 {name} の単位 {u_screen} が違う"))
    return out


def selftest_docstring_terms() -> bool:
    """当たり: 語 件数 のある cnt の論理名が『件数(推定)』→ 1件。外れ: 論理名が『件数』(推定でない)→ 0件。"""
    def case(lc):
        return {"backend_draft": {"docstring_terms": {"cnt": ["件数"]}},
                "main": {"logical_names": [{"physical_table": "t", "physical_column": "cnt", "logical_column": lc}]}}
    # 外れ: 同じ列に推定でない『件数』と推定の『件数(推定)』が並ぶ → 列は揃っているので 0件
    settled = {"backend_draft": {"docstring_terms": {"cnt": ["件数"]}},
               "main": {"logical_names": [
                   {"physical_table": "t", "physical_column": "cnt", "logical_column": "件数"},
                   {"physical_table": "t", "physical_column": "cnt", "logical_column": "件数(推定)"}]}}
    # 第20回(run17): 集計の別名(min・max の alias)の行は、列の語(会計日)があっても列の出典の指摘にしない。同じ列の別名なしの行は従来どおり指摘する
    agg = {"backend_draft": {"docstring_terms": {"kai_date": ["会計日"]}},
           "main": {"logical_names": [
               {"physical_table": "t", "physical_column": "kai_date", "logical_column": "最初の会計日(推定)", "alias_name": "first_date", "derived": True},
               {"physical_table": "t", "physical_column": "kai_date", "logical_column": "最後の会計日(推定)", "alias_name": "last_date", "derived": True}],
               "db_operations": [{"id": "DB1", "aggregate": [{"function": "min", "target": "p.kai_date", "alias": "first_date"},
                                                              {"function": "max", "target": "p.kai_date", "alias": "last_date"}]}]}}
    plain = {"backend_draft": agg["backend_draft"],
             "main": {"logical_names": agg["main"]["logical_names"] + [
                 {"physical_table": "u", "physical_column": "kai_date", "logical_column": "日(推定)", "alias_name": "d"}],
                 "db_operations": agg["main"]["db_operations"]}}
    return (len(check_docstring_terms(case("件数(推定)"))) == 1 and check_docstring_terms(case("件数")) == []
            and check_docstring_terms(settled) == [] and check_docstring_terms(agg) == []
            and len(check_docstring_terms(plain)) == 1)


def selftest_logical_unit_word() -> bool:
    """当たり: 論理名 件数 と画面項目 販売点 → 1件。外れ: 画面項目 販売件数 → 0件。"""
    def case(screen):
        return {"main": {
            "api_spec": [{"action": "a", "response_items": [{"key": "cnt", "source": {"source_column": "cnt"}}],
                          "response_to_screen_mapping": [{"api_field_path": "rows[].cnt", "screen_item_name": screen}]}],
            "logical_names": [{"physical_table": "t", "physical_column": "cnt", "logical_column": "件数"}],
            "screen_layout": {"items": [{"item_name": screen, "format": ""}]}}}
    return len(check_logical_unit_word(case("販売点"))) == 1 and check_logical_unit_word(case("販売件数")) == []


CHECKS.append(check_docstring_terms)
CHECKS.append(check_logical_unit_word)
SELFTESTS.append(selftest_docstring_terms)
SELFTESTS.append(selftest_logical_unit_word)


def check_merged_single_select(d) -> list:
    """SELECT が1本(WITH・副問い合わせが無い)の API なのに、応答項目の出典が merged になっているものを指摘する。
    WHY: 1本の SELECT の列は1つの表から来るはずで、merged と書かれているなら出典の読みが誤っている可能性が高いため。
    Where: 出典は d["main"]["api_spec"][*]["response_items"][*]["source"]、SELECT の本数は d["backend_draft"]["apis"][action]["db_operations"]。"""
    main = d.get("main") or {}
    apis = (d.get("backend_draft") or {}).get("apis") or {}
    out = []
    for api in main.get("api_spec") or []:
        action = api.get("action")
        ops = (apis.get(action) or {}).get("db_operations") or []
        single = len(ops) == 1 and not ops[0].get("ctes") and not ops[0].get("subqueries")
        if not single:
            continue
        for ri in api.get("response_items") or []:
            if (ri.get("source") or {}).get("type") == "merged":
                out.append(_vs_common.warn(
                    "データ起源", f"{action}.{ri.get('key', '?')}",
                    "SELECT が1本なのに出典が merged になっている。1つの表の列として読み直す"))
    return out


def check_remark_date_format(d) -> list:
    """画面項目の remarks・example の日付の区切り(/ か -)が、同じ項目の画面ソースの日付の区切りと違うものを指摘する。
    WHY: 設計書の書き方とソースの表示が食い違うと、利用者が見る日付の形式が設計書と合わなくなるため。
    Where: 項目は d["main"]["screen_layout"]["items"]、ソースの表示は d["react_draft"]["jsx_elements"]・["ui_strings"](label・text で結ぶ)。"""
    import re
    date_re = re.compile(r"\d{4}([/-])\d{1,2}\1\d{1,2}")

    def seps(text):
        return {m.group(1) for m in date_re.finditer(text or "")}

    rd = d.get("react_draft") or {}
    out = []
    for it in ((d.get("main") or {}).get("screen_layout") or {}).get("items") or []:
        name = it.get("item_name")
        doc = seps(it.get("remarks")) | seps(it.get("example"))
        if not name or not doc:
            continue
        src = set()
        for el in rd.get("jsx_elements") or []:
            if el.get("label") == name:
                src |= seps(el.get("text"))
        for us in rd.get("ui_strings") or []:
            if name in (us.get("text") or ""):
                src |= seps(us.get("text"))
        if src and doc != src:
            out.append(_vs_common.warn(
                "日付表記", name,
                f"設計書の日付の区切り {'・'.join(sorted(doc))} と画面ソースの {'・'.join(sorted(src))} が違う"))
    return out


def selftest_merged_single_select() -> bool:
    """当たり: SELECT 1本・出典 merged → 1件。外れ: WITH 有り(ctes 非空)→ 0件。"""
    def case(ctes):
        return {"main": {"api_spec": [{"action": "a", "response_items": [
                    {"key": "k", "source": {"type": "merged"}}]}]},
                "backend_draft": {"apis": {"a": {"db_operations": [{"ctes": ctes, "subqueries": []}]}}}}
    return len(check_merged_single_select(case([]))) == 1 and check_merged_single_select(case([{"name": "c"}])) == []


def selftest_remark_date_format() -> bool:
    """当たり: 設計書 2026/01/31・画面ソース 2026-01-31 → 1件。外れ: 両方 / → 0件。"""
    def case(src):
        return {"main": {"screen_layout": {"items": [{"item_name": "締日", "remarks": "2026/01/31 形式"}]}},
                "react_draft": {"jsx_elements": [{"label": "締日", "text": src}]}}
    return len(check_remark_date_format(case("2026-01-31"))) == 1 and check_remark_date_format(case("2026/01/31")) == []


CHECKS.append(check_merged_single_select)
CHECKS.append(check_remark_date_format)
SELFTESTS.append(selftest_merged_single_select)
SELFTESTS.append(selftest_remark_date_format)
