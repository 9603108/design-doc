#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_vs_source.py — 中間 JSON の記述を、ソースの実物と機械で突き合わせる(stdlib のみ)

【目的】
LLM が書いた文言・例示値が、ソース(Python / TSX)の文字列に実在するかを照合し、作り話を止める。
今あるのは B4(メッセージ文言)・D1 後半(S1 の example)・B1(式)・B2(画面項目)・B5(処理の種別。ERROR で終了コード 1)・B3(応答項目の型と null 可。null 可の違いは ERROR)・F2(境界値と DB 例外。WARN)・F3(初期取得の順。WARN)・B1 の事実(実列と別名・CTE・副問合せ・集計の単位・例外の code。ERROR、集計の単位だけ WARN)。F の照合は同じファイルへ足す(改良案 0.2)。

【意味合い】
- 戻りは verify_partials と同じ「[種別] 場所: 内容 → 差し戻し先 <単位>」。先頭に [WARN] を付けた行は終了コードに影響しない。
  新しい検査はまず WARN で入れ、2回の実走で誤検出が無ければ ERROR へ上げる(改良案 0.2)
- 文字列の抽出は「ある」ことの確認用なので、取りすぎる方(比較の < > まで JSX の文言として拾う)は許す。
  取りすぎは WARN を減らす向き(見逃し)にしか働かず、作り話の誤検出は増やさない

【接続情報】
  - 呼出元: run_units.AFTER(verify_intermediate の直前) / verify_partials.main_units(S1 の example を collect で照合)
  - 入力: target/intermediate/<機能名>.json(合成後の中間 JSON)と screen_config の source_files、
          中間 JSON の src[].file とその同じフォルダの .py(補助モジュール)
  例: python3 target/scripts/verify_vs_source.py <機能名>
"""
import ast
import html
import json
import re
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
import tempfile
from pathlib import Path

# TSX の文字列: 引用符の文字列 / テンプレート(${…} は定数部だけ残す) / JSX の文言(タグの間の字面)
_QUOTED = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')
_TEMPLATE = re.compile(r"`([^`]*)`")
# WHY: JSX の文の途中の式({shops.length} など)は実行時の値なので、文の中の {…} は取った文言の中に残す(穴として照合する)
_JSX_TEXT = re.compile(r">((?:[^<>{}]|\{[^{}]*\})+)<")
# WHY: 強調・コードなどのインライン要素は文の途中に入るが、文は1つなので、照合用にはタグを外す
_INLINE_TAG = re.compile(r"</?(?:strong|em|b|i|code|span|small|a|kbd)\b[^>]*>")
_HOLE = re.compile(r"\{[^{}\s]+\}")
# WHY(round14 R2): JSX は、改行を含む空白がタグ・式の境界に接すると、その空白を捨てる(Babel の cleanJSXElementLiteralChild)。
#   「こちらは⏎        <strong>POSが…」は画面で「こちらはPOSが…」と出る。norm は改行を空白1つにするので「こちらは POSが…」になり、
#   画面どおりの全文(S1 が正しく書いた例示値)が1字の空白の差で WARN になった(run13 の IT63)。タグを外す前に、この空白を落とした版も作る
_NL_WS = r"[ \t]*\r?\n\s*"
_INLINE_TAG_RX = _INLINE_TAG.pattern


def _jsx_faithful(text):
    """TSX の本文から、改行を含む空白が強調・コード等のインライン要素の境界に接する所の空白を落とし、タグを外した文字列。
    画面に出る字面(JSX の空白の規則)に近い版。呼出元: collect_source_strings(従来の版に足す。厳しくせず、通る例を増やす側だけ)。"""
    t = re.sub(rf"{_NL_WS}(?=({_INLINE_TAG_RX}))", "", text)
    t = re.sub(rf"({_INLINE_TAG_RX}){_NL_WS}", r"\1", t)
    return _INLINE_TAG.sub("", t)


def norm(s):
    """空白をまとめ、末尾の「。」と前後の空白を外す。呼出元: collect_source_strings・check_*(両辺を同じ形で比べる)。"""
    return re.sub(r"\s+", " ", s).strip().rstrip("。").strip()


class _Pat:
    """テンプレートリテラル・JSX の式の並びを、固定部分は字面・穴({…} や ${…})は任意の値とみなす正規表現にしたもの。
    WHY: 穴で割った断片だけを集めると、穴をまたぐ語順(「（レジ2）： 」など)が照合できず、正しい表示まで WARN になる。"""
    __slots__ = ("text", "rx")

    def __init__(self, text, rx):
        self.text, self.rx = text, rx

    def __hash__(self):
        return hash(("pat", self.text))

    def __eq__(self, other):
        return isinstance(other, _Pat) and other.text == self.text


def _skip_quoted(text, j):
    """text[j] の引用符が閉じる位置の次を返す。TSX の文字列は1行なので改行で止める。"""
    q, k = text[j], j + 1
    while k < len(text) and text[k] != q and text[k] != "\n":
        k += 2 if text[k] == "\\" else 1
    return k + 1


def _read_template(text, i):
    """text[i] の ` で始まるテンプレートリテラルの中身(${…} を含む生の文字列)と、閉じた次の位置を返す。"""
    j, out = i + 1, []
    while j < len(text):
        c = text[j]
        if c == "\\":
            out.append(text[j:j + 2])
            j += 2
        elif c == "`":
            return "".join(out), j + 1
        elif text.startswith("${", j):
            end = _skip_expr(text, j + 2)
            out.append(text[j:end])
            j = end
        else:
            out.append(c)
            j += 1
    return "".join(out), j


def _skip_expr(text, j):
    """{ や ${ の直後の位置 j から、対応する } の次の位置を返す。文字列と入れ子の `…` は飛ばす。
    WHY: 式の中の { } を数えないと、JSX の式の並びの終わりを誤る(「{r.reji_no ? `（レジ${r.reji_no}）` : ''}」は入れ子の { } を持つ)。"""
    depth = 1
    while j < len(text):
        c = text[j]
        if c in "\"'":
            j = _skip_quoted(text, j)
            continue
        if c == "`":
            _, j = _read_template(text, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def _split(text, dollar):
    """穴で区切った固定部分の列(穴の数は len(戻り値) - 1)。dollar=True は ${…}(テンプレート)、False は {…}(JSX の式)。"""
    parts, buf, j = [], [], 0
    while j < len(text):
        if (dollar and text.startswith("${", j)) or (not dollar and text[j] == "{"):
            parts.append("".join(buf))
            buf = []
            j = _skip_expr(text, j + (2 if dollar else 1))
        else:
            buf.append(text[j])
            j += 1
    parts.append("".join(buf))
    return parts


def _jsx_runs(text):
    """TSX の JSX の文言の並び(タグの > と次の < の間)を、式の { … } を含む生の文字列で返す。
    WHY: _JSX_TEXT の正規表現は式の中の { } で切れるので、入れ子の式を含む文言は別の方法で拾う。"""
    runs, i, n = [], 0, len(text)
    while i < n:
        if text[i] != ">":
            i += 1
            continue
        j, buf = i + 1, []
        while j < n and text[j] != "<":
            if text[j] == "{":
                end = _skip_expr(text, j + 1)
                buf.append(text[j:end])
                j = end
            else:
                buf.append(text[j])
                j += 1
        runs.append("".join(buf))
        i = j
    return runs


def _lit_rx(s):
    """固定部分の字面を正規表現にする。空白の違いは \\s* で吸収する(JSX の改行・インデントは norm で潰れるため)。"""
    return r"\s*".join(re.escape(p) for p in re.split(r"\s+", s))


def _make_pat(text, parts):
    """固定部分の列から _Pat を作る。固定部分の字(空白を除く)が無いもの(穴だけ)は作らない。
    WHY: 穴だけの文言({error} など)は何にでも当たるため。値がまるごと実行時に決まる項目は check_examples が別に通す。"""
    if not "".join(re.sub(r"\s+", "", p) for p in parts):
        return None
    return _Pat(text, re.compile(".*?".join(_lit_rx(p) for p in parts), re.S))


def _patterns(text):
    """TSX の文字列から、テンプレートリテラル(${…})と JSX の式の並び({…})を _Pat の列にする。"""
    out, i = [], 0
    while i < len(text):
        if text[i] != "`":
            i += 1
            continue
        body, i = _read_template(text, i)
        out.append(_make_pat(body, _split(body, True)))
    out += [_make_pat(run, _split(run, False)) for run in _jsx_runs(text) if "{" in run]
    return [p for p in out if p is not None]


def _pat_hit(text, strings):
    """text(正規化済みの生の文字列)が、ソースのテンプレート・JSX の式の並びの正規表現のどれかに当たるか。呼出元: _in_source・_example_in_source。"""
    return any(s.rx.search(text) for s in strings if isinstance(s, _Pat))


def _src_body(src):
    """出典(file・line_start・line_end。1 始まり)の範囲の本文。読めなければ None。"""
    try:
        lines = Path(src["file"]).read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[src["line_start"] - 1:src["line_end"]])
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _wholly_runtime(src):
    """項目の出典の本文に、固定の字面が1つも無く式({error} など)だけで出る文言があるか。
    WHY: String(e) のように値がそのまま実行時に決まる要素は、例示の字面を照合しても意味が無いので、例示の形を問わず通す。
    呼出元: check_examples。出典が読めないときは False(照合する)。"""
    for s in src or []:
        body = _src_body(s) if isinstance(s, dict) else None
        for run in _jsx_runs(body or ""):
            if "{" in run and not re.sub(r"\s+", "", "".join(_split(run, False))):
                return True
    return False


# WHY(2026-10-15 run15 差7): 画面の文面が「"A" + " — B"」「(x ?? "A") + " — B"」のように文字列の + 連結で組まれていると、
#   リテラルを1つずつ集めただけでは連結後の全文(画面に出る文面)がどの文字列にも当たらず、正しい文言を「ソースに無い」と誤検出した。
#   連結(+)でつながるリテラル・テンプレートは、つないだ全文でも持つ。間に挟まる変数(" + res.status + ")は穴として扱う。
#   取りすぎる方(取りこぼしでなく余分に持つ)は見逃しにしか働かず、作り話の誤検出は増やさない(上の【意味合い】のとおり)
_CONCAT_GAP = re.compile(r"^[\s)]*\+\s*(?:([^\s+;,{}\"'`]+)\s*\+\s*)?[\s(]*$")
_CONCAT_GAP_MAX = 80


def _unescape_quoted(s):
    """引用符の文字列の \\" \\' \\\\ \\n などを実際の字にする。呼出元: _concat_strings。"""
    return re.sub(r"\\(.)", lambda m: {"n": " ", "t": " "}.get(m.group(1), m.group(1)), s)


def _concat_strings(text):
    """TSX/TS の文字列リテラル・テンプレートが + でつながる並びを、つないだ全文として返す(穴の無いものは norm 済みの str、
    間に式・${…} を挟むものは _Pat)。呼出元: collect_source_strings。
    つながりの判定: 直前のリテラルの終わりと次のリテラルの始まりの間が「閉じ括弧・空白 + (式 +)? 空白・開き括弧」だけであること。"""
    toks, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            end = _skip_quoted(text, i)
            if end - 1 > i and text[end - 1] == c:
                toks.append((i, end, [_unescape_quoted(text[i + 1:end - 1])]))
                i = end
            else:
                i += 1  # 閉じない引用符(JSX の文言の撇号・コメント)は文字列ではない
        elif c == "`":
            body, end = _read_template(text, i)
            toks.append((i, end, _split(body, True)))
            i = end
        else:
            i += 1
    chains, cur, prev_end = [], [], None
    for s, e, parts in toks:
        gap = _CONCAT_GAP.match(text[prev_end:s]) if prev_end is not None and s - prev_end <= _CONCAT_GAP_MAX else None
        if gap is not None and cur:
            if gap.group(1):
                cur.append(None)  # 間の式は穴
            cur.append(parts)
        else:
            if len(cur) > 1:
                chains.append(cur)
            cur = [parts]
        prev_end = e
    if len(cur) > 1:
        chains.append(cur)
    out = []
    for chain in chains:
        merged, hole_pending = [""], False
        for piece in chain:
            if piece is None:
                hole_pending = True
                continue
            if hole_pending:
                merged.append("")
                hole_pending = False
            merged[-1] += piece[0]
            for p in piece[1:]:  # テンプレートの ${…} は穴
                merged.append(p)
        if len(merged) == 1:
            out.append(norm(merged[0]))
        else:
            pat = _make_pat("{_}".join(merged), merged)
            if pat is not None:
                out.append(pat)
    return out


def _py_fold_concat(node):
    """Python の「"A" + "B"」(BinOp の Add)を1つの文字列にする。文字列でない項があれば None。呼出元: collect_source_strings。
    WHY: ast の Constant は + でつながった全文を持たないため(TSX の _concat_strings と同じ事情)。f-string の穴は {_}。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{_}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _py_fold_concat(node.left), _py_fold_concat(node.right)
        return None if a is None or b is None else a + b
    return None


def collect_source_strings(files):
    """files(Path の列)の文字列を正規化して返す(set)。読めない・構文が壊れたファイルは飛ばす。
    Python は ast の Constant(str)と JoinedStr の定数部と + の連結、TSX/TS は引用符・テンプレートの定数部・JSX の文言と + の連結。"""
    out = set()
    for f in files:
        try:
            text = Path(f).read_text(encoding="utf-8")
        except OSError:
            continue
        if str(f).endswith(".py"):
            try:
                for n in ast.walk(ast.parse(text)):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        out.add(norm(n.value))
                    elif isinstance(n, ast.JoinedStr):
                        # f-string は穴を {_} にした型文も入れる(呼出の実引数で展開した文言が、この型文に当てはまるかを _in_source が見る)
                        out.add(norm("".join(v.value if isinstance(v, ast.Constant) else "{_}" for v in n.values)))
                    elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                        folded = _py_fold_concat(n)
                        if folded is not None:
                            out.add(norm(folded))
            except SyntaxError:
                continue
        else:
            for m in _QUOTED.finditer(text):
                out.add(norm(m.group(1) or m.group(2) or ""))
            for m in _TEMPLATE.finditer(text):
                out.update(norm(p) for p in re.split(r"\$\{[^}]*\}", m.group(1)))
            out.update(norm(m.group(1)) for m in _JSX_TEXT.finditer(text))
            # WHY: <strong>や<code>の境界で文が切れている(「<strong>取込欠損の疑い</strong>：…」)ので、インライン要素のタグを外した版からも文を取る
            out.update(norm(m.group(1)) for m in _JSX_TEXT.finditer(_INLINE_TAG.sub("", text)))
            # WHY(round14 R2): 画面は、改行の空白がタグに接する所で空白を持たない(JSX の規則)。その版の文も持つ(上の版は残す)
            out.update(norm(m.group(1)) for m in _JSX_TEXT.finditer(_jsx_faithful(text)))
            # WHY: テンプレートと式の並びは、字面を穴で割った断片に加えて、穴を任意の値とみなす正規表現(_Pat)としても持つ
            out.update(_patterns(_INLINE_TAG.sub("", text)))
            # WHY(run15 差7): + で連結した文字列の全文も持つ(画面に出る文面は連結後)。タグを外す前の素の本文から取る(引用符の位置が変わらないように)
            out.update(_concat_strings(text))
    out.discard("")
    return out


def source_files(feat):
    """feat の照合に使うソースのパス(重複なし)。screen_config の source_files、中間 JSON の src[].file、
    backend の src の同じフォルダの .py(auth.py など補助モジュール)。呼出元: source_strings。"""
    from _config_loader import INTERMEDIATE_DIR, get_source_files
    files = [p for p in get_source_files(feat)]
    jp = Path(INTERMEDIATE_DIR) / f"{feat}.json"
    if jp.exists():
        def walk(n):
            if isinstance(n, dict):
                for k, v in n.items():
                    if k == "src" and isinstance(v, list):
                        yield from (Path(s["file"]) for s in v if isinstance(s, dict) and s.get("file"))
                    else:
                        yield from walk(v)
            elif isinstance(n, list):
                for v in n:
                    yield from walk(v)
        files += list(walk(json.loads(jp.read_text(encoding="utf-8"))))
    files += [q for p in list(files) if p.suffix == ".py" and p.parent.is_dir() for q in p.parent.glob("*.py")]
    return list(dict.fromkeys(files))


def source_strings(feat):
    """source_files の文字列の集合。呼出元: main・verify_partials.main_units。"""
    return collect_source_strings(source_files(feat))


def _in_source(text, strings):
    """text(正規化済み)がソースのどれかの文字列に含まれるか。{key} などの穴は、穴の間の定数部がどれかの文字列に
    全部含まれるかで見る(穴の中身は実行時に決まるため)。"""
    chunks = [c.strip() for c in _HOLE.split(text) if len(c.strip()) >= 2]
    lits = [s for s in strings if isinstance(s, str)]
    if not chunks or any(all(c in s for c in chunks) for s in lits):
        return True
    if _pat_hit(text, strings):
        return True
    # 展開済みの文言(例 shop_id は正の整数で指定してください)は、穴({_})を持つソースの f-string の型文に当てはまれば実在とみなす
    return any("{_}" in s and len("".join(s.split("{_}")).strip()) >= 2 and re.search("(?:.+?)".join(re.escape(p) for p in s.split("{_}")), text) for s in lits)


def _msg_fulltext(data):
    """messages[] の message が、src の範囲(TSX/JSX で、範囲の先頭が開始タグ)の画面の文の固定文を全部含むか。欠けは ERROR(差し戻し先 H4)。
    WHY(2026-10-15 run18): 従来の文言照合(_in_source)は「message がソースの文字列に含まれるか」なので、本文の頭の見出し(強調要素の
    「取込欠損の疑い」と区切りの「：」)が落ちた部分文字列も通った(MSW00001)。画面の文は src の範囲に全部ある事実なので、範囲の全文の側から
    message を照らし、見出し・前後の固定文の欠けを捕まえる。空白は画面でも見えないので全部除いて比べる。
    決まらない式({…})で断片が分かれるため、断片ごとに message(の穴の前後を含めた全体)に入るかを見る。範囲の先頭が開始タグでない src
    (属性の行・コードの行・py の raise)は照らさない。"""
    from _compose_fixed_example import fixed_segments

    def sq(s):
        return "".join(html.unescape(s).split())

    def has_text(seg):
        return re.search(r"[0-9A-Za-z぀-ヿ㐀-鿿]", seg) is not None

    out, cache = [], {}
    for m in data.get("messages") or []:
        if not isinstance(m, dict) or not m.get("message"):
            continue
        srcs = m.get("src") or []
        if isinstance(srcs, dict):
            srcs = [srcs]
        body = sq(str(m["message"]))
        for s in srcs:
            f = str((s or {}).get("file") or "")  # fallback-ok: file が無い src は読めないので下の拡張子判定で飛ばす
            if not f.endswith((".tsx", ".jsx")):
                continue
            if f not in cache:
                try:
                    cache[f] = Path(f).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    cache[f] = None
            if cache[f] is None:
                continue
            a = s.get("line_start")
            b = s.get("line_end") or a  # fallback-ok: 終了行が無い src は開始行だけの1行
            if not isinstance(a, int) or not isinstance(b, int):
                continue
            segs = fixed_segments(cache[f], a, b)
            missing = [g for g in (segs or []) if has_text(g) and sq(g) not in body]
            if missing:
                out.append(f"[ERROR] [文言] messages[{m.get('message_id')}]: 画面の文(src {Path(f).name}:{a}-{b})の「{missing[0][:40]}」が本文に無い(見出し・区切りの「：」・前後の固定文を落とさず全文を写す) → 差し戻し先 H4")
                break
    return out


def check_messages(data, strings):
    """messages[]/messages_backend[] の message と、event_processes[].details の「…」の画面文言が、ソースに実在するか。"""
    problems = []
    for key in ("messages", "messages_backend"):
        for m in data.get(key) or []:
            t = norm(m.get("message") or "") if isinstance(m, dict) else ""
            if t and not _in_source(t, strings):
                problems.append(f"[WARN] [文言] {key}[{m.get('message_id')}]: 「{t}」がソースの文字列に無い → 差し戻し先 H4")
    # WHY: 本文は記号(：、。など)で始めない。強調要素の文言を先頭に付けて直す(N10)。run9 の MSW00001 は messages にあるので、messages と messages_backend の両方を見る
    for key in ("messages", "messages_backend"):
        for m in data.get(key) or []:
            t = str(m.get("message") or "") if isinstance(m, dict) else ""
            if t[:1] and t[:1] in "：:、。 　":
                problems.append(f"[ERROR] [文言] {key}[{m.get('message_id')}]: 本文が記号「{t[:1]}」で始まる。強調要素の文言を先頭に付けるか記号を除く → 差し戻し先 H4")
    problems += _msg_fulltext(data)
    for ep in data.get("event_processes") or []:
        for line in ep.get("details") or []:
            for q in re.findall(r"「([^」]+)」", line) if isinstance(line, str) else []:
                if "…" not in q and not _in_source(norm(q), strings):
                    problems.append(f"[WARN] [文言] event_processes[{ep.get('event_code') or ep.get('event_ref')}].details: 「{q}」がソースの文字列に無い → 差し戻し先 S3")
    return problems


_EX_NUMBER = re.compile(r"[0-9０-９][0-9０-９,.，．]*")


def _example_in_source(text, strings):
    """S1 の例示値が、ソースの文字列に実在するか。数字の並び(金額・件数・日付)は実行時の値なので穴 {_} にしてから _in_source で見る。
    単位・記号・固有の語は字面のまま照合する。
    WHY: 例示値は実データの一例で、書式に合う数字まで字面で照合すると、正しい書式の例示が WARN になる(POS 会計レポートの S1 で発生)。
    呼出元: check_examples(verify_partials.check_examples も同じ関数を呼ぶので、両検査の基準は揃う)。"""
    # WHY: 「3 点 / 4,500 円」のように区切った例示は、区切りごとに別々に照合する。画面では点と円が別のセルに出るので、ソースも別々の文字列を持つ
    whole = norm(text)
    # WHY: 穴をまたぐ例示(「横浜店（レジ2）： 2026-10-05 / 2026-10-09」)は、 / で割る前に式の並びの正規表現で見る(割ると「：」の前後の穴が別々になる)
    if _pat_hit(whole, strings):
        return True
    return all(_pat_hit(part, strings) or _in_source(_EX_NUMBER.sub("{_}", part), strings) for part in whole.split(" / "))


# WHY(round14 R2): チェックの状態(オン・オフ)と表の行数(22行(空欄項目を除くと16行))は、ソースに字面が無い導出の値。
#   字面で照合すると S1 が「空欄項目も表示」「22」のように検査に合わせた不自然な例へ変えた(run13)。この2つの形だけ照合しない。
_STATE_EXAMPLES = ("オン", "オフ", "ON", "OFF", "チェックあり", "チェックなし", "選択", "未選択")
_ROW_COUNT_EX = re.compile(r"[0-9０-９]+\s*行(?:\s*[（(][^）)]*?[0-9０-９]+\s*行\s*[）)])?")


def _derived_example(item):
    """例示値が、ソースの字面を持たない導出の値の形か。Checkbox・RadioButton の状態語、Grid の行数(N行・N行(…を除くとM行))。
    呼出元: check_examples。形が合うものだけ。他の語を足して通さない。"""
    ex = norm(str(item.get("example") or ""))
    kind = item.get("item_type")
    if kind in ("Checkbox", "RadioButton"):
        return ex in _STATE_EXAMPLES
    if kind == "Grid":
        return bool(_ROW_COUNT_EX.fullmatch(ex))
    return False


def check_examples(items, strings):
    """S1 の screen_layout.items[].example が、ソースの文字列に実在するか(D1 後半)。"""
    return [f"[WARN] [例示値] screen_layout.items[{i.get('item_name')}]: example「{i['example']}」がソースの文字列に無い → 差し戻し先 S1"
            for i in items if isinstance(i, dict) and i.get("example") and not _wholly_runtime(i.get("src")) and not _derived_example(i)
            and not _example_in_source(norm(str(i["example"])), strings)]


def _name_variants(names):
    """画面での名前(label・csv)の字面の揺れ。括弧を全角に、番号の数字を除き、括弧の中身も候補にする(例 小計3(税込) → 小計（税込）・税込)。"""
    out = set()
    for n in names:
        w = n.replace("(", "（").replace(")", "）")
        out |= {n, w, re.sub(r"\d+", "", w)}
        out.update(re.findall(r"（([^）]+)）", w))
    return {v for v in out if len(v) >= 2}


def check_calcs(calcs, candidates):
    """H4 の calculations[] を react 下書きの calc_candidates と突き合わせる(B1。WARN)。
    対: 計算の name が候補の label と同じ。対の無い候補は [式の漏れ]。対の式は、項目の名前・切り捨ての語・数字を見る。"""
    problems, paired = [], set()
    for c in calcs or []:
        cand = next((k for k in candidates or [] if k.get("label") and k["label"] == c.get("name")), None)
        if cand is None:
            continue
        paired.add(cand["id"])
        text = (c.get("formula") or "") + " " + " ".join(i.get("name", "") for i in c.get("inputs") or [])
        where = f"calculations[{c.get('name')}]"
        for o in cand.get("operands") or []:
            vs = _name_variants((cand.get("operand_labels") or {}).get(o) or [])
            if vs and not any(v in text for v in vs):
                problems.append(f"[WARN] [式] {where}: ソースの式 {cand['expr']} の {o}({'・'.join(sorted(vs)[:2])})が式に無い → 差し戻し先 H4")
        # WHY: num() 等を通らないオペランドを持つ式に「値が無いときは 0」と書くと、ソースに無い扱いを書くことになる(run9 の CA1)
        guard = cand.get("operand_guard") or {}
        nones = [o for o in cand.get("operands") or [] if guard.get(o) == "none"]
        if nones and zero_fill_phrase(c.get("remarks")):
            problems.append(f"[ERROR] [式の欠損] {where}: 値が無いときの扱いはソースに無い(オペランド {'・'.join(nones)} が num() 等を通らない)。「0 として扱う」を外す → 差し戻し先 H4")
        words = (c.get("formula") or "") + (c.get("remarks") or "")
        if "Math.floor" in cand.get("funcs", []) and not re.search(r"切り捨|切捨|FLOOR|floor", words):
            problems.append(f"[WARN] [式] {where}: ソースは Math.floor(切り捨て)だが式に切り捨ての語が無い → 差し戻し先 H4")
        for w in ("四捨五入", "切り上げ", "切上"):
            if w in words and not {"Math.round", "Math.ceil"} & set(cand.get("funcs", [])):
                problems.append(f"[WARN] [式] {where}: 「{w}」はソースの式に無い → 差し戻し先 H4")
        # WHY: 入力の名前(例 合計2(支払科目計))の数字は列の名前で、式の定数ではない。名前を外してから数字を見る
        body = c.get("formula") or ""
        for i in c.get("inputs") or []:
            if i.get("name"):
                body = body.replace(i["name"], "")
        for n in re.findall(r"\d+(?:\.\d+)?", re.sub(r"[（(][^）)]*[）)]", "", body)):
            if n not in (cand.get("literals") or []):
                problems.append(f"[WARN] [式] {where}: 式の数字 {n} がソースの定数 {cand.get('literals')} に無い → 差し戻し先 H4")
    problems += [f"[WARN] [式の漏れ] calc_candidates[{k['id']}]「{k.get('label')}」: ソースの式 {k['expr']} に対の calculations が無い → 差し戻し先 H4"
                 for k in candidates or [] if k["id"] not in paired]
    return problems


# WHY: 値が無いときを「0 として扱う」と書いた備考の形(check_calcs が見る)
_ZERO_FILL = re.compile("0 として|値が無いとき[^。]*0|欠損[^。]*0")
# WHY(run20): 「計算は 0 補完をしない」のような否定の文、「表示は値が無いと 0 になる」のような計算以外(表示)の文は、0 補完を書いたことにならない
_NEGATION = re.compile("ない|せず|なし|ず、|ません|除く|外す")
_DISPLAY_WORDS = re.compile("表示|画面")


def zero_fill_phrase(remarks):
    """remarks のうち「0 として扱う」と書いた語句(節ごとに見る。否定の節・表示を述べる節は拾わない)。無ければ None。"""
    for sent in re.split("[。\n]", remarks or ""):
        for m in _ZERO_FILL.finditer(sent):
            # 語句をまたぐ読点までを、その語句の節とする
            a = sent.rfind("、", 0, m.start()) + 1
            b = sent.find("、", m.end())
            clause = sent[a:] if b < 0 else sent[a:b]
            if not (_NEGATION.search(clause) or _DISPLAY_WORDS.search(clause)):
                return m.group(0)
    return None


_OPERATION_TYPES = {"ComboBox", "Calendar", "Checkbox", "Input", "Button", "TextArea"}
# jsx_elements のうち表示専用の要素(_react_facts._DISPLAY_HINT の tag と同じ)。check_items の (1)〜(3) の対象外
_DISPLAY_TAGS = {"span", "strong", "h1", "h2", "a", "table"}


def check_items(items, jsx, display_rows=None):
    """S1 の screen_layout.items[] を react 下書きの jsx_elements と突き合わせる(B2。WARN)。
    (1) ラベルを持つ JSX 要素に同じ字面の item_name が無い [画面項目] 欠け / (2) 入力・操作系の item に対の JSX 要素が無い
    [画面項目] ソースに無い項目 / (3) item_type と item_type_hint が違う [種別]。
    (4) display_rows(定数配列の行名)が item_name に全部ある(無ければ ERROR。WARN の接頭辞を付けない)。既存の行名に括弧書きを足した item_name(「小計（現金計）」)は ERROR。行名そのもの・jsx の label と字面が一致する項目名は通す。
    表示専用の要素(Label・Grid・Link。label は空)は (1)〜(3) の対象外。jsx が None なら (4) だけ見る。
    呼出元: main(最終)・verify_partials.check_unit_extras(S1 の単位)。"""
    by_name = {i.get("item_name"): i for i in items if isinstance(i, dict)}
    labels = list(dict.fromkeys(lb for r in display_rows or [] for lb in _row_labels(r)))
    lack = [lb for lb in labels if lb not in by_name]
    # WHY: 行名そのもの(括弧付きの行名も含む)や react の jsx の label と一字一句同じ項目名は、括弧書きの足し方ではないので通す
    known = set(labels) | {j["label"] for j in jsx or [] if isinstance(j, dict) and j.get("label")}
    # WHY: 既存の行名に括弧書きを足した項目名(「小計（現金計）」)は行名と字面が違うので、表示行の名前が1つに決まらない。行名どおりに書かせる
    split = [f"[画面項目] screen_layout.items[{name}]: 表示行の行名「{lb}」に括弧書きを足した名前。行名どおり「{lb}」と書く → 差し戻し先 S1"
             for name in by_name if isinstance(name, str) and name not in known for lb in labels
             if name.startswith(lb) and name[len(lb):len(lb) + 1] in ("（", "(")]
    disp = ([f"[画面項目] screen_layout.items: react 下書きの display_rows の行名 {', '.join(lack)} が無い → 差し戻し先 S1"]
            if lack else []) + split
    if jsx is None:
        return disp
    by_label = {norm(j["label"]): j for j in jsx or [] if j.get("label")}
    problems = disp + [f"[WARN] [画面項目] screen_layout.items: ソースの {j['tag']}「{lb}」({j['item_type_hint']})に対応する item_name が無い(欠け) → 差し戻し先 S1"
                       for lb, j in by_label.items() if lb not in by_name]
    for name, it in by_name.items():
        if it.get("item_type") not in _OPERATION_TYPES:
            continue
        j = by_label.get(norm(name or ""))
        if j is None:
            problems.append(f"[WARN] [画面項目] screen_layout.items[{name}]: ソースに対応する入力・操作系の要素が無い項目 → 差し戻し先 S1")
        elif j["item_type_hint"] != it["item_type"]:
            problems.append(f"[WARN] [種別] screen_layout.items[{name}]: item_type={it['item_type']} だがソースの要素は {j['tag']}(目安 {j['item_type_hint']}) → 差し戻し先 S1")
    return problems


_KIND_OF = {"入力検査": "input_check", "表示切替": "display_toggle", "活性切替": "enable_toggle", "絞り込み": "data_filter"}


def _overlap(a, b):
    """src の配列 a・b のどれかが同じファイルで行範囲が重なるか。"""
    return any(x.get("file") == y.get("file") and x["line_start"] <= y["line_end"] and y["line_start"] <= x["line_end"]
               for x in a or [] for y in b or [])


def _within(a, b):
    """b の行範囲のどれかが、a の行範囲のどれかの中に入るか(同じファイル)。呼出元: check_kinds(1)。"""
    return any(x.get("file") == y.get("file") and x["line_start"] <= y["line_start"] and y["line_end"] <= x["line_end"]
               for x in a or [] for y in b or [])


def check_kinds(validations, front_processes, behaviors):
    """処理の種別の書き間違いを react 下書きの behaviors と突き合わせる(B5。ERROR)。

    (1) validations(H4 の validations_frontend / 最終の validations)の kind が、字面で対になる behavior の kind と違う [種別]。
        kind=入力検査 なのに画面側に input_check が1件も無い [種別]。
    (2) front_processes[].steps[] の kind=validation が、src で対になる behavior(表示切替・活性切替・絞り込み)で、
        同じ処理に input_check が無い [種別](表示切替を入力検査と書く誤り)。
    呼出元: main。behaviors が空(下書きが古い)なら何も言わない。"""
    if not behaviors:
        return []
    problems, has_input = [], any(b["kind"] == "input_check" for b in behaviors)
    for v in validations or []:
        want = _KIND_OF.get(v.get("kind"))
        if want is None:
            continue
        text = " ".join(str(v.get(k) or "") for k in ("field", "rule", "applied_when"))
        # WHY: 行範囲の中にある behavior だけを候補にする。範囲の外の behavior(control_src だけが範囲に重なるもの)と結ぶと、別の処理の種別と食い違わせる(POS の VL16 は 216 行の表示切替なのに、範囲外の 282 行の活性切替と結ばれて ERROR になった)
        if v.get("src"):
            got = {b["kind"] for b in behaviors if _within(v.get("src"), b["src"])}
        else:
            got = {b["kind"] for b in behaviors if (len(b.get("target") or "") >= 2 and b["target"] in text) or (b.get("cond") and b["cond"] in text)}
        where = f"validations[{v.get('no') or v.get('field')}]"
        if want == "input_check" and not has_input:
            problems.append(f"[種別] {where}: kind=入力検査 だが画面のソースに入力検査(setError・throw・required 等)が無い → 差し戻し先 H4")
        elif got and want not in got:
            problems.append(f"[種別] {where}: kind={v['kind']} だがソースの書き方は {'・'.join(sorted(got))} → 差し戻し先 H4")
    for fp in front_processes or []:
        pair = [b for b in behaviors if _overlap(fp.get("src"), b["src"] + b.get("control_src", []))]
        if not pair or any(b["kind"] == "input_check" for b in pair):
            continue
        for s in fp.get("steps") or []:
            if s.get("kind") == "validation":
                kinds = "・".join(sorted({b["kind"] for b in pair}))
                problems.append(f"[種別] front_processes[{fp.get('process_id')}].steps[{s.get('step_no')}]: kind=validation(入力検査)だがソースの書き方は {kinds}(入力検査ではない) → 差し戻し先 S3")
    return problems


def _cls(t):
    """型の語(TS の型・H3 の data_type のどちらも)を string / number / boolean / array / object に分類する。分からなければ None。"""
    s = re.sub(r"\s*\|\s*(null|undefined)\b", "", str(t or "")).strip().lower()
    if s.endswith("[]") or s.startswith(("array", "配列")):
        return "array"
    # SQL の型名(bigint・varchar など。db_type)も同じ分類に入れる
    for k, words in (("string", ("string", "date", "文字列", "日付", "varchar", "text", "char", "timestamp", "time", "uuid")),
                     ("number", ("number", "int", "bigint", "smallint", "numeric", "decimal", "float", "double", "real", "数値", "整数")),
                     ("boolean", ("boolean", "bool", "真偽")), ("object", ("object", "オブジェクト"))):
        if s.startswith(words):
            return k
    return None


def check_types(api_spec, apis):
    """H3 の response_items の型と null 可を、backend 下書きの apis[action].response_fields(api.ts の型)と突き合わせる(B3)。
    [型]: 型の分類が違う(WARN)。[null可]: nullable が違う(ERROR)。nullable が無い行は data_type の「| null」で見る。
    呼出元: main。response_fields が無い API・下書きに無い key は見ない。"""
    problems = []
    for a in api_spec or []:
        fields = {f["path"]: f for f in ((apis or {}).get(a.get("action")) or {}).get("response_fields") or []}
        for r in a.get("response_items") or []:
            f = fields.get(r.get("key"))
            if f is None:
                continue
            where = f"api_spec[{a.get('action')}].response_items[{r['key']}]"
            # index signature の列は api.ts の型が共用体で当てにならないので、SQL の型(db_type)と null 可(db_nullable。不明なら見ない)で見る
            sql = f.get("index_signature") and f.get("db_type")
            want, exp = (f["db_type"], f.get("db_nullable")) if sql else (f["ts_base"], f["nullable"])
            if _cls(r.get("data_type")) and _cls(want) and _cls(r.get("data_type")) != _cls(want):
                problems.append(f"[WARN] [型] {where}: data_type={r.get('data_type')} だが {'SQL' if sql else 'api.ts'} の型は {want if sql else f['ts_type']} → 差し戻し先 H3")
            got = r["nullable"] if "nullable" in r else "null" in str(r.get("data_type")).lower()
            if exp is not None and bool(got) != exp:
                problems.append(f"[null可] {where}: nullable={bool(got)} だが {'SQL' if sql else 'api.ts'} の型は {want if sql else f['ts_type']}(null 可={exp}) → 差し戻し先 H3")
    return problems


def check_fetch_plan(plan, front_processes):
    """初期表示(front_processes の名前に「初期」を含む処理)の steps が、react 下書き events[0].fetch_plan.mode と同じ取得順を書いているかを見る(F3)。
    parallel なら「並行|同時」、serial なら「待って」の語が要る。逆の語だけがあるのも食い違い。呼出元: main。
    返す行は [WARN] [取得順]。差し戻し先は付けない: この語は migrate_v17_to_v18.py(_react_facts.add_fetch_order_step)が
    初期処理の steps に機械で入れるので、無ければモデルの書き直しでなくスクリプトの不具合(units 表に無い S0 へ戻さない)。
    plan が無い・single は見ない。"""
    mode = (plan or {}).get("mode")
    if mode not in ("parallel", "serial"):
        return []
    out = []
    for p in front_processes or []:
        if "初期" not in str(p.get("name")):
            continue
        t = " ".join(str(s.get("description")) for s in p.get("steps") or [])
        par, wait = bool(re.search(r"並行|同時", t)), "待って" in t
        if (mode == "parallel" and not par) or (mode == "serial" and (par or not wait)):
            want = "「並行」か「同時」" if mode == "parallel" else "「…の応答を待って…を取得する」"
            out.append(f"[WARN] [取得順] {p.get('process_id')}.steps: 下書きの fetch_plan.mode={mode}({plan.get('basis', '')})だが、steps に{want}の書き方が無い → スクリプトの不具合(migrate_v17_to_v18.py が初期処理の steps に入れるはずの行。差し戻しでなく報告する)")
    return out


_SCREEN_RULE_RX = re.compile(r"押せ|押下でき|活性|無効|disabled")


def check_front_rules(merged):
    """FR の手順(front_processes[].steps[].description)に画面規則(ボタンの活性・押せる条件)が書かれていないかを見る(ERROR)。
    画面規則は VL(画面ルール)と項目の備考に書く。呼出元: main。
    WHY: 押せる条件を処理の手順に書くと、呼出・成功・失敗の手順と画面規則が1つの欄に混ざり、S1 の画面ルールと食い違う(POS の FR8 の step1・2 がこれに当たった)。"""
    out = []
    for p in (merged or {}).get("front_processes") or []:
        for i, s in enumerate(p.get("steps") or [], 1):
            d = str(s.get("description") or "")
            if _SCREEN_RULE_RX.search(d):
                out.append(f"[ERROR] [画面規則] {p.get('process_id')} の step{i} に画面規則が書かれている({d[:40]})。ボタンの活性・押せる条件は処理本体の手順ではなく、画面ルール(validations)と項目の備考に書く → 差し戻し先 S3")
    return out


def _boundary_words(b):
    """boundaries の1件から、H4 の rule に要る語の候補(どれか1つ)を返す。候補が決められない検査(集合に無い値など)は None。"""
    if b.get("present") is False:
        return {"order": ("大小", "前後", "逆転"), "span_limit": ("上限", "期間の長さ")}.get(b.get("check"))
    if b.get("impossible_dates_accepted"):
        return ("暦", "実在", "存在しない")
    if re.search(r"<=\s*0\b|<\s*1\b", " ".join(b.get("compare") or [])):
        return ("正の整数", "0以下", "0 以下", "1以上", "1 以上")
    if "\\d{4}-\\d{2}-\\d{2}" in str(b.get("pattern")):
        return ("YYYY-MM-DD", "形式", "書式")
    return None


def check_boundaries(validations, api_spec, apis, common_errors):
    """境界値と DB 例外を、backend 下書きの boundaries・common_errors と突き合わせる(F2。WARN)。
    [境界値]: 下書きの境界値(正の整数・日付の書式・暦に無い日付を通すこと・大小や期間上限の検査が無いこと)の語が、
              H4 の validations の rule・field・applied_when のどこにも無い(差し戻し先 H4)。
    [DB例外]: common_errors で status が取れた共通エラー(数値のもの)が api_spec の error_patterns に無い、または
              api_spec の 5xx がソースの common_errors・その API の error_patterns に無い(差し戻し先 S2)。
    呼出元: main。下書きの boundaries が空なら境界値は見ない。status が式のもの(getattr(…) or 400)・via の中で決まるものは見ない。"""
    problems = []
    text = " ".join(str(v.get(k) or "") for v in validations or [] if isinstance(v, dict) for k in ("field", "rule", "applied_when"))
    for act, a in (apis or {}).items():
        for b in a.get("boundaries") or []:
            words = _boundary_words(b)
            if words and not any(w in text for w in words):
                what = b.get("fact") or f"{b.get('param')} の検査 {b.get('check')}"
                problems.append(f"[WARN] [境界値] validations: 下書きの boundaries({act})「{what}」の語({'・'.join(words[:3])})が rule に無い → 差し戻し先 H4")
    shared = {}
    for c in common_errors or []:
        for e in c.get("errors") or []:
            if isinstance(e.get("status_code"), int):
                shared.setdefault(e["status_code"], []).append(c.get("exception"))
    for a in api_spec or []:
        got = {str(e.get("status_code")) for e in a.get("error_patterns") or [] if isinstance(e, dict)}
        own = {str(e.get("status_code")) for e in ((apis or {}).get(a.get("action")) or {}).get("error_patterns") or []}
        where = f"api_spec[{a.get('action')}].error_patterns"
        problems += [f"[WARN] [DB例外] {where}: ソースの共通エラー {sc}({'・'.join(map(str, ex))})が無い → 差し戻し先 S2"
                     for sc, ex in shared.items() if str(sc) not in got]
        problems += [f"[WARN] [DB例外] {where}: status {s} はソースの common_errors にも下書きの error_patterns にも無い → 差し戻し先 S2"
                     for s in sorted(got) if s.startswith("5") and int(s) not in shared and s not in own]
    return problems


# WHY: SQL の関数の形(name( ))。日本語の説明の式と、SQL の式を区別するために使う
_SQL_CALL = re.compile(r"\b[A-Za-z_]+\s*\(")


def _sq(s):
    """SQL の式の字面を比べる形にする(小文字・::型・表の接頭辞・空白を除く)。呼出元: check_backend_facts。"""
    s = re.sub(r"::\w+", "", str(s or "").lower())
    return re.sub(r"\s+", "", re.sub(r"\b[a-z_]\w*\.(?=[a-z_*])", "", s))


def _sq_head(s):
    """_sq の形から FILTER (…) 以降を落とす。FILTER の条件は定数名で書かれる(_WARN_COND 等)ことがあり、字面で比べられないため。"""
    return re.sub(r"filter\(.*", "", _sq(s))


# WHY: 件数の単位を「日数」「N日」と説明しているかは説明文(remarks・description)だけで見る。表示名・見出し・例示値は
#      ソースの文字列をそのまま写すもの(PosReport.tsx の CSV 見出し「日数」「取込欠損疑い日数」)なので対象外
_DAY_TEXT = re.compile(r"日数|日の数")
# WHY: 同じ説明の中に単位の補足(行数・集計単位による)があれば、日数と書いた文は単位の説明として通す
_UNIT_NOTE = re.compile(r"行数|集計単位による")


def _day_unit_claim(text, strings=None, pat=None):
    """説明文 text が、件数の単位を日数として説明しているか(指摘の対象か)。
    ソースの文字列と字面が一致する文言は対象外。単位の補足(行数・集計単位による)があれば対象外。
    呼出元: check_backend_facts (3)・check_item_remarks_units(remarks)。"""
    t = norm(str(text or ""))
    if not t or not (pat or _HAS_DAYS).search(t) or _UNIT_NOTE.search(t):
        return False
    return not (strings and t in strings)


_FILTER_COND = re.compile(r"FILTER\s*\(\s*WHERE\s+(.*)\)\s*(?:::\w+)?\s*$", re.IGNORECASE | re.DOTALL)
_CASE_DATE = re.compile(r"CASE\s+WHEN\s+(.*?)\s+THEN\s+([\w.]+)\s+END", re.IGNORECASE | re.DOTALL)


def _counts_dates(u, cols):
    """count の集計単位 u が、数える対象を日付そのものにしているか(GROUP BY に日付の列が無くても日数になる形)。
    (a) count(DISTINCT 日付の列)。
    (b) count(*) FILTER (WHERE 条件) と同じ条件で日付の列を並べる欄(array_agg(CASE WHEN 条件 THEN 日付の列 END))が同じ DB 操作の columns にある。
        数えた件数はその日付の欄の要素数と同じなので、件数の単位は日付の列を持つ行(日)になる。
    WHY: GROUP BY の列(店・レジ)だけで単位を決めると、日ごとに1行の表を店・レジ単位でまとめた count(*)(=その店・レジの日数)まで日数でないと誤る(run12 の warn_days)。
    判定が付かないとき(式が FILTER で終わらない等)は False(従来どおり GROUP BY の列で見る)。呼出元: _units_with_date_count。"""
    m = re.match(r"\s*DISTINCT\s+(.+?)\s*$", str(u.get("arg") or ""), re.IGNORECASE)
    if m and _is_date_col(_split_col(m.group(1))[1]):
        return True
    own = next((c for c in cols if c.get("alias") == u.get("alias") and c.get("is_expression")), None)
    fm = _FILTER_COND.search(str((own or {}).get("expression") or ""))
    if not fm:
        return False
    want = _sq(fm.group(1))
    return any(c is not own and (cm := _CASE_DATE.search(str(c.get("expression") or ""))) and _sq(cm.group(1)) == want and _is_date_col(_split_col(cm.group(2))[1])
               for c in cols)


def _units_with_date_count(dop):
    """DB 操作 dop の aggregate_units に、counts_dates(count が日付を数えるか。_counts_dates)を足したコピーを返す。呼出元: check_backend_facts (3)・check_item_remarks_units。"""
    return [dict(u, counts_dates=str(u.get("func")).lower() == "count" and _counts_dates(u, dop.get("columns") or [])) for u in dop.get("aggregate_units") or []]


def check_backend_facts(data, apis, common_errors, strings=None):
    """設計書(data)を backend 下書きの事実(apis[action].db_operations の columns・ctes・subqueries・aggregate_units と common_errors)と突き合わせる(ERROR)。
    [別名]: 物理列として書いた名前が SQL の別名(実列に無い) / 集計の別名が null なのにソースは AS を持つ。差し戻し先 H2
    [式]: api_spec の response_items[].expression が実 SQL の式と食い違う(SQL の関数の形を持つ式だけ比べる。日本語の説明は言い換えを許すので見ない)。差し戻し先 S2
    [CTE]: ソースに CTE が無いのに CTE と書く / 定義が空の CTE を参照 / 絞り込みのある CTE を空・条件なしと書く / 本体の集計と書いた別名が CTE の集計。差し戻し先 S2・H2
    [副問合せ]: 別名の無い集計が副問合せの中の集計なのに本体の集計(aggregate)として書いてある。差し戻し先 H2
    [集計単位]: count の説明の「日数」が、数える単位(GROUP BY)に日付の列が無い・日付以外の列も含むのと合わない(WARN)。差し戻し先 S2
        ただし日付そのものを数える count(DISTINCT 日付の列・同じ条件で日付を並べる欄がある FILTER 付き count。_counts_dates)は、GROUP BY が店・レジでも日数なので見ない
    [エラーcode]: api_spec の error_patterns の error_code が、ソースの例外処理の code_default のどれでもない。error_code が空で status が1つの共通エラーに当たる行も(差し戻し先 S2)。condition の code= は見ない。messages の code は見ない(MSE 番号が正。形は verify_intermediate.py)
    呼出元: main(下書きがあるとき)。apis が空なら何も言わない。差し戻し先は units.json の owns に合わせた(dataset・aggregate は H2、expression・説明・error_patterns は S2)。"""
    ops = {act: a.get("db_operations") or [] for act, a in (apis or {}).items()}
    problems, alias_only, real = [], {}, set()
    for olist in ops.values():
        for op in olist:
            for c in op.get("columns") or []:
                if c.get("kind") == "alias_only" and c.get("alias_name"):
                    alias_only.setdefault(c["alias_name"], c.get("alias_expression") or c.get("expression"))
                real.update(s.rsplit(".", 1)[-1] for s in [c.get("real_column")] + list(c.get("source_columns") or []) if s)
    # (1) 物理列として書かれた名前が、実列に無く別名だけにある
    rows = [("logical_names", r) for r in data.get("logical_names") or []] + \
           [(f"db_operations[{op.get('id')}].dataset", r) for op in data.get("db_operations") or [] for r in op.get("dataset") or []]
    seen = set()
    for where, r in rows:
        n = r.get("physical_column") if isinstance(r, dict) else None
        if n in alias_only and n not in real and (where, n) not in seen:
            seen.add((where, n))
            problems.append(f"[別名] {where}: physical_column「{n}」は SQL の別名(式 {alias_only[n]})で実列ではない → 差し戻し先 H2")
    texts = {}  # action → [(場所, 文, 差し戻し先)]。CTE・集計単位の記述を見る
    for op in data.get("db_operations") or []:
        act = (op.get("api_endpoint") or {}).get("action")
        texts.setdefault(act, []).extend((f"db_operations[{op.get('id')}].{k}", op.get(k), "S2") for k in ("remarks", "dataset_name") if op.get(k))
    for a in data.get("api_spec") or []:
        t = texts.setdefault(a.get("action"), [])
        t.append((f"api_spec[{a.get('action')}].description", a.get("description"), "S2"))
        t.extend((f"api_spec[{a.get('action')}].response_items[{r.get('key')}].{k}", r.get(k), "S2") for r in a.get("response_items") or [] for k in ("description", "transform_backend") if r.get(k))
    # (2) 集計: 別名 null・副問合せの集計・CTE の集計
    for op in data.get("db_operations") or []:
        dops = ops.get((op.get("api_endpoint") or {}).get("action")) or []
        units = [u for d in dops for u in d.get("aggregate_units") or []]
        subs = [_sq(s.get("definition")) for d in dops for s in d.get("subqueries") or []]
        doc_alias = {a.get("alias") for a in op.get("aggregate") or [] if a.get("alias")}
        for a in op.get("aggregate") or []:
            fn, where = str(a.get("function")).lower(), f"db_operations[{op.get('id')}].aggregate[{a.get('alias') or a.get('function')}]"
            if not a.get("alias"):
                if any(_sq(f"{fn}({a.get('target')})") in s for s in subs):
                    problems.append(f"[副問合せ] {where}: {fn}({a.get('target')}) は副問合せの中の集計で、本体の集計(aggregate)ではない → 差し戻し先 H2")
                    continue
                miss = [u for u in units if str(u.get("func")).lower() == fn and u.get("alias") and u["alias"] not in doc_alias]
                exact = [u for u in miss if _sq(u.get("arg")) == _sq(a.get("target"))]
                if exact or len(miss) == 1:
                    problems.append(f"[別名] {where}: alias が null だがソースは {(exact or miss)[0]['alias']} という別名を付けている → 差し戻し先 H2")
            elif (a.get("scope") or "main") == "main":
                same = [u for u in units if u.get("alias") == a["alias"] and str(u.get("func")).lower() == fn]
                if same and all(u.get("scope") != "main" for u in same):
                    problems.append(f"[CTE] {where}: 別名 {a['alias']} の集計は {same[0].get('scope')} の中にあり、本体の集計ではない → 差し戻し先 H2")
    # (2) 式: response_items[].expression が実 SQL の式と同じか(FILTER の中の条件は定数名のことがあるので見ない)
    for a in data.get("api_spec") or []:
        cols = {c.get("alias"): c for d in ops.get(a.get("action")) or [] for c in d.get("columns") or [] if c.get("is_expression")}
        for r in a.get("response_items") or []:
            c = cols.get(str(r.get("key")).rsplit(".", 1)[-1])
            # WHY: 日本語の説明(例 期間内の総売上を合計して整数にする)は言い換えを許すので字面では比べない。SQL の関数の形を持つ式だけ比べる
            if c and r.get("expression") and _SQL_CALL.search(str(r["expression"])):
                x, y = _sq_head(r["expression"]), _sq_head(c["expression"])
                fx, fy = set(re.findall(r"[a-z_]+(?=\()", x)), set(re.findall(r"[a-z_]+(?=\()", y))
                # 関数の名前が全部実 SQL にあれば、引数の細部(CASE の展開・外側の array_remove 等)は字面で比べない
                if x != y and x not in y and y not in x and not (fx and fx <= fy):
                    problems.append(f"[式] api_spec[{a.get('action')}].response_items[{r['key']}].expression: 「{r['expression']}」が実 SQL の式「{c['expression']}」と違う → 差し戻し先 S2")
    # (2) CTE の記述
    for act, dops in ops.items():
        ctes = [c for d in dops for c in d.get("ctes") or []]
        for where, t, unit in texts.get(act, []):
            t = str(t)
            if not ctes and re.search(r"CTE|共通表式", t):
                problems.append(f"[CTE] {where}: CTE と書いてあるがソースの SQL に WITH(CTE)が無い → 差し戻し先 {unit}")
            for c in ctes:
                m = re.search(rf"(?:CTE|共通表式)[「『(（\s]*{re.escape(c['name'])}\b|\b{re.escape(c['name'])}\b[」』)）\s]*(?:という|の)?\s*(?:CTE|共通表式)", t)
                if not m:
                    continue
                sent = next((s for s in t.split("。") if m.group(0) in s), t)
                if not str(c.get("definition")).strip():
                    problems.append(f"[CTE] {where}: CTE {c['name']} はソースで定義が空なのに参照している → 差し戻し先 {unit}")
                elif c.get("filtered") and re.search(r"空|絞り込み(?:は)?(?:無|な)し|条件(?:は)?(?:無|な)し|全件", sent):
                    problems.append(f"[CTE] {where}: CTE {c['name']} はソースで WHERE により絞り込むのに「{sent}」と書いている → 差し戻し先 {unit}")
    # (3) count の説明と数える単位(同じ別名の単位が複数の DB 操作にあるときは1件にまとめる)
    unit_seen = set()
    for a in data.get("api_spec") or []:
        for r in a.get("response_items") or []:
            leaf = str(r.get("key")).rsplit(".", 1)[-1]
            for d in ops.get(a.get("action")) or []:
                for u in _units_with_date_count(d):
                    if u.get("alias") == leaf and str(u.get("func")).lower() == "count" and u.get("scope") == "main" and _day_unit_claim(r.get("description"), strings, _DAY_TEXT) and (a.get("action"), r["key"]) not in unit_seen:
                        # 行数が日数になるのは、数える単位が日付の列だけのとき。日付が無い・日付以外(店舗・レジなど)も含むときは WARN(両方向を捕まえる)
                        # WHY: ただし日付そのものを数える count(DISTINCT 日付・同じ条件で日付を並べる欄がある FILTER 付きの count)は、GROUP BY が店・レジでも日数(counts_dates)
                        gb = u.get("group_by") or []
                        dates = [g for g in gb if re.search(r"date|_dt|day|日", g, re.IGNORECASE)]
                        others = [g for g in gb if g not in dates]
                        if (dates and not others) or u.get("counts_dates"):
                            continue
                        unit_seen.add((a.get("action"), r["key"]))
                        why = "日付の列が無い" if not dates else f"日付の列以外({'・'.join(others)})も数える単位に含まれ、行数が日数と合わない"
                        problems.append(f"[WARN] [集計単位] api_spec[{a.get('action')}].response_items[{r['key']}]: 「日数」と書くが count の単位は {u.get('unit')} で{why} → 差し戻し先 S2")
    # (4) エラー code: api_spec の error_patterns の error_code は common_errors の code_default のどれかであること。
    # WHY: code は condition の字面に書かず専用の欄に持つ(合成が status から機械で入れる)。condition の code= は見ない。
    #      messages・messages_backend の code は message_id と同じ MSE 番号で、API の code ではない(形は verify_intermediate.py が見る)
    known = {e.get("code_default") for c in common_errors or [] for e in c.get("errors") or [] if e.get("code_default")}
    for a in data.get("api_spec") or []:
        for ep in a.get("error_patterns") or []:
            if not isinstance(ep, dict):
                continue
            where = f"api_spec[{a.get('action')}].error_patterns"
            if ep.get("error_code"):
                if known and ep["error_code"] not in known:
                    problems.append(f"[エラーcode] {where}: error_code「{ep['error_code']}」はソースの例外処理の code({'・'.join(sorted(known))})に無い → 差し戻し先 S2")
                continue
            # WHY: 候補が1つに決まるときだけ抜けと見る(複数の code に当たる status は、どれを入れるか決められない)
            hit = _codes_for_status(common_errors, ep.get("status_code"))
            if len(hit) == 1:
                problems.append(f"[エラーcode] {where}: status {ep.get('status_code')} はソースの code「{hit[0]}」に当たるが error_code が空 → 差し戻し先 S2")
    return problems


# --- B15: 合成後の DB 名・数え方・選択肢・SQL の文字列・禁止語の照合(すべて ERROR。差し戻し先は各行の末尾) ---
# WHY: 下書きの事実(実列・式・選択肢)と最終の JSON の文言が食い違う所だけを止める。言い換えは見ない。
#      新しく入れる検査なので、誤検出は run8 の実走の件数で確かめる
_HAS_DAYS = re.compile(r"日数|N日|\d+日(?!本)")
_DEFAULT_WORDS = re.compile(r"既定|初期|省略|指定が無い|指定がない|指定しない")
_FORBIDDEN_WORDS = ("DB 例外", "DB例外", "データベースの例外", "DB のエラー", "DBのエラー")
_JOIN_ALIAS = re.compile(r"(?<![A-Za-z0-9_])[a-z]{1,2}\.\w+")
# WHY: 「と」はひらがなの中(ごと・として)に出るので除き、「A と B」のように名詞を括る語だけを拾う
_AND_WORD = re.compile(r"(?<![ぁ-ん])と(?![ぁ-ん])|および")


def _ja(s):
    """ひらがな・カタカナ・漢字を含むか。呼出元: check_literal_transform。"""
    return re.search(r"[ぁ-んァ-ヶ一-龠]", s) is not None


def _split_col(s):
    """表.列 を (表, 列) に分ける。表が無いときは表を空にする。呼出元: check_db_names・check_item_remarks_units。"""
    return str(s).rsplit(".", 1) if "." in str(s) else ["", str(s)]


def _is_date_col(name):
    """列の名前が日付の列か(check (3) と同じ語で見る)。呼出元: check_item_remarks_units。"""
    return re.search(r"date|_dt|day|日", str(name), re.IGNORECASE) is not None


def _row_labels(r):
    """display_rows の1行の行名。labels(現行の形)と specs[].label(契約の形)のどちらでも取る。呼出元: check_items。"""
    return list(r.get("labels") or []) + [s.get("label") for s in r.get("specs") or [] if isinstance(s, dict) and s.get("label")]


def _codes_for_status(common_errors, status):
    """status に当たる共通エラーの code_default(重複なし)。数値の status_code は同じ数で、式の行は式の中の数値リテラルで当てる。
    WHY: 式の既定値(getattr(e, 'status', 400) or 400)は 400 の行として数える。呼出元: check_backend_facts (4)。"""
    out = []
    for c in common_errors or []:
        for e in c.get("errors") or []:
            st, code = e.get("status_code"), e.get("code_default")
            if status is None or not code:
                continue
            hit = (st == status) if isinstance(st, int) else re.search(rf"(?<!\d){status}(?!\d)", str(st or "")) is not None
            if hit and code not in out:
                out.append(code)
    return out


def _draft_cols(apis):
    """backend 下書きの全 DB 操作の columns を1つに並べる(action をまたぐ)。呼出元: check_db_names。"""
    return [c for a in (apis or {}).values() for d in a.get("db_operations") or [] for c in d.get("columns") or []]


def check_db_names(data, apis):
    """DB の名前を、backend 下書きの事実と突き合わせる(B15 a。ERROR)。
    [物理列]: physical_column が空の行で、同じ表の実列が1つに決まる(H2 が補えるもの)/ 句(where・group_by・order_by)の論理列が物理名のまま。
    [結合]: joins の条件に表の別名(s.・p.)が残る。
    [論理表]: 論理表の名前に「(推定)」が付いていない(出所は DDL に無いので、合成が機械で付ける)。
    [論理名]: 論理名に「と」「および」があり、別名1つで複数の列を括っている(別名1つに論理名1つ)。
    呼出元: main(下書きが無くても、最終の JSON の句・結合・論理表は見る)。"""
    names = [r for r in data.get("logical_names") or [] if isinstance(r, dict)]
    ops = [op for op in data.get("db_operations") or [] if isinstance(op, dict)]
    cols = _draft_cols(apis)
    phys = {r.get("physical_column") for r in names if r.get("physical_column")}
    problems = []
    rows = [("logical_names", r) for r in names] + [(f"db_operations[{op.get('id')}].dataset", r) for op in ops for r in op.get("dataset") or [] if isinstance(r, dict)]
    for where, r in rows:
        if not r.get("physical_column") and r.get("alias_name") and r.get("physical_table"):
            tbl = r["physical_table"]
            real = {_split_col(s)[1] for c in cols if c.get("alias_name") == r["alias_name"] for s in c.get("source_columns") or [] if _split_col(s)[0] == tbl}
            if len(real) == 1:
                problems.append(f"[物理列] {where}: 別名 {r['alias_name']} の physical_column が空で、下書きの実列は {tbl}.{next(iter(real))} の1つに決まる → 差し戻し先 H2")
        if r.get("logical_column") and _AND_WORD.search(str(r["logical_column"])):
            problems.append(f"[論理名] {where}: 論理名「{r['logical_column']}」に「と」「および」があり、別名 {r.get('alias_name')} の複数の列を括っている → 差し戻し先 H2")
    for op in ops:
        oid = op.get("id")
        for k in ("where", "group_by", "order_by"):
            for i, c in enumerate(op.get(k) or []):
                if isinstance(c, dict) and c.get("logical_column") in phys:
                    problems.append(f"[物理列] db_operations[{oid}].{k}[{i}]: 論理列「{c['logical_column']}」が物理名のまま → 差し戻し先 H2")
        for i, j in enumerate(op.get("joins") or []):
            cond = str(j.get("condition") or "") if isinstance(j, dict) else ""
            if _JOIN_ALIAS.search(cond):
                problems.append(f"[結合] db_operations[{oid}].joins[{i}].condition「{cond}」に表の別名が残る → 差し戻し先 H2")
    # WHY: 論理表の(推定)は箇所ごとに件数をまとめる(同じ論理表が句の数だけ並ぶため、1行ずつにすると読めない)
    lt = {}
    sections = [("logical_names", names)] + [(f"db_operations[{op.get('id')}].{k}", op.get(k) or []) for op in ops for k in ("dataset", "where", "group_by", "order_by", "joins")]
    for where, rs in sections:
        for r in rs:
            t = r.get("logical_table") if isinstance(r, dict) else None
            if t and not t.endswith("(推定)"):
                lt[(where, t)] = lt.get((where, t), 0) + 1
    problems += [f"[論理表] {where}: 論理表「{t}」に(推定)が無い({n} 箇所) → 差し戻し先 H2" for (where, t), n in lt.items()]
    return problems


def _logical_of_physical(data):
    """論理名の表から、物理列 → 論理列(「(推定)」を除いた名前)。呼出元: check_item_remarks_units。"""
    out = {}
    for r in data.get("logical_names") or []:
        if isinstance(r, dict) and r.get("physical_column") and r.get("logical_column"):
            out.setdefault(r["physical_column"], re.sub(r"\(推定\)$", "", r["logical_column"]))
    return out


def _react_uses(react):
    """react 下書きの field_usage を、応答の項目名(末尾の名前)ごとに使用行(file・行範囲)へ引く。呼出元: check_item_remarks_units。"""
    out = {}
    for f in (react or {}).get("field_usage") or []:
        leaf = str(f.get("path") or "").replace("[]", "").split(".")[-1]
        for u in f.get("uses") or []:
            out.setdefault(leaf, []).extend(u.get("src") or [])
    return out


def _strings_in(node):
    """JSON の値の中の文字列を全部出す。呼出元: check_item_remarks_units(手順の行)。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings_in(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings_in(v)


def check_item_remarks_units(data, apis, react=None, strings=None):
    """画面項目が結ぶ応答項目の件数(count)の単位に日付以外の列があるのに、項目の説明に「日数」「N日」と書いたものを ERROR にする(B15 b)。
    項目との結び: (1)response_to_screen_mapping の画面項目名 (2)react 下書きの field_usage の使用行が入る画面項目(src の行範囲)。どちらでも結ぶ項目を見る。
    WHY: mapping だけでは、店舗の選択肢(IT2)に日数の応答項目が結ばれ、日数の列を説明する会計カードの副題が見られない(run9 で数え方 0 件の原因)。
    WHY: 行数が日数になるのは、数える単位が日付の列だけのとき。店舗・レジ別の行数を日数と書くと、読み手は期間の日数と取り違える。
    加えて、その項目の名前が「日数」を含み「行数」を含まない手順・イベントの行に出るものも ERROR にする(差し戻し先 S3)。CSV の行は見出し等の語で外す。
    差し戻し文に正しい言い方「<単位>の行数」を載せる。呼出元: main(下書きがあるとき)。"""
    items = {i.get("item_name"): i for i in (data.get("screen_layout") or {}).get("items") or [] if isinstance(i, dict)}
    logical = _logical_of_physical(data)
    used = _react_uses(react)
    problems, seen, flagged = [], set(), {}

    def remark(it, us, leaf):
        """us は同じ別名の count の単位の全部(通りごとに group_by が変わるとき when 付きで複数ある)。"""
        if (it.get("item_name"), leaf) in seen:
            return
        # WHY: 例示値(「（14日）」など)はソースの表示を写すので対象外。単位を説明する remarks だけを見る
        # WHY: 日付そのものを数える count(counts_dates)は、GROUP BY が店・レジでも日数なので指摘しない(check_backend_facts (3) と同じ基準)
        # WHY(2026-10-16 run19 差3): 集計キーが条件で変わる(レジ別は店番号・レジ番、店舗別は店番号だけ)ときに先頭の通りの列だけを「単位」にすると、
        #      別の通りで成り立たない文(店舗別に「レジ番ごと」)を S1 に書かせた。通りごと(when 付き)の単位を全部集め、条件ごとに書き分けさせる。
        per = []  # (when, 単位の語)。同じ組は1回
        for u in us:
            others = [g for g in u.get("group_by") or [] if not _is_date_col(g)]
            if not others or u.get("counts_dates"):
                continue
            pair = (u.get("when") or "", "・".join(logical.get(_split_col(g)[1], _split_col(g)[1]) for g in others))
            if pair not in per:
                per.append(pair)
        if not per or not _day_unit_claim(it.get("remarks"), strings):
            return
        seen.add((it.get("item_name"), leaf))
        if len(per) == 1 and not per[0][0]:
            unit = per[0][1]
            phrase = f"{unit}の行数"
        else:
            # 条件のある単位が1つでも残るときは、条件ごとの文にする(条件の無い通りは「それ以外」)
            unit = "、".join(f"{w or 'それ以外'} のときは {x}" for w, x in per)
            phrase = "、".join(f"{w or 'それ以外'} のとき {x}の行数" for w, x in per)
        flagged[it.get("item_name")] = (unit, phrase)
        problems.append(f"[数え方] screen_layout.items[{it.get('item_name')}]: 「日数」「N日」と書くが、件数の単位は {unit} を含み日数ではない。「{phrase}」と書く"
                        f"{'(集計キーは条件で変わる。条件ごとに単位を書き分ける。1つの条件の列を全部の条件に当てはめない)' if len(per) > 1 or per[0][0] else ''} → 差し戻し先 S1")

    for a in data.get("api_spec") or []:
        draft = [u for d in ((apis or {}).get(a.get("action")) or {}).get("db_operations") or [] for u in _units_with_date_count(d)]
        counts = [u for u in draft if str(u.get("func")).lower() == "count" and (u.get("scope") or "main") == "main"]
        for m in a.get("response_to_screen_mapping") or []:
            it = items.get(m.get("screen_item_name"))
            leaf = _split_col(str(m.get("api_field_path") or "").replace("[]", ""))[1]
            us = [u for u in counts if u.get("alias") == leaf]
            if it is not None and us:
                remark(it, us, leaf)
        for u in counts:
            for it in items.values():
                if _overlap(it.get("src"), used.get(u.get("alias")) or []):
                    remark(it, [x for x in counts if x.get("alias") == u.get("alias")], u.get("alias"))
    for node in [data.get("front_processes"), data.get("event_processes")]:
        for s in _strings_in(node or []):
            if "日数" not in s or "行数" in s or "見出し" in s or "CSV" in s:
                continue
            for name, (unit, phrase) in flagged.items():
                if name and name in s:
                    problems.append(f"[ERROR] [数え方] 手順「{s}」が項目「{name}」の日数を書く。件数の単位は {unit} で、「{phrase}」と書く → 差し戻し先 S3")
    return problems


def check_enum_mentions(data, react_draft):
    """列挙型の引数(react 下書きの select の options)の選択肢を、API の説明が一部だけ書いているものを ERROR にする(B15 c)。
    WHY: 「店舗別のときは…」だけ書くと、残りの選択肢(レジ別)の扱いが読み手に伝わらない。全部並べるか、既定値を添える。
    対: 選択肢の表示名の括弧書きを除いた語(「レジ別（現行と同じ）」→「レジ別」)。一部だけ現れ、既定の語が無いときだけ ERROR。
    全選択肢を書いた引数の行(request_params)があれば通す(第19回。description は30字以内で全部は書けない。合成 _compose_enum_description が足す)。
    呼出元: main(react 下書きがあるとき)。"""
    problems = []
    for el in (react_draft or {}).get("jsx_elements") or []:
        opts = [re.sub(r"[（(].*$", "", str(o.get("text") or "")).strip() for o in el.get("options") or [] if isinstance(o, dict) and o.get("value") not in ("", None)]
        opts = [o for o in dict.fromkeys(opts) if len(o) >= 2]
        if len(opts) < 2:
            continue
        for a in data.get("api_spec") or []:
            d = str(a.get("description") or "")
            found = [o for o in opts if o in d]
            # WHY(2026-10-15 run16): API の description は30字以内(verify_partials.check_s2_overview)で、選択肢と既定値を全部は書けない。
            # 選択肢の全部を、その引数の行(request_params の name・description・validation・default)が書いていれば、description が
            # 片方だけを書いても読み手に残りの扱いは伝わる。S2 に description を30字に収めたまま全部並べさせると毎回差し戻されたので、
            # 全部を書いた引数の行があるときは通す(合成 _compose_enum_description がその引数の行へ全選択肢と既定値を足す)。
            vals = [str(o.get("value")) for o in el.get("options") or [] if isinstance(o, dict) and o.get("value") not in ("", None)]
            if any(all(t in pt for t in opts) or (vals and all(len(v) >= 3 and v in pt for v in vals))
                   for pt in (" ".join(str(p.get(k) or "") for k in ("name", "description", "validation", "default"))
                              for p in a.get("request_params") or [] if isinstance(p, dict))):
                continue
            if found and len(found) < len(opts) and not _DEFAULT_WORDS.search(d):
                # WHY(2026-10-15 run15): 末尾が「→ 全部並べるか既定値を添える(差し戻し先 S2)」で、[ERROR] の印も「→ 差し戻し先 X」の形も無く、
                # run_units.parse_sendbacks が拾えず after の差し戻しが「unknown: ERROR 0 件の INFO 行」になった。他の ERROR 行と同じ形にする
                problems.append(f"[ERROR] [列挙] api_spec[{a.get('action')}].description: 選択肢「{'・'.join(opts)}」(画面の{el.get('label')})のうち「{'・'.join(found)}」だけを書いている。全部並べるか既定値を添える → 差し戻し先 S2")
    return problems


def check_literal_transform(data, apis):
    """SQL の式にある日本語の文字列('(不明:' など)を、対の応答項目の transform_backend・description に字面どおり書いているかを見る(B15 d。ERROR)。
    WHY: SQL で作る表記は画面にそのまま出る文字なので、説明が言い換えると画面と食い違う。差し戻し文に字面を載せる。
    対: 応答項目の key の末尾が、下書き columns の alias_name と同じもの。呼出元: main(下書きがあるとき)。"""
    problems = []
    for act, a in (apis or {}).items():
        for d in a.get("db_operations") or []:
            for c in d.get("columns") or []:
                lits = [s for s in re.findall(r"'([^']*)'", str(c.get("expression") or "")) if _ja(s)]
                for api in data.get("api_spec") or []:
                    if not lits or api.get("action") != act:
                        continue
                    for r in api.get("response_items") or []:
                        if _split_col(r.get("key"))[1] != c.get("alias_name"):
                            continue
                        text = str(r.get("transform_backend") or "") + str(r.get("description") or "")
                        problems += [f"[文字列] api_spec[{act}].response_items[{r.get('key')}]: SQL の文字列「{s}」を transform_backend・description に字面どおり書いていない → 差し戻し先 S2" for s in lits if s not in text]
    return problems


def _walk_dicts(node, key, path=""):
    """JSON の中で key を持つリストの各 dict を (場所, dict) で順に返す。呼出元: check_forbidden_words。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, list):
                for i, e in enumerate(v):
                    if isinstance(e, dict):
                        yield f"{path}.{k}[{i}]", e
            else:
                yield from _walk_dicts(v, key, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_dicts(v, key, f"{path}[{i}]")


def check_forbidden_words(data):
    """「DB 例外」などの断定を、validations と API の説明・error_patterns で ERROR にする(B15 g)。
    WHY: DB の例外になるかは、ソースに暦の検査が無いので断定できない。「(推測)」を付けた文は通す(B14 c と同じ語)。
    呼出元: main(最終の JSON に対して)。"""
    srcs = []
    for key in ("validations", "validations_frontend"):
        for v in data.get(key) or []:
            if isinstance(v, dict):
                srcs += [(f"{key}[{v.get('no') or v.get('field')}].{k}", v.get(k)) for k in ("field", "rule", "applied_when")]
    for a in data.get("api_spec") or []:
        srcs.append((f"api_spec[{a.get('action')}].description", a.get("description")))
    # WHY: error_patterns は api_spec の下だけでなく backend_processes の処理の下にも入るので、JSON の中の全部を見る
    srcs += [(f"{path.lstrip('.')}.{k}", e.get(k)) for path, e in _walk_dicts(data, "error_patterns") for k in ("condition", "description")]
    problems = []
    for where, t in srcs:
        for s in re.split(r"[。\n]", str(t or "")):
            if any(w in s for w in _FORBIDDEN_WORDS) and not re.search(r"[(（]推測[)）]", s):
                problems.append(f"[禁止語] {where}: 「{s.strip()}」は DB の例外になると断定している(ソースに根拠が無い) → 差し戻し先 {'H4' if where.startswith('validations') else 'S2'}")
    return problems


# --- V7: 表示切替の画面ルール(VL)と CSV の実際の動き・CSV の整形の有無の照合(新5・新9。ERROR。差し戻し先は各行の末尾) ---
# WHY: 文面と処理の実際の動きが食い違う所だけを止める。言い換えは見ない。CSV の整形は下書きの formats_numbers(false のときだけ)で判定する
_CSV_FORMAT_WORDS = ("桁区切り", "整形", "書式")


def _state_changes(failure_effects):
    """イベントごとに、処理で値が変わる state の名前の集合(failure_effects の before_call・in_finally・on_success・on_failure)。
    記録の無いイベント(CSV 出力など)は含めない。呼出元: check_state_setters。"""
    out = {}
    for f in failure_effects or []:
        if not isinstance(f, dict) or not f.get("event"):
            continue
        names = {str(s).split("=")[0].strip() for s in list(f.get("before_call") or []) + list(f.get("in_finally") or [])}
        names |= {str(s).strip() for s in list(f.get("on_success") or []) + list(f.get("on_failure") or [])}
        out.setdefault(f["event"], set()).update(n for n in names if n)
    return out


def check_state_setters(validations, behaviors, failure_effects, csv_exports):
    """表示切替の画面ルール(VL)の文面に CSV の処理が出るのに、CSV 出力は画面の条件の state を変えない場合を ERROR にする(新5)。
    対: kind=表示切替 の VL で、src が表示切替の behavior と重なり、rule・applied_when に「CSV」があり、field に CSV が無いもの。
    state: behavior の cond の語のうち、取得の処理で変わるもの(_state_changes)。CSV の trigger_events のどれも、その state を変えないときだけ ERROR。
    WHY: 「読込中…」は取得の処理中だけ出る。CSV 出力は loading を立てないので、CSV の処理中に読込中と出すのは画面と食い違う(run11 の VL13)。
    記録の無いイベントは state を変えないとみなす。呼出元: main。behaviors・csv_exports が無ければ何も言わない。"""
    if not behaviors or not csv_exports:
        return []
    changes = _state_changes(failure_effects)
    known = set().union(*changes.values()) if changes else set()
    csv_events = [e for c in csv_exports if isinstance(c, dict) for e in c.get("trigger_events") or []]
    if not csv_events:
        return []
    problems = []
    for v in validations or []:
        if not isinstance(v, dict) or v.get("kind") != "表示切替" or not v.get("src") or "CSV" in str(v.get("field") or ""):
            continue
        if "CSV" not in " ".join(str(v.get(k) or "") for k in ("rule", "applied_when")):
            continue
        for b in behaviors:
            if not isinstance(b, dict) or b.get("kind") != "display_toggle" or not _overlap(v["src"], b.get("src") or []):
                continue
            states = set(re.findall(r"[A-Za-z_]\w*", str(b.get("cond") or ""))) & known
            if not states or any(states & changes.get(e, set()) for e in csv_events):
                continue
            who = sorted(e for e, s in changes.items() if states & s)
            problems.append(f"[ERROR] [状態] validations[{v.get('no') or v.get('field')}]: 文面に CSV の処理が出るが、CSV 出力は {'・'.join(sorted(states))} を変えない(変えるのは {'・'.join(who)})。文面から外す → 差し戻し先 H4")
    return problems


# 否定の語(「整形しない」「使わず」「CSVでは使わない」「画面でなくCSV」)。節がこの語を持てば、その節は整形すると言っていない
_NEGATION = re.compile(r"ない|なく|せず|わず|さず|れず|めず|ません|対象外")
# 節を切る所(読点)と、前後が逆の主張に変わる所(「…しないが、画面は…」「一方、…」)
_CLAUSE_SPLIT = re.compile(r"[、,，]")
_CONTRAST_SPLIT = re.compile(r"が[、,，]|一方[、,，]?|ものの[、,，]?|だが[、,，]?")


def csv_format_claimed(sentence) -> bool:
    """1つの文が「CSV の値を整形する」と肯定で言っているか。「CSV」の語と整形の語(桁区切り・整形・書式)が同じ文にあり、
    整形の語を持つ節が否定(_NEGATION)でないときだけ True。
    WHY(run17 の CL2): 「画面の表示だけに使い、CSVの値は整形しない」「画面でだけ使い、CSVでは使わない」は、CSV を整形しないと書いた文だが、
    以前の検査は「CSV」と「整形」が同じ文にあるだけで ERROR にした(否定を肯定と取る誤検出。run14 の CL6 も同じ型の疑い)。
    文を、逆の主張に変わる所(「…しないが、」「一方、」)で区切り、各区間を読点の節に分け、「CSV」が現れた後の節で整形の語を持つものが否定でなければ肯定とみなす
    (「表示・CSVで使う値を、3桁区切りに整形する」は CSV の節の後に整形の節が続く肯定。「CSVは整形しないが、画面は整形する」は区間が分かれるので肯定でない)。
    呼出元: check_cl_csv_format。"""
    for seg in _CONTRAST_SPLIT.split(sentence):
        seen_csv = False
        for clause in _CLAUSE_SPLIT.split(seg):
            if "CSV" in clause:
                seen_csv = True
            if seen_csv and any(w in clause for w in _CSV_FORMAT_WORDS) and not _NEGATION.search(clause):
                return True
    return False


def check_cl_csv_format(common_logic, csv_exports):
    """CSV が数値を整形しない(csv_exports の formats_numbers が全部 false)のに、共通ロジックの文が「CSV の値を整形する」と肯定で書くものを ERROR にする(新9)。
    文は「。」と改行で切り、肯定かどうかは csv_format_claimed が決める(否定文「CSVの値は整形しない」「CSVでは使わない」は通す。第20回)。
    formats_numbers が null・欄なしのときは読み飛ばす(判定できない)。
    WHY: 画面の整形と CSV の値を1文に混ぜると、CSV も整形すると読まれる(run11 の CL2)。呼出元: main。"""
    fmt = {c.get("formats_numbers") for c in csv_exports or [] if isinstance(c, dict)} - {None}
    if fmt != {False}:
        return []
    problems = []
    for c in common_logic or []:
        if not isinstance(c, dict):
            continue
        texts = [str(c.get(k) or "") for k in ("name", "description", "expression")] + [str(s.get("description") or "") for s in c.get("processing_steps") or [] if isinstance(s, dict)]
        sents = [x for t in texts for x in re.split(r"[。\n]", t)]
        if any(csv_format_claimed(x) for x in sents):
            problems.append(f"[ERROR] [CSV整形] common_logic[{c.get('no') or c.get('name')}]: CSV の値を整形すると書いているが、CSV 出力は数値を整形しない(formats_numbers=false)。画面だけの整形と書き分ける → 差し戻し先 S1")
    return problems


# --- W11: 例示値の行数の矛盾(新1)と、書式を落とした transform の写し(新3の保険)。どちらも ERROR。検査だけで、例示値・transform は書き換えない ---
# WHY: 「N行」の数だけを見る。「1行目」「2行以上」のような数は、表の行数と一致しなければ見ない
_ROW_COUNT = re.compile(r"(\d+)\s*行(?!目)")
_EXCLUDE_WORDS = re.compile(r"除く|除いた|除き")
_ROW_QUALIFIED = re.compile(r"(?:(?:表示|含め|含む|オンに)(?:する|した)?(?:と|とき|場合)?は?\s*\d+\s*行|\d+\s*行のうち)")


def check_row_count_example(items, display_rows):
    """画面項目の example・remarks が書く「N行」が、「空欄項目(dead)を除く」と言いながら除かない行数になっていないかを見る(新1。ERROR)。
    下書きの display_rows ごとに、全行数 T(specs の数)と dead を除いた行数 L を取る。文(「。」と改行で切る)ごとに「N行」を拾い、
    N が T のどれかに一致し L のどれにも一致しないとき、同じ文に「除く・除いた・除き」があれば ERROR(例 左段22行（空欄項目を除く）)。
    ただし同じ文に L の数(正しい行数)も書いてあれば通す(第19回。「22行(空欄項目を除くと14行)」は全行数と既定の行数を並べた正しい書き方)。
    WHY: S1 は表の行数に dead の行を含めたまま「空欄項目を除く」と書く(run12 の IT17)。dead は画面で隠れるので読み手は除いた数だと読む。
    呼出元: main(display_rows のある下書きがあるとき)。差し戻し先は S1(例示値は S1 が書く)。dead の欄が無い spec は dead でないとみなす。"""
    ts, ls = [], []
    for r in display_rows or []:
        specs = [s for s in r.get("specs") or [] if isinstance(s, dict)]
        if specs:
            ts.append(len(specs))
            ls.append(sum(1 for s in specs if not s.get("dead")))
    if not ts:
        return []
    problems = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        bad = []
        for k in ("example", "remarks"):
            for sent in re.split(r"[。\n]", str(it.get(k) or "")):
                if not _EXCLUDE_WORDS.search(sent):
                    continue
                # WHY(2026-10-16 run23): 「22行のうち、既定では空欄項目を除く行を出す(空欄項目も表示すると22行)」は、22行を全行数と断った書き方で、除いた数と言っていない。
                # 「空欄項目も表示すると N行」「N行のうち」の N は、全行数の但し書きとして数えない。
                sent = _ROW_QUALIFIED.sub("", sent)
                nums = list(dict.fromkeys(int(x) for x in _ROW_COUNT.findall(sent)))
                # WHY(2026-10-15 run16): 同じ文に dead を除いた正しい行数(L)も書いてあれば、全行数(T)は「オンのときの数」として並べたもので、
                # 食い違いではない(「22行(空欄項目を除くと14行)」「既定(空欄項目を除く)は14行で、オンにすると全項目の22行」)。
                # 以前はこの形も ERROR にし、S1 の手順書が「でよい」と書く形と食い違って毎回差し戻された。正しい行数の無い文だけを見る。
                if any(n in ls for n in nums):
                    continue
                bad += [n for n in nums if n in ts and n not in ls and n not in bad]
        # WHY: 1項目に1行(左段・右段の2つの数が同じ誤りでも指摘は1件。差し戻しの数を増やさない)
        if bad:
            problems.append(f"[ERROR] [例示値] screen_layout.items[{it.get('no') or it.get('item_name')}]({it.get('item_name')}): 例示値の {'・'.join(f'{n}行' for n in bad)} は空欄項目を含む行数(除くと {'・'.join(str(x) for x in dict.fromkeys(ls))}行) → 差し戻し先 S1")
    return problems


def check_transform_format(api_spec, items, display_rows):
    """response_to_screen_mapping の transform が、画面の書式(format の「#,##0」)を落として「そのまま…」で始まるものを ERROR にする(新3の保険)。
    対: api_field_path の葉が display_rows の spec の kin・cnt に一致し、写し先の画面項目(screen_item_name)の format に「#,##0」がある行。
    WHY: 画面は3桁区切りと単位で出すのに、transform が「そのまま表示」だと読み手は生の値が出ると読む(run12 の 55 件)。
    合成(_compose_mapping)がセルの書式の事実から transform を作るので、合成の後は 0 件になる。作れずに「そのまま」が残ったときに止める保険。
    呼出元: main(display_rows のある下書きがあるとき)。差し戻し先は S2(api_spec の transform は S2 が持つ)。"""
    leaves = {x for r in display_rows or [] for s in r.get("specs") or [] if isinstance(s, dict) for x in (s.get("kin"), s.get("cnt")) if x}
    by_name = {i.get("item_name"): i for i in items or [] if isinstance(i, dict)}
    problems = []
    for a in api_spec or []:
        for m in a.get("response_to_screen_mapping") or []:
            path = str(m.get("api_field_path") or "")
            leaf = path.replace("[]", "").rsplit(".", 1)[-1]
            it = by_name.get(m.get("screen_item_name"))
            fmt = str((it or {}).get("format") or "")
            if leaf in leaves and "#,##0" in fmt and str(m.get("transform") or "").startswith("そのまま"):
                problems.append(f"[ERROR] [写し] api_spec[{a.get('action')}].response_to_screen_mapping[{path}]: 画面は {fmt} で出すが transform が「{m.get('transform')}」 → 差し戻し先 S2")
    return problems


_LEVEL_RE = re.compile(r"\[(?:ERROR|WARN|INFO)\]")
_SENDBACK_TAIL_RE = re.compile(r"→ 差し戻し先 \S+")


def normalize_findings(lines):
    """指摘の行を共通の形にそろえる: [WARN]・[ERROR]・[INFO] の印で始まらない行は、先頭に「[ERROR] 」を付ける。
    WHY(2026-10-15 run15): この main は「[WARN] で始まらない行」を ERROR として終了コード 1 にするが、[ERROR] の印を持たない行
    ([別名]・[CTE]・[列挙] など)は run_units.parse_sendbacks が ERROR 行と見なせず、末尾に「→ 差し戻し先 X」も無いと拾われなかった。
    出力の入口(main)で印をそろえ、検査の側が印を付け忘れても差し戻しの読み取りから落ちないようにする。呼出元: main。"""
    return [ln if _LEVEL_RE.match(ln) else "[ERROR] " + ln for ln in lines]


def lines_without_sendback(lines):
    """[ERROR] の行のうち、「→ 差し戻し先 <単位>」を持たない行を返す(呼出元の差し戻し先の引き当てが欄名頼みになる行)。
    WHY: 検査を足すときに差し戻し先を書き忘れた行を、実走の出力の INFO 行の件数と selftest で見つけるため。呼出元: main・selftest。"""
    return [ln for ln in lines if ln.startswith("[ERROR]") and not _SENDBACK_TAIL_RE.search(ln)]


def form_info_line(lines):
    """出力の形の照合の INFO 行(差し戻し先の印を持たない ERROR 行の件数)。
    WHY: この行自体に「→ 差し戻し先 <語>」の形を書くと、run_units.parse_sendbacks が拾って「X」などの unknown を作る(実走で確認)ので、
    その形を避けて書く。selftest が parse_sendbacks で拾われないことを確かめる。呼出元: main。"""
    return f"[INFO] 出力の形の照合(ERROR 行の末尾の差し戻し先の印): 持たない行 {len(lines_without_sendback(lines))} 件(0 件でなければ検査の書式の不備)"


def selftest():
    # W11: 例示値の行数(T=22・L=14 の表)と書式を落とした transform
    w11_rows = [{"array": "LEFT", "specs": [{"label": f"r{i}", "kin": f"k{i}", "cnt": None, "dead": i >= 14} for i in range(22)]}]
    w11_it = lambda ex, rm="", fmt="": [{"no": "IT17", "item_name": "合計カードの会計表", "example": ex, "remarks": rm, "format": fmt}]
    got = check_row_count_example(w11_it("左段22行・右段17行（空欄項目を除く）"), w11_rows)
    assert len(got) == 1 and got[0].startswith("[ERROR] [例示値] screen_layout.items[IT17]") and "22行" in got[0] and "除くと 14行" in got[0] and "差し戻し先 S1" in got[0], got
    assert check_row_count_example(w11_it("左段22行・右段17行"), w11_rows) == [], "除く無しは通す"
    assert check_row_count_example(w11_it("1行目", "2行以上のとき表示する。1行目を除く"), w11_rows) == [], "T・L に一致しない数は見ない"
    assert check_row_count_example(w11_it("左段14行（空欄項目を除く）"), w11_rows) == [], "L に一致する数は通す"
    assert check_row_count_example(w11_it("左段22行（空欄項目を除く）"), []) == [] and check_row_count_example([], w11_rows) == []
    # 第19回: 同じ文に dead を除いた正しい行数(14行)があれば、全行数(22行)はオンのときの数として並べたもの。通す
    assert check_row_count_example(w11_it("22行(空欄項目を除くと14行)"), w11_rows) == [], "正しい行数を併記した文を ERROR にした"
    assert check_row_count_example(w11_it("14行(空欄項目を表示すると22行)", "既定(空欄項目を除く)は14行で、オンにすると全項目の22行になる"), w11_rows) == [], "既定の行数を書いた文を ERROR にした"
    assert len(check_row_count_example(w11_it("22行(空欄項目を除く)", "既定は14行"), w11_rows)) == 1, "正しい行数が別の文にあるだけの誤りを通した"
    # run23: 「22行のうち、既定では空欄項目を除く行を出す(空欄項目も表示すると22行)」は全行数の但し書き。通す。誤りの書き方は通さない
    assert check_row_count_example(w11_it("14行", "売上から合計までの22行のうち、既定では空欄項目を除く行を出す(空欄項目も表示すると22行)"), w11_rows) == [], "全行数の但し書きを ERROR にした"
    assert len(check_row_count_example(w11_it("14行", "表は22行で、空欄項目を除く"), w11_rows)) == 1, "但し書きの無い誤りを通した"
    w11_map = [{"action": "act", "response_to_screen_mapping": [{"api_field_path": "rows[].k0", "screen_item_name": "総売上", "transform": "そのまま表示"}]}]
    w11_items = [{"item_name": "総売上", "format": "#,##0 円"}]
    got = check_transform_format(w11_map, w11_items, w11_rows)
    assert len(got) == 1 and got[0].startswith("[ERROR] [写し]") and "k0" in got[0] and "差し戻し先 S2" in got[0], got
    ok_map = [{"action": "act", "response_to_screen_mapping": [{"api_field_path": "rows[].k0", "screen_item_name": "総売上", "transform": "3桁区切りの数値と「円」で出す"}]}]
    assert check_transform_format(ok_map, w11_items, w11_rows) == []
    assert check_transform_format(w11_map, [{"item_name": "総売上", "format": "固定文言"}], w11_rows) == [], "書式に #,##0 が無ければ通す"
    assert check_transform_format([{"action": "act", "response_to_screen_mapping": [{"api_field_path": "rows[].id", "screen_item_name": "総売上", "transform": "そのまま表示"}]}], w11_items, w11_rows) == [], "spec の葉でなければ通す"
    # 事実(別名・集計・CTE・副問合せ・例外の code)の照合
    col = lambda al, kind, real_c="", src=(), expr="x": {"alias": al, "alias_name": al, "kind": kind, "real_column": real_c, "source_columns": list(src),
                                                         "expression": expr, "is_expression": kind == "alias_only"}
    dop = {"id": "db_op_a", "columns": [col("shop_id", "column", "t.mise_no", ["t.mise_no"]), col("days", "alias_only", expr="count(*)"),
                                         col("first_date", "alias_only", "", ["t.kai_date"], "min(t.kai_date)"), col("amount", "alias_only", "", ["t.amount"], "SUM(t.amount)")],
           "ctes": [{"name": "d", "definition": "SELECT * FROM t WHERE x = 1", "from": "t", "filtered": True}, {"name": "e", "definition": "", "from": "", "filtered": False}],
           "subqueries": [{"location": "WHERE", "definition": "SELECT max(kai_date) FROM t", "filtered": False}],
           "aggregate_units": [{"func": "count", "arg": "*", "alias": "days", "scope": "main", "group_by": ["t.mise_no"], "unit": "t.mise_no"},
                               {"func": "min", "arg": "t.kai_date", "alias": "first_date", "scope": "main", "group_by": ["t.mise_no"], "unit": "t.mise_no"},
                               {"func": "sum", "arg": "t.n", "alias": "tot", "scope": "cte d", "group_by": ["t.kai_date"], "unit": "t.kai_date"}]}
    bapis = {"act": {"db_operations": [dop], "error_patterns": [{"id": "MSE00001", "status_code": 400}]}}
    ces = [{"exception": "VE", "errors": [{"code_default": "validation_error", "status_code": "getattr(e, 'status', 400) or 400"}]},
           {"exception": "Exception", "errors": [{"code_default": "internal_error", "status_code": 500}]}]
    good = {"logical_names": [{"physical_column": "mise_no"}, {"physical_column": "amount"}],
            "db_operations": [{"id": "DB1", "api_endpoint": {"action": "act"}, "dataset": [{"no": 1, "physical_column": "kai_date"}],
                               "aggregate": [{"function": "min", "target": "t.kai_date", "alias": "first_date", "scope": "main"}], "remarks": "期間で絞る"}],
            "api_spec": [{"action": "act", "description": "d", "response_items": [{"key": "rows[].first_date", "expression": "min(kai_date)", "description": "最初"},
                                                                                   {"key": "rows[].days", "expression": "count(*)", "description": "店舗の行数"}],
                          "error_patterns": [{"status_code": 400, "error_code": "validation_error", "condition": "不正"}]}],
            "messages_backend": [{"message_id": "MSE00001", "code": "validation_error"}]}
    assert check_backend_facts(good, bapis, ces) == [], check_backend_facts(good, bapis, ces)
    assert check_backend_facts({}, None, None) == []
    # 別名を物理列にした(days は式の別名で実列に無い。amount は実列でもあるので許す)→ ERROR
    bad = json.loads(json.dumps(good))
    bad["logical_names"].append({"physical_column": "days"})
    bad["db_operations"][0]["dataset"].append({"no": 2, "physical_column": "first_date"})
    got = check_backend_facts(bad, bapis, ces)
    assert [g.split(" ")[0] for g in got] == ["[別名]"] * 2 and "days" in got[0] and "first_date" in got[1] and all("差し戻し先 H2" in g for g in got), got
    # R14: 別名の行の決まった書き方(physical_column 空・alias_name つき)は通す。別名を物理列に書いた形も、合成の機械の直し(restore_alias_rows)を通せば通る
    #      (verify_partials の [列の省略] は同じ書き方を alias_name で数える。2本が逆向きに止めない)
    ok = json.loads(json.dumps(good))
    ok["logical_names"].append({"physical_table": "t", "physical_column": "", "alias_name": "days", "logical_column": "日数"})
    ok["db_operations"][0]["dataset"].append({"no": 2, "physical_table": "t", "physical_column": "", "alias_name": "first_date", "logical_column": "最初の日"})
    assert check_backend_facts(ok, bapis, ces) == [], check_backend_facts(ok, bapis, ces)
    import _compose_db_facts
    fixed = json.loads(json.dumps(bad))
    assert _compose_db_facts.restore_alias_rows(fixed, [{"apis": bapis}]) == 2 and check_backend_facts(fixed, bapis, ces) == [], check_backend_facts(fixed, bapis, ces)
    # 集計の別名が null / 副問合せの集計を本体として書いた / CTE の集計を本体として書いた
    bad = json.loads(json.dumps(good))
    bad["db_operations"][0]["aggregate"] = [{"function": "min", "target": "kai_date", "alias": None, "scope": "main"}, {"function": "max", "target": "kai_date", "alias": None, "scope": "main"},
                                            {"function": "sum", "target": "t.n", "alias": "tot", "scope": "main"}]
    got = check_backend_facts(bad, bapis, ces)
    assert [g.split(" ")[0] for g in got] == ["[別名]", "[副問合せ]", "[CTE]"] and "first_date" in got[0], got
    # 式の食い違い(FILTER の中の定数名は見ない)
    bad = json.loads(json.dumps(good))
    bad["api_spec"][0]["response_items"][0]["expression"] = "max(kai_date)"
    got = check_backend_facts(bad, bapis, ces)
    assert len(got) == 1 and got[0].startswith("[式]") and "差し戻し先 S2" in got[0], got
    # CTE: 定義が空の e を参照 / 絞り込みのある d を条件なしと書く / ソースに CTE が無い API
    bad = json.loads(json.dumps(good))
    bad["db_operations"][0]["remarks"] = "CTE e を使う。CTE d は条件なしで全件を取る"
    got = check_backend_facts(bad, bapis, ces)
    assert [g.split(" ")[0] for g in got] == ["[CTE]"] * 2 and "e" in got[0] and "d" in got[1], got
    assert check_backend_facts(bad, {"act": {"db_operations": [{"columns": [], "ctes": []}]}}, ces)[0].startswith("[CTE]")
    # count の単位に日付が無いのに「日数」→ WARN
    bad = json.loads(json.dumps(good))
    bad["api_spec"][0]["response_items"][1]["description"] = "期間内の会計日数"
    got = check_backend_facts(bad, bapis, ces)
    assert len(got) == 1 and got[0].startswith("[WARN] [集計単位]"), got
    # 数える単位に日付以外(店舗)も含むのに「日数」→ WARN。日付だけの単位なら WARN にしない
    bapis2 = json.loads(json.dumps(bapis))
    bapis2["act"]["db_operations"][0]["aggregate_units"][0]["group_by"] = ["t.mise_no", "t.kai_date"]
    got = check_backend_facts(bad, bapis2, ces)
    assert len(got) == 1 and got[0].startswith("[WARN] [集計単位]") and "t.mise_no" in got[0], got
    bapis2["act"]["db_operations"][0]["aggregate_units"][0]["group_by"] = ["t.kai_date"]
    assert check_backend_facts(bad, bapis2, ces) == [], check_backend_facts(bad, bapis2, ces)
    # 日付そのものを数える count は、GROUP BY が店・レジでも日数(run12 の warn_days: count(*) FILTER (WHERE 条件) と、同じ条件で日付を並べる array_agg(CASE WHEN 条件 THEN 日付 END))。日付を並べる欄が無い・条件が違うものは従来どおり WARN
    wc = "p.seisan_cnt = 0 AND p.nz > p.z"
    wdays = col("warn_days", "alias_only", expr=f"count(*) FILTER (WHERE {wc})::int")
    wdates = lambda cond: col("warn_dates", "alias_only", expr=f"array_remove(array_agg( CASE WHEN {cond} THEN p.kai_date END ORDER BY p.kai_date), NULL)")
    wunit = lambda alias, arg="*": {"func": "count", "arg": arg, "alias": alias, "scope": "main", "group_by": ["pos.mise_no", "pos.reji_no", "shops.name"], "unit": "pos.mise_no, pos.reji_no, shops.name"}
    wdata = lambda text: {"api_spec": [{"action": "act", "response_items": [{"key": "rows[].warn_days", "description": text}]}],
                          "screen_layout": {"items": [{"item_name": "警告", "remarks": text, "example": ""}]}}
    wmap = {"act": {"db_operations": [{"columns": [wdays, wdates(wc)], "aggregate_units": [wunit("warn_days")]}]}}
    wdata_all = lambda text: dict(wdata(text), api_spec=[{"action": "act", "response_items": [{"key": "rows[].warn_days", "description": text}], "response_to_screen_mapping": [{"api_field_path": "rows[].warn_days", "screen_item_name": "警告"}]}])
    assert check_backend_facts(wdata("取込欠損の疑いがある日数"), wmap, ces) == [], check_backend_facts(wdata("取込欠損の疑いがある日数"), wmap, ces)
    assert check_item_remarks_units(wdata_all("取込欠損の疑いがある日数"), wmap) == [], check_item_remarks_units(wdata_all("取込欠損の疑いがある日数"), wmap)
    # 負例: 日付を並べる欄の条件が違う / 日付を並べる欄が無い → 日数と言えないので WARN・ERROR のまま
    for cols_bad in ([wdays, wdates("p.seisan_cnt = 1")], [wdays]):
        wbad = {"act": {"db_operations": [{"columns": cols_bad, "aggregate_units": [wunit("warn_days")]}]}}
        got = check_backend_facts(wdata("取込欠損の疑いがある日数"), wbad, ces)
        assert len(got) == 1 and got[0].startswith("[WARN] [集計単位]"), got
        got = check_item_remarks_units(wdata_all("取込欠損の疑いがある日数"), wbad)
        assert len(got) == 1 and got[0].startswith("[数え方]"), got
    # count(DISTINCT 日付の列)は日数。count(DISTINCT 日付以外)は従来どおり
    wdist = lambda arg: {"act": {"db_operations": [{"columns": [col("warn_days", "alias_only", expr=f"count({arg})")], "aggregate_units": [wunit("warn_days", arg)]}]}}
    assert check_backend_facts(wdata("会計日数"), wdist("DISTINCT p.kai_date"), ces) == []
    assert len(check_backend_facts(wdata("会計日数"), wdist("DISTINCT p.mise_no"), ces)) == 1
    # エラー code: error_code は common_errors の code_default と照合する。condition の code= は見ない(廃止)。messages の code は照合しない(MSE 番号が正)
    bad = json.loads(json.dumps(good))
    bad["messages_backend"][0]["code"] = "MSE00001"
    bad["messages"] = [{"message_id": "MSE00001", "code": "MSE00001"}, {"message_id": "MSE00001", "message": "code 欄なし"}]
    bad["api_spec"][0]["error_patterns"] = [{"status_code": 400, "error_code": "MSE00001", "condition": "不正 (code=validation_error)"}, {"status_code": 400, "condition": "不正 (code=MSE00001)"}]
    got = check_backend_facts(bad, bapis, ces)
    # error_code が common_errors に無い行 / error_code が空で status 400 が validation_error に当たる行(抜け)の2件
    assert [g.split(" ")[0] for g in got] == ["[エラーcode]"] * 2 and "MSE00001" in got[0] and "差し戻し先 S2" in got[0] and "validation_error" in got[1], got
    assert _codes_for_status(ces, 400) == ["validation_error"] and _codes_for_status(ces, 500) == ["internal_error"] and _codes_for_status(ces, 404) == []
    # messages の code が validation_error でも MSE 番号でも、この検査は見ない(形は verify_intermediate.py の検査)
    bad["messages_backend"][0]["code"] = "validation_error"
    bad["api_spec"][0]["error_patterns"] = [{"condition": "不正 (code=validation_error)"}]
    assert check_backend_facts(bad, bapis, ces) == [], check_backend_facts(bad, bapis, ces)
    # F2: 境界値の語が rule に無い・共通エラーの status が api_spec に無いと WARN
    bs = [{"param": "shop_id", "check": "v <= 0", "compare": ["v <= 0"]},
          {"param": "from", "check": "x", "compare": [], "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "impossible_dates_accepted": ["2026-02-30"]},
          {"param": "from,to", "present": False, "check": "order", "fact": "大小を比べる式が無い"},
          {"param": "group", "check": "group not in _G", "compare": ["group not in _G"]}]
    fa = {"act": {"boundaries": bs, "error_patterns": [{"status_code": 400}]}}
    ce = [{"exception": "psycopg2.Error", "errors": [{"status_code": 500}]}, {"exception": "ValidationError", "errors": [{"status_code": "getattr(e) or 400"}]}]
    vl = [{"field": "店舗", "rule": "正の整数", "applied_when": ""}, {"field": "日付", "rule": "YYYY-MM-DD。暦に無い日付は DB 例外になる。開始と終了の大小は検査しない"}]
    ap = [{"action": "act", "error_patterns": [{"status_code": 400}, {"status_code": 500}]}]
    assert check_boundaries(vl, ap, fa, ce) == [], check_boundaries(vl, ap, fa, ce)
    got = check_boundaries([{"field": "店舗", "rule": "必須"}], [{"action": "act", "error_patterns": [{"status_code": 400}, {"status_code": 503}]}], fa, ce)
    assert [g.split(" ")[1] for g in got] == ["[境界値]"] * 3 + ["[DB例外]"] * 2 and all(g.startswith("[WARN]") for g in got), got
    assert not check_boundaries([], [{"action": "act"}], {}, []) and not check_boundaries([], [], None, None)
    # B3: reji_no は string | null。nullable を反転すると [null可] ERROR、型の分類違いは WARN
    apis = {"act": {"response_fields": [{"path": "rows[].reji_no", "ts_type": "string | null", "ts_base": "string", "nullable": True},
                                         {"path": "rows[].days", "ts_type": "number", "ts_base": "number", "nullable": False},
                                         {"path": "dead_cols", "ts_type": "string[]", "ts_base": "string[]", "nullable": False}]}}
    spec = lambda *rs: [{"action": "act", "response_items": [dict(zip(("key", "data_type", "nullable"), r)) for r in rs]}]
    assert check_types(spec(("rows[].reji_no", "string", True), ("rows[].days", "integer", False), ("dead_cols", "string[]", False)), apis) == []
    assert check_types([{"action": "act", "response_items": [{"key": "rows[].reji_no", "data_type": "string | null"}]}], apis) == [], "nullable 無しは data_type の | null で見る"
    got = check_types(spec(("rows[].reji_no", "string", False), ("rows[].days", "string", False), ("x.unknown", "string", True)), apis)
    assert [g.split(" ")[0] for g in got] == ["[null可]", "[WARN]"] and "rows[].reji_no" in got[0], got
    # B3 SQL の型: index signature の列は db_type(bigint → number)と db_nullable で見る。api.ts の共用体型は見ない
    ix = {"act": {"response_fields": [{"path": "rows[].s", "ts_type": "number | string | null", "ts_base": "number | string", "nullable": True,
                                       "index_signature": True, "sql_hint": "SUM(x)::bigint", "db_type": "bigint", "db_nullable": False}]}}
    assert check_types(spec(("rows[].s", "integer", False)), ix) == []
    got = check_types(spec(("rows[].s", "string", True)), ix)
    assert [g.split(" ")[0] for g in got] == ["[WARN]", "[null可]"] and "SQL" in got[0], got
    # F3: 初期表示の取得順。parallel は「並行」、serial は「待って」の記述が要る。語が無い・食い違うと WARN
    fr = lambda *d: [{"process_id": "FR1", "name": "初期表示", "steps": [{"description": x} for x in d]}]
    par, ser = {"mode": "parallel", "basis": "別々の useEffect"}, {"mode": "serial", "basis": "then"}
    assert check_fetch_plan(par, fr("店舗の一覧と会計レポートの取得は並行して進める")) == []
    assert check_fetch_plan(ser, fr("店舗の一覧の応答を待って会計レポートを取得する")) == []
    got = check_fetch_plan(par, fr("店舗の一覧を取得する", "会計レポートを取得する"))
    assert len(got) == 1 and got[0].startswith("[WARN] [取得順]") and "差し戻し先" not in got[0] and "スクリプトの不具合" in got[0], got
    assert check_fetch_plan(ser, fr("取得は並行して進める")) and not check_fetch_plan({"mode": "single"}, fr("x")) and not check_fetch_plan(None, fr("x"))
    # U12: FR の手順に画面規則(押せ・活性など)を書いたら ERROR。表示の文は通す
    fr_rule = lambda *d: [{"process_id": "FR8", "name": "CSV出力", "steps": [{"description": x} for x in d]}]
    assert check_front_rules({"front_processes": fr_rule("CSV出力ボタンを表示する")}) == []
    got = check_front_rules({"front_processes": fr_rule("集計結果が0件のときは CSV 出力ボタンを押せないようにする", "出力ボタンは活性にする")})
    assert len(got) == 2 and all(g.startswith("[ERROR] [画面規則]") and "差し戻し先 S3" in g for g in got), got
    assert check_front_rules({}) == [] and check_front_rules({"front_processes": [{"process_id": "FR1", "steps": []}]}) == []
    sp = lambda a, b=None: [{"file": "P.tsx", "line_start": a, "line_end": b or a}]
    beh = [{"id": "BH01", "kind": "data_filter", "cond": "showDead || !s.dead", "target": "", "src": sp(124), "control_src": sp(276)},
           {"id": "BH02", "kind": "display_toggle", "cond": "rows.length > 1", "target": "合計", "src": sp(305), "control_src": []}]
    fp = [{"process_id": "FR7", "src": sp(276), "steps": [{"step_no": 1, "kind": "validation"}, {"step_no": 2, "kind": "state_update"}]}]
    got = check_kinds([], fp, beh)
    assert len(got) == 1 and "FR7" in got[0] and not got[0].startswith("[WARN]"), got
    fp[0]["steps"][0]["kind"] = "filter"
    assert check_kinds([], fp, beh) == []
    vs = [{"no": "VL1", "field": "合計カード", "rule": "合計の表示", "kind": "入力検査"}, {"no": "VL2", "field": "合計", "kind": "表示切替"}]
    got = check_kinds(vs, [], beh)
    assert len(got) == 1 and "VL1" in got[0] and "入力検査" in got[0], got
    assert check_kinds([{"no": "VL3", "field": "合計", "kind": "絞り込み"}], [], beh) and not check_kinds(vs, [], [])
    # kind=API検査(バック側の入力検査。H4 手順書)は、画面に input_check が無くても [種別] にしない(画面の検査だけを見る照合の対象外)
    assert check_kinds([{"no": "VL9", "field": "集計開始日", "rule": "YYYY-MM-DD の形式", "kind": "API検査"}], [], beh) == []
    jsx = [{"tag": "select", "label": "店舗", "item_type_hint": "ComboBox"}, {"tag": "input", "label": "開始", "item_type_hint": "Calendar"}]
    assert check_items([{"item_name": "店舗", "item_type": "ComboBox"}, {"item_name": "開始", "item_type": "Calendar"}], jsx) == []
    got = check_items([{"item_name": "店舗", "item_type": "ComboBox"}, {"item_name": "開始", "item_type": "Input"}, {"item_name": "幻", "item_type": "Button"}], jsx)
    assert [g.split(" ")[1] for g in got] == ["[種別]", "[画面項目]"], got
    assert [g for g in check_items([{"item_name": "店舗", "item_type": "ComboBox"}], jsx) if "欠け" in g]
    # B2 残り: 表示専用要素(label 無し)は (1)〜(3) の対象外。display_rows の欠けは ERROR(接頭辞 [WARN] 無し)
    disp = [{"tag": "span", "label": "", "text": "読込中…", "item_type_hint": "Label"}]
    assert check_items([], disp) == []
    got = check_items([{"item_name": "総売上"}], None, [{"labels": ["総売上", "返品"]}])
    assert len(got) == 1 and "返品" in got[0] and not got[0].startswith("[WARN]"), got
    cands = [{"id": "CC01", "label": "差", "expr": "r.s3 - r.n", "operands": ["s3", "n"], "funcs": [], "literals": [],
              "operand_labels": {"s3": ["小計", "小計3(税込)"], "n": ["点数"]}},
             {"id": "CC02", "label": "平均", "expr": "r.n ? Math.floor(r.s3 / r.n) : 0", "operands": ["n", "s3"],
              "funcs": ["Math.floor"], "literals": ["0"], "operand_labels": {"s3": ["小計3(税込)"], "n": ["点数"]}}]
    ok = [{"name": "差", "formula": "差 = 小計（税込） − 点数", "inputs": []},
          {"name": "平均", "formula": "平均 = FLOOR(小計（税込） ÷ 点数)", "inputs": []}]
    assert check_calcs(ok, cands) == [], check_calcs(ok, cands)
    # 四捨五入に変える・点数を落とす・対を消すと WARN
    bad = [{"name": "差", "formula": "差 = 小計（税込）", "inputs": []},
           {"name": "平均", "formula": "平均 = 四捨五入(小計（税込） ÷ 点数)", "inputs": []}]
    got = check_calcs(bad, cands)
    assert any("点数" in g and "[式]" in g for g in got) and any("四捨五入" in g for g in got) and any("切り捨て" in g for g in got), got
    assert [g for g in check_calcs(ok[:1], cands) if "[式の漏れ]" in g] and not check_calcs(ok[:1], cands[:1])
    with tempfile.TemporaryDirectory() as d:
        py, ts = Path(d, "q.py"), Path(d, "P.tsx")
        py.write_text('def f(k):\n    raise ValueError("from は YYYY-MM-DD 形式で指定してください")\n    raise ValueError(f"{k} は正の整数で指定してください")\n', encoding="utf-8")
        ts.write_text('const a = <option value="">- 全店 -</option>;\nconst b = "読込中…";\n', encoding="utf-8")
        s = collect_source_strings([py, ts])
        assert "from は YYYY-MM-DD 形式で指定してください" in s and "- 全店 -" in s and "は正の整数で指定してください" in s, s
        ok = {"messages": [{"message_id": "M1", "message": "from は YYYY-MM-DD 形式で指定してください"},
                           {"message_id": "M2", "message": "{key} は正の整数で指定してください。"}]}
        assert check_messages(ok, s) == [], check_messages(ok, s)
        # 型文({key})を呼出の実引数で展開した文言は、ソースの f-string の型文に当てはまれば実在。当てはまらない文言は WARN
        ex = {"messages_backend": [{"message_id": "M3", "message": "shop_id は正の整数で指定してください"},
                                   {"message_id": "M4", "message": "shop_id は日付で指定してください"}]}
        got = check_messages(ex, s)
        assert len(got) == 1 and "M4" in got[0], got
        # 受け入れ条件: 「from」を「集計開始日」に書き換えた文言は WARN
        bad = {"messages": [{"message_id": "M1", "message": "集計開始日 は YYYY-MM-DD 形式で指定してください"}]}
        assert len(check_messages(bad, s)) == 1 and check_messages(bad, s)[0].startswith("[WARN] [文言]")
        det = {"event_processes": [{"event_code": "EV1", "details": ["画面に「- 全店 -」と出す", "「作り話」と出す"]}]}
        assert len(check_messages(det, s)) == 1
        assert check_examples([{"item_name": "店舗", "example": "- 全店 -"}, {"item_name": "日付", "example": "2026-10-01"}], s) == [], "日付の書式の実値は通す"
        assert check_examples([{"item_name": "店名", "example": "作り話の店 3 件"}], s) == [
            "[WARN] [例示値] screen_layout.items[店名]: example「作り話の店 3 件」がソースの文字列に無い → 差し戻し先 S1"]
    # S1 の例示値: 数字の並び(日付・金額)は実行時の値として穴扱い。字面の語(店名など)がソースに無ければ WARN
    assert check_examples([{"item_name": "日付", "example": "2026-10-14"}], s) == []
    assert check_examples([{"item_name": "店舗", "example": "- 全店 - 2 件"}], s) == []
    got = check_examples([{"item_name": "店舗", "example": "作り話の店 2 件"}], s)
    assert len(got) == 1 and got[0].startswith("[WARN] [例示値]"), got
    # S2 の式: 日本語の説明は字面で比べない(SQL の関数の形の式は従来どおり比べる)
    prose = {"api_spec": [{"action": "act", "response_items": [{"key": "rows[].first_date", "expression": "期間内の最小の会計日"}]}]}
    assert check_backend_facts(prose, bapis, ces) == [], check_backend_facts(prose, bapis, ces)
    # H4 の式: 列の名前(合計2(支払科目計))の数字は定数ではない。式の中の定数 3 は WARN
    cc = [{"id": "CC09", "label": "支払科目に無い額", "expr": "r.s - r.t", "operands": ["s", "t"], "funcs": [], "literals": [],
           "operand_labels": {"s": ["小計（税込売上金額）"], "t": ["合計2(支払科目計)"]}}]
    dd = [{"name": "支払科目に無い額", "formula": "支払科目に無い額 = 小計（税込売上金額） − 合計2(支払科目計)",
           "inputs": [{"name": "小計（税込売上金額）"}, {"name": "合計2(支払科目計)"}]}]
    assert check_calcs(dd, cc) == [], check_calcs(dd, cc)
    cc2 = [{"id": "CC09", "label": "支払科目に無い額", "expr": "r.s * 3", "operands": ["s"], "funcs": [], "literals": [],
            "operand_labels": {"s": ["小計（税込売上金額）"]}}]
    got = check_calcs([{"name": "支払科目に無い額", "formula": "支払科目に無い額 = 小計（税込売上金額） × 3", "inputs": [{"name": "小計（税込売上金額）"}]}], cc2)
    assert len(got) == 1 and "式の数字 3" in got[0], got
    # VL5: 同じ行範囲の対(124 の data_filter)を先に使う。src が無いときは字面で対を探す(従来どおり表示用の行に当たる)
    bh = [{"id": "BH01", "kind": "display_toggle", "cond": "s.dead", "target": "空", "src": sp(132), "control_src": []},
          {"id": "BH10", "kind": "data_filter", "cond": "showDead || !s.dead", "target": "", "src": sp(124), "control_src": sp(276)}]
    vl = {"no": "VL9", "field": "空欄項目も表示", "rule": "表示の絞り込み: 空欄項目(s.dead)を表示対象から外す", "kind": "絞り込み"}
    assert check_kinds([dict(vl, src=sp(123, 124) + sp(276))], [], bh) == []
    assert len(check_kinds([vl], [], bh)) == 1
    # S1 の例示: 区切り(" / ")ごとに照合する。インライン要素(strong)と JSX の式({…})で切れた文も、タグを外せば照合できる
    assert _example_in_source("3 点 / 4,500 円", s)
    with tempfile.TemporaryDirectory() as d:
        tsx = Path(d, "N.tsx")
        tsx.write_text("<p>\n  <strong>取込欠損の疑い</strong>：この期間に、上書きされた疑いのある日があります。\n</p>\n"
                       "<p>\n  POSがあるのは\n  {shops.length}店だけなので\n</p>\n", encoding="utf-8")
        ss = collect_source_strings([tsx])
        assert _example_in_source("取込欠損の疑い：この期間に、上書きされた疑いのある日があります。", ss), ss
        assert _example_in_source("POSがあるのは10店だけなので", ss), ss
        assert not _example_in_source("作り話の文は10店だけなので", ss)
    # run15 差7: + で連結した文字列は、つないだ全文でも照合する(api.ts の「(x ?? "A") + " — B"」と、間に変数を挟む連結)
    with tempfile.TemporaryDirectory() as d:
        cts, cpy = Path(d, "api.ts"), Path(d, "m.py")
        # fallback-ok: 下の ?? は検査の入力にするソース(TSX)の字面で、このスクリプトの既定値への切り替えではない
        cts.write_text('throw new Error((body?.error?.message ?? "ログインの有効期限が切れています") + " — 画面を再読込してください");\n'
                       'throw new Error("取得に失敗しました（HTTP " + res.status + "）。もう一度操作してください");\n'
                       'const t = `${a}件` + "の" + "合計";\n'
                       'const x = "別の文" , y = "離れた文";\n', encoding="utf-8")
        cpy.write_text('raise ValueError("開始日は" + "必須です")\n', encoding="utf-8")
        cs = collect_source_strings([cts, cpy])
        cat = {"messages": [{"message_id": "MSE00006", "message": "ログインの有効期限が切れています — 画面を再読込してください"},
                            {"message_id": "MSE00005", "message": "取得に失敗しました（HTTP {status}）。もう一度操作してください"},
                            {"message_id": "MSE00009", "message": "開始日は必須です"}]}
        assert check_messages(cat, cs) == [], check_messages(cat, cs)
        # 連結でない並び(カンマ・離れた文)は全文にしない。連結後に無い文言は従来どおり WARN
        cat_bad = {"messages": [{"message_id": "M1", "message": "別の文離れた文"},
                                {"message_id": "M2", "message": "ログインの有効期限が切れています — 作り話を足した"}]}
        assert len(check_messages(cat_bad, cs)) == 2, check_messages(cat_bad, cs)
    # B15 a: DB 名(物理列の空・句の物理名・結合の別名・論理表の(推定)・「と」の論理名)
    apis_db = {"act": {"db_operations": [{"columns": [{"alias_name": "first_date", "source_columns": ["pos.kai_date"]},
                                                     {"alias_name": "shop_name", "source_columns": ["shops.name", "pos.mise_no"]}]}]}}
    names_ok = [{"physical_table": "pos", "logical_table": "POS会計(推定)", "physical_column": "mise_no", "logical_column": "店舗ID(推定)", "alias_name": "shop_id"}]
    op_ok = {"id": "DB1", "dataset": [{"logical_table": "POS会計(推定)", "logical_column": "初日(推定)", "physical_table": "pos", "physical_column": "kai_date", "alias_name": "first_date"}],
             "where": [{"logical_table": "POS会計(推定)", "logical_column": "店舗ID(推定)"}], "group_by": [], "order_by": [],
             "joins": [{"logical_table": "店舗マスタ(推定)", "condition": "店舗マスタ.店舗ID(推定) = POS会計(推定).店舗ID(推定)"}]}
    assert check_db_names({"logical_names": names_ok, "db_operations": [op_ok]}, apis_db) == [], check_db_names({"logical_names": names_ok, "db_operations": [op_ok]}, apis_db)
    op_bad = {"id": "DB1", "dataset": [{"logical_table": "POS会計(推定)", "logical_column": "初日と最終日(推定)", "physical_table": "pos", "physical_column": "", "alias_name": "first_date"}],
              "where": [{"logical_table": "POS会計(推定)", "logical_column": "mise_no"}], "group_by": [], "order_by": [],
              "joins": [{"logical_table": "shops", "condition": "s.id = p.店舗ID(推定)"}]}
    names_bad = names_ok + [{"physical_table": "shops", "logical_table": "店舗マスタ", "physical_column": "", "logical_column": "店舗名称(推定)", "alias_name": "shop_name"}]
    got = check_db_names({"logical_names": names_bad, "db_operations": [op_bad]}, apis_db)
    tags = [g.split(" ")[0] for g in got]
    assert tags.count("[物理列]") == 3 and tags.count("[結合]") == 1 and tags.count("[論理表]") == 2 and tags.count("[論理名]") == 1, got
    assert any("shops.name" in g and "差し戻し先 H2" in g for g in got) and any("pos.kai_date" in g for g in got), got
    assert _AND_WORD.search("店舗と日付") and not _AND_WORD.search("店舗ごとに"), "「と」は名詞を括る語だけ拾う"
    # B15 b: 件数の単位に日付以外を含むのに「日数」と書いた項目は ERROR。日付だけの単位は日数でよい
    apis_r = {"act": {"db_operations": [{"aggregate_units": [{"func": "count", "arg": "*", "alias": "days", "scope": "main", "group_by": ["pos.mise_no", "pos.reji_no"]}]}]}}
    dr = {"screen_layout": {"items": [{"item_name": "件数", "remarks": "店舗ごとの集計日数を表示する", "example": ""}, {"item_name": "店舗表示", "remarks": "日数を表示", "example": ""}]},
          "api_spec": [{"action": "act", "response_to_screen_mapping": [{"api_field_path": "rows[].days", "screen_item_name": "件数"}, {"api_field_path": "rows[].mise", "screen_item_name": "店舗表示"}]}],
          "logical_names": [{"physical_column": "mise_no", "logical_column": "店舗ID(推定)"}, {"physical_column": "reji_no", "logical_column": "レジ番号(推定)"}]}
    got = check_item_remarks_units(dr, apis_r)
    assert len(got) == 1 and got[0].startswith("[数え方]") and "店舗ID・レジ番号の行数" in got[0] and "差し戻し先 S1" in got[0], got
    apis_r["act"]["db_operations"][0]["aggregate_units"][0]["group_by"] = ["pos.kai_date"]
    assert check_item_remarks_units(dr, apis_r) == [], "日付だけの単位は日数でよい"
    # run19 差3: 集計キーが条件で変わる(レジ別は店・レジ、店舗別は店だけ)ときは、条件ごとに単位を書き分けさせる(レジ番を全部の条件に当てはめない)
    apis_r["act"]["db_operations"][0]["aggregate_units"] = [
        {"func": "count", "arg": "*", "alias": "days", "scope": "main", "group_by": ["pos.mise_no", "pos.reji_no"], "when": "group = register"},
        {"func": "count", "arg": "*", "alias": "days", "scope": "main", "group_by": ["pos.mise_no"], "when": "group = shop"}]
    got = check_item_remarks_units(dr, apis_r)
    assert len(got) == 1 and "group = register のとき 店舗ID・レジ番号の行数" in got[0] and "group = shop のとき 店舗IDの行数" in got[0] \
        and "条件ごとに単位を書き分ける" in got[0] and got[0].endswith("差し戻し先 S1"), got
    # B15 c: 選択肢の一部だけを書いた説明(既定の語が無い)は ERROR。全部を書く・既定の語を添えるなら通す
    rd_e = {"jsx_elements": [{"tag": "select", "label": "集計単位", "options": [{"value": "register", "text": "レジ別（現行と同じ）"}, {"value": "shop", "text": "店舗別（レジ合算）"}]},
                             {"tag": "select", "label": "店舗", "options": [{"value": "", "text": "- 全店 -"}]}]}
    got = check_enum_mentions({"api_spec": [{"action": "act", "description": "POS会計データを店舗別に期間集計する"}]}, rd_e)
    assert len(got) == 1 and got[0].startswith("[ERROR] [列挙]") and got[0].endswith("→ 差し戻し先 S2") and "レジ別" in got[0], got
    assert check_enum_mentions({"api_spec": [{"action": "act", "description": "店舗別かレジ別で集計する"}]}, rd_e) == []
    assert check_enum_mentions({"api_spec": [{"action": "act", "description": "店舗別に集計する(既定)"}]}, rd_e) == []
    # 第19回: 全選択肢を書いた引数の行(表示名の語、または value の語)があれば、description が片方だけでも通す。引数の行も片方だけなら ERROR のまま
    full_p = {"name": "group", "description": "集計単位。register はレジ別、shop は店舗別", "validation": "register または shop"}
    assert check_enum_mentions({"api_spec": [{"action": "act", "description": "店舗別に集計する", "request_params": [full_p]}]}, rd_e) == []
    assert check_enum_mentions({"api_spec": [{"action": "act", "description": "店舗別に集計する", "request_params": [{"name": "group", "description": "集計単位", "validation": "register または shop"}]}]}, rd_e) == []
    assert len(check_enum_mentions({"api_spec": [{"action": "act", "description": "店舗別に集計する", "request_params": [{"name": "group", "description": "店舗別にまとめる", "validation": "文字列"}]}]}, rd_e)) == 1
    # 形(run15): [列挙] の行が run_units.parse_sendbacks で S2 に拾われる。印の無い行は main の入口で [ERROR] が付く。差し戻し先の無い ERROR 行は数えられる
    import run_units
    assert list(run_units.parse_sendbacks("\n".join(normalize_findings(got)), run_units.load_units())) == ["S2"], got
    assert normalize_findings(["[別名] x → 差し戻し先 H2", "[WARN] w", "[ERROR] e → 差し戻し先 S1", "[INFO] i"]) == \
        ["[ERROR] [別名] x → 差し戻し先 H2", "[WARN] w", "[ERROR] e → 差し戻し先 S1", "[INFO] i"]
    assert lines_without_sendback(normalize_findings(got)) == [] and len(lines_without_sendback(["[ERROR] [x] 差し戻し先を書き忘れた行", "[WARN] w"])) == 1
    assert run_units.parse_sendbacks(form_info_line(["[ERROR] [x] 書き忘れ"]), run_units.load_units()) == {}, "INFO 行が差し戻しとして拾われる"
    # B15 d: SQL の日本語の文字列は、対の応答項目に字面どおり書く
    # fallback-ok: COALESCE は SQL の式の字面そのもの(検査の入力)。既定値への切り替えではない
    apis_l = {"act": {"db_operations": [{"columns": [{"alias_name": "shop_name", "expression": "COALESCE(s.name, '(不明:' || p.mise_no || ')')"}, {"alias_name": "kyakusuu", "expression": "SUM(t.n)"}]}]}}
    dl = {"api_spec": [{"action": "act", "response_items": [{"key": "rows[].shop_name", "transform_backend": "「不明:」に店舗番号を付ける"}, {"key": "rows[].kyakusuu", "transform_backend": ""}]}]}
    got = check_literal_transform(dl, apis_l)
    assert len(got) == 1 and got[0].startswith("[文字列]") and "(不明:" in got[0] and "差し戻し先 S2" in got[0], got
    dl["api_spec"][0]["response_items"][0]["description"] = "名称が無いときは (不明: に店舗番号を付ける"
    assert check_literal_transform(dl, apis_l) == []
    # B15 g: 「DB 例外」の断定は ERROR(「(推測)」の文は通す)
    fb = {"validations": [{"no": "VL1", "field": "日付", "rule": "暦に無い日付は DB 例外になる"}],
          "api_spec": [{"action": "act", "description": "集計する", "error_patterns": [{"condition": "暦に無い日付は形式検査を通り DB 例外(500)になる"}, {"condition": "形式が不正(推測)。DB 例外になる(推測)"}]}]}
    got = check_forbidden_words(fb)
    assert len(got) == 2 and got[0].startswith("[禁止語] validations[VL1]") and "差し戻し先 H4" in got[0] and "差し戻し先 S2" in got[1], got
    # error_patterns が api_spec の外(backend_processes の処理の下)にあっても見る
    nested = {"backend_processes": [{"processing": [{"error_patterns": [{"condition": "形式が正しくても DB 例外になる"}]}]}]}
    got = check_forbidden_words(nested)
    assert len(got) == 1 and "差し戻し先 S2" in got[0] and "backend_processes" in got[0], got
    # B15 f: 行名に括弧書きを足した項目名は ERROR。表示専用の行名の欠けも ERROR(接頭辞の WARN は付けない)
    got = check_items([{"item_name": "小計（現金計）"}, {"item_name": "小計"}], [], [{"specs": [{"label": "小計"}]}])
    assert len(got) == 1 and "括弧書き" in got[0] and not got[0].startswith("[WARN]"), got
    # 形(run8 の旧書式「→ 行名どおり…(差し戻し先 S1)」は parse_sendbacks が拾えなかった): 末尾が「→ 差し戻し先 S1」
    assert got[0].endswith("→ 差し戻し先 S1") and lines_without_sendback(normalize_findings(got)) == [], got
    # 正例: 行名そのものが括弧付き(display_rows の labels・specs[].label と一字一句同じ)は通す
    assert check_items([{"item_name": "小計（税込売上金額）"}], None, [{"labels": ["小計（税込売上金額）"]}]) == []
    assert check_items([{"item_name": "小計（値引後 税抜売上金額）"}], None, [{"specs": [{"label": "小計（値引後 税抜売上金額）"}]}]) == []
    # 正例: jsx の文字列リテラルと一字一句同じ項目名は通す(行名 小計 に対して括弧付きの jsx の label)
    assert check_items([{"item_name": "小計"}, {"item_name": "小計（現金計）"}], [{"tag": "Label", "label": "小計（現金計）", "item_type_hint": "Label"}], [{"specs": [{"label": "小計"}]}]) == []
    # 負例: 既存の行名「小計」に括弧を足し、jsx にも同じ字面が無いものは ERROR のまま
    got = check_items([{"item_name": "小計"}, {"item_name": "小計（現金計）"}], None, [{"specs": [{"label": "小計"}]}])
    assert len(got) == 1 and "括弧書き" in got[0] and not got[0].startswith("[WARN]"), got
    got = check_items([], None, [{"specs": [{"label": "返品"}]}])
    assert len(got) == 1 and "返品" in got[0] and not got[0].startswith("[WARN]"), got
    # 式の並び・テンプレート(_Pat): 穴は任意の値。固定部分の語順は照合する。入れ子のテンプレートも拾う
    with tempfile.TemporaryDirectory() as d:
        tsx2 = Path(d, "T.tsx")
        tsx2.write_text("\n".join([
            "const sub = `${applied.from} 〜 ${applied.to} / ${rows.length}${group === 'shop' ? '店舗' : 'レジ'}分`;",
            "const hdr = `${r.shop_name}${r.reji_no ? ` / レジNO ${r.reji_no}` : ''} / ${applied.from} 〜 ${applied.to}（${r.days}日）`;",
            "<li>",
            "  {r.shop_name}",
            "  {r.reji_no ? `（レジ${r.reji_no}）` : ''}： {r.warn_dates.join(' / ')}",
            "</li>",
            "{error && <div className='error-note'>{error}</div>}",
            "<span>読込中…</span>",
        ]), encoding="utf-8")
        ps = collect_source_strings([tsx2])
        assert _example_in_source("2026-10-01 〜 2026-10-14 / 3レジ分", ps), "合計カードの副題の形を通さない"
        assert _example_in_source("横浜店（レジ2）： 2026-10-05 / 2026-10-09", ps), "式の並びの形を通さない"
        assert _example_in_source("横浜店 / レジNO 1 / 2026-10-01 〜 2026-10-14（14日）", ps), "入れ子のテンプレートを通さない"
        assert not _example_in_source("横浜店 / レジNO 1 / 2026-10-01 まで（14日）", ps), "語順の違う例示を通した"
        line_at = lambda a, b: [{"file": str(tsx2), "line_start": a, "line_end": b}]
        assert _wholly_runtime(line_at(7, 7)) and not _wholly_runtime(line_at(8, 8)), "まるごと実行時の判定が違う"
        assert check_examples([{"item_name": "エラー表示", "example": "Error: Failed to fetch", "src": line_at(7, 7)}], ps) == [], "まるごと実行時の項目を WARN にした"
        got = check_examples([{"item_name": "読込中表示", "example": "作り話の表示", "src": line_at(8, 8)}], ps)
        assert len(got) == 1 and got[0].startswith("[WARN] [例示値]"), got
    # round14 R2: JSX は改行を含む空白がタグに接すると空白を捨てる。画面どおりの全文(こちらはPOSが…)を通す。空白を足した従来の形も通す。作り話は通さない
    with tempfile.TemporaryDirectory() as td:
        r2 = Path(td, "note.tsx")
        r2.write_text('export default function P() {\n  return (\n    <div className="meta-note">\n'
                      '        ソースは <code>pos_account_reports</code>（POS＝PowerDo の取込結果）。\n'
                      '        売上レポートが受注から集計するのに対し、こちらは\n'
                      '        <strong>POSが締めた金額そのまま</strong>で、金額は税込。\n'
                      '        <br />\n    </div>\n  );\n}\n', encoding="utf-8")
        rs = collect_source_strings([r2])
        full = "ソースは pos_account_reports（POS＝PowerDo の取込結果）。 売上レポートが受注から集計するのに対し、こちらはPOSが締めた金額そのままで、金額は税込。"
        assert check_examples([{"item_name": "注記", "example": full}], rs) == [], "画面どおりの全文(改行の空白が捨てられる形)を WARN にした"
        assert check_examples([{"item_name": "注記", "example": full.replace("こちらは", "こちらは ")}], rs) == [], "従来の形(空白あり)を通さなくなった"
        assert len(check_examples([{"item_name": "注記", "example": full.replace("POSが締めた", "POSが締めない")}], rs)) == 1, "作り話の全文を通した"
        # チェックの状態と表の行数は導出の値。Label など他の種別は字面で照合する
        chk = lambda t, e: check_examples([{"item_name": "x", "item_type": t, "example": e}], rs)
        assert chk("Checkbox", "オフ") == [] and chk("Grid", "22行(空欄項目を除くと16行)") == [] and chk("Grid", "17行") == [], "導出の値を WARN にした"
        assert len(chk("Label", "オフ")) == 1 and len(chk("Grid", "22行(作り話)")) == 1 and len(chk("Checkbox", "作り話")) == 1, "導出の形以外を通した"
    # 行範囲: 範囲の外の behavior(control_src だけが範囲に重なる)とは結ばない。範囲の中の表示切替に合う kind は通す
    bh16 = [{"id": "BH16", "kind": "display_toggle", "cond": "rows.length > 1", "target": "合計", "src": sp(216), "control_src": []},
            {"id": "BH17", "kind": "enable_toggle", "cond": "!rows.length", "target": "", "src": sp(282), "control_src": sp(200, 232)}]
    v16 = {"no": "VL16", "field": "CSV出力", "rule": "合計行の表示", "kind": "表示切替", "src": sp(200, 232)}
    assert check_kinds([v16], [], bh16) == [], check_kinds([v16], [], bh16)
    assert len(check_kinds([dict(v16, kind="活性切替")], [], bh16)) == 1, "表示切替と活性切替の食い違いを拾わない"
    # T18: 数え方は mapping と react の使用行の両方で項目を結ぶ。日数の手順の行は、結んだ項目の名前がある行だけ ERROR
    t18_api = {"act": {"db_operations": [{"aggregate_units": [{"alias": "days", "func": "count", "group_by": ["s.shop_id", "s.sales_date"], "scope": "main"}]}]}}
    t18_data = {"api_spec": [{"action": "act", "response_to_screen_mapping": [{"api_field_path": "rows[].shop_id", "screen_item_name": "店舗"}]}],
                "screen_layout": {"items": [{"item_name": "会計カードの副題", "remarks": "店名・日数を表示する", "example": "", "src": [{"file": "P.tsx", "line_start": 9, "line_end": 9}]}]},
                "front_processes": [{"steps": [{"description": "会計カードの副題に日数を出す"}, {"description": "件数は店舗の行数で数える"}]}]}
    t18_react = {"field_usage": [{"path": "rows[].days", "uses": [{"src": [{"file": "P.tsx", "line_start": 9, "line_end": 9}]}]}]}
    got = check_item_remarks_units(t18_data, t18_api, t18_react)
    assert len(got) == 2 and got[0].startswith("[数え方]") and got[1].startswith("[ERROR] [数え方]") and "差し戻し先 S3" in got[1], got
    assert check_item_remarks_units(t18_data, t18_api) == [], "react の使用行が無いのに項目を結んだ"
    # T18: 本文が記号で始まる messages・messages_backend は ERROR。code は見ない(messages[].code は message ID のまま。差異 10 は採らない)
    bad_mb = {"messages": [{"message_id": "MSW00001", "code": "MSW00001", "message": "：この期間に取込欠損があります"}],
              "messages_backend": [{"message_id": "MSE00002", "message": "正しい本文", "code": "MSE00002"}]}
    errs = [g for g in check_messages(bad_mb, set()) if g.startswith("[ERROR]")]
    assert len(errs) == 1 and "H4" in errs[0] and "messages[MSW00001]" in errs[0], errs
    # T22(run18): メッセージ本文を src の範囲の画面の全文と照らす。見出し(強調要素)・区切りの「：」・前後の固定文の欠けは ERROR(H4)、全文は通す
    with tempfile.TemporaryDirectory() as d22:
        tp = Path(d22, "W.tsx")
        tp.write_text("\n".join([
            "export default function W() {",                                                  # 1
            "  return (",                                                                     # 2
            '    <div className="warn-note">',                                                # 3
            "      <strong>取込欠損の疑い</strong>：この期間に、取込の不具合があります。",     # 4
            "      該当日の金額は小さくなっています（§6.4）。",                                # 5
            "      <ul>{rows.map((r) => <li>{r.n}</li>)}</ul>",                               # 6
            "    </div>",                                                                     # 7
            '    <div className="m">この期間のデータはありません。{n}件</div>',               # 8
            "  );",                                                                           # 9
            "}",                                                                              # 10
        ]) + "\n", encoding="utf-8")
        sr = [{"file": str(tp), "line_start": 4, "line_end": 5}]
        full = "取込欠損の疑い：この期間に、取込の不具合があります。 該当日の金額は小さくなっています（§6.4）。"
        assert [g for g in _msg_fulltext({"messages": [{"message_id": "MSW00001", "message": full, "src": sr}]})] == [], "全文を誤検出"
        for lost in (full.replace("取込欠損の疑い：", ""), full.replace("該当日の金額は小さくなっています（§6.4）。", ""), full.replace("：", "")):
            got = _msg_fulltext({"messages": [{"message_id": "MSW00001", "message": lost, "src": sr}]})
            assert len(got) == 1 and got[0].startswith("[ERROR] [文言]") and "H4" in got[0], (lost, got)
        # 式({n})で断片が分かれても、固定文が全部あれば通る。範囲の先頭が開始タグでない src(コード・py)は照らさない
        s8 = [{"file": str(tp), "line_start": 8, "line_end": 8}]
        assert _msg_fulltext({"messages": [{"message_id": "MSI00001", "message": "この期間のデータはありません。{n}件", "src": s8}]}) == []
        assert len(_msg_fulltext({"messages": [{"message_id": "MSI00001", "message": "この期間のデータ", "src": s8}]})) == 1
        s1 = [{"file": str(tp), "line_start": 1, "line_end": 2}]
        assert _msg_fulltext({"messages": [{"message_id": "MSE1", "message": "何でも", "src": s1}]}) == []
        assert _msg_fulltext({"messages": [{"message_id": "MSE1", "message": "何でも"}]}) == []
    # T18: 値が無いときを 0 と書いた備考は、num() 等を通らないオペランドを持つ式だけ ERROR
    cg = [{"id": "CC11", "label": "差引額", "expr": "r.a - r.b", "operands": ["a", "b"], "funcs": [], "literals": [],
           "operand_labels": {"a": ["売上"], "b": ["返品"]}, "operand_guard": {"a": "num", "b": "none"}}]
    cg_calc = [{"name": "差引額", "formula": "差引額 = 売上 - 返品", "remarks": "返品が無いときは 0 として扱う", "inputs": [{"name": "売上"}, {"name": "返品"}]}]
    assert [g for g in check_calcs(cg_calc, cg) if g.startswith("[ERROR] [式の欠損]")], check_calcs(cg_calc, cg)
    assert not [g for g in check_calcs([dict(cg_calc[0], remarks="")], cg) if "[式の欠損]" in g], "備考が空なのに ERROR"
    assert check_calcs(cg_calc, cg)[0].endswith("差し戻し先 H4"), "差し戻し先が H4 でない"
    # 否定の文・表示について述べた文は拾わない。肯定の文は拾う(読点をまたぐ形も)
    for ok in ("計算は 0 補完をしない", "計算は返品をそのまま引き、値が無いときの 0 補完はしない（X.tsx 80 行）。返品の表示は値が無いと 0 になる",
               "画面では欠損を 0 と表示する"):
        assert zero_fill_phrase(ok) is None, ok
        assert not [g for g in check_calcs([dict(cg_calc[0], remarks=ok)], cg) if "[式の欠損]" in g], ok
    for ng in ("返品が無いときは 0 として扱う", "値が無いときは、0 とする", "計算は引き算。欠損は 0 として扱う"):
        assert zero_fill_phrase(ng), ng
    # 日数の指摘(説明文だけ見る)。正例: 単位を日数と説明した説明文は指摘。負例: 表示名・ソースの文字列の一致・単位の補足(行数・集計単位による)は通す
    assert _day_unit_claim("期間内の会計日数") and _day_unit_claim("店舗ごとの集計日数を表示する", {"日数"}), "説明文の日数を拾わない"
    assert not _day_unit_claim("期間内の会計日数", {"期間内の会計日数"}), "ソースの文字列と一致する文言を指摘した"
    assert not _day_unit_claim("店舗ごとの行数で数える日数"), "行数の補足があるのに指摘した"
    assert not _day_unit_claim("レジ別は会計日の数、店舗別は行数。件数の単位は集計単位による(日数ではない)"), "集計単位による の補足があるのに指摘した"
    dd_api = {"act": {"db_operations": [{"aggregate_units": [{"alias": "days", "func": "count", "group_by": ["t.mise_no"], "scope": "main"}]}]}}
    dd = {"api_spec": [{"action": "act", "response_items": [{"key": "rows[].days", "display_name": "日数", "description": "期間内の会計日数"}]}]}
    assert len(check_backend_facts(dd, dd_api, [])) == 1, "説明文の日数を拾わない(response_items)"
    assert check_backend_facts(dd, dd_api, [], {"期間内の会計日数"}) == [], "ソースの文字列と一致する説明を指摘した"
    dd["api_spec"][0]["response_items"][0]["description"] = "店舗の行数"
    assert check_backend_facts(dd, dd_api, []) == [], "表示名の日数を指摘した"
    dd_item = {"screen_layout": {"items": [{"item_name": "副題", "remarks": "店舗名と日数を出す。件数の単位は集計単位による", "example": "武蔵小杉（14日）"}]},
               "api_spec": [{"action": "act", "response_to_screen_mapping": [{"api_field_path": "rows[].days", "screen_item_name": "副題"}]}]}
    assert check_item_remarks_units(dd_item, dd_api, None, {"武蔵小杉（14日）"}) == [], "集計単位による の補足があるのに指摘した"
    dd_item["screen_layout"]["items"][0]["remarks"] = "店舗名と日数を出す"
    assert len(check_item_remarks_units(dd_item, dd_api, None, set())) == 1, "説明文の日数を拾わない(項目)"
    dd_item["screen_layout"]["items"][0]["example"] = "作り話の副題（14日）"
    assert len(check_item_remarks_units(dd_item, dd_api, None, set())) == 1, "例示値を見て指摘の数が変わった"
    # V7(新5・新9): 表示切替の文面に CSV の処理が出て、CSV が state を変えないときだけ ERROR。CSV が整形しないのに「CSV」と整形の語を同じ文に書いたものだけ ERROR
    vsp = lambda a, b=None: [{"file": "P.tsx", "line_start": a, "line_end": b or a}]
    v7_vl = {"no": "VL13", "field": "読込中…", "rule": "取得の処理中(loading)だけ「読込中…」を表示する", "applied_when": "表示や CSV の取得の処理が続いているあいだ、「読込中…」を出す", "kind": "表示切替", "src": vsp(285)}
    v7_beh = [{"id": "BH04", "kind": "display_toggle", "cond": "loading", "target": "読込中…", "src": vsp(285), "control_src": []}]
    v7_fe = [{"event": "EV00", "before_call": ["loading=true", "error=null"], "in_finally": ["loading=false"], "on_success": ["rows"], "on_failure": ["error"]}]
    v7_csv = [{"function": "exportCsv", "trigger_events": ["EV07"], "formats_numbers": False}]
    got = check_state_setters([v7_vl], v7_beh, v7_fe, v7_csv)
    assert len(got) == 1 and got[0].startswith("[ERROR] [状態] validations[VL13]") and "loading" in got[0] and "EV00" in got[0] and "差し戻し先 H4" in got[0], got
    # 負例: CSV の処理が state を変えるなら通す / field に CSV があれば通す / 文面に CSV が無ければ通す / src が重ならなければ通す / CSV が無ければ通す
    assert check_state_setters([v7_vl], v7_beh, v7_fe + [{"event": "EV07", "before_call": ["loading=true"]}], v7_csv) == []
    assert check_state_setters([dict(v7_vl, field="CSV出力")], v7_beh, v7_fe, v7_csv) == []
    assert check_state_setters([dict(v7_vl, rule="読込中を表示する", applied_when="取得が続くあいだ")], v7_beh, v7_fe, v7_csv) == []
    assert check_state_setters([dict(v7_vl, src=vsp(300))], v7_beh, v7_fe, v7_csv) == []
    assert check_state_setters([v7_vl], v7_beh, v7_fe, []) == [] and check_state_setters([], v7_beh, v7_fe, v7_csv) == []
    # V7(新9): 整形しない CSV の文に「CSV」と整形の語が同じ文にあれば ERROR。画面の整形と CSV の文を分けて書いたもの・判定できない形は通す
    v7_cl = [{"no": "CL2", "name": "金額・点数の数値化", "expression": "表示・CSV出力で使う値が空のときは0として扱い、3桁区切りの日本式で整形する(例 1,234,500)", "processing_steps": [{"description": "表示・CSV出力で使う値が空のときは0として扱い、3桁区切りの日本式で整形する"}]}]
    got = check_cl_csv_format(v7_cl, [{"formats_numbers": False}])
    assert len(got) == 1 and got[0].startswith("[ERROR] [CSV整形] common_logic[CL2]") and "差し戻し先 S1" in got[0], got
    assert check_cl_csv_format(v7_cl, [{"formats_numbers": True}]) == [] and check_cl_csv_format(v7_cl, [{}]) == [] and check_cl_csv_format(v7_cl, []) == []
    assert check_cl_csv_format(v7_cl, [{"formats_numbers": False}, {"formats_numbers": True}]) == [], "整形する CSV が1つでもあれば読み飛ばす"
    ok_cl = [{"no": "CL9", "expression": "画面の表示は3桁区切りで整形する。CSVの合計行は2行以上のときだけ足す"}]
    assert check_cl_csv_format(ok_cl, [{"formats_numbers": False}]) == [], "文を分けた書き分けを拾った"
    # 第20回(run17 の CL2): CSV を整形しないと否定で書いた文は、「CSV」と整形の語が同じ文にあっても通す。肯定の文・否定と肯定が並ぶ文は従来どおり ERROR
    neg_cl = [{"no": "CL2", "name": "金額・点数の3桁区切り", "description": "画面の数値を3桁区切りで整える",
               "expression": "数値を日本語の表記(1,234 のように3桁区切り)の文字列にする。画面でだけ使い、CSVでは使わない",
               "processing_steps": [{"description": "数値を日本語の表記で3桁ごとにカンマ区切りの文字列にする"}, {"description": "画面の表示だけに使い、CSVの値は整形しない"}]}]
    assert check_cl_csv_format(neg_cl, [{"formats_numbers": False}]) == [], "否定文(整形しない・CSVでは使わない)を肯定と取った"
    for neg in ("CSV出力の値は桁区切りにしない", "CSVの値は整形せず、そのまま出す", "画面でだけ整形し、CSVは整形しない", "整形は画面だけで、CSVには書式を適用しない",
                "CSVは整形しないが、画面は3桁区切りで整形する", "整形でなくCSVの値はそのまま出す", "CSVの書式は対象外"):
        assert not csv_format_claimed(neg), f"否定文を肯定と取った: {neg}"
    for pos in ("CSV出力の値も3桁区切りに整形する", "画面の表示とCSVの値を桁区切りで整形する", "表示・CSV出力で使う値が空のときは0として扱い、3桁区切りの日本式で整形する",
                "画面では整形せず、CSVの値も、桁区切りに整形する", "CSVの値を整形し、画面は整形しない"):
        assert csv_format_claimed(pos), f"肯定文を拾えなかった: {pos}"
    both = [{"no": "CL3", "expression": "画面の表示は整形しない。CSVの値は3桁区切りで整形する"}]
    assert len(check_cl_csv_format(both, [{"formats_numbers": False}])) == 1, "別の文の肯定を見落とした"
    # 追加の照合(_vs_*.py)の自己テスト。WHY: 検査は4つのモジュールに足すので、ここは入口の selftest を1回呼ぶだけにする
    import _vs_checks_ext
    assert _vs_checks_ext.selftest(), "_vs_checks_ext.selftest が False"
    print("selftest OK")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    # --warn-allow-strict: 許可リスト({機能名}_warn_allow.json)に無い WARN を不合格(戻り値1)にする選択肢。run_units.py の gate が付けて呼ぶ。
    # フラグが先頭に来ても機能名と取り違えないよう、argv から取り除いてから機能名を読む。
    warn_allow_strict = "--warn-allow-strict" in argv
    argv = [x for x in argv if x != "--warn-allow-strict"]
    if not argv:
        print("使い方: verify_vs_source.py <機能名> [--warn-allow-strict] | --selftest", file=sys.stderr)
        return 2
    from _config_loader import INTERMEDIATE_DIR
    feat = argv[0]
    jp = Path(INTERMEDIATE_DIR) / f"{feat}.json"
    try:
        data = json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[入力不正] {jp}: {e}", file=sys.stderr)
        return 2
    strings = source_strings(feat)
    # 目的: react 下書きの ui_strings(画面の文言。B4)も照合の文字列に足す。{…} で途切れた文言や、source_files の外の画面を拾う
    n_ui = 0
    for p in sorted(Path(INTERMEDIATE_DIR).glob(f"{feat}_react_draft.json")):
        ui = json.loads(p.read_text(encoding="utf-8")).get("ui_strings") or []
        n_ui += len(ui)
        strings |= {norm(u["text"]) for u in ui if u.get("text")}
    if not strings:
        print("[WARN] [文言] ソースの文字列が1つも取れない(source_files と src の file を確認)")
        return 0
    problems = check_messages(data, strings) + check_examples((data.get("screen_layout") or {}).get("items") or [], strings)
    cands, jsx, beh, disp_rows = [], [], [], []
    bapis, rd_all = {}, {}
    type_problems, n_sql, plan, bound_problems, n_bound, fact_problems = [], 0, None, [], 0, []
    for p in sorted(Path(INTERMEDIATE_DIR).glob(f"{feat}_backend_draft.json")):
        bd = json.loads(p.read_text(encoding="utf-8"))
        apis = bd.get("apis") or {}
        bapis = apis
        type_problems = check_types(data.get("api_spec"), apis)
        # F2: 境界値と共通エラー(DB 例外)の照合。validations は最終の validations か、断片の validations_frontend
        bound_problems = check_boundaries(data.get("validations") or data.get("validations_frontend"), data.get("api_spec"), apis, bd.get("common_errors"))
        # B1 の事実(別名・CTE・副問合せ・集計の単位・例外の code)の照合。[WARN] 付きは終了コードに影響しない
        fact_problems = check_backend_facts(data, apis, bd.get("common_errors"), strings)
        n_bound = sum(len(a.get("boundaries") or []) for a in apis.values())
        n_sql = sum(1 for a in apis.values() for f in a.get("response_fields") or [] if f.get("db_type"))
    for p in sorted(Path(INTERMEDIATE_DIR).glob(f"{feat}_react_draft.json")):
        rd = json.loads(p.read_text(encoding="utf-8"))
        rd_all = rd
        cands, jsx, beh = rd.get("calc_candidates") or [], rd.get("jsx_elements") or [], rd.get("behaviors") or []
        disp_rows = rd.get("display_rows") or []
        plan = ((rd.get("events") or [{}])[0]).get("fetch_plan")
    items = (data.get("screen_layout") or {}).get("items") or []
    item_problems = check_items(items, jsx, disp_rows) if jsx else []
    problems += item_problems
    ops = [i for i in items if i.get("item_type") in _OPERATION_TYPES]
    shown = [j for j in jsx if j.get("tag") in _DISPLAY_TAGS]
    names = {norm(i.get("item_name") or "") for i in items}
    print(f"[INFO] 画面項目の照合: ソースの入力・操作系 {len(jsx) - len(shown)} 件・S1 の入力・操作系 {len(ops)} 件・WARN {len(item_problems)} 件")
    print(f"[INFO] 表示専用要素の照合: ソースの表示専用要素 {len(shown)} 件(visible_if あり {sum(1 for j in shown if j.get('visible_if'))} 件)・S1 に同じ字面の項目あり {sum(1 for j in shown if norm(j.get('text') or '') in names)} 件")
    calc_problems = check_calcs(data.get("calculations") or [], cands) if cands else []
    problems += calc_problems
    kind_problems = check_kinds(data.get("validations") or data.get("validations_frontend"), data.get("front_processes"), beh)
    fetch_problems = check_fetch_plan(plan, data.get("front_processes"))
    # B15: 合成後の DB 名・数え方・選択肢・SQL の文字列・禁止語(ERROR)
    db_problems = check_db_names(data, bapis)
    remark_problems = check_item_remarks_units(data, bapis, rd_all, strings)
    enum_problems = check_enum_mentions(data, rd_all)
    literal_problems = check_literal_transform(data, bapis)
    forbidden_problems = check_forbidden_words(data)
    new_problems = db_problems + remark_problems + enum_problems + literal_problems + forbidden_problems
    front_problems = check_front_rules(data)
    # V7: 表示切替の文面と CSV の動き(新5)、CSV の整形の有無と文(新9)。判定の材料は react 下書きの failure_effects・csv_exports
    state_problems = check_state_setters(data.get("validations") or data.get("validations_frontend"), beh, rd_all.get("failure_effects"), rd_all.get("csv_exports"))
    csv_format_problems = check_cl_csv_format(data.get("common_logic"), rd_all.get("csv_exports"))
    # W11: 例示値の行数の矛盾・書式を落とした transform(ERROR)。display_rows が無い下書きでは何も言わない
    w11_problems = (check_row_count_example(items, disp_rows) + check_transform_format(data.get("api_spec"), items, disp_rows)) if disp_rows else []
    all_lines = problems + kind_problems + type_problems + fetch_problems + bound_problems + fact_problems + new_problems + front_problems + state_problems + csv_format_problems + w11_problems
    # WHY: 追加の照合(写し・例示値・手順・DB。_vs_*.py)は [WARN] だけを返すので、終了コードの判定(下)に混ぜても変わらない
    import _vs_checks_ext
    all_lines += _vs_checks_ext.run_all(INTERMEDIATE_DIR, feat)
    # WHY: ERROR の行は「[ERROR] 〜 → 差し戻し先 X」の形にそろえて出す(run_units.parse_sendbacks が読む形。normalize_findings の WHY)
    all_lines = normalize_findings(all_lines)
    for p in all_lines:
        print(p)
    fact_errors = [p for p in fact_problems if not p.startswith("[WARN]")]
    print(f"[INFO] 下書きの事実(別名・CTE・副問合せ・集計の単位・エラー code)の照合: ERROR {len(fact_errors)} 件・WARN {len(fact_problems) - len(fact_errors)} 件")
    n_code = sum(1 for a in data.get("api_spec") or [] for e in a.get("error_patterns") or [] if isinstance(e, dict) and e.get("error_code"))
    print(f"[INFO] エラー code の照合: api_spec の error_patterns に error_code が入った行 {n_code} 件(common_errors の code_default と照合。0 件なら合成の補いが動いていない)")
    print(f"[INFO] 境界値・DB 例外の照合: 下書きの boundaries {n_bound} 件・WARN {len(bound_problems)} 件")
    null_problems = [p for p in type_problems if not p.startswith("[WARN]")]
    print(f"[INFO] 応答項目の型の照合: ERROR {len(null_problems)} 件・WARN {len(type_problems) - len(null_problems)} 件・SQL の型の手掛かりつきの欄 {n_sql} 件")
    n = len(data.get("calculations") or [])
    print(f"[INFO] 初期取得の順の照合: fetch_plan {(plan or {}).get('mode', 'なし')}・WARN {len(fetch_problems)} 件")
    print(f"[INFO] 計算式の照合: ソースの式 {len(cands)} 件・calculations {n} 件・WARN {len(calc_problems)} 件")
    print(f"[INFO] 処理の種別の照合: ソースの behaviors {len(beh)} 件・ERROR {len(kind_problems)} 件")
    print(f"[INFO] 文言の照合: ソースの文字列 {len(strings)} 件(うち下書きの ui_strings {n_ui} 件)に対し WARN {len(problems)} 件")
    print(f"[INFO] DB 名・数え方・選択肢・SQL の文字列・禁止語の照合: {len(new_problems)} 件(すべて ERROR。差し戻し先は各行の末尾)")
    # U12: FR の手順に画面規則を書いていないか(ERROR。差し戻し先 S3)
    print(f"[INFO] 画面規則の照合(FR の手順に押せ・活性などを書いた行): ERROR {len(front_problems)} 件")
    print(f"[INFO] 表示切替の文面と CSV の動き・CSV の整形の照合: ERROR {len(state_problems) + len(csv_format_problems)} 件")
    print(f"[INFO] 例示値の行数・transform の書式の照合: ERROR {len(w11_problems)} 件")
    # 形の照合: ERROR 行は末尾に差し戻し先の印を持つ。無い行は、差し戻し先が欄名の引き当て頼み(引けなければ unknown)になる。
    print(form_info_line(all_lines))
    # --warn-allow-strict: 許可リストに無い WARN が1件でもあれば不合格にする。「WARN にはすべて説明がつく」を納品の条件にするため。
    # 理由の無い登録は _warn_allow.load_allow が ValueError で拒否する。接続: run_units.py の gate が呼ぶ。
    if warn_allow_strict:
        import _warn_allow
        try:
            allow = _warn_allow.load_allow(Path(INTERMEDIATE_DIR) / f"{feat}_warn_allow.json")
        except ValueError as e:
            print(f"[ERROR] 許可リストが不正: {e}")
            return 1
        bad = _warn_allow.unallowed([p for p in all_lines if p.startswith("[WARN]")], allow)
        if bad:
            for p in bad:
                print(p)
            print(f"[ERROR] 許可リストに無い WARN {len(bad)} 件")
            return 1
    # WHY: 終了コードは、WARN の接頭辞が無い行(ERROR)が1件でもあれば 1。WARN だけでは止めない
    return 1 if any(not p.startswith("[WARN]") for p in all_lines) else 0


if __name__ == "__main__":
    sys.exit(main())
