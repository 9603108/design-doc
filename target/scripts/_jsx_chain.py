"""TSX の行範囲から、表示条件の鎖と文言を取り出す(round10 T1)。

WHY: 初期処理の活性・表示は、画面項目の src(ファイルと行範囲)から JSX の囲む条件を辿って決める。
画面項目の名前と JSX 要素の名前を突き合わせる方式では、要素の見つからない項目が多く残った(round10 の 6)。
鎖の形は _react_facts._visible_chain を拡張したもの。呼出元が複数ある部品は caller_chains で全部持つ。
ponytail: 文字列は ' " ` のみ扱う(_react_facts と同じ)。JSX の文言の中の撇号(')は誤って文字列扱いになる。
"""
import re
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
import tempfile
from pathlib import Path

from _react_facts import _end, _tag_end, _open_stack, _top_q, _scope_after_params

# WHY: 関数の定義(function f( / const f = ( )。引数の ( の位置を返すので、_scope_after_params で本体を取る
_DEF = re.compile(r"\b(?:function\s+(\w+)\s*\(|const\s+(\w+)\s*=\s*(?:async\s*)?\()")
# WHY: 配列の定義は JSX の外で書かれる(const LEFT: Spec[] = [ ...])。型注釈の = は含めない
_ARRAY_DEF = re.compile(r"^const\s+(\w+)\b[^=\n]*=\s*\[", re.M)
# WHY: 配列の形を変えずに要素を絞るだけのメソッドは、map の元をその配列名とみなす
_KEEP_METHODS = ("filter", "slice", "sort", "reverse", "toSorted")
_MAX_DEPTH = 4


def _is_jsx_brace(before):
    """`{` の直前の文字列(rstrip 済み)から、JSX の式の波括弧かどうかを判定する。

    WHY: 関数本体の `=> {` の波括弧は JSX の式ではないので条件として数えない。
    """
    if before.endswith("=>"):
        return False
    return before == "" or before[-1] in ">}"


def _defs(text):
    """同じファイルの関数定義を [(名前, 定義の位置, 本体の開始, 本体の終了)] で返す。本体が取れないものは除く。"""
    out = []
    for m in _DEF.finditer(text):
        sc = _scope_after_params(text, m.end() - 1)
        if sc:
            out.append((m.group(1) or m.group(2), m.start(), sc[0], sc[1]))
    return out


def _callers(text, name, s, e):
    """名前 name の呼出位置(<Name と Name( )のうち、本体 [s, e] の外のものを昇順で返す。"""
    n = re.escape(name)
    pos = [m.start() for m in re.finditer(rf"<{n}\b", text)]
    for m in re.finditer(rf"(?<![\w$.]){n}\s*\(", text):
        p = m.start()
        # WHY: function Name( は定義であって呼出ではない
        if text[max(0, p - 9):p] == "function ":
            continue
        pos.append(p)
    return sorted(p for p in pos if not (s <= p <= e))


def _map_source(s):
    """`.map(` の手前の文字列 s から、配列の元の識別子を返す。`rows.filter(...)` は rows。判定できなければ None。"""
    s = s.rstrip()
    while s.endswith(")"):
        depth, open_at = 0, -1
        for i in range(len(s) - 1, -1, -1):
            if s[i] == ")":
                depth += 1
            elif s[i] == "(":
                depth -= 1
                if depth == 0:
                    open_at = i
                    break
        if open_at < 0:
            return None
        head = s[:open_at].rstrip()
        mm = re.search(r"\.(\w+)$", head)
        if not mm or mm.group(1) not in _KEEP_METHODS:
            return None
        s = head[:mm.start()].rstrip()
    mm = re.search(r"[\w$.]+$", s)
    return mm.group(0) if mm else None


def chain_at(text, pos, depth=0):
    """pos(JSX の要素の位置)を囲む表示条件の鎖を、外側から順に返す。

    kind: and / ternary_then / ternary_else / map(cond は配列名)/
          component(同じファイルの関数。caller_chains に呼出位置ごとの鎖、callers に呼出の数)
    WHY: 要素が画面に出るかは囲む条件の積で決まる。関数の呼出元が複数あるとき、
    どれか1つでも真なら出るので、呼出元ごとの鎖を持って評価を後回しにする。
    """
    st = _open_stack(text, pos)
    local = []  # 内側から順に積む
    braces = [o for ch, o in st if ch == "{"]
    if braces and _is_jsx_brace(text[:braces[-1]].rstrip()):
        # WHY: 括弧なしの直接形({cond && <el})は、一番内側の { から要素までの文字列で判定する
        seg = text[braces[-1] + 1:pos]
        s = seg.rstrip()
        if s.endswith("&&"):
            local.append({"cond": s[:-2].strip(), "kind": "and"})
        elif s.endswith("?") and not s.endswith("??"):
            local.append({"cond": s[:-1].strip(), "kind": "ternary_then"})
        elif s.endswith(":") and _top_q(seg) >= 0:
            local.append({"cond": seg[:_top_q(seg)].strip(), "kind": "ternary_else"})
    for idx in range(len(st) - 1, -1, -1):
        ch, o = st[idx]
        if ch != "(":
            continue
        pre = text[:o].rstrip()
        if pre.endswith(".map"):
            src = _map_source(pre[:-4])
            if src:
                local.append({"cond": src, "kind": "map"})
            continue
        if idx == 0 or st[idx - 1][0] != "{":
            continue
        b = st[idx - 1][1]
        if not _is_jsx_brace(text[:b].rstrip()):
            continue
        if pre.endswith("&&"):
            local.append({"cond": text[b + 1:len(pre) - 2].strip(), "kind": "and"})
        elif pre.endswith("?") and not pre.endswith("??"):
            local.append({"cond": text[b + 1:len(pre) - 1].strip(), "kind": "ternary_then"})
        elif pre.endswith(":"):
            seg = text[b + 1:len(pre) - 1]
            q = _top_q(seg)
            if q >= 0:
                local.append({"cond": seg[:q].strip(), "kind": "ternary_else"})

    markers = []
    if depth < _MAX_DEPTH:
        # WHY: pos を囲む関数(本体の中)と、pos が引数の中にある呼出先の関数(render(LEFT) の LEFT)の両方を、部品の印として足す
        arg_names = set()
        for ch, o in st:
            if ch == "(":
                mm = re.search(r"([A-Za-z_$][\w$]*)\s*$", text[max(0, o - 200):o])
                if mm:
                    arg_names.add(mm.group(1))
        seen = set()
        for name, dstart, s, e in _defs(text):
            if name in seen or not (s <= pos <= e or name in arg_names):
                continue
            callers = _callers(text, name, s, e)
            if not callers:
                # WHY: 呼出元が0本の関数(画面の本体)は部品の印を付けない
                continue
            seen.add(name)
            markers.append((dstart, {
                "cond": "", "kind": "component", "name": name, "callers": len(callers),
                "caller_chains": [chain_at(text, c, depth + 1) for c in callers],
            }))
        markers.sort(key=lambda x: x[0])
    return [m for _, m in markers] + list(reversed(local))


def _offsets(text, line_start, line_end):
    """1 始まりの行範囲 [line_start, line_end](両端含む)を、文字位置の [start, end) で返す。"""
    lines = text.split("\n")
    start = sum(len(x) + 1 for x in lines[:line_start - 1])
    end = sum(len(x) + 1 for x in lines[:line_end])
    return start, min(end, len(text))


def _array_use(text, start, end):
    """範囲が const NAME = [ … ] の中にあるとき、NAME の JSX の { } 内の使用箇所を返す。無ければ None。

    WHY: 画面の行は配列の要素(const LEFT: Spec[] = [...])で、画面に出るのは使用箇所({render(LEFT)})の側。
    WHY(round11 U1): 範囲が配列の定義行(const X = [)や閉じ括弧(];)を含むときも配列の範囲とみなす。
    定義行を含む範囲(src が宣言の全体を指す項目)を、重なりで判定しないと found=False に落ちて仮埋めになる。
    """
    for m in reversed(list(_ARRAY_DEF.finditer(text, 0, end))):
        b = m.end() - 1
        close = _end(text, b)
        if not (m.start() < end and close >= start):
            continue
        name = m.group(1)
        usages = []
        for u in re.finditer(rf"(?<![\w$.]){re.escape(name)}\b", text):
            p = u.start()
            if m.start() <= p <= close or text[max(0, p - 3):p] == "...":
                continue
            if not any(ch == "{" and _is_jsx_brace(text[:o].rstrip()) for ch, o in _open_stack(text, p)):
                continue
            usages.append(p)
        if not usages:
            return None
        return {"found": True, "visible_chain": [{
            "cond": "", "kind": "array_use", "name": name,
            "caller_chains": [chain_at(text, p) for p in usages],
        }], "disabled_chain": None}
    return None


def chain_for_lines(file, line_start, line_end):
    """ファイルの行範囲の画面項目について、表示条件の鎖と disabled の鎖を返す。

    戻り値: {"found": bool, "visible_chain": [...], "disabled_chain": [...] | None}
    found=False は、JSX の外で、配列の使用箇所も無いこと。呼出側は推測しない。
    """
    text = Path(file).read_text(encoding="utf-8")
    start, end = _offsets(text, line_start, line_end)
    m = re.search(r"<", text[start:end])
    if m:
        p = start + m.start()
        chain = chain_at(text, p)
        disabled = None
        dm = re.compile(r"\bdisabled=\{").search(text, p, _tag_end(text, p))
        if dm:
            idx = dm.end() - 1
            expr = text[idx + 1:_end(text, idx)].strip()
            disabled = chain + [{"cond": expr, "kind": "disabled"}]
        return {"found": True, "visible_chain": chain, "disabled_chain": disabled}
    arr = _array_use(text, start, end)
    if arr:
        return arr
    return {"found": False, "visible_chain": [], "disabled_chain": None}


def _enclosing_open_line(file, start):
    """範囲の前にある最後の開始タグ（行頭が <Name の行）の行番号。属性の行が囲まれる要素を探すために使う。無ければ None。

    WHY(round11 U1): merge_partials から移した。項目の結びと検査(verify_intermediate)が同じ規則で引けるように1か所に置く。
    """
    lines = Path(file).read_text(encoding="utf-8").split("\n")
    for n in range(start - 1, 0, -1):
        if re.match(r"\s*<[A-Za-z]", lines[n - 1]):
            return n
    return None


def resolve_for_lines(file, line_start, line_end):
    """画面項目の行範囲を、表示条件の鎖の元の位置に結ぶ。結べなければ None。

    戻り値: (r, attr)。r は chain_for_lines と同じ形。attr は「範囲に要素が無く、囲む要素の開始タグから引いた」ときの True。
    手順: まず範囲そのもの(JSX の要素・配列の定義を含む範囲)で chain_for_lines。found=False なら、
    範囲の前にある最後の開始タグの行で引き直す(属性の行の救済)。それも found=False なら None(推測しない)。
    WHY: 結べない項目を名前での結びに落とすと、仮埋め(活性・表示)のまま誰も止めない(round10 の 6)。
    """
    r = chain_for_lines(file, line_start, line_end)
    if r["found"]:
        return r, False
    n = _enclosing_open_line(file, line_start)
    if n is None:
        return None
    r = chain_for_lines(file, n, n)
    if not r["found"]:
        return None
    return r, True


def text_of_lines(file, line_start, line_end, eval_expr=None):
    """範囲の最初の JSX 要素の文言を返す(内側の要素の文言も含む。タグは除く)。

    `{式}` は eval_expr(式) が文字列を返せばその値、None なら `{式}` のまま。`{" "}` は空白。
    WHY: 初期値は JSX の文言(ラベル・ボタン名)を画面どおりに取るため。式は評価器に任せ、評価できない分は残す。
    """
    text = Path(file).read_text(encoding="utf-8")
    start, end = _offsets(text, line_start, line_end)
    m = re.search(r"<", text[start:end])
    if not m:
        return ""
    p = start + m.start()
    e = _tag_end(text, p)
    if text[e - 1:e] == "/":
        return " " if re.match(r"<br\b", text[p:e + 1]) else ""
    out = []
    i, depth = e + 1, 1
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "{":
            j = _end(text, i)
            out.append(_expr_text(text[i + 1:j].strip(), eval_expr))
            i = j + 1
        elif c == "<":
            te = _tag_end(text, i)
            if text.startswith("</", i):
                depth -= 1
            elif text[te - 1:te] != "/":
                depth += 1
            elif re.match(r"<br\b", text[i:te + 1]):
                out.append(" ")
            i = te + 1
        else:
            out.append(c)
            i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _expr_text(inner, eval_expr):
    mm = re.fullmatch(r"(['\"`])(.*)\1", inner, re.S)
    if mm:
        return mm.group(2)
    if "<" in inner:
        return "{" + inner + "}"
    if eval_expr is not None:
        v = eval_expr(inner)
        if v is not None:
            return str(v)
    return "{" + inner + "}"


# ---- selftest ----

_SRC_DIRECT = """export default function P({ error, ok, shops }) {
  return (
    <div>
      {error && <i className="e">{error}</i>}
      <button disabled={!ok}>保存</button>
      <span>店舗 {shops.length}店</span>
    </div>
  );
}
"""

_SRC_MAP = """export default function P({ rows }) {
  return (
    <ul>
      {rows.filter((r) => r.ok).map((r) => (
        <li key={r.id}>{r.name}</li>
      ))}
    </ul>
  );
}
"""

_SRC_LOCAL = """export default function P({ rows }) {
  const render = (specs) => (
    <tbody>
      {specs.map((s) => <td key={s}>{s}</td>)}
    </tbody>
  );
  return (
    <table>
      {rows.length > 1 && render(LEFT)}
    </table>
  );
}
"""

_SRC_TWO = """function Card({ items }) {
  return (
    <section>
      {items.length > 0 && <p>x</p>}
    </section>
  );
}
export default function P({ rows, error }) {
  return (
    <div>
      {rows.length > 1 && <Card items={rows} />}
      {error && <Card items={[]} />}
    </div>
  );
}
"""

_SRC_ARRAY = """const LEFT = [
  { label: "店舗" },
];
export default function P({ rows }) {
  return (
    <table>
      <tbody>{render(LEFT)}</tbody>
    </table>
  );
}
function render(specs) {
  return specs.map((s) => <tr key={s.label}><th>{s.label}</th></tr>);
}
"""

_SRC_ARROW = """export default function P({ error }) {
  const f = () => {
    return <i>a</i>;
  };
  return <div>{error && <b>e</b>}</div>;
}
"""


def _line_of(src, needle):
    for n, line in enumerate(src.split("\n"), 1):
        if needle in line:
            return n
    raise AssertionError("needle not found: " + needle)


def _write(tmp, name, src):
    f = Path(tmp) / name
    f.write_text(src, encoding="utf-8")
    return str(f)


def _selftest():
    with tempfile.TemporaryDirectory() as tmp:
        f = _write(tmp, "direct.tsx", _SRC_DIRECT)
        n = _line_of(_SRC_DIRECT, "<i className")
        r = chain_for_lines(f, n, n)
        assert {"cond": "error", "kind": "and"} in r["visible_chain"], r
        n = _line_of(_SRC_DIRECT, "<button")
        r = chain_for_lines(f, n, n)
        assert r["visible_chain"] == [], r
        assert r["disabled_chain"] == [{"cond": "!ok", "kind": "disabled"}], r
        n = _line_of(_SRC_DIRECT, "<span>")
        assert text_of_lines(f, n, n, lambda s: "0" if s == "shops.length" else None) == "店舗 0店"
        assert text_of_lines(f, n, n) == "店舗 {shops.length}店"

        f = _write(tmp, "map.tsx", _SRC_MAP)
        n = _line_of(_SRC_MAP, "<li key")
        r = chain_for_lines(f, n, n)
        assert {"cond": "rows", "kind": "map"} in r["visible_chain"], r

        f = _write(tmp, "local.tsx", _SRC_LOCAL)
        n = _line_of(_SRC_LOCAL, "<td key")
        chain = chain_for_lines(f, n, n)["visible_chain"]
        mk = [c for c in chain if c["kind"] == "component"]
        assert mk and mk[0]["name"] == "render" and mk[0]["callers"] == 1, chain
        assert {"cond": "rows.length > 1", "kind": "and"} in mk[0]["caller_chains"][0], chain
        assert {"cond": "specs", "kind": "map"} in chain, chain

        f = _write(tmp, "two.tsx", _SRC_TWO)
        n = _line_of(_SRC_TWO, "<p>x")
        chain = chain_for_lines(f, n, n)["visible_chain"]
        mk = [c for c in chain if c["kind"] == "component"]
        assert mk and mk[0]["name"] == "Card" and mk[0]["callers"] == 2, chain
        assert {"cond": "rows.length > 1", "kind": "and"} in mk[0]["caller_chains"][0], chain
        assert {"cond": "error", "kind": "and"} in mk[0]["caller_chains"][1], chain
        assert {"cond": "items.length > 0", "kind": "and"} in chain, chain

        f = _write(tmp, "array.tsx", _SRC_ARRAY)
        n = _line_of(_SRC_ARRAY, '{ label: "店舗" }')
        r = chain_for_lines(f, n, n)
        assert r["found"] and r["visible_chain"][0]["kind"] == "array_use", r
        assert r["visible_chain"][0]["caller_chains"][0][0]["name"] == "render", r
        # WHY(round11 U1): 範囲が配列の定義行から閉じ括弧までの全体(src が宣言の全体を指す項目)でも、使用箇所で結ぶ。
        #   定義行を含むと旧版は found=False になり、仮埋め(活性・表示)に落ちた(run10 の IT17・IT18)
        a, z = _line_of(_SRC_ARRAY, "const LEFT"), _line_of(_SRC_ARRAY, "];")
        r = chain_for_lines(f, a, z)
        assert r["found"] and r["visible_chain"][0]["kind"] == "array_use", r
        res = resolve_for_lines(f, a, z)
        assert res is not None and res[1] is False and res[0]["found"], res
        # 要素も配列も無い行(関数の宣言行)は結べない。推測で結ばない
        plain = _write(tmp, "plain.tsx", _SRC_ARROW)
        assert resolve_for_lines(plain, 1, 1) is None

        f = _write(tmp, "arrow.tsx", _SRC_ARROW)
        n = _line_of(_SRC_ARROW, "<i>a")
        assert chain_for_lines(f, n, n)["visible_chain"] == [], "=> の後の波括弧を条件に数えている"
    print("_jsx_chain selftest: OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: python3 _jsx_chain.py --selftest", file=sys.stderr)
        sys.exit(2)
