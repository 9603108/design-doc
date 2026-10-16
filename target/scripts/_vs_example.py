#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_vs_example.py: verify_vs_source の追加検査: 例示値・注記の照合(screen_layout.items[].example・remarks など)(stdlib のみ)

【受け持ち】
例示値・注記。画面項目の example・remarks や、計算・共通ロジックの備考に書かれた例示値・注記が、
ソース(react 下書きの ui_strings・display_rows・state_initials など)の事実と合うかを見る。
d(_vs_common.load の戻り)の読み方は _vs_common の docstring。

【検査の足し方】
  1. check(d) -> list[str] の関数を書く。指摘が無ければ []。必要な JSON が d に無いときも [] を返す(何も言わない)。
  2. 行は _vs_common.warn(tag, where, msg) で作る(新しい検査は [WARN]。終了コードに影響しない)。
  3. 関数を CHECKS に足す。
  4. 検査の正例・負例を、引数なしで bool を返す selftest 用の関数に書き、SELFTESTS に足す(selftest() が全部呼ぶ)。

【接続情報】
  呼出元: _vs_checks_ext.run_all / selftest(verify_vs_source.main と --selftest から)
"""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
import ast
import math
import operator
import re
import tempfile
import unicodedata
from pathlib import Path

import _react_facts
import _vs_common
import verify_vs_source as _vs

# 検査の本体(check(d) -> list[str])。まだ空。後で1検査ずつ足す
CHECKS: list = []
# 検査の自己テスト(引数なし・bool を返す)。まだ空
SELFTESTS: list = []


def check_note_owner(d) -> list:
    """react_draft.display_rows[].specs[].note のうち、同じ注記が1行(1 spec)にしか無いものが、
    main.screen_layout.items[] の remarks・example に出ていれば指摘する。
    WHY: 注記は、それを書いた1行の説明として読まれる前提で書かれている。その行にだけ属する注記が
    ほかの画面項目の説明文に混ざると、別の項目の値の説明として誤って読まれるので、ここで止める。
    注記の所属先は、spec の label を画面項目の item_name と照合して決める。持ち主本人(item_name が label と等しい項目)の文に出ているものは指摘しない。
    Where: 呼出元は run(_vs_checks_ext.run_all 経由で verify_vs_source.main)。
    読むキー: react_draft.display_rows[].specs[].note、main.screen_layout.items[].item_name・remarks・example。
    必要な JSON が d に無いときは [] を返す。
    """
    rd = d.get("react_draft")
    main = d.get("main")
    if not rd or not main:
        return []
    notes = []
    for i, row in enumerate(rd.get("display_rows", [])):
        for j, spec in enumerate(row.get("specs", [])):
            note = spec.get("note")
            if isinstance(note, str) and note.strip():
                notes.append((i, j, note.strip(), spec.get("label")))
    # 注記の文ごとに、display_rows の specs に何行で出るかを数える
    count: dict = {}
    for _, _, note, _ in notes:
        count[note] = count.get(note, 0) + 1
    items = (main.get("screen_layout") or {}).get("items", [])
    out = []
    for i, j, note, label in notes:
        if count[note] != 1:
            continue
        for item in items:
            # WHY: spec の label と同名の項目は注記の持ち主本人なので、その項目の文に出ていても混ざった誤りではない
            if item.get("item_name") == label:
                continue
            for key in ("remarks", "example"):
                text = item.get(key)
                if isinstance(text, str) and note in text:
                    where = f"display_rows[{i}].specs[{j}]"
                    msg = f"注記「{note}」は1行にしか無いのに、画面項目「{item.get('item_name')}」の {key} に出ている"
                    out.append(_vs_common.warn("注記の行", where, msg))
    return out


def _selftest_note_owner_hit() -> bool:
    """check_note_owner の当たり: 1行だけの注記が別項目の remarks に出ていれば1件出る。"""
    d = {
        "react_draft": {"display_rows": [{"specs": [{"label": "A", "note": "※税込"}]}]},
        "main": {"screen_layout": {"items": [{"item_name": "B", "remarks": "金額は※税込で表示"}]}},
    }
    return len(check_note_owner(d)) == 1


def _selftest_note_owner_miss() -> bool:
    """check_note_owner の外れ: 1行だけの注記が画面項目のどの文にも出ていなければ何も出ない。"""
    d = {
        "react_draft": {"display_rows": [{"specs": [{"label": "A", "note": "※税込"}]}]},
        "main": {"screen_layout": {"items": [{"item_name": "B", "remarks": "金額を表示"}]}},
    }
    # 持ち主の外れ: 注記の spec の label と同名の項目(持ち主本人)の remarks にだけ注記があれば何も出ない
    owner = {
        "react_draft": {"display_rows": [{"specs": [{"label": "B", "note": "※税込"}]}]},
        "main": {"screen_layout": {"items": [{"item_name": "B", "remarks": "金額は※税込で表示"}]}},
    }
    return check_note_owner(d) == [] and check_note_owner(owner) == []


# 注記の行の検査を CHECKS と SELFTESTS に登録する(run と selftest が呼ぶ)
CHECKS.append(check_note_owner)
SELFTESTS.append(_selftest_note_owner_hit)
SELFTESTS.append(_selftest_note_owner_miss)


def _fragment_texts(item, rd):
    """画面項目の出典(src)の行範囲にある文言(正規化済み)。TSX の本文は verify_vs_source の _jsx_faithful・_jsx_runs で、
    画面に出る字面の並びに直してから取る。呼出元: check_example_fragment。
    WHY: JSX の空白と強調のタグは画面の字面に無いので、_jsx_faithful で外してから文の並びを取る。
    react 下書きの jsx_elements・ui_strings の text も、同じ行範囲に重なるものだけ足す。"""
    src = item.get("src") or []
    out = []
    for s in src:
        body = _vs._src_body(s) if isinstance(s, dict) else None
        if body is None:
            continue
        for run in _vs._jsx_runs(_vs._jsx_faithful(body)):
            # WHY: 式の穴({shops.length} など)は実行時の値なので、字面の部分だけを残す
            # WHY(G35 b): 行範囲がタグの途中から始まると run にタグの切れ端(onClick={…}> の後ろの「>税抜</option><option value=」)が入る。
            #   そのまま1つの文言にすると、正しい例示値(「税抜」)が「途中から始まる断片」と誤検出されるので、< > で区切った各片を別々の文言として持つ
            #   入れ子の {…}(`{a ? `…${x}…` : "…"}` の外側。内側の ${x} を消した後に残る)・引用符・バッククォートでも区切る
            for piece in re.split(r"[<>{}\"`]", re.sub(r"\{[^{}]*\}", "", run)):
                out.append(_vs.norm(piece))
    for el in list(rd.get("jsx_elements") or []) + list(rd.get("ui_strings") or []):
        if isinstance(el, dict) and el.get("text") and _vs._overlap(src, el.get("src") or []):
            # WHY(G35 b): 下書きの text は {…} やタグ・引用符をまたいで取れることがある(`>税抜</option><option value=`・`cond ? "…" : "まだ…"`)。
            #   そのまま1つの文言にすると、正しい例示値がその途中からの断片と誤検出されるので、< > { } " ` で区切った各片を別々の文言として持つ
            for piece in re.split(r"[<>{}\"`]", str(el["text"])):
                out.append(_vs.norm(piece))
    return [t for t in out if t]


def _is_mid_fragment(ex, t):
    """ex が t の途中からだけ始まる断片か。どの出現も t の先頭でなく、『。』の直後でもないときだけ True。
    WHY: 文言の先頭からの抜き出し・『。』の後の一文からの抜き出しは、画面の文として正しい例なので通す。"""
    starts = [m.start() for m in re.finditer(re.escape(ex), t)]
    return bool(starts) and all(p > 0 and t[p - 1] != "。" for p in starts)


def check_example_fragment(d) -> list:
    """画面項目の example が、画面の文言の途中から始まる断片になっていないかを見る(WARN)。
    対: 項目の src の行範囲にある JSX の文言(_jsx_faithful・_jsx_runs で取る字面)と、react 下書きの jsx_elements・ui_strings の text。
    WHY: example が文の途中から切り出されていると、読み手は画面に出ていない文として読んでしまう。
    Where: 呼出元は run(_vs_checks_ext.run_all 経由で verify_vs_source.main)。
    読むキー: main.screen_layout.items[].item_name・example・src、react_draft.jsx_elements・ui_strings。
    必要な JSON が d に無いときは [] を返す。
    """
    main = d.get("main")
    if not main:
        return []
    rd = d.get("react_draft") or {}
    out = []
    for item in (main.get("screen_layout") or {}).get("items", []):
        ex = _vs.norm(str(item.get("example") or ""))
        if not ex or not item.get("src"):
            continue
        for t in _fragment_texts(item, rd):
            if ex != t and _is_mid_fragment(ex, t):
                where = f"screen_layout.items[{item.get('item_name')}]"
                msg = f"example「{ex}」は文言「{t}」の途中から始まる断片 → 差し戻し先 S1"
                out.append(_vs_common.warn("例示値の断片", where, msg))
                break
    return out


def _fragment_example_count(ex) -> int:
    """selftest 用: 取込欠損の注記(<strong> の後に「：」で続く文)を持つ一時の TSX に対し、example ex の指摘の数を返す。"""
    with tempfile.TemporaryDirectory() as td:
        tsx = Path(td, "N.tsx")
        tsx.write_text("<p>\n  <strong>取込欠損の疑い</strong>：この期間に、上書きされた疑いのある日があります。\n</p>\n", encoding="utf-8")
        src = [{"file": str(tsx), "line_start": 1, "line_end": 3}]
        d = {"main": {"screen_layout": {"items": [{"item_name": "注記", "example": ex, "src": src}]}}, "react_draft": {}}
        return len(check_example_fragment(d))


def _selftest_example_fragment_hit() -> bool:
    """check_example_fragment の当たり: 「：」の後から切り出した例示は、文の途中から始まる断片として1件出る。"""
    return _fragment_example_count("この期間に、上書きされた疑いのある日があります。") == 1


def _selftest_example_fragment_miss() -> bool:
    """check_example_fragment の外れ: 文言の先頭からの全文の例示は何も出ない。"""
    return _fragment_example_count("取込欠損の疑い：この期間に、上書きされた疑いのある日があります。") == 0


def _selftest_example_fragment_tag_remnant() -> bool:
    """G35(b): タグの切れ端(タグの途中から始まる行範囲)を含む文言でも、タグで区切った文言と同じ例示は断片にしない。
    文言の途中からの切り出し(「（シートと同じ）」だけ)は従来どおり1件出る。"""
    def count(ex):
        with tempfile.TemporaryDirectory() as td:
            tsx = Path(td, "T.tsx")
            tsx.write_text('<option value="a">税抜（シートと同じ）</option><option value="b">\n<button onClick={() => del(i)}>外す</button>\n', encoding="utf-8")
            src = [{"file": str(tsx), "line_start": 1, "line_end": 2}]
            d = {"main": {"screen_layout": {"items": [{"item_name": "金額", "example": ex, "src": src}]}}, "react_draft": {}}
            return len(check_example_fragment(d))
    return count("税抜（シートと同じ）") == 0 and count("外す") == 0 and count("（シートと同じ）") == 1


def _selftest_example_fragment_draft_text() -> bool:
    """G35(b): react 下書きの ui_strings の text がタグ・{…}・引用符をまたいでいても、区切った片と同じ例示値は断片にしない。
    片の途中からの切り出し(「ありません」だけ)は従来どおり1件出る。"""
    def count(ex, body="x\n", ui_on=True):
        with tempfile.TemporaryDirectory() as td:
            tsx = Path(td, "U.tsx")
            tsx.write_text(body, encoding="utf-8")
            src = [{"file": str(tsx), "line_start": 1, "line_end": 1}]
            ui = [{"text": '{a ? `保存します` : "まだ担当割りがありません。"}', "src": src},
                  {"text": ">税抜（シートと同じ）</option><option value=", "src": src}] if ui_on else []
            d = {"main": {"screen_layout": {"items": [{"item_name": "文言", "example": ex, "src": src}]}}, "react_draft": {"ui_strings": ui}}
            return len(check_example_fragment(d))
    # ソース本文の入れ子の {…}(内側の ${n} を消した後に外側の {…} が残る)も同じ
    nested = "<p>{a ? `保存${n}件` : \"まだ担当割りがありません。\"}</p>\n"
    return (count("まだ担当割りがありません") == 0 and count("税抜（シートと同じ）") == 0 and count("担当割りがありません") == 1
            and count("まだ担当割りがありません", nested, False) == 0 and count("担当割りがありません", nested, False) == 1)


# 例示値の断片の検査を CHECKS と SELFTESTS に登録する(run と selftest が呼ぶ)
CHECKS.append(check_example_fragment)
SELFTESTS.append(_selftest_example_fragment_hit)
SELFTESTS.append(_selftest_example_fragment_miss)
SELFTESTS.append(_selftest_example_fragment_tag_remnant)
SELFTESTS.append(_selftest_example_fragment_draft_text)


def _error_template_rx(template):
    """client_error_templates の template の正規表現。穴(${…} や {…})を任意の文字列とみなし、字面は逐語で当てる。呼出元: check_error_example。"""
    parts = re.split(r"\$\{[^}]*\}|\{[^{}]*\}", str(template))
    return re.compile(".*?".join(re.escape(p) for p in parts), re.S)


def check_error_example(d) -> list:
    """failure_state を描く画面項目の example のうち、'Error: ' で始まるものが、画面のクライアント側のエラー文の形
    (react_draft.client_error_templates の template の穴を任意の文字列にしたもの)のどれにも当たらなければ指摘する(WARN)。
    'TypeError:' で始まる例示は通す(実行時の例外の文そのもので、画面が作る文ではないため。'Error: ' では始まらないので対象外になる)。
    WHY: 'Error: ' で始まる例示がソースの作る文の形と合わないと、読み手は画面がそう出すと読んでしまう。
    対: react_draft.failure_effects[].failure_state の state を、state_initials の同じ state の bound[].label で結んだ画面項目。
    bound が空のときは、項目の src 本文に {state} という JSX の式があるもの(例: {error && <div>{error}</div>})を結ぶ。
    WHY: extract が bound を取りこぼした版では label で結べず、検査が一度も発火しなかったため。
    Where: 呼出元は run(_vs_checks_ext.run_all 経由で verify_vs_source.main)。
    読むキー: main.screen_layout.items[].item_name・example、react_draft.failure_effects・state_initials・client_error_templates。
    必要な JSON が d に無いときは [] を返す。
    """
    main = d.get("main")
    rd = d.get("react_draft")
    if not main or not rd:
        return []
    states = {f.get("failure_state") for f in rd.get("failure_effects") or [] if isinstance(f, dict) and f.get("failure_state")}
    labels = set()
    for s in rd.get("state_initials") or []:
        if isinstance(s, dict) and s.get("state") in states:
            labels |= {b.get("label") for b in s.get("bound") or [] if isinstance(b, dict) and b.get("label")}
    rxs = [_error_template_rx(t["template"]) for t in rd.get("client_error_templates") or [] if isinstance(t, dict) and t.get("template")]
    out = []
    for item in (main.get("screen_layout") or {}).get("items", []):
        ex = _vs.norm(str(item.get("example") or ""))
        # WHY: bound が空の版では label で結べないので、項目の src 本文の {state} で結ぶ
        body = "".join(_vs._src_body(s) or "" for s in item.get("src") or [] if isinstance(s, dict))
        linked = item.get("item_name") in labels or any(re.search(r"\{\s*%s\s*\}" % re.escape(st), body) for st in states)
        if not linked or not ex.startswith("Error: "):
            continue
        # WHY: 'Error: ' は new Error(template) を String(e) で表示したときの接頭辞なので、外した残りを template と比べる
        msg_body = ex[len("Error: "):]
        if any(rx.fullmatch(msg_body) for rx in rxs):
            continue
        where = f"screen_layout.items[{item.get('item_name')}]"
        out.append(_vs_common.warn("例示値のエラー文", where, f"example「{ex}」は画面のエラー文の形(client_error_templates)のどれにも当たらない → 差し戻し先 S1"))
    return out


def _error_example_count(example, template) -> int:
    """selftest 用: failure_state の画面項目(エラー表示)に example を置き、template を1つ持つ d に対する指摘の数を返す。"""
    d = {
        "main": {"screen_layout": {"items": [{"item_name": "エラー表示", "example": example}]}},
        "react_draft": {
            "failure_effects": [{"event": "EV01", "failure_state": "error"}],
            "state_initials": [{"state": "error", "bound": [{"id": "JX05", "label": "エラー表示"}]}],
            "client_error_templates": [{"template": template}],
        },
    }
    return len(check_error_example(d))


def _selftest_error_example_hit() -> bool:
    """check_error_example の当たり: 'Error: ' で始まり、どのエラー文の形にも当たらない例示は1件出る。"""
    return _error_example_count("Error: 取得失敗", "取得に失敗しました（${r2.status}）") == 1


def _selftest_error_example_miss() -> bool:
    """check_error_example の外れ: 形に当たる 'Error: ' の例示と、'TypeError:' の例示は何も出ない。"""
    return _error_example_count("Error: 500", "${r2.status}") == 0 and _error_example_count("TypeError: x is undefined", "取得に失敗しました（${r2.status}）") == 0


def _selftest_error_example_body() -> bool:
    """check_error_example の当たり(bound 空): 項目の src 本文に {error} があり、Error: 付きの例示が形に当たらなければ1件出る。"""
    with tempfile.TemporaryDirectory() as td:
        tsx = Path(td, "E.tsx")
        tsx.write_text("{error && <div>{error}</div>}\n", encoding="utf-8")
        src = [{"file": str(tsx), "line_start": 1, "line_end": 1}]
        d = {
            "main": {"screen_layout": {"items": [{"item_name": "エラー表示", "example": "Error: 取得失敗", "src": src}]}},
            "react_draft": {
                "failure_effects": [{"event": "EV01", "failure_state": "error"}],
                "state_initials": [{"state": "error", "bound": []}],
                "client_error_templates": [{"template": "取得に失敗しました（${r2.status}）"}],
            },
        }
        return len(check_error_example(d)) == 1


# 例示値のエラー文の検査を CHECKS と SELFTESTS に登録する(run と selftest が呼ぶ)
CHECKS.append(check_error_example)
SELFTESTS.append(_selftest_error_example_hit)
SELFTESTS.append(_selftest_error_example_miss)
SELFTESTS.append(_selftest_error_example_body)


_ARITH_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
# Math.floor・Math.ceil・Math.round・Math.trunc(JS の丸め。round は JS どおり 0.5 を切り上げる)
_MATH_FUNCS = {"floor": math.floor, "ceil": math.ceil, "trunc": math.trunc,
               "round": lambda x: math.floor(x + 0.5)}


def _js_to_py(expr):
    """JS の式の字面を Python の式の字面にする。直すのは Math.xxx( → xxx( と、式全体が `条件 ? 値 : 値` の形の三項演算子だけ。
    WHY: 平均単価・客単価のように `r.n ? Math.floor(r.a / r.n) : 0` と書く式を検算の対象にするため。入れ子の三項は直さない(Python の式にならず None になる)。"""
    s = re.sub(r"\bMath\.(floor|ceil|round|trunc)\(", r"\1(", str(expr).strip())
    m = re.fullmatch(r"(.+?)\s*\?\s*(.+)\s*:\s*([^:?]+)", s)
    return f"({m.group(2)}) if ({m.group(1)}) else ({m.group(3)})" if m else s


def _eval_arith(expr, values):
    """画面ソースの式 expr(JS の字面)を、r.<オペランド> を values の数に置き換えて計算する。
    使えるのは + - * / と括弧と数の定数と、Math.floor 等と `条件 ? 値 : 値`(条件は数の真偽)だけ。eval は使わず、ast の木を自前でたどる。
    読めない式・0 除算は None を返す。呼出元: check_example_arithmetic。"""
    def walk(n):
        if isinstance(n, ast.Expression):
            return walk(n.body)
        if isinstance(n, ast.BinOp) and type(n.op) in _ARITH_OPS:
            return _ARITH_OPS[type(n.op)](walk(n.left), walk(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = walk(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.IfExp):
            return walk(n.body) if walk(n.test) else walk(n.orelse)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _MATH_FUNCS
                and len(n.args) == 1 and not n.keywords):
            return _MATH_FUNCS[n.func.id](walk(n.args[0]))
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return n.value
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "r" and n.attr in values:
            return values[n.attr]
        raise ValueError("式の字面が使えない")
    try:
        return float(walk(ast.parse(_js_to_py(expr), mode="eval")))
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError):
        return None


def _example_number(ex):
    """例示値を数として読む(桁区切り・単位を除き、数字の並びだけなら数)。(値, 小数の桁数) を返し、読めなければ None。
    WHY: 例示値は画面の表示(1,000円・12件など)なので、単位と区切りを外してから数に直す。呼出元: check_example_arithmetic。"""
    s = re.sub(r"[,円件日行点名人%\s]", "", unicodedata.normalize("NFKC", str(ex or "")))
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    return float(s), (len(s.split(".")[1]) if "." in s else 0)


def check_example_arithmetic(d) -> list:
    """react 下書きの calc_candidates の式(+ - * / と括弧だけのもの)を、オペランドの画面項目の例示値で計算し、
    結果の画面項目の例示値と違えば指摘する(WARN)。読めない値・結べないオペランドがあれば飛ばす。
    対: calc_candidates[].operand_labels のオペランド → 画面項目の item_name。結果の項目は calc_candidates[].label と item_name の字面で結ぶ。
    WHY: 例示値の数が式と合わないと、読み手は画面の計算の結果をその数と読んでしまう。
    Where: 呼出元は run(_vs_checks_ext.run_all 経由で verify_vs_source.main)。
    読むキー: main.screen_layout.items[].item_name・example、react_draft.calc_candidates[].label・expr・operands・operand_labels。
    必要な JSON が d に無いときは [] を返す。
    """
    main = d.get("main")
    rd = d.get("react_draft")
    if not main or not rd:
        return []
    by_name = {}
    for i in (main.get("screen_layout") or {}).get("items", []):
        if isinstance(i, dict):
            by_name.setdefault(i.get("item_name"), []).append(i)

    def values_of(label):
        """項目名 label の読める値(値, 小数の桁数)の集合。同じ名前の項目が複数あるときは全部見る。
        WHY: 同名の項目(「合計」が2つ並ぶ画面)は別の数なので、1つ目だけを取ると別の項目の値を式に入れてしまう。"""
        return {_example_number(i.get("example")) for i in by_name.get(label, [])} - {None}

    out = []
    for c in rd.get("calc_candidates") or []:
        if not isinstance(c, dict):
            continue
        res_set = values_of(c.get("label"))
        if len(res_set) != 1:
            continue
        res = res_set.pop()
        values, ambiguous = {}, False
        for o in c.get("operands") or []:
            # WHY: オペランドが読める値を1つに決められないとき(同名や別名の項目が別の数)は、どれかを推測せず飛ばす
            #      (例: 合計 と 合計2(支払科目計) は別の数。片方を選ぶと、式に合わない誤った指摘になる)
            #      さらに、結びの名前のうち1つでも読める値が無い(画面項目に無い字面)ときも飛ばす。残った別の名前
            #      (例: 「合計」)の値だけで決めると、別の項目の値を式に入れてしまう(run8 で実測した誤検出)
            lbs = (c.get("operand_labels") or {}).get(o) or []
            per = [values_of(lb) for lb in lbs]
            vs = set().union(*per)
            if len(vs) != 1 or any(not p for p in per):
                ambiguous = True
                break
            values[o] = vs.pop()[0]
        if ambiguous or len(values) != len(c.get("operands") or []):
            continue
        calc = _eval_arith(c.get("expr"), values)
        if calc is None:
            continue
        value, dec = res
        if abs(round(calc, dec) - value) > 1e-9:
            where = f"screen_layout.items[{c.get('label')}]"
            msg = f"例示値 {value:g} が式 {c.get('expr')} の計算結果 {round(calc, dec):g} と違う → 差し戻し先 S1"
            out.append(_vs_common.warn("例示値の検算", where, msg))
    return out


def _arith_count(result_example) -> int:
    """selftest 用: 差引 = 小計 - 返品 の式を持つ d に、結果の例示値 result_example を置いたときの指摘の数を返す。"""
    d = {
        "main": {"screen_layout": {"items": [
            {"item_name": "小計", "example": "1,000円"},
            {"item_name": "返品", "example": "300円"},
            {"item_name": "差引", "example": result_example},
        ]}},
        "react_draft": {"calc_candidates": [
            {"id": "CC1", "label": "差引", "expr": "r.a - r.b", "operands": ["a", "b"], "operand_labels": {"a": ["小計"], "b": ["返品"]}},
        ]},
    }
    return len(check_example_arithmetic(d))


def _selftest_example_arith_hit() -> bool:
    """check_example_arithmetic の当たり: 1,000 - 300 = 700 のところに 800 と書けば1件出る。"""
    return _arith_count("800円") == 1


def _selftest_example_arith_miss() -> bool:
    """check_example_arithmetic の外れ: 式どおりの 700 円は出ない。式が読めない(% 演算)候補は飛ばす。
    結びの名前の1つが画面項目に無い(run8 の「合計2(支払科目計)」)ときも、残った名前の値で決めず飛ばす。"""
    d = {
        "main": {"screen_layout": {"items": [{"item_name": "小計", "example": "1,000円"}, {"item_name": "差引", "example": "700円"}]}},
        "react_draft": {"calc_candidates": [{"id": "CC2", "label": "差引", "expr": "r.a % 7", "operands": ["a"], "operand_labels": {"a": ["小計"]}}]},
    }
    # 同じオペランドに当たる2つの項目の値が違うときは、どちらかを選ばず飛ばす
    amb = {
        "main": {"screen_layout": {"items": [{"item_name": "小計", "example": "1,000円"}, {"item_name": "小計2", "example": "900円"}, {"item_name": "差引", "example": "700円"}]}},
        "react_draft": {"calc_candidates": [{"id": "CC3", "label": "差引", "expr": "r.a - 300", "operands": ["a"], "operand_labels": {"a": ["小計", "小計2"]}}]},
    }
    # 結びの名前のうち「小計0」は画面項目に無い。「小計」の値だけで計算すると 1,000 - 300 = 700 でなく別の項目の値を使う恐れがあるので飛ばす
    unres = {
        "main": {"screen_layout": {"items": [{"item_name": "小計", "example": "1,000円"}, {"item_name": "差引", "example": "5円"}]}},
        "react_draft": {"calc_candidates": [{"id": "CC4", "label": "差引", "expr": "r.a - 300", "operands": ["a"], "operand_labels": {"a": ["小計", "小計0"]}}]},
    }
    return (_arith_count("700円") == 0 and len(check_example_arithmetic(d)) == 0 and len(check_example_arithmetic(amb)) == 0
            and len(check_example_arithmetic(unres)) == 0)


def _floor_count(example, n="620") -> int:
    """selftest 用: 客単価 = r.n ? Math.floor(r.a / r.n) : 0 の式(a=1,259,600・n)に、結果の例示値 example を置いたときの指摘の数。"""
    d = {
        "main": {"screen_layout": {"items": [
            {"item_name": "売上", "example": "1,259,600円"}, {"item_name": "客数", "example": f"{n}人"},
            {"item_name": "客単価", "example": example}]}},
        "react_draft": {"calc_candidates": [
            {"id": "CC5", "label": "客単価", "expr": "r.n ? Math.floor(r.a / r.n) : 0", "operands": ["n", "a"],
             "operand_labels": {"a": ["売上"], "n": ["客数"]}}]},
    }
    return len(check_example_arithmetic(d))


def _selftest_example_arith_floor() -> bool:
    """当たり: 1,259,600 ÷ 620 = 2031.6 の切り捨ては 2,031 なので、例示値 2,032 は1件(run10 で実測)。外れ: 2,031 は0件。三項の else 側(客数0)の 0 も読める。"""
    return _floor_count("2,032円") == 1 and _floor_count("2,031円") == 0 and _floor_count("0円", "0") == 0


# 例示値の検算の検査を CHECKS と SELFTESTS に登録する(run と selftest が呼ぶ)
CHECKS.append(check_example_arithmetic)
SELFTESTS.append(_selftest_example_arith_hit)
SELFTESTS.append(_selftest_example_arith_miss)
SELFTESTS.append(_selftest_example_arith_floor)


# 件数の表示の式・件数の写しの判定・単位の読み方は _react_facts が正本(第20回)。下書きの count_displays と合成 _compose_mapping が同じものを使うため、
# ここには定義を持たず別名で受ける(WHY の詳細は _react_facts の COUNT_SHOWN・shown_units・is_count_map)。
_COUNT_SHOWN = _react_facts.COUNT_SHOWN
_COUNT_WORDS = _react_facts.COUNT_WORDS
_is_count_map = _react_facts.is_count_map


def _count_mapping_ok(maps, item_name, resp_key) -> bool:
    """item_name への写しに、応答 resp_key の件数の写しがあるか(_is_count_map)。呼出元: check_count_mapping。
    resp_key が None のとき(下書きが応答のキーを持たない古い版)は、根を問わず同じ形・語で見る。"""
    return any(m.get("screen_item_name") == item_name and _is_count_map(m, resp_key) for m in maps)


# 件数の直後の字面(単位)の読み方(shown_units)も _react_facts が正本。呼出元: check_count_mapping
_shown_units = _react_facts.shown_units


# transform が件数の形を書く部分「N件」「N 店」の N の直後の日本語の語。呼出元: _unit_mismatch
_N_FORM = re.compile(r"(?<![A-Za-z])N\s*([぀-ヿ一-鿿]{1,6})")


def _unit_mismatch(transform, units):
    """transform の「N○○」の形の単位 ○○ が、画面の単位(units)のどれとも前方一致しないとき、その ○○ の並びを返す。合う・書いていない・units が無いときは None。
    前方一致で見るのは、「店舗分または」のように transform の語が単位の後ろへ続いても、単位から始まっていれば合うとするため(「N件」は「店舗分」「レジ分」と合わない)。"""
    forms = _N_FORM.findall(str(transform or ""))
    if not forms or not units:
        return None
    if any(u.startswith(f) or f.startswith(u) for f in forms for u in units):
        return None
    return forms


def check_count_mapping(d) -> list:
    """画面が配列の件数({shops.length}・${rows.length})を表示している項目に、その応答の件数の写しが無ければ指摘する(WARN)。
    対: react_draft.failure_effects[].on_success_from(state → 応答のキー)で API の応答から入る配列 state の .length を、画面項目の
    出典(src)の行範囲の字面から拾い、api_spec[].response_to_screen_mapping に同じ項目名への件数の写しがあるかを見る。
    WHY: 件数は応答の1項目でなく配列の長さなので、field_usage(rows[].xxx の使用)に載らず、[写しの漏れ] が拾えない。件数がどの応答から来るかが
    写しに無いと、設計書だけでは数の出所が分からない(run14: IT14 の「N レジ分」・IT64 の「N 店だけ」)。
    1つの式を複数の項目の src が囲むとき(Card の見出しと副題など)は、いちばん狭い範囲の項目だけを持ち主とする(囲む側を誤検出しない)。
    写しがあるときも、件数の式の直後の字面(`{X.length}店だけ`・`${X.length}${cond ? "店舗" : "レジ"}分`)から画面の単位を取り、transform が「N件」の形で
    別の単位を書いていれば指摘する(run15 の IT15: 画面は「N店舗分/Nレジ分」、transform は「N件」)。transform に N の形が無い・単位が読めないときは照合しない。
    Where: 呼出元は run(_vs_checks_ext.run_all 経由で verify_vs_source.main)。
    読むキー: main.screen_layout.items[].item_name・no・src、main.api_spec[].response_to_screen_mapping、react_draft.failure_effects。
    必要な JSON が d に無いときは [] を返す。"""
    main = d.get("main")
    rd = d.get("react_draft")
    if not main or not rd:
        return []
    # 応答から入る配列 state → 応答のキー。on_success_from(新しい下書き)が正。無い古い下書きは、成功時に設定する state のうち初期値が [] のものを、
    # 応答のキー不明(None)で入れる(run8・run9 の下書きは on_success_from を持たない)
    empty_arr = {s.get("state") for s in rd.get("state_initials") or [] if isinstance(s, dict) and str(s.get("initial") or "").strip() == "[]"}
    arr_of = {}
    for fe in rd.get("failure_effects") or []:
        if not isinstance(fe, dict):
            continue
        frm = fe.get("on_success_from") if isinstance(fe.get("on_success_from"), dict) else {}
        arr_of.update({k: v for k, v in frm.items() if k and v})
        for st in fe.get("on_success") or []:
            if st in empty_arr:
                arr_of.setdefault(st, None)
    if not arr_of:
        return []
    items = [i for i in (main.get("screen_layout") or {}).get("items") or [] if isinstance(i, dict)]
    maps = [m for a in main.get("api_spec") or [] if isinstance(a, dict) for m in a.get("response_to_screen_mapping") or [] if isinstance(m, dict)]
    files = {}
    for it in items:
        for s in it.get("src") or []:
            if isinstance(s, dict) and s.get("file"):
                files.setdefault(s["file"], None)
    out = []
    for f in files:
        try:
            lines = Path(f).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln, text in enumerate(lines, 1):
            for m in _COUNT_SHOWN.finditer(text):
                state = m.group(1)
                if state not in arr_of:
                    continue
                # この行を囲む出典を持つ項目のうち、範囲がいちばん狭いもの(同じ狭さは全部)
                cover = [(int(s["line_end"]) - int(s["line_start"]), it) for it in items for s in it.get("src") or []
                         if isinstance(s, dict) and s.get("file") == f and isinstance(s.get("line_start"), int)
                         and s["line_start"] <= ln <= int(s.get("line_end") or s["line_start"])]
                if not cover:
                    continue
                narrow = min(w for w, _ in cover)
                owners = list({id(it): it for w, it in cover if w == narrow}.values())
                # WHY: 同じ範囲の項目が複数あるとき(Card の見出しと副題が同じ行範囲を持つ run11 の IT13・IT14)は、どれが件数を出すか src で決められない。
                #      どれかに件数の写しがあれば足り、1件も無いときだけ、候補を並べて1件で指摘する(同じ式で項目の数だけ指摘して重ねない)
                key = arr_of[state]  # None は古い下書き(応答のキー不明)
                names = "・".join(f"「{o.get('item_name')}」" for o in owners) + ("のどれか" if len(owners) > 1 else "")
                nos = "・".join(str(o.get("no") or o.get("item_name")) for o in owners)
                where = f"screen_layout.items[{nos}] ({Path(f).name}:{ln})"
                if any(_count_mapping_ok(maps, o.get("item_name"), key) for o in owners):
                    # WHY: 件数の写しがあっても、transform が件数の単位を画面の字面と違う語(「N件」)で書くと、読み手は画面に「N件」と出ると読む
                    #      (run15 の IT15: ソースは「N店舗分/Nレジ分」、transform は「N件」。JSX の中では合っていて、transform だけが食い違った)。
                    #      画面の単位は件数の式の直後の字面から取り、読めない(式の直後が日本語でも式の穴でもない)ときは照合しない
                    units = _shown_units(text[m.end():]) if m.group(0).endswith("}") else []
                    names_set = {o.get("item_name") for o in owners}
                    for mm in maps:
                        forms = _unit_mismatch(mm.get("transform"), units) if mm.get("screen_item_name") in names_set and _is_count_map(mm, key) else None
                        if forms:
                            shown = "/".join(f"N{u}" for u in units)
                            out.append(_vs_common.warn("件数の写し", where, f"写しの transform が件数を「N{forms[0]}」と書いているが、画面は「{shown}」と出す"
                                                       f"({Path(f).name}:{ln})。transform の件数の表し方を画面の字面に合わせる → 差し戻し先 S2"))
                            break
                    continue
                msg = (f"画面は {state}.length({'応答 ' + key if key else '応答の配列'} の件数)を表示しているが、response_to_screen_mapping に{names}への"
                       f"件数の写しが無い。api_field_path を応答の配列の key({key or '<配列名>'}。response_items に {key or '<配列名>'}.length の key があればそれ)にし、"
                       "transform に件数を何として出すかを書く → 差し戻し先 S2")
                out.append(_vs_common.warn("件数の写し", where, msg))
    return list(dict.fromkeys(out))


def _count_mapping_out(maps, same_span_item=None, react=None) -> list:
    """selftest 用: 件数を表示する2行(IT64 の {shops.length}・IT14 の ${rows.length})、条件に使う行(.length > 1・!rows.length)、
    それらを囲む広い項目(IT13)を持つ一時の TSX と、その d(写しは maps)に check_count_mapping を掛けた結果を返す。呼出元: _selftest_count_mapping_*。"""
    with tempfile.TemporaryDirectory() as td:
        tsx = Path(td, "C.tsx")
        tsx.write_text("\n".join([
            "{rows.length > 1 && (",                                   # 1
            "  <Card",                                                  # 2
            "    subtitle={`${applied.from} / ${rows.length}レジ分`}",  # 3
            "  />",                                                     # 4
            ")}",                                                       # 5
            "<button disabled={!rows.length}>CSV</button>",             # 6
            "<div>POSがあるのは",                                       # 7
            "  {shops.length}店だけ",                                   # 8
            "</div>",                                                   # 9
        ]) + "\n", encoding="utf-8")
        f = str(tsx)

        def span(a, b):
            return [{"file": f, "line_start": a, "line_end": b}]
        items = [{"no": "IT13", "item_name": "合計カードの見出し", "src": span(1, 5)},
                 {"no": "IT14", "item_name": "合計カードの副題", "src": span(3, 3)},
                 {"no": "IT8", "item_name": "CSV出力", "src": span(6, 6)},
                 {"no": "IT64", "item_name": "注記", "src": span(7, 9)}]
        if same_span_item:  # 同じ範囲(3 行目)を持つもう1つの項目(IT14 と同じ狭さ)
            items.append(dict(same_span_item, src=span(3, 3)))
        d = {
            "main": {"screen_layout": {"items": items}, "api_spec": [{"response_to_screen_mapping": maps}]},
            "react_draft": react or {"failure_effects": [{"call": "fetchShops", "on_success_from": {"shops": "rows"}},
                                                         {"call": "fetchRows", "on_success_from": {"rows": "rows"}}]},
        }
        return check_count_mapping(d)


def _selftest_count_mapping_hit() -> bool:
    """check_count_mapping の当たり: 件数の写しが無ければ表示している2項目(IT14・IT64)だけ出る。囲む広い項目(IT13)・条件の .length(IT8 など)は出ない。"""
    out = _count_mapping_out([{"api_field_path": "rows[].id", "screen_item_name": "注記"}])
    return len(out) == 2 and any("IT14" in o for o in out) and any("IT64" in o for o in out) and not any("IT13" in o or "IT8" in o for o in out) \
        and all(o.endswith("差し戻し先 S2") for o in out)


def _selftest_count_mapping_miss() -> bool:
    """check_count_mapping の外れ: 件数の写し(path が rows.length)・transform に件数の語がある写しがあれば出ない。応答の根が違う写しは件数と認めない。"""
    ok = [{"api_field_path": "rows.length", "screen_item_name": "合計カードの副題", "transform": "行数を出す"},
          {"api_field_path": "rows[].id", "screen_item_name": "注記", "transform": "選択肢の件数を「N 店」に出す"}]
    other_root = [{"api_field_path": "items.length", "screen_item_name": "合計カードの副題"},
                  {"api_field_path": "rows.length", "screen_item_name": "注記"}]
    return _count_mapping_out(ok) == [] and len(_count_mapping_out(other_root)) == 1


def _selftest_count_mapping_tie_old() -> bool:
    """check_count_mapping の同じ範囲・古い下書き。
    同じ範囲の項目が2つ(IT14 と IT15)あり件数の写しが無ければ、IT14 の分は1件にまとまり(「のどれか」)、どちらかに写しがあれば出ない(run11 の IT13・IT14)。
    on_success_from が無い古い下書き(run8・run9)は、成功時に設定する初期値 [] の state を配列とし、応答のキーを問わず件数の形の写しを認める。"""
    tie = _count_mapping_out([], same_span_item={"no": "IT15", "item_name": "副題の別名"})
    tie_hit = [o for o in tie if "IT14" in o]
    ok = _count_mapping_out([{"api_field_path": "rows.length", "screen_item_name": "副題の別名"}], same_span_item={"no": "IT15", "item_name": "副題の別名"})
    old = {"failure_effects": [{"call": "fetchShops", "on_success": ["shops"]}, {"call": "fetchRows", "on_success": ["rows", "other"]}],
           "state_initials": [{"state": "shops", "initial": "[]"}, {"state": "rows", "initial": "[]"}, {"state": "other", "initial": "false"}]}
    old_hit = _count_mapping_out([], react=old)
    old_ok = _count_mapping_out([{"api_field_path": "items.length", "screen_item_name": "合計カードの副題"},
                                 {"api_field_path": "rows", "screen_item_name": "注記"}], react=old)
    return (len(tie_hit) == 1 and "IT14・IT15" in tie_hit[0] and "のどれか" in tie_hit[0] and all("IT13" not in o for o in tie)
            and [o for o in ok if "IT14" in o] == [] and len(old_hit) == 2 and old_ok == [])


def _count_unit_out(transform) -> list:
    """selftest 用: run15 の PosReport.tsx:308 と同じ形の1行(${rows.length}${group === "shop" ? "店舗" : "レジ"}分)を持つ一時の TSX と、
    その副題への件数の写し(transform を指定)に check_count_mapping を掛けた結果を返す。"""
    with tempfile.TemporaryDirectory() as td:
        tsx = Path(td, "U.tsx")
        tsx.write_text("subtitle={`${applied.from} / ${rows.length}${group === \"shop\" ? \"店舗\" : \"レジ\"}分`}\n", encoding="utf-8")
        d = {
            "main": {"screen_layout": {"items": [{"no": "IT15", "item_name": "合計カードの副題", "src": [{"file": str(tsx), "line_start": 1, "line_end": 1}]}]},
                     "api_spec": [{"response_to_screen_mapping": [{"api_field_path": "rows", "screen_item_name": "合計カードの副題", "transform": transform}]}]},
            "react_draft": {"failure_effects": [{"call": "fetchRows", "on_success_from": {"rows": "rows"}}]},
        }
        return check_count_mapping(d)


def _selftest_count_mapping_unit() -> bool:
    """check_count_mapping の単位の照合(run15 の差2)。transform が「N件」なのに画面が「N店舗分/Nレジ分」なら1件(差し戻し先 S2)、
    画面の字面どおり(「N店舗分」「Nレジ分」)・N の形を書かない transform は0件。_shown_units は直後の語・式の穴のリテラルから単位を取る。"""
    hit = _count_unit_out("応答 rows の件数を、合計カードの副題に「N件」の形で出す")
    return (len(hit) == 1 and "「N件」" in hit[0] and "N店舗分/Nレジ分" in hit[0] and hit[0].endswith("→ 差し戻し先 S2")
            and _count_unit_out("応答 rows の件数を、集計単位がレジ別なら「Nレジ分」、店舗別なら「N店舗分」の形で出す") == []
            and _count_unit_out("応答 rows の件数を、合計カードの副題に出す") == []
            and _shown_units("店だけ</div>") == ["店だけ"]
            and _shown_units('${group === "shop" ? "店舗" : "レジ"}分`}') == ["店舗分", "レジ分"]
            and _shown_units("}件") == [])


# 件数の写しの検査を CHECKS と SELFTESTS に登録する(run と selftest が呼ぶ)
CHECKS.append(check_count_mapping)
SELFTESTS.append(_selftest_count_mapping_hit)
SELFTESTS.append(_selftest_count_mapping_miss)
SELFTESTS.append(_selftest_count_mapping_tie_old)
SELFTESTS.append(_selftest_count_mapping_unit)


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
