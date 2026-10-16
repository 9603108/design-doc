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
  ただし、その項目が「応答の項目を被演算子にする計算(calc_candidates)の出力先」(calc の label = 画面項目名)なら入れ替えない(run15 差1)。
  候補のうち行が無いものは行を足す。候補が空の欄は触らない。冪等(2回目は何も変えない)。
件数の写し(第20回): 画面が配列の件数({rows.length}・${shops.length})を表示する所(react 下書きの count_displays)は、応答のキーに結ばれる API の
  response_to_screen_mapping へ件数の行(api_field_path は response_items の配列の key。無ければ <キー>.length)を足す(単位と表示条件は下書きの事実。S2 が書いていれば足さない。_add_count_rows)。
  WHY: [件数の写し] の WARN が run15・16・17 で毎回 S2 に出た。件数の表示は下書きの事実で決まるので、S2 に書かせず機械で足す。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す

ORDER = 120

_NOTE_DIRECT = "{name}の表示に使う"
_NOTE_DERIVED = "{name}を出すかどうかの判定に使う(条件: {expr})"


def _drafts(directory):
    """react 下書きを全部読む。読めないファイルは無いものとして飛ばす(_compose_response と同じ読み方)。"""
    from _config_loader import draft_files
    out = []
    for p in draft_files("react", directory):  # 機能名の下書き1本
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


def _is_literal_item(it):
    """画面項目 it の例示値が、出典 src の行範囲に書かれた文字列リテラルそのもの(title="会計レポート" の見出し・<h1>見出し</h1> など)なら True。
    WHY(run17 差1): S1 が Card の見出し(IT15)と副題(IT16)に同じ src(Card 要素の全体 314-321)を付けたため、副題の使用行(318)が両方に当たり、
    shop_name・reji_no・days が固定の見出し「会計レポート」(317)にも結ばれた。範囲は同じで入れ子にならず、_innermost では絞れない。
    例示値が範囲内の文字列リテラルと一字一句同じ項目は、画面に出る字面が固定で応答の値を出さないので、写し先の候補から外す。
    例示値が空・TSX でない出典・読めない出典は False(外さない。推測しない)。接続先: _candidates の b) 使用箇所の当たり。"""
    ex = re.sub(r"\s+", " ", str(it.get("example") or "")).strip()
    if not ex:
        return False
    quoted = re.compile(r"([\"'`])" + re.escape(ex) + r"\1")
    in_text = re.compile(r">\s*" + re.escape(ex) + r"\s*<")
    for r in it.get("src") or []:
        if not isinstance(r, dict) or not r.get("file") or r.get("line_start") is None:
            continue
        if Path(r["file"]).suffix not in (".tsx", ".jsx"):
            continue
        try:
            lines = Path(r["file"]).read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError):
            continue
        # fallback-ok: 終了行が無い src は開始行だけの1行とみなす(行範囲は src が常に持つ想定)
        body = "\n".join(lines[int(r["line_start"]) - 1:int(r.get("line_end") or r["line_start"])])
        if quoted.search(body) or in_text.search(body):
            return True
    return False


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
                # WHY(run17 差1): format が固定文言でなくても、例示値が出典の範囲内の文字列リテラルそのものの項目(Card の title)は
                # 固定文なので外す(_is_literal_item)。同じ範囲の副題の項目だけが残り、最も狭い一致(_innermost)で決まる
                idxs = [i for i in _items_at(items, s)
                        if items[i].get("format") != "固定文言" and not _is_literal_item(items[i])]
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


def _calc_targets(drafts, items, leaf):
    """応答の項目 leaf を被演算子に持つ計算(calc_candidates)の出力先の画面項目名の集合。決まらなければ空。
    WHY(run15 差1): 計算の結果が出る画面項目(支払科目に無い額・現金過不足・平均単価・客単価)は、field_usage にも表示行の spec(kin/cnt)にも
    使用として現れないので、候補(_candidates)に入らない。S2 が正しく結んだ「leaf から計算した結果を出す項目」への行まで、候補の1番目
    (入力側の項目)へ付け替えて壊していた。出力先は calc の label と画面項目の item_name の字面が一致するものだけを採る
    (_vs_example と同じ結び方。推測しない)。接続先: _write_rows の keep(この名前の行は付け替えない)。"""
    names = {it.get("item_name") for it in items}
    return {c["label"] for d in drafts for c in d.get("calc_candidates") or []
            if isinstance(c, dict) and c.get("label") in names and leaf in (c.get("operands") or [])}


def _csv_targets(drafts, items, leaf):
    """応答の項目 leaf を CSV に出す出力(csv_exports)の、起動ボタンの画面項目名の集合。決まらなければ空。
    WHY(run20 差1): CSV の列は画面の表示項目ではなく、field_usage にも使用として現れないので候補(_candidates)に入らない。S2 が
    写し先を「CSV出力」(ボタン)にした店舗名・日数などの写しまで、候補の1番目(IT13・IT17 の表示項目)へ付け替えて壊していた。
    結び方: csv_exports[].trigger_events の event_code → 同じ event_code の events[].src と行が重なる画面項目。
    かつ、その csv_exports の行の組み立て(row_builder 等)に `.leaf` が出るときだけ(leaf を CSV に出さない出力なら、S2 の行は
    CSV の写しではないので、従来どおり候補へ直す)。接続先: fill の _write_rows の keep(この項目への行は付け替えない)。推測しない。"""
    word = re.compile(r"\." + re.escape(leaf) + r"\b")
    names = set()
    for d in drafts:
        ev_src = {e.get("event_code"): [s for s in e.get("src") or [] if isinstance(s, dict)]
                  for e in d.get("events") or [] if isinstance(e, dict)}
        for ex in d.get("csv_exports") or []:
            if not isinstance(ex, dict) or not word.search(json.dumps(ex, ensure_ascii=False)):
                continue
            for code in ex.get("trigger_events") or []:
                for s in ev_src.get(code, []):
                    # WHY: ボタンを囲む入れ物(フォーム等)まで保護すると、入れ物への誤った行も残るので内側の項目だけにする(_innermost)
                    for i in _innermost(items, _items_at(items, s), s):
                        if items[i].get("item_name"):
                            names.add(items[i]["item_name"])
    return names


def _write_rows(api, path, cands, items, log, keep=frozenset()):
    """path の行を候補に合わせる。モデルの行の項目が候補にも keep(計算の出力先)にも無ければ候補の1番目へ入れ替え、候補で行が無いものは足す。
    keep の項目への行は S2 が結んだ計算の出力先なので触らない。keep への行は足さない(出力先ごとの文は S2 が書く事実で、機械の定型文では作れない)。"""
    maps = api["response_to_screen_mapping"]
    action = api.get("action")
    rows = [m for m in maps if isinstance(m, dict) and m.get("api_field_path") == path]
    cand_names = {items[i].get("item_name") for i, _ in cands} | set(keep)
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


def _state_keys(drafts):
    """配列 state → 応答のキー({"rows": "rows", "shops": "rows"})。failure_effects[].on_success_from が正。決まらない state は入れない。
    WHY: 件数の写しの api_field_path(rows.length)の根は、画面の state 名でなく応答のキーのため。呼出元: _add_count_rows。"""
    out = {}
    for d in drafts:
        for fe in d.get("failure_effects") or []:
            frm = fe.get("on_success_from") if isinstance(fe, dict) and isinstance(fe.get("on_success_from"), dict) else {}
            out.update({k: v for k, v in frm.items() if k and v})
    return out


def _call_action(drafts, event, call):
    """イベント event の呼出(event_processes[].calls[])のうち、出典(api.ts の呼出の行)の直前3行を含む範囲に API 関数名 call が字面である呼出の action。
    ちょうど1つに決まるときだけ返す(複数・無し・ファイルが読めない場合は None)。
    WHY: 初期表示のように複数の API を呼ぶイベントは、失敗経路の呼出名(fetchPosShops)と action(list_pos_shops)の対応を _single_action では決められない
    (推測しない)。呼出の出典 src は API 関数の本体(return call(…"list_pos_shops"…))を指し、関数名はその数行上にあるので、その範囲の字面で結ぶ。"""
    acts = set()
    for d in drafts:
        for ep in d.get("event_processes") or []:
            if not isinstance(ep, dict) or ep.get("event_code") != event:
                continue
            for c in ep.get("calls") or []:
                for s in (c.get("src") if isinstance(c, dict) else None) or []:
                    try:
                        lines = Path(s["file"]).read_text(encoding="utf-8").splitlines()
                    except (OSError, KeyError):
                        continue
                    window = "\n".join(lines[max(0, int(s["line_start"]) - 4):int(s.get("line_end") or s["line_start"])])
                    if re.search(rf"(?<![\w$.]){re.escape(call)}(?![\w$])", window):
                        acts.add(c.get("action"))
    acts.discard(None)
    return next(iter(acts)) if len(acts) == 1 else None


def _count_owners(items, cd):
    """件数の表示(count_displays の1件)の行を囲む出典を持つ画面項目のうち、範囲がいちばん狭いものの index の list(同じ狭さは全部)。無ければ空。
    WHY: _vs_example.check_count_mapping と同じ持ち主の決め方(囲む広い項目に写しを足さない)。"""
    s = cd["src"][0]
    ln = s["line_start"]
    cover = []
    for i, it in enumerate(items):
        for r in it.get("src") or []:
            if (isinstance(r, dict) and r.get("file") == s.get("file") and isinstance(r.get("line_start"), int)
                    and r["line_start"] <= ln <= int(r.get("line_end") or r["line_start"])):
                cover.append((int(r.get("line_end") or r["line_start"]) - r["line_start"], i))
    if not cover:
        return []
    narrow = min(w for w, _ in cover)
    return sorted({i for w, i in cover if w == narrow})


def _pick_owner(items, owners, cd):
    """同じ狭さの項目が複数あるとき、件数を出す1つを決める。画面の単位(units)が example に出る項目がちょうど1つならそれ、決まらなければ最初の項目。
    WHY: Card の見出しと副題が同じ行範囲を持つ(run11・run17 の IT13・IT14)。例示値(S1 が画面の字面から書く)に単位があるほうが、件数を出す項目。"""
    units = [u for u in cd.get("units") or [] if u]
    hit = [i for i in owners if any(u in str(items[i].get("example") or "") for u in units)]
    return hit[0] if len(hit) == 1 else owners[0]


def _count_transform(key, name, cd):
    """件数の写しの transform: 何の件数を、どの項目に、どんな単位の形で、どんなときに出すか。画面の字面(単位・表示条件)は下書きの事実をそのまま使う。"""
    units = [u for u in cd.get("units") or [] if u]
    shape = ("に" + "または".join(f"「N{u}」" for u in units) + "の形で出す") if units else "に出す"
    cond = cd.get("visible_if") or []
    return f"応答 {key} の件数を、{name}{shape}" + (f"。出るのは {' かつ '.join(cond)} のとき" if cond else "")


def _add_count_rows(out, drafts, items, log):
    """画面が配列の件数({rows.length}・${shops.length})を表示する所(下書きの count_displays)に、response_to_screen_mapping の件数の写しを足す(第20回)。
    行: api_field_path=応答の配列の key(response_items にある rows。key が無く <キー>.length だけがあればそれ)・screen_item_name=件数を出す項目・
    transform=単位と表示条件の文(_count_transform)。response_items が無い下書きは <キー>.length。
    足さない(推測しない): その件数を持つ項目(同じ狭さの項目のどれか)に S2 が既に件数の写し(_react_facts.is_count_map)を書いている・
    state が応答のどのキーから入るか決まらない・その応答を返す API が単一の API を呼ぶイベントから決まらない・件数の式を囲む項目が無い。
    WHY: [件数の写し] の WARN は run15・16・17 で毎回 S2 に出た。件数の表示は下書きの事実で全部決まるので、S2 に書かせず機械で足す。冪等(2回目は何も足さない)。"""
    import _react_facts
    cds = [c for d in drafts for c in d.get("count_displays") or [] if isinstance(c, dict) and c.get("state") and c.get("src")]
    if not cds:
        return
    arr_of = _state_keys(drafts)
    apis = [a for a in out.get("api_spec") or [] if isinstance(a, dict) and isinstance(a.get("response_to_screen_mapping"), list)]
    for cd in cds:
        key = arr_of.get(cd["state"])
        owners = _count_owners(items, cd)
        if not key or not owners:
            continue
        names = {items[i].get("item_name") for i in owners}
        maps = [m for a in apis for m in a["response_to_screen_mapping"] if isinstance(m, dict)]
        if any(m.get("screen_item_name") in names and _react_facts.is_count_map(m, key) for m in maps):
            continue
        target = next((a for a in apis if cd["state"] in _states_for(a.get("action"), drafts, key)), None)
        if target is None:
            # 複数の API を呼ぶイベント(初期表示)の失敗経路は、呼出名を API 関数の出典の字面で action へ結ぶ(_call_action)。1つに決まらなければ足さない
            acts = {_call_action(drafts, fe.get("event"), fe.get("call")) for d in drafts for fe in d.get("failure_effects") or []
                    if isinstance(fe, dict) and fe.get("call") and isinstance(fe.get("on_success_from"), dict)
                    and cd["state"] in fe["on_success_from"]} - {None}
            target = next((a for a in apis if a.get("action") in acts), None) if len(acts) == 1 else None
        if target is None:
            continue
        # WHY: api_field_path は response_items の key に無いと verify_partials の [参照] が S2 へ戻す(rows.length は key に無い)。
        #      件数を持つ配列の key(rows)そのものを使い、配列の写しでないことは transform の「件数」が示す(is_count_map は rows も件数の写しと認める)。
        #      key が response_items に無く rows.length だけがあるなら、それを使う。どちらも無ければ足さない(推測しない)
        keys = {x.get("key") for x in target.get("response_items") or [] if isinstance(x, dict)}
        path = next((p for p in (key, f"{key}.length") if p in keys), None) if keys else f"{key}.length"
        if path is None:
            continue
        row = {"api_field_path": path}
        _place(row, items, _pick_owner(items, owners, cd))
        row["transform"] = _count_transform(key, row["screen_item_name"], cd)
        target["response_to_screen_mapping"].append(row)
        log.append(f"[写し] api_spec[{target.get('action')}].response_to_screen_mapping に件数の写し {row['api_field_path']} → {row['screen_item_name']} を足す")


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
                # WHY(run20 差1): calc の出力先に加え、CSV 出力のボタンも付け替えない(S2 が正しく結んだ CSV の写し)
                _write_rows(api, path, cands, items, log, _calc_targets(drafts, items, leaf) | _csv_targets(drafts, items, leaf))
            elif _key_only(usage, states, leaf):
                # WHY: 使用が key だけで候補が無い項目は、モデルが書いた写しの行も消す(V6)
                _drop_rows(api, path, log)
    # 件数の表示の写しは、応答の項目ごとの処理(上)の後で足す(上の処理は "[]." の path だけを見るので、rows.length の行には触れない)
    _add_count_rows(out, drafts, items, log)


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
        import os
        os.environ["FEATURE_NAME"] = "S"  # 下書きは機能名の1本だけ読む(draft_files)。試験の下書きの名前に合わせる
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


def _selftest_count_rows():
    """第20回: 画面が件数を表示する所(下書きの count_displays)に件数の写しを足す。
    (1) 応答のキーに結ばれる API ごとに rows.length の行が足り、transform に単位と表示条件が入る。同じ狭さの2項目は example に単位がある項目を選ぶ。
    (2) 同じ狭さの別の項目に S2 が件数の写しを書いていれば足さない。2回目は何も足さない。
    (3) state が応答のキーに結べない(on_success_from が無い)・囲む項目が無いときは足さない(推測しない)。"""
    import copy
    F = "F.tsx"

    def span(a, b):
        return [{"file": F, "line_start": a, "line_end": b}]
    drafts = [{
        "event_processes": [{"event_code": "EV1", "calls": [{"action": "get_rows"}]}, {"event_code": "EV2", "calls": [{"action": "get_shops"}]}],
        "failure_effects": [{"event": "EV1", "on_success": ["rows"], "on_success_from": {"rows": "rows"}},
                            {"event": "EV2", "on_success": ["shops"], "on_success_from": {"shops": "rows"}}],
        "count_displays": [
            {"state": "rows", "units": ["店舗分", "レジ分"], "visible_if": ["rows.length > 1"], "src": span(3, 3)},
            {"state": "shops", "units": ["店だけ"], "visible_if": [], "src": span(8, 8)},
            {"state": "other", "units": [], "visible_if": [], "src": span(8, 8)}]}]
    items = [{"item_name": "見出し", "screen_no": "1", "example": "合計", "src": span(1, 5)},
             {"item_name": "副題", "screen_no": "1", "example": "2026-10-01 / 11レジ分", "src": span(1, 5)},
             {"item_name": "注記", "screen_no": "1", "example": "POSがあるのは11店だけ", "src": span(7, 9)}]

    def mk(pre=None):
        return {"api_spec": [{"action": "get_rows", "response_to_screen_mapping": list(pre or [])},
                             {"action": "get_shops", "response_to_screen_mapping": []}]}
    out, log = mk(), []
    _add_count_rows(out, drafts, items, log)
    r1, r2 = out["api_spec"][0]["response_to_screen_mapping"], out["api_spec"][1]["response_to_screen_mapping"]
    assert len(r1) == 1 and r1[0]["api_field_path"] == "rows.length" and r1[0]["screen_item_name"] == "副題", r1
    assert r1[0]["transform"] == "応答 rows の件数を、副題に「N店舗分」または「Nレジ分」の形で出す。出るのは rows.length > 1 のとき", r1
    assert len(r2) == 1 and r2[0]["api_field_path"] == "rows.length" and r2[0]["screen_item_name"] == "注記" and r2[0]["screen_no"] == "1", r2
    assert r2[0]["transform"] == "応答 rows の件数を、注記に「N店だけ」の形で出す", r2
    assert len(log) == 2, log
    snap = copy.deepcopy(out)
    log2 = []
    _add_count_rows(out, drafts, items, log2)
    assert out == snap and log2 == [], "2回目で変わった(冪等でない)"
    # S2 が同じ狭さの別の項目(見出し)に件数の写しを書いていれば、副題へ足さない
    own = mk([{"api_field_path": "rows.length", "screen_item_name": "見出し", "transform": "件数を出す"}])
    _add_count_rows(own, drafts, items, [])
    assert len(own["api_spec"][0]["response_to_screen_mapping"]) == 1, own["api_spec"][0]
    # 応答の根が違う写し(items.length)は件数の写しと認めず、足す
    other = mk([{"api_field_path": "items.length", "screen_item_name": "副題", "transform": "x"}])
    _add_count_rows(other, drafts, items, [])
    assert len(other["api_spec"][0]["response_to_screen_mapping"]) == 2, other["api_spec"][0]
    # api_field_path は response_items の key にあるもの: rows があれば rows、rows.length だけなら rows.length、どちらも無ければ足さない
    with_key = mk()
    with_key["api_spec"][0]["response_items"] = [{"key": "rows"}, {"key": "rows[].id"}]
    with_key["api_spec"][1]["response_items"] = [{"key": "rows.length"}]
    _add_count_rows(with_key, drafts, items, [])
    assert [m["api_field_path"] for m in with_key["api_spec"][0]["response_to_screen_mapping"]] == ["rows"], with_key["api_spec"][0]
    assert [m["api_field_path"] for m in with_key["api_spec"][1]["response_to_screen_mapping"]] == ["rows.length"], with_key["api_spec"][1]
    no_key = mk()
    no_key["api_spec"][0]["response_items"] = [{"key": "rows[].id"}]
    _add_count_rows(no_key, drafts, items, [])
    assert no_key["api_spec"][0]["response_to_screen_mapping"] == [], no_key["api_spec"][0]
    # 複数の API を呼ぶイベント(初期表示)は、失敗経路の呼出名を API 関数の出典の字面で action へ結ぶ。結べなければ足さない
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        api_ts = Path(td, "api.ts")
        api_ts.write_text("\n".join(['export function fetchRows() {', '  return call("get_rows", {});', '}',
                                     'export function fetchShops() {', '  return call("get_shops", {});', '}']) + "\n", encoding="utf-8")

        def csrc(n):
            return [{"file": str(api_ts), "line_start": n, "line_end": n}]
        multi = copy.deepcopy(drafts)
        multi[0]["event_processes"] = [{"event_code": "EV9", "calls": [{"action": "get_rows", "src": csrc(2)}, {"action": "get_shops", "src": csrc(5)}]}]
        multi[0]["failure_effects"] = [{"event": "EV9", "call": "fetchRows", "on_success_from": {"rows": "rows"}},
                                       {"event": "EV9", "call": "fetchShops", "on_success_from": {"shops": "rows"}}]
        assert _call_action(multi, "EV9", "fetchShops") == "get_shops" and _call_action(multi, "EV9", "fetchRows") == "get_rows"
        assert _call_action(multi, "EV9", "fetchNone") is None and _call_action(multi, "EV0", "fetchRows") is None
        out_m = mk()
        _add_count_rows(out_m, multi, items, [])
        assert [m["screen_item_name"] for m in out_m["api_spec"][0]["response_to_screen_mapping"]] == ["副題"], out_m["api_spec"][0]
        assert [m["screen_item_name"] for m in out_m["api_spec"][1]["response_to_screen_mapping"]] == ["注記"], out_m["api_spec"][1]
        # 呼出名が出典の近くに無い(結べない)ときは足さない
        multi[0]["failure_effects"][1]["call"] = "fetchElsewhere"
        multi[0]["failure_effects"][0]["call"] = "fetchElsewhere2"
        out_n = mk()
        _add_count_rows(out_n, multi, items, [])
        assert out_n == mk(), out_n
    # on_success_from が無い古い下書き・囲む項目が無い式は足さない
    old = copy.deepcopy(drafts)
    for fe in old[0]["failure_effects"]:
        fe.pop("on_success_from")
    none = mk()
    _add_count_rows(none, old, items, [])
    assert none == mk(), none
    nowhere = mk()
    _add_count_rows(nowhere, drafts, [{"item_name": "遠い項目", "src": span(50, 60)}], [])
    assert nowhere == mk(), nowhere
    print("[selftest OK] _compose_mapping 件数の写しの足し(第20回)")


def _selftest_calc_target():
    """run15 差1: 被演算子 leaf から計算した結果を出す項目(calc の label)への行は、候補に無くても付け替えない。
    出力先でない項目への行は従来どおり候補の1番目へ入れ替える。"""
    items = [{"item_name": "小計", "screen_no": "3"}, {"item_name": "差引", "screen_no": "3"}, {"item_name": "別", "screen_no": "3"}]
    drafts = [{"calc_candidates": [{"id": "CC01", "label": "差引", "expr": "r.a - r.b", "operands": ["a", "b"]},
                                   {"id": "CC02", "label": "存在しない項目", "expr": "r.a", "operands": ["a"]}]}]
    assert _calc_targets(drafts, items, "a") == {"差引"}, _calc_targets(drafts, items, "a")
    assert _calc_targets(drafts, items, "z") == set()
    api = {"action": "x", "response_to_screen_mapping": [
        {"api_field_path": "rows[].a", "screen_item_name": "小計", "transform": "T1"},
        {"api_field_path": "rows[].a", "screen_item_name": "差引", "transform": "引いて表示"},
        {"api_field_path": "rows[].a", "screen_item_name": "別", "transform": "T3"}]}
    log = []
    _write_rows(api, "rows[].a", [(0, None)], items, log, _calc_targets(drafts, items, "a"))
    got = [(m["screen_item_name"], m["transform"]) for m in api["response_to_screen_mapping"]]
    # 差引は残り、候補に無い別だけ小計へ入れ替わる(その結果、小計の行が2つになる)
    assert got == [("小計", "T1"), ("差引", "引いて表示"), ("小計", "T3")], got
    assert len(log) == 1, log
    print("[selftest OK] _compose_mapping calc 出力先の保護(run15 差1)")


def _selftest_fixed_title():
    """run17 差1: 見出し(title="会計レポート")と副題(subtitle={…r.shop_name…})が同じ src(Card 要素の全体)を持つとき、
    応答の欄(shop_name)の写し先は副題だけになる。(1) 候補が副題だけ・見出しへの行は足されない。(2) どちらも固定文でなければ、同じ範囲の両方が残る
    (範囲が同じで決められないものは絞らない)。(3) 例示値が空・リテラルが範囲の外にあるときは固定文とみなさない。(4) 2回目は何も変えない。"""
    import copy
    import tempfile
    src = ("export default function S() {\n"
           "  return (\n"
           "    <div>\n"
           "      {rows.map((r, i) => (\n"
           "        <Card\n"
           "          key={r.shop_id}\n"
           "          title=\"会計レポート\"\n"
           "          subtitle={`${r.shop_name} / ${r.days}日`}\n"
           "        />\n"
           "      ))}\n"
           "      <h2>別の見出し</h2>\n"
           "    </div>\n"
           "  );\n"
           "}\n")
    with tempfile.TemporaryDirectory() as td:
        tsx = Path(td, "S.tsx")
        tsx.write_text(src, encoding="utf-8")
        f = str(tsx)
        whole = [{"file": f, "line_start": 4, "line_end": 10}]
        use = [{"file": f, "line_start": 8, "line_end": 8}]
        title = {"item_name": "見出し", "screen_no": "3", "format": "-", "example": "会計レポート", "src": whole}
        sub = {"item_name": "副題", "screen_no": "3", "format": "-", "example": "A店 / 15日", "src": copy.deepcopy(whole)}
        assert _is_literal_item(title) and not _is_literal_item(sub)
        assert not _is_literal_item(dict(title, example="")), "例示値が空なのに固定文とみなした"
        assert not _is_literal_item(dict(title, example="別の見出し")), "リテラルが範囲の外なのに固定文とみなした"
        assert _is_literal_item({"item_name": "h", "example": "別の見出し", "src": [{"file": f, "line_start": 11, "line_end": 11}]}), "<h2>…</h2> を固定文と認めない"
        # (1) 見出し・副題の順(見出しが先)で、副題だけが候補
        items = [title, sub]
        got = _candidates({"rows"}, "shop_name", [], {"rows[].shop_name": [{"src": use}]}, items, [], {})
        assert [i for i, _ in got] == [1], got
        # (2) どちらも固定文でない同じ範囲は両方残る(例示値が字面のリテラルでないとき)
        other = [dict(title, example="会計カード"), sub]
        got = _candidates({"rows"}, "shop_name", [], {"rows[].shop_name": [{"src": use}]}, other, [], {})
        assert [i for i, _ in got] == [0, 1], got
        # fill の全体: モデルの行が無くても、副題だけに行が足される
        draft = {"event_processes": [{"event_code": "EV1", "calls": [{"action": "get_rows"}]}],
                 "failure_effects": [{"event": "EV1", "on_success": ["rows"], "on_success_from": {"rows": "rows"}}],
                 "field_usage": [{"path": "rows[].shop_name", "uses": [{"element": "Card の subtitle", "src": use}]},
                                 {"path": "rows[].days", "uses": [{"element": "Card の subtitle", "src": use}]}]}
        import os
        os.environ["FEATURE_NAME"] = "S"  # 下書きは機能名の1本だけ読む(draft_files)。試験の下書きの名前に合わせる
        Path(td, "S_react_draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        import _config_loader
        orig = _config_loader.INTERMEDIATE_DIR
        out = {"screen_layout": {"items": copy.deepcopy(items)},
               "api_spec": [{"action": "get_rows", "response_to_screen_mapping": [
                   {"api_field_path": "rows[].shop_name", "screen_item_name": "副題", "transform": "T1", "screen_no": "3"}],
                   "response_items": [{"key": "rows[].shop_name"}, {"key": "rows[].days"}]}]}
        try:
            _config_loader.INTERMEDIATE_DIR = Path(td)
            log = []
            fill(out, log)
            once = json.dumps(out, ensure_ascii=False, sort_keys=True)
            log2 = []
            fill(out, log2)
            assert json.dumps(out, ensure_ascii=False, sort_keys=True) == once and log2 == [], log2
        finally:
            _config_loader.INTERMEDIATE_DIR = orig
    got = [(m["api_field_path"], m["screen_item_name"]) for m in out["api_spec"][0]["response_to_screen_mapping"]]
    assert got == [("rows[].shop_name", "副題"), ("rows[].days", "副題")], got
    print("[selftest OK] _compose_mapping 固定の見出しを写し先にしない(run17 差1)")


def _selftest_csv_target():
    """run20 差1: S2 が写し先を CSV 出力のボタンにした行(店舗名・日数など)は、候補(画面の表示項目)へ付け替えない。
    (1) CSV に出る葉の行は、ボタンが入れ物の中にあっても残る。(2) CSV に出ない葉(.leaf が出力に無い)の行は従来どおり候補へ直す。
    (3) 起動ボタンに結べない csv_exports(event が無い)は何も保護しない。(4) 2回目は何も変えない。"""
    import copy
    F = "/src/P.tsx"

    def sp(a, b=None):
        return [{"file": F, "line_start": a, "line_end": b or a}]
    items = [{"item_name": "副題", "screen_no": "3", "src": sp(318)},
             {"item_name": "操作欄", "screen_no": "3", "src": sp(280, 284)},
             {"item_name": "CSV出力", "screen_no": "3", "src": sp(282)}]
    drafts = [{"events": [{"event_code": "EV07", "src": sp(282)}],
               "csv_exports": [{"function": "exportCsv", "trigger_events": ["EV07"], "row_builder": {"columns": [{"row_value": "r.shop_name"}]}}]}]
    assert _csv_targets(drafts, items, "shop_name") == {"CSV出力"}, _csv_targets(drafts, items, "shop_name")  # 入れ物(操作欄)は含めない
    assert _csv_targets(drafts, items, "days") == set()  # CSV に出ない葉は保護しない
    assert _csv_targets([{"csv_exports": drafts[0]["csv_exports"]}], items, "shop_name") == set()  # 起動ボタンが決まらない
    api = {"action": "x", "response_to_screen_mapping": [
        {"api_field_path": "rows[].shop_name", "screen_item_name": "CSV出力", "transform": "店舗名をCSVに出す"},
        {"api_field_path": "rows[].days", "screen_item_name": "CSV出力", "transform": "日数をCSVに出す"}]}
    log = []
    for leaf in ("shop_name", "days"):
        _write_rows(api, f"rows[].{leaf}", [(0, None)], items, log, _calc_targets(drafts, items, leaf) | _csv_targets(drafts, items, leaf))
    got = [(m["api_field_path"], m["screen_item_name"]) for m in api["response_to_screen_mapping"]]
    # 店舗名は CSV に出るので S2 の行が残り、候補(副題)の行が足される。日数は CSV の出力に無いので、従来どおり候補(副題)へ直る
    assert got == [("rows[].shop_name", "CSV出力"), ("rows[].shop_name", "副題"), ("rows[].days", "副題")], got
    assert len(log) == 2, log
    once = copy.deepcopy(api)
    _write_rows(api, "rows[].shop_name", [(0, None)], items, [], _csv_targets(drafts, items, "shop_name"))
    assert api == once, "2回目で変わった"
    print("[selftest OK] _compose_mapping CSV 出力ボタンへの写しを付け替えない(run20 差1)")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        _selftest_w6()
        _selftest_calc_target()
        _selftest_count_rows()
        _selftest_fixed_title()
        _selftest_csv_target()
    else:
        print("usage: _compose_mapping.py --selftest (fill は _units.compose から呼ばれる)")
        sys.exit(2)
