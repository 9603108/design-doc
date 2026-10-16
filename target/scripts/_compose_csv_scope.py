"""共通ロジックの「CSV 出力では呼ばない」を、下書きの事実から機械で足す(第20回)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=170(common_logic を書く S1 の断片が合成された後)。
WHY: S1 は「画面だけの整形(3桁区切りなど)」と「CSV でも使う処理」を1つの文に混ぜ、CSV は整形しないのに CSV の値を整形すると読める文を書いた
  (run11 の CL2・run14・run15・run17)。画面側の共通関数が CSV 出力から呼ばれるかは TSX から決まる事実なので、react 下書きの
  logic_candidates[].csv_called(extract_react_draft._attach_csv_called)に持ち、呼ばれない(false)関数の common_logic が CSV に一言も触れていなければ、
  processing_steps の末尾に「画面側だけで使い、CSV 出力では呼ばない」の1手順を足す。

足す条件(推測しない):
  - 共通ロジックの出典(src)が、csv_called が false の候補の出典(定義の行)と同じファイルで行が重なる。
  - 共通ロジックの name・description・expression・processing_steps・used_in・used_by_processes のどこにも「CSV」が無い
    (S1 が自分で書き分けた行・CSV の使用を書いた行は触らない。CSV の整形を肯定で書いた行は、検査 verify_vs_source.check_cl_csv_format が S1 へ戻す)。
  - 同じ手順が既にある(冪等)。
下書き・出典が無いときは何もしない。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す

ORDER = 170

# WHY: 「画面の表示だけ」とは書かない。本日の日付(todayJst)のように、表示でなく画面の既定値に使う関数も csv_called=false になるため
NOTE = "画面側だけで使い、CSV 出力では呼ばない"


def _drafts(directory):
    """react 下書きを全部読む。読めないファイルは無いものとして飛ばす(_compose_mapping と同じ読み方)。"""
    out = []
    for p in sorted(Path(directory).glob("*_react_draft.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _overlap(a, b) -> bool:
    """出典 {file, line_start, line_end} の2つが、同じファイルで行が重なるか。"""
    if not (isinstance(a, dict) and isinstance(b, dict)) or a.get("file") != b.get("file"):
        return False
    if not (isinstance(a.get("line_start"), int) and isinstance(b.get("line_start"), int)):
        return False
    return a["line_start"] <= int(b.get("line_end") or b["line_start"]) and b["line_start"] <= int(a.get("line_end") or a["line_start"])


def _mentions_csv(row) -> bool:
    """共通ロジックの行のどこかに「CSV」の語があるか。"""
    texts = [str(row.get(k) or "") for k in ("name", "description", "expression")]
    texts += [str(s.get("description") or "") for s in row.get("processing_steps") or [] if isinstance(s, dict)]
    texts += [str(x) for k in ("used_in", "used_by_processes") for x in row.get(k) or []]
    return any("CSV" in t for t in texts)


def fill(out, log):
    """csv_called が false の画面側の共通関数に対応する共通ロジックへ、「CSV 出力では呼ばない」の手順を足す。"""
    from _config_loader import INTERMEDIATE_DIR
    cands = [c for d in _drafts(INTERMEDIATE_DIR) for c in d.get("logic_candidates") or []
             if isinstance(c, dict) and c.get("csv_called") is False]
    if not cands:
        return
    for row in out.get("common_logic") or []:
        if not isinstance(row, dict) or _mentions_csv(row):
            continue
        if not any(_overlap(s, cs) for s in row.get("src") or [] for c in cands for cs in c.get("src") or []):
            continue
        steps = row.get("processing_steps")
        if not isinstance(steps, list):
            steps = row["processing_steps"] = []
        steps.append({"no": len(steps) + 1, "kind": "処理", "description": NOTE})
        log.append(f"[CSV] common_logic[{row.get('name')}] に「{NOTE}」を足す(下書きの csv_called=false)")


def _selftest():
    """csv_called が false の関数の共通ロジックだけに手順を足す。CSV に触れる行・true の関数・出典が重ならない行・2回目は触らない。"""
    import copy
    import tempfile
    import _config_loader
    F = "/src/P.tsx"

    def span(a, b):
        return [{"file": F, "line_start": a, "line_end": b}]
    draft = {"logic_candidates": [
        {"name": "fmt", "csv_called": False, "src": span(93, 93)},
        {"name": "num", "csv_called": True, "src": span(92, 92)},
        {"name": "old", "src": span(100, 100)}]}
    rows = [
        {"name": "桁区切り", "expression": "3桁区切りにする", "processing_steps": [{"no": 1, "kind": "処理", "description": "区切る"}], "src": span(93, 93)},
        {"name": "欠損0", "expression": "nullは0", "src": span(92, 92)},
        {"name": "CSVも書いた", "expression": "画面でだけ使い、CSVでは使わない", "src": span(93, 93)},
        {"name": "別のファイル", "expression": "x", "src": [{"file": "/src/Q.tsx", "line_start": 93, "line_end": 93}]},
        {"name": "古い下書き", "expression": "y", "src": span(100, 100)},
        {"name": "手順なし", "expression": "z", "src": span(90, 95)}]
    orig = _config_loader.INTERMEDIATE_DIR
    with tempfile.TemporaryDirectory() as td:
        Path(td, "S_react_draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        try:
            _config_loader.INTERMEDIATE_DIR = Path(td)
            out, log = {"common_logic": copy.deepcopy(rows)}, []
            fill(out, log)
            once = json.dumps(out, ensure_ascii=False, sort_keys=True)
            log2 = []
            fill(out, log2)
            assert json.dumps(out, ensure_ascii=False, sort_keys=True) == once and log2 == [], "2回目で変わった(冪等でない)"
            # 下書きが無い(候補が無い)ときは何もしない
            _config_loader.INTERMEDIATE_DIR = Path(td) / "none"
            empty = {"common_logic": copy.deepcopy(rows)}
            fill(empty, [])
            assert empty == {"common_logic": rows}, "下書きが無いのに変わった"
        finally:
            _config_loader.INTERMEDIATE_DIR = orig
    got = {r["name"]: [s["description"] for s in r.get("processing_steps") or []] for r in out["common_logic"]}
    assert got["桁区切り"] == ["区切る", NOTE] and out["common_logic"][0]["processing_steps"][1]["no"] == 2, got
    assert got["欠損0"] == [] and got["CSVも書いた"] == [] and got["別のファイル"] == [] and got["古い下書き"] == [], got
    assert got["手順なし"] == [NOTE], got  # 出典の範囲(90-95)が定義の行(93)を含む。手順が無い行は新しく作る
    assert len(log) == 2, log
    print("[selftest OK] _compose_csv_scope (csv_called=false の共通関数に CSV で呼ばない手順を足す)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_csv_scope.py --selftest (fill は _units.compose から呼ばれる)")
        sys.exit(2)
