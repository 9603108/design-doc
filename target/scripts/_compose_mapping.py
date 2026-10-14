"""応答の項目 → 画面項目の写し(round10 T16: N2・N3)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=120(応答の合成 110 の後)。
WHY: S2 が応答の項目を画面項目へ結ぶとき、別の表示の項目を選ぶ誤りが出る(run9 では days が「店舗」に結ばれ、
  派生配列 warnRows の warn_days は結ぶ先を取れなかった)。結ぶ先の候補はソースの事実から決め、モデルの行はその候補の中で直す。

候補の決め方(どれも react 下書きの事実):
  a) 応答の最上位キー(rows)→ state: 単一の API だけを呼ぶイベントの失敗経路の on_success_from(state ← rows)。
     複数の API を呼ぶイベントは、失敗経路の呼出名と action の対応がソースから決まらないので使わない(推測しない)。
  b) state[].項目 の使用箇所(field_usage の uses の行)が入る画面項目(format=固定文言 の見出しは外す。React の key だけの使用は外す)。
  c) 派生配列(filter 等)の fields に項目があるとき、配列名を表示条件に含む画面項目のうち、行が最小の1つ。
  d) 表示行の specs の kin・cnt が項目と一致し、その表示名の画面項目が1つに決まるとき。
  b) の補足(round13 W6): 使用行に当たった項目が入れ子のときは内側の項目だけを候補にする(入れ物は外す)。
表示行の spec の葉は、display_rows[].cell_format・specs[].unit(react 下書き)から transform を事実の文で上書きする(cell_format が null なら触らない)。
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


def _range_at(it, s):
    """画面項目 it の src のうち、使用行 s が入る範囲 (line_start, line_end)。無ければ None。_items_at と同じ判定。"""
    ln = s["line_start"]
    for r in it.get("src") or []:
        if not isinstance(r, dict) or r.get("file") != s.get("file") or r.get("line_start") is None:
            continue
        end = r.get("line_end") or r["line_start"]
        if r["line_start"] <= ln <= end:
            return (r["line_start"], end)
    return None


def _innermost(items, idxs, s):
    """使用行 s に当たった画面項目 idxs のうち、別の当たった項目の範囲を真に含む外側の項目を外した list(round13 W6 a・新4')。
    WHY: Card の subtitle(318行)の使用が、その行を含む入れ物の項目(IT18: 314-321)にも当たり、入れ物にまで写しの行が足された。
    使用行を直に持つのは内側の項目なので、入れ物は候補にしない。範囲が入れ子でない(一部だけ重なる・同じ範囲)当たりは全部残す
    (どちらが内側か事実で決まらないため)。接続先: _candidates の b) 使用箇所の当たり。"""
    rng = {i: _range_at(items[i], s) for i in idxs}
    out = []
    for i in idxs:
        a = rng[i]
        inner_exists = a is not None and any(
            j != i and rng[j] is not None and a[0] <= rng[j][0] and rng[j][1] <= a[1] and a != rng[j]
            for j in idxs)
        if not inner_exists:
            out.append(i)
    return out


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


def _is_key_use(u):
    """使用が React の key だけのもの(element が「… の key」)。画面に出ない値なので応答の値の写し先にしない。
    WHY: key は画面の項目ではない(rows[].shop_id の key が会計カードの副題に結ばれた。round12 V6)。"""
    return bool(re.search(r"の key$", str(u.get("element") or "")))


def _key_only(usage, states, leaf):
    """応答の項目 leaf の使用が1件以上あり、全部 key だけのとき True(写しの行を消す根拠)。使用が無ければ False。"""
    uses = [u for st in sorted(states) for u in usage.get(f"{st}[].{leaf}", [])]
    return bool(uses) and all(_is_key_use(u) for u in uses)


def _drop_rows(api, path, log):
    """path の写しの行を消す(使用が key だけで候補が無いとき)。行が無ければ何もしない(冪等)。"""
    maps = api["response_to_screen_mapping"]
    keep = [m for m in maps if not (isinstance(m, dict) and m.get("api_field_path") == path)]
    if len(keep) == len(maps):
        return
    log.append(f"[写し] api_spec[{api.get('action')}].response_to_screen_mapping[{path}] の行を消す(使用が React の key だけ)")
    maps[:] = keep


def _candidates(states, leaf, drafts, usage, items, derived, cache):
    """応答の項目 leaf の候補 [(画面項目の index, 派生の式 または None)] を items の順に返す。None は直接の使用。"""
    hits = {}
    for st in sorted(states):
        for u in usage.get(f"{st}[].{leaf}", []):
            # WHY: key だけの使用は画面に出ない値なので、候補にも入れない(V6)
            if _is_key_use(u):
                continue
            for s in u.get("src") or []:
                # WHY: 画面の見出し(format=固定文言)は応答の値を出さない。使用行が重なっても候補にしない
                # (入れ物の判定の前に外す。見出しだけが内側のとき、入れ物まで候補から消えないようにする)
                idxs = [i for i in _items_at(items, s) if items[i].get("format") != "固定文言"]
                for i in _innermost(items, idxs, s):
                    hits.setdefault(i, None)
    for i in _spec_items(drafts, items, leaf):
        hits.setdefault(i, None)
    for da in derived:
        if da.get("source") in states and leaf in (da.get("fields") or []):
            i = _derived_item(items, da["name"], cache)
            if i is not None:
                hits.setdefault(i, da.get("lambda_expr") or "")
    return [(i, hits[i]) for i in sorted(hits)]


def _spec_of(drafts, leaf):
    """表示行の spec のうち kin か cnt が leaf のもの (spec, 表示行の cell_format)。ちょうど1つのときだけ返し、無い・2つ以上は None。
    WHY: 同じ葉を2つの spec が持つと、どちらの書式か事実で決まらない(推測しない)。"""
    found = [(s, row.get("cell_format")) for d in drafts for row in d.get("display_rows") or [] if isinstance(row, dict)
             for s in row.get("specs") or [] if isinstance(s, dict) and leaf in (s.get("kin"), s.get("cnt"))]
    return found[0] if len(found) == 1 else None


def _spec_transform(drafts, leaf):
    """表示行の spec の葉 leaf の transform の文と、その spec の label。決まらなければ None(round13 W6 b・新3)。
    WHY: S2 の transform が55行とも「そのまま表示」で、点数の「点」・金額の「円」・人数の「人」、3桁区切り、値が無いときの0表示が
    設計書に出なかった。これらは W7 が react 下書きに出した display_rows[].cell_format(セルの書式の事実)と spec.unit から機械で作る。
    cell_format が null(書式が決まらない画面)・spec が無い・同じ葉の spec が2つ以上のときは None で、モデルの行を触らない。
    単位 = 書式の suffix、無ければ suffix_from_unit が true のとき spec.unit(無ければ unit_default)。単位が決まらなければ「と「…」」は書かない。
    3桁区切り・0表示は grouping・null_as_zero が true のときだけ書く(true でなければ書かない。推測しない)。
    0表示の null_as_zero は spec.null_as_zero[cnt|kin|single](spec ごと)を先に読み、無ければ cell_format の値(R1)。"""
    hit = _spec_of(drafts, leaf)
    if not hit:
        return None
    spec, cf = hit
    if not isinstance(cf, dict) or not spec.get("label"):
        return None
    if leaf == spec.get("cnt"):
        role, where = "cnt", "点数欄"
    elif spec.get("cnt"):
        role, where = "kin", "金額欄"
    else:
        role, where = "single", None
    fmt = cf.get(role)
    if not isinstance(fmt, dict):
        return None
    suffix = fmt.get("suffix")
    if not suffix and fmt.get("suffix_from_unit"):
        suffix = spec.get("unit") or fmt.get("unit_default")
    text = (f"「{spec['label']}」行の{where}に、" if where else f"「{spec['label']}」行に、")
    text += ("3桁区切りの" if fmt.get("grouping") is True else "") + "数値"
    text += (f"と「{suffix}」" if suffix else "") + "で出す"
    # WHY(R1): 値が無いときの0は spec ごとの事実(spec.null_as_zero[役割])を先に見る。欄(cell_format)の値は三項 `s.calc ? … : num(…)` の
    #   枝が決まらず null になり、calc を持たない金額欄・単独セルの行にも文が付かなかった。spec の値が無い(古い下書き)ときだけ欄の値を使う。
    #   spec の値が null(calc の式が 0 にするか決まらない行)は欄が true でも書かない(推測しない)
    per = spec.get("null_as_zero")
    zero = per[role] if isinstance(per, dict) and role in per else fmt.get("null_as_zero")
    if zero is True:
        text += "。値が無いときは0として出す"
    return text, spec["label"], spec.get("nth")


def _apply_spec_transform(api, path, leaf, drafts, log):
    """path の写しの行のうち、画面項目が spec の label と同じものの transform を事実の文で上書きする(違うときだけ log に書く)。
    WHY: 他の画面項目(副題・一覧など)への行は、spec の書式の対象ではないので触らない。
    同名の項目が複数あり、行の screen_item_nth と spec.nth が食い違うものも触らない。接続先: fill の応答の項目ごとの処理。"""
    res = _spec_transform(drafts, leaf)
    if not res:
        return
    text, label, nth = res
    for m in api["response_to_screen_mapping"]:
        if not isinstance(m, dict) or m.get("api_field_path") != path or m.get("screen_item_name") != label:
            continue
        if m.get("screen_item_nth") is not None and nth is not None and m["screen_item_nth"] != nth:
            continue
        if m.get("transform") != text:
            m["transform"] = text
            log.append(f"[写し] api_spec[{api.get('action')}].response_to_screen_mapping[{path}] の transform を事実で直す")


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
            # WHY: spec の書式は state の候補と無関係に決まる(表示行の葉の事実)ので、states の有無より先に直す(W6 b)
            _apply_spec_transform(api, path, leaf, drafts, log)
            states = _states_for(api.get("action"), drafts, top)
            if not states:
                continue
            cands = _candidates(states, leaf, drafts, usage, items, derived, cache)
            if cands:
                _write_rows(api, path, cands, items, log)
            elif _key_only(usage, states, leaf):
                # WHY: 使用が key だけで候補が無い項目は、モデルが書いた写しの行も消す(V6)
                _drop_rows(api, path, log)


def _selftest():
    """a) 候補の入れ替え(項目が候補に無い)・b) 候補の行の足し(モデルの行は残す)・c) 派生配列の項目への結び・
    d) 候補が空の欄は触らない・e) 複数の API を呼ぶイベントの応答は結ばない・f) 2回目は何も変えない・
    g) 画面の見出し(固定文言)は使用行が重なっても候補に入らない、を一時の下書きで確かめる。"""
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
                # WHY: key と値の併用の形。key の使用(行 7)は候補に入れず、値の使用(行 9)だけで決まる
                {"path": "rows[].code", "uses": [{"element": "Card の key", "src": sp(7)}, {"src": sp(9)}]},
                # WHY: key だけの使用の形。候補が無いので、モデルが書いた写しの行は消える
                {"path": "rows[].sid", "uses": [{"element": "Card の key", "src": sp(9)}]},
                {"path": "rows[].days", "uses": [{"src": sp(3)}]}],
            "derived_arrays": [{"name": "w", "source": "rows", "op": "filter", "fields": ["days"],
                                "lambda_expr": "r.days > 0", "src": sp(3)}],
        }
        Path(td, "S_react_draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        items = [{"item_name": "件数表示", "screen_no": "1", "src": sp(7)},
                 {"item_name": "店名", "screen_no": "2", "src": sp(9)},
                 # WHY: 見出し(固定文言)は行 7 の使用行に重なるが、応答の値を出す項目ではないので候補に入れない
                 {"item_name": "見出し", "format": "固定文言", "screen_no": "1", "src": sp(7)}]

        def mk():
            return {"screen_layout": {"items": copy.deepcopy(items)},
                    "api_spec": [
                        {"action": "get_x", "response_to_screen_mapping": [
                            {"api_field_path": "rows[].name", "screen_item_name": "店名", "transform": "T1", "screen_no": "2"},
                            {"api_field_path": "rows[].code", "screen_item_name": "件数表示", "transform": "T2", "screen_no": "1"},
                            {"api_field_path": "rows[].days", "screen_item_name": "店名", "transform": "T3", "screen_no": "2"},
                            {"api_field_path": "rows[].sid", "screen_item_name": "店名", "transform": "T6", "screen_no": "2"},
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
    # WHY: 4件 = 書き換え2件(days・code)・足し1件(件数表示)・key だけの行の削除1件(sid)
    assert len(log) == 4, log
    assert not any("rows[].sid" in m["api_field_path"] and "店名" in m["screen_item_name"] and m["transform"] == "T6"
                   for m in x["response_to_screen_mapping"]), x["response_to_screen_mapping"]
    print("[selftest OK] _compose_mapping (N2・N3: 応答の項目を画面項目へ写す)")


def _selftest_w6():
    """round13 W6 の形: (1) 入れ物の項目(範囲が使用行の項目を真に含む)は候補から外れ、入れ子でない当たりは両方残る。
    (2) cell_format ありの spec の transform が事実の文になる(cnt・kin で cnt あり・kin で cnt 無しの単位 unit)。
    (3) cell_format が null・spec 無し・同じ葉が2つの spec のときは transform を触らない。"""
    def it(name, a, b, **kw):
        return dict({"item_name": name, "src": [{"file": "F.tsx", "line_start": a, "line_end": b}]}, **kw)

    s = {"file": "F.tsx", "line_start": 318, "line_end": 318}
    items = [it("入れ物", 314, 321), it("副題", 318, 318)]
    assert _innermost(items, _items_at(items, s), s) == [1], "入れ物が候補に残った"
    items = [it("A", 310, 320), it("B", 315, 330)]
    s2 = {"file": "F.tsx", "line_start": 318, "line_end": 318}
    assert _innermost(items, _items_at(items, s2), s2) == [0, 1], "入れ子でない当たりが減った"
    # 候補の経路でも確かめる: 見出し(固定文言)を外した後に入れ物判定する
    usage = {"rows[].days": [{"src": [s]}]}
    items = [it("入れ物", 314, 321), it("副題", 318, 318), it("見出し", 318, 318, format="固定文言")]
    got = _candidates({"rows"}, "days", [], {"rows[].days": [{"src": [s]}]}, items, [], {})
    assert [i for i, _ in got] == [1], got

    def F(suffix=None, from_unit=False, default=None, grouping=True, zero=None):
        return {"suffix": suffix, "suffix_from_unit": from_unit, "unit_default": default,
                "grouping": grouping, "null_as_zero": zero, "src": []}
    cf = {"cnt": F("点", zero=True), "kin": F("円", zero=True), "single": F(None, True, "円", zero=True)}
    rows = [{"cell_format": cf, "specs": [
        {"label": "売上訂正", "kin": "k1", "cnt": "c1", "unit": None, "nth": 1},
        {"label": "総売上", "kin": "k2", "cnt": None, "unit": None, "nth": 1},
        {"label": "客数", "kin": "k3", "cnt": None, "unit": "人", "nth": 1},
        {"label": "重複A", "kin": "dup", "cnt": None, "unit": None, "nth": 1},
        {"label": "重複B", "kin": "dup", "cnt": None, "unit": None, "nth": 1}]},
        {"cell_format": None, "specs": [{"label": "書式なし", "kin": "k9", "cnt": None, "unit": None, "nth": 1}]}]
    drafts = [{"display_rows": rows}]
    assert _spec_transform(drafts, "c1")[0] == "「売上訂正」行の点数欄に、3桁区切りの数値と「点」で出す。値が無いときは0として出す"
    assert _spec_transform(drafts, "k1")[0] == "「売上訂正」行の金額欄に、3桁区切りの数値と「円」で出す。値が無いときは0として出す"
    assert _spec_transform(drafts, "k2")[0] == "「総売上」行に、3桁区切りの数値と「円」で出す。値が無いときは0として出す"
    assert _spec_transform(drafts, "k3")[0] == "「客数」行に、3桁区切りの数値と「人」で出す。値が無いときは0として出す"
    assert _spec_transform(drafts, "dup") is None, "同じ葉の spec が2つなのに作った"
    assert _spec_transform(drafts, "k9") is None, "cell_format null なのに作った"
    assert _spec_transform(drafts, "nothing") is None
    # grouping・null_as_zero が true でなければ、その語を書かない(推測しない)
    cf["kin"] = F("円", grouping=None, zero=None)
    assert _spec_transform(drafts, "k1")[0] == "「売上訂正」行の金額欄に、数値と「円」で出す"
    # R1: 欄の値が null(三項で決まらない)でも、spec ごとの null_as_zero が true の行には文が付き、calc を持つ行(null)には付かない。
    #   点数欄は cnt、金額欄は kin、cnt の無い spec は single の値を読む。spec の値が無ければ欄の値(上の従来の形)
    cf2 = {"cnt": F("点", zero=True), "kin": F("円", zero=None), "single": F(None, True, "円", zero=None)}
    d2 = [{"display_rows": [{"cell_format": cf2, "specs": [
        {"label": "売上訂正", "kin": "k1", "cnt": "c1", "unit": None, "nth": 1, "null_as_zero": {"cnt": True, "kin": True}},
        {"label": "総売上", "kin": "k2", "cnt": None, "unit": None, "nth": 1, "null_as_zero": {"single": True}},
        {"label": "客数", "kin": "k3", "cnt": None, "unit": "人", "nth": 1, "null_as_zero": {"single": True}},
        {"label": "平均単価", "kin": None, "cnt": None, "unit": None, "nth": 1, "null_as_zero": {"single": None}},
        {"label": "旧下書き", "kin": "k4", "cnt": None, "unit": None, "nth": 1}]}]}]
    assert _spec_transform(d2, "c1")[0].endswith("点数欄に、3桁区切りの数値と「点」で出す。値が無いときは0として出す")
    assert _spec_transform(d2, "k1")[0] == "「売上訂正」行の金額欄に、3桁区切りの数値と「円」で出す。値が無いときは0として出す"
    assert _spec_transform(d2, "k2")[0] == "「総売上」行に、3桁区切りの数値と「円」で出す。値が無いときは0として出す"
    assert _spec_transform(d2, "k3")[0] == "「客数」行に、3桁区切りの数値と「人」で出す。値が無いときは0として出す"
    assert _spec_transform(d2, "k4")[0] == "「旧下書き」行に、3桁区切りの数値と「円」で出す", "spec の値が無く欄が null なら書かない"
    cf2["single"]["null_as_zero"] = True
    d2[0]["display_rows"][0]["specs"][3]["kin"] = "k5"
    assert _spec_transform(d2, "k5")[0] == "「平均単価」行に、3桁区切りの数値と「円」で出す", "spec が null(calc 行)は欄が true でも書かない"
    # 適用: 画面項目が label と同じ行だけ上書きし、2回目は変えない。cell_format null の行は従来のまま
    api = {"action": "a", "response_to_screen_mapping": [
        {"api_field_path": "rows[].k2", "screen_item_name": "総売上", "transform": "そのまま表示"},
        {"api_field_path": "rows[].k2", "screen_item_name": "副題", "transform": "副題の表示に使う"},
        {"api_field_path": "rows[].k9", "screen_item_name": "書式なし", "transform": "そのまま表示"}]}
    log = []
    for p, leaf in (("rows[].k2", "k2"), ("rows[].k9", "k9")):
        _apply_spec_transform(api, p, leaf, drafts, log)
    t = [m["transform"] for m in api["response_to_screen_mapping"]]
    assert t == ["「総売上」行に、3桁区切りの数値と「円」で出す。値が無いときは0として出す", "副題の表示に使う", "そのまま表示"], t
    assert len(log) == 1, log
    _apply_spec_transform(api, "rows[].k2", "k2", drafts, log)
    assert len(log) == 1, "2回目で変わった"
    print("[selftest OK] _compose_mapping W6 (入れ物の候補外し・spec の transform)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        _selftest_w6()
    else:
        print("usage: _compose_mapping.py --selftest (fill は _units.compose から呼ばれる)")
        sys.exit(2)
