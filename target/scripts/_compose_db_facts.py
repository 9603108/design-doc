"""DB 名の合成(round9 B2)。H2 が出した db_operations・logical_names を、backend 下書きの事実で補う。

呼出元: _units._run_fillers(compose の番号振りの前)。ORDER=10 で他の _compose_*.py より先に走る。
WHY: 物理列の空欄・句の物理名・表の論理名の付け忘れ・分岐の when・集計の別名は、H2 が書き落とすと
     下書きに事実があっても直す経路が無かった。モデルに書き直させず、事実から機械で埋める。
     POS 固有の決め打ちはしない。全て欄の形と下書きの対応だけで決める。
"""
import json
import re
import sys
from pathlib import Path

ORDER = 10
SUFFIX = "(推定)"
_QUAL = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")


def _nows(s):
    return "".join(str(s or "").split())  # fallback-ok: None を空文字として比べる(比較専用)


def _sfx(name):
    """論理表名に「(推定)」を付ける。表の出所(DDL の表コメント)は読んでいないので、全部を推定にする。"""
    # WHY: 半角括弧にする。全角は apply_react_draft の仮判定に掛かるため。
    if not name or name.endswith(SUFFIX):
        return name
    return name + SUFFIX


def _load_drafts(directory):
    out = []
    for p in sorted(Path(directory).glob("*_backend_draft.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _draft_op(drafts, op, log):
    """db_operation に対応する下書きの (db_op, api) を返す。見つからない・曖昧なときは (None, None)。
    WHY: 同じ action と id が複数の下書きにあると、どれの事実か決まらない。推測せず合成を止める。"""
    action = (op.get("api_endpoint") or {}).get("action")  # fallback-ok: api_endpoint は任意。無ければ対応が取れないだけ
    found = []
    for d in drafts:
        a = (d.get("apis") or {}).get(action) if action else None  # fallback-ok: 下書きの apis は任意
        if not a:
            continue
        for dop in a.get("db_operations") or []:  # fallback-ok: 下書きの欄は任意(無ければ対応なし)
            if dop.get("id") == op.get("id"):
                found.append((dop, a))
    if not found:
        return None, None
    if len(found) > 1:
        log.append(f"WARN _compose_db_facts: {op.get('id')} の下書きが {len(found)} 件あり、合成しない")
        return None, None
    return found[0]


def _column_of(table, sources):
    """source_columns("表.列" の並び)から、表がこの表の列を1つに決められたときだけ列名を返す。
    WHY: 2つ以上あれば、どの列を指すか事実が決まらない。空のまま残す。"""
    cols = {s.split(".", 1)[1] for s in sources or []  # fallback-ok: 式の列は無いことがある(count(*) 等)
            if "." in s and table and s.split(".", 1)[0] == table}
    return next(iter(cols)) if len(cols) == 1 else None


def _fill_physical(out, pairs, log):
    """物理列の空欄を、下書きの列(別名の一致)の source_columns から補う。決まらない行は alias_expression を入れる。
    WHY: 式(min(p.kai_date) 等)でも、元の表の列が1つに決まるなら物理列は分かる。count(*) のように決まらないものは式で示す。"""
    cand = {}
    for op, dop, _ in pairs:
        cols = {c.get("alias_name"): c for c in dop.get("columns") or [] if c.get("alias_name")}  # fallback-ok: 下書きの欄は任意
        for c in cols.values():
            cand.setdefault(c["alias_name"], []).append(c)
        for row in op.get("dataset") or []:  # fallback-ok: dataset は任意の欄
            if row.get("physical_column") or not row.get("alias_name"):
                continue
            c = cols.get(row["alias_name"])
            if not c:
                continue
            pc = _column_of(row.get("physical_table"), c.get("source_columns"))  # fallback-ok: 式の列は任意
            if pc:
                row["physical_column"] = pc
            elif c.get("alias_expression"):  # fallback-ok: 式が無い列は alias_expression を入れない
                row["alias_expression"] = c["alias_expression"]
    # WHY: logical_names は全 op 共通の表なので、同じ別名の下書き列が全部同じ列を指すときだけ補う
    for row in out.get("logical_names") or []:  # fallback-ok: logical_names は任意の欄
        alias = row.get("alias_name")
        if row.get("physical_column") or not alias or not row.get("physical_table"):
            continue
        res = {_column_of(row["physical_table"], c.get("source_columns")) for c in cand.get(alias, [])}  # fallback-ok: 候補が無い別名は空
        if len(res) == 1 and None not in res:
            row["physical_column"] = res.pop()
            row["derived"] = True
            log.append(f"INFO _compose_db_facts: logical_names {row['physical_table']}.{alias} を derived で補う")


def _fill_when(op, a):
    """where の when を、下書きの where の when で置く。下書きに when が無ければ H2 の when を外す。
    WHY: 分岐の有無は下書きが事実。H2 の書いた when は推測になりうる。"""
    dwh = a.get("where") or []  # fallback-ok: 下書きの where は任意
    for row in op.get("where") or []:  # fallback-ok: where は任意
        key = (row.get("logical_table"), row.get("logical_column"), row.get("operator"), row.get("value"))
        ms = [d for d in dwh
              if (d.get("logical_table"), d.get("logical_column"), d.get("operator"), d.get("value")) == key]
        if len(ms) != 1:
            continue
        if ms[0].get("when"):
            row["when"] = ms[0]["when"]
        else:
            row.pop("when", None)  # fallback-ok: 無い欄を外すだけ


def _maps(logical_names):
    pt2lt, lt2pt, col = {}, {}, {}
    for r in logical_names:
        pt, lt = r.get("physical_table") or "", r.get("logical_table") or ""  # fallback-ok: 欄が空の行は対応に使わない
        pc, lc = r.get("physical_column") or "", r.get("logical_column") or ""  # fallback-ok: 同上
        if pt and lt:
            pt2lt.setdefault(pt, lt)
            lt2pt.setdefault(lt, pt)
        # WHY: derived の行は合成が別名から補った行(first_date が kai_date を指す等)。列の対応の根拠にしない。
        if pt and pc and lc and not r.get("derived"):
            col.setdefault((pt, pc), lc)
    return pt2lt, lt2pt, col


def _translate_row(row, pt2lt, lt2pt, col):
    """句の行の物理名を論理名へ置く。物理表か論理表のどちらで書かれていても、表を1つに決めてから列を引く。"""
    lt = row.get("logical_table") or ""  # fallback-ok: 表名が無い行は対象外
    lc = row.get("logical_column") or ""  # fallback-ok: 列名が無い行は対象外
    pt = lt if lt in pt2lt else lt2pt.get(lt)
    if not pt:
        return
    if pt in pt2lt:
        row["logical_table"] = pt2lt[pt]
    if (pt, lc) in col:
        row["logical_column"] = col[(pt, lc)]


def _translate_condition(cond, aliases, pt2lt, col):
    """joins の「別名.列」を、下書きの table_aliases で表へ解決し「論理表.論理列」に作り直す。
    WHY: 解決できない側は元の字面のまま残す(推測しない)。"""
    def rep(m):
        q, c = m.group(1), m.group(2)
        pt = aliases.get(q)
        if not pt or pt not in pt2lt:
            return m.group(0)
        lc = col.get((pt, c))
        if not lc:
            return m.group(0)
        return _sfx(pt2lt[pt]) + "." + lc
    return _QUAL.sub(rep, cond or "")  # fallback-ok: 条件が無い joins は対象外


def _fill_agg(op, dop):
    """集計の別名が null の行を、下書き aggregate_units の別名で埋める。関数・引数・絞り込みが一致し、別名が1つに決まるときだけ。
    WHY: 改行や ORDER BY を含む array_agg は別名が付いていても H2 が落とす。下書きの事実で戻す。"""
    units = dop.get("aggregate_units") or []  # fallback-ok: 下書きの aggregate_units は任意
    for row in op.get("aggregate") or []:  # fallback-ok: aggregate は任意
        if row.get("alias"):
            continue
        f = str(row.get("function") or "").lower()  # fallback-ok: 関数名が無い行は一致しない
        t = _nows(row.get("target"))
        fl = _nows(row.get("filter"))
        al = {u.get("alias") for u in units  # fallback-ok: 別名が無い単位は対象外
              if str(u.get("func") or "").lower() == f and _nows(u.get("arg")) == t  # fallback-ok: 同上
              and _nows(u.get("filter")) == fl and u.get("alias")}  # fallback-ok: 同上
        if len(al) == 1:
            row["alias"] = al.pop()


def _compose_db(out, drafts, log):
    ops = out.get("db_operations") or []  # fallback-ok: db_operations は任意(無い版は何もしない)
    lns = out.get("logical_names") or []  # fallback-ok: logical_names は任意
    pairs = []
    drafts_of = {}
    for op in ops:
        dop, a = _draft_op(drafts, op, log)
        drafts_of[id(op)] = (dop, a)
        if dop:
            pairs.append((op, dop, a))
    _fill_physical(out, pairs, log)                 # a) 物理列の空欄
    for op, dop, a in pairs:                        # e) where の when
        _fill_when(op, a)
    pt2lt, lt2pt, col = _maps(lns)                  # b) 句の論理名
    for op in ops:
        dop, _ = drafts_of[id(op)]
        aliases = (dop or {}).get("table_aliases") or {}  # fallback-ok: 下書きが無い op は別名解決しない
        for key in ("where", "group_by", "order_by"):
            for row in op.get(key) or []:  # fallback-ok: 句は任意
                _translate_row(row, pt2lt, lt2pt, col)
        for row in op.get("joins") or []:  # fallback-ok: joins は任意
            _translate_row(row, pt2lt, lt2pt, col)
            if row.get("condition"):  # fallback-ok: 条件の無い joins は対象外
                row["condition"] = _translate_condition(row["condition"], aliases, pt2lt, col)
    for r in lns:                                   # c) 論理表名の「(推定)」
        if r.get("logical_table"):  # fallback-ok: 表名の無い行は対象外
            r["logical_table"] = _sfx(r["logical_table"])
    for op in ops:
        for key in ("dataset", "where", "group_by", "order_by", "joins"):
            for row in op.get(key) or []:  # fallback-ok: 任意の句
                if row.get("logical_table"):  # fallback-ok: 表名の無い行は対象外
                    row["logical_table"] = _sfx(row["logical_table"])
    for op, dop, _ in pairs:                        # d) 集計の別名
        _fill_agg(op, dop)
    return out


def fill(out, log):
    """_run_fillers から呼ばれる入口。下書きは INTERMEDIATE_DIR の *_backend_draft.json を読む。"""
    from _config_loader import INTERMEDIATE_DIR
    _compose_db(out, _load_drafts(INTERMEDIATE_DIR), log)


def selftest():
    drafts = [{"apis": {"list_x": {
        "where": [{"logical_table": "pos", "logical_column": "mise_no", "operator": "=",
                   "value": "shop_id", "when": "shop_id が指定されたとき"}],
        "db_operations": [{
            "id": "db_op_list_x",
            "table_aliases": {"p": "pos", "s": "shops"},
            "columns": [
                {"alias_name": "shop_id", "source_columns": ["pos.mise_no"], "alias_expression": ""},
                {"alias_name": "shop_name", "source_columns": ["shops.name", "pos.mise_no"],
                 "alias_expression": "COALESCE(s.name, '不明')"},  # fallback-ok: テスト用の表示式の文字列(既定値ではない)
                {"alias_name": "days", "source_columns": [], "alias_expression": "count(*)::bigint"},
                {"alias_name": "first_date", "source_columns": ["pos.kai_date"],
                 "alias_expression": "min(p.kai_date)"},
            ],
            "aggregate_units": [
                {"func": "array_agg", "arg": "CASE WHEN p.k > 0 THEN p.d END ORDER BY p.d",
                 "alias": "warn_dates"},
                {"func": "count", "arg": "*", "alias": "days"},
            ],
        }],
    }}}]
    out = {
        "logical_names": [
            {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "mise_no",
             "logical_column": "店舗ID", "alias_name": "shop_id"},
            {"physical_table": "shops", "logical_table": "店舗マスタ", "physical_column": "",
             "logical_column": "店舗名称", "alias_name": "shop_name"},
            {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "kai_date",
             "logical_column": "会計日", "alias_name": "kai"},
            {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "",
             "logical_column": "最初の会計日", "alias_name": "first_date"},
        ],
        "db_operations": [{
            "id": "db_op_list_x", "api_endpoint": {"action": "list_x"},
            "dataset": [
                {"no": 1, "logical_table": "POS会計", "logical_column": "店舗ID", "physical_table": "pos",
                 "physical_column": "mise_no", "alias_name": "shop_id"},
                {"no": 2, "logical_table": "店舗マスタ", "logical_column": "店舗名称", "physical_table": "shops",
                 "physical_column": "", "alias_name": "shop_name"},
                {"no": 3, "logical_table": "POS会計", "logical_column": "日数", "physical_table": "pos",
                 "physical_column": "", "alias_name": "days"},
            ],
            "joins": [{"type": "LEFT JOIN", "logical_table": "shops", "condition": "s.id = p.mise_no"}],
            "where": [{"logical_table": "pos", "logical_column": "mise_no", "operator": "=",
                       "value": "shop_id", "when": "誤った when"}],
            "group_by": [{"logical_table": "pos", "logical_column": "mise_no"}],
            "order_by": [{"logical_table": "pos", "logical_column": "kai_date", "direction": "昇順"}],
            "aggregate": [
                {"function": "array_agg", "target": "CASE WHEN p.k > 0 THEN p.d END\n  ORDER BY p.d",
                 "filter": None, "alias": None},
                {"function": "count", "target": "*", "filter": None, "alias": "days"},
            ],
        }],
    }
    log = []
    _compose_db(out, drafts, log)
    ln = out["logical_names"]
    op = out["db_operations"][0]
    assert op["dataset"][0]["physical_column"] == "mise_no", op["dataset"][0]
    assert op["dataset"][1]["physical_column"] == "name", op["dataset"][1]
    assert op["dataset"][2]["physical_column"] == "" and op["dataset"][2]["alias_expression"] == "count(*)::bigint", op["dataset"][2]
    assert ln[1]["physical_column"] == "name" and ln[1].get("derived") is True, ln[1]
    assert "derived" not in ln[0], ln[0]
    assert ln[3]["physical_column"] == "kai_date" and ln[3].get("derived") is True, ln[3]
    assert op["where"][0]["when"] == "shop_id が指定されたとき", op["where"][0]
    assert op["where"][0]["logical_table"] == "POS会計(推定)" and op["where"][0]["logical_column"] == "店舗ID", op["where"][0]
    assert op["group_by"][0]["logical_column"] == "店舗ID", op["group_by"][0]
    assert op["order_by"][0]["logical_column"] == "会計日", op["order_by"][0]
    assert op["joins"][0]["logical_table"] == "店舗マスタ(推定)", op["joins"][0]
    assert op["joins"][0]["condition"] == "s.id = POS会計(推定).店舗ID", op["joins"][0]["condition"]
    assert op["aggregate"][0]["alias"] == "warn_dates" and op["aggregate"][1]["alias"] == "days"
    assert all(r["logical_table"].endswith("(推定)") for r in ln), ln
    # 2回目を流しても結果が変わらない(冪等)
    import copy
    snap = copy.deepcopy(out)
    _compose_db(out, drafts, [])
    assert out == snap, "not idempotent"
    # 下書きが無い op は、句の論理名・表名の(推定)だけ受ける(物理列・when は触らない)
    out2 = {"logical_names": [{"physical_table": "pos", "logical_table": "POS会計", "physical_column": "a",
                               "logical_column": "A"}],
            "db_operations": [{"id": "db_op_zz", "api_endpoint": {"action": "zz"},
                               "where": [{"logical_table": "pos", "logical_column": "a", "operator": "=",
                                          "value": "1", "when": "x"}]}]}
    _compose_db(out2, drafts, [])
    assert out2["db_operations"][0]["where"][0]["logical_column"] == "A"
    assert out2["db_operations"][0]["where"][0]["when"] == "x"
    # 同じ id の下書きが2件あるときは合成しない(推測しない)
    amb = [drafts[0], drafts[0]]
    log3 = []
    out3 = {"logical_names": [], "db_operations": [{"id": "db_op_list_x", "api_endpoint": {"action": "list_x"},
                                                  "aggregate": [{"function": "count", "target": "*", "alias": None}]}]}
    _compose_db(out3, amb, log3)
    assert out3["db_operations"][0]["aggregate"][0]["alias"] is None and log3, (out3, log3)
    # derived の行が先に来ても、句の列名の対応には使わない(kai_date が「最初の会計日」に化けない)
    out4 = {"logical_names": [
                {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "kai_date",
                 "logical_column": "最初の会計日", "alias_name": "first_date", "derived": True},
                {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "kai_date",
                 "logical_column": "会計日"}],
            "db_operations": [{"id": "db_op_q", "api_endpoint": {"action": "q"},
                               "where": [{"logical_table": "pos", "logical_column": "kai_date", "operator": ">=",
                                          "value": "x"}]}]}
    _compose_db(out4, [], [])
    assert out4["db_operations"][0]["where"][0]["logical_column"] == "会計日", out4["db_operations"][0]["where"]
    print("[selftest OK] _compose_db_facts (B2: 物理列・when・句の論理名・(推定)・集計の別名・derived の除外)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
