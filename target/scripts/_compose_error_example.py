"""取得エラーの表示項目の例示値を、ソースの事実から作る(round13 W8・新2)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=130(応答の写し 110・写し 120 の後、備考 150 の前)。
WHY: 画面項目「取得エラーの表示」の example を S1(モデル)が書くと、ソースに無い文言(Failed to fetch など)や
  接頭辞の違い(Error: と TypeError: の揺れ)が出た(run9・run10・run12)。画面が実際に出す文言は次の3つの事実で決まる。
    1. 画面の表示: failure_effects の failure_text(String(e) なら Error: が付く。e.message なら付かない)
    2. API 関数の throw: client_error_templates の「${…code}: ${…message}」の形(W7 が react 下書きに出す)
    3. サーバーの返す文: backend 下書きの error_patterns の text_expanded と、status に対応する common_errors の code_default
  この3つから文字列を組み立て、対象の項目の example だけを直す。決まらなければ何も変えない(推測で作らない)。

対象の項目: behaviors の display_toggle のうち cond が failure_state そのものの行の src と、行範囲が重なる画面項目が1つだけのとき。
代表例: そのエラーの状態を書くイベントが呼ぶ API の順に、text_expanded と数の status_code を持つ最初のエラー行を使う
  (その画面の取得の経路で実際に出るもの。画面で通らない関数の throw 文言は使わない)。
"""
import json
import re
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
from pathlib import Path

ORDER = 130

# WHY: failure_text の式から、画面の表示に付く接頭辞を決める。String(e) は Error の toString なので「Error: 」が頭に付く。
#      e.message は本文だけ。これ以外の式(三項演算子など)は文字列が決まらないので対象外にする
_PREFIX_BY_EXPR = {"String(e)": "Error: ", "e.message": ""}


def _drafts(directory, suffix):
    """<機能名>_<suffix>.json を全部読む。読めないファイルは無いものとして飛ばす(_compose_row_notes と同じ読み方)。"""
    out = []
    for p in sorted(Path(directory).glob(f"*_{suffix}.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _failure_state(react):
    """react 下書きの failure_effects から (failure_state, 接頭辞, 並んだ event の一覧) を返す。決まらなければ None。
    WHY: failure_effects が複数あっても state と text が1種類のときだけ使う(別々なら代表が決まらない)。"""
    pairs = set()
    events = []
    for fe in react.get("failure_effects") or []:
        if not isinstance(fe, dict):
            continue
        st, tx = fe.get("failure_state"), fe.get("failure_text")
        if not (isinstance(st, str) and st and isinstance(tx, str)):
            continue
        pairs.add((st, tx.strip()))
        if fe.get("event") and fe["event"] not in events:
            events.append(fe["event"])
    if len(pairs) != 1:
        return None
    st, tx = next(iter(pairs))
    if tx not in _PREFIX_BY_EXPR:
        return None
    return st, _PREFIX_BY_EXPR[tx], events


def _overlap(a, b):
    """出典(file・line_start・line_end)2件の行範囲が重なるか。ファイルが違えば重ならない。"""
    if a.get("file") != b.get("file"):
        return False
    a0, a1 = a.get("line_start"), a.get("line_end") or a.get("line_start")
    b0, b1 = b.get("line_start"), b.get("line_end") or b.get("line_start")
    if None in (a0, a1, b0, b1):
        return False
    return a0 <= b1 and b0 <= a1


def _target_item(out, react, state):
    """エラーを表示する画面項目。display_toggle で cond が state の行の src と重なる画面項目が1つだけなら、その項目。"""
    srcs = []
    for b in react.get("behaviors") or []:
        if isinstance(b, dict) and b.get("kind") == "display_toggle" and (b.get("cond") or "").strip() == state:
            srcs += [r for r in b.get("src") or [] if isinstance(r, dict)]
    if not srcs:
        return None
    hit = [it for it in ((out.get("screen_layout") or {}).get("items") or [])
           if isinstance(it, dict) and any(_overlap(s, r) for s in srcs for r in it.get("src") or [] if isinstance(r, dict))]
    return hit[0] if len(hit) == 1 else None


def _template(react):
    """client_error_templates から「<x>.code と <x>.message の2つを差し込む template」を1つ選ぶ。決まらなければ None。
    WHY: API 関数の throw の形(`${body.error.code}: ${body.error.message}`)がサーバーのエラー本文を画面へ運ぶ。
    HTTP ${res.status} のような1つだけの差し込みは code と文が揃わないので代表にしない。"""
    hit = []
    for t in react.get("client_error_templates") or []:
        if not isinstance(t, dict) or t.get("kind") != "template":
            continue
        ph = t.get("placeholders") or []
        if len(ph) == 2 and ph[0].rsplit(".", 1)[-1] == "code" and ph[1].rsplit(".", 1)[-1] == "message":
            hit.append(t)
    return hit[0] if len(hit) == 1 else None


def _codes_by_status(backends):
    """backend 下書きの common_errors から {status 数: {code_default}} を作る(_units._fill_error_codes と同じ規則)。
    status は数値のほか式(getattr(e, 'status', 400) or 400)に入った数値リテラルも取る。"""
    m = {}
    for d in backends:
        for c in d.get("common_errors") or []:
            for e in c.get("errors") or []:
                code = e.get("code_default") if isinstance(e, dict) else None
                if not (isinstance(code, str) and code):
                    continue
                nums = set()
                for key in ("status_code", "status"):
                    v = e.get(key)
                    if isinstance(v, int):
                        nums.add(v)
                    elif isinstance(v, str):
                        nums |= {int(n) for n in re.findall(r"(?<![\w.])\d{3}(?![\w.])", v)}
                for n in nums:
                    m.setdefault(n, set()).add(code)
    return m


def _first_error(react, backends, events):
    """エラーの状態を書くイベントが呼ぶ API の順に、text_expanded と数の status_code を持つ最初の行を (status, text) で返す。"""
    apis = {}
    for d in backends:
        for act, a in (d.get("apis") or {}).items():
            apis.setdefault(act, a)
    for ev in events:
        for ep in react.get("event_processes") or []:
            if not isinstance(ep, dict) or ep.get("event_code") != ev:
                continue
            for call in ep.get("calls") or []:
                act = call.get("action") if isinstance(call, dict) else None
                for row in (apis.get(act) or {}).get("error_patterns") or []:
                    if isinstance(row, dict) and isinstance(row.get("status_code"), int) \
                            and isinstance(row.get("text_expanded"), str) and row["text_expanded"]:
                        return row["status_code"], row["text_expanded"]
    return None


def compose_example(out, react, backends):
    """1つの react 下書きから (対象の項目, 新しい example) を返す。決まらなければ None。"""
    fs = _failure_state(react)
    if fs is None:
        return None
    state, prefix, events = fs
    item = _target_item(out, react, state)
    tpl = _template(react)
    if item is None or tpl is None:
        return None
    err = _first_error(react, backends, events)
    if err is None:
        return None
    status, text = err
    codes = _codes_by_status(backends).get(status)
    if not codes or len(codes) != 1:
        return None
    ph = tpl["placeholders"]
    body = tpl["template"].replace("${" + ph[0] + "}", next(iter(codes))).replace("${" + ph[1] + "}", text)
    return item, prefix + body


def fill(out, log):
    """対象の項目の example を事実から作った文字列にする(違うときだけ)。下書きが無い・決まらないときは何もしない。"""
    from _config_loader import INTERMEDIATE_DIR
    backends = _drafts(INTERMEDIATE_DIR, "backend_draft")
    for react in _drafts(INTERMEDIATE_DIR, "react_draft"):
        got = compose_example(out, react, backends)
        if got is None:
            continue
        item, ex = got
        if item.get("example") != ex:
            log.append(f"[例示値] {item.get('no') or item.get('item_name')} の example を事実で直す: {item.get('example')!r} -> {ex!r}")
            item["example"] = ex


def _selftest():
    import tempfile
    import _config_loader

    F = "/src/Screen.tsx"

    def src(a, b):
        return [{"file": F, "line_start": a, "line_end": b}]

    react = {
        "failure_effects": [
            {"event": "EV00", "call": "fetchA", "failure_state": "error", "failure_text": "String(e)"},
            {"event": "EV01", "call": "fetchB", "failure_state": "error", "failure_text": "String(e)"}],
        "behaviors": [
            {"id": "BH1", "kind": "display_toggle", "cond": "loading", "src": src(10, 10)},
            {"id": "BH2", "kind": "display_toggle", "cond": "error", "src": src(20, 20)}],
        "event_processes": [{"event_code": "EV00", "calls": [{"action": "list_a"}, {"action": "list_b"}]}],
        "client_error_templates": [
            {"template": "HTTP ${res.status}", "placeholders": ["res.status"], "kind": "template"},
            {"template": "${body.error.code}: ${body.error.message}", "placeholders": ["body.error.code", "body.error.message"], "kind": "template"},
            {"template": "S3 失敗（HTTP ${put.status}）", "placeholders": ["put.status"], "kind": "template"}],
    }
    backend = {
        "apis": {"list_a": {"error_patterns": []},
                 "list_b": {"error_patterns": [
                     {"status_code": 400, "text_expanded": "from は YYYY-MM-DD 形式で指定してください"},
                     {"status_code": 400, "text_expanded": "to は YYYY-MM-DD 形式で指定してください"}]}},
        "common_errors": [{"errors": [
            {"code_default": "validation_error", "status_code": "getattr(e, 'status', 400) or 400"},
            {"code_default": "internal_error", "status_code": 500}]}],
    }

    def mk():
        return {"screen_layout": {"items": [
            {"no": "IT9", "item_name": "読込中", "example": "読込中…", "src": src(10, 10)},
            {"no": "IT10", "item_name": "取得エラーの表示", "example": "Error: Failed to fetch", "src": src(20, 20)}]}}

    def run(react_d, backend_d, out):
        old = _config_loader.INTERMEDIATE_DIR
        with tempfile.TemporaryDirectory() as td:
            Path(td, "x_react_draft.json").write_text(json.dumps(react_d, ensure_ascii=False), encoding="utf-8")
            Path(td, "x_backend_draft.json").write_text(json.dumps(backend_d, ensure_ascii=False), encoding="utf-8")
            _config_loader.INTERMEDIATE_DIR = Path(td)
            try:
                log = []
                fill(out, log)
                return log
            finally:
                _config_loader.INTERMEDIATE_DIR = old

    ex = "Error: validation_error: from は YYYY-MM-DD 形式で指定してください"
    out = mk()
    log = run(react, backend, out)
    items = {i["no"]: i["example"] for i in out["screen_layout"]["items"]}
    assert items["IT10"] == ex, items  # 最初の API(list_a)にエラー行が無いので2つ目の最初の行を使う
    assert items["IT9"] == "読込中…", items  # 他の項目は触らない
    assert len(log) == 1 and log[0].startswith("[例示値] IT10"), log
    log2 = run(react, backend, out)  # 冪等
    assert log2 == [] and out["screen_layout"]["items"][1]["example"] == ex, (log2, out)

    # e.message なら接頭辞を付けない
    r2 = json.loads(json.dumps(react))
    for fe in r2["failure_effects"]:
        fe["failure_text"] = "e.message"
    out = mk()
    run(r2, backend, out)
    assert out["screen_layout"]["items"][1]["example"] == ex[len("Error: "):], out

    # failure_text が別の式 -> 触らない
    r3 = json.loads(json.dumps(react))
    r3["failure_effects"][0]["failure_text"] = "e instanceof Error ? e.message : String(e)"
    out = mk()
    assert run(r3, backend, out) == [] and out == mk(), out

    # 対象の項目が2つ -> 触らない
    out = mk()
    out["screen_layout"]["items"].append({"no": "IT11", "item_name": "重複", "example": "x", "src": src(20, 20)})
    before = json.dumps(out, ensure_ascii=False)
    assert run(react, backend, out) == [] and json.dumps(out, ensure_ascii=False) == before, out

    # code_default が2種類の status -> 触らない
    b2 = json.loads(json.dumps(backend))
    b2["common_errors"][0]["errors"].append({"code_default": "other_error", "status_code": 400})
    out = mk()
    assert run(react, b2, out) == [] and out == mk(), out

    # code/message の template が無い -> 触らない
    r4 = json.loads(json.dumps(react))
    r4["client_error_templates"] = r4["client_error_templates"][:1] + r4["client_error_templates"][2:]
    out = mk()
    assert run(r4, backend, out) == [] and out == mk(), out
    print("[selftest OK] _compose_error_example (新2: 取得エラーの表示の例示値を failure_text・throw の形・error_patterns から作る)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_error_example.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
