"""応答項目の合成(B3)。H3 の応答の型と S2 の画面への写しを、下書きの事実で補う。

呼出元: _units.compose → _run_fillers(out, log)。この中で fill(out, log) を1回呼ぶ。
ORDER を 110 にする理由: 表示名の補完は論理名(_compose_db_facts.py が補う logical_names)を使う。
  既定の 100 のままだと、ファイル名の順(_compose_db_facts.py が先)にしか頼れないため、順序を数で固定する。
入力: out(compose の途中の dict)。下書きは target/intermediate の *_backend_draft.json と *_react_draft.json を読む
  (_fill_error_codes と同じ読み方)。
書き換えるのは空の欄と未設定の欄だけ。ただし a) の表示名のうち、表示行の事実で決まる行(キーに [] を含み、欄名が表示行の kin・cnt に在る行)は、書かれた値があっても事実で上書きする(V3。H3 が表示行と違う名前を書いても、ここで直す)。
  a) api_spec[].response_items[].display_name が空: 画面への写し(同じ path の画面項目名が1つに決まるとき)
     → 同じ別名の列の元の列の論理名 → request_params の同名の説明、の順。どれでも決まらなければ空のまま
     (検査 B14 が H3 へ戻す)
  b) response_items[].source.type が merged で、その API の db 操作が1本なら db にする
  c) response_to_screen_mapping[].screen_item_nth: 同じ名前の表示行が複数あるときだけ、表示行の nth を入れる
  d) response_items[].data_type・nullable: キーが [] を含む行は、下書きの列の事実(式の ::型・COUNT・COALESCE・配列、DDL の型)で決める。
     事実が無い欄はモデルの値を残す。モデルの値と食い違えば事実で上書きし、log に残す(H3 が index signature の union を写すのを止める)
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す

ORDER = 110

_ESTIMATE = "(推定)"


def _list(d, key):
    """d[key] の辞書の要素だけを list で返す。欠けは空の list(合成の途中で欄が無い行を落とさないため)。"""
    return [x for x in (d.get(key) or []) if isinstance(x, dict)]


def _leaf(path):
    """response key や api_field_path の末尾の語(rows[].shop_id → shop_id)。react の kin・cnt や別名と同じ語で引くため。"""
    parts = [p for p in re.split(r"[.\[\]]+", str(path or "")) if p]
    return parts[-1] if parts else ""


def _plain(name):
    """論理名から推定の印を外す。(推定) は B2 が表の論理名に付ける印で、列の名前には付けない。"""
    s = str(name or "")
    return s[: -len(_ESTIMATE)] if s.endswith(_ESTIMATE) else s


def _mapped_name(api, key):
    """画面への写し: 同じ api_field_path の行の画面項目名が1つに決まるときだけ返す。
    WHY: 2つ以上の画面項目に出る値は、どの項目の名前が業務の名前か決められない(推測しない)。"""
    names = {m.get("screen_item_name") for m in _list(api, "response_to_screen_mapping")
             if m.get("api_field_path") == key and m.get("screen_item_name")}
    return names.pop() if len(names) == 1 else ""


def _logical_of(out, table, column):
    """(物理表, 物理列)の論理列を composed logical_names から引く。論理名が1つに決まるときだけ返す。"""
    names = {_plain(n.get("logical_column")) for n in _list(out, "logical_names")
             if n.get("physical_table") == table and n.get("physical_column") == column}
    names.discard("")
    return names.pop() if len(names) == 1 else ""


def _alias_logical(out, draft_cols, leaf):
    """同じ別名の列(下書きの db 操作の columns)の元の列の論理名。元の列が1つに決まる列だけを使う。
    WHY: 式が複数の列を参照するとき(SUM(a+b) 等)は、どの列の名前が業務の名前か決まらない。"""
    got = set()
    for c in draft_cols:
        if c.get("alias") != leaf:
            continue
        src = c.get("source_columns") or []
        if len(src) == 1 and "." in str(src[0]):
            table, column = str(src[0]).rsplit(".", 1)
            name = _logical_of(out, table, column)
            if name:
                got.add(name)
    return got.pop() if len(got) == 1 else ""


def _request_desc(api, leaf):
    """request の項目(started_on 等)を返す時は、request_params の同名の説明を業務の名前にする。"""
    descs = {str(p.get("description") or "") for p in _list(api, "request_params")
             if p.get("name") == leaf and p.get("description")}
    return descs.pop() if len(descs) == 1 else ""


def _spec_display_names(specs):
    """表示行(react 下書きの display_rows の specs)から、応答の欄の表示名を決める {欄名: 表示名}(V3)。
    規則: 名 = csv があれば csv、なければ label。cnt の欄は 名+「点数」、kin の欄は cnt があれば 名+「金額」、なければ 名。
    WHY: 応答のキー順と表示行の並びは一致しない(RIGHT 配列の並びと kamoku_1〜6 の対応がずれる)。位置で対にせず、欄の名で引く。
    同じ欄を2つ以上の表示行が指すときは決めない(推測しない)。"""
    got = {}
    for s in specs:
        base = str(s.get("csv") or s.get("label") or "")
        if not base:
            continue
        if s.get("cnt"):
            got.setdefault(s["cnt"], []).append(base + "点数")
        if s.get("kin"):
            got.setdefault(s["kin"], []).append(base + ("金額" if s.get("cnt") else ""))
    return {k: v[0] for k, v in got.items() if len(v) == 1}


def _fill_display_names(out, api, draft_cols, log, spec_names=None):
    """response_items の display_name を補う。
    a) 表示行の事実(spec_names)に当たる行(キーに [] を含み、欄名が表示行の kin・cnt に在る)は、書かれた値も事実で上書きする(V3)。
    b) それ以外は、空の行だけを画面への写し・論理名・request の説明で補う(従来どおり)。"""
    spec_names = spec_names or {}
    for x in _list(api, "response_items"):
        key = x.get("key")
        leaf = _leaf(key)
        fact = spec_names.get(leaf) if "[]" in str(key or "") else None
        if fact:
            old = x.get("display_name")
            if old != fact:
                x["display_name"] = fact
                log.append(f"[表示名] api_spec[{api.get('action')}].response_items[{key}] の display_name を {old or '(空)'} から表示行の事実 {fact} に直す")
            continue
        if x.get("display_name"):
            continue
        name = _mapped_name(api, key) or _alias_logical(out, draft_cols, leaf) or _request_desc(api, leaf)
        if name:
            x["display_name"] = name
            log.append(f"[表示名] api_spec[{api.get('action')}].response_items[{key}] の display_name を {name} で補う")


def _fix_merged_source(api, n_db, log):
    """db 操作が1本の API では、merged(複数の出典の混成)の出典は db の1つに決まる(b)。複数本なら決めない。
    WHY: 1本しか db 操作が無ければ、混成に見えた値はその操作の列から来ている。"""
    if n_db != 1:
        return
    for x in _list(api, "response_items"):
        s = x.get("source")
        if isinstance(s, dict) and s.get("type") == "merged":
            s["type"] = "db"
            log.append(f"[出典] api_spec[{api.get('action')}].response_items[{x.get('key')}] の source.type を merged から db にする")


def _fill_screen_nth(api, specs, log):
    """同じ名前の表示行が複数あるときだけ、画面項目が何番目かを入れる(c)。
    WHY: 「小計」が3つあると、名前だけでは migrate の find_item_no が結ぶ先を決められない。
    名前が1つなら番号は要らない。kin・cnt は応答のどの欄を表示するかの語で、末尾の語と一致する行だけを使う。"""
    nths = {}
    for s in specs:
        nths.setdefault(s.get("label"), set()).add(s.get("nth"))
    for m in _list(api, "response_to_screen_mapping"):
        if m.get("screen_item_nth"):
            continue
        name, leaf = m.get("screen_item_name"), _leaf(m.get("api_field_path"))
        if len(nths.get(name, ())) <= 1 or not leaf:
            continue
        hit = {s.get("nth") for s in specs
               if s.get("label") == name and leaf in (s.get("kin"), s.get("cnt"))}
        if len(hit) == 1:
            m["screen_item_nth"] = hit.pop()
            log.append(f"[画面項目] api_spec[{api.get('action')}].response_to_screen_mapping[{m.get('api_field_path')}] の screen_item_nth を入れる")


def _load_drafts(directory):
    """下書きを読む。戻り: (action → backend 下書きの api の辞書, 表示行の specs の list)。読めないファイルは無いものとして飛ばす。"""
    backend, specs = {}, []
    d = Path(directory)
    for p in sorted(d.glob("*_backend_draft.json")):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for act, a in (b.get("apis") or {}).items():
            if isinstance(a, dict):
                backend.setdefault(act, a)
    for p in sorted(d.glob("*_react_draft.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in r.get("display_rows") or []:
            if isinstance(row, dict):
                specs += [s for s in (row.get("specs") or []) if isinstance(s, dict)]
    return backend, specs


# d) の型の対応(SQL の型名 → 設計書の型)。一般的な対応だけを持つ。ここに無い型(timestamp など)は決めず、モデルの値を残す
_TYPE_WORDS = (
    ("number", ("bigint", "int", "int2", "int4", "int8", "integer", "smallint", "numeric", "decimal", "real", "float", "float4", "float8", "double")),
    ("string", ("text", "varchar", "char", "character", "bpchar", "citext", "uuid")),
    ("boolean", ("bool", "boolean")),
)


def _is_array_type(t):
    """設計書の型が配列か。「number | string | string[]」のような共用体は配列と見ない(index signature の写しを配列の印にしないため)。"""
    s = str(t or "").strip()
    return "|" not in s and (s.endswith("[]") or s.startswith(("array", "配列")))


def _design_type(t):
    """SQL の型名(小文字)を設計書の型へ。配列は "array" の印で返す。対応が無ければ None。"""
    if t == "array":
        return "array"
    if t == "date":
        return "date(YYYY-MM-DD)"
    for design, words in _TYPE_WORDS:
        if t in words:
            return design
    return None


def _expr_fact(expr):
    """SELECT 式の (SQL の型名, null 可) を式の形から読む。読めない欄は None。
    取れる順: ARRAY_AGG(ARRAY_REMOVE で包んだものを含む)は array / 末尾の ::型(NULL::型 なら null 可)/ COUNT(...) は bigint・非 null /
    COALESCE(...) は最後の引数が NULL でなければ非 null(型は見ない)。SUM(x) の無印などは推測しない。"""
    e = str(expr or "").strip()
    if re.match(r"(ARRAY_REMOVE\s*\(\s*)?ARRAY_AGG\s*\(", e, re.IGNORECASE):
        return "array", None
    t, null = None, None
    m = re.search(r"::\s*(\w+)\s*$", e)
    if m:
        t, null = m.group(1).lower(), bool(re.match(r"NULL\s*::", e, re.IGNORECASE))
    elif re.match(r"COUNT\s*\(", e, re.IGNORECASE):
        t, null = "bigint", False
    if re.match(r"COALESCE\s*\(", e, re.IGNORECASE) and not re.search(r",\s*NULL\s*\)", e, re.IGNORECASE):
        null = False
    return t, null


def _merge_facts(facts):
    """同じ別名の式の事実を1つに合わせる。型は1通りに決まるときだけ決め、null 可は1つでも NULL を返す式があれば可。
    WHY: 集計単位で式が変わる列(店舗別だけ NULL::varchar、レジ別は p.reji_no)は、選び方によって NULL になり得る1列。
         どれか1つでも NULL なら null 可、全部が非 null と分かったときだけ非 null。型が食い違うときは決めない。"""
    ts = {t for t, _ in facts if t}
    if len(ts) > 1:
        return None
    ns = [n for _, n in facts]
    if True in ns:
        null = True
    elif all(n is False for n in ns):
        null = False
    else:
        null = None
    return (ts.pop() if ts else None), null


def _column_facts(bd):
    """backend 下書きの列ごとの事実 {別名: (SQL の型名, null 可)}。同じ別名の複数の式は _merge_facts で1つに合わせる(型が違うときは決めない)。"""
    per = {}
    for o in bd.get("db_operations") or []:
        if not isinstance(o, dict):
            continue
        for c in o.get("columns") or []:
            if isinstance(c, dict) and c.get("alias"):
                per.setdefault(c["alias"], set()).add(_expr_fact(c.get("expression")))
    # WHY: 下書きの aliases(SELECT の式の一覧)も事実。画面の集計単位で式が変わる列(NULL::varchar の行)はここにしか無いことがある
    for c in bd.get("aliases") or []:
        if isinstance(c, dict) and c.get("alias"):
            per.setdefault(c["alias"], set()).add(_expr_fact(c.get("expression")))
    ddl = {}
    for f in bd.get("response_fields") or []:
        if isinstance(f, dict) and f.get("index_signature") and f.get("path"):
            ddl[_leaf(f["path"])] = (f.get("db_type"), f.get("db_nullable"))
    out = {}
    for alias, facts in per.items():
        m = _merge_facts(list(facts))
        if m is None:
            continue
        t, null = m
        dt, dn = ddl.get(alias, (None, None))
        # fallback-ok: 式から型・null 可が読めない欄だけ、index signature の行の DDL の型で補う(式の事実を優先する)
        t = t or dt
        null = dn if null is None else null
        if t is None and null is None:
            continue
        out[alias] = (t, null)
    return out


def _fill_types(api, bd, log):
    """response_items の data_type・nullable を、backend 下書きの列の事実で決める(d)。
    キーが [] を含む行(一覧の項目)だけを、同じ別名の列の事実で見る。事実が無い欄はモデルの値を残す。
    WHY: TS の index signature(number | string | string[] | null)を H3 が写すと、SQL の型と食い違って毎回 verify_vs_source で止まるため。
    SUM(...)::bigint は number・非 null。配列(ARRAY_AGG)は要素の型が分からなくても配列の印を付け、元が配列ならそのまま残す。"""
    facts = _column_facts(bd)
    for x in _list(api, "response_items"):
        key = str(x.get("key") or "")
        if "[]" not in key or _leaf(key) not in facts:
            continue
        t, null = facts[_leaf(key)]
        old = (x.get("data_type"), x.get("nullable"))
        d = _design_type(t)
        if d == "array":
            if not _is_array_type(x.get("data_type")):
                x["data_type"] = "配列"
        elif d:
            x["data_type"] = d
        if null is not None:
            x["nullable"] = null
        new = (x.get("data_type"), x.get("nullable"))
        if new != old:
            log.append(f"[型] api_spec[{api.get('action')}].response_items[{key}] を事実で決める: {old[0]}・null可 {old[1]} → {new[0]}・null可 {new[1]}")


def _fill_sources(out, api, bd, log):
    """response_items の source(type・source_column・source_ref)と expression を、backend 下書きの response_fields の事実(契約 i3.k1)で決める。
    WHY: 欄の起源は担当(S2)に書かせると run ごとに揺れる(pos_account_reports と DB2 など)ため、extract_backend_draft._field_sources が決めた値を写す。
    下書きに source が無い欄・response_items に同じ key の行が無い欄は触らない。source_ref は、下書きの op(db_op_…)が out の db_operations の行
    (id か、番号振り後の legacy_id が一致)に引けるときだけ設定し、引けないときは変えずにログに残す。"""
    ids = {r.get(k) for r in _list(out, "db_operations") for k in ("id", "legacy_id") if r.get(k)}
    facts = {f.get("path"): f for f in bd.get("response_fields") or [] if isinstance(f, dict) and isinstance(f.get("source"), dict)}
    for x in _list(api, "response_items"):
        key = str(x.get("key") or "")
        f = facts.get(key)
        if not f:
            continue
        s = x.get("source") if isinstance(x.get("source"), dict) else {}
        old = (s.get("type"), s.get("source_ref"), s.get("source_column"), x.get("expression"))
        s["type"], s["source_column"] = f["source"].get("type"), f["source"].get("column")
        op = f["source"].get("op")
        if op in ids:
            s["source_ref"] = op
        else:
            log.append(f"[出典] api_spec[{api.get('action')}].response_items[{key}] の source_ref は下書きの op {op} を db_operations に引けないので変えない")
        x["source"] = s
        if f.get("expression") is not None:
            x["expression"] = f["expression"]
        new = (s.get("type"), s.get("source_ref"), s.get("source_column"), x.get("expression"))
        if new != old:
            log.append(f"[出典] api_spec[{api.get('action')}].response_items[{key}] を下書きの事実で決める: {old} → {new}")


def fill(out, log):
    """api_spec の各行を補う。下書きが無い API は、下書きの db 操作の数が 0 なので b) は動かず、a) の列の参照も空になる。"""
    from _config_loader import INTERMEDIATE_DIR
    backend, specs = _load_drafts(INTERMEDIATE_DIR)
    names = _spec_display_names(specs)
    for api in _list(out, "api_spec"):
        bd = backend.get(api.get("action")) or {}
        dbs = [o for o in (bd.get("db_operations") or []) if isinstance(o, dict)]
        cols = [c for o in dbs for c in (o.get("columns") or []) if isinstance(c, dict)]
        _fill_display_names(out, api, cols, log, names)
        _fix_merged_source(api, len(dbs), log)
        _fill_screen_nth(api, specs, log)
        _fill_types(api, bd, log)
        _fill_sources(out, api, bd, log)


def _selftest():
    """a) 表示名の4段の順・既存値の保持、b) merged の db 化(1本だけ)、c) nth の付与(同名が複数のときだけ)、
    fill 全体(一時の下書きを INTERMEDIATE_DIR に向けて)を assert する。"""
    import tempfile
    import _config_loader

    # a) 画面への写し(1項目に決まる)→ 論理名(別名の元の列が1つ)→ request の説明 → 補えない → 既存の値は触らない
    api = {"action": "get_x",
           "response_items": [{"key": "rows[].total", "display_name": ""},
                              {"key": "rows[].amount", "display_name": ""},
                              {"key": "started_on", "display_name": ""},
                              {"key": "rows[].zz", "display_name": ""},
                              {"key": "rows[].kept", "display_name": "既存"}],
           "request_params": [{"name": "started_on", "description": "集計開始日"}],
           "response_to_screen_mapping": [{"api_field_path": "rows[].total", "screen_item_name": "合計"},
                                          {"api_field_path": "rows[].amount", "screen_item_name": "小計"},
                                          {"api_field_path": "rows[].amount", "screen_item_name": "件数"}]}
    out = {"api_spec": [api], "logical_names": [{"physical_table": "sales", "physical_column": "amt", "logical_column": "売上額(推定)"}]}
    cols = [{"alias": "amount", "source_columns": ["sales.amt"]}, {"alias": "zz", "source_columns": ["a.b", "a.c"]}]
    log = []
    _fill_display_names(out, api, cols, log)
    got = [x["display_name"] for x in api["response_items"]]
    assert got == ["合計", "売上額", "集計開始日", "", "既存"], got
    assert len(log) == 3, log

    # b) db 操作が1本の API だけ merged を db にする。2本以上・0本は決めない
    for n, want in ((1, "db"), (2, "merged"), (0, "merged")):
        a2 = {"action": "a", "response_items": [{"key": "k", "source": {"type": "merged", "source_ref": "x"}}]}
        _fix_merged_source(a2, n, [])
        assert a2["response_items"][0]["source"]["type"] == want, (n, a2)

    # G20: 下書きに source がある欄だけ type・source_column・expression を書き換え、source_ref は db_operations に引けるときだけ。無い欄は元のまま
    a3 = {"action": "g", "response_items": [{"key": "rows[].t", "source": {"type": "merged", "source_ref": "old"}, "expression": "x"},
                                          {"key": "rows[].u", "source": {"type": "const", "source_ref": "keep"}, "expression": "keep"},
                                          {"key": "rows[].v", "source": {"type": "merged", "source_ref": "old"}}]}
    bd3 = {"response_fields": [{"path": "rows[].t", "source": {"type": "calculated", "op": "db_op_g", "column": "t"}, "expression": "SUM(t.k)"},
                               {"path": "rows[].v", "source": {"type": "db", "op": "db_op_zz", "column": "v"}, "expression": "t.v"},
                               {"path": "rows[].u"}]}
    log3 = []
    _fill_sources({"db_operations": [{"id": "DB1", "legacy_id": "db_op_g"}]}, a3, bd3, log3)
    g_t, g_u, g_v = a3["response_items"]
    assert g_t["source"] == {"type": "calculated", "source_ref": "db_op_g", "source_column": "t"} and g_t["expression"] == "SUM(t.k)", g_t
    assert g_u == {"key": "rows[].u", "source": {"type": "const", "source_ref": "keep"}, "expression": "keep"}, g_u
    assert g_v["source"]["type"] == "db" and g_v["source"]["source_ref"] == "old" and g_v["expression"] == "t.v" and any("db_op_zz" in m for m in log3), (g_v, log3)

    # c) 「小計」が3つ(kin s3・s4・cnt n5)のとき、kin か cnt の末尾の語で nth を引く。「合計」は1つだけなので入れない。
    #    どの表示行にも無い語(s9)は入れない
    specs = [{"label": "小計", "kin": "s3", "cnt": None, "nth": 1},
             {"label": "小計", "kin": "s4", "cnt": None, "nth": 2},
             {"label": "小計", "kin": None, "cnt": "n5", "nth": 3},
             {"label": "合計", "kin": "t1", "cnt": None, "nth": 1}]
    api3 = {"action": "a", "response_to_screen_mapping": [
        {"api_field_path": "rows[].s4", "screen_item_name": "小計"},
        {"api_field_path": "rows[].n5", "screen_item_name": "小計"},
        {"api_field_path": "rows[].t1", "screen_item_name": "合計"},
        {"api_field_path": "rows[].s9", "screen_item_name": "小計"}]}
    _fill_screen_nth(api3, specs, [])
    assert [m.get("screen_item_nth") for m in api3["response_to_screen_mapping"]] == [2, 3, None, None], api3

    # d) 型の事実: SUM(...)::bigint は number・非 null(index signature の union を上書き)、COUNT は number、COALESCE は非 null、
    #    ARRAY_AGG は配列(元が配列ならそのまま)、式が読めず DDL も無い列はモデルの値を残す、NULL::型 は null 可、DDL の型は式が読めない列だけ使う
    bd = {"db_operations": [{"columns": [
            {"alias": "sou", "expression": "SUM(p.sou)::bigint"},
            {"alias": "n", "expression": "count(*)::bigint"},
            # fallback-ok: COALESCE は式の形から非 null を読むことを確かめるテストの入力(既定値の切り替えではない)
            {"alias": "nm", "expression": "COALESCE(s.name, '(不明)')"},
            {"alias": "ds", "expression": "array_remove(array_agg(p.d ORDER BY p.d), NULL)"},
            {"alias": "bare", "expression": "p.bare"},
            {"alias": "nn", "expression": "NULL::text"}]},
          # WHY: 同じ別名が店舗別(NULL::varchar)とレジ別(p.rj)で式が違う列は、1つでも NULL なら null 可・型は1通りに決まる
          {"columns": [{"alias": "rj", "expression": "p.rj"}]}],
          "aliases": [{"alias": "rj", "expression": "NULL::varchar", "when": "group = shop"}],
          "response_fields": [{"path": "rows[].bare", "index_signature": True, "db_type": "date", "db_nullable": False}]}
    fa = {"action": "a", "response_items": [
        {"key": "rows[].sou", "data_type": "number | string | string[]", "nullable": True},
        {"key": "rows[].n", "data_type": "number | string | string[]", "nullable": True},
        {"key": "rows[].nm", "data_type": "string", "nullable": True},
        {"key": "rows[].ds", "data_type": "string[]", "nullable": False},
        {"key": "rows[].bare", "data_type": "string", "nullable": True},
        {"key": "rows[].nn", "data_type": "string", "nullable": False},
        {"key": "rows[].miss", "data_type": "number | string", "nullable": True},
        {"key": "rows[].rj", "data_type": "number | string", "nullable": False},
        {"key": "total", "data_type": "number | string", "nullable": True}]}
    tlog = []
    _fill_types(fa, bd, tlog)
    got = {x["key"]: (x["data_type"], x["nullable"]) for x in fa["response_items"]}
    assert got == {"rows[].sou": ("number", False), "rows[].n": ("number", False), "rows[].nm": ("string", False),
                   "rows[].ds": ("string[]", False), "rows[].bare": ("date(YYYY-MM-DD)", False),
                   "rows[].nn": ("string", True), "rows[].rj": ("string", True),
                   "rows[].miss": ("number | string", True), "total": ("number | string", True)}, got
    assert len(tlog) == 6, tlog

    # a2) 表示行の事実(V3): 名 = csv か label、cnt は 名+点数、kin は cnt があれば 名+金額。欄の名で引くので RIGHT の並びとずれない。
    #     書かれた値(入れ替わった値・空)は事実で上書きする。同じ欄を2行が指すときは決めない。[] を含まないキーは触らない
    sp = [{"label": "内金", "csv": "", "kin": "kamoku_2", "cnt": None},
          {"label": "現金", "csv": "現金(実)", "kin": "kamoku_1", "cnt": "cnt_1"},
          {"label": "小計", "csv": "", "kin": "syoukei_5", "cnt": None},
          {"label": "小計", "csv": "小計1(値引前)", "kin": "syoukei_6", "cnt": None},
          {"label": "重複", "csv": "", "kin": "same", "cnt": None},
          {"label": "重複", "csv": "", "kin": "same", "cnt": None}]
    names = _spec_display_names(sp)
    assert names == {"kamoku_2": "内金", "kamoku_1": "現金(実)金額", "cnt_1": "現金(実)点数",
                     "syoukei_5": "小計", "syoukei_6": "小計1(値引前)"}, names
    api4 = {"action": "a", "response_items": [
        {"key": "rows[].kamoku_2", "display_name": "現金"},
        {"key": "rows[].kamoku_1", "display_name": "現金"},
        {"key": "rows[].cnt_1", "display_name": ""},
        {"key": "rows[].same", "display_name": "x"},
        {"key": "total", "display_name": "合計"}]}
    l4 = []
    _fill_display_names({}, api4, [], l4, names)
    assert [x["display_name"] for x in api4["response_items"]] == ["内金", "現金(実)金額", "現金(実)点数", "x", "合計"], api4
    assert len(l4) == 3, l4

    # fill 全体: 一時の下書きを読み、表示名・出典・nth が入る。INTERMEDIATE_DIR は必ず戻す
    import json as _json
    orig = _config_loader.INTERMEDIATE_DIR
    with tempfile.TemporaryDirectory() as td:
        Path(td, "X_backend_draft.json").write_text(_json.dumps({"apis": {"get_y": {"db_operations": [
            {"columns": [{"alias": "amount", "source_columns": ["sales.amt"]}]}]}}}, ensure_ascii=False), encoding="utf-8")
        Path(td, "X_react_draft.json").write_text(_json.dumps({"display_rows": [{"specs": specs}]}, ensure_ascii=False), encoding="utf-8")
        _config_loader.INTERMEDIATE_DIR = Path(td)
        try:
            full = {"api_spec": [{"action": "get_y",
                                  "response_items": [{"key": "rows[].amount", "display_name": "",
                                                      "source": {"type": "merged", "source_ref": "x"}}],
                                  "response_to_screen_mapping": [{"api_field_path": "rows[].s4", "screen_item_name": "小計"}]}],
                    "logical_names": [{"physical_table": "sales", "physical_column": "amt", "logical_column": "売上額"}]}
            flog = []
            fill(full, flog)
        finally:
            _config_loader.INTERMEDIATE_DIR = orig
    fa = full["api_spec"][0]
    assert fa["response_items"][0]["display_name"] == "売上額", fa
    assert fa["response_items"][0]["source"]["type"] == "db", fa
    assert fa["response_to_screen_mapping"][0]["screen_item_nth"] == 2, fa
    assert len(flog) == 3, flog


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        print("[selftest OK] _compose_response (B3: 表示名の補完・merged の db 化・screen_item_nth)")
    else:
        print("usage: _compose_response.py --selftest (fill は _units.compose から呼ばれる)")
        sys.exit(2)
