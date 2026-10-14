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
    """db_operation に対応する下書きの (db_op, api) を返す。決まらないときは (None, None) と WARN を残す。
    対応の鍵は _units.draft_db_op が一か所で決める(番号振りの前は id、後は legacy_id・action・表で結ぶ)。
    WHY: 同じ id や action の下書きが複数あると、どれの事実か決まらない。推測せず合成を止める。"""
    import _units  # 結び方を _units の1か所に置く。compose の中から呼ばれるので、読み込み順の循環は無い
    dop, a, note = _units.draft_db_op(op, drafts)
    if note:
        log.append(f"WARN _compose_db_facts: {op.get('id')} {note}、合成しない")
    return dop, a


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
    # WHY(V4): 同じ別名でも表が違えば別の列を指す(shops.name と pos_account_reports.mise_no)。
    #          行の表と下書き列の表(c["table"])が食い違うときは、行の表の列を当てない。
    #          下書き列の表が1つに決まるときは、行をその表へ付け替える(論理表は同じ表の他の行から取る)。
    lt_of = {}
    for row in out.get("logical_names") or []:  # fallback-ok: logical_names は任意の欄
        if row.get("physical_table") and row.get("logical_table"):  # fallback-ok: 欄が空の行は表の対応に使わない
            lt_of.setdefault(row["physical_table"], row["logical_table"])
    for row in out.get("logical_names") or []:  # fallback-ok: logical_names は任意の欄
        alias = row.get("alias_name")
        if row.get("physical_column") or not alias or not row.get("physical_table"):
            continue
        pt = row["physical_table"]
        cs = cand.get(alias, [])  # fallback-ok: 候補が無い別名は空
        same = [c for c in cs if c.get("table") in (pt, None, "")]  # fallback-ok: 表の無い下書き列は従来どおり数える
        if same:
            res = {_column_of(pt, c.get("source_columns")) for c in same}  # fallback-ok: 式の列は無いことがある
            if len(res) == 1 and None not in res:
                row["physical_column"] = res.pop()
                row["derived"] = True
                log.append(f"INFO _compose_db_facts: logical_names {pt}.{alias} を derived で補う")
            continue
        others = {c.get("table") for c in cs}  # fallback-ok: 候補が無ければ空
        if len(others) != 1:
            continue
        (t,) = others
        pcs = {_column_of(t, c.get("source_columns")) for c in cs}  # fallback-ok: 式の列は無いことがある
        if len(pcs) != 1 or None in pcs or t not in lt_of:
            log.append(f"WARN _compose_db_facts: logical_names {pt}.{alias} は下書きの表 {t} の列が1つに決まらない、または論理表が無いので付け替えない")
            continue
        row["physical_table"] = t
        row["logical_table"] = lt_of[t]
        row["physical_column"] = pcs.pop()
        row["derived"] = True
        log.append(f"INFO _compose_db_facts: logical_names {pt}.{alias} を下書きの表 {t}.{row['physical_column']} へ付け替える(derived)")


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


_EQ_JOIN = re.compile(r"^\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*$")


def _side(aliases, pt2lt, col, q, c):
    """別名.列 の片側を (物理表, 論理列) にする。表か列が決まらないときは (None, None)。"""
    pt = aliases.get(q)
    if not pt or pt not in pt2lt:
        return None, None
    return pt, col.get((pt, c))


def _translate_condition(cond, aliases, pt2lt, col, log=None):
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
    text = _QUAL.sub(rep, cond or "")  # fallback-ok: 条件が無い joins は対象外
    # WHY: 等結合 a.x = b.y で片側だけ論理列が決まるときは、同じ鍵を指すので、もう片側の表にも同じ論理名を付ける。
    #      残すと別名(s.id)が結合条件に残り、検査の[結合]で止まる。両側とも決まらないときは字面のまま残す。
    m = _EQ_JOIN.match(cond or "")  # fallback-ok: 等結合の形でない条件は片側推定しない
    if not m:
        return text
    p1, l1 = _side(aliases, pt2lt, col, m.group(1), m.group(2))
    p2, l2 = _side(aliases, pt2lt, col, m.group(3), m.group(4))
    if l1 is None and l2 and p1 in pt2lt:
        lc = l2
    elif l2 is None and l1 and p2 in pt2lt:
        lc = l1
    else:
        return text
    if log is not None:
        log.append(f"INFO _compose_db_facts: 結合条件の {m.group(1)}.{m.group(2)} の論理列を等結合の相手の「{lc}」で補う")
    return f"{_sfx(pt2lt[p1])}.{lc} = {_sfx(pt2lt[p2])}.{lc}"


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


_RENAME_SOURCES = ("docstring", "comment", "csv_header", "ui_label")


def _draft_names(drafts):
    """backend 下書きの列の論理名のうち、出所が事実(_RENAME_SOURCES)のものを 別名 -> 論理名 にする。
    WHY: 出所が推定(ui_label_guess・guess)の名前は寄せる根拠にしない。同じ別名の論理名が下書きごとに
         食い違うときは、どれが正しいか事実が決まらないので載せない。"""
    seen = {}
    for d in drafts:
        for a in (d.get("apis") or {}).values():  # fallback-ok: 下書きの apis は任意
            for dop in (a or {}).get("db_operations") or []:  # fallback-ok: 下書きの欄は任意
                for c in dop.get("columns") or []:  # fallback-ok: 列は任意
                    if c.get("logical_name_source") in _RENAME_SOURCES and c.get("alias_name") and c.get("logical_name"):  # fallback-ok: 欄が無い列は対象外
                        seen.setdefault(c["alias_name"], set()).add(c["logical_name"])
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def _rename_logical_names(out, drafts, log):
    """logical_names の論理名を、事実の出所の名前へ寄せ、同じ旧名を使う句・応答の表示名も同じ置換にする。
    WHY: H2 が付けた論理名と、ソースの型コメント・docstring の論理名が食い違うと、同じ列が表ごとに別名になる。
         旧名が別の別名にも付いているときは、どの列の置換か決まらないので触らない。"""
    names = _draft_names(drafts)
    lns = out.get("logical_names") or []  # fallback-ok: logical_names は任意
    aliases_of = {}
    for r in lns:
        if r.get("logical_column"):  # fallback-ok: 列名の無い行は対象外
            aliases_of.setdefault(r["logical_column"], set()).add(r.get("alias_name"))  # fallback-ok: 別名の無い行も数える
    renames = {}  # (論理表, 旧名) -> 新名
    for r in lns:
        alias, old = r.get("alias_name"), r.get("logical_column")  # fallback-ok: 欄が無い行は下で除く
        new = names.get(alias)
        if not old or not new or new == old:
            continue
        if len(aliases_of[old]) > 1:
            log.append(f"WARN _compose_db_facts: 論理名 {old} が複数の別名に付くため {new} へ寄せない({alias})")
            continue
        r["logical_column"] = new
        renames[(r.get("logical_table") or "", old)] = new  # fallback-ok: 表名の無い行は表を空として扱う
        log.append(f"INFO _compose_db_facts: 論理名 {old} を {new} へ寄せる({alias})")
    if not renames:
        return

    def _fix(rows):
        for row in rows or []:  # fallback-ok: 句は任意
            k = (row.get("logical_table") or "", row.get("logical_column") or "")  # fallback-ok: 欄が空の行は一致しない
            if k in renames:
                row["logical_column"] = renames[k]

    for op in out.get("db_operations") or []:  # fallback-ok: db_operations は任意
        for key in ("dataset", "where", "group_by", "order_by", "joins"):
            _fix(op.get(key))
    by_name = {old: new for (_t, old), new in renames.items()}
    for spec in out.get("api_spec") or []:  # fallback-ok: api_spec は任意
        for it in spec.get("response_items") or []:  # fallback-ok: 応答の項目は任意
            if it.get("display_name") in by_name:  # fallback-ok: 表示名の無い項目は対象外
                it["display_name"] = by_name[it["display_name"]]


def _bare(name):
    """論理名の末尾の「(推定)」を除く(名前の比較専用)。"""
    s = str(name or "")  # fallback-ok: None は空文字として比べる(比較専用)
    return s[:-len(SUFFIX)] if s.endswith(SUFFIX) else s


def _unify_same_expression(out, drafts, log):
    """backend 下書きの全 API の列を式(expression の空白を詰めたもの)で束ね、別名が2つ以上ある束は
    logical_names の論理名を1つに揃える。dataset・where・group_by・order_by の同じ別名の行も同じ名前にする。
    呼出元: _compose_db(_rename_logical_names の後、論理表名へ「(推定)」を付ける前)。
    WHY(W5/(d)): 同じ式の列が API ごとに別名(shop_name と name)で出ると、H2 が別々の論理名(店舗名称・名称)を付け、
         同じ列が帳票の中で2通りに見える。式が同じなら同じ列なので、論理名を1つに寄せる。
    選び方: 出所が事実(_RENAME_SOURCES)の名前があればそれ(複数なら決まらない)。無ければ、一方が他方を末尾に含むとき
         長い方(店舗名称 ⊃ 名称)。どちらでもなければ揃えず WARN。
    やらない事: 式が違う列(min(kai_date) と max(kai_date))は束ねない。物理名は触らない。H2 の行は消さない。"""
    bundles = {}  # 空白を詰めた式 -> {別名: [下書き列, ...]}
    for d in drafts:
        for a in (d.get("apis") or {}).values():  # fallback-ok: 下書きの apis は任意
            for dop in (a or {}).get("db_operations") or []:  # fallback-ok: 下書きの欄は任意
                for c in dop.get("columns") or []:  # fallback-ok: 列は任意
                    ex, al = _nows(c.get("expression")), c.get("alias_name")
                    if ex and al:
                        bundles.setdefault(ex, {}).setdefault(al, []).append(c)
    lns = out.get("logical_names") or []  # fallback-ok: logical_names は任意
    if not lns:
        return
    # 旧名が複数の別名に付いているときは、句の行の置換を表と旧名だけでは決められない(_rename_logical_names と同じ配慮)
    aliases_of = {}
    for r in lns:
        if r.get("logical_column"):  # fallback-ok: 列名の無い行は対象外
            aliases_of.setdefault((r.get("logical_table") or "", _bare(r["logical_column"])), set()).add(r.get("alias_name"))  # fallback-ok: 表・別名の無い行も数える
    renames = []  # (別名, 論理表, 旧名(推定なし), 新名)
    for ex, by_alias in sorted(bundles.items()):
        if len(by_alias) < 2:
            continue
        rows = [r for r in lns if r.get("alias_name") in by_alias and r.get("logical_column")]  # fallback-ok: 行が無い別名は対象外
        raw = {r["logical_column"] for r in rows}
        names = sorted({_bare(x) for x in raw})
        if len(names) < 2:
            continue  # 行が無い・既に揃っている
        facts = {c.get("logical_name") for cs in by_alias.values() for c in cs
                 if c.get("logical_name_source") in _RENAME_SOURCES and c.get("logical_name")}  # fallback-ok: 出所の無い列は事実にしない
        if len(facts) == 1:
            pick = _bare(next(iter(facts)))
        elif len(facts) > 1:
            log.append(f"WARN _compose_db_facts: 式 {ex} の別名 {sorted(by_alias)} は事実の論理名が複数あり {sorted(facts)}、揃えない")
            continue
        else:
            longest = max(names, key=len)
            if all(longest.endswith(n) for n in names):
                pick = longest
            else:
                log.append(f"WARN _compose_db_facts: 式 {ex} の別名 {sorted(by_alias)} の論理名 {names} は包含関係に無く、揃えない")
                continue
        # 「(推定)」の有無は、選んだ名前を持つ行に合わせる(事実の名前には付けない)
        new = pick + SUFFIX if (pick + SUFFIX) in raw else pick
        for r in rows:
            if r["logical_column"] != new:
                old = _bare(r["logical_column"])
                renames.append((r.get("alias_name"), r.get("logical_table") or "", old, new))  # fallback-ok: 表名の無い行は表を空として扱う
                r["logical_column"] = new
                log.append(f"INFO _compose_db_facts: 同じ式 {ex} の列の論理名 {old} を {new} へ揃える({r.get('alias_name')})")
    if not renames:
        return
    for op in out.get("db_operations") or []:  # fallback-ok: db_operations は任意
        for al, lt, old, new in renames:
            for row in op.get("dataset") or []:  # fallback-ok: dataset は任意
                if row.get("alias_name") == al and _bare(row.get("logical_column")) == old:
                    row["logical_column"] = new
            if len(aliases_of.get((lt, old), ())) != 1:  # fallback-ok: 旧名が複数の別名に付くときは句の行を触らない
                continue
            for key in ("where", "group_by", "order_by"):
                for row in op.get(key) or []:  # fallback-ok: 句は任意
                    if (row.get("logical_table") or "") == lt and _bare(row.get("logical_column")) == old:  # fallback-ok: 欄が空の行は一致しない
                        row["logical_column"] = new


def _ws(s):
    """空白を1つに詰めて小文字にする(字面の比較専用)。"""
    return " ".join(str(s or "").split()).lower()  # fallback-ok: None は空文字として比べる(比較専用)


def _api_draft(drafts, action, log):
    """action の下書きの API(apis[action])を取る。下書きが複数あって決まらないときは None(推測しない)。
    WHY: 合成の db_operations の id(DB1 等)は下書きの id(db_op_…)と一致しないことがあるため、id ではなく API の action で結ぶ(run10 で確認)。"""
    found = [a for a in ((d.get("apis") or {}).get(action) for d in drafts) if action and a]  # fallback-ok: 下書きの apis は任意
    if len(found) > 1:
        log.append(f"WARN _compose_db_facts: {action} の下書きが {len(found)} 件あり、副問い合わせの集計の除外を行わない")
        return None
    return found[0] if found else None


def _drop_subquery_aggs(op, api, log):
    """aggregate の行のうち、下書きの副問い合わせ(subqueries の定義文)に同じ集計があり、主の SELECT の集計(aggregate_units の scope が main)に無いものを外す(F2)。
    呼出元: _compose_db(_fill_agg の前)。意味合い: 副問い合わせの max(...) を外側の集計として書くと、外側の単位の集計に化ける。
    モデルが入れても、下書きの事実(副問い合わせの定義文)で消す。主の SELECT にも同じ字面の集計があるときは残す(別の集計として使われている)。
    api は下書きの API 1件(apis[action])。その中の全 db_operations の副問い合わせ・単位を見る。"""
    dops = [x for x in api.get("db_operations") or [] if isinstance(x, dict)]  # fallback-ok: 下書きの欄は任意
    subs = " ".join(_ws(s.get("definition")) for d in dops for s in d.get("subqueries") or [] if isinstance(s, dict))  # fallback-ok: 副問い合わせが無ければ空
    if not subs:
        return
    main = {(_ws(u.get("func")), _ws(u.get("arg"))) for d in dops for u in d.get("aggregate_units") or []  # fallback-ok: 下書きの単位は任意
            if str(u.get("scope") or "").startswith("main")}  # fallback-ok: scope の無い単位は主の集計として数えない
    keep = []
    for row in op.get("aggregate") or []:  # fallback-ok: aggregate は任意
        f, t = _ws(row.get("function")), _ws(row.get("target"))  # fallback-ok: 関数名・式の無い行は一致しない
        pat = r"(?<![\w.])" + re.escape(f) + r"\s*\(\s*" + re.escape(t) + r"\s*\)(?![\w.])"
        if f and re.search(pat, subs) and (f, t) not in main:
            log.append(f"INFO _compose_db_facts: aggregate {f}({t}) は副問い合わせの中の集計なので外す({op.get('id')})")
            continue
        keep.append(row)
    if len(keep) != len(op.get("aggregate") or []):
        op["aggregate"] = keep


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
                row["condition"] = _translate_condition(row["condition"], aliases, pt2lt, col, log)
    _rename_logical_names(out, drafts, log)         # c0) 論理名を事実の出所の名前へ寄せる(下の(推定)の前)
    _unify_same_expression(out, drafts, log)        # c0b) 同じ式の列の論理名を1つに揃える(W5。(推定)の前)
    for r in lns:                                   # c) 論理表名の「(推定)」
        if r.get("logical_table"):  # fallback-ok: 表名の無い行は対象外
            r["logical_table"] = _sfx(r["logical_table"])
    for op in ops:
        for key in ("dataset", "where", "group_by", "order_by", "joins"):
            for row in op.get(key) or []:  # fallback-ok: 任意の句
                if row.get("logical_table"):  # fallback-ok: 表名の無い行は対象外
                    row["logical_table"] = _sfx(row["logical_table"])
    for op in ops:                                  # d0) 副問い合わせの中の集計は外側から外す(下書きの subqueries が正。API の action で結ぶ)
        ep = op.get("api_endpoint")  # fallback-ok: api_endpoint は任意
        api = _api_draft(drafts, ep.get("action") if isinstance(ep, dict) else None, log)  # fallback-ok: 形の崩れた endpoint は結べない
        if api:
            _drop_subquery_aggs(op, api, log)
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
    import copy
    raw = copy.deepcopy(out)  # 番号振りの後の形を作るため、合成の前の入力を残す
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
    # WHY: 店舗マスタ側の列(shops.id)は論理名が無い。等結合の相手(店番号)の論理名を借りる。別名 s. は残さない
    assert op["joins"][0]["condition"] == "店舗マスタ(推定).店舗ID = POS会計(推定).店舗ID", op["joins"][0]["condition"]
    # 等結合の片側だけ決まる → 相手の論理名。両側とも決まらない・表が不明 → 字面のまま。等結合でない → 片側推定しない
    tl, ptl, cl = {"p": "pos", "s": "shops"}, {"pos": "POS会計", "shops": "店舗マスタ"}, {("pos", "mise_no"): "店番号"}
    assert _translate_condition("s.id = p.mise_no", tl, ptl, cl) == "店舗マスタ(推定).店番号 = POS会計(推定).店番号"
    assert _translate_condition("s.id = x.mise_no", tl, ptl, cl) == "s.id = x.mise_no"
    assert _translate_condition("s.id > p.mise_no", tl, ptl, cl) == "s.id > POS会計(推定).店番号"
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
    # T17: 論理名を事実の出所の名前へ寄せる(docstring・ui_label)。推定の出所は寄せない。旧名が複数の別名に付くときは触らない
    drafts2 = [{"apis": {"x": {"db_operations": [{"id": "d1", "columns": [
        {"alias_name": "shop_id", "logical_name": "店番号", "logical_name_source": "docstring"},
        {"alias_name": "uri_cnt", "logical_name": "売上訂正点数", "logical_name_source": "ui_label"},
        {"alias_name": "amb", "logical_name": "別名X", "logical_name_source": "comment"},
        {"alias_name": "guess_col", "logical_name": "推定名(推定)", "logical_name_source": "ui_label_guess"},
    ]}]}}}]
    out5 = {"logical_names": [
        {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "mise_no", "logical_column": "店舗ID", "alias_name": "shop_id"},
        {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "uri_cnt", "logical_column": "売上訂正件数", "alias_name": "uri_cnt"},
        {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "amb", "logical_column": "旧名", "alias_name": "amb"},
        {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "g", "logical_column": "旧名", "alias_name": "other"},
        {"physical_table": "pos", "logical_table": "POS会計", "physical_column": "gc", "logical_column": "推定元", "alias_name": "guess_col"},
    ], "db_operations": [{"id": "x_op", "dataset": [
        {"logical_table": "POS会計", "logical_column": "店舗ID", "alias_name": "shop_id"},
        {"logical_table": "POS会計", "logical_column": "売上訂正件数", "alias_name": "uri_cnt"},
    ], "where": [{"logical_table": "POS会計", "logical_column": "旧名"}]}],
        "api_spec": [{"response_items": [{"display_name": "店舗ID"}, {"display_name": "売上訂正件数"}]}]}
    log5 = []
    _rename_logical_names(out5, drafts2, log5)
    ln5 = out5["logical_names"]
    assert ln5[0]["logical_column"] == "店番号", ln5[0]
    assert ln5[1]["logical_column"] == "売上訂正点数", ln5[1]
    assert ln5[2]["logical_column"] == "旧名" and ln5[3]["logical_column"] == "旧名", (ln5[2], ln5[3])
    assert ln5[4]["logical_column"] == "推定元", ln5[4]
    op5 = out5["db_operations"][0]
    assert op5["dataset"][0]["logical_column"] == "店番号" and op5["dataset"][1]["logical_column"] == "売上訂正点数", op5["dataset"]
    assert op5["where"][0]["logical_column"] == "旧名", op5["where"]
    assert out5["api_spec"][0]["response_items"][0]["display_name"] == "店番号", out5["api_spec"]
    assert out5["api_spec"][0]["response_items"][1]["display_name"] == "売上訂正点数", out5["api_spec"]
    assert log5 and any("WARN" in x for x in log5), log5
    # F2: 副問い合わせの中の集計(max(kai_date))は、モデルが aggregate に入れても外す。主の SELECT の同じ字面の集計(max(kai_date))は残す
    sub_drafts = [{"apis": {"lp": {"db_operations": [{"id": "db_op_lp",
        "subqueries": [{"location": "WHERE", "definition": "SELECT max(kai_date) FROM pos_account_reports", "filtered": False}],
        "aggregate_units": [{"func": "max", "arg": "p.kai_date", "alias": "last_date", "scope": "main"},
                            {"func": "max", "arg": "kai_date", "alias": "", "scope": "subquery WHERE"}]}]},
        "lq": {"db_operations": [{"id": "db_op_lq",
        "subqueries": [{"location": "WHERE", "definition": "SELECT max(kai_date)  FROM pos_account_reports", "filtered": False}],
        "aggregate_units": [{"func": "max", "arg": "kai_date", "alias": "same", "scope": "main"}]}]}}}]
    out6 = {"logical_names": [], "db_operations": [
        {"id": "DB9", "api_endpoint": {"action": "lp"},  # 合成の id(DB9)は下書きの id(db_op_lp)と違う。action で結ぶ
         "aggregate": [{"function": "max", "target": "p.kai_date", "alias": "last_date", "scope": "main"},
                       {"function": "max", "target": "kai_date", "alias": None, "scope": "main"}]},
        {"id": "DB10", "api_endpoint": {"action": "lq"},
         "aggregate": [{"function": "max", "target": "kai_date", "alias": None, "scope": "main"}]}]}
    log6 = []
    _compose_db(out6, sub_drafts, log6)
    got6 = [[(r["function"], r["target"]) for r in op["aggregate"]] for op in out6["db_operations"]]
    assert got6 == [[("max", "p.kai_date")], [("max", "kai_date")]], got6  # db_op_lq は主の SELECT にも同じ集計があるので残す
    assert any("副問い合わせ" in x and "max(kai_date)" in x for x in log6), log6
    # 番号振りの後の id でも、番号振りの前と同じ事実が入る(WHY: 番号振り後は id が DB1 等になり、id の一致だけでは下書きに結べない)。
    # 旧 id が legacy_id に残る形(id だけ番号)と、legacy_id が無い形(action の1件で結ぶ)の両方を見る。
    strip = lambda o: {k: v for k, v in o.items() if k not in ("id", "legacy_id")}
    for extra in ({"legacy_id": "db_op_list_x"}, {}):
        num = copy.deepcopy(raw)
        num["db_operations"][0].update({"id": "DB1", **extra})
        _compose_db(num, drafts, [])
        assert strip(num["db_operations"][0]) == strip(op), (extra, num["db_operations"][0], op)
        assert num["logical_names"] == out["logical_names"], extra
    # V4: 論理名の別名が下書き列の表と違う行は、行の表の列を当てず、下書き列の表へ付け替える(derived)
    # WHY: 同じ別名 name が shops.name(店舗名)と pos_account_reports.mise_no を指すと、行の表だけで列を決めると mise_no に化ける
    v4_drafts = {"apis": {"list_y": {"db_operations": [{"id": "db_op_y", "columns": [
        {"alias_name": "name", "table": "shops", "source_columns": ["shops.name", "pos_account_reports.mise_no"]},
        {"alias_name": "kai", "table": "pos_account_reports", "source_columns": ["pos_account_reports.kai_date"]},
        {"alias_name": "zz", "table": "stores", "source_columns": ["stores.code", "pos_account_reports.mise_no"]},
    ]}]}}}
    v4_dop = v4_drafts["apis"]["list_y"]["db_operations"][0]
    # 同じ別名 two が2つの op の下書きで別の表を指す(候補が割れる)
    v4_dop2 = {"id": "db_op_y2", "columns": [{"alias_name": "two", "table": "shops", "source_columns": ["shops.name"]}]}
    v4_dop3 = {"id": "db_op_y3", "columns": [{"alias_name": "two", "table": "stores", "source_columns": ["stores.code"]}]}
    v4_out = {"logical_names": [
        {"physical_table": "shops", "logical_table": "店舗", "physical_column": "", "logical_column": "店舗名称", "alias_name": "shop_name"},
        {"physical_table": "pos_account_reports", "logical_table": "POS会計レポート", "physical_column": "", "logical_column": "名称(推定)", "alias_name": "name"},
        {"physical_table": "pos_account_reports", "logical_table": "POS会計レポート", "physical_column": "", "logical_column": "会計日(推定)", "alias_name": "kai"},
        {"physical_table": "pos_account_reports", "logical_table": "POS会計レポート", "physical_column": "", "logical_column": "別の値", "alias_name": "zz"},
        {"physical_table": "pos_account_reports", "logical_table": "POS会計レポート", "physical_column": "", "logical_column": "二つ", "alias_name": "two"},
        {"physical_table": "pos_account_reports", "logical_table": "POS会計レポート", "physical_column": "", "logical_column": "候補なし", "alias_name": "nothing"},
    ]}
    v4_log = []
    _fill_physical(v4_out, [({"id": "db_op_y", "dataset": []}, v4_dop, None),
                            ({"id": "db_op_y2", "dataset": []}, v4_dop2, None),
                            ({"id": "db_op_y3", "dataset": []}, v4_dop3, None)], v4_log)
    v4_ln = v4_out["logical_names"]
    assert (v4_ln[1]["physical_table"], v4_ln[1]["physical_column"], v4_ln[1]["logical_table"]) == ("shops", "name", "店舗"), v4_ln[1]
    assert v4_ln[1].get("derived") is True and v4_ln[1]["logical_column"] == "名称(推定)", v4_ln[1]
    assert (v4_ln[2]["physical_table"], v4_ln[2]["physical_column"]) == ("pos_account_reports", "kai_date"), v4_ln[2]
    assert v4_ln[2].get("derived") is True, v4_ln[2]
    # 下書き列の表の論理表が無い(stores)・候補の表が2つ(two)・候補が無い(nothing)は触らない
    for i in (3, 4, 5):
        assert v4_ln[i]["physical_table"] == "pos_account_reports" and v4_ln[i]["physical_column"] == "" and "derived" not in v4_ln[i], v4_ln[i]
    assert any("付け替え" in x and "name" in x for x in v4_log), v4_log
    assert any("WARN" in x and "zz" in x for x in v4_log), v4_log
    # W5: 2つの API の同じ式の列(shop_name=店舗名称・name=名称)は長い方へ揃え、式の違う列(first_date・last_date)は変えない
    w5_col = lambda al, ex, ln, src: {"alias_name": al, "expression": ex, "logical_name": ln, "logical_name_source": src}
    w5_cols1 = [w5_col("shop_name", "COALESCE(s.name, 'x')", "店舗名称", "guess"), w5_col("first_date", "min(p.kai_date)", "", "")]  # fallback-ok: テスト用の列の入力(既定値ではない)
    w5_cols2 = [w5_col("name", "COALESCE(s.name,  'x')", "名称", "guess"), w5_col("last_date", "max(p.kai_date)", "", "")]  # fallback-ok: テスト用の列の入力(既定値ではない)
    w5_drafts = [{"apis": {"a1": {"db_operations": [{"id": "d1", "columns": w5_cols1}]},
                           "a2": {"db_operations": [{"id": "d2", "columns": w5_cols2}]}}}]
    w5_ln = lambda al, lc: {"physical_table": "shops", "logical_table": "店舗マスタ", "physical_column": "", "logical_column": lc, "alias_name": al}
    w5_out = {"logical_names": [w5_ln("shop_name", "店舗名称(推定)"), w5_ln("name", "名称(推定)"),
                                w5_ln("first_date", "最初の日"), w5_ln("last_date", "最後の日")],
              "db_operations": [{"id": "x", "dataset": [{"alias_name": "name", "logical_table": "店舗マスタ", "logical_column": "名称(推定)"}],
                                 "order_by": [{"logical_table": "店舗マスタ", "logical_column": "名称(推定)"}]}]}
    w5_log = []
    _unify_same_expression(w5_out, w5_drafts, w5_log)
    assert [r["logical_column"] for r in w5_out["logical_names"]] == ["店舗名称(推定)", "店舗名称(推定)", "最初の日", "最後の日"], w5_out["logical_names"]
    assert w5_out["db_operations"][0]["dataset"][0]["logical_column"] == "店舗名称(推定)", w5_out["db_operations"][0]
    assert w5_out["db_operations"][0]["order_by"][0]["logical_column"] == "店舗名称(推定)", w5_out["db_operations"][0]
    snap5 = copy.deepcopy(w5_out)
    _unify_same_expression(w5_out, w5_drafts, [])
    assert w5_out == snap5, "W5 not idempotent"
    # 包含関係に無い(別々の名前)ときは揃えず WARN。事実の出所の名前があればそれに揃える
    w5_out2 = {"logical_names": [w5_ln("shop_name", "店舗名称"), w5_ln("name", "名前")]}
    w5_log2 = []
    _unify_same_expression(w5_out2, w5_drafts, w5_log2)
    assert [r["logical_column"] for r in w5_out2["logical_names"]] == ["店舗名称", "名前"] and any("WARN" in x for x in w5_log2), (w5_out2, w5_log2)
    w5_fact = copy.deepcopy(w5_drafts)
    w5_fact[0]["apis"]["a2"]["db_operations"][0]["columns"][0].update({"logical_name": "店名", "logical_name_source": "docstring"})
    _unify_same_expression(w5_out2, w5_fact, [])
    assert [r["logical_column"] for r in w5_out2["logical_names"]] == ["店名", "店名"], w5_out2
    print("[selftest OK] _compose_db_facts W5 (同じ式の列の論理名を揃える・式が違う列は変えない・決まらないときは WARN)")
    print("[selftest OK] _compose_db_facts V4 (別名の表が違う行は下書き列の表へ付け替える・論理表が無い・候補が割れるときは触らない)")
    print("[selftest OK] _compose_db_facts 番号振りの後の id でも同じ事実(legacy_id・action で結ぶ)")
    print("[selftest OK] _compose_db_facts F2 (副問い合わせの集計を外す・主の集計は残す)")
    print("[selftest OK] _compose_db_facts T17 (論理名の事実への寄せ・推定は寄せない・旧名の衝突は触らない)")
    print("[selftest OK] _compose_db_facts (B2: 物理列・when・句の論理名・(推定)・集計の別名・derived の除外)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
