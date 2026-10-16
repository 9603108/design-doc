#!/usr/bin/env python3
"""同じ画面の2つの run の中間 JSON を突き合わせ、値が揺れた欄と、増えた行・減った行を一覧にする。

WHY: 回し直しで値が揺れる欄は、LLM に書かせている限り毎回ぶれる。揺れた欄を担当の単位つきで
一覧にし、スクリプトで決められる欄(合成・変換の段へ移す候補)を見つけるための道具。
正解は常にソースで、どちらの run も正解扱いしない(ここは差を出すだけで、どちらが正しいかは決めない)。

突き合わせの考え方:
  - 配列の行は添字(位置)でなく、行の識別子(ROW_KEYS)で突き合わせる。1行増えても後ろがずれず、
    「増えた行」1件として数える(位置で突き合わせると、後ろの行が全部「揺れ」になっていた)。
  - 識別子が無い並び(steps・文字列の配列など)は、SEQ_SIG の印で並びを合わせてから、
    残りを位置の順に対にする(difflib)。
  - 同じ識別子が並ぶときは、出現の順(#2, #3)で区別する。
  - units.json の owns に無い欄はスクリプトが作る欄として「スクリプト」に分ける(担当不明にしない)。
  - 採番 ID・legacy_*・タイムスタンプ類は比べる価値が低いので数えない(IGNORE_FIELDS ほか)。

使い方: python3 diff_runs.py <A.json> <B.json>   /   python3 diff_runs.py --selftest
出力: 先頭に単位ごとの件数(欄の揺れ・増えた行・減った行)、次に揺れの大きい欄、
      増えた行・減った行・揺れた欄の一覧、末尾に単位ごとの「スクリプトで埋める候補」。
終了コード: 比較できれば 0(差があっても 0)。読めない・JSON でないときは 2。

接続: 単位の引き方は run_units.owner_of と load_units(units.json の owns)を再利用する(分担を二重に持たない)。
入力は target/runs/<run名>/ に控えた中間 JSON(run_units.py save-run が作る)を想定する。標準ライブラリのみ。
"""
import difflib
import json
import sys
from pathlib import Path

from run_units import load_units, owner_of

# WHY: 値の表示は1欄あたりこの長さで切り、一覧を目で追えるようにする(比較は全文で行う)
SHOW_MAX = 80
# WHY: 識別子の表示は短く切る(突き合わせは全文で行う)
LABEL_MAX = 24
# WHY: 欄が片方の run に無いことを、値 null と区別して表すための印(JSON の値としては現れない文字列)
MISSING = "<欄なし>"
# WHY: units.json の owns に無い欄は、LLM の担当ではなくスクリプトが作る欄なので、担当不明と分ける
SCRIPT = "スクリプト"
# WHY: 揺れの大きい欄として何件並べるか
TOP_N = 10

# 配列(道筋は添字なしの形)ごとの行の識別子。先頭から、いずれかの欄が空でない最初の組を使う。
# 欄名の . は入れ子(api_endpoint.action)。採番 ID(no・IT44・DB1 など)は識別子に使わない。
ROW_KEYS = {
    "meta.backend_files": [("path",)],
    "meta.frontend_files": [("path",)],
    "logical_names": [("physical_table", "physical_column", "alias_name")],
    "screen_layout.items": [("item_name",)],
    "screen_layout.areas": [("area_name",)],
    "initialization.items": [("item_name",)],
    "events": [("legacy_event_code",), ("action", "location")],
    "db_operations": [("api_endpoint.action",)],
    "db_operations[].dataset": [("physical_table", "physical_column", "alias_name")],
    "db_operations[].joins": [("logical_table",)],
    "db_operations[].where": [("logical_table", "logical_column", "operator", "param_name")],
    "db_operations[].group_by": [("logical_table", "logical_column")],
    "db_operations[].aggregate": [("alias",)],
    "db_operations[].order_by": [("logical_table", "logical_column")],
    "db_operations[].ctes": [("name",)],
    "db_operations[].subqueries": [("location",)],
    "parameters": [("name",)],
    "validations": [("field", "rule")],
    "messages": [("message_id",)],
    "calculations": [("name",)],
    "calculations[].inputs": [("name",)],
    "calculations[].outputs": [("name",)],
    "api_spec": [("action",)],
    "api_spec[].request_params": [("name",)],
    "api_spec[].response_items": [("key",)],
    "api_spec[].response_to_screen_mapping": [("api_field_path", "screen_item_name")],
    "api_spec[].error_patterns": [("status_code", "error_code")],
    "common_logic": [("name",)],
    "trigger_groups": [("kind", "category")],
    "trigger_groups[].triggers": [("location", "action")],
    "front_processes": [("name",)],
    "front_processes[].steps[].response_mapping.mappings": [("api_field_path", "screen_item_name")],
    "backend_processes": [("endpoint.action",)],
    "backend_processes[].called_by_front_processes": [("action",)],
    "diagrams": [("id",)],
    "diagrams[].spec.nodes": [("id",)],
    "diagrams[].spec.edges": [("from", "to")],
    "diagrams[].spec.structure": [("label",)],
    "diagrams[].spec.structure[].children": [("label",)],
    "diagrams[].spec.structure[].children[].children": [("label",)],
}

# 識別子の無い並び(順序に意味がある行)の、並びを合わせるための印。印が同じ行どうしを対にし、
# 印が違う区間は位置の順に対にする。ここに無い dict の行は、行の内容全体を印にする。
SEQ_SIG = {
    "event_processes": ("event_ref",),
    "front_processes[].steps": ("kind",),
    "front_processes[].called_by_triggers": ("group_kind", "group_category"),
    "backend_processes[].processing": ("kind",),
    "common_logic[].processing_steps": ("kind",),
}
# src(根拠のソース位置)は、行番号がずれても同じファイルの行どうしを対にする
SRC_SIG = ("file",)

# 道筋の別名: 同じ形の配列を、親が違っても同じ識別子の定義で引く(ROW_KEYS・SEQ_SIG の引き用)
PATH_ALIASES = [
    ("backend_processes[].processing[].db_op_detail", "db_operations[]"),
    ("backend_processes[].response_spec.items", "api_spec[].response_items"),
    ("backend_processes[].request_spec.params", "api_spec[].request_params"),
    ("backend_processes[].error_patterns", "api_spec[].error_patterns"),
]

# 比べない欄(欄名で見る): 採番 ID・他の欄への参照 ID・タイムスタンプ類
IGNORE_FIELDS = {
    "no", "step_no", "process_id", "trigger_no", "area_no", "screen_no", "item_ref_no", "screen_area_no",
    "param_no", "event_ref", "area_ref", "backend_id", "then_call", "generated_at",
    "db_operations_ref", "parameter_refs", "validation_refs", "calculation_refs", "common_logic_refs",
    "calls", "called_by_front", "used_by_processes_ids", "trigger_events", "related_db_operations",
}
# 道筋まで見て比べない欄(欄名だけでは他の欄を巻き込むもの)
IGNORE_PATHS = {"db_operations[].id"}


def _ignored(name, gpath):
    """比べない欄か。legacy_* と *_at(タイムスタンプ)も除く。"""
    return (name in IGNORE_FIELDS or name.startswith("legacy_") or name.endswith("_at")
            or gpath in IGNORE_PATHS)


def _norm(gpath):
    """識別子の定義を引くための道筋(別名を本来の道筋にそろえる)。"""
    for src, dst in PATH_ALIASES:
        if gpath == src or gpath.startswith(src + ".") or gpath.startswith(src + "["):
            return dst + gpath[len(src):]
    return gpath


def _get(row, dotted):
    cur = row
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _text(v):
    """識別子・印に使う文字列。空・None は空文字(識別子としては使えない)。"""
    if v is None or v == "" or v == [] or v == {}:
        return ""
    if isinstance(v, list):
        return "/".join(_text(x) for x in v)
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)


def _strip(obj):
    """比べない欄を取り除いた写し(内容全体を印にするとき用)。"""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if not _ignored(k, "")}
    if isinstance(obj, list):
        return [_strip(x) for x in obj]
    return obj


def _row_key(row, gpath):
    if not isinstance(row, dict):
        return None
    for fields in ROW_KEYS.get(_norm(gpath), []):
        vals = tuple(_text(_get(row, f)) for f in fields)
        # WHY: 一部の欄が空でも(alias_name が無い列など)、残りの欄で識別できれば使う
        if any(vals):
            return vals
    return None


def _sig(row, gpath):
    """並びを合わせる印(ハッシュできる値)。"""
    n = _norm(gpath)
    spec = SEQ_SIG.get(n) or (SRC_SIG if n.endswith("src") else None)
    if isinstance(row, dict) and spec:
        return tuple(_text(_get(row, f)) for f in spec)
    return json.dumps(_strip(row), ensure_ascii=False, sort_keys=True)


def _short(s, n=LABEL_MAX):
    return s if len(s) <= n else s[:n] + "…"


def _align(la, lb, gpath):
    """2つの配列の行を対にする。([(ia, ib, 表示名)], [(ia, 表示名)], [(ib, 表示名)]) を返す。"""
    if _norm(gpath) in ROW_KEYS:
        return _align_keyed(la, lb, gpath)
    return _align_seq(la, lb, gpath)


def _keyed(rows, gpath):
    seen, out = {}, []
    for i, r in enumerate(rows):
        k = _row_key(r, gpath)
        if k is None:
            # 識別子が取れない行は、行の内容全体を識別子にする(同じ内容なら対になる)
            k = ("\0", _sig(r, gpath))
        n = seen.get(k, 0)
        seen[k] = n + 1
        name = "|".join(_short(x) for x in k) if k[0] != "\0" else "(識別子なし)"
        out.append(((k, n), i, name + (f"#{n + 1}" if n else "")))
    return out


def _align_keyed(la, lb, gpath):
    ka, kb = _keyed(la, gpath), _keyed(lb, gpath)
    index_b = {k: (i, name) for k, i, name in kb}
    pairs, removed, used = [], [], set()
    for k, ia, name in ka:
        if k in index_b:
            ib = index_b[k][0]
            pairs.append((ia, ib, name))
            used.add(ib)
        else:
            removed.append((ia, name))
    added = [(i, name) for k, i, name in kb if i not in used]
    return pairs, removed, added


def _align_seq(la, lb, gpath):
    sa, sb = [_sig(r, gpath) for r in la], [_sig(r, gpath) for r in lb]
    pairs, removed, added = [], [], []
    for op, a0, a1, b0, b1 in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes():
        if op == "equal":
            pairs += [(a0 + k, b0 + k, "") for k in range(a1 - a0)]
            continue
        m = min(a1 - a0, b1 - b0)
        pairs += [(a0 + k, b0 + k, "") for k in range(m)]
        removed += [(i, "") for i in range(a0 + m, a1)]
        added += [(i, "") for i in range(b0 + m, b1)]
    pairs = [(ia, ib, str(ia) if ia == ib else f"{ia}→{ib}") for ia, ib, _ in pairs]
    removed = [(i, str(i)) for i, _ in removed]
    added = [(i, str(i)) for i, _ in added]
    return pairs, removed, added


class _Collector:
    def __init__(self, units):
        self.units = units
        self.fields, self.added, self.removed = [], [], []
        self._cache = {}

    def unit_of(self, gpath):
        if gpath not in self._cache:
            self._cache[gpath] = owner_of(gpath, self.units) or self._majority(gpath) or SCRIPT
        return self._cache[gpath]

    def _majority(self, gpath):
        """行そのものの担当: その配下の欄を最も多く owns に持つ単位(owns に直接当たらない配列の行用)。"""
        base = gpath.replace("[]", "")
        count = {}
        for u, spec in self.units.items():
            for o in spec["owns"]:
                if o.replace("[]", "").startswith(base + "."):
                    count[u] = count.get(u, 0) + 1
        return max(sorted(count), key=lambda u: count[u]) if count else None

    def field(self, gpath, label, a, b):
        self.fields.append({"path": gpath, "label": label, "a": a, "b": b, "unit": self.unit_of(gpath)})

    def row(self, bucket, gpath, label, value):
        bucket.append({"path": gpath, "label": label, "value": value, "unit": self.unit_of(gpath)})


def _cmp(a, b, gpath, label, st):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            cpath = f"{gpath}.{k}" if gpath else str(k)
            if _ignored(k, cpath):
                continue
            clabel = f"{label}.{k}" if label else str(k)
            if k not in a or k not in b:
                st.field(cpath, clabel, a.get(k, MISSING), b.get(k, MISSING))
            else:
                _cmp(a[k], b[k], cpath, clabel, st)
    elif isinstance(a, list) and isinstance(b, list):
        epath = gpath + "[]"
        pairs, removed, added = _align(a, b, gpath)
        for ia, ib, name in pairs:
            _cmp(a[ia], b[ib], epath, f"{label}[{name}]", st)
        for ia, name in removed:
            st.row(st.removed, epath, f"{label}[{name}]", a[ia])
        for ib, name in added:
            st.row(st.added, epath, f"{label}[{name}]", b[ib])
    elif a != b or type(a) is not type(b):
        st.field(gpath, label, a, b)


def diff_runs(a, b, units):
    """2つの JSON(読み込み済み)を比べ、{"fields": 揺れた欄, "added": 増えた行, "removed": 減った行} を返す。

    fields の各要素は {path(添字なしの道筋), label(行の識別子つきの道筋), a, b, unit}。
    added・removed は {path, label, value(行の中身), unit}。added は B にだけある行、removed は A にだけある行。
    片方に無い欄の値は None でなく専用の印(MISSING)で区別し、「null と書いた」と「欄が無い」を混ぜない。
    unit は run_units.owner_of が units の owns から引いた単位で、引けなければ SCRIPT。
    """
    st = _Collector(units)
    _cmp(a, b, "", "", st)
    return {"fields": st.fields, "added": st.added, "removed": st.removed}


def summarize(res):
    """単位ごとの件数 {単位: {fields, added, removed}}。単位名の順で、SCRIPT は末尾。"""
    out = {}
    for key in ("fields", "added", "removed"):
        for r in res[key]:
            out.setdefault(r["unit"], {"fields": 0, "added": 0, "removed": 0})[key] += 1
    return dict(sorted(out.items(), key=lambda kv: (kv[0] == SCRIPT, kv[0])))


def _show(v):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= SHOW_MAX else s[:SHOW_MAX] + "…"


def top_fields(res, n=TOP_N):
    """揺れが多い欄(添字なしの道筋ごと)の上位。[(道筋, 単位, 件数, 例の1件)]。"""
    by = {}
    for r in res["fields"]:
        e = by.setdefault(r["path"], {"unit": r["unit"], "n": 0, "ex": r})
        e["n"] += 1
    ranked = sorted(by.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:n]
    return [(p, e["unit"], e["n"], e["ex"]) for p, e in ranked]


def format_report(res):
    """先頭に単位ごとの件数、次に揺れの大きい欄、行の増減、揺れた欄、末尾に単位ごとの候補。"""
    summ = summarize(res)
    tot = {k: len(res[k]) for k in ("fields", "added", "removed")}
    lines = ["=== 単位ごとの件数(欄の揺れ / 増えた行 / 減った行) ==="]
    for u, c in summ.items():
        lines.append(f"  {u}: 欄の揺れ {c['fields']} / 増えた行 {c['added']} / 減った行 {c['removed']}")
    lines.append(f"  合計: 欄の揺れ {tot['fields']} / 増えた行 {tot['added']} / 減った行 {tot['removed']}")
    lines.append(f"  ({SCRIPT} = units.json の owns に無い欄。比べない欄 = 採番 ID・他の欄への参照 ID・legacy_*・タイムスタンプ類)")
    lines.append("")
    lines.append(f"=== 揺れが大きい欄(道筋ごと、上位 {TOP_N}) ===")
    for p, u, n, ex in top_fields(res):
        lines.append(f"  {n} 件 {p} [{u}]")
        lines.append(f"    例 {ex['label']}")
        lines.append(f"      A: {_show(ex['a'])}")
        lines.append(f"      B: {_show(ex['b'])}")
    lines.append("")
    lines.append(f"=== 増えた行(B にだけある): {tot['added']} 件 ===")
    for r in res["added"]:
        lines.append(f"  {r['label']} [{r['unit']}]  {_show(r['value'])}")
    lines.append("")
    lines.append(f"=== 減った行(A にだけある): {tot['removed']} 件 ===")
    for r in res["removed"]:
        lines.append(f"  {r['label']} [{r['unit']}]  {_show(r['value'])}")
    lines.append("")
    lines.append(f"=== 揺れた欄: {tot['fields']} 件 ===")
    for r in res["fields"]:
        lines.append(f"  {r['label']} [{r['unit']}]")
        lines.append(f"    A: {_show(r['a'])}")
        lines.append(f"    B: {_show(r['b'])}")
    lines.append("")
    lines.append("=== スクリプトで埋める候補(揺れた欄を単位ごとに。道筋は添字なし) ===")
    by_unit = {}
    for r in res["fields"]:
        by_unit.setdefault(r["unit"], {}).setdefault(r["path"], 0)
        by_unit[r["unit"]][r["path"]] += 1
    for u in sorted(by_unit, key=lambda x: (x == SCRIPT, x)):
        lines.append(f"  {u}: {sum(by_unit[u].values())} 件")
        for p, n in sorted(by_unit[u].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {n} {p}")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--selftest"]:
        selftest()
        print("selftest OK")
        return 0
    if len(argv) != 2:
        print("使い方: diff_runs.py <A.json> <B.json> | --selftest", file=sys.stderr)
        return 2
    try:
        a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in argv)
    except (OSError, ValueError) as e:
        print(f"読めない: {e}", file=sys.stderr)
        return 2
    print(format_report(diff_runs(a, b, load_units())))
    return 0


def selftest():
    units = {"S1": {"owns": ["screen_layout"]}, "H4": {"owns": ["validations_frontend", "calculations"]}}

    def d(a, b):
        return diff_runs(a, b, units)

    def item(name, **kw):
        return {"item_name": name, **kw}

    # 先頭に1行増えても、後ろの行は揺れに数えない(位置で突き合わせると全部ずれる)
    a = {"screen_layout": {"items": [item("x", remarks="r1"), item("y", remarks="r2")]}}
    b = {"screen_layout": {"items": [item("new", remarks="n"), item("x", remarks="r1"), item("y", remarks="r2")]}}
    r = d(a, b)
    assert r["fields"] == [] and r["removed"] == [] and len(r["added"]) == 1, r
    assert r["added"][0]["label"] == "screen_layout.items[new]" and r["added"][0]["unit"] == "S1", r["added"]
    r = d(b, a)
    assert len(r["removed"]) == 1 and r["added"] == [] and r["fields"] == [], r
    # 同じ行の欄が違えば、その欄だけが揺れ。単位が付く
    b2 = {"screen_layout": {"items": [item("y", remarks="R2"), item("x", remarks="r1")]}}
    r = d(a, b2)
    assert len(r["fields"]) == 1 and r["added"] == [] and r["removed"] == [], r
    f = r["fields"][0]
    assert (f["path"], f["label"], f["a"], f["b"], f["unit"]) == (
        "screen_layout.items[].remarks", "screen_layout.items[y].remarks", "r2", "R2", "S1"), f
    # 比べない欄(採番 ID・legacy_*・タイムスタンプ)は数えない
    a3 = {"meta": {"generated_at": "t1"}, "screen_layout": {"items": [item("x", no="IT1", legacy_id="1", step_no=1)]}}
    b3 = {"meta": {"generated_at": "t2"}, "screen_layout": {"items": [item("x", no="IT44", legacy_id="9", step_no=7)]}}
    assert d(a3, b3) == {"fields": [], "added": [], "removed": []}
    # owns に無い欄は担当不明でなく「スクリプト」
    r = d({"parameters": [{"name": "p", "type": "string"}]}, {"parameters": [{"name": "p", "type": "int"}]})
    assert [(x["unit"], x["label"]) for x in r["fields"]] == [(SCRIPT, "parameters[p].type")], r
    # 同じ識別子が並ぶときは出現順で区別する(1つ減ったら減った行1件)
    two = {"calculations": [{"name": "c", "formula": "1"}, {"name": "c", "formula": "2"}]}
    one = {"calculations": [{"name": "c", "formula": "1"}]}
    r = d(two, one)
    assert len(r["removed"]) == 1 and r["removed"][0]["label"] == "calculations[c#2]" and r["fields"] == [], r
    assert r["removed"][0]["unit"] == "H4", r
    # 識別子の無い並び: 文字列の配列は、増えた行は増えた行、変わった行は揺れ
    r = d({"event_processes": [{"event_ref": "EV2", "details": ["a", "b"]}]},
          {"event_processes": [{"event_ref": "EV2", "details": ["x", "a", "b"]}]})
    assert len(r["added"]) == 1 and r["fields"] == [] and r["removed"] == [], r
    r = d({"event_processes": [{"event_ref": "EV2", "details": ["a", "b"]}]},
          {"event_processes": [{"event_ref": "EV2", "details": ["a", "c"]}]})
    assert [(x["a"], x["b"]) for x in r["fields"]] == [("b", "c")] and r["added"] == [], r
    # steps は kind を印に並びを合わせる(途中に1つ入っても、後ろは対のまま)
    s_a = {"front_processes": [{"name": "F", "steps": [{"kind": "k1", "description": "d1"}, {"kind": "k3", "description": "d3"}]}]}
    s_b = {"front_processes": [{"name": "F", "steps": [{"kind": "k1", "description": "d1"}, {"kind": "k2", "description": "d2"},
                                                        {"kind": "k3", "description": "d3"}]}]}
    r = d(s_a, s_b)
    assert len(r["added"]) == 1 and r["fields"] == [] and r["removed"] == [], r
    # 別名: backend_processes の db_op_detail は db_operations と同じ識別子で引く
    p_a = {"backend_processes": [{"endpoint": {"action": "act"}, "processing": [
        {"kind": "db", "db_op_detail": {"dataset": [{"physical_table": "t", "physical_column": "c1", "alias_name": "x"}]}}]}]}
    p_b = {"backend_processes": [{"endpoint": {"action": "act"}, "processing": [
        {"kind": "db", "db_op_detail": {"dataset": [{"physical_table": "t", "physical_column": "c0", "alias_name": "w"},
                                                    {"physical_table": "t", "physical_column": "c1", "alias_name": "x"}]}}]}]}
    r = d(p_a, p_b)
    assert len(r["added"]) == 1 and r["fields"] == [] and r["removed"] == [], r
    # null と欄なしは別物。型が違えば揺れ
    assert d({"k": None}, {})["fields"][0]["b"] == MISSING
    assert d({"k": None}, {"k": None}) == {"fields": [], "added": [], "removed": []}
    assert len(d({"k": 1}, {"k": "1"})["fields"]) == 1
    assert d({}, {}) == {"fields": [], "added": [], "removed": []}
    # 空の配列と行のある配列は、行が増えた
    assert len(d({"parameters": []}, {"parameters": [{"name": "p"}]})["added"]) == 1
    # 単位ごとの件数が、出力の先頭に出る
    r = d(a, b)
    r["fields"] += d(a, b2)["fields"] + d({"parameters": [{"name": "p", "type": "a"}]}, {"parameters": [{"name": "p", "type": "b"}]})["fields"]
    summ = summarize(r)
    assert summ["S1"] == {"fields": 1, "added": 1, "removed": 0} and summ[SCRIPT]["fields"] == 1, summ
    assert list(summ)[-1] == SCRIPT
    rep = format_report(r)
    assert rep.splitlines()[0].startswith("=== 単位ごとの件数"), rep
    assert "S1: 欄の揺れ 1 / 増えた行 1 / 減った行 0" in rep and "スクリプトで埋める候補" in rep, rep
    assert top_fields(r)[0][0] in ("screen_layout.items[].remarks", "parameters[].type"), top_fields(r)
    # 実際の units.json でも、owns の欄は担当の単位が付く
    real = load_units()
    u0 = next(iter(real))
    own = real[u0]["owns"][0].replace("[]", "[]")
    assert diff_runs({own: "x"}, {own: "y"}, real)["fields"][0]["unit"] in real
    # 読めない入力は終了コード2
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()):
        assert main(["/nonexistent_a.json", "/nonexistent_b.json"]) == 2
        assert main(["x"]) == 2


if __name__ == "__main__":
    sys.exit(main())
