#!/usr/bin/env python3
# 目的: 意味の判定が要る記述を、1件ずつの判定項目(audit claim)として書き出す。
# 意味合い: design-doc の audit 段(Haiku)が、記述(text)とソースの行(src_excerpt)を並べて OK / NG を判定するための入力。
#           機械で決まる形の検査(verify_* 系)では止められない型（画面項目の備考・FR の手順・CL の説明・VL の rule・計算の備考）を対象にする。
# 接続情報: 入力 = target/intermediate/<機能名>.json / ソース = 各行の src（file・line_start・line_end）
#           出力 = target/intermediate/<機能名>_audit_claims.json
#           キャッシュ = target/intermediate/<機能名>_audit_results.json（前回の判定で ok だった text と src_excerpt の組は書き出さない）
#           担当 = run_units.owner_of（差し戻し先の単位 S1〜H4）。
#           終了コードは常に 0（判定項目の書き出しの失敗で段を止めない）。
import json
import sys
import tempfile
from pathlib import Path

from _config_loader import INTERMEDIATE_DIR, load_screen_config
from run_units import load_units, owner_of

# WHY: 判定の文脈として src の前後 2 行だけ見せる。抜粋は 40 行で打ち切り、判定の入力を小さく保つ
EXCERPT_CONTEXT = 2
EXCERPT_MAX = 40


def _src_of(row):
    """行の出典（src）のうち最初の、file と開始行がそろったものを返す。無ければ None。"""
    srcs = row.get("src") or []
    if isinstance(srcs, dict):
        srcs = [srcs]
    for s in srcs:
        if isinstance(s, dict) and s.get("file") and isinstance(s.get("line_start"), int):
            return s
    return None


def _candidates(merged):
    """判定項目の候補 (kind, 欄の道筋, 記述の本文, src)。空の記述は候補にしない。
    WHY: 種類は「意味の判定が要る記述」に限る。型・件数のような機械で決まる欄は verify_* 系に任せる。"""
    out = []
    for i, it in enumerate((merged.get("screen_layout") or {}).get("items") or []):
        if str(it.get("remarks") or "").strip():
            out.append(("remarks", f"screen_layout.items[{i}].remarks", it["remarks"], _src_of(it)))
    for i, fp in enumerate(merged.get("front_processes") or []):
        for j, st in enumerate(fp.get("steps") or []):
            if str(st.get("description") or "").strip():
                out.append(("fr_step", f"front_processes[{i}].steps[{j}].description", st["description"], _src_of(st) or _src_of(fp)))
    for i, cl in enumerate(merged.get("common_logic") or []):
        if str(cl.get("description") or "").strip():
            out.append(("cl", f"common_logic[{i}].description", cl["description"], _src_of(cl)))
    for i, v in enumerate(merged.get("validations") or []):
        if str(v.get("rule") or "").strip():
            out.append(("vl", f"validations[{i}].rule", v["rule"], _src_of(v)))
    for i, c in enumerate(merged.get("calculations") or []):
        if str(c.get("remarks") or "").strip():
            out.append(("calc_remarks", f"calculations[{i}].remarks", c["remarks"], _src_of(c)))
    return out


def _excerpt(src, project_root, cache):
    """src の行の前後 EXCERPT_CONTEXT 行を、EXCERPT_MAX 行以内で返す。読めなければ空文字。
    WHY: 出典の file は絶対パスと project_root 基準の相対が混ざるため、両方に対応する。cache はファイルごとの行の一覧。"""
    path = Path(src["file"])
    if not path.is_absolute():
        path = Path(project_root or ".") / path
    if path not in cache:
        try:
            cache[path] = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[path] = None
    lines = cache[path]
    if lines is None:
        return ""
    ls = max(src["line_start"] - EXCERPT_CONTEXT, 1)
    le = min(int(src.get("line_end") or src["line_start"]) + EXCERPT_CONTEXT, len(lines))
    le = min(le, ls + EXCERPT_MAX - 1)
    return "\n".join(lines[ls - 1:le])


def build_claims(merged, project_root, prior_ok, units):
    """判定項目の一覧を作る。prior_ok は前回 ok だった (text, src_excerpt) の集合で、そのまま除く（キャッシュ）。
    呼出元 = main() と _selftest()。owner は run_units.owner_of で欄の道筋から決める（引けなければ None）。"""
    cache = {}
    claims = []
    for kind, road, text, src in _candidates(merged):
        excerpt = _excerpt(src, project_root, cache) if src else ""
        if (text, excerpt) in prior_ok:
            continue
        claims.append({
            "id": f"AC{len(claims) + 1:03d}",
            "kind": kind,
            "owner": owner_of(f"{road} {text}", units),
            "text": text,
            "src_excerpt": excerpt,
            "src_ref": ({"file": src["file"], "line_start": src["line_start"], "line_end": src.get("line_end")} if src else None),
        })
    return claims


def load_prior_ok(feature):
    """前回の判定結果（_audit_results.json）から ok の (text, src_excerpt) の集合を返す。無ければ空集合。
    WHY: 前回 ok のものを書き直すと、判定の Haiku が同じ項目を毎回読み直すため。結果が壊れていれば警告して空にする。"""
    p = INTERMEDIATE_DIR / f"{feature}_audit_results.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[警告] 前回の判定結果を読めないので、キャッシュは使わない: {p} ({e})", file=sys.stderr)
        return set()
    rows = data if isinstance(data, list) else (data.get("items") or [])
    return {(str(r.get("text") or ""), str(r.get("src_excerpt") or ""))
            for r in rows if isinstance(r, dict) and (r.get("ok") is True or r.get("verdict") == "ok")}


def _selftest():
    """判定項目の形（抜粋の行・owner の鍵・キャッシュの除外）を、一時ファイルのソースで確かめる。"""
    with tempfile.TemporaryDirectory() as tmp:
        src_file = Path(tmp) / "a.tsx"
        src_file.write_text("\n".join(f"line{i}" for i in range(1, 21)) + "\n", encoding="utf-8")
        s = {"file": str(src_file), "line_start": 10, "line_end": 11}
        merged = {
            "screen_layout": {"items": [{"remarks": "編集モード時のみ表示", "src": [s]}, {"remarks": "", "src": [s]}]},
            "front_processes": [{"steps": [{"description": "保存する", "src": [s]}]}],
            "common_logic": [],
            "validations": [{"rule": "必須", "src": [s]}],
            "calculations": [],
        }
        units = load_units()
        claims = build_claims(merged, "", set(), units)
        if len(claims) != 3:
            print(f"[selftest NG] 判定項目が 3 件でない（空の備考は除く）: {len(claims)}")
            return 0
        if claims[0]["src_excerpt"].splitlines() != [f"line{i}" for i in range(8, 14)]:
            print(f"[selftest NG] 抜粋の行が前後2行の範囲でない: {claims[0]['src_excerpt']!r}")
            return 0
        if not all("owner" in c for c in claims):
            print("[selftest NG] owner の欄が無い項目がある")
            return 0
        prior = {(c["text"], c["src_excerpt"]) for c in claims if c["text"] == "保存する"}
        if len(build_claims(merged, "", prior, units)) != 2:
            print("[selftest NG] 前回 ok の項目が書き出しから除かれていない")
            return 0
    print("[OK] selftest: 判定項目の形（抜粋・owner・キャッシュの除外）")
    return 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if len(argv) < 2:
        print("使い方: build_audit_claims.py <機能名> | --selftest", file=sys.stderr)
        return 0
    feature = argv[1]
    src_json = INTERMEDIATE_DIR / f"{feature}.json"
    if not src_json.exists():
        print(f"[警告] 中間JSON {src_json} が無いので、判定項目は書かない", file=sys.stderr)
        return 0
    merged = json.loads(src_json.read_text(encoding="utf-8"))
    try:
        sc = load_screen_config(feature)
    except FileNotFoundError:
        sc = {}
    claims = build_claims(merged, sc.get("project_root") or "", load_prior_ok(feature), load_units())
    out = INTERMEDIATE_DIR / f"{feature}_audit_claims.json"
    out.write_text(json.dumps(claims, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"判定項目 {len(claims)} 件を {out} に書いた（owner が None の件数 {sum(1 for c in claims if c['owner'] is None)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
