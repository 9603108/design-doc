"""応答項目の合成(B3)。H3 の応答の型と S2 の画面への写しを、下書きの事実で補う。

呼出元: _units.compose → _run_fillers(out, log)。この中で fill(out, log) を1回呼ぶ。
ORDER を 110 にする理由: 表示名の補完は論理名(_compose_db_facts.py が補う logical_names)を使う。
  既定の 100 のままだと、ファイル名の順(_compose_db_facts.py が先)にしか頼れないため、順序を数で固定する。
入力: out(compose の途中の dict)。下書きは target/intermediate の *_backend_draft.json と *_react_draft.json を読む
  (_fill_error_codes と同じ読み方)。
書き換えるのは空の欄と未設定の欄だけ。書かれた値は触らない。
  a) api_spec[].response_items[].display_name が空: 画面への写し(同じ path の画面項目名が1つに決まるとき)
     → 同じ別名の列の元の列の論理名 → request_params の同名の説明、の順。どれでも決まらなければ空のまま
     (検査 B14 が H3 へ戻す)
  b) response_items[].source.type が merged で、その API の db 操作が1本なら db にする
  c) response_to_screen_mapping[].screen_item_nth: 同じ名前の表示行が複数あるときだけ、表示行の nth を入れる
"""
import json
import re
from pathlib import Path

ORDER = 110

_ESTIMATE = "(推定)"


def _list(d, key):
    """d[key] の辞書の要素だけを list で返す。欠けは空の list(合成の途中で欄が無い行を落とさないため)。"""
    return [x for x in (d.get(key) or []) if isinstance(x, dict)]


def _leaf(path):
    """response key や api_field_path の末尾の語(rows[].shop_id → shop_id)。react の kin・cnt や別名と同じ語で引くため。"""
    parts = [p for p in re.split(r"[.\[\]]+", str(path or "")) if p]
    return parts[-1] if parts else ""


def _plain(name):
    """論理名から推定の印を外す。(推定) は B2 が表の論理名に付ける印で、列の名前には付けない。"""
    s = str(name or "")
    return s[: -len(_ESTIMATE)] if s.endswith(_ESTIMATE) else s


def _mapped_name(api, key):
    """画面への写し: 同じ api_field_path の行の画面項目名が1つに決まるときだけ返す。
    WHY: 2つ以上の画面項目に出る値は、どの項目の名前が業務の名前か決められない(推測しない)。"""
    names = {m.get("screen_item_name") for m in _list(api, "response_to_screen_mapping")
             if m.get("api_field_path") == key and m.get("screen_item_name")}
    return names.pop() if len(names) == 1 else ""


def _logical_of(out, table, column):
    """(物理表, 物理列)の論理列を composed logical_names から引く。論理名が1つに決まるときだけ返す。"""
    names = {_plain(n.get("logical_column")) for n in _list(out, "logical_names")
             if n.get("physical_table") == table and n.get("physical_column") == column}
    names.discard("")
    return names.pop() if len(names) == 1 else ""


def _alias_logical(out, draft_cols, leaf):
    """同じ別名の列(下書きの db 操作の columns)の元の列の論理名。元の列が1つに決まる列だけを使う。
    WHY: 式が複数の列を参照するとき(SUM(a+b) 等)は、どの列の名前が業務の名前か決まらない。"""
    got = set()
    for c in draft_cols:
        if c.get("alias") != leaf:
            continue
        src = c.get("source_columns") or []
        if len(src) == 1 and "." in str(src[0]):
            table, column = str(src[0]).rsplit(".", 1)
            name = _logical_of(out, table, column)
            if name:
                got.add(name)
    return got.pop() if len(got) == 1 else ""


def _request_desc(api, leaf):
    """request の項目(started_on 等)を返す時は、request_params の同名の説明を業務の名前にする。"""
    descs = {str(p.get("description") or "") for p in _list(api, "request_params")
             if p.get("name") == leaf and p.get("description")}
    return descs.pop() if len(descs) == 1 else ""


def _fill_display_names(out, api, draft_cols, log):
    """response_items の display_name が空の行を、画面への写し・論理名・request の説明で補う(a)。"""
    for x in _list(api, "response_items"):
        if x.get("display_name"):
            continue
        key = x.get("key")
        leaf = _leaf(key)
        name = _mapped_name(api, key) or _alias_logical(out, draft_cols, leaf) or _request_desc(api, leaf)
        if name:
            x["display_name"] = name
            log.append(f"[表示名] api_spec[{api.get('action')}].response_items[{key}] の display_name を {name} で補う")


def _fix_merged_source(api, n_db, log):
    """db 操作が1本の API では、merged(複数の出典の混成)の出典は db の1つに決まる(b)。複数本なら決めない。
    WHY: 1本しか db 操作が無ければ、混成に見えた値はその操作の列から来ている。"""
    if n_db != 1:
        return
    for x in _list(api, "response_items"):
        s = x.get("source")
        if isinstance(s, dict) and s.get("type") == "merged":
            s["type"] = "db"
            log.append(f"[出典] api_spec[{api.get('action')}].response_items[{x.get('key')}] の source.type を merged から db にする")


def _fill_screen_nth(api, specs, log):
    """同じ名前の表示行が複数あるときだけ、画面項目が何番目かを入れる(c)。
    WHY: 「小計」が3つあると、名前だけでは migrate の find_item_no が結ぶ先を決められない。
    名前が1つなら番号は要らない。kin・cnt は応答のどの欄を表示するかの語で、末尾の語と一致する行だけを使う。"""
    nths = {}
    for s in specs:
        nths.setdefault(s.get("label"), set()).add(s.get("nth"))
    for m in _list(api, "response_to_screen_mapping"):
        if m.get("screen_item_nth"):
            continue
        name, leaf = m.get("screen_item_name"), _leaf(m.get("api_field_path"))
        if len(nths.get(name, ())) <= 1 or not leaf:
            continue
        hit = {s.get("nth") for s in specs
               if s.get("label") == name and leaf in (s.get("kin"), s.get("cnt"))}
        if len(hit) == 1:
            m["screen_item_nth"] = hit.pop()
            log.append(f"[画面項目] api_spec[{api.get('action')}].response_to_screen_mapping[{m.get('api_field_path')}] の screen_item_nth を入れる")


def _load_drafts(directory):
    """下書きを読む。戻り: (action → backend 下書きの api の辞書, 表示行の specs の list)。読めないファイルは無いものとして飛ばす。"""
    backend, specs = {}, []
    d = Path(directory)
    for p in sorted(d.glob("*_backend_draft.json")):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for act, a in (b.get("apis") or {}).items():
            if isinstance(a, dict):
                backend.setdefault(act, a)
    for p in sorted(d.glob("*_react_draft.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in r.get("display_rows") or []:
            if isinstance(row, dict):
                specs += [s for s in (row.get("specs") or []) if isinstance(s, dict)]
    return backend, specs


def fill(out, log):
    """api_spec の各行を補う。下書きが無い API は、下書きの db 操作の数が 0 なので b) は動かず、a) の列の参照も空になる。"""
    from _config_loader import INTERMEDIATE_DIR
    backend, specs = _load_drafts(INTERMEDIATE_DIR)
    for api in _list(out, "api_spec"):
        bd = backend.get(api.get("action")) or {}
        dbs = [o for o in (bd.get("db_operations") or []) if isinstance(o, dict)]
        cols = [c for o in dbs for c in (o.get("columns") or []) if isinstance(c, dict)]
        _fill_display_names(out, api, cols, log)
        _fix_merged_source(api, len(dbs), log)
        _fill_screen_nth(api, specs, log)


def _selftest():
    """a) 表示名の4段の順・既存値の保持、b) merged の db 化(1本だけ)、c) nth の付与(同名が複数のときだけ)、
    fill 全体(一時の下書きを INTERMEDIATE_DIR に向けて)を assert する。"""
    import tempfile
    import _config_loader

    # a) 画面への写し(1項目に決まる)→ 論理名(別名の元の列が1つ)→ request の説明 → 補えない → 既存の値は触らない
    api = {"action": "get_x",
           "response_items": [{"key": "rows[].total", "display_name": ""},
                              {"key": "rows[].amount", "display_name": ""},
                              {"key": "started_on", "display_name": ""},
                              {"key": "rows[].zz", "display_name": ""},
                              {"key": "rows[].kept", "display_name": "既存"}],
           "request_params": [{"name": "started_on", "description": "集計開始日"}],
           "response_to_screen_mapping": [{"api_field_path": "rows[].total", "screen_item_name": "合計"},
                                          {"api_field_path": "rows[].amount", "screen_item_name": "小計"},
                                          {"api_field_path": "rows[].amount", "screen_item_name": "件数"}]}
    out = {"api_spec": [api], "logical_names": [{"physical_table": "sales", "physical_column": "amt", "logical_column": "売上額(推定)"}]}
    cols = [{"alias": "amount", "source_columns": ["sales.amt"]}, {"alias": "zz", "source_columns": ["a.b", "a.c"]}]
    log = []
    _fill_display_names(out, api, cols, log)
    got = [x["display_name"] for x in api["response_items"]]
    assert got == ["合計", "売上額", "集計開始日", "", "既存"], got
    assert len(log) == 3, log

    # b) db 操作が1本の API だけ merged を db にする。2本以上・0本は決めない
    for n, want in ((1, "db"), (2, "merged"), (0, "merged")):
        a2 = {"action": "a", "response_items": [{"key": "k", "source": {"type": "merged", "source_ref": "x"}}]}
        _fix_merged_source(a2, n, [])
        assert a2["response_items"][0]["source"]["type"] == want, (n, a2)

    # c) 「小計」が3つ(kin s3・s4・cnt n5)のとき、kin か cnt の末尾の語で nth を引く。「合計」は1つだけなので入れない。
    #    どの表示行にも無い語(s9)は入れない
    specs = [{"label": "小計", "kin": "s3", "cnt": None, "nth": 1},
             {"label": "小計", "kin": "s4", "cnt": None, "nth": 2},
             {"label": "小計", "kin": None, "cnt": "n5", "nth": 3},
             {"label": "合計", "kin": "t1", "cnt": None, "nth": 1}]
    api3 = {"action": "a", "response_to_screen_mapping": [
        {"api_field_path": "rows[].s4", "screen_item_name": "小計"},
        {"api_field_path": "rows[].n5", "screen_item_name": "小計"},
        {"api_field_path": "rows[].t1", "screen_item_name": "合計"},
        {"api_field_path": "rows[].s9", "screen_item_name": "小計"}]}
    _fill_screen_nth(api3, specs, [])
    assert [m.get("screen_item_nth") for m in api3["response_to_screen_mapping"]] == [2, 3, None, None], api3

    # fill 全体: 一時の下書きを読み、表示名・出典・nth が入る。INTERMEDIATE_DIR は必ず戻す
    import json as _json
    orig = _config_loader.INTERMEDIATE_DIR
    with tempfile.TemporaryDirectory() as td:
        Path(td, "X_backend_draft.json").write_text(_json.dumps({"apis": {"get_y": {"db_operations": [
            {"columns": [{"alias": "amount", "source_columns": ["sales.amt"]}]}]}}}, ensure_ascii=False), encoding="utf-8")
        Path(td, "X_react_draft.json").write_text(_json.dumps({"display_rows": [{"specs": specs}]}, ensure_ascii=False), encoding="utf-8")
        _config_loader.INTERMEDIATE_DIR = Path(td)
        try:
            full = {"api_spec": [{"action": "get_y",
                                  "response_items": [{"key": "rows[].amount", "display_name": "",
                                                      "source": {"type": "merged", "source_ref": "x"}}],
                                  "response_to_screen_mapping": [{"api_field_path": "rows[].s4", "screen_item_name": "小計"}]}],
                    "logical_names": [{"physical_table": "sales", "physical_column": "amt", "logical_column": "売上額"}]}
            flog = []
            fill(full, flog)
        finally:
            _config_loader.INTERMEDIATE_DIR = orig
    fa = full["api_spec"][0]
    assert fa["response_items"][0]["display_name"] == "売上額", fa
    assert fa["response_items"][0]["source"]["type"] == "db", fa
    assert fa["response_to_screen_mapping"][0]["screen_item_nth"] == 2, fa
    assert len(flog) == 3, flog


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        print("[selftest OK] _compose_response (B3: 表示名の補完・merged の db 化・screen_item_nth)")
    else:
        print("usage: _compose_response.py --selftest (fill は _units.compose から呼ばれる)")
        sys.exit(2)
