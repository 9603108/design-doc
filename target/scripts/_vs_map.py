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
     例外: check_csv_column_name([CSV列名])・check_common_logic_dropped([共通ロジック漏れ]の sql_condition)・check_docstring_count_example([例示値の実数])・
     check_jsx_text_gap([JSXの空白])は、依頼で ERROR とされたため、このファイルの _error で [ERROR] の行を作る(終了コード 1)。
     check_common_logic_dropped の sql_fragment(部品の定数)は WARN。
     行の書式は「[ERROR] [タグ] 場所: 内容 → 差し戻し先 S1」(差し戻し先は msg の末尾)。
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


# ---- 差4: サーバー・DB 側の共通ロジックの候補の漏れ ----------------------------------------------------------
# WHY(run15): backend の SQL が使う定数(取込欠損の条件 _WARN_COND・有効な行 LIVE・集計単位 _GROUPS)は、S1 が common_logic に起こす
# 決まりだが、run15 は CL1〜CL5 が全部ブラウザの処理になり、3件とも丸ごと消えた(run14 までは有った)。下書きの logic_candidates と
# 最終の common_logic を照合しないと、候補の落ちは他の検査で見えない(BE2 の備考・RI12 には文として残るので、残ったように見える)。
# 日本語の語(かな・漢字・カタカナの連なり)。_jp_grams が部分文字列に切る
_JP_RUN = re.compile(r"[぀-ゟァ-ヺー㐀-鿿]+")


def _jp_grams(text) -> set:
    """日本語の語の部分文字列のうち、語として照合に足るものの集合。4字で漢字・カタカナが3字以上、または5字で2字以上。
    WHY: 候補のコメントと common_logic の文を共通の語でつなぐ。ひらがなだけの連なり(「している」)や、漢字1字を含むだけの連なり
    (「現行に無い」「置き換え」)は別の文にも出るので拾わない。run11・run15 の実データで、4字で漢字2字の連なりまで拾うと
    ブラウザの処理の文(「0に置き換えて」)が候補のコメントに一致した。呼出元: _cl_covers。"""
    out = set()
    for run in _JP_RUN.findall(str(text or "")):
        for n, need in ((4, 3), (5, 2)):
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if sum(1 for c in g if not "぀" <= c <= "ゟ") >= need:
                    out.add(g)
    return out


def _src_overlap(a, b) -> bool:
    """出典(src の並び)a と b に、同じファイルで行の範囲が重なるものがあるか。行が欠けた出典は重ならないものとして扱う。"""
    for x in a or []:
        for y in b or []:
            lo1, hi1, lo2, hi2 = x.get("line_start"), x.get("line_end"), y.get("line_start"), y.get("line_end")
            if x.get("file") == y.get("file") and None not in (lo1, hi1, lo2, hi2) and lo1 <= hi2 and lo2 <= hi1:
                return True
    return False


def _cl_covers(cand, cl) -> bool:
    """共通ロジック cl が、backend の候補 cand を拾っているか。次のどれかで拾っているとみなす。
    (1) 出典(src)の行が重なる。(2) 候補の実装の名前(_WARN_COND など)・SQL の列名(seisan_cnt・superseded_by_import_id のように _ を含み8字以上のもの)が
    cl の文に出る。(3) 候補に付けたコメント(下書きの comment)と cl の文に、共通の語(_jp_grams)がある。
    WHY: common_logic の src は空のことがある(run11・run12)ので、行の重なりだけでは拾った候補を落ちと誤る。コメントは業務の語で
    書かれ、S1 が common_logic の名前・説明に使う語と重なる(run8〜13 で確かめた)。cl の文は used_in・used_by_processes を含めない
    (「集計単位の切替」のように、別の処理の名前が候補の語に一致するため)。呼出元: check_common_logic_dropped。"""
    if _src_overlap(cand.get("src"), cl.get("src")):
        return True
    # WHY(2026-10-15 run16): processing_steps は {no・kind・description} の dict の並びのほか、文字列の並び(run16 の CL1〜CL3)で
    # 来ることがある。dict 前提の .get が文字列で落ち、_vs_map の検査が丸ごと走らなかった。文字列はそのまま文として読む。
    steps = [s.get("description") if isinstance(s, dict) else s for s in cl.get("processing_steps") or []]
    body = " ".join(str(x) for x in [cl.get("name"), cl.get("description"), cl.get("expression"), cl.get("null_handling"),
                                     cl.get("zero_division"), cl.get("remarks")] + steps if x)
    ids = {cand.get("name")} | {t for t in re.findall(r"[A-Za-z]+(?:_[A-Za-z0-9]+)+", str(cand.get("text") or "")) if len(t) >= 8}
    if any(t and len(t) >= 4 and t in body for t in ids):
        return True
    # WHY(第19回): 画面側の関数名(num・fmt)は3字で、上の4字以上の部分一致では拾えない。3字の名前は別の語の一部と誤るので、前後が英数字でないときだけ拾う
    if any(t and len(t) == 3 and re.search(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", body) for t in ids):
        return True
    return bool(_jp_grams(cand.get("comment")) & _jp_grams(body))


def check_common_logic_dropped(d) -> list:
    """backend の下書きの logic_candidates(SQL に入る共通の条件・列の表)のうち、この画面の API が使うものが、common_logic に
    拾われていない(_cl_covers が真になる項目が1つも無い)ものを指摘する(kind が sql_condition なら ERROR、
    sql_fragment(表名の対応・列の並びなどの部品の定数)なら WARN。差し戻し先は S1)。
    この画面の API = main の api_spec の action。候補の使い手は下書きの used_by(API の辿った関数が名前を使う action)で決め、
    used_by が無い古い下書きは、候補のファイルが画面の API のファイルかで決める。
    WHY: 取込欠損の判定・有効な会計行・集計単位のようなサーバー・DB 側の規則が common_logic から丸ごと消えても、他の検査は何も言わない(run15 の差4)。
    main か backend_draft か、この画面の API(下書きの apis に出典のあるもの)が無ければ何も言わない。
    呼出元: _vs_map.run(CHECKS)。d の読み方は _vs_common の docstring。"""
    bd, main = d.get("backend_draft"), d.get("main")
    if not bd or not main:
        return []
    actions = {a.get("action") for a in main.get("api_spec") or [] if a.get("action")}
    apis = bd.get("apis") or {}
    files = {s.get("file") for a in actions for s in (apis.get(a) or {}).get("src") or []}
    if not actions or not files:
        return []
    cls = main.get("common_logic") or []
    out = []
    for cand in bd.get("logic_candidates") or []:
        srcs = cand.get("src") or [{}]
        if cand.get("used_by") is not None:
            if not set(cand["used_by"]) & actions:
                continue
        elif srcs[0].get("file") not in files:
            continue
        if any(_cl_covers(cand, cl) for cl in cls):
            continue
        s = srcs[0]
        where = f"logic_candidates[{cand.get('name')}] ({Path(str(s.get('file') or '')).name}:{s.get('line_start')}-{s.get('line_end')})"
        cm = str(cand.get("comment") or "")
        msg = (f"この画面の API({', '.join(sorted(set(cand.get('used_by') or []) & actions or actions))})が使う共通の規則 {cand.get('name')}"
               f"({str(cand.get('text') or '')[:50]})が、common_logic のどの項目にも対応していない"
               f"(src の行が重ならず、名前・コメントの語{('「' + cm[:30] + '…」') if cm else ''}も common_logic の文に出ない)。"
               f"サーバー・DB 側の規則を common_logic に足す(runs_on はデータベース) → 差し戻し先 S1")
        # WHY: sql_condition は SQL の条件・集計の事実(取込欠損の判定など)で、落ちると仕様が事実と食い違うので ERROR。
        # それ以外(sql_fragment: 表名の対応・列の並びなど部品の定数)は、common_logic の1項目に立てるか文に織り込むかが設計の判断で、
        # 無いこと自体は誤りと言い切れないので WARN に下げる(終了コードに影響しない)。
        out.append(_error("共通ロジック漏れ", where, msg) if cand.get("kind") == "sql_condition"
                   else _vs_common.warn("共通ロジック漏れ", where, msg))
    return list(dict.fromkeys(out))


def _selftest_common_logic_dropped() -> bool:
    """check_common_logic_dropped の正例・負例。合成 d で、候補が common_logic の src・コメントの語に拾われていれば指摘なし、
    拾われていなければ ERROR、この画面の API が使わない候補は対象外、used_by の無い古い下書きは候補のファイルで決める。
    WHY: 誤検出を実データの前に合成で確かめる。呼出元: selftest。"""
    f = "/x/queries_pos.py"

    def cand(name, line, comment, used_by):
        c = {"name": name, "kind": "sql_condition", "text": "p.seisan_cnt = 0", "src": [{"file": f, "line_start": line, "line_end": line}],
             "comment": comment}
        if used_by is not None:
            c["used_by"] = used_by
        return c

    def d_with(cls, used_by=("list",), extra=()):
        return {"main": {"api_spec": [{"action": "list"}], "common_logic": cls},
                "backend_draft": {"apis": {"list": {"src": [{"file": f, "line_start": 1, "line_end": 2}]}},
                                  "logic_candidates": [cand("_WARN", 79, "取込欠損の疑いがある日の判定条件", list(used_by)),
                                                       cand("LIVE", 85, "取り直しで置き換えた古い行には印が立つ", list(used_by))] + list(extra)}}
    by_src = {"name": "疑い日の判定", "src": [{"file": f, "line_start": 66, "line_end": 79}]}
    by_word = {"name": "有効な会計行", "description": "取り直しで置き換えた古い行は数えない", "src": []}
    browser = {"name": "全行の合算", "description": "表示中の全行の数値項目を置き換える", "src": [{"file": "/x/Screen.tsx", "line_start": 5, "line_end": 9}]}
    ok = check_common_logic_dropped(d_with([by_src, by_word, browser])) == []
    one = check_common_logic_dropped(d_with([by_src, browser]))
    none_ = check_common_logic_dropped(d_with([browser]))
    # 他の API だけが使う候補・used_by が空の候補は、この画面では求めない
    other = check_common_logic_dropped(d_with([by_src, by_word], used_by=("other",)))
    # used_by が無い古い下書き: 候補のファイルが画面の API のファイルなら求め、別のファイルなら求めない
    old = d_with([browser], extra=())
    for c in old["backend_draft"]["logic_candidates"]:
        c.pop("used_by")
    old_hits = len(check_common_logic_dropped(old))
    old["backend_draft"]["logic_candidates"][0]["src"][0]["file"] = "/x/other.py"
    old["backend_draft"]["logic_candidates"][1]["src"][0]["file"] = "/x/other.py"
    old_other = check_common_logic_dropped(old)
    # SQL の列名(seisan_cnt)が common_logic の文に出れば拾っているとみなす
    by_id = {"name": "判定", "description": "seisan_cnt が0の日", "src": []}
    by_ident = check_common_logic_dropped(d_with([by_id, by_word]))
    nodata = check_common_logic_dropped({"main": {"api_spec": [{"action": "list"}]}}) == []
    # 部品の定数(sql_fragment: 表名の対応・列の並び)は、拾われていなくても WARN に留まる。sql_condition は ERROR のまま
    frag = cand("_COLS", 90, "列の並び", ["list"])
    frag["kind"] = "sql_fragment"
    frag["text"] = '["a", "b"]'
    mixed = check_common_logic_dropped(d_with([by_src, by_word, browser], extra=(frag,)))
    frag_warn = len(mixed) == 1 and mixed[0].startswith("[WARN] [共通ロジック漏れ]") and "_COLS" in mixed[0]
    cond_err = check_common_logic_dropped(d_with([by_src, browser], extra=(frag,)))
    cond_err_ok = (len([x for x in cond_err if x.startswith("[ERROR]")]) == 1 and len([x for x in cond_err if x.startswith("[WARN]")]) == 1)
    # WHY(run16 の例外): processing_steps が文字列の並びでも落ちず、その文の語(コメントの語)で拾う。dict と文字列が混ざっても落ちない
    str_steps = {"name": "有効な行", "processing_steps": ["取り直しで置き換えた古い行には印が付く", {"description": "印の無い行だけ数える"}], "src": []}
    str_ok = check_common_logic_dropped(d_with([by_src, str_steps])) == []
    return (ok and str_ok and len(one) == 1 and "LIVE" in one[0] and one[0].startswith("[ERROR] [共通ロジック漏れ]") and "差し戻し先 S1" in one[0]
            and len(none_) == 2 and other == [] and old_hits == 2 and old_other == [] and by_ident == [] and nodata
            and frag_warn and cond_err_ok)


def check_front_logic_dropped(d) -> list:
    """画面側の下書き(react_draft)の logic_candidates(2か所以上から使う非 JSX の関数。num・fmt・todayJst など)のうち、
    common_logic に拾われていない(_cl_covers が真になる項目が1つも無い)ものを指摘する([WARN]・差し戻し先 S1)。
    WHY(run16): S1 が backend の候補(SQL の条件 3件)だけを common_logic に書き、画面側の共通の処理(値が無いときの0扱い・3桁区切り・
    本日の決め方。run15 までの CL にあった)が丸ごと消えた。check_common_logic_dropped は backend の候補しか見ないので、画面側の落ちは
    どの検査も言わなかった。画面側の候補は下書き(extract_react_draft → _react_facts.logic_candidates)が事実として出す。
    WARN にする理由: 関数ごとに common_logic の1項目に立てるか、1項目にまとめるか(「値が無いときは0」と「3桁区切り」を別にするか)は
    設計の判断で、落ちていること自体が誤りと言い切れる backend の sql_condition(ERROR)とは違うため。終了コードには影響しない。
    main に common_logic の欄が無いとき(合成の前)・react_draft に logic_candidates が無いときは何も言わない。
    呼出元: _vs_map.run(CHECKS)。d の読み方は _vs_common の docstring。"""
    rd, main = d.get("react_draft"), d.get("main")
    if not rd or not main or main.get("common_logic") is None:
        return []
    cls = [cl for cl in main.get("common_logic") or [] if isinstance(cl, dict)]
    out = []
    for cand in rd.get("logic_candidates") or []:
        if any(_cl_covers(cand, cl) for cl in cls):
            continue
        s = (cand.get("src") or [{}])[0]
        where = f"react_draft.logic_candidates[{cand.get('name')}] ({Path(str(s.get('file') or '')).name}:{s.get('line_start')}-{s.get('line_end')})"
        out.append(_vs_common.warn(
            "共通ロジック漏れ", where,
            f"画面側の共通の処理 {cand.get('name')}({str(cand.get('text') or '')[:50]})が、common_logic のどの項目にも対応していない"
            f"(src の行が重ならず、名前も common_logic の文に出ない)。画面で動く共通ロジックとして足す"
            f"(runs_on はブラウザ。src は定義の行 {s.get('line_start')}-{s.get('line_end')}) → 差し戻し先 S1"))
    return list(dict.fromkeys(out))


def _selftest_front_logic_dropped() -> bool:
    """check_front_logic_dropped の正例・負例。合成 d で、画面側の候補が common_logic の src の行・関数名に拾われていれば指摘なし、
    落ちていれば WARN(差し戻し先 S1)。backend の3件だけを書いた run16 の形(画面側が丸ごと無い)で、num・fmt・todayJst の3件が出る。
    WHY: 誤検出を実データの前に合成で確かめる。呼出元: selftest。"""
    f = "/x/Screen.tsx"

    def cand(name, line, end=None, comment=""):
        return {"name": name, "kind": "frontend_function", "text": f"const {name} = ...", "runs_on": "ブラウザ", "comment": comment,
                "src": [{"file": f, "line_start": line, "line_end": end or line}]}
    cands = [cand("num", 92), cand("fmt", 93), cand("todayJst", 107, 109, "本日の日付の決め方")]
    db_only = [{"name": "判定", "description": "取込欠損の日", "src": [{"file": "/x/queries_pos.py", "line_start": 79, "line_end": 79}]}]

    def d_with(cls, cs=cands):
        return {"main": {"common_logic": cls}, "react_draft": {"logic_candidates": cs}}
    none_ = check_front_logic_dropped(d_with(db_only))
    by_src = [{"name": "値が無いときは0", "src": [{"file": f, "line_start": 92, "line_end": 92}]},
              {"name": "3桁区切り", "src": [{"file": f, "line_start": 93, "line_end": 93}]},
              {"name": "本日", "src": [{"file": f, "line_start": 107, "line_end": 109}]}]
    # 行が重ならなくても、関数名(3字は前後が英数字でないとき)・コメントの語が文に出れば拾っているとみなす
    by_word = [{"name": "値の0扱い", "description": "num() で値が無いときは0にする", "src": []},
               {"name": "整形", "description": "fmt で3桁区切りにする", "src": []},
               {"name": "本日の日付(JST基準)", "description": "現在時刻に9時間を足す", "src": []}]
    # 3字の名前は別の語の一部では拾わない(number の中の num)
    partial = [{"name": "数", "description": "number の扱い", "src": []}, by_word[1], by_word[2]]
    one = check_front_logic_dropped(d_with(partial))
    return (len(none_) == 3 and all(x.startswith("[WARN] [共通ロジック漏れ] react_draft.logic_candidates[") and x.endswith("差し戻し先 S1") for x in none_)
            and check_front_logic_dropped(d_with(by_src)) == [] and check_front_logic_dropped(d_with(by_word)) == []
            and len(one) == 1 and "num" in one[0]
            and check_front_logic_dropped({"main": {}, "react_draft": {"logic_candidates": cands}}) == []
            and check_front_logic_dropped({"main": {"common_logic": []}, "react_draft": {}}) == []
            and check_front_logic_dropped({"main": {"common_logic": []}}) == [])


# ---- 差5: 実行時の値を含む文言の例示値(店数など)の照合 ----------------------------------------------------------
def _item_texts(main):
    """画面項目の example と、initialization の initial_value の文字列を (場所, 種別, 文字列) で返す。呼出元: 差5 の2つの検査。"""
    out = []
    for it in (main.get("screen_layout") or {}).get("items") or []:
        out.append((f"screen_layout.items[{it.get('no') or it.get('item_name')}]({it.get('item_name')})", "example", str(it.get("example") or "")))
    for it in (main.get("initialization") or {}).get("items") or []:
        out.append((f"initialization.items[{it.get('no') or it.get('item_name')}]({it.get('item_name')})", "initial_value", str(it.get("initial_value") or "")))
    return out


def check_docstring_count_example(d) -> list:
    """画面項目の例示値(example)に書かれた数が、ソース(docstring・コメント)の実数と食い違うものを指摘する(ERROR・差し戻し先 S1)。
    実数は下書き(backend_draft)の docstring_counts({lead=数の直前の語, count, unit})。例示値に「<lead の末尾>(空白)<数><単位>」の形があり、
    数が count と違えば食い違い。lead が4字未満の実数は手掛かりにならないので見ない。
    WHY(run15 の差5): IT65 注記3 の例示値「POSがあるのは 10店」は、docstring「実際にPOSがあるのは11店」と食い違った。S1 の手順書は
    「実行時の値を含む文言の例示値は、ソースのコメントに実数があればそれを使う」「docstring の店数と食い違う値を避ける」と決めている。
    initialization の initial_value は、取得前の状態(店数 0)の画面の値で実数とは別なので見ない。
    呼出元: _vs_map.run(CHECKS)。d の読み方は _vs_common の docstring。"""
    bd, main = d.get("backend_draft"), d.get("main")
    if not bd or not main:
        return []
    out = []
    for f in bd.get("docstring_counts") or []:
        lead = str(f.get("lead") or "")
        if len(lead) < 4 or not f.get("unit"):
            continue
        rx = re.compile(re.escape(lead[-8:]) + r"\s*([0-9]+)\s*" + re.escape(str(f["unit"])))
        s = (f.get("src") or [{}])[0]
        for where, kind, text in _item_texts(main):
            if kind != "example":
                continue
            for m in rx.finditer(text):
                if int(m.group(1)) == f.get("count"):
                    continue
                out.append(_error(
                    "例示値の実数", where,
                    f"例示値「…{m.group(0)}」の数 {m.group(1)} が、ソースの {f.get('kind')} の実数「{f.get('text')}」"
                    f"({Path(str(s.get('file') or '')).name}:{s.get('line_start')})の {f.get('count')} と食い違う。"
                    f"実行時の値を含む文言の例示値は、ソースのコメント・docstring の実数を使う → 差し戻し先 S1"))
    return list(dict.fromkeys(out))


def _selftest_docstring_count_example() -> bool:
    """check_docstring_count_example の正例・負例。合成 d で、例示値の数が docstring の実数と同じなら指摘なし、違えば1件。
    initial_value の数(0)は見ない。lead が短い実数は見ない。WHY: 誤検出を実データの前に合成で確かめる。呼出元: selftest。"""
    fact = {"lead": "実際にPOSがあるのは", "count": 11, "unit": "店", "text": "実際にPOSがあるのは11店", "kind": "docstring",
            "src": [{"file": "/x/queries_pos.py", "line_start": 154, "line_end": 154}]}
    short = {"lead": "約", "count": 3, "unit": "店", "text": "約3店", "kind": "comment", "src": []}

    def d_with(example, initial="…POSがあるのは0店だけ"):
        return {"main": {"screen_layout": {"items": [{"no": "IT65", "item_name": "注記3", "example": example}]},
                         "initialization": {"items": [{"no": "65", "item_name": "注記3", "initial_value": initial}]}},
                "backend_draft": {"docstring_counts": [fact, short]}}
    same = check_docstring_count_example(d_with("現行は…、POSがあるのは11店だけなので"))
    diff = check_docstring_count_example(d_with("現行は…、POSがあるのは 10店だけなので"))
    # 空白の有無は数の食い違いとは別(JSX の空白の検査が見る)。数が同じなら空白があっても指摘なし
    gap_same = check_docstring_count_example(d_with("POSがあるのは 11店だけ"))
    unrelated = check_docstring_count_example(d_with("レジが3店ある"))
    return (same == [] and gap_same == [] and unrelated == [] and len(diff) == 1 and diff[0].startswith("[ERROR] [例示値の実数] screen_layout.items[IT65]")
            and "10" in diff[0] and "11" in diff[0] and "差し戻し先 S1" in diff[0] and "queries_pos.py:154" in diff[0]
            and check_docstring_count_example({"main": {}}) == [])


def check_jsx_text_gap(d) -> list:
    """実行時の値を含む文言(JSX の {式} とその前後の地の文)の例示値・初期値で、地の文と値の間の空白が、JSX の変換規則の字面と
    食い違うものを指摘する(ERROR・差し戻し先 S1)。字面は下書き(react_draft)の jsx_text_joins({before, after, gap_before, gap_after})。
    例示値・初期値に「<before の末尾の語>(空白)<値>」の形があり、空白の有無が gap_before と違えば食い違い。after 側も同じ。
    WHY(run15 の差5): JSX は改行を挟んだ {式} の前後に空白を入れない(「POSがあるのは(改行){shops.length}店」は画面で「POSがあるのは10店」)のに、
    例示値は「POSがあるのは 10店」と空白を足した。固定文言の Label には合成が JSX から字面を付け直しているが、実行時の値を含む文言には当たっていない。
    対象は screen_layout.items[].example と initialization.items[].initial_value。語が3字未満の前後は手掛かりにならないので見ない。
    呼出元: _vs_map.run(CHECKS)。d の読み方は _vs_common の docstring。"""
    rd, main = d.get("react_draft"), d.get("main")
    if not rd or not main:
        return []
    out = []
    for jx in rd.get("jsx_text_joins") or []:
        tail = str(jx.get("before") or "").rstrip()[-6:]
        head = str(jx.get("after") or "").lstrip()[:6]
        pats = []
        if len(tail) >= 3:
            pats.append(("前", re.compile(re.escape(tail) + r"(\s*)\S"), bool(jx.get("gap_before"))))
        if len(head) >= 3:
            pats.append(("後", re.compile(r"\S(\s*)" + re.escape(head)), bool(jx.get("gap_after"))))
        for where, kind, text in _item_texts(main):
            for side, rx, gap in pats:
                for m in rx.finditer(text):
                    if bool(m.group(1)) == gap:
                        continue
                    seen = "空白が入る" if m.group(1) else "空白が入らない"
                    want = "空白が入る" if gap else "空白が入らない"
                    s = (jx.get("src") or [{}])[0]
                    out.append(_error(
                        "JSXの空白", f"{where} {kind}",
                        f"{{{jx.get('expr')}}} の{side}の空白: {kind}「…{text[m.start():m.end() + 4]}…」は{seen}が、JSX の規則("
                        f"{Path(str(s.get('file') or '')).name}:{s.get('line_start')}-{s.get('line_end')})では{want}"
                        f"(改行の前後の空白は画面に出ない)。字面を JSX に合わせる → 差し戻し先 S1"))
    return list(dict.fromkeys(out))


def _selftest_jsx_text_gap() -> bool:
    """check_jsx_text_gap の正例・負例。合成 d で、JSX が空白を入れない式の前後に空白があれば ERROR、無ければ指摘なし。
    同じ行の空白を JSX が残す式(gap_before が真)では、空白が無い例示値を指摘する。WHY: 誤検出を実データの前に合成で確かめる。呼出元: selftest。"""
    src = [{"file": "/x/PosReport.tsx", "line_start": 337, "line_end": 338}]
    tight = {"expr": "shops.length", "before": "全店舗を出すが、POSがあるのは", "after": "店だけなのでその店だけを出している。",
             "gap_before": False, "gap_after": False, "src": src}
    loose = {"expr": "n", "before": "ありました ", "after": " 件です", "gap_before": True, "gap_after": True, "src": src}

    def d_with(example, initial="", joins=(tight, loose)):
        return {"main": {"screen_layout": {"items": [{"no": "IT65", "item_name": "注記3", "example": example}]},
                         "initialization": {"items": [{"no": "65", "item_name": "注記3", "initial_value": initial}]}},
                "react_draft": {"jsx_text_joins": list(joins)}}
    good = check_jsx_text_gap(d_with("全店舗を出すが、POSがあるのは11店だけなのでその店だけを出している。"))
    bad = check_jsx_text_gap(d_with("全店舗を出すが、POSがあるのは 10店だけなのでその店だけを出している。"))
    both = check_jsx_text_gap(d_with("全店舗を出すが、POSがあるのは 10店だけなので", "全店舗を出すが、POSがあるのは 0店だけなので"))
    # 地の文と値が同じ行にあり、JSX が空白を残す式: 空白が無い例示値は食い違い
    keep_bad = check_jsx_text_gap(d_with("ありました3 件です", joins=(loose,)))
    keep_ok = check_jsx_text_gap(d_with("ありました 3 件です", joins=(loose,)))
    # 前後の語が3字未満・例示値に前の語が無いときは何も言わない
    short = dict(tight, before="は", after="店")
    nothing = check_jsx_text_gap(d_with("POSがあるのは 10店だけ", joins=(short,))) == [] and check_jsx_text_gap(d_with("関係の無い文", joins=(tight,))) == []
    return (good == [] and len(bad) == 1 and bad[0].startswith("[ERROR] [JSXの空白] screen_layout.items[IT65]") and "差し戻し先 S1" in bad[0]
            and len(both) == 2 and "initialization.items[65]" in both[1] and len(keep_bad) >= 1 and keep_ok == [] and nothing
            and check_jsx_text_gap({"main": {}}) == [])


# 検査の本体(check(d) -> list[str])。後で1検査ずつ足す
CHECKS: list = [check_mapping_target, check_mapping_missing, check_transform_zero_fill, check_response_display_name,
                check_csv_column_name, check_common_logic_dropped, check_front_logic_dropped, check_docstring_count_example, check_jsx_text_gap]
# 検査の自己テスト(引数なし・bool を返す)
SELFTESTS: list = [_selftest_mapping_target, _selftest_mapping_missing, _selftest_transform_zero_fill,
                   _selftest_response_display_name, _selftest_csv_column_name, _selftest_common_logic_dropped,
                   _selftest_front_logic_dropped, _selftest_docstring_count_example, _selftest_jsx_text_gap]


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
