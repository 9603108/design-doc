#!/usr/bin/env python3
# 目的: 初期処理の表（merge_partials.build_initialization）の「活性/表示」と固定文の初期値を、
#       react 下書きの事実（state の初期値・派生配列・表示条件の鎖）から評価する（round9 B7）。
# 接続情報: 入力 = 下書きの state_initials / derived_arrays、jsx_elements の visible_chain / disabled_chain（_react_facts が出す）
#           出力 = True / False / None（None = 評価できない。呼出元が「条件付き(式)」にする）
# WHY: 式を eval() で評価しない。TSX の式は任意の処理を含みうるため、識別子・比較・論理・.length だけを読む
#      小さな評価器にする。読めない式や値が決まらない state は None にし、推測で表示・非表示を決めない。

import re

UNKNOWN = object()  # 評価できない値（式・props・関数の結果など、初期値の事実から決まらないもの）
UNDEF = object()    # JavaScript の undefined（state の「未設定(undefined)」）。null は Python の None で表す

_DERIVED_OPS = ("filter", "map", "slice", "sort")  # 空の配列から空の配列しか作らない操作だけ長さ 0 を決める

_TOKEN = re.compile(
    r"""(?P<num>\d+(?:\.\d+)?)"""
    r"""|(?P<str>"[^"]*"|'[^']*')"""
    r"""|(?P<id>[A-Za-z_$][\w$]*)"""
    r"""|(?P<op>===|!==|==|!=|>=|<=|&&|\|\||[!<>()\[\].])"""
)


def _tokens(src: str) -> list:
    """式の文字列を (種類, 字面) の並びにする。読めない字があれば ValueError（呼出側で None になる）。"""
    out, pos = [], 0
    src = src.strip()
    while pos < len(src):
        if src[pos].isspace():
            pos += 1
            continue
        m = _TOKEN.match(src, pos)
        if not m:
            raise ValueError(src)
        out.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    return out


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _truth(v):
    """JavaScript の真偽（true/false）。決まらなければ None。配列は JavaScript では空でも真。"""
    if v is UNKNOWN:
        return None
    if v is None or v is UNDEF:
        return False
    if isinstance(v, bool):
        return v
    if _is_num(v):
        return v != 0
    if isinstance(v, str):
        return v != ""
    return True


def _not(v):
    t = _truth(v)
    return UNKNOWN if t is None else (not t)


def _and(left, right):
    # WHY: 左が決まらないときは右を見ても値が決まらない（conservative に UNKNOWN）
    tl = _truth(left)
    if tl is None:
        return UNKNOWN
    return right if tl else left


def _or(left, right):
    tl = _truth(left)
    if tl is None:
        return UNKNOWN
    return left if tl else right


def _equal(left, right, strict: bool):
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    if _is_num(left) and _is_num(right):
        return left == right
    if type(left) is not type(right):
        return False if strict else UNKNOWN
    if isinstance(left, list):
        return UNKNOWN  # 配列の同一性は値だけでは決められない
    return left == right


def _compare(left, right, op: str):
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    if (_is_num(left) and _is_num(right)) or (isinstance(left, str) and isinstance(right, str)):
        return {">": left > right, "<": left < right, ">=": left >= right, "<=": left <= right}[op]
    return UNKNOWN


def _length(v):
    if isinstance(v, (list, str)):
        return len(v)
    return UNKNOWN


class _Parser:
    """優先順位: || < && < 等値(=== !== == !=) < 比較(< > <= >=) < 単項(!) < 後置(.length) < 一次(リテラル・識別子・括弧・空の配列)。
    関数呼出し・三項演算子・その他の記号は読めないので ValueError にする。"""

    def __init__(self, toks: list, env: dict):
        self.toks, self.i, self.env = toks, 0, env

    def _peek(self):
        return self.toks[self.i][1] if self.i < len(self.toks) else None

    def _next(self):
        tok = self.toks[self.i]  # 末尾の先読みは IndexError（呼出側で UNKNOWN にする）
        self.i += 1
        return tok

    def parse(self):
        v = self._or()
        if self.i != len(self.toks):
            raise ValueError("読めない余りのトークン")
        return v

    def _or(self):
        v = self._and()
        while self._peek() == "||":
            self._next()
            v = _or(v, self._and())
        return v

    def _and(self):
        v = self._eq()
        while self._peek() == "&&":
            self._next()
            v = _and(v, self._eq())
        return v

    def _eq(self):
        v = self._rel()
        while self._peek() in ("===", "!==", "==", "!="):
            op = self._next()[1]
            r = _equal(v, self._rel(), strict=op in ("===", "!=="))
            v = _not(r) if op in ("!==", "!=") else r
        return v

    def _rel(self):
        v = self._unary()
        while self._peek() in (">", "<", ">=", "<="):
            op = self._next()[1]
            v = _compare(v, self._unary(), op)
        return v

    def _unary(self):
        if self._peek() == "!":
            self._next()
            return _not(self._unary())
        return self._postfix()

    def _postfix(self):
        v = self._primary()
        while self._peek() == ".":
            self._next()
            kind, name = self._next()
            if kind != "id":
                raise ValueError("メンバー名でない")
            v = _length(v) if name == "length" else UNKNOWN
        return v

    def _primary(self):
        kind, val = self._next()
        if kind == "num":
            return float(val) if "." in val else int(val)
        if kind == "str":
            return val[1:-1]
        if kind == "id":
            fixed = {"true": True, "false": False, "null": None, "undefined": UNDEF}
            if val in fixed:
                return fixed[val]
            # fallback-ok: env に無い名前（props・関数の結果など）は事実が無いので UNKNOWN（推測しない）
            return self.env.get(val, UNKNOWN)
        if val == "(":
            v = self._or()
            if self._next()[1] != ")":
                raise ValueError("閉じ括弧が無い")
            return v
        if val == "[":
            if self._next()[1] != "]":
                raise ValueError("空でない配列リテラルは読まない")
            return []
        raise ValueError(f"読めない字句: {val}")


def _value(expr, env: dict):
    """式の値。読めない式・空の式は UNKNOWN。"""
    if not expr or not str(expr).strip():
        return UNKNOWN
    try:
        return _Parser(_tokens(str(expr)), env).parse()
    except (ValueError, IndexError):
        return UNKNOWN


def _literal_value(si: dict):
    """state_initials の1件の初期値。literal の true・false・null・undefined・[]・数値・引用符付きの文字列だけ読む。"""
    kind = si.get("kind")
    if kind == "none":
        return UNDEF
    if kind != "literal":
        return UNKNOWN  # 式（todayJst() 等）は初期値の事実が無いので決めない
    # fallback-ok: initial が無い（None）ときは空文字として扱い、下の判定で UNKNOWN になる
    raw = str(si.get("initial") or "").strip()
    fixed = {"true": True, "false": False, "null": None, "undefined": UNDEF, "[]": []}
    if raw in fixed:
        return fixed[raw]
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return float(raw) if "." in raw else int(raw)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return UNKNOWN


def build_env(state_initials: list, derived_arrays: list) -> dict:
    """state の初期値（state 名 → 値）と派生配列（名前 → 値）の環境を作る。
    派生配列の長さは、元が空の配列 [] で操作が filter・map・slice・sort のときだけ 0 に決まる。それ以外は UNKNOWN。"""
    env = {}
    for si in state_initials or []:
        if isinstance(si, dict) and si.get("state"):
            env.setdefault(si["state"], _literal_value(si))
    for d in derived_arrays or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        # fallback-ok: 元の名前が env に無ければ UNKNOWN（元が決まらない派生配列は長さも決めない）
        src = env.get(d.get("source"), UNKNOWN)
        empty_src = isinstance(src, list) and not src
        env.setdefault(d["name"], [] if empty_src and d.get("op") in _DERIVED_OPS else UNKNOWN)
    return env


def truth(expr: str, env: dict):
    """式（TSX の条件）の真偽。真は True、偽は False、評価できなければ None。"""
    t = _truth(_value(expr, env))
    return t


def _any_of(chains: list, env: dict):
    """呼出元（使用箇所）の鎖のどれかが表示かどうか。どれかが真なら True、全部偽なら False、他は None。
    WHY: 呼出元のどれかで表示されるなら、その部品は初期表示で出る。全部偽のときだけ出ない。"""
    results = [chain_value(c, env) for c in chains if isinstance(c, list)]
    if not results:
        return None
    if any(r is True for r in results):
        return True
    if all(r is False for r in results):
        return False
    return None


def chain_value(chain: list, env: dict):
    """表示条件の鎖（visible_chain）の評価。全部真なら True、どれか偽なら False、評価できない項があれば None。空の鎖は True。
    kind: and・ternary_then は cond が真、ternary_else は cond が偽、map は配列（cond が配列名）が1件以上。
    component は呼出元の鎖が前側に連結済みなので cond を見ない。caller_chains（array_use も同じ）があれば、呼出元ごとの鎖のどれかで決める。caller_chains が無く呼出元が2か所以上なら None。"""
    result = True
    for item in chain or []:
        kind = item.get("kind")
        # fallback-ok: cond が無い項は空の式として扱い、評価できず None になる（推測で真偽を決めない）
        cond = item.get("cond") or ""
        if kind in ("component", "array_use"):
            # WHY: caller_chains があれば、呼出元（使用箇所）ごとの鎖で決める。callers の件数だけでは
            #      どの呼出元かが分からず、1つ目が偽・2つ目が真のときに誤るため。
            chains = item.get("caller_chains") or []
            if chains:
                r = _any_of(chains, env)
            elif kind == "array_use":
                r = None  # 使用箇所が無い配列は決められない（呼出側は found=False にする）
            # fallback-ok: callers が無い component は呼出元1か所とみなす（_react_facts は必ず callers を出す）
            elif (item.get("callers") or 1) < 2:
                continue
            else:
                r = None
        elif kind == "map":
            v = _value(cond, env)
            r = len(v) > 0 if isinstance(v, list) else None
        elif kind == "ternary_else":
            t = truth(cond, env)
            r = None if t is None else (not t)
        elif kind in ("and", "ternary_then"):
            r = truth(cond, env)
        else:
            r = None
        if r is False:
            return False
        if r is None:
            result = None
    return result


def disabled_value(disabled_chain: list, env: dict):
    """要素が非活性か。disabled_chain が無ければ False（活性）。末尾の disabled の cond を評価する（真なら非活性）。決まらなければ None。"""
    if not disabled_chain:
        return False
    last = disabled_chain[-1]
    if last.get("kind") != "disabled":
        return None
    # fallback-ok: cond が無い disabled は空の式として None になる（推測しない）
    return truth(last.get("cond") or "", env)


def literal_text_elements(drafts: list) -> list:
    """react 下書きの jsx_elements のうち、中身が文字列リテラルだけで決まる要素（text があり label が無い）の一覧。
    WHY: 固定文は JSX の文字列そのもの（見出し・注記・読込中の文言など）。{error} や DB の値のように実行時に決まる要素は text に入らないので、ここに出ない。"""
    return [el for d in drafts or [] for el in d.get("jsx_elements") or []
            if isinstance(el, dict) and str(el.get("text") or "").strip() and not el.get("label")]


def linked_fixed_elements(names: list, elements: list) -> list:
    """画面項目の名前の候補（item_name・example）と文言が一致する固定文の要素の一覧。どれとも一致しなければ空（推測で結ばない）。
    WHY: 画面項目の item_name は設計上の名前（例「画面見出し」）で、JSX の文言（例「POS会計レポート」）と一致しないことがある。
         画面に書かれた文言は example なので、example も候補にする。"""
    keys = {str(n).strip() for n in names or [] if str(n or "").strip()}
    return [el for el in elements or [] if str(el.get("text") or "").strip() in keys]


def visible_initially(el: dict, env: dict):
    """要素が初期表示で見えるか（True / False / None = 決まらない）。
    visible_chain があれば鎖を評価する。無ければ visible_if（条件の式）を評価する。どちらも無ければ常に表示。
    WHY: run9 の「読込中…」は visible_chain が空で、visible_if に loading だけを持つ（{loading && <span>…</span>}）。
         鎖だけ見ると、初期表示で出ていない文言を出ている扱いにしてしまう。"""
    chain = el.get("visible_chain") or []
    if chain:
        return chain_value(chain, env)
    cond = el.get("visible_if")
    if cond:
        return truth(str(cond), env)
    return True


def selftest() -> int:
    """式の評価・派生配列・表示条件の鎖・活性の判定を assert する。終了コード 0=OK / 1=NG。"""
    env = build_env(
        [{"state": "rows", "kind": "literal", "initial": "[]"},
         {"state": "loading", "kind": "literal", "initial": "false"},
         {"state": "shop", "kind": "literal", "initial": '""'},
         {"state": "dyn", "kind": "expression", "initial": "f()"},
         {"state": "u", "kind": "none", "initial": None}],
        [{"name": "w", "source": "rows", "op": "filter"}],
    )
    cases = [
        ("rows.length > 1", False),
        ("!rows.length", True),
        ("loading", False),
        ("!loading && shop === ''", True),
        ("shop !== ''", False),
        ("w.length > 0", False),
        ("w", True),
        ("dyn", None),
        ("dyn || loading", None),
        ("loading || true", True),
        ("u === undefined", True),
        ("someFn(dyn)", None),
        ("rows.length > 1 ? 1 : 0", None),
        ("", None),
    ]
    for expr, want in cases:
        got = truth(expr, env)
        if got != want:
            print(f"[selftest NG] truth({expr!r}) = {got!r}, want {want!r}")
            return 1
    chains = [
        ([{"cond": "rows.length > 1", "kind": "and"}], False),
        ([{"cond": "loading", "kind": "ternary_else"}], True),
        ([{"cond": "loading", "kind": "ternary_then"}], False),
        ([{"cond": "w", "kind": "map"}], False),
        ([{"cond": "dyn", "kind": "and"}], None),
        ([{"cond": "", "kind": "component", "name": "Row", "callers": 2}], None),
        ([{"cond": "", "kind": "component", "name": "Row", "callers": 1}, {"cond": "!loading", "kind": "and"}], True),
        ([], True),
    ]
    for chain, want in chains:
        got = chain_value(chain, env)
        if got != want:
            print(f"[selftest NG] chain_value({chain}) = {got!r}, want {want!r}")
            return 1
    # caller_chains（呼出元ごとの鎖）は callers より優先する。どれかが真なら真、全部偽なら偽、他は None。
    # WHY: 呼出元の件数だけでは「1つ目が偽・2つ目が真」を取り違えるため、呼出元ごとに評価する。
    yes = [{"cond": "!loading", "kind": "and"}]
    no = [{"cond": "rows.length > 1", "kind": "and"}]
    unknown = [{"cond": "dyn", "kind": "and"}]
    caller_cases = [
        ([{"kind": "component", "name": "Card", "callers": 2, "caller_chains": [no, yes]}], True),
        ([{"kind": "component", "name": "Card", "callers": 2, "caller_chains": [no, no]}], False),
        ([{"kind": "component", "name": "Card", "callers": 2, "caller_chains": [no, unknown]}], None),
        ([{"kind": "component", "name": "Card", "callers": 2, "caller_chains": []}], None),
        ([{"kind": "array_use", "name": "LEFT", "caller_chains": [no, yes]}], True),
        ([{"kind": "array_use", "name": "LEFT", "caller_chains": [no, no]}], False),
        ([{"kind": "array_use", "name": "LEFT"}], None),
        ([{"kind": "component", "name": "Row", "callers": 2, "caller_chains": [
            [{"kind": "component", "name": "Inner", "callers": 2, "caller_chains": [no]}]]}], False),
        ([{"kind": "component", "name": "Row", "callers": 2, "caller_chains": [
            [{"kind": "component", "name": "Inner", "callers": 2, "caller_chains": [yes]}]]}], True),
    ]
    for chain, want in caller_cases:
        got = chain_value(chain, env)
        if got != want:
            print(f"[selftest NG] chain_value(caller_chains) = {got!r}, want {want!r}: {chain}")
            return 1
    if disabled_value(None, env) is not False:
        print("[selftest NG] disabled_value(None)")
        return 1
    if disabled_value([{"cond": "loading", "kind": "ternary_else"}, {"cond": "!rows.length", "kind": "disabled"}], env) is not True:
        print("[selftest NG] disabled_value(!rows.length)")
        return 1
    if disabled_value([{"cond": "dyn", "kind": "disabled"}], env) is not None:
        print("[selftest NG] disabled_value(dyn)")
        return 1
    # visible_chain が空でも visible_if の式で初期表示を決める（読込中など）。鎖があれば鎖を優先する
    if visible_initially({"visible_chain": [], "visible_if": "loading"}, env) is not False:
        print("[selftest NG] visible_initially(visible_if=loading)")
        return 1
    if visible_initially({"visible_chain": [], "visible_if": None}, env) is not True:
        print("[selftest NG] visible_initially(条件なし)")
        return 1
    if visible_initially({"visible_chain": [{"cond": "rows.length > 1", "kind": "and"}], "visible_if": "loading"}, env) is not False:
        print("[selftest NG] visible_initially(鎖の優先)")
        return 1
    # 固定文は文字列リテラルだけの要素。名前か example の文言で結ぶ（ラベル付きの要素は固定文ではない）
    lit = literal_text_elements([{"jsx_elements": [
        {"text": "見出し", "visible_chain": []},
        {"label": "店舗", "text": "店舗", "visible_chain": []},
    ]}])
    if [el["text"] for el in linked_fixed_elements(["画面見出し", "見出し"], lit)] != ["見出し"]:
        print("[selftest NG] linked_fixed_elements")
        return 1
    if linked_fixed_elements(["店舗"], lit) != []:
        print("[selftest NG] linked_fixed_elements(label 付きを固定文にした)")
        return 1
    print("[selftest OK] _initial_eval")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    print("usage: _initial_eval.py --selftest")
