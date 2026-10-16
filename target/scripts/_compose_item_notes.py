"""表の行の注記(マウスで出る文)の原文を、その行の画面項目の備考へ機械で入れる(run17 差2・差3)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=155(別の行の注記の写しを取り除く
  _compose_row_notes=150 の後・例示値の計算 160 の前)。検査 _vs_map.check_row_note_in_remarks も、この module の
  noted_items・contains を呼ぶ(合成と検査で「どの項目がどの行の注記を持つか」の決め方を1つにするため)。

WHY: 画面の表の行は、注記(react 下書きの display_rows の spec の note。title 属性でマウスを乗せると出る)を持つことがある。
  備考に注記の字面を書くかどうかを S1 に任せると、毎回揺れた。
  run17 は 8 行のうち 4 行(IT35 内税額・IT50 支払科目に無い額・IT53 現金過不足・IT57 客単価)で原文が備考から消えるか、言い換えられた。
  IT35 は原文「画面上は括弧付きの参考値だが、…」が「括弧付きの参考値として表示されるが、…」になり、
  「この画面で括弧付きで出る」と読める(この画面は括弧を付けずに出す)。注記の原文は下書きが事実として持つので、
  書かせずに合成が入れる。モデルは、注記以外の備考(既定の表示・計算式など)に専念する。

入れ方(項目ごと。決まらない項目は触らない。推測しない):
  1. 項目が、注記を持つ spec の行に属する(noted_items)。属し方は src の行範囲が1つの spec に収まること。
     収まらないときは、項目名と同じ label の spec が1つだけで、src が重なる(src が無い項目は label だけ)こと。
  2. 備考に注記の字面(contains。空白・句読点・括弧を除き、全角半角と横棒の種類をそろえて比べる)が含まれれば何もしない。
  3. 含まれなければ、備考の文のうち注記を言い換えたもの(注記と、正規化後に 8 字以上の連続する共通部分を持つ文)を取り除き、
     末尾に「注記: <原文>」を足す。言い換えを残して原文を足すと、備考に注記が2通り並ぶので、言い換えは取り除く。
     取り除くのは、注記の字面に近い文だけ。注記と関係の無い文(計算式・既定の表示など)は残す。
冪等: 2回目は注記が含まれているので何も変えない。下書き・画面項目が無ければ何もしない。
"""
import difflib
import json
import re
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
import unicodedata
from pathlib import Path

ORDER = 155

# WHY: 比べるときだけ、全角半角(NFKC)・横棒の種類・空白・句読点・括弧をそろえる。S1 は「（」を「(」に、「−」を「-」に書き替えることがある
_DASHES = re.compile(r"[−–—‐‑‒―]")
_IGNORE = re.compile(r"[\s、。，,．.・:：;；!！?？()（）「」『』\[\]]")
# WHY: 言い換えの文を見分ける連続一致の字数。注記と 8 字続けて同じ語は、同じ内容を書いた文と見てよい
# (run15〜17 の実データで、注記と無関係の文との共通部分は最大 4 字だった)
_PARA_MIN = 8
_PREFIX = "注記: "


def norm(s) -> str:
    """字面を比べる形にする。呼出元: contains・_is_paraphrase。"""
    t = unicodedata.normalize("NFKC", str(s or ""))
    return _IGNORE.sub("", _DASHES.sub("-", t))


def contains(remarks, note) -> bool:
    """備考に注記の字面が(正規化して)含まれるか。注記が空なら含まれるとみなす(足す物が無い)。呼出元: fill・_vs_map.check_row_note_in_remarks。"""
    n = norm(note)
    return not n or n in norm(remarks)


def _drafts(directory):
    """react 下書きを全部読む。読めないファイルは無いものとして飛ばす(_compose_row_notes と同じ読み方)。"""
    out = []
    for p in sorted(Path(directory).glob("*_react_draft.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def all_specs(drafts):
    """下書きの display_rows の spec を全部並べる(src の無いものも数える。label の一意を決めるため)。呼出元: fill・_vs_map。"""
    return [sp for d in drafts for row in d.get("display_rows") or [] if isinstance(row, dict)
            for sp in row.get("specs") or [] if isinstance(sp, dict)]


def _span(s):
    """出典1件の (file, 開始行, 終了行)。行が欠けていれば None。"""
    a = s.get("line_start")
    b = s.get("line_end") or a
    return (s.get("file"), a, b) if a is not None and b is not None else None


def _within(outer, inner) -> bool:
    """出典 outer の行範囲が、出典 inner を含むか(同じファイル)。"""
    o, i = _span(outer), _span(inner)
    return bool(o and i and o[0] == i[0] and o[1] <= i[1] and i[2] <= o[2])


def _overlap(a, b) -> bool:
    """出典 a・b の行範囲が重なるか(同じファイル)。"""
    x, y = _span(a), _span(b)
    return bool(x and y and x[0] == y[0] and x[1] <= y[2] and y[1] <= x[2])


def own_spec(item, specs):
    """画面項目が属する表の行(spec)。決まらなければ None。
    (1) 項目の src の全部が、同じ1つの spec の src に収まる。(2) (1)で決まらないとき、項目名と同じ label の spec が1つだけで、
    項目に src が無いか src が重なる。WHY: 2つ以上の spec にまたがる項目は、どの行の注記か決まらないので触らない。
    項目 src が少し広い(spec の行より前後に出る)ときだけ、名前で決める。呼出元: noted_items。"""
    srcs = [r for r in item.get("src") or [] if isinstance(r, dict) and r.get("file")]
    if srcs:
        hit = [sp for sp in specs if sp.get("src") and all(any(_within(s, r) for s in sp["src"]) for r in srcs)]
        if len(hit) == 1:
            return hit[0]
    same = [sp for sp in specs if sp.get("label") and sp.get("label") == item.get("item_name")]
    if len(same) == 1 and (not srcs or any(_overlap(s, r) for s in same[0].get("src") or [] for r in srcs)):
        return same[0]
    return None


def noted_items(items, specs):
    """注記を持つ行に属する画面項目を (項目, spec, 注記の原文) で返す。呼出元: fill・_vs_map.check_row_note_in_remarks。"""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sp = own_spec(it, specs)
        note = str((sp or {}).get("note") or "").strip()
        if sp and note:
            out.append((it, sp, note))
    return out


def _is_paraphrase(sentence, note_n) -> bool:
    """文が注記の言い換えか。正規化後に注記と _PARA_MIN 字以上の連続する共通部分を持つ。呼出元: fill。"""
    s = norm(sentence)
    if len(s) < _PARA_MIN or len(note_n) < _PARA_MIN:
        return False
    m = difflib.SequenceMatcher(None, s, note_n, autojunk=False).find_longest_match(0, len(s), 0, len(note_n))
    return m.size >= _PARA_MIN


def _with_note(remarks, note):
    """備考から注記の言い換えの文を取り除き、末尾に「注記: 原文」を足す。戻り: (新しい備考, 取り除いた文の数)。呼出元: fill。"""
    text = str(remarks or "")
    note_n = norm(note)
    kept, dropped = [], 0
    for seg in re.split(r"(?<=。)", text):
        if not seg.strip():
            continue
        if _is_paraphrase(seg, note_n):
            dropped += 1
            continue
        kept.append(seg)
    body = "".join(kept).strip()
    # WHY: 備考の文末の書き方(「。」で終える・終えない)は S1 の書き方に合わせる。run15 は付けず run16 は付けた。足す文だけ違うと読みにくい
    closed = text.rstrip().endswith("。")
    if body and not body.endswith("。"):
        body += "。"
    new = f"{body}{_PREFIX}{note.strip()}"
    if closed and not new.endswith("。"):
        new += "。"
    return new, dropped


def fill(out, log):
    """備考に注記の原文が無い項目へ、「注記: 原文」を足す(言い換えの文は取り除く)。下書き・画面項目が無ければ何もしない。"""
    from _config_loader import INTERMEDIATE_DIR
    items = [x for x in ((out.get("screen_layout") or {}).get("items") or []) if isinstance(x, dict)]
    drafts = _drafts(INTERMEDIATE_DIR)
    specs = all_specs(drafts)
    if not items or not specs:
        return
    added, replaced = [], []
    for it, _sp, note in noted_items(items, specs):
        if contains(it.get("remarks"), note):
            continue
        it["remarks"], dropped = _with_note(it.get("remarks"), note)
        no = it.get("no") or it.get("item_name")
        (replaced if dropped else added).append(f"{no}")
    if added or replaced:
        log.append(f"[新2] 表の行の注記の原文を備考へ足した: 計 {len(added) + len(replaced)} 件"
                   f"(言い換えの文を取り除いて原文に置き換えた {len(replaced)} 件: {'、'.join(replaced) or 'なし'}。足しただけ {len(added)} 件: {'、'.join(added) or 'なし'})")


def _selftest():
    import tempfile
    import _config_loader

    F = "/src/PosReport.tsx"

    def sp(label, a, b, note=None):
        return {"label": label, "note": note, "src": [{"file": F, "line_start": a, "line_end": b}]}

    n_tax = "画面上は括弧付きの参考値だが、実データでは 小計（税込）= 小計（税抜）+ この額 が全行で成立する（＝加算されている）"
    n_sum = "税込売上 − 支払科目合計。2025-03-01 以降どの店でも発生し、2026年7月は売上の約20%（§6.5）"
    n_zero = "全期間ゼロ"
    rows = [{"array": "LEFT", "specs": [sp("特売値引", 55, 55, n_zero), sp("内税額", 57, 58, n_tax), sp("小計", 44, 44), sp("小計", 69, 69)]},
            {"array": "RIGHT", "specs": [sp("支払科目に無い額", 76, 77, n_sum), sp("現金過不足", 80, 81, "現金在高・回収高は…ゼロのため常に0")]}]

    def item(no, name, rem, a, b):
        return {"no": no, "item_name": name, "remarks": rem, "src": [{"file": F, "line_start": a, "line_end": b}] if a else []}

    def mk():
        return {"screen_layout": {"items": [
            # 言い換え(run17 IT35): 注記を言い換えた文を原文に置き換える
            item("IT35", "内税額", "括弧付きの参考値として表示されるが、実データでは小計（税込売上金額）が小計（値引後 税抜売上金額）と内税額の合計に一致する", 57, 58),
            # 注記が消えた(run17 IT50): 他の文は残し、末尾に原文を足す
            item("IT50", "支払科目に無い額", "小計（税込売上金額）から右の表の合計を引いた差額。現行に無い追加行。", 76, 77),
            # 原文が(記号の揺れつきで)含まれている: 触らない
            item("IT33", "特売値引", "左の表の行。注記は「全期間ゼロ」", 55, 55),
            # 全角半角・横棒の揺れがあっても含まれているとみなす
            item("IT51", "支払科目に無い額", "計算式の説明。注記: 税込売上 - 支払科目合計。2025-03-01 以降どの店でも発生し、2026年7月は売上の約20%(§6.5)", 76, 77),
            # 同名の行(小計)は注記を持たない: 触らない
            item("IT23", "小計", "値引前の小計", 44, 44),
            # src が行を少し広く取っている項目は、名前が一意なら決まる
            item("IT53", "現金過不足", "現金在高から回収高を引いた額", 79, 82),
            # 備考が空の項目: 注記だけを入れる
            item("IT34", "内税額", "", 57, 58),
            # src が無い項目も、項目名が一意なら決まる
            item("IT36", "特売値引", None, None, None),
            # 2つの行にまたがる項目は決まらない: 触らない
            item("IT99", "ふたつの行", "備考", 55, 58),
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
            assert got["IT35"] == "注記: " + n_tax, got["IT35"]  # 言い換えの文は取り除かれ、原文だけが残る
            assert got["IT50"] == "小計（税込売上金額）から右の表の合計を引いた差額。現行に無い追加行。注記: " + n_sum + "。", got["IT50"]  # 他の文は残る。S1 が「。」で終えていたので足した文も「。」で終える
            assert got["IT33"] == mk()["screen_layout"]["items"][2]["remarks"], got["IT33"]
            assert got["IT51"] == mk()["screen_layout"]["items"][3]["remarks"], got["IT51"]
            assert got["IT23"] == "値引前の小計", got["IT23"]
            assert got["IT53"].startswith("現金在高から回収高を引いた額。注記: 現金在高・回収高は"), got["IT53"]  # 名前で決まる。他の文は残る(「。」の無い備考に足すので句点を補う)
            assert got["IT34"] == "注記: " + n_tax, got["IT34"]
            assert got["IT36"] == "注記: " + n_zero, got["IT36"]
            assert got["IT99"] == "備考", got["IT99"]
            assert len(log) == 1 and log[0].startswith("[新2]") and "計 5 件" in log[0] and "1 件: IT35" in log[0], log
            # 2回目は何も変えない(冪等)
            snap = json.dumps(out, ensure_ascii=False, sort_keys=True)
            log2 = []
            fill(out, log2)
            assert json.dumps(out, ensure_ascii=False, sort_keys=True) == snap, "2回目で変わった(冪等でない)"
            assert log2 == [], log2
            # 下書きの display_rows が無ければ何もしない
            Path(td, "x_react_draft.json").write_text("{}", encoding="utf-8")
            out2 = mk()
            fill(out2, [])
            assert out2 == mk(), "下書きに行が無いのに変えた"
        finally:
            _config_loader.INTERMEDIATE_DIR = old
    # 注記と関係の無い文は、短い共通語(4字)があっても取り除かない
    assert not _is_paraphrase("小計（税込売上金額）を客数で割り、円未満を切り捨てた額", norm("現行と同じ 税込売上÷客数。POSが日次で送ってくる客単価は 支払科目合計÷客数 で別物（§6.3）"))
    assert _is_paraphrase("現行と同じ税込売上÷客数で、POSが日次で送ってくる客単価とは別物", norm("現行と同じ 税込売上÷客数。POSが日次で送ってくる客単価は 支払科目合計÷客数 で別物（§6.3）"))
    assert contains("a。注記: ％レート1〜5は最終計上 2015-06-15。", "％レート1〜5は最終計上 2015-06-15") and not contains("", "全期間ゼロ") and contains("", "")
    print("[selftest OK] _compose_item_notes (新2: 注記の原文を備考へ足す・言い換えは原文に置き換える・属する行が決まらない項目は触らない・冪等)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_item_notes.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
