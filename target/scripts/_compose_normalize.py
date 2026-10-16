#!/usr/bin/env python3
"""合成の最後で、担当が書いた値の表記揺れを機械的に1つにそろえる。

WHY: 同じ版のまま run22 と run23 を回すと、screen_layout.items[].data_type が string と 文字列 で揺れる、
値の無い欄が null と欄なしで揺れる、といった「機械的に決まるべき値」が回ごとにぶれた。
担当の言い回しに任せず、合成の最後に辞書で正規の表記へ寄せる。正解は常にソースで、ここは表記をそろえるだけ。
接続: _units.compose の最後から呼ばれる想定。型の辞書は 16_docx出力ハンドオフ.md §9 と同じ語で、
verify_partials.py の check_data_type_warn が CANONICAL_DATA_TYPES を参照して辞書外の値を WARN にする。
使い方: python3 _compose_normalize.py --selftest。標準ライブラリのみ。
"""
import sys

# WHY: data_type の正規の語。15_中間JSONスキーマ.md のサンプル(string・integer)に合わせた英小文字
CANONICAL_DATA_TYPES = ("string", "integer", "number", "boolean", "date", "datetime", "array", "object", "null")

# WHY: 担当が日本語などで書きがちな別名を正規の語へ引く。キーは小文字・前後空白除去済み(関数側で畳んでから引く)
DATA_TYPE_ALIASES = {
    "文字列": "string",
    "整数": "integer",
    "数値": "number",
    "真偽": "boolean",
    "日付": "date",
    "日時": "datetime",
    "配列": "array",
    "オブジェクト": "object",
}

def normalize_data_types(composed):
    """composed 内の data_type(str)を、別名なら正規の語へ直す。直した箇所の数を返す(in-place)。

    WHY: data_type が string と 文字列 で回ごとに揺れたため、合成の最後で辞書の語にそろえる。
    辞書に無い値・str 以外(None など)は触らない(値を捨てない。WARN は verify_partials の仕事)。
    呼出元: _units.compose の最後。
    """
    n = 0
    stack = [composed]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            v = node.get("data_type")
            if isinstance(v, str):
                key = v.strip().lower()
                canon = DATA_TYPE_ALIASES.get(key) or (key if key in CANONICAL_DATA_TYPES else None)
                if canon is not None and canon != v:
                    node["data_type"] = canon
                    n += 1
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return n

def selftest():
    """normalize_data_types の試験。後続の正規化関数の試験はこの関数の末尾(print の直前)へ足す。"""
    # 別名と大文字混じり・前後空白は正規の語へ。件数は直した箇所の数
    c = {"screen_layout": {"items": [{"data_type": "文字列"}, {"data_type": "整数"}, {"data_type": " String "}]}}
    assert normalize_data_types(c) == 3, c
    assert [i["data_type"] for i in c["screen_layout"]["items"]] == ["string", "integer", "string"], c
    # 正規の語はそのまま(件数0)
    c = {"screen_layout": {"items": [{"data_type": "string"}, {"data_type": "datetime"}]}}
    assert normalize_data_types(c) == 0, c
    # 辞書に無い語と None は変えない(値を捨てない)
    c = {"screen_layout": {"items": [{"data_type": "foo"}, {"data_type": None}]}}
    assert normalize_data_types(c) == 0 and c["screen_layout"]["items"][0]["data_type"] == "foo" and c["screen_layout"]["items"][1]["data_type"] is None, c
    # 入れ子も直す
    c = {"api_spec": [{"response_items": [{"data_type": "配列"}]}]}
    assert normalize_data_types(c) == 1 and c["api_spec"][0]["response_items"][0]["data_type"] == "array", c
    print("[selftest OK] _compose_normalize")
    return 0


# WHY: 値が None の欄を消すと下流が壊れる欄だけをここに入れる。verify_intermediate.py と generate_docx.js の参照を調べた限り
#      (null 判定は screenNo の === null 1 箇所と、verify の step_no 等の is None のみで、欄なしも同じ扱い)、影響する欄は無いので空。
NULL_KEEP_KEYS = frozenset()

# WHY: 空文字(前後の空白だけも含む)の欄も null と同じく欄ごと消すが、「空であること」そのものを書いている欄だけは残す。
#      残す欄(理由):
#        example        画面に出す例示値。空欄として出す項目は「空欄である」ことが例示の内容
#        initial_value  初期値。空欄で始まることが初期値の内容
#        default        API パラメータの既定値。既定値が空文字なのと、既定値が無いのとを区別する
#        default_value / default_text  同上(15_中間JSONスキーマ.md の欄名)
#        value          where などの比較値・リテラル。空文字との比較は条件の内容
#        physical_column  空=物理列を持たない別名・導出列の行。_compose_db_facts の R14、verify_partials、_vs_db がこの空を印として読む
#      消す欄は「無い」を表すだけの欄(length・event_code・format・and_or・param_no・param_name・details_none_reason など)。
#      下流(verify_intermediate.py・merge_partials.py・generate_docx.js)はこれらを真偽で判定し、欄なしも同じ扱い。
EMPTY_KEEP_KEYS = frozenset({"example", "initial_value", "default", "default_value", "default_text", "value", "physical_column"})


def _is_blank_str(v):
    return isinstance(v, str) and v.strip() == ""


def normalize_empty(composed):
    """composed 内の値が None または空文字(空白だけも含む)の dict の欄を、欄ごと消す(in-place)。消した件数を返す。

    WHY: 値の無い欄(aggregate[].over など)が null と欄なしで回ごとに揺れた(run22/run23 で 124 件)。さらに空文字と欄なしも
    揺れた(screen_layout.items[].length・event_code で計約 90 件)ため、どちらも「欄ごと消す」にそろえる。
    list の要素の None・空文字、空配列・0・False は触らない。NULL_KEEP_KEYS(null を残す欄)と
    EMPTY_KEEP_KEYS(空文字を残す欄)は残す。呼出元: _units.compose の最後。
    """
    n = 0
    stack = [composed]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k in [k for k, v in node.items()
                      if (v is None and k not in NULL_KEEP_KEYS) or (_is_blank_str(v) and k not in EMPTY_KEEP_KEYS)]:
                del node[k]
                n += 1
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return n


def selftest_empty():
    """normalize_empty の試験。"""
    c = {"db_operations": [{"aggregate": [{"function": "count", "over": None}]}]}
    assert normalize_empty(c) == 1 and "over" not in c["db_operations"][0]["aggregate"][0], c
    keep = {"aggregate": [{"over": {"partition_by": [], "order_by": []}, "b": [], "c": 0, "d": False, "e": "x", "l": ["", None]}]}
    assert normalize_empty(keep) == 0 and len(keep["aggregate"][0]) == 6 and keep["aggregate"][0]["l"] == ["", None], keep  # 空配列・0・False・値あり・list の要素は触らない
    # 空文字・空白だけの文字列は null と同じく欄ごと消す(欄なしにそろえる)
    c = {"screen_layout": {"items": [{"item_name": "見出し", "length": "", "event_code": "  ", "format": "　", "remarks": None},
                                    {"item_name": "合計", "length": "10"}]}}
    assert normalize_empty(c) == 4, c
    assert c["screen_layout"]["items"] == [{"item_name": "見出し"}, {"item_name": "合計", "length": "10"}], c
    # null と空文字と欄なしの3つが、消した後は同じ形になる
    t1, t2, t3 = {"x": [{"a": None}]}, {"x": [{"a": ""}]}, {"x": [{}]}
    normalize_empty(t1), normalize_empty(t2)
    assert t1 == t2 == t3, (t1, t2, t3)
    # 空であることに意味がある欄(EMPTY_KEEP_KEYS)は空文字のまま残す。ただし null は消す
    k = {"items": [{"example": "", "initial_value": " ", "default": "", "value": "", "physical_column": "", "remarks": ""},
                   {"example": None, "physical_column": None}]}
    assert normalize_empty(k) == 3, k
    assert k["items"] == [{"example": "", "initial_value": " ", "default": "", "value": "", "physical_column": ""}, {}], k
    # 2回目は何も変えない(冪等)
    assert normalize_empty(k) == 0, k
    c = {"x": {"y": [{"z": {"w": None}}]}, "v": None}
    assert normalize_empty(c) == 2 and c == {"x": {"y": [{"z": {}}]}}, c
    assert normalize_empty(c) == 0, c
    print("[selftest OK] _compose_normalize.empty")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        rc = selftest() or selftest_empty()
        if rc:
            sys.exit(rc)
        print("selftest OK")
        sys.exit(0)
    print("使い方: _compose_normalize.py --selftest", file=sys.stderr)
    sys.exit(2)
