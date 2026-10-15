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
import ast
import math
import operator
import re
import tempfile
import unicodedata
from pathlib import Path

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
            out.append(_vs.norm(re.sub(r"\{[^{}]*\}", "", run)))
    for el in list(rd.get("jsx_elements") or []) + list(rd.get("ui_strings") or []):
        if isinstance(el, dict) and el.get("text") and _vs._overlap(src, el.get("src") or []):
            out.append(_vs.norm(str(el["text"])))
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


# 例示値の断片の検査を CHECKS と SELFTESTS に登録する(run と selftest が呼ぶ)
CHECKS.append(check_example_fragment)
SELFTESTS.append(_selftest_example_fragment_hit)
SELFTESTS.append(_selftest_example_fragment_miss)


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
