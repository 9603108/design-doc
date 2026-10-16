#!/usr/bin/env python3
"""同じ画面の2つの run の中間 JSON を欄ごとに突き合わせ、値が揺れた欄を一覧にする。

WHY: 回し直しで値が揺れる欄は、LLM に書かせている限り毎回ぶれる。揺れた欄を担当の単位つきで
一覧にし、スクリプトで決められる欄(合成・変換の段へ移す候補)を見つけるための道具。
正解は常にソースで、どちらの run も正解扱いしない(ここは差を出すだけで、どちらが正しいかは決めない)。

使い方: python3 diff_runs.py <A.json> <B.json>
出力: 揺れた欄ごとに「道筋 [単位]」と両方の値。末尾に「スクリプトで埋める候補」の見出しと単位ごとの欄の一覧。
終了コード: 比較できれば 0(差があっても 0)。読めない・JSON でないときは 2。

接続: 単位の引き方は run_units.owner_of と load_units(units.json の owns)を再利用する(分担を二重に持たない)。
入力は target/runs/<run名>/ に控えた中間 JSON(run_units.py save-run が作る)を想定する。標準ライブラリのみ。
"""
import json
import sys
from pathlib import Path

from run_units import load_units, owner_of

# WHY: 値の表示は1欄あたりこの長さで切り、一覧を目で追えるようにする(比較は全文で行う)
SHOW_MAX = 80


def flatten(obj, prefix=""):
    """JSON を {欄の道筋: 末端の値} に平らにする。道筋は api_spec[0].description の形(owner_of が読める形)。

    空の dict・list も末端として1欄に数える(片方だけ空のときに差として拾うため)。
    """
    out = {}
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list) and obj:
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    elif prefix or not isinstance(obj, (dict, list)):
        # 根が空の dict・list のときは欄が無いので何も出さない(道筋が空の欄を作らない)
        out[prefix] = obj
    return out


def diff_runs(a, b, units):
    """2つの JSON(読み込み済み)を比べ、揺れた欄の [{path, a, b, unit}] を道筋の順で返す。

    片方に無い欄の値は None でなく専用の印(MISSING)で区別し、「null と書いた」と「欄が無い」を混ぜない。
    unit は run_units.owner_of が units の owns から引いた単位。引けなければ None。
    """
    fa, fb = flatten(a), flatten(b)
    rows = []
    for path in sorted(set(fa) | set(fb)):
        va, vb = fa.get(path, MISSING), fb.get(path, MISSING)
        if va != vb:
            rows.append({"path": path, "a": va, "b": vb, "unit": owner_of(path, units)})
    return rows


# WHY: 欄が片方の run に無いことを、値 null と区別して表すための印(JSON の値としては現れない文字列)
MISSING = "<欄なし>"


def _show(v):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= SHOW_MAX else s[:SHOW_MAX] + "…"


def format_report(rows):
    """揺れた欄の一覧を本文にする。末尾は単位ごとの「スクリプトで埋める候補」。"""
    lines = [f"揺れた欄: {len(rows)} 件"]
    for r in rows:
        lines.append(f"  {r['path']} [{r['unit'] or '担当不明'}]")
        lines.append(f"    A: {_show(r['a'])}")
        lines.append(f"    B: {_show(r['b'])}")
    lines.append("")
    lines.append("=== スクリプトで埋める候補(揺れた欄を単位ごとに) ===")
    by_unit = {}
    for r in rows:
        by_unit.setdefault(r["unit"] or "担当不明", []).append(r["path"])
    for u in sorted(by_unit):
        lines.append(f"  {u}: {len(by_unit[u])} 件")
        for p in by_unit[u]:
            lines.append(f"    - {p}")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--selftest"]:
        selftest()
        print("selftest OK")
        return 0
    if len(argv) != 2:
        print("使い方: diff_runs.py <A.json> <B.json>", file=sys.stderr)
        return 2
    try:
        a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in argv)
    except (OSError, ValueError) as e:
        print(f"読めない: {e}", file=sys.stderr)
        return 2
    print(format_report(diff_runs(a, b, load_units())))
    return 0


def selftest():
    f = flatten({"a": [{"b": 1}, {"b": 2}], "c": {}, "d": "x"})
    assert f == {"a[0].b": 1, "a[1].b": 2, "c": {}, "d": "x"}, f
    # units の owns と同じ道筋の欄は担当の単位が付く(owns を units.json から1つ借りて試す)
    units = load_units()
    u0 = next(iter(units))
    own = units[u0]["owns"][0].replace("[]", "")
    rows = diff_runs({own: "x", "same": 1, "only_a": 1}, {own: "y", "same": 1}, units)
    assert [r["path"] for r in rows] == sorted([own, "only_a"]), rows
    r_own = next(r for r in rows if r["path"] == own)
    assert r_own["unit"] in units and r_own["a"] == "x" and r_own["b"] == "y", r_own
    r_only = next(r for r in rows if r["path"] == "only_a")
    assert r_only["b"] == MISSING and r_only["unit"] is None, r_only
    # null と欄なしは別物
    assert diff_runs({"k": None}, {}, units)[0]["b"] == MISSING
    assert diff_runs({"k": None}, {"k": None}, units) == []
    rep = format_report(rows)
    assert rep.splitlines()[0] == "揺れた欄: 2 件" and "スクリプトで埋める候補" in rep, rep
    assert diff_runs({}, {}, units) == []
    # 読めない入力は終了コード2
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()):
        assert main(["/nonexistent_a.json", "/nonexistent_b.json"]) == 2
        assert main(["x"]) == 2


if __name__ == "__main__":
    sys.exit(main())
