#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_vs_map.py: verify_vs_source の追加検査: 写しの照合(API 応答 → 画面項目の写し・transform など)(stdlib のみ)

【受け持ち】
写しの照合。api_spec[].response_to_screen_mapping の api_field_path・screen_item_name・transform が、
react 下書きの field_usage・display_rows・jsx_elements など画面ソースの事実と合うかを見る。
d(_vs_common.load の戻り)の読み方は _vs_common の docstring。

【検査の足し方】
  1. check(d) -> list[str] の関数を書く。指摘が無ければ []。必要な JSON が d に無いときも [] を返す(何も言わない)。
  2. 行は _vs_common.warn(tag, where, msg) で作る(新しい検査は [WARN]。終了コードに影響しない)。
  3. 関数を CHECKS に足す。
  4. 検査の正例・負例を、引数なしで bool を返す selftest 用の関数に書き、SELFTESTS に足す(selftest() が全部呼ぶ)。

【接続情報】
  呼出元: _vs_checks_ext.run_all / selftest(verify_vs_source.main と --selftest から)
"""
import re
from pathlib import Path

import _vs_common

def _innermost_item(items, file, line):
    """行 line(file の中)を含む画面項目のうち、範囲が最も狭いものを返す。含むものが無ければ None。
    WHY: 画面項目は範囲が入れ子になるので、使われている場所の項目は最も内側のものになる。
    呼出元: check_mapping_target。"""
    best = None
    best_span = None
    for it in items:
        for s in it.get("src") or []:
            lo, hi = s.get("line_start"), s.get("line_end")
            if s.get("file") != file or lo is None or hi is None or not (lo <= line <= hi):
                continue
            if best_span is None or hi - lo < best_span:
                best, best_span = it, hi - lo
    return best


def check_mapping_target(d) -> list:
    """画面ソースで API 応答の項目を使う場所に対し、写しの先(response_to_screen_mapping の screen_item_name)が
    その使われ方の画面項目と一致するかを見る。WHY: 写しの先が外れていると、画面の表示と仕様の対応が静かにずれる。
    key 属性だけの使用は画面の値として使われないので除く。派生配列(derived_arrays)を経由する使い方は、
    その配列名を visible_if に使う jsx 要素の項目へ伝える(配列名から元のキーへ読み替えて照合する)。
    呼出元: _vs_map.run(CHECKS)。d の読み方は _vs_common の docstring。"""
    rd = d.get("react_draft")
    main = d.get("main")
    if not rd or not main:
        return []
    items = (main.get("screen_layout") or {}).get("items") or []
    mappings = []
    for api in main.get("api_spec") or []:
        mappings += api.get("response_to_screen_mapping") or []
    da_map = {a["name"]: a for a in rd.get("derived_arrays") or [] if a.get("name")}
    jsx = rd.get("jsx_elements") or []
    out = []
    for fu in rd.get("field_usage") or []:
        path = fu.get("path") or ""
        root = path.split("[]")[0]
        for use in fu.get("uses") or []:
            if str(use.get("element") or "").endswith("key"):
                continue
            srcs = use.get("src") or []
            if not srcs:
                continue
            file, line = srcs[0].get("file"), srcs[0].get("line_start")
            if root in da_map:
                src_path = (da_map[root].get("source") or "") + path[len(root):]
                expected = None
                for j in jsx:
                    if re.search(r"\b%s\b" % re.escape(root), str(j.get("visible_if") or "")):
                        for s in j.get("src") or []:
                            expected = _innermost_item(items, s.get("file"), s.get("line_start"))
                            if expected is not None:
                                break
                    if expected is not None:
                        break
            else:
                src_path = path
                expected = _innermost_item(items, file, line)
            if expected is None:
                continue
            names = [m.get("screen_item_name") for m in mappings if m.get("api_field_path") == src_path]
            if not names or expected.get("item_name") in names:
                continue
            out.append(_vs_common.warn(
                "写しの先", f"{path} ({file}:{line})",
                f"応答キー {src_path} の写しの先が {names} だが、この使い方は画面項目「{expected.get('item_name')}」で使われている。写しの先を直す"))
    return list(dict.fromkeys(out))


def _selftest_mapping_target() -> bool:
    """check_mapping_target の正例・負例。合成 d で、写しの先が使っている項目と一致すれば指摘なし、外れれば1件。
    WHY: 誤検出を実データの前に合成で確かめる。呼出元: selftest。"""
    f = "/x/Screen.tsx"
    items = [
        {"item_name": "店舗", "src": [{"file": f, "line_start": 10, "line_end": 40}]},
        {"item_name": "店舗名", "src": [{"file": f, "line_start": 20, "line_end": 25}]},
    ]

    def d_with(target):
        return {
            "main": {"screen_layout": {"items": items},
                     "api_spec": [{"response_to_screen_mapping": [
                         {"api_field_path": "rows[].shop_name", "screen_item_name": target}]}]},
            "react_draft": {"field_usage": [{"path": "rows[].shop_name", "uses": [
                {"expr": "r.shop_name", "element": "option", "src": [{"file": f, "line_start": 22, "line_end": 22}]},
                {"expr": "r.id", "element": "option の key", "src": [{"file": f, "line_start": 22, "line_end": 22}]},
            ]}]},
        }
    return check_mapping_target(d_with("店舗名")) == [] and len(check_mapping_target(d_with("店舗"))) == 1


def check_mapping_missing(d) -> list:
    """画面ソースで表示に使われている応答キー(field_usage の path)のうち、api_spec の response_to_screen_mapping に
    1行も写しが無いものを指摘する。WHY: 写しが無いキーは、画面のどの項目に出るかが仕様に残らず、写し漏れとして読めない。
    key 属性だけの使用は表示に使われないので除く。呼出元: _vs_map.run(CHECKS)。d の読み方は _vs_common の docstring。"""
    rd = d.get("react_draft")
    main = d.get("main")
    if not rd or not main:
        return []
    mapped = set()
    for api in main.get("api_spec") or []:
        for m in api.get("response_to_screen_mapping") or []:
            mapped.add(m.get("api_field_path"))
    # WHY: state 名の配列(例 shops)は応答の rows と名前が違い、path 完全一致では写しが見つからない。
    # 根が写しの根にも派生配列名にも無いときだけ、`[].` より後ろの末尾(id・name など)で写しと結ぶ。
    roots = {str(p).split("[]")[0] for p in mapped if p}
    tails = {str(p).split("[].", 1)[1] for p in mapped if p and "[]." in str(p)}
    da_map = {a["name"]: a for a in rd.get("derived_arrays") or [] if a.get("name")}
    out = []
    for fu in rd.get("field_usage") or []:
        path = fu.get("path") or ""
        root = path.split("[]")[0]
        # WHY: 派生配列の根は元の配列(source)に読み替えてから写しと照合する
        if root in da_map and path[len(root):] and (da_map[root].get("source") or "") + path[len(root):] in mapped:
            continue
        if root not in roots and root not in da_map and "[]." in path and path.split("[].", 1)[1] in tails:
            continue
        # WHY: 「rows[].*」は展開(...r)の印で、単一の応答キーではないので写しを求めない
        if path in mapped or "*" in path:
            continue
        shown = [u for u in fu.get("uses") or [] if not str(u.get("element") or "").endswith("key") and u.get("src")]
        if not shown:
            continue
        s = shown[0]["src"][0]
        out.append(_vs_common.warn(
            "写しの漏れ", f"{path} ({s.get('file')}:{s.get('line_start')})",
            f"応答キー {path} は画面で表示に使われているが、api_spec の response_to_screen_mapping に写しが1行も無い。写しを足す"))
    return list(dict.fromkeys(out))


def _selftest_mapping_missing() -> bool:
    """check_mapping_missing の正例・負例。合成 d で、写しが無いキーに1件、写しがあるキーには指摘なし。
    WHY: 誤検出を実データの前に合成で確かめる。呼出元: selftest。"""
    f = "/x/Screen.tsx"

    def d_with(api_path):
        maps = [{"api_field_path": api_path, "screen_item_name": "店舗"}] if api_path else []
        return {
            "main": {"api_spec": [{"response_to_screen_mapping": maps}]},
            "react_draft": {"field_usage": [{"path": "rows[].shop_name", "uses": [
                {"expr": "r.shop_name", "element": "option", "src": [{"file": f, "line_start": 22, "line_end": 22}]},
                {"expr": "r.id", "element": "option の key", "src": [{"file": f, "line_start": 23, "line_end": 23}]},
            ]}]},
        }
    # 根違い: path が shops[].name、写しが rows[].name のときは末尾で結ばれて指摘なし
    alias = d_with("rows[].name")
    alias["react_draft"]["field_usage"][0]["path"] = "shops[].name"
    return (check_mapping_missing(d_with("rows[].shop_name")) == [] and len(check_mapping_missing(d_with(None))) == 1
            and check_mapping_missing(alias) == [])


_ZERO_RE = re.compile(r"(?<![0-9])0(?![0-9])|ゼロ")


def check_transform_zero_fill(d) -> list:
    """表示行(display_rows)の列で、null_as_zero(空を0として表示するか)と、写しの transform に0の扱いが書かれているかが
    食い違うものを指摘する。WHY: 0扱いの片側だけ仕様に残ると、空を0にするかどうかが写しから読めず、画面の表示が仕様と食い違う。
    列は specs の kin・cnt に対応する写し(api_field_path の末尾が列のキー)で結ぶ。transform が「そのまま」で始まるものは対象外。
    呼出元: _vs_map.run(CHECKS)。d の読み方は _vs_common の docstring。"""
    rd = d.get("react_draft")
    main = d.get("main")
    if not rd or not main:
        return []
    mappings = []
    for api in main.get("api_spec") or []:
        mappings += api.get("response_to_screen_mapping") or []
    out = []
    for dr in rd.get("display_rows") or []:
        cf = dr.get("cell_format") or {}
        src = (dr.get("src") or [{}])[0]
        where = f"display_rows[{dr.get('array')}] ({src.get('file')}:{src.get('line_start')})"
        for spec in dr.get("specs") or []:
            for kind in ("kin", "cnt"):
                key = spec.get(kind)
                if not key:
                    continue
                zero = bool((cf.get(kind) or {}).get("null_as_zero"))
                for m in mappings:
                    if m.get("api_field_path") != f"rows[].{key}" or m.get("screen_item_name") != spec.get("label"):
                        continue
                    t = str(m.get("transform") or "")
                    if t.startswith("そのまま"):
                        continue
                    mentioned = bool(_ZERO_RE.search(t))
                    if zero and not mentioned:
                        out.append(_vs_common.warn(
                            "写しの0扱い", f"{where} {m['api_field_path']}",
                            f"列「{spec.get('label')}」({kind}) は空を0にして表示するのに、写しの transform に0の扱いが書かれていない"))
                    elif not zero and mentioned:
                        out.append(_vs_common.warn(
                            "写しの0扱い", f"{where} {m['api_field_path']}",
                            f"列「{spec.get('label')}」({kind}) は空を0にしないのに、写しの transform に0の扱いが書かれている"))
    return list(dict.fromkeys(out))


def _selftest_transform_zero_fill() -> bool:
    """check_transform_zero_fill の正例・負例。合成 d で、null_as_zero と transform の0の記述が揃えば指摘なし、
    片側だけなら指摘が出る。WHY: 誤検出を実データの前に合成で確かめる。呼出元: selftest。"""
    f = "/x/Screen.tsx"

    def d_with(kin_text, cnt_text):
        return {
            "main": {"api_spec": [{"response_to_screen_mapping": [
                {"api_field_path": "rows[].uri_kin", "screen_item_name": "売上訂正", "transform": kin_text},
                {"api_field_path": "rows[].uri_cnt", "screen_item_name": "売上訂正", "transform": cnt_text},
            ]}]},
            "react_draft": {"display_rows": [{"array": "LEFT", "src": [{"file": f, "line_start": 1, "line_end": 2}],
                                              "cell_format": {"kin": {"null_as_zero": True}, "cnt": {"null_as_zero": False}},
                                              "specs": [{"label": "売上訂正", "kin": "uri_kin", "cnt": "uri_cnt"}]}]},
        }
    good = check_transform_zero_fill(d_with("金額を円で表示する。空のときは0", "件数として表示する"))
    bad = check_transform_zero_fill(d_with("金額を円で表示する", "件数として表示する。空のときは0"))
    return good == [] and len(bad) == 2


def _name_core(s) -> str:
    """表示名の芯。括弧の中身と空白・記号(％・・)を落とした語。WHY: 画面の列名は「％１（合計）」のように括弧や記号が付くので、語だけで比べる。
    呼出元: check_response_display_name。"""
    return re.sub(r"[（(][^）)]*[）)]|[\s％%・　]", "", str(s or ""))


def check_response_display_name(d) -> list:
    """API 応答の項目の display_name が、同じキーを描く画面の列(display_rows の specs の kin・cnt)の名前の芯を含むかを見る。
    WHY: 応答の表示名と画面の列名が食い違うと、利用者に見える名前が仕様と別になる。芯が空の名前は比べられないので対象外。
    呼出元: _vs_map.run(CHECKS)。d の読み方は _vs_common の docstring。"""
    rd = d.get("react_draft")
    main = d.get("main")
    if not rd or not main:
        return []
    specs_by_key: dict = {}
    for dr in rd.get("display_rows") or []:
        for sp in dr.get("specs") or []:
            for kind in ("kin", "cnt"):
                if sp.get(kind):
                    specs_by_key.setdefault(sp[kind], []).append((sp, dr))
    out = []
    for api in main.get("api_spec") or []:
        action = api.get("action") or api.get("path") or ""
        for ri in api.get("response_items") or []:
            raw_key = str(ri.get("key") or "")
            key = raw_key.split("[].")[-1]
            disp = str(ri.get("display_name") or "")
            cands = specs_by_key.get(key)
            if not cands or not disp:
                continue
            first = None
            matched = False
            for sp, dr in cands:
                cores = [c for c in [_name_core(sp.get("label")), _name_core(sp.get("csv"))] if c]
                if not cores or any(c in disp for c in cores):
                    matched = True
                    break
                first = first or (sp, dr)
            if matched:
                continue
            sp, dr = first
            src = (dr.get("src") or [{}])[0]
            out.append(_vs_common.warn(
                "表示名の行", f"{action} {raw_key} ({src.get('file')}:{src.get('line_start')})",
                f"応答項目の表示名「{disp}」に、同じキーを描く画面の列「{sp.get('label')}」の名前が含まれない。表示名と列名を揃える"))
    return list(dict.fromkeys(out))


def _selftest_response_display_name() -> bool:
    """check_response_display_name の正例・負例。合成 d で、表示名に列名の芯が入っていれば指摘なし、外れれば1件。
    WHY: 誤検出を実データの前に合成で確かめる。呼出元: selftest。"""
    f = "/x/Screen.tsx"

    def d_with(disp):
        return {
            "main": {"api_spec": [{"action": "sales", "response_items": [
                {"key": "rows[].uri_kin", "display_name": disp}]}]},
            "react_draft": {"display_rows": [{"array": "LEFT", "src": [{"file": f, "line_start": 1, "line_end": 2}],
                                              "specs": [{"label": "売上訂正（合計）", "kin": "uri_kin", "cnt": None, "csv": None}]}]},
        }
    return check_response_display_name(d_with("売上訂正金額")) == [] and len(check_response_display_name(d_with("返品金額"))) == 1


# 検査の本体(check(d) -> list[str])。後で1検査ずつ足す
CHECKS: list = [check_mapping_target, check_mapping_missing, check_transform_zero_fill, check_response_display_name]
# 検査の自己テスト(引数なし・bool を返す)
SELFTESTS: list = [_selftest_mapping_target, _selftest_mapping_missing, _selftest_transform_zero_fill,
                   _selftest_response_display_name]


def run(inter_dir, feature) -> list:
    """d = _vs_common.load(...) を各 check に渡し、指摘の行を連結して返す。呼出元: _vs_checks_ext.run_all。"""
    d = _vs_common.load(Path(inter_dir), feature)
    out = []
    for check in CHECKS:
        out += check(d)
    return out


def selftest() -> bool:
    """SELFTESTS の各関数を全部呼び、全部 True なら True。WHY: 1つ落ちても残りを呼び、どれが落ちたかを1回の実走で全部出せるようにする。"""
    results = [bool(t()) for t in SELFTESTS]
    return all(results)
