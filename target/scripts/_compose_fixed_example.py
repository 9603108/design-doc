"""固定文(Label)の例示値を、出典 src の JSX から機械で組み立てて埋める(round14 R2・差異 新10)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=60 で、src を付ける _compose_item_src(50)の後、
src を読む後段(_compose_response 110 以降)の前に走る。

WHY: 固定文の例示値は、画面に出る字面そのものが正解で、S1(モデル)に書かせると壊れる。run13 では S1 が正しい全文を書いたのに、
  検査(verify_vs_source。改行の空白を JSX の規則で捨てていなかった)が1字の空白の差で WARN にし、S1 が「検査を通すため」本文の
  一部(「（POS＝PowerDo …）。」)だけにした。文頭の「ソースは pos_account_reports」が落ち、初期表示後の項目表の全文と食い違った。
  画面の字面はソースの JSX から決まる事実なので、モデルに書かせず、src の JSX から組み立てる。

付け方(item_type が Label で、TSX の src が1件だけの項目。決まらない項目は触らない。推測しない):
  1. src の行範囲の先頭が JSX の開始タグであること(配列の行・属性の行・式だけの行は対象外)
  2. 子要素のテキストは連結する(強調・コード等のインライン要素は境界に空白を足さない。<br /> と段落の要素は空白1つ)
  3. テキストは JSX の空白の規則で読む(行の前後の空白を捨て、空でない行を空白1つでつなぐ。改行を含む空白がタグ・式に接する所は空白無し)
  4. {式} は、ソースの値が決まるときだけ値にする(引用符の文字列・${} の無いテンプレート・ファイル先頭の const の文字列/数値)。
     決まらない式(state・props・関数の結果・条件)が1つでもあれば、その項目は触らず、決まらない式をログに残す
     (実行時に決まる値は S1 が書いた例示値を使う。初期表示の値とは別)
  5. 範囲が開いた要素の途中の文で終わり、次の行がその続き({式}・文字・インライン要素)なら、字面は途中で切れているので触らない
     (<br />・閉じタグ・ブロックの要素が次に来るときは文の切れ目)。最初の要素が閉じた後の閉じ括弧・セミコロンは読まない
  6. 範囲が強調・コード等のインライン要素で始まり、その要素が閉じた後に文字が続くときは触らない(src は行単位なので、
     見出しの項目の範囲か後ろの本文の項目の範囲かが決まらない。どちらの字面も正解になりうる)
  既に同じ字面なら触らない(冪等)。
"""
import html
import re
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
from pathlib import Path

ORDER = 60

# WHY: 文の途中に入るインライン要素。境界に空白を足さない。他の要素(div・p・li・h1 等)と <br /> の境界は空白1つにする
_INLINE = {"strong", "em", "b", "i", "code", "span", "small", "a", "kbd", "u", "mark", "sub", "sup", "label", "abbr"}
_TAG = re.compile(r"<(/?)([A-Za-z][\w.]*)?")
_CONST = re.compile(
    r"^(?:export\s+)?const\s+(\w+)\s*(?::\s*[^=\n]+?)?=\s*(?:\"([^\"\\\n]*)\"|'([^'\\\n]*)'|(-?\d+(?:\.\d+)?))\s*;?\s*$", re.M)


def _jsx_clean(s):
    """JSX のテキストの空白の規則(Babel の cleanJSXElementLiteralChild と同じ)。
    行に分け、先頭以外の行は前の空白を、末尾以外の行は後ろの空白を捨て、空でない行を空白1つでつなぐ。"""
    lines = s.replace("\t", " ").replace("\r", "").split("\n")
    out = []
    for k, ln in enumerate(lines):
        if k != 0:
            ln = ln.lstrip(" ")
        if k != len(lines) - 1:
            ln = ln.rstrip(" ")
        if ln:
            out.append(ln)
    return " ".join(out)


def _consts(text):
    """ファイル先頭(行頭)の const のうち、文字列・数値のリテラルで決まるもの(名前 → 文字列)。"""
    out = {}
    for m in _CONST.finditer(text):
        v = next((g for g in m.group(2, 3, 4) if g is not None), None)
        if v is not None:
            out[m.group(1)] = v
    return out


def _expr(inner, consts):
    """JSX の {式} の値。引用符の文字列・${} の無いテンプレート・コメント・const の文字列/数値だけ決まる。決まらなければ None。"""
    s = inner.strip()
    if re.fullmatch(r"/\*.*\*/", s, re.S):
        return ""
    m = re.fullmatch(r"\"([^\"\\]*)\"|'([^'\\]*)'|`([^`\\$]*)`", s)
    if m:
        return next(g for g in m.groups() if g is not None)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return s
    return consts.get(s)


def screen_text(text, line_start, line_end):
    """TSX の本文 text の行範囲(1 始まり・両端含む)が描く画面の文字列を (文字列, 決まらない式の一覧) で返す。
    範囲の先頭が JSX の開始タグでなければ None。呼出元: fill。"""
    from _react_facts import _end, _tag_end
    lines = text.split("\n")
    offs = [0]
    for ln in lines:
        offs.append(offs[-1] + len(ln) + 1)
    if not (1 <= line_start <= line_end <= len(lines)):
        return None
    start, stop = offs[line_start - 1], min(offs[line_end], len(text))
    i = start
    while i < stop and text[i] in " \t\r\n":
        i += 1
    if not re.match(r"<(?:[A-Za-z]|>)", text[i:i + 2]):
        return None
    consts = _consts(text)
    nodes, buf, unresolved = [], [], []
    depth, closed_top, last, first = 0, False, "", None

    def flush():
        if buf:
            nodes.append(html.unescape(_jsx_clean("".join(buf))))
            buf.clear()

    while i < stop:
        c = text[i]
        if c == "<":
            te = _tag_end(text, i)
            if te >= stop:
                break  # 範囲の外へ続くタグは読まない
            m = _TAG.match(text, i)
            closing, name = m.group(1) == "/", m.group(2) or ""
            if first is None:
                first = name
            flush()
            selfclose = text[te - 1:te] == "/"
            if name == "br":
                nodes.append(" ")
            elif name and (name[0].isupper() or "." in name):
                # 部品(<Card … />)の中身は、その部品の定義で決まる。ここでは決められない
                if not closing:
                    unresolved.append(f"<{name}>")
            elif name not in _INLINE and name:
                nodes.append(" ")
            if closing:
                depth -= 1
            elif not selfclose:
                depth += 1
            closed_top = depth <= 0
            last = "tag"
            i = te + 1
        elif c == "{":
            if closed_top and first in _INLINE:
                unresolved.append("範囲の先頭の強調要素の後ろに文字が続く")
                break
            j = _end(text, i)
            flush()
            v = _expr(text[i + 1:j], consts)
            if v is None:
                unresolved.append("{" + text[i + 1:j].strip() + "}")
            else:
                nodes.append(v)
            i = j + 1
            last = "text"
        else:
            if closed_top and c in ");,}]":
                break  # 最初の要素が閉じた後の閉じ括弧・セミコロン等は JSX の文ではなくコード。ここで読み終える
            if closed_top and first in _INLINE and not c.isspace():
                # WHY: 範囲は行単位なので、強調要素(<strong>見出し</strong>：本文)から始まる範囲は、見出しの項目の範囲か、
                #      後ろの本文の項目の範囲かが決まらない。どちらの字面も正解になりうるので触らない(run13 の取込欠損の疑い・警告文)
                unresolved.append("範囲の先頭の強調要素の後ろに文字が続く")
                break
            buf.append(c)
            if not c.isspace():
                last = "text"
            i += 1
    flush()
    # WHY: 範囲が開いた要素の途中の文で終わり、次の行がその文の続き(式・文字・インライン要素)なら、字面は途中で切れている(run11 の IT61)。
    #      <br />・閉じタグ・ブロックの要素が次に来るときだけ、範囲の終わりを文の切れ目とみなす
    if depth > 0 and last == "text":
        rest = [ln.strip() for ln in lines[line_end:] if ln.strip()]
        nxt = rest[0] if rest else ""
        m = re.match(r"<(/?)([A-Za-z][\w.]*)?", nxt)
        natural = nxt == "" or (nxt.startswith("<") and m and (m.group(1) == "/" or m.group(2) == "br" or (m.group(2) or "") not in _INLINE))
        if not natural:
            unresolved.append("範囲の終わりが文の途中")
    return re.sub(r"\s+", " ", "".join(nodes)).strip(), unresolved


def literal_screen_text(file, line_start, line_end):
    """TSX の行範囲が描く画面の文字列。文字列リテラルだけで決まるとき(決まらない式・文の途中の切れ目・強調要素の曖昧が無いとき)だけ返す。
    それ以外・読めないファイル・空は None。呼出元: fill(example)・merge_partials._src_fact(初期値。両方が同じ字面になる)。"""
    try:
        got = screen_text(Path(file).read_text(encoding="utf-8"), int(line_start), int(line_end))
    except (OSError, UnicodeDecodeError):
        return None
    if got is None or got[1] or not got[0]:
        return None
    return got[0]


def fixed_segments(text, line_start, line_end):
    """TSX の行範囲が描く画面の文の、固定文の並び(決まらない式・段落の要素・<br /> で区切った断片。強調・コード等のインライン要素では区切らない)。
    範囲の先頭が JSX の開始タグでなければ None。決まらない式は穴として断片を区切るだけで、値は問わない。
    WHY(2026-10-15 run18): screen_text は「強調要素(<strong>見出し</strong>)で始まり、後ろに文字が続く範囲」を、見出しか本文かが決まらないとして
    返さない(例示値を触らないための慎重さ)。メッセージの本文は逆で、src の範囲の画面の文を全部写すのが正解なので、見出しも「：」も含めた
    固定文そのものが要る(MSW00001 の「取込欠損の疑い：」が本文から落ちたのを、verify_vs_source が捕まえるための断片)。
    呼出元: verify_vs_source.check_messages(_msg_fulltext)。"""
    from _react_facts import _end, _tag_end
    lines = text.split("\n")
    if not (1 <= line_start <= line_end <= len(lines)):
        return None
    chunk = "\n".join(lines[line_start - 1:line_end])
    chunk = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", chunk, flags=re.S)
    if not re.match(r"\s*<(?:[A-Za-z]|>)", chunk):
        return None
    consts = _consts(text)
    segs, nodes, buf = [], [], []
    depth, closed_top, i = 0, False, 0

    def flush_buf():
        if buf:
            nodes.append(html.unescape(_jsx_clean("".join(buf))))
            buf.clear()

    def cut():
        flush_buf()
        seg = "".join(nodes)
        nodes.clear()
        if seg.strip():
            segs.append(re.sub(r"\s+", " ", seg).strip())

    while i < len(chunk):
        c = chunk[i]
        try:
            if c == "<" and re.match(r"<(?:[A-Za-z/]|>)", chunk[i:i + 2]):
                te = _tag_end(chunk, i)
                m = _TAG.match(chunk, i)
                closing, name = m.group(1) == "/", m.group(2) or ""
                flush_buf()
                if name == "br" or (name and (name[0].isupper() or "." in name)) or (name and name not in _INLINE):
                    cut()
                selfclose = chunk[te - 1:te] == "/"
                if closing:
                    depth -= 1
                elif not selfclose:
                    depth += 1
                closed_top = depth <= 0
                i = te + 1
                continue
            if c == "{":
                j = _end(chunk, i)
                v = _expr(chunk[i + 1:j], consts)
                if v is None:
                    cut()
                else:
                    buf.append(v)
                i = j + 1
                continue
        except (ValueError, IndexError):
            break  # 範囲の外へ続く括弧・タグは読まない
        if closed_top and c in ");,}]":
            break  # 最初の要素が閉じた後の閉じ括弧・セミコロンは JSX の文ではなくコード
        buf.append(c)
        i += 1
    cut()
    return segs


def _tsx_src(item):
    """項目の src のうち TSX/JSX の1件(file・line_start・line_end)。TSX の src が1件でなければ None(どの範囲か決められない)。"""
    srcs = item.get("src") or []
    if isinstance(srcs, dict):
        srcs = [srcs]
    cands = [s for s in srcs if isinstance(s, dict) and s.get("line_start") and str(s.get("file") or "").endswith((".tsx", ".jsx"))]
    if len(cands) != 1 or not Path(str(cands[0]["file"])).is_file():
        return None
    return cands[0]


# G31: ソースに根拠の無い例示値(店名・担当者名・金額など実行時のデータで決まる欄)の決まった見本値。
# WHY: 担当が「A店」「佐藤」「38,500」と思い思いに作り、回ごとに例示値が揺れた(POS では金額が38件)。型ごとに1つに固定して揺れを消す。
# 手順書(i5.4)の表と同じ字面。呼出元: _runtime_example(→ fill)。
EXAMPLE_BY_TYPE = {"date": "2026-10-01", "amount": "1,234 円", "count": "123", "ratio": "12.3%", "code": "1", "name": "実行時の値"}

# 項目名・format・data_type の語から型を決める語彙。上から順に当て、当たらなければ型が取れない(None=触らない)
_TYPE_WORDS = (
    ("ratio", re.compile(r"率|割合|比率|%|％")),
    ("count", re.compile(r"数$|件数|点数|枚数|人数|数量")),
    ("amount", re.compile(r"金額|売上|単価|価格|原価|利益|税|残高|円")),
    ("date", re.compile(r"日付|年月|日$|yyyy|YYYY")),
    ("code", re.compile(r"コード|番号|ID|code")),
    ("name", re.compile(r"名$|名称|氏名|担当者|店舗|店名|顧客|得意先|取引先")),
)
_UNIT = re.compile(r"単位[:：=]?\s*([^\s,、。)）]+)")


def _runtime_example(item):
    """項目1件の、ソースに根拠が無い例示値の見本値(EXAMPLE_BY_TYPE)。根拠がある・型が取れない・Label・固定文言は None(触らない)。
    根拠の有無は screen_text 系の既存の判定(literal_screen_text が字面を決められたか)で決め、新しい判定を重ねない。
    呼出元: fill。個数の単位は項目の format・remarks の「単位: ○」から付ける(無ければ数値だけ)。"""
    if item.get("item_type") == "Label" or item.get("format") == "固定文言":
        return None  # 画面のソースが描く固定の文字(ラベル・見出し)は対象外
    s = _tsx_src(item)
    if s is not None and literal_screen_text(s["file"], s["line_start"], s.get("line_end") or s["line_start"]):
        return None  # 出典の JSX が字面を決める欄はソースが正
    hay = " ".join(str(item.get(k) or "") for k in ("item_name", "format", "data_type"))
    kind = next((k for k, rx in _TYPE_WORDS if rx.search(hay)), None)
    if kind is None:
        return None
    if kind == "count":
        u = _UNIT.search(f"{item.get('format') or ''} {item.get('remarks') or ''}")
        return EXAMPLE_BY_TYPE["count"] + (f" {u.group(1)}" if u else "")
    return EXAMPLE_BY_TYPE[kind]


def fill(out, log):
    """screen_layout.items のうち Label の example を、出典 src の JSX が描く字面に直す。決まらない項目は触らない。"""
    items = [x for x in ((out.get("screen_layout") or {}).get("items") or []) if isinstance(x, dict)]  # fallback-ok: 画面項目を持たない断片の合成では何もしない
    cache, done, skipped = {}, [], []
    for it in items:
        if it.get("item_type") != "Label":
            new = _runtime_example(it)  # G31: Label 以外の、実行時のデータが入る欄を型ごとの見本値にそろえる
            old = str(it.get("example") or "")
            if new is not None and old != new:
                it["example"] = new
                log.append(f"[例示値] {it.get('item_name')} の例示値「{old}」を「{new}」に揃える(ソースに根拠が無い)")
            continue
        s = _tsx_src(it)
        if s is None:
            continue
        f = str(s["file"])
        if f not in cache:
            cache[f] = Path(f).read_text(encoding="utf-8")
        got = screen_text(cache[f], int(s["line_start"]), int(s.get("line_end") or s["line_start"]))  # fallback-ok: 終了行が無い src は開始行だけの1行
        if got is None:
            continue
        text, unresolved = got
        name = str(it.get("item_name"))
        if unresolved:
            skipped.append(f"{name}({'・'.join(dict.fromkeys(unresolved))})")
            continue
        if not text or re.sub(r"\s+", " ", str(it.get("example") or "")).strip() == text:
            continue
        it["example"] = text
        done.append(name)
    if done:
        log.append(f"[R2] 固定文の例示値を出典の JSX が描く字面に直した: {len(done)} 件: " + "、".join(done))
    if skipped:
        log.append(f"[R2] 字面がソースで決まらず例示値を触らない固定文 {len(skipped)} 件: " + "、".join(skipped))


_SRC = """const NOTE = "注意";
export default function P({ shops, ok }) {
  const LEFT = [
    { label: "総売上" },
  ];
  return (
    <main>
      <span className="loading">読込中…</span>
      <div className="meta">
        ソースは <code>pos_account_reports</code>（POS＝PowerDo の取込結果）。
        売上レポートが受注から集計するのに対し、こちらは
        <strong>POSが締めた金額そのまま</strong>で、金額は税込。
        <br />
        「<span className="tag">空</span>」= 既定で非表示。
      </div>
      <p>{" "}前{"後"}{NOTE}&amp;{/* 注 */}</p>
      <div className="m2">店は{shops.length}店だけ。</div>
      <div className="m3">{ok && <b>条件付き</b>}</div>
      <Badge tone="x" />
      <Card
        title="会計レポート"
      />
      <div className="m4">
        前半の文。
        <br />
        後半は
        {shops.length}店。
      </div>
      <div className="m6">
        <strong>見出し</strong>：続きの文。
      </div>
      <label>店舗</label>
      <title>
        x
      </title>
    </main>
  );
}
"""


def _selftest():
    import tempfile

    def at(needle, n=0):
        return [k for k, ln in enumerate(_SRC.split("\n"), 1) if needle in ln][n]

    with tempfile.TemporaryDirectory() as td:
        f = Path(td, "P.tsx")
        f.write_text(_SRC, encoding="utf-8")
        F = str(f)

        def src(a, b):
            return [{"file": F, "line_start": a, "line_end": b}]

        meta = at('<div className="meta">')
        out = {"screen_layout": {"items": [
            {"item_name": "注記", "item_type": "Label", "example": "（POS＝PowerDo の取込結果）。", "src": src(meta, at("<br />"))},
            {"item_name": "読込中", "item_type": "Label", "example": "読込中", "src": src(at("loading"), at("loading"))},
            {"item_name": "段落", "item_type": "Label", "example": "x", "src": src(at("<p>"), at("<p>"))},
            {"item_name": "店数", "item_type": "Label", "example": "店は11店だけ。", "src": src(at("m2"), at("m2"))},
            {"item_name": "条件", "item_type": "Label", "example": "条件付き", "src": src(at("m3"), at("m3"))},
            {"item_name": "部品", "item_type": "Label", "example": "会計レポート", "src": src(at("<Badge"), at("<Badge"))},
            {"item_name": "行", "item_type": "Label", "example": "総売上", "src": src(at('{ label: "総売上" }'), at('{ label: "総売上" }'))},
            {"item_name": "属性", "item_type": "Label", "example": "会計レポート", "src": src(at('title="会計'), at('title="会計'))},
            {"item_name": "ボタン", "item_type": "Button", "example": "旧", "src": src(at("<label>"), at("<label>"))},
            {"item_name": "同じ", "item_type": "Label", "example": "店舗", "src": src(at("<label>"), at("<label>"))},
            {"item_name": "複数の出典", "item_type": "Label", "example": "旧", "src": src(at("<label>"), at("<label>")) + src(at("loading"), at("loading"))},
            {"item_name": "出典なし", "item_type": "Label", "example": "旧"},
            {"item_name": "複数行の要素", "item_type": "Label", "example": "旧", "src": src(at("<title>"), at("</title>"))},
            {"item_name": "開始タグだけ", "item_type": "Label", "example": "旧", "src": src(at("<title>"), at("<title>"))},
            {"item_name": "文の切れ目", "item_type": "Label", "example": "旧", "src": src(at("m4"), at("前半の文"))},
            {"item_name": "文の途中", "item_type": "Label", "example": "旧", "src": src(at("m4"), at("後半は"))},
            {"item_name": "強調と続き", "item_type": "Label", "example": "旧", "src": src(at("<strong>見出し"), at("<strong>見出し"))},
            # G31: 実行時のデータが入る欄(Label 以外で出典の字面が決まらない)は、型ごとの見本値にそろえる
            {"item_name": "店名", "item_type": "TextBox", "example": "A店"},
            {"item_name": "担当者名", "item_type": "TextBox", "example": "佐藤"},
            {"item_name": "売上金額", "item_type": "TextBox", "example": "500 円"},
            {"item_name": "対象日付", "item_type": "Calendar", "example": "2025/1/1"},
            {"item_name": "点数", "item_type": "TextBox", "example": "9 点", "format": "単位: 点"},
            {"item_name": "備考", "item_type": "TextBox", "example": "自由記述"},                      # 型が取れない欄は触らない
            {"item_name": "店舗名(出典あり)", "item_type": "TextBox", "example": "店舗", "src": src(at("<label>"), at("<label>"))},  # 出典が字面を決める欄は触らない
            {"item_name": "担当者名ラベル", "item_type": "Label", "example": "店舗", "src": src(at("<label>"), at("<label>"))},  # Label は対象外
        ]}}
        log = []
        fill(out, log)
        ex = {i["item_name"]: i["example"] for i in out["screen_layout"]["items"]}
        # 画面どおりの全文: 「こちらは」と <strong> の間は改行の空白が捨てられ、<code> の前の「ソースは 」の空白は残る。<br /> の後ろは範囲の外
        want = ("ソースは pos_account_reports（POS＝PowerDo の取込結果）。 売上レポートが受注から集計するのに対し、こちらは"
                "POSが締めた金額そのままで、金額は税込。")
        assert ex["注記"] == want, ex["注記"]
        assert ex["読込中"] == "読込中…", ex["読込中"]
        assert ex["段落"] == "前後注意&", ex["段落"]           # {"後"}・const・実体参照・コメント。前後の空白は画面でも見えない(段落の外)
        assert ex["店数"] == "店は11店だけ。", ex["店数"]       # {shops.length} は決まらない。S1 の値を触らない
        assert ex["条件"] == "条件付き", ex["条件"]             # {ok && …} は決まらない
        assert ex["部品"] == "会計レポート", ex["部品"]         # 部品の中身は決められない
        assert ex["行"] == "総売上" and ex["属性"] == "会計レポート", (ex["行"], ex["属性"])  # 開始タグで始まらない範囲は触らない
        assert ex["ボタン"] == "旧", ex["ボタン"]                # Label だけ
        assert ex["同じ"] == "店舗", ex["同じ"]
        assert ex["複数の出典"] == "旧" and ex["出典なし"] == "旧", ex
        assert ex["複数行の要素"] == "x", ex["複数行の要素"]     # 開始タグから閉じタグまでの範囲は中身を拾う
        assert ex["開始タグだけ"] == "旧", ex["開始タグだけ"]    # 中身が範囲の外なら字面が決まらない(空にしない)
        assert ex["文の切れ目"] == "前半の文。", ex["文の切れ目"]   # 範囲の終わりの次が <br /> なら文の切れ目
        assert ex["文の途中"] == "旧", ex["文の途中"]               # 次の行が {式} で続く文の途中で切れた範囲は触らない
        assert ex["強調と続き"] == "旧", ex["強調と続き"]            # 強調要素で始まる範囲の後ろに文字が続くと、見出しの項目か本文の項目か決まらない
        assert ex["店名"] == "実行時の値" and ex["担当者名"] == "実行時の値", (ex["店名"], ex["担当者名"])
        assert ex["売上金額"] == "1,234 円" and ex["対象日付"] == "2026-10-01" and ex["点数"] == "123 点", ex
        assert ex["備考"] == "自由記述" and ex["店舗名(出典あり)"] == "店舗" and ex["担当者名ラベル"] == "店舗", ex
        assert any("[例示値] 店名 の例示値「A店」を「実行時の値」に揃える(ソースに根拠が無い)" in x for x in log), log
        assert any("固定文の例示値を出典の JSX が描く字面に直した: 5 件" in x and "注記" in x for x in log), log
        assert any("字面がソースで決まらず例示値を触らない固定文 5 件" in x and "{shops.length}" in x and "<Badge>" in x and "文の途中" in x and "強調要素" in x for x in log), log
        # 2回目は何も変えない(冪等)
        snap = repr(out)
        log2 = []
        fill(out, log2)
        assert repr(out) == snap and not any("直した" in x for x in log2), log2
        # 閉じた後に続く行(兄弟の閉じタグ)があっても、字面は変わらない
        t = screen_text(_SRC, at("<title>"), at("</title>") + 1)
        assert t == ("x", []), t
        assert screen_text(_SRC, at("{ label"), at("{ label")) is None
        # 要素が閉じた後のコード(閉じ括弧・セミコロン)は字面に入れない
        code = "const A = (\n  <b>終わり</b>\n);\nexport default A;\n"
        assert screen_text(code, 2, 4) == ("終わり", []), screen_text(code, 2, 4)
    print("[selftest OK] _compose_fixed_example (R2: 子要素の連結・JSX の空白の規則・式が決まらない/開始タグで始まらない/部品/複数の出典は触らない・冪等)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_fixed_example.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
