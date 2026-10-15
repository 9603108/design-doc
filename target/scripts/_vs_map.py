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
     例外: check_csv_column_name([CSV列名])は、依頼で ERROR とされたため、このファイルの _error で [ERROR] の行を作る(終了コード 1)。
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
    null_as_zero は spec ごとの値を優先し(1列の行は single)、無ければ行単位の cell_format、それも None なら判定外。
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
                # WHY: 0扱いの事実は列(spec)ごとの null_as_zero({kin・cnt} か、1列の行は {single})が正。行単位の
                # cell_format は三項演算の枝が決まらないと None のままになり、bool(None) を「0にしない」と断定すると
                # run14 では全35列(spec は全部 true)が誤検出になった。1列の行(cnt 無し)の kin は single のキーで読む。
                # spec に無いときだけ行単位へ落とし、それも None(未判定)なら「0にしない」とは言えないので対象外にする。
                nz_key = "single" if kind == "kin" and not spec.get("cnt") else kind
                zero = (spec.get("null_as_zero") or {}).get(nz_key)
                if zero is None:
                    zero = (cf.get(nz_key) or {}).get("null_as_zero")
                if zero is None:
                    continue
                zero = bool(zero)
                # WHY(2026-10-15 run15): 同じ API 項目 × 画面項目に写しの行が複数あるのは普通(表示用の行 + 計算の入力として使う行
                # 「小計から合計を引いて表示」「点数で割る小計として使う」)。0の扱いを書くのは表示用の1行でよいので、
                # 1行ずつ見ると、計算用の行が「0扱いが無い」と誤検出になった(syoukei_3・total_2・aridaka・kaisyuu の4件)。
                # 同じ組の写しをまとめて見て、「空を0にする」のに組のどの行にも0の扱いが無いときだけ指摘する。
                group = [m for m in mappings
                         if m.get("api_field_path") == f"rows[].{key}" and m.get("screen_item_name") == spec.get("label")
                         and not str(m.get("transform") or "").startswith("そのまま")]
                if not group:
                    continue
                path = group[0]["api_field_path"]
                mentioned = any(_ZERO_RE.search(str(m.get("transform") or "")) for m in group)
                if zero and not mentioned:
                    out.append(_vs_common.warn(
                        "写しの0扱い", f"{where} {path}",
                        f"列「{spec.get('label')}」({kind}) は空を0にして表示するのに、写しの transform に0の扱いが書かれていない"))
                elif not zero and mentioned:
                    out.append(_vs_common.warn(
                        "写しの0扱い", f"{where} {path}",
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
    # WHY: run14 の誤検出(全35列)の再現。cell_format が None(未判定)でも、spec の null_as_zero が true で
    # transform に0の記述があれば指摘なし。1列の行(cnt 無し)は single で読む。spec が false で0を書けば1件。
    def d_spec(nz, text, cf):
        return {
            "main": {"api_spec": [{"response_to_screen_mapping": [
                {"api_field_path": "rows[].sou_kin", "screen_item_name": "総売上", "transform": text}]}]},
            "react_draft": {"display_rows": [{"array": "LEFT", "src": [{"file": f, "line_start": 1, "line_end": 2}],
                                              "cell_format": cf,
                                              "specs": [{"label": "総売上", "kin": "sou_kin", "cnt": None, "null_as_zero": nz}]}]},
        }
    none_cf = {"kin": {"null_as_zero": None}, "single": {"null_as_zero": None}}
    spec_ok = check_transform_zero_fill(d_spec({"single": True}, "金額として表示する。空のときは0", none_cf)) == []
    # 行単位が None で spec も無ければ判定外(0を書いていても言わない)
    undecided = check_transform_zero_fill(d_spec(None, "金額として表示する。空のときは0", none_cf)) == []
    # spec が false なのに0を書けば1件(spec を優先する。行単位の true には引きずられない)
    spec_false = len(check_transform_zero_fill(d_spec({"single": False}, "空のときは0", {"single": {"null_as_zero": True}}))) == 1
    # WHY(run15 の誤検出の再現): 同じ項目の写しが複数(表示用 + 計算の入力として使う行)で、表示用の行に0の扱いがあれば指摘なし。
    # どの行にも0の扱いが無ければ(組に対して)1件だけ
    def d_multi(texts):
        return {
            "main": {"api_spec": [{"response_to_screen_mapping": [
                {"api_field_path": "rows[].sou_kin", "screen_item_name": "総売上", "transform": t} for t in texts]}]},
            "react_draft": {"display_rows": [{"array": "LEFT", "src": [{"file": f, "line_start": 1, "line_end": 2}],
                                              "cell_format": {},
                                              "specs": [{"label": "総売上", "kin": "sou_kin", "cnt": None, "null_as_zero": {"single": True}}]}]},
        }
    multi_ok = check_transform_zero_fill(d_multi(["金額として出す。値が無いときは0として出す", "小計から合計を引いて表示", "点数で割る総売上として使う"])) == []
    multi_bad = len(check_transform_zero_fill(d_multi(["金額として出す", "小計から合計を引いて表示"]))) == 1
    return good == [] and len(bad) == 2 and spec_ok and undecided and spec_false and multi_ok and multi_bad


# WHY: 「列名「A」「B」」のように「」が続く書き方もあるので、列名の直後に続く「」をまとめて取り、後で1つずつ照合する
_CSV_NAME_RE = re.compile(r"列名[^「」。\n]{0,6}((?:「[^」]+」[、・,]?)+)")
_QUOTED_RE = re.compile(r"「([^」]+)」")


def _csv_col_names(rd):
    """ソースの CSV 見出しの列名(display_rows の specs の csv ?? label。cnt のある列は cnt_suffixes を付けた2列)を
    集合で返す。csv_exports が無い・specs が無いときは None(判定しない)。
    WHY: CSV の見出しは s.csv ?? s.label で作る(PosReport.tsx:200-232)ので、備考の列名はこれと字面が一致しなければならない。
    呼出元: check_csv_column_name。"""
    if not rd.get("csv_exports") or not rd.get("display_rows"):
        return None
    sfx = ["(点数)", "(金額)"]
    for ce in rd["csv_exports"]:
        for sp in (ce.get("header") or {}).get("spread") or []:
            if sp.get("cnt_suffixes"):
                sfx = list(sp["cnt_suffixes"])
    names = set()
    for dr in rd["display_rows"]:
        for spec in dr.get("specs") or []:
            n = spec.get("csv") or spec.get("label")  # fallback-ok: ソースの見出しが s.csv ?? s.label なので、csv が無ければ label を使うのが事実どおり
            if not n:
                continue
            if spec.get("cnt"):
                names.update(n + s for s in sfx)
            else:
                names.add(n)
    if not names:
        return None
    return names


def _error(tag: str, where: str, msg: str) -> str:
    """検査の指摘の1行([ERROR] 付き。verify_vs_source の終了コードが1になる)。
    WHY: _vs_common.warn は [WARN] 専用で ERROR の書式が無く、_vs_common は別の担当の持ち分なので、この検査だけ自前で組む。
    書式は verify_vs_source の他の ERROR 行('[ERROR] [<tag>] <where>: <msg>')と同じ。呼出元: check_csv_column_name。"""
    return f"[ERROR] [{tag}] {where}: {msg}"


def check_csv_column_name(d) -> list:
    """備考(画面項目の remarks・計算の remarks)に書かれた CSV の列名「X」が、ソースの CSV 見出しの列名と
    字面で一致するかを見る(ERROR)。列名は display_rows の specs の csv ?? label(cnt のある列は(点数)(金額)付きの2列)。
    WHY: 論理名の説明文を CSV 列名の欄に入れた備考(run14 の IT54 点数・IT56 客数: 「合計した点数。…」)は、利用者が
    CSV を開いて見る名前と別物になる。FR の step3 の見出し行は正しいのに備考だけ食い違う形は、他の検査では捕まらない。
    「列名」の直後の「」だけを見るので、他の語を「」で引く文は拾わない。csv_exports か display_rows が無ければ何も言わない。
    呼出元: _vs_map.run(CHECKS)。d の読み方は _vs_common の docstring。"""
    rd = d.get("react_draft")
    main = d.get("main")
    if not rd or not main:
        return []
    names = _csv_col_names(rd)
    if names is None:
        return []
    # WHY: 画面項目名が列の label と同じ(括弧書きを除いた芯で一致)なら、期待する列名をその spec の csv ?? label で添える
    by_core: dict = {}
    for dr in rd.get("display_rows") or []:
        for spec in dr.get("specs") or []:
            if spec.get("label"):
                by_core.setdefault(_name_core(spec["label"]), set()).add(spec.get("csv") or spec["label"])  # fallback-ok: 見出しは csv ?? label
    out = []
    holders = [("screen_layout.items", it, it.get("no") or it.get("item_name"), it.get("item_name"))
               for it in (main.get("screen_layout") or {}).get("items") or []]
    holders += [("calculations", c, c.get("no") or c.get("name"), c.get("name"))
                for c in main.get("calculations") or []]
    for kind, obj, ident, label in holders:
        quoted = [x for g in _CSV_NAME_RE.findall(str(obj.get("remarks") or "")) for x in _QUOTED_RE.findall(g)]
        for x in quoted:
            if x in names:
                continue
            exp = sorted(by_core.get(_name_core(label), ()))
            hint = f"この項目に対応する列の CSV 列名は「{'」「'.join(exp)}」" if exp else "ソースの CSV 見出しに無い名前"
            out.append(_error(
                "CSV列名", f"{kind}[{ident}]({label})",
                f"備考の CSV 列名「{x}」が、ソースの CSV 見出し(csv ?? label)と一致しない。{hint}。列名を字面で揃える → 差し戻し先 S1"))
    return list(dict.fromkeys(out))


def _selftest_csv_column_name() -> bool:
    """check_csv_column_name の正例・負例。合成 d で、備考の列名が csv ?? label に一致すれば指摘なし、説明文が入っていれば
    ERROR 1件、cnt のある列は(点数)(金額)付きでも通る。csv_exports が無ければ何も言わない。WHY: 誤検出を実データの前に合成で確かめる。呼出元: selftest。"""
    def d_with(remarks_cnt, remarks_csv, remarks_sum):
        return {
            "main": {"screen_layout": {"items": [
                {"no": "IT1", "item_name": "点数", "remarks": remarks_cnt},
                {"no": "IT2", "item_name": "小計", "remarks": remarks_csv},
                {"no": "IT3", "item_name": "売上訂正", "remarks": remarks_sum}]}},
            "react_draft": {
                "csv_exports": [{"header": {"spread": [{"cnt_suffixes": ["(点数)", "(金額)"]}]}}],
                "display_rows": [{"array": "LEFT", "specs": [
                    {"label": "点数", "csv": None, "kin": "a", "cnt": None},
                    {"label": "小計", "csv": "小計1(値引前)", "kin": "b", "cnt": None},
                    {"label": "売上訂正", "csv": None, "kin": "c", "cnt": "d"}]}],
            },
        }
    good = d_with("CSVでは列名「点数」で出す。", "CSVでは列名「小計1(値引前)」で出す。", "CSVでは列名「売上訂正(点数)」「売上訂正(金額)」で出す。")
    bad = d_with("CSVでは列名「合計した点数。レジ締め会計の行を合算した値」で出す。", "合計の説明。「CSV」とは別の語。", "")
    got = check_csv_column_name(bad)
    nodata = d_with("CSVでは列名「何か」で出す。", "", "")
    nodata["react_draft"].pop("csv_exports")
    return (check_csv_column_name(good) == [] and len(got) == 1 and got[0].startswith("[ERROR] [CSV列名] screen_layout.items[IT1]")
            and "「点数」" in got[0] and "差し戻し先 S1" in got[0] and check_csv_column_name(nodata) == [])


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
CHECKS: list = [check_mapping_target, check_mapping_missing, check_transform_zero_fill, check_response_display_name,
                check_csv_column_name]
# 検査の自己テスト(引数なし・bool を返す)
SELFTESTS: list = [_selftest_mapping_target, _selftest_mapping_missing, _selftest_transform_zero_fill,
                   _selftest_response_display_name, _selftest_csv_column_name]


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
