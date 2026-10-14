"""日付表記の統一(round10 T12)。out の文字列欄にある YYYY/MM/DD・YYYY.MM.DD を、ソースに実在する形へ直す。

呼出元: _units._run_fillers(ORDER=200。scripts/_compose_*.py の中で最後に走る)。
WHY: 本文に書いた日付の区切りが「/」「.」で、ソースの字面は「-」だけ、という食い違いが run9 の備考・計算の文言に出た。
     モデルに直させず、ソースの字面から機械で揃える。直すのは次の場合だけ。
       - 書かれた形がソースのどこにも無く、同じ日付のソースの形が1通りに決まるとき
     次の場合は触らない(推測しない)。
       - ソースに同じ日付が無い(例示値の 2026/10/05 等。事実が無いので直す先が無い)
       - 書かれた形がソースにそのまま有る
       - ソースの形が2通り以上あって1つに決まらない(WARN を残す)
     POS 固有の決め打ちはしない。全て欄の形とソースの字面だけで決める。
"""
import json
import os
import re
import sys
from pathlib import Path

ORDER = 200  # WHY: 他の _compose_*.py が文字列を書き終えた後に走らせる。後から入った日付も揃えるため。

# WHY: 区切りは「-」「/」「.」。前後の区切りを同じものに縛る(2018-12/10 のような混ぜ書きを日付にしない)。
#      前後の数字は連続させない(12018-12-10 の一部を日付にしない)。
_DATE = re.compile(r"(?<!\d)(\d{4})([-/.])(\d{1,2})\2(\d{1,2})(?!\d)")


def _key(m):
    """同じ日付かを比べる鍵。年・月・日を数として比べる(0 埋めの有無は問わない)。"""
    return (m.group(1), int(m.group(3)), int(m.group(4)))


def _source_files(out, feat):
    """照合に使うソースのパス(重複なし・順序を保つ)。
    out の src[].file(合成が各欄に付けたソースの位置)と、機能名 feat の screen_config.source_files。
    WHY: _run_fillers は out と log だけを渡すので、機能名は呼出側(fill)が環境変数から取って渡す。
         機能名が無ければ out の src だけで照合する(ソースを増やさない = 直しすぎない)。"""
    files = []

    def add(p):
        s = str(p)
        if s not in files:
            files.append(s)

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "src" and isinstance(v, list):
                    for s in v:
                        if isinstance(s, dict) and s.get("file"):  # fallback-ok: src の無い行は照合の対象外
                            add(s["file"])
                else:
                    walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(out)
    if feat:
        from _config_loader import get_source_files
        for p in get_source_files(feat):
            add(p)
    return files


def _forms(files):
    """ソースの日付の字面。鍵(年・月・日)ごとに、ソースに実在する形の集合を返す。読めないファイルは飛ばす。"""
    forms = {}
    for f in files:
        try:
            text = Path(f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _DATE.finditer(text):
            forms.setdefault(_key(m), set()).add(m.group(0))
    return forms


def _fix_text(text, path, forms, log):
    """文字列の中の日付を、ソースの形へ直す。直した所と直さなかった所を log に残す。"""
    def rep(m):
        written = m.group(0)
        cands = forms.get(_key(m), set())
        if written in cands or not cands:
            return written
        if len(cands) > 1:
            # WHY: 形が2通りあるとどちらへ直すかが決まらない。推測せず、残して報告する。
            log.append(f"WARN _compose_literals: {path} の「{written}」はソースに2通りの形があるため直さない")
            return written
        new = next(iter(cands))
        log.append(f"INFO _compose_literals: {path} の「{written}」をソースの形「{new}」に直す")
        return new
    return _DATE.sub(rep, text)


def _walk_out(node, path, forms, log):
    """out の全ての文字列欄(再帰)を直す。キー・数値・真偽は触らない。"""
    if isinstance(node, dict):
        for k in list(node):
            node[k] = _walk_out(node[k], f"{path}.{k}" if path else str(k), forms, log)
        return node
    if isinstance(node, list):
        for i, v in enumerate(node):
            node[i] = _walk_out(v, f"{path}[{i}]", forms, log)
        return node
    if isinstance(node, str):
        return _fix_text(node, path, forms, log)
    return node


def _compose(out, files, log):
    """files の字面を照合の元にして out の日付を直す。ソースに日付が1つも無ければ何もしない。"""
    forms = _forms(files)
    if not forms:
        return out
    _walk_out(out, "", forms, log)
    return out


def fill(out, log):
    """_run_fillers から呼ばれる入口。機能名は環境変数 FEATURE_NAME から取る(無ければ out の src だけで照合する)。"""
    _compose(out, _source_files(out, os.environ.get("FEATURE_NAME")), log)


def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = Path(td, "screen.tsx")
        src.write_text(
            "const A = '2018-12-10';\n"
            "const B = '2015-6-5';\n"
            "const C = '2021-12-2';\n"
            "const D = '2021-12-02';\n"
            "const E = '2025-03-01';\n"
            "const F = '2025/03/01';\n",
            encoding="utf-8")
        missing = str(Path(td, "no_such.tsx"))  # 読めないファイルは飛ばす(照合の元にしない)
        out = {
            "screen_layout": {"items": [
                {"remarks": "基準日は2018/12/10以降。"},
                {"remarks": "2015/06/05 の行"},
                {"remarks": "前日 2018/12/10 と 2018-12-10"},
            ]},
            "calculations": [
                {"remarks": "2021/12/2 と 2021/12/02"},   # ソースの形が2通り: 触らない
                {"remarks": "2025/03/01 と 2025-03-01"},  # 書かれた形がソースに有る: 触らない
            ],
            "example": "2026/10/05",                      # ソースに無い日付: 触らない
            "src": [{"file": str(src)}, {"file": missing}],
        }
        files = _source_files(out, None)
        assert files == [str(src), missing], files
        log = []
        _compose(out, files, log)
        items = out["screen_layout"]["items"]
        assert items[0]["remarks"] == "基準日は2018-12-10以降。", items[0]
        assert items[1]["remarks"] == "2015-6-5 の行", items[1]
        assert items[2]["remarks"] == "前日 2018-12-10 と 2018-12-10", items[2]
        assert out["calculations"][0]["remarks"] == "2021/12/2 と 2021/12/02", out["calculations"][0]
        assert out["calculations"][1]["remarks"] == "2025/03/01 と 2025-03-01", out["calculations"][1]
        assert out["example"] == "2026/10/05", out["example"]
        infos = [x for x in log if x.startswith("INFO")]
        warns = [x for x in log if x.startswith("WARN")]
        assert len(infos) == 3, log  # 2018/12/10・2015/06/05・前日の 2018/12/10
        assert len(warns) == 2, log  # 2021 の2つ(形が2通り)
        # 2回目を流しても結果が変わらない(冪等)。直す行は増えない
        snap = json.dumps(out, ensure_ascii=False, sort_keys=True)
        log2 = []
        _compose(out, files, log2)
        assert json.dumps(out, ensure_ascii=False, sort_keys=True) == snap, "not idempotent"
        assert not [x for x in log2 if x.startswith("INFO")], log2
    # 照合の元が無いとき(out の src も機能名も無い)は何もしない
    out2 = {"remarks": "2018/12/10"}
    _compose(out2, _source_files(out2, None), [])
    assert out2["remarks"] == "2018/12/10", out2
    print("[selftest OK] _compose_literals (日付の区切りをソースの形へ。2通り・同形・ソースに無い日付は触らない)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
