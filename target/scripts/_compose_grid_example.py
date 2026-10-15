"""表(Grid)の画面項目の例示値の行数を、react 下書きの display_rows の行数から作る(第19回)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=140(画面項目の src を付ける 50 の後、備考 150 の前)。
WHY(run13〜run16): S1(モデル)が表の例示値を「22行(空欄項目を除くと14行)」のように、全行数を先に書いて書いた。画面は
  `showDead || !s.dead` で行を描くので、既定(空欄項目の表示がオフ)で出る行数は dead を除いた数(14行)で、全行数(22行)はオンのとき。
  検査 verify_vs_source.check_row_count_example は「空欄項目を除く」と書きながら全行数を使う文を ERROR にするので、毎回 S1 へ差し戻された。
  行数は TSX の定数配列の事実(下書き display_rows の rows_total・rows_shown)で決まるので、S1 に数えさせず合成が例示値に入れる。
対象の項目: item_type が Grid で、出典(src)の行の中に display_rows の配列名(render(LEFT) の LEFT)が識別子として出る項目。
  配列を1つの Grid 項目だけが描くときだけ作る(2つ以上・0なら何もしない。推測で結ばない)。
文面: dead があれば「<既定の行数>行(空欄項目を表示すると<全行数>行)」、無ければ「<行数>行」。「除く」は使わない(上の検査の対象になる語)。
"""
import json
import re
import sys
from pathlib import Path

ORDER = 140


def _drafts(directory, suffix):
    """<機能名>_<suffix>.json を全部読む。読めないファイルは無いものとして飛ばす(_compose_error_example と同じ読み方)。"""
    out = []
    for p in sorted(Path(directory).glob(f"*_{suffix}.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _src_text(item):
    """項目の出典(src)の行の範囲の TSX の文字列を連結して返す。読めないファイル・行の無い出典は飛ばす。"""
    parts = []
    for s in item.get("src") or []:
        if not isinstance(s, dict) or not s.get("file") or s.get("line_start") is None:
            continue
        try:
            lines = Path(s["file"]).read_text(encoding="utf-8").split("\n")
        except OSError:
            continue
        lo, hi = int(s["line_start"]), int(s.get("line_end") or s["line_start"])
        parts.append("\n".join(lines[max(lo - 1, 0):hi]))
    return "\n".join(parts)


def example_text(total, shown):
    """表の例示値。既定で出る行数 shown と、空欄項目を表示したときの全行数 total から作る。"""
    if total == shown:
        return f"{shown}行"
    return f"{shown}行(空欄項目を表示すると{total}行)"


def fill(out, log):
    """対象の Grid 項目の example を、display_rows の行数から作った文字列にする(違うときだけ)。決まらなければ何もしない。"""
    from _config_loader import INTERMEDIATE_DIR
    items = [it for it in ((out.get("screen_layout") or {}).get("items") or []) if isinstance(it, dict) and it.get("item_type") == "Grid"]
    if not items:
        return
    texts = {id(it): _src_text(it) for it in items}
    for react in _drafts(INTERMEDIATE_DIR, "react_draft"):
        for dr in react.get("display_rows") or []:
            arr, total, shown = dr.get("array"), dr.get("rows_total"), dr.get("rows_shown")
            if not arr or not isinstance(total, int) or not isinstance(shown, int) or shown <= 0 or shown > total:
                continue
            rx = re.compile(r"(?<![\w$])" + re.escape(arr) + r"(?![\w$])")
            hit = [it for it in items if rx.search(texts[id(it)])]
            if len(hit) != 1:
                continue
            ex = example_text(total, shown)
            if hit[0].get("example") != ex:
                log.append(f"[例示値] {hit[0].get('no') or hit[0].get('item_name')} の example を display_rows[{arr}] の行数で直す: {hit[0].get('example')!r} -> {ex!r}")
                hit[0]["example"] = ex


def _selftest():
    import tempfile
    import _config_loader

    tsx = (
        "export default function Card() {\n"                    # 1
        "  return (\n"                                          # 2
        "    <div>\n"                                           # 3
        "      <table>\n"                                       # 4
        "        <tbody>{render(LEFT)}</tbody>\n"               # 5
        "      </table>\n"                                      # 6
        "      <table>\n"                                       # 7
        "        <tbody>{render(RIGHT)}</tbody>\n"              # 8
        "      </table>\n"                                      # 9
        "      <table>\n"                                       # 10
        "        <tbody>{render(LEFTOVER)}</tbody>\n"           # 11
        "      </table>\n"                                      # 12
        "    </div>\n"
        "  );\n"
        "}\n"
    )
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td, "Card.tsx")
        tp.write_text(tsx, encoding="utf-8")
        F = str(tp)

        def src(a, b):
            return [{"file": F, "line_start": a, "line_end": b}]

        def mk():
            return {"screen_layout": {"items": [
                {"no": "IT1", "item_name": "左段", "item_type": "Grid", "example": "22行(空欄項目を除くと14行)", "src": src(4, 6)},
                {"no": "IT2", "item_name": "右段", "item_type": "Grid", "example": "17行", "src": src(7, 9)},
                {"no": "IT3", "item_name": "題", "item_type": "Label", "example": "タイトル", "src": src(5, 5)}]}}
        react = {"display_rows": [
            {"array": "LEFT", "rows_total": 22, "rows_shown": 14},
            {"array": "RIGHT", "rows_total": 14, "rows_shown": 14},
            {"array": "NONE", "rows_total": 3, "rows_shown": 2}]}

        def run(react_d, out):
            old = _config_loader.INTERMEDIATE_DIR
            Path(td, "x_react_draft.json").write_text(json.dumps(react_d, ensure_ascii=False), encoding="utf-8")
            _config_loader.INTERMEDIATE_DIR = Path(td)
            try:
                log = []
                fill(out, log)
                return log
            finally:
                _config_loader.INTERMEDIATE_DIR = old

        out = mk()
        log = run(react, out)
        ex = {i["no"]: i["example"] for i in out["screen_layout"]["items"]}
        # 全行数を先に書いた従来の誤りが、既定の行数に直る。dead が無い表は「N行」。Grid でない項目・配列を描かない項目は触らない
        assert ex["IT1"] == "14行(空欄項目を表示すると22行)" and ex["IT2"] == "14行" and ex["IT3"] == "タイトル", ex
        assert len(log) == 2 and log[0].startswith("[例示値] IT1"), log
        assert "除く" not in ex["IT1"], "検査 check_row_count_example の対象語を使った"
        assert run(react, out) == [], "冪等でない"
        # 配列名が別の識別子の一部(LEFTOVER の中の LEFT)では結ばない。2つの Grid が同じ配列を描くとき・行数が決まらないときは触らない
        out = mk()
        out["screen_layout"]["items"][0]["src"] = src(10, 12)  # LEFTOVER だけを描く。LEFT は別の識別子の一部としては結ばない
        out["screen_layout"]["items"][1]["src"] = src(10, 12)
        before = json.dumps(out, ensure_ascii=False)
        assert run({"display_rows": [{"array": "LEFT", "rows_total": 5, "rows_shown": 4}]}, out) == [] and json.dumps(out, ensure_ascii=False) == before, out
        out = mk()
        assert run({"display_rows": [{"array": "LEFT", "rows_total": 22}]}, out) == [] and out == mk(), "行数が無い下書きを触った"
        out = mk()
        out["screen_layout"]["items"][1]["src"] = src(4, 6)  # IT1 と IT2 が同じ LEFT を描く
        assert run({"display_rows": [{"array": "LEFT", "rows_total": 22, "rows_shown": 14}]}, out) == [], "2つの Grid が同じ配列のとき結んだ"
    print("[selftest OK] _compose_grid_example (表の例示値を display_rows の既定の行数と全行数から作る。除く を使わない・曖昧な結びは触らない・冪等)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_grid_example.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
