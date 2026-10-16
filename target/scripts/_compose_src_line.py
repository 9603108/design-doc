"""api_spec の src(ソースの行)の1行ずれを、行の本文で確かめて直す(run20 差4)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=20(_compose_db_facts の10の後、他の _compose_*.py より先)。
WHY: H3 が書いた api_spec・request_params の src に、0始まりの行番号が混じる(Serena の read_file は0始まりで返すため)。
  run20 では handler.py の登録行(正 134・133)が 133・130-132、queries_pos.py の shop_id(正 100)が 99、group(正 101-103)が 100-102 になった。
  下書きにある行の src は合成が付ける(_units._fill_src)が、行が既に src を持つと触らないので、H3 の誤りがそのまま残る。
  行番号の「0始まりか1始まりか」は1件ずつ違う(同じ行の中で、dispatch の 1006-1055 は正しく、登録行はずれる)。行単位では決められないので、
  指す名前(api_spec は action 名、request_params は param 名)がその行の本文に出るかで、1件ずつ確かめる。

直し方(推測しない。確かめられないものは触らない):
  api_spec の行(関数・登録行など、名前が範囲の途中に出てよい出典):
    名前が範囲の本文に出る → そのまま。出ないが +1 行ずらした範囲に出る → +1 ずらし、名前が最初に出る行まで先頭を詰める(直前の注記を外す)。
    どちらにも出ない → そのまま(行を指さない出典。dispatch 関数の全体など)。
  request_params の行(param を読む文が先頭になる出典):
    先頭の行に名前が出る → そのまま。+1 行の先頭に出る → +1 ずらす。範囲の途中に出る → 先頭を詰める。+1 範囲の途中に出る → ずらして詰める。どれも無ければそのまま。
  api_spec の行は、下書き(*_backend_draft.json)の同じ action の src と「+1 するとちょうど一致」するとき、下書きの src へ置き換える
    (関数の範囲は名前が範囲の先頭に出るので、本文の照合では ずれが分からない。下書きは AST の行で正)。
冪等(直した後は名前が先頭に出る・下書きと一致するので、2回目は何も変えない)。ファイルが読めない・行が無い出典は触らない。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
import _vs_common

ORDER = 20


def _lines(path, cache):
    """ソースの行の list(読めなければ None)。同じファイルは1度だけ読む。"""
    if path not in cache:
        try:
            cache[path] = Path(path).read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError):
            cache[path] = None
    return cache[path]


def _first(lines, a, b, rx):
    """1始まりの行 a〜b(両端を含む)のうち、rx に合う最初の行。無い・範囲がファイルの外なら None。"""
    if lines is None or a < 1 or b > len(lines) or a > b:
        return None
    return next((i for i in range(a, b + 1) if rx.search(lines[i - 1])), None)


def _span(s):
    """出典 s の (開始行, 終了行)。行が整数でなければ None(触らない)。"""
    a = s.get("line_start")
    b = s.get("line_end") or a  # fallback-ok: 終了行が無い出典は開始行だけの1行
    return (a, b) if isinstance(a, int) and isinstance(b, int) else None


def _fix_api(s, rx, cache):
    """api_spec の行の出典を直した新しい dict を返す(直さないなら同じもの)。"""
    sp = _span(s)
    lines = _lines(s.get("file"), cache) if sp else None
    if not sp or lines is None or _first(lines, sp[0], sp[1], rx):
        return s
    f = _first(lines, sp[0] + 1, sp[1] + 1, rx)
    return s if f is None else {**s, "line_start": f, "line_end": sp[1] + 1}


def _fix_param(s, rx, cache):
    """request_params の行の出典を直した新しい dict を返す(直さないなら同じもの)。"""
    sp = _span(s)
    lines = _lines(s.get("file"), cache) if sp else None
    if not sp or lines is None:
        return s
    a, b = sp
    if _first(lines, a, a, rx):
        return s
    if _first(lines, a + 1, a + 1, rx):
        return {**s, "line_start": a + 1, "line_end": b + 1}
    f = _first(lines, a, b, rx)
    if f is not None:
        return {**s, "line_start": f}
    f = _first(lines, a + 1, b + 1, rx)
    return s if f is None else {**s, "line_start": f, "line_end": b + 1}


def _draft_src(directory):
    """下書きの {action: src の配列}。読めないファイルは無いものとして飛ばす。"""
    out = {}
    for p in sorted(Path(directory).glob("*_backend_draft.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for act, a in (d.get("apis") or {}).items():  # fallback-ok: 下書きの apis は任意
            if isinstance(a, dict) and a.get("src"):
                out[act] = [s for s in a["src"] if isinstance(s, dict)]
    return out


def _draft_field_src(directory):
    """下書きの {action: {response_fields の path: src の配列}}(契約 i3.k1)。src を持つ欄だけ。読めないファイルは飛ばす。
    WHY: response_items の src(出典の行)は backend と frontend のどちらを指すかが run ごとに揺れたため、SQL の出典を持つ下書きの値に固定する。
    接続: fill が response_items の key と path を突き合わせて src を置き換える。"""
    out = {}
    for p in sorted(Path(directory).glob("*_backend_draft.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for act, a in (d.get("apis") or {}).items():  # fallback-ok: 下書きの apis は任意
            if isinstance(a, dict):
                out[act] = {f["path"]: [s for s in f["src"] if isinstance(s, dict)] for f in a.get("response_fields") or []
                            if isinstance(f, dict) and f.get("path") and isinstance(f.get("src"), list) and f["src"]}
    return out


def _by_draft(s, drafts):
    """s が下書きの src の1件と「+1 するとちょうど一致」するなら、その下書きの src。違えば s。
    WHY: 関数の範囲は名前が範囲の先頭の行に出るので、0始まりでずれても本文の照合(_fix_api)では分からない。下書きの行(AST)が正。"""
    sp = _span(s)
    if not sp:
        return s
    for d in drafts:
        dp = _span(d)
        if dp and d.get("file") == s.get("file") and dp == (sp[0] + 1, sp[1] + 1):
            return d
    return s


def _uniq(srcs):
    out = []
    for s in srcs:
        if s not in out:
            out.append(s)
    return out


def _fix_list(srcs, fix, label, log):
    """src の配列の各要素を fix で直し、直したものを log に書く。直した結果が同じ出典になった要素は1つにまとめる。"""
    fixed = [fix(s) if isinstance(s, dict) else s for s in srcs]
    for a, b in zip(srcs, fixed):  # WHY: 重複を消す前に対で比べる(消した後は要素の位置が対応しない)
        if a != b:
            log.append(f"[出典] {label} の src {_vs_common.span(a)} を "
                       f"{_vs_common.lines(b.get('line_start'), b.get('line_end'))} に直す(指す名前が本文に出る行へ。0始まりの混同)")
    return _uniq(fixed)


def fill(out, log):
    """api_spec の行と request_params の行の src の1行ずれを直す。下書き・ソースが読めなければ何もしない。"""
    from _config_loader import INTERMEDIATE_DIR
    drafts, cache, fsrc = _draft_src(INTERMEDIATE_DIR), {}, _draft_field_src(INTERMEDIATE_DIR)
    for api in out.get("api_spec") or []:  # fallback-ok: api_spec は任意
        if not isinstance(api, dict) or not api.get("action"):
            continue
        act = api["action"]
        rx_api = re.compile(r"\b" + re.escape(act) + r"\b")
        if isinstance(api.get("src"), list):
            api["src"] = _fix_list(api["src"], lambda s: _fix_api(_by_draft(s, drafts.get(act, [])), rx_api, cache), f"api_spec[{act}]", log)
        for p in api.get("request_params") or []:  # fallback-ok: request_params は任意
            if not isinstance(p, dict) or not p.get("name") or not isinstance(p.get("src"), list):
                continue
            n = re.escape(str(p["name"]))
            rx_p = re.compile(r"[\"']" + n + r"[\"']|\b" + n + r"\??\s*[:=]")
            p["src"] = _fix_list(p["src"], lambda s: _fix_param(s, rx_p, cache), f"api_spec[{act}].request_params[{p['name']}]", log)
        # G20: response_items の src は下書きの SQL の出典(契約 i3.k1)に固定する。下書きに src が無い欄は触らない
        for x in api.get("response_items") or []:  # fallback-ok: response_items は任意
            new = fsrc.get(act, {}).get(x.get("key")) if isinstance(x, dict) else None
            if new and x.get("src") != new:
                log.append(f"[出典] api_spec[{act}].response_items[{x['key']}] の src を下書きの SQL の出典 {[_vs_common.span(s) for s in new]} に直す")
                x["src"] = new


def _selftest():
    """0始まりの行を、指す名前が出る行へ直す。(1) 登録行 +1・直前の注記を外す・正しい行は触らない・名前が出ない範囲(dispatch 全体)は触らない
    (2) param: 先頭に出る・+1 の先頭・範囲の途中・+1 範囲の途中 (3) 下書きの src と +1 で一致する関数の範囲は下書きへ
    (4) 直した src の本文に action 名・param 名が出る (5) 2回目は何も変えない (6) 読めないファイル・下書き無しは何もしない。"""
    import copy
    import tempfile
    import _config_loader
    # G20: response_items の src は、下書きの response_fields に src がある欄だけ置き換わる。src の無い欄と、response_items の src 以外の欄(display_name 等)は残る
    with tempfile.TemporaryDirectory() as gd:
        gs = {"file": "q.py", "line_start": 3, "line_end": 9}
        Path(gd, "G_backend_draft.json").write_text(json.dumps({"apis": {"ga": {"response_fields": [
            {"path": "rows[].t", "src": [gs]}, {"path": "rows[].u"}]}}}), encoding="utf-8")
        assert _draft_field_src(gd) == {"ga": {"rows[].t": [gs]}}, _draft_field_src(gd)
        orig_g = _config_loader.INTERMEDIATE_DIR
        try:
            _config_loader.INTERMEDIATE_DIR = Path(gd)
            gout = {"api_spec": [{"action": "ga", "response_items": [
                {"key": "rows[].t", "display_name": "合計", "src": [{"file": "app.tsx", "line_start": 1, "line_end": 1}]},
                {"key": "rows[].u", "src": [{"file": "app.tsx", "line_start": 2, "line_end": 2}]}]}]}
            glog = []
            fill(gout, glog)
        finally:
            _config_loader.INTERMEDIATE_DIR = orig_g
        gt, gu = gout["api_spec"][0]["response_items"]
        assert gt["src"] == [gs] and gt["display_name"] == "合計" and len(glog) == 1, (gt, glog)
        assert gu["src"] == [{"file": "app.tsx", "line_start": 2, "line_end": 2}], gu
    with tempfile.TemporaryDirectory() as td:
        h = Path(td, "handler.py")
        h.write_text('ACTIONS = {\n    "list_kinds": f,\n    # 注記1\n    # 注記2\n    "list_a": g,\n    "list_b": h,\n}\n'
                     "def dispatch(body):\n    return ACTIONS\n", encoding="utf-8")
        q = Path(td, "queries.py")
        q.write_text('def f(params):\n    a = req(params, "from")\n    b = req(params, "to")\n    sid = opt(params, "shop_id")\n'
                     '    group = params.get("group") or "register"\n    if group not in G:\n        raise V("bad")\n', encoding="utf-8")
        hf, qf = str(h), str(q)

        def sp(f, a, b=None):
            return {"file": f, "line_start": a, "line_end": b or a}
        draft = {"apis": {"list_a": {"src": [sp(qf, 1, 7)]}}}
        Path(td, "S_backend_draft.json").write_text(json.dumps(draft), encoding="utf-8")
        rows = [
            {"action": "list_b", "src": [sp(hf, 5), sp(hf, 8, 9)], "request_params": []},        # 5(0始まり)→6。dispatch(8-9)は名前が無いので触らない
            {"action": "list_a", "src": [sp(hf, 3, 4), sp(qf, 0, 6)],                            # 3-4(0始まりの 4-5。注記を含む)→5。0-6 は下書きの 1-7 へ
             "request_params": [
                 {"name": "from", "src": [sp(qf, 1)]},       # 0始まり → 2
                 {"name": "to", "src": [sp(qf, 1, 2)]},      # 0始まりの 2-3(from の行を含む)→ +1 範囲 2-3 の途中 → 3
                 {"name": "shop_id", "src": [sp(qf, 3)]},    # → 4
                 {"name": "group", "src": [sp(qf, 4, 6)]},   # → 5-7(先頭に group)
                 {"name": "ok", "src": [sp(qf, 2)]}]},       # 名前がどこにも出ない → 触らない
            {"action": "list_x", "src": [sp(hf, 2)], "request_params": [{"name": "from", "src": [sp(qf, 2)]}]},   # 正しい行は触らない(from は先頭に出る)
        ]
        orig = _config_loader.INTERMEDIATE_DIR
        try:
            _config_loader.INTERMEDIATE_DIR = Path(td)
            out, log = {"api_spec": copy.deepcopy(rows)}, []
            fill(out, log)
            once = json.dumps(out, sort_keys=True)
            log2 = []
            fill(out, log2)
            assert json.dumps(out, sort_keys=True) == once and log2 == [], "2回目で変わった(冪等でない)"
            # (6) 下書きが無い・読めないファイル
            _config_loader.INTERMEDIATE_DIR = Path(td) / "none"
            keep = {"api_spec": [{"action": "list_q", "src": [sp(str(Path(td, "none.py")), 3)], "request_params": []}]}
            before = copy.deepcopy(keep)
            fill(keep, [])
            assert keep == before, keep
        finally:
            _config_loader.INTERMEDIATE_DIR = orig
        b, a_, x = out["api_spec"]
        assert b["src"] == [sp(hf, 6), sp(hf, 8, 9)], b["src"]
        assert a_["src"] == [sp(hf, 5), sp(qf, 1, 7)], a_["src"]
        got = {p["name"]: p["src"] for p in a_["request_params"]}
        assert got["from"] == [sp(qf, 2)] and got["to"] == [sp(qf, 3)] and got["shop_id"] == [sp(qf, 4)] and got["group"] == [sp(qf, 5, 7)] and got["ok"] == [sp(qf, 2)], got
        assert x["src"] == [sp(hf, 2)] and x["request_params"][0]["src"] == [sp(qf, 2)], x
        assert len(log) == 7, log
        # (4) 直した src の先頭の行の本文に、action 名・param 名が出る
        hl, ql = h.read_text(encoding="utf-8").split("\n"), q.read_text(encoding="utf-8").split("\n")
        assert "list_b" in hl[b["src"][0]["line_start"] - 1] and "list_a" in hl[a_["src"][0]["line_start"] - 1]
        for name in ("from", "to", "shop_id", "group"):
            assert name in ql[got[name][0]["line_start"] - 1], name
    print("[selftest OK] _compose_src_line (0始まりの src を、指す名前が出る行へ直す)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_src_line.py --selftest (fill は _units.compose から呼ばれる)")
        sys.exit(2)
