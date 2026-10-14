"""例示値の計算の合成(round11 U11・新2)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=160(備考の取り除き 150 の後)。
WHY: 画面項目の例示値(example)の計算結果(客単価など)を単位が手で書くと、端数の処理を誤る。
  run10 では 客単価 = 小計（税込売上金額）1,259,600 ÷ 客数 620 = 2,031.6 を切り捨てて 2,031 のところを 2,032 と書いていた。
  react 下書きの calc_candidates の式を、被演算子の項目の例示値の数値で評価し、結果の項目の例示値の数値が違えば直す。

評価する式: r.<名前>・数値・+ - * /・括弧・Math.floor・三項(A ? B : C)だけ(許可リストの構文解析。三項は選ばれた枝だけを評価する)。
触らない場合: 式がこの形以外・被演算子の項目が1つに決まらない・被演算子の例示値の数値が1つでない・
  結果が整数にならない(切り捨てが無い)・結果の項目が1つに決まらない。
直すのは例示値の数値だけ(桁区切り・単位・他の文字はそのまま)。被演算子の値は、この fill を呼ぶ時点の例示値で読む
(直した順で別の結果が変わらないように、先に全部評価してから書く)。
"""
import json
import math
import re
import sys
from pathlib import Path

ORDER = 160

_TOKEN = re.compile(r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<name>r\.[A-Za-z_]\w*)|(?P<fn>Math\.floor)|(?P<op>[-+*/?:()]))")
# WHY: 例示値の数値は「1,259,600」のような桁区切りを1つの数として読む。「2026-10-01」のような日付は3つの数になり、1つに決まらない
_NUM = re.compile(r"\d+(?:,\d{3})+(?!\d)|\d+")


def _drafts(directory):
    """react 下書きを全部読む。読めないファイルは無いものとして飛ばす(_compose_mapping と同じ読み方)。"""
    out = []
    for p in sorted(Path(directory).glob("*_react_draft.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _tokens(expr):
    """式を字句に分ける。扱えない字(比較演算子・識別子など)があれば ValueError。"""
    toks, pos, s = [], 0, expr.strip()
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m:
            raise ValueError(f"式に扱えない字: {s[pos:]}")
        toks.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    return toks


def _parse(toks):
    """字句から木を作る。構文が外れれば ValueError。三項の枝は木のまま持ち、評価で選ぶ(JS と同じく選ばれた枝だけ評価する)。"""
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else (None, None)

    def take(text=None):
        kind, t = peek()
        if kind is None or (text is not None and t != text):
            raise ValueError("構文")
        pos[0] += 1
        return kind, t

    def cond():
        c = add()
        if peek()[1] == "?":
            take("?")
            a = cond()
            take(":")
            return ("cond", c, a, cond())
        return c

    def add():
        n = mul()
        while peek()[1] in ("+", "-"):
            op = take()[1]
            n = ("bin", op, n, mul())
        return n

    def mul():
        n = unary()
        while peek()[1] in ("*", "/"):
            op = take()[1]
            n = ("bin", op, n, unary())
        return n

    def unary():
        if peek()[1] == "-":
            take()
            return ("neg", unary())
        return atom()

    def atom():
        kind, t = take()
        if kind == "num":
            return ("num", float(t))
        if kind == "name":
            return ("var", t[2:])
        if kind == "fn":
            take("(")
            x = cond()
            take(")")
            return ("floor", x)
        if t == "(":
            x = cond()
            take(")")
            return x
        raise ValueError("構文")

    tree = cond()
    if pos[0] != len(toks):
        raise ValueError("構文")
    return tree


def _eval(n, env):
    """木を評価する。env に無い名前は KeyError(被演算子に決まらなかった名前を使う式は触らない)。"""
    k = n[0]
    if k == "num":
        return n[1]
    if k == "var":
        return env[n[1]]
    if k == "neg":
        return -_eval(n[1], env)
    if k == "floor":
        return float(math.floor(_eval(n[1], env)))
    if k == "cond":
        return _eval(n[2] if _eval(n[1], env) else n[3], env)
    a, b = _eval(n[2], env), _eval(n[3], env)
    if n[1] == "+":
        return a + b
    if n[1] == "-":
        return a - b
    if n[1] == "*":
        return a * b
    return a / b  # 0 で割るときは ZeroDivisionError(呼出元で触らない)


def _one_number(text):
    """例示値の文字列にある数が1つだけのときその文字列(桁区切りを含む)。0 個・2 個以上は None。"""
    if not isinstance(text, str):
        return None
    nums = _NUM.findall(text)
    return nums[0] if len(nums) == 1 else None


def _item_at(items, span):
    """出典 span の行を含む画面項目のうち、行範囲が最も狭い1つの index。同じ狭さが2つ以上・無いときは None。
    WHY: 表全体の項目(例 表(右) の src が表の全行を持つ)は、行ごとの項目をすべて含む。行ごとの項目を取るため範囲の狭い方を選ぶ。"""
    if not isinstance(span, dict) or span.get("line_start") is None:
        return None
    hit = []
    for i, it in enumerate(items):
        for r in it.get("src") or []:
            if not isinstance(r, dict) or r.get("file") != span.get("file") or r.get("line_start") is None:
                continue
            end = r.get("line_end") or r["line_start"]
            if r["line_start"] <= span["line_start"] <= end:
                hit.append((end - r["line_start"], i))
                break
    if not hit:
        return None
    best = min(w for w, _ in hit)
    idx = [i for w, i in hit if w == best]
    return idx[0] if len(idx) == 1 else None


def _spec_of_kin(kin, specs):
    """表示行の spec のうち kin が一致するものが1つだけのときその spec。無い・2つ以上は None(被演算子が決まらない)。"""
    hit = [sp for sp in specs if sp.get("kin") == kin and sp.get("src")]
    return hit[0] if len(hit) == 1 else None


def _plan_one(c, items, specs):
    """calc_candidates の1件について、結果の項目の新しい例示値を決める。直さないときは None。戻り: (項目, 新しい例示値, 注記)。"""
    expr, ops, src = c.get("expr"), c.get("operands") or [], (c.get("src") or [None])[0]
    if not isinstance(expr, str) or not ops or not isinstance(src, dict):
        return None
    res = _item_at(items, src)
    if res is None:
        return None
    env = {}
    for name in ops:
        sp = _spec_of_kin(name, specs)
        if sp is None:
            return None
        idx = _item_at(items, sp["src"][0])
        if idx is None:
            return None
        v = _one_number(items[idx].get("example"))
        if v is None:
            return None
        env[name] = float(v.replace(",", ""))
    try:
        val = _eval(_parse(_tokens(expr)), env)
    except (ValueError, KeyError, ZeroDivisionError, TypeError):
        return None  # 式が扱えない・被演算子が決まらない・0 で割る(ガードで届かない枝)は触らない
    if not math.isfinite(val) or val != int(val):
        return None  # WHY: 切り捨ての有無を式から決めきれないので、整数にならない結果は触らない
    it = items[res]
    old_text = it.get("example")
    old = _one_number(old_text)
    if old is None:
        return None
    want = int(val)
    if int(old.replace(",", "")) == want:
        return None
    shown = f"{want:,}" if "," in old else str(want)
    new = _NUM.sub(lambda _m: shown, old_text, count=1)
    return it, new, f"[新2] {it.get('no') or it.get('item_name')} の例示値「{old_text}」を「{new}」に直す(式: {expr})"


def fill(out, log):
    """calc_candidates の式の結果を、結果の項目の例示値に写す(新2)。react 下書きや画面項目が無ければ何もしない。"""
    from _config_loader import INTERMEDIATE_DIR
    items = [x for x in ((out.get("screen_layout") or {}).get("items") or []) if isinstance(x, dict)]
    drafts = _drafts(INTERMEDIATE_DIR)
    calcs = [c for d in drafts for c in d.get("calc_candidates") or [] if isinstance(c, dict)]
    specs = []
    for d in drafts:
        for row in d.get("display_rows") or []:
            for sp in (row.get("specs") or []) if isinstance(row, dict) else []:
                if isinstance(sp, dict):
                    specs.append(sp)
    if not items or not calcs or not specs:
        return
    plan = [p for p in (_plan_one(c, items, specs) for c in calcs) if p]  # 先に全部評価してから書く
    for it, new, msg in plan:
        it["example"] = new
        log.append(msg)


def _selftest():
    import tempfile
    import _config_loader

    F = "/src/PosReport.tsx"

    def sp(kin, line):
        return {"label": kin, "kin": kin, "src": [{"file": F, "line_start": line, "line_end": line}]}

    specs = [sp("syoukei_3", 60), sp("total_2", 75), sp("tensuu", 82), sp("kyakusuu", 84),
             sp("kamoku_1_kin", 67), sp("aridaka", 78), sp("kaisyuu", 79)]
    calcs = [
        {"id": "CC01", "expr": "r.syoukei_3 - r.total_2", "operands": ["syoukei_3", "total_2"], "src": [{"file": F, "line_start": 76, "line_end": 76}]},
        {"id": "CC04", "expr": "r.kyakusuu ? Math.floor(r.syoukei_3 / r.kyakusuu) : 0", "operands": ["kyakusuu", "syoukei_3"], "src": [{"file": F, "line_start": 85, "line_end": 85}]},
        {"id": "CC03", "expr": "r.tensuu ? Math.floor(r.syoukei_3 / r.tensuu) : 0", "operands": ["tensuu", "syoukei_3"], "src": [{"file": F, "line_start": 83, "line_end": 83}]},
        {"id": "CC90", "expr": "r.kamoku_1_kin - r.tensuu", "operands": ["kamoku_1_kin", "tensuu"], "src": [{"file": F, "line_start": 90, "line_end": 90}]},
        {"id": "CC95", "expr": "r.syoukei_3 / r.kyakusuu", "operands": ["syoukei_3", "kyakusuu"], "src": [{"file": F, "line_start": 95, "line_end": 95}]},
        {"id": "CC96", "expr": "r.syoukei_3 >= r.total_2 ? 1 : 0", "operands": ["syoukei_3", "total_2"], "src": [{"file": F, "line_start": 96, "line_end": 96}]},
        {"id": "CC97", "expr": "r.zz - r.tensuu", "operands": ["tensuu"], "src": [{"file": F, "line_start": 97, "line_end": 97}]},
    ]

    def it(no, name, example, line):
        return {"no": no, "item_name": name, "example": example, "src": [{"file": F, "line_start": line, "line_end": line}]}

    def mk(kyaku="620 人"):
        return {"screen_layout": {"items": [
            # WHY: 表(右)の項目は表の全行(66〜87)を持つ。行ごとの項目より広いので、結ぶ先は狭い方の項目になる
            {"no": "IT18", "item_name": "会計レポート表（右）", "example": "表", "src": [{"file": F, "line_start": 66, "line_end": 87}]},
            it("IT40", "小計（税込売上金額）", "1,259,600 円", 60),
            it("IT57", "点数", "1,480 点", 82),
            it("IT59", "客数", kyaku, 84),
            it("IT52", "合計", "1,125,600 円", 75),
            it("IT53", "支払科目に無い額", "134,000 円", 76),
            it("IT60", "客単価", "2,032 円", 85),
            it("IT58", "平均単価", "851 円", 83),
            it("IT44", "現金", "820 点 / 645,300 円", 67),
            it("IT90", "差", "999 円", 90),
            it("IT95", "客単価(素)", "2,031 円", 95),
            it("IT96", "判定", "1 件", 96),
            it("IT97", "未決", "5 円", 97),
        ]}}

    old = _config_loader.INTERMEDIATE_DIR
    with tempfile.TemporaryDirectory() as td:
        Path(td, "x_react_draft.json").write_text(json.dumps(
            {"display_rows": [{"array": "RIGHT", "specs": specs}], "calc_candidates": calcs}, ensure_ascii=False), encoding="utf-8")
        _config_loader.INTERMEDIATE_DIR = Path(td)
        try:
            out = mk()
            log = []
            fill(out, log)
            got = {x["no"]: x["example"] for x in out["screen_layout"]["items"]}
            assert got["IT60"] == "2,031 円", got  # 1,259,600 ÷ 620 = 2,031.6 を切り捨て(run10 は 2,032 と書いていた)
            assert got["IT53"] == "134,000 円", got  # 差の計算は合っている(触らない)
            assert got["IT58"] == "851 円", got  # 平均単価は合っている(触らない)
            assert got["IT90"] == "999 円", got  # 被演算子の例示値が「820 点 / 645,300 円」(数が2つ)なので決まらない
            assert got["IT95"] == "2,031 円", got  # 整数にならない結果は触らない
            assert got["IT96"] == "1 件", got  # 比較演算子は式の形に含めない
            assert got["IT97"] == "5 円", got  # 式の中の名前が被演算子に無い
            assert [x for x in log if x.startswith("[新2]")] == [
                "[新2] IT60 の例示値「2,032 円」を「2,031 円」に直す(式: r.kyakusuu ? Math.floor(r.syoukei_3 / r.kyakusuu) : 0)"], log
            snap = json.dumps(out, ensure_ascii=False, sort_keys=True)
            log2 = []
            fill(out, log2)
            assert json.dumps(out, ensure_ascii=False, sort_keys=True) == snap, "2回目で変わった(冪等でない)"
            assert log2 == [], log2
            # 0 人のとき三項の枝は 0 になり、割り算は評価されない(0 で割らない)
            out0 = mk("0 人")
            log3 = []
            fill(out0, log3)
            got0 = {x["no"]: x["example"] for x in out0["screen_layout"]["items"]}
            assert got0["IT60"] == "0 円", got0
            assert got0["IT58"] == "851 円", got0
        finally:
            _config_loader.INTERMEDIATE_DIR = old
    print("[selftest OK] _compose_calc_examples (新2: 式の結果を例示値の数値へ。切り捨て・被演算子が決まらない形は触らない)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_calc_examples.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
