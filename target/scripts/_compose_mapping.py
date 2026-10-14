"""応答の項目 → 画面項目の写し(round10 T16: N2・N3)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=120(応答の合成 110 の後)。
WHY: S2 が応答の項目を画面項目へ結ぶとき、別の表示の項目を選ぶ誤りが出る(run9 では days が「店舗」に結ばれ、
  派生配列 warnRows の warn_days は結ぶ先を取れなかった)。結ぶ先の候補はソースの事実から決め、モデルの行はその候補の中で直す。

候補の決め方(どれも react 下書きの事実):
  a) 応答の最上位キー(rows)→ state: 単一の API だけを呼ぶイベントの失敗経路の on_success_from(state ← rows)。
     複数の API を呼ぶイベントは、失敗経路の呼出名と action の対応がソースから決まらないので使わない(推測しない)。
  b) state[].項目 の使用箇所(field_usage の uses の行)が入る画面項目。
  c) 派生配列(filter 等)の fields に項目があるとき、配列名を表示条件に含む画面項目のうち、行が最小の1つ。
  d) 表示行の specs の kin・cnt が項目と一致し、その表示名の画面項目が1つに決まるとき。
入れ替え: モデルの行の項目が候補に無ければ、候補の1番目へ入れ替える(transform はそのまま。log に書く)。
  候補のうち行が無いものは行を足す。候補が空の欄は触らない。冪等(2回目は何も変えない)。
"""
import json
import re
from pathlib import Path

ORDER = 120

_NOTE_DIRECT = "{name}の表示に使う"
_NOTE_DERIVED = "{name}を出すかどうかの判定に使う(条件: {expr})"


def _drafts(directory):
    """react 下書きを全部読む。読めないファイルは無いものとして飛ばす(_compose_response と同じ読み方)。"""
    out = []
    for p in sorted(Path(directory).glob("*_react_draft.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _single_action(ep):
    """イベントの呼出が1件だけのとき、その action。複数・無しは None。
    WHY: 失敗経路は呼出名(fetchX)しか持たず、複数の API を呼ぶイベントでは action と結べないため。"""
    calls = [c for c in ep.get("calls") or [] if isinstance(c, dict)]
    return calls[0].get("action") if len(calls) == 1 else None


def _states_for(action, drafts, top):
    """API の応答の最上位キー top を受ける state 名の集合。決まらなければ空。
    WHY: on_success_from(state ← 応答のキー)が top と一致するものだけ採る。古い下書きで on_success_from が無いときは何も結ばない。"""
    if not action:
        return set()
    single = {ep.get("event_code") for d in drafts for ep in d.get("event_processes") or []
              if isinstance(ep, dict) and _single_action(ep) == action}
    states = set()
    for d in drafts:
        for fe in d.get("failure_effects") or []:
            if not isinstance(fe, dict) or fe.get("event") not in single:
                continue
            froms = fe.get("on_success_from") or {}
            for st in fe.get("on_success") or []:
                if froms.get(st) == top:
                    states.add(st)
    return states


def _usage_index(drafts):
    """field_usage の path(state[].項目)→ uses の list。同じ path は全部まとめる。"""
    idx = {}
    for d in drafts:
        for fu in d.get("field_usage") or []:
            if isinstance(fu, dict) and fu.get("path"):
                idx.setdefault(fu["path"], []).extend(u for u in fu.get("uses") or [] if isinstance(u, dict))
    return idx


def _items_at(items, s):
    """行 s(file・line_start を持つ出典)が入る画面項目の index の list。同じファイルの行範囲で判定する。"""
    if not isinstance(s, dict) or not s.get("line_start"):
        return []
    ln = s["line_start"]
    hit = []
    for i, it in enumerate(items):
        for r in it.get("src") or []:
            if not isinstance(r, dict) or r.get("file") != s.get("file") or r.get("line_start") is None:
                continue
            end = r.get("line_end") or r["line_start"]
            if r["line_start"] <= ln <= end:
                hit.append(i)
                break
    return hit


def _conds(chain):
    """表示条件の鎖から、条件の文字列を全部取る(caller_chains の中も)。"""
    for el in chain or []:
        if not isinstance(el, dict):
            continue
        if el.get("cond"):
            yield el["cond"]
        for c in el.get("caller_chains") or []:
            yield from _conds(c)


def _derived_item(items, name, cache):
    """派生配列 name を表示条件に含む画面項目のうち、行が最小の1つの index。決まらなければ None。
    WHY: 派生配列の行は画面の条件(warnRows.length > 0 など)の中に出るので、その条件を囲む項目を鎖から引く。
    鎖は _jsx_chain.chain_for_lines で TSX の行範囲から取る(TSX 以外の出典は引かない)。"""
    from _jsx_chain import chain_for_lines
    pat = re.compile(rf"\b{re.escape(name)}\b")
    best = None
    for i, it in enumerate(items):
        for s in it.get("src") or []:
            if not isinstance(s, dict) or not s.get("file") or s.get("line_start") is None:
                continue
            if Path(s["file"]).suffix not in (".tsx", ".jsx"):
                continue
            key = (s["file"], s["line_start"], s.get("line_end") or s["line_start"])
            if key not in cache:
                cache[key] = chain_for_lines(*key)
            r = cache[key]
            if r["found"] and any(pat.search(c) for c in _conds(r["visible_chain"])):
                if best is None or s["line_start"] < best[0]:
                    best = (s["line_start"], i)
    return best[1] if best else None


def _spec_items(drafts, items, leaf):
    """表示行の specs の kin・cnt が leaf と一致する行の画面項目(表示名が1つに決まるものだけ)の index の list。"""
    hit = []
    for d in drafts:
        for row in d.get("display_rows") or []:
            for s in (row.get("specs") or []) if isinstance(row, dict) else []:
                if not isinstance(s, dict) or leaf not in (s.get("kin"), s.get("cnt")):
                    continue
                same = [i for i, it in enumerate(items) if it.get("item_name") == s.get("label")]
                if len(same) == 1:
                    hit.append(same[0])
    return hit


def _candidates(states, leaf, drafts, usage, items, derived, cache):
    """応答の項目 leaf の候補 [(画面項目の index, 派生の式 または None)] を items の順に返す。None は直接の使用。"""
    hits = {}
    for st in sorted(states):
        for u in usage.get(f"{st}[].{leaf}", []):
            for s in u.get("src") or []:
                for i in _items_at(items, s):
                    hits.setdefault(i, None)
    for i in _spec_items(drafts, items, leaf):
        hits.setdefault(i, None)
    for da in derived:
        if da.get("source") in states and leaf in (da.get("fields") or []):
            i = _derived_item(items, da["name"], cache)
            if i is not None:
                hits.setdefault(i, da.get("lambda_expr") or "")
    return [(i, hits[i]) for i in sorted(hits)]


def _place(row, items, i):
    """行 row の画面項目を items[i] にする(画面項目名・screen_no・同名の順番)。
    WHY: 同名の項目が2つ以上あるときだけ screen_item_nth を入れる(名前だけでは結ぶ先が決まらないため)。
    注: 同名の順番は画面項目の並び(items の出現順)で数える。後段の結び方と一致するかは未確認(推測)。"""
    it = items[i]
    name = it.get("item_name")
    same = [j for j, x in enumerate(items) if x.get("item_name") == name]
    row["screen_item_name"] = name
    if it.get("screen_no") is not None:
        row["screen_no"] = it["screen_no"]
    if len(same) > 1:
        row["screen_item_nth"] = same.index(i) + 1
    else:
        row.pop("screen_item_nth", None)


def _write_rows(api, path, cands, items, log):
    """path の行を候補に合わせる。モデルの行の項目が候補に無ければ候補の1番目へ入れ替え、候補で行が無いものは足す。"""
    maps = api["response_to_screen_mapping"]
    action = api.get("action")
    rows = [m for m in maps if isinstance(m, dict) and m.get("api_field_path") == path]
    cand_names = {items[i].get("item_name") for i, _ in cands}
    for m in rows:
        if m.get("screen_item_name") in cand_names:
            continue
        before = m.get("screen_item_name")
        _place(m, items, cands[0][0])
        log.append(f"[写し] api_spec[{action}].response_to_screen_mapping[{path}] の画面項目 {before} を {m['screen_item_name']} に直す(候補に無い)")
    have = {(m.get("screen_item_name"), m.get("screen_item_nth")) for m in rows}
    # WHY: 行が1つも無い path は末尾へ足す(maps が空でも動くように -1 から数える)
    existing = [k for k, m in enumerate(maps) if isinstance(m, dict) and m.get("api_field_path") == path]
    last = existing[-1] if existing else len(maps) - 1
    for i, expr in cands:
        row = {"api_field_path": path}
        _place(row, items, i)
        key = (row["screen_item_name"], row.get("screen_item_nth"))
        if key in have:
            continue
        have.add(key)
        row["transform"] = (_NOTE_DERIVED.format(name=row["screen_item_name"], expr=expr) if expr is not None
                            else _NOTE_DIRECT.format(name=row["screen_item_name"]))
        last += 1
        maps.insert(last, row)
        log.append(f"[写し] api_spec[{action}].response_to_screen_mapping[{path}] に画面項目 {row['screen_item_name']} を足す")


def fill(out, log):
    """api_spec ごとに、応答の項目の写しを react 下書きの事実で補う(N2・N3)。下書きや画面項目が無ければ何もしない。"""
    from _config_loader import INTERMEDIATE_DIR
    drafts = _drafts(INTERMEDIATE_DIR)
    items = [x for x in ((out.get("screen_layout") or {}).get("items") or []) if isinstance(x, dict)]
    if not drafts or not items:
        return
    usage = _usage_index(drafts)
    derived = [da for d in drafts for da in d.get("derived_arrays") or [] if isinstance(da, dict) and da.get("name")]
    cache = {}
    for api in out.get("api_spec") or []:
        if not isinstance(api, dict) or not isinstance(api.get("response_to_screen_mapping"), list):
            continue
        # WHY: 写しの行が無い応答の項目も候補を見る(warn_days のように、モデルが行を書かなかった項目へ行を足すため)
        keys = [m.get("api_field_path") for m in api["response_to_screen_mapping"] if isinstance(m, dict)]
        keys += [x.get("key") for x in api.get("response_items") or [] if isinstance(x, dict)]
        paths = dict.fromkeys(keys)
        for path in paths:
            if not isinstance(path, str) or "[]." not in path:
                continue
            top, leaf = path.split("[].", 1)
            if "." in leaf or "[" in leaf:
                continue
            states = _states_for(api.get("action"), drafts, top)
            if not states:
                continue
            cands = _candidates(states, leaf, drafts, usage, items, derived, cache)
            if cands:
                _write_rows(api, path, cands, items, log)


def _selftest():
    """a) 候補の入れ替え(項目が候補に無い)・b) 候補の行の足し(モデルの行は残す)・c) 派生配列の項目への結び・
    d) 候補が空の欄は触らない・e) 複数の API を呼ぶイベントの応答は結ばない・f) 2回目は何も変えない、を一時の下書きで確かめる。"""
    import copy
    import tempfile
    import _config_loader

    src = ("export default function S() {\n"
           "  const [rows, setRows] = useState([]);\n"
           "  const w = useMemo(() => rows.filter((r) => r.days > 0), [rows]);\n"
           "  return (\n"
           "    <div>\n"
           "      {w.length > 0 && (\n"
           "        <span>{rows[0].label}</span>\n"
           "      )}\n"
           "      <span>{rows[0].name}</span>\n"
           "    </div>\n"
           "  );\n"
           "}\n")
    orig = _config_loader.INTERMEDIATE_DIR
    with tempfile.TemporaryDirectory() as td:
        tsx = Path(td, "S.tsx")
        tsx.write_text(src, encoding="utf-8")
        f = str(tsx)

        def sp(line):
            return [{"file": f, "line_start": line, "line_end": line}]

        draft = {
            "event_processes": [{"event_code": "EV01", "calls": [{"action": "get_x"}]},
                                {"event_code": "EV03", "calls": [{"action": "get_z"}, {"action": "get_w"}]}],
            "failure_effects": [
                {"event": "EV01", "call": "fetchX", "on_success": ["rows"], "on_success_from": {"rows": "rows"}},
                {"event": "EV03", "call": "fetchZ", "on_success": ["rows"], "on_success_from": {"rows": "rows"}}],
            "field_usage": [
                {"path": "rows[].name", "uses": [{"src": sp(7)}, {"src": sp(9)}]},
                {"path": "rows[].code", "uses": [{"src": sp(9)}]},
                {"path": "rows[].days", "uses": [{"src": sp(3)}]}],
            "derived_arrays": [{"name": "w", "source": "rows", "op": "filter", "fields": ["days"],
                                "lambda_expr": "r.days > 0", "src": sp(3)}],
        }
        Path(td, "S_react_draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        items = [{"item_name": "件数表示", "screen_no": "1", "src": sp(7)},
                 {"item_name": "店名", "screen_no": "2", "src": sp(9)}]

        def mk():
            return {"screen_layout": {"items": copy.deepcopy(items)},
                    "api_spec": [
                        {"action": "get_x", "response_to_screen_mapping": [
                            {"api_field_path": "rows[].name", "screen_item_name": "店名", "transform": "T1", "screen_no": "2"},
                            {"api_field_path": "rows[].code", "screen_item_name": "件数表示", "transform": "T2", "screen_no": "1"},
                            {"api_field_path": "rows[].days", "screen_item_name": "店名", "transform": "T3", "screen_no": "2"},
                            {"api_field_path": "rows[].zz", "screen_item_name": "店名", "transform": "T4", "screen_no": "2"}]},
                        {"action": "get_z", "response_to_screen_mapping": [
                            {"api_field_path": "rows[].name", "screen_item_name": "件数表示", "transform": "T5", "screen_no": "1"}]}]}

        try:
            _config_loader.INTERMEDIATE_DIR = Path(td)
            out = mk()
            log = []
            fill(out, log)
            once = json.dumps(out, ensure_ascii=False, sort_keys=True)
            log2 = []
            fill(out, log2)
            assert json.dumps(out, ensure_ascii=False, sort_keys=True) == once, "2回目で変わった(冪等でない)"
            assert log2 == [], log2
        finally:
            _config_loader.INTERMEDIATE_DIR = orig

    x, z = out["api_spec"]
    got = [(m["api_field_path"], m["screen_item_name"], m["transform"]) for m in x["response_to_screen_mapping"]]
    assert got == [
        ("rows[].name", "店名", "T1"),
        ("rows[].name", "件数表示", "件数表示の表示に使う"),
        ("rows[].code", "店名", "T2"),
        ("rows[].days", "件数表示", "T3"),
        ("rows[].zz", "店名", "T4"),
    ], got
    by = {m["api_field_path"]: m for m in x["response_to_screen_mapping"]}
    assert by["rows[].code"]["screen_no"] == "2", by["rows[].code"]
    assert by["rows[].days"]["screen_no"] == "1", by["rows[].days"]
    assert [(m["screen_item_name"], m["transform"]) for m in z["response_to_screen_mapping"]] == [("件数表示", "T5")], z
    assert len(log) == 3, log
    print("[selftest OK] _compose_mapping (N2・N3: 応答の項目を画面項目へ写す)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_mapping.py --selftest (fill は _units.compose から呼ばれる)")
        sys.exit(2)
