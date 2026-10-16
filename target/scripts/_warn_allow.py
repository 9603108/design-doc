#!/usr/bin/env python3
"""WARN の許可リスト(target/intermediate/{機能名}_warn_allow.json)の読込と照合。

WHY: 検査の WARN は「直さなくてよい理由がある」ものと「見落とし」が混ざる。理由つきで登録した WARN だけを許し、
     登録の無い WARN が残れば不合格にできるようにする(納品の条件「WARN にはすべて説明がつく」)。
WHERE: verify_intermediate.py・verify_vs_source.py の --warn-allow-strict と、run_units.py の gate が使う共通部品(stdlib のみ)。
形: {"allow":[{"key":"WARN行に部分一致する文字列","reason":"空でない説明","date":"YYYY-MM-DD"}]}
"""
import json
import re
import sys
from pathlib import Path

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_allow(path):
    """許可リストを読んで登録の一覧を返す。ファイルが無ければ [] 。
    key・reason・date のどれかが空、または date が YYYY-MM-DD でない登録は ValueError(理由の無い登録は受けない)。"""
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    entries = data.get("allow") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{p}: 'allow' の配列が無い")
    for i, e in enumerate(entries):
        for f in ("key", "reason", "date"):
            v = e.get(f) if isinstance(e, dict) else None
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{p}: allow[{i}].{f} が空(理由の無い登録は受けない)")
        if not DATE_RE.match(e["date"]):
            raise ValueError(f"{p}: allow[{i}].date が YYYY-MM-DD でない: {e['date']}")
    return entries


def split_warns(lines):
    """検査の出力の行から WARN 行(印 [WARN] を含む行)だけを返す。呼出元: 各検査の --warn-allow-strict、gate。"""
    return [ln for ln in lines if "[WARN]" in ln]


def unallowed(warn_lines, allow):
    """許可に当たらない WARN 行を返す。照合は WARN 行に key が部分一致するか。"""
    return [w for w in warn_lines if not any(e["key"] in w for e in allow)]


def selftest():
    import tempfile
    lines = ["[ERROR] x", "  [WARN] api_spec[a].description 長い", "[WARN] db_operations[DB1] 空", "[INFO] i"]
    warns = split_warns(lines)
    assert len(warns) == 2, warns
    allow = [{"key": "api_spec[a].description", "reason": "原文どおり", "date": "2026-10-16"}]
    assert unallowed(warns, allow) == ["[WARN] db_operations[DB1] 空"], unallowed(warns, allow)
    assert unallowed(warns, []) == warns
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x_warn_allow.json"
        assert load_allow(p) == [], "ファイルが無ければ []"
        p.write_text(json.dumps({"allow": allow}), encoding="utf-8")
        assert load_allow(p) == allow
        for bad in ({"key": "", "reason": "r", "date": "2026-10-16"},
                    {"key": "k", "reason": " ", "date": "2026-10-16"},
                    {"key": "k", "reason": "r", "date": ""},
                    {"key": "k", "reason": "r", "date": "10/16"}):
            p.write_text(json.dumps({"allow": [bad]}), encoding="utf-8")
            try:
                load_allow(p)
            except ValueError:
                continue
            raise AssertionError(f"拒否されない: {bad}")
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    print("usage: _warn_allow.py --selftest", file=sys.stderr)
    sys.exit(2)
