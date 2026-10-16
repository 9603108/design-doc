"""画面項目の出典(src)を react 下書きから機械で付ける(round12 V1・差異 6)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=50 で、src を読む後段
(_compose_response 110・_compose_mapping 120・_compose_row_notes 150・_compose_calc_examples 160)より前に走る。

WHY: 単位 S1 は screen_layout.items の src を、下書きにある行は合成が付けると読んで、62 件中 9 件しか書かなかった(run11)。
  src が無い項目は初期処理の表で活性・表示を位置で結べず、仮埋めの「活性・表示」になる。src は機械抽出の下書きが正なので、
  下書きの事実から付ける。単位に書かせる値ではない。

付け方(上から順に試し、最初に決まったものを使う。決まらない項目は触らない。推測しない):
  1. display_rows[].specs[] を全部連結した並び(下書きの順)で、label が項目名と同じ n 番目の spec の src
  2. jsx_elements を出典の行順に並べ、label か text が項目名と同じ n 番目の src
  3. ui_strings(kind が jsx_text か literal)を出典の行順に並べ、text が項目名か example と同じ n 番目の src
  n = その項目が items の中で同じ項目名の何番目か(「小計」「合計」のように同名の行が複数あるとき、下書きの同名の並びと位置で対にする)。
既に src がある項目は触らない。下書きが無ければ何もしない。
"""
import copy
import json
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
from pathlib import Path

ORDER = 50

_UI_KINDS = ("jsx_text", "literal")


def _drafts(directory):
    """react 下書きを全部読む。読めないファイルは無いものとして飛ばす(_compose_row_notes と同じ読み方)。"""
    from _config_loader import draft_files
    out = []
    for p in draft_files("react", directory):  # 機能名の下書き1本
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _order_key(e):
    """出典の行順。ファイル名と最初の行番号で並べる(下書きが複数あっても、同じ行を同じ位置に置く)。"""
    src = [s for s in e.get("src") or [] if isinstance(s, dict)]  # fallback-ok: 出典の無い行は並びの末尾へ回すための空の一覧
    if not src:
        return ("", 0)
    return (str(src[0].get("file") or ""), src[0].get("line_start") or 0)  # fallback-ok: 並べ替えの鍵だけに使う(出典の欠けは末尾)


def _spec_rows(drafts):
    """display_rows の spec を下書きの順に全部連結する。spec は src が無くても数える(n 番目を下書きと合わせるため)。"""
    rows = []
    for d in drafts:
        for row in d.get("display_rows") or []:  # fallback-ok: 下書きに display_rows の無い画面もある(空として扱う)
            if isinstance(row, dict):
                rows += [sp for sp in row.get("specs") or [] if isinstance(sp, dict)]  # fallback-ok: 行に specs が無ければ空
    return rows


def _sorted_rows(drafts, key, keep=None):
    rows = [e for d in drafts for e in d.get(key) or [] if isinstance(e, dict) and (keep is None or keep(e))]  # fallback-ok: 下書きに欄が無い画面もある(空として扱う)
    return sorted(rows, key=_order_key)


def _same(v, name):
    """字面が項目名と同じか(前後の空白を除いて比べる)。項目名が無い・文字列でないときは一致しない。"""
    return isinstance(v, str) and isinstance(name, str) and v.strip() == name.strip()


def _nth(rows, n, hit):
    """rows のうち hit(e) が真のものの n 番目(1 始まり)。無ければ None。"""
    found = [e for e in rows if hit(e)]
    return found[n - 1] if 0 < n <= len(found) else None


def _decide(name, n, item, specs, jsx, ui):
    """項目1件の出典を決める。戻り: (src のリスト, 決めた規則の名前)。決まらなければ (None, None)。"""
    rules = (
        ("display_rows の spec", _nth(specs, n, lambda e: _same(e.get("label"), name))),
        ("jsx_elements", _nth(jsx, n, lambda e: _same(e.get("label"), name) or _same(e.get("text"), name))),
        ("ui_strings", _nth(ui, n, lambda e: _same(e.get("text"), name) or _same(e.get("text"), item.get("example")))),
    )
    for label, hit in rules:
        # WHY: n 番目が見つかっても出典(src)が空なら決まらない扱いにし、次の規則へ回す(空の出典を書かない)
        if hit and hit.get("src"):
            return hit["src"], label
    return None, None


def fill(out, log):
    """screen_layout.items のうち src が無い項目へ、react 下書きの出典を付ける(V1)。下書き・項目が無ければ何もしない。"""
    from _config_loader import INTERMEDIATE_DIR
    items = [x for x in ((out.get("screen_layout") or {}).get("items") or []) if isinstance(x, dict)]  # fallback-ok: 画面項目を持たない断片の合成では何もしない
    if not items:
        return
    drafts = _drafts(INTERMEDIATE_DIR)
    if not drafts:
        return
    specs = _spec_rows(drafts)
    jsx = _sorted_rows(drafts, "jsx_elements")
    ui = _sorted_rows(drafts, "ui_strings", keep=lambda e: e.get("kind") in _UI_KINDS)
    seen, done, undecided = {}, {}, []
    for it in items:
        name = it.get("item_name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        # WHY: n は既に src がある項目も含めて数える。下書きの同名の並びと位置を合わせるため
        seen[name] = seen.get(name, 0) + 1  # fallback-ok: 初回の項目名は 0 から数える(件数の数え上げ)
        if it.get("src"):
            continue
        src, rule = _decide(name, seen[name], it, specs, jsx, ui)
        if src is None:
            undecided.append(name)
            continue
        it["src"] = copy.deepcopy(src)
        done[rule] = done.get(rule, 0) + 1  # fallback-ok: 規則ごとの件数の初期値 0
    if done:
        detail = "、".join(f"{k} から {v} 件" for k, v in done.items())
        log.append(f"[V1] screen_layout.items の出典(src)を下書きから付けた: 計 {sum(done.values())} 件({detail})")
    if undecided:
        log.append(f"[V1] 下書きで出典が決まらず触らない項目 {len(undecided)} 件: " + "、".join(undecided))


def _selftest():
    import tempfile
    import _config_loader

    F = "/src/PosReport.tsx"

    def sp(label, a, b):
        return {"label": label, "src": [{"file": F, "line_start": a, "line_end": b}]}

    react = {
        "display_rows": [
            {"array": "LEFT", "specs": [sp("小計", 10, 10), sp("総売上", 9, 9), sp("小計", 20, 20), {"label": "欠落", "src": None}]},
        ],
        "jsx_elements": [
            {"tag": "span", "label": "", "text": "空", "src": [{"file": F, "line_start": 132, "line_end": 132}]},
            {"tag": "button", "label": "検索", "text": "", "src": [{"file": F, "line_start": 200, "line_end": 200}]},
        ],
        "ui_strings": [
            {"text": "注記X", "kind": "jsx_text", "src": [{"file": F, "line_start": 300, "line_end": 300}]},
            {"text": "新", "kind": "jsx_text", "src": [{"file": F, "line_start": 133, "line_end": 133}]},
            {"text": "新", "kind": "literal", "src": [{"file": F, "line_start": 140, "line_end": 140}]},
            {"text": "別種", "kind": "other", "src": [{"file": F, "line_start": 150, "line_end": 150}]},
        ],
    }

    def mk():
        return {"screen_layout": {"items": [
            {"item_name": "小計"},                      # 1 番目の小計 → spec の 1 番目(10 行)
            {"item_name": "小計"},                      # 2 番目 → spec の 2 番目(20 行)
            {"item_name": "小計"},                      # 3 番目 → 同名の spec が2つだけなので決まらない
            {"item_name": "空"},                        # spec に無い → jsx の 1 番目(132 行)
            {"item_name": "検索"},                      # jsx の label(200 行)
            {"item_name": "注記X", "example": "注記X"}, # jsx に無い → ui_strings の text(300 行)
            {"item_name": "新"},                        # jsx に無い → ui_strings の 1 番目(133 行。kind が other の行は数えない)
            {"item_name": "欠落"},                      # spec の src が無いので決まらない
            {"item_name": "既存", "src": [{"file": "/keep.tsx", "line_start": 1, "line_end": 2}]},  # 既に src がある
            {"item_name": "総売上", "src": []},          # 空の出典は無いものとして付け直す
            {"item_name": "決まらない"},
        ]}}

    old = _config_loader.INTERMEDIATE_DIR
    with tempfile.TemporaryDirectory() as td:
        import os
        os.environ["FEATURE_NAME"] = "x"  # 下書きは機能名の1本だけ読む(draft_files)。試験の下書きの名前に合わせる
        Path(td, "x_react_draft.json").write_text(json.dumps(react, ensure_ascii=False), encoding="utf-8")
        _config_loader.INTERMEDIATE_DIR = Path(td)
        try:
            out = mk()
            log = []
            fill(out, log)
            got = {}
            for it in out["screen_layout"]["items"]:
                got.setdefault(it["item_name"], []).append(it["src"][0]["line_start"] if it.get("src") else None)  # fallback-ok: 検査用に出典の無い項目を None と数える
            assert got["小計"] == [10, 20, None], got["小計"]  # n 番目の対。3 番目は決まらず触らない
            assert got["空"] == [132], got["空"]
            assert got["検索"] == [200], got["検索"]
            assert got["注記X"] == [300], got["注記X"]
            assert got["新"] == [133], got["新"]  # kind が other の行は数えない
            assert got["欠落"] == [None], got["欠落"]  # 出典の無い spec では付けない
            assert got["既存"] == [1], got["既存"]  # 既存の src は触らない
            assert got["総売上"] == [9], got["総売上"]  # 空の出典は付け直す
            assert got["決まらない"] == [None], got["決まらない"]
            assert any(x.startswith("[V1] screen_layout.items の出典(src)を下書きから付けた: 計 7 件") for x in log), log
            assert any("決まらず触らない項目 3 件" in x and "小計" in x and "欠落" in x and "決まらない" in x for x in log), log
            # 2 回目は何も変えない(冪等)
            snap = json.dumps(out, ensure_ascii=False, sort_keys=True)
            log2 = []
            fill(out, log2)
            assert json.dumps(out, ensure_ascii=False, sort_keys=True) == snap, "2 回目で変わった(冪等でない)"
            assert not any("付けた" in x for x in log2), log2
        finally:
            _config_loader.INTERMEDIATE_DIR = old
    # 下書きが無いときは何もしない(出典は1件も増えない)
    with tempfile.TemporaryDirectory() as td:
        _config_loader.INTERMEDIATE_DIR = Path(td)
        try:
            out = mk()
            fill(out, [])
            assert not any(it.get("src") for it in out["screen_layout"]["items"] if it["item_name"] != "既存"), out
        finally:
            _config_loader.INTERMEDIATE_DIR = old
    print("[selftest OK] _compose_item_src (V1: n 番目の対・既存 src を触らない・決まらない項目を触らない・下書き無しは何もしない)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_item_src.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
