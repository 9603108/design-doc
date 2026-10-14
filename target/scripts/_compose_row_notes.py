"""備考の誤写しを取り除く(round11 U11・新1)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=150(応答の写し 120 の後・例示値の計算 160 の前)。
WHY: 画面項目の備考(remarks)を単位(S1)が書くとき、同じ画面の別の行の注記(表示行の note の原文)を写すことがある。
  run10 では「％１（合計）」の備考に「％５（明細）」の注記「％レート1〜5は最終計上 2015-06-15」が入った。
  注記の正は react 下書きの display_rows の spec の note なので、別の行の note の原文を含む文は、
  この項目自身の note に同じ原文が無いかぎり取り除く。足すことはしない(備考の本文は取り除くだけ)。

判定の対象: 画面項目の src の全部が1つの spec の行範囲に収まるものだけ(収まらない項目は触らない)。
照合の字面: 空白・句読点・括弧を除いて比べる。6字未満の原文は照合しない(「全期間ゼロ」のような短い注記が偶然の文に含まれるため)。
取り除く文: 別の行の注記の原文を含む文と、取り除いた後に「注記は、」だけが残る文。
"""
import json
import re
import sys
from pathlib import Path

ORDER = 150

# WHY: 比べるときだけ空白・句読点・括弧を除く。数字・記号(％ ～ − など)は残す(字面の差は括弧と句読点だけに出るため)
_IGNORE = re.compile(r"[\s、。，,．.・:：;；!！?？()（）「」『』]")
_MIN_LEN = 6
# WHY: 取り除いた後に「注記は、」だけが残る文は、中身の無い文なので消す
_BARE = re.compile(r"^注記は[、,]?$")


def _norm(s):
    return _IGNORE.sub("", s or "")


def _drafts(directory):
    """react 下書きを全部読む。読めないファイルは無いものとして飛ばす(_compose_mapping と同じ読み方)。"""
    out = []
    for p in sorted(Path(directory).glob("*_react_draft.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _covers(span, r):
    """行範囲 span(出典の1件)が、出典 r(画面項目の src の1件)を含むか。ファイルが違えば含まない。"""
    if span.get("file") != r.get("file"):
        return False
    s0, s1 = span.get("line_start"), span.get("line_end") or span.get("line_start")
    r0, r1 = r.get("line_start"), r.get("line_end") or r.get("line_start")
    if None in (s0, s1, r0, r1):
        return False
    return s0 <= r0 and r1 <= s1


def _own_spec(item, specs):
    """画面項目の src の各行が、1つの spec の行範囲に収まるときのその spec。決まらなければ None。
    WHY: 2つ以上の spec にまたがる項目は、どの行の注記が正しいか決まらないので触らない。"""
    srcs = [r for r in item.get("src") or [] if isinstance(r, dict) and r.get("file")]
    if not srcs:
        return None
    hit = [sp for sp in specs if all(any(_covers(s, r) for s in sp["src"]) for r in srcs)]
    return hit[0] if len(hit) == 1 else None


def _clean(remarks, foreign, own_norm):
    """備考を文(。で区切る)に分け、取り除く文を外した備考と、取り除いた文の数を返す。
    foreign は、この項目の注記に含まれない別の行の注記の原文(字面を正規化したもの)。"""
    kept, n = [], 0
    for seg in re.split(r"(?<=。)", remarks):
        if not seg:
            continue
        ns = _norm(seg)
        if any(f in ns for f in foreign) or _BARE.match(seg.rstrip("。").strip()):
            n += 1
            continue
        kept.append(seg)
    return "".join(kept), n


def fill(out, log):
    """備考の文のうち、別の行の注記の写しを取り除く(新1)。react 下書きや画面項目が無ければ何もしない。"""
    from _config_loader import INTERMEDIATE_DIR
    items = [x for x in ((out.get("screen_layout") or {}).get("items") or []) if isinstance(x, dict)]
    specs = []
    for d in _drafts(INTERMEDIATE_DIR):
        for row in d.get("display_rows") or []:
            for sp in (row.get("specs") or []) if isinstance(row, dict) else []:
                if isinstance(sp, dict) and sp.get("src"):
                    specs.append(sp)
    if not items or not specs:
        return
    notes = [(sp, _norm(sp.get("note"))) for sp in specs]
    for it in items:
        rem = it.get("remarks")
        if not isinstance(rem, str) or not rem:
            continue
        own = _own_spec(it, specs)
        if own is None:
            continue
        own_norm = _norm(own.get("note"))
        foreign = sorted({n for sp, n in notes if sp is not own and len(n) >= _MIN_LEN and n not in own_norm})
        new, n = _clean(rem, foreign, own_norm)
        if n == 0:
            continue
        no = it.get("no") or it.get("item_name")
        if not new:
            # WHY: 全部の文が外れると備考が空になる。その判断は人に任せ、触らずに知らせる
            log.append(f"WARN _compose_row_notes: {no} の備考の文が全部外れるため触らない")
            continue
        it["remarks"] = new
        log.append(f"[新1] {no} の備考から文を {n} 件取り除く(別の行の注記の写し)")


def _selftest():
    import tempfile
    import _config_loader

    F = "/src/PosReport.tsx"

    def sp(label, a, b, note=None):
        return {"label": label, "note": note, "src": [{"file": F, "line_start": a, "line_end": b}]}

    rows = [{"array": "RIGHT", "specs": [
        sp("％１（合計）", 49, 49),
        sp("％５（明細）", 53, 54, "％レート1〜5は最終計上 2015-06-15"),
        sp("特売値引", 55, 55, "全期間ゼロ"),
        sp("現金過不足", 80, 81, "現金在高・回収高はサブレジ運用停止（2021-12-22）以降ゼロのため常に0"),
        sp("現金在高", 78, 78),
    ]}]
    base = "実測でゼロの行(空)。既定では非表示。"

    def item(no, name, rem, a, b):
        return {"no": no, "item_name": name, "remarks": rem, "src": [{"file": F, "line_start": a, "line_end": b}]}

    def mk():
        return {"screen_layout": {"items": [
            item("IT31", "％１（合計）", base + "注記は、％レート1〜5は最終計上 2015-06-15。", 49, 49),
            item("IT35", "％５（明細）", base + "注記は、％レート1〜5は最終計上 2015-06-15。", 53, 54),
            item("IT36", "特売値引", "実測でゼロの行(空)。注記は「全期間ゼロ」。", 55, 55),
            item("IT56", "現金過不足", "現金在高から回収高を引いた額。注記は、現金在高・回収高はサブレジ運用停止(2021-12-22)以降ゼロのため常に0。", 80, 81),
            item("IT40", "前後", "前の文。注記は、。", 78, 78),
            item("IT99", "二つの行にまたがる", "注記は、％レート1〜5は最終計上 2015-06-15。", 49, 54),
        ]}}

    old = _config_loader.INTERMEDIATE_DIR
    with tempfile.TemporaryDirectory() as td:
        Path(td, "x_react_draft.json").write_text(json.dumps({"display_rows": rows}, ensure_ascii=False), encoding="utf-8")
        _config_loader.INTERMEDIATE_DIR = Path(td)
        try:
            out = mk()
            log = []
            fill(out, log)
            got = {i["no"]: i["remarks"] for i in out["screen_layout"]["items"]}
            assert got["IT31"] == base, got["IT31"]  # 別の行(％５)の注記の写しだけ取り除く
            assert got["IT35"] == base + "注記は、％レート1〜5は最終計上 2015-06-15。", got["IT35"]  # 自分の注記に同じ原文がある
            assert got["IT36"] == "実測でゼロの行(空)。注記は「全期間ゼロ」。", got["IT36"]  # 6字未満の注記は照合しない
            assert got["IT56"] == mk()["screen_layout"]["items"][3]["remarks"], got["IT56"]  # 自分の注記と同じ原文は残す
            assert got["IT40"] == "前の文。", got["IT40"]  # 「注記は、。」だけの文は消す
            assert got["IT99"] == "注記は、％レート1〜5は最終計上 2015-06-15。", got["IT99"]  # 2つの spec にまたがる項目は触らない
            assert [x for x in log if x.startswith("[新1]")] == [
                "[新1] IT31 の備考から文を 1 件取り除く(別の行の注記の写し)",
                "[新1] IT40 の備考から文を 1 件取り除く(別の行の注記の写し)"], log
            # 2回目は何も変えない(冪等)
            snap = json.dumps(out, ensure_ascii=False, sort_keys=True)
            log2 = []
            fill(out, log2)
            assert json.dumps(out, ensure_ascii=False, sort_keys=True) == snap, "2回目で変わった(冪等でない)"
            assert log2 == [], log2
            # 全部の文が外れるときは触らず WARN
            only = {"screen_layout": {"items": [item("IT70", "％１（合計）", "注記は、％レート1〜5は最終計上 2015-06-15。", 49, 49)]}}
            log3 = []
            fill(only, log3)
            assert only["screen_layout"]["items"][0]["remarks"] == "注記は、％レート1〜5は最終計上 2015-06-15。", only
            assert any(x.startswith("WARN") for x in log3), log3
        finally:
            _config_loader.INTERMEDIATE_DIR = old
    print("[selftest OK] _compose_row_notes (新1: 別の行の注記の写しを文ごと取り除く)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_row_notes.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
