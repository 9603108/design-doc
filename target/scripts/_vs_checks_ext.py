#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_vs_checks_ext.py: verify_vs_source の追加検査(_vs_map・_vs_example・_vs_flow・_vs_db)をまとめて呼ぶ入口(stdlib のみ)

【目的】
verify_vs_source.py に検査を足すたびにそのファイルを太らせず、検査は4つの _vs_*.py へ足す。この入口は4つを順に呼ぶだけ。
検査の足し方は各 _vs_*.py の docstring、d の読み方は _vs_common の docstring。

【接続情報】
  呼出元: verify_vs_source.main(run_all の結果を指摘の行に足す)・verify_vs_source.selftest(selftest を呼ぶ)
  呼出先: _vs_map・_vs_example・_vs_flow・_vs_db の run / selftest
"""
from pathlib import Path

from pathlib import Path

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
import _vs_db
import _vs_example
import _vs_flow
import _vs_map

# WHY: 並びは出力の順になる。写し → 例示値 → 手順 → DB の順で固定する
_MODULES = (("_vs_map", _vs_map), ("_vs_example", _vs_example), ("_vs_flow", _vs_flow), ("_vs_db", _vs_db))


def exception_line(name, e) -> str:
    """検査モジュール name の例外 e を、[ERROR] の1行にする(末尾に例外の起きた行を添える)。
    WHY(2026-10-15 run16): 以前は [WARN] だったので、_vs_map の例外(common_logic の processing_steps が文字列の並びで .get が落ちた)で
    _vs_map の検査が丸ごと走らないまま after が通った。検査が走っていないことは、検査の指摘が0件であることと見分けがつかないので、
    ERROR にして after を止める。差し戻し先は担当の単位ではなく検査スクリプトの不具合(unknown に残り、行の内容から直す)。
    呼出元: run_all。"""
    import traceback
    tb = traceback.extract_tb(e.__traceback__)
    last = tb[-1] if tb else None
    at = f"{Path(last.filename).name}:{last.lineno}" if last else "不明"
    return (f"[ERROR] [検査の例外] {name}: {type(e).__name__}: {e}({at})。検査が走っていない。"
            f"スクリプトの不具合なので検査を直す → 差し戻し先 検査スクリプト")


def run_all(inter_dir, feature) -> list:
    """4モジュールの run を順に呼んで連結する。1モジュールが例外を出しても、その1行だけ出して残りのモジュールを続ける。
    WHY: 1つ壊れても、他のモジュールの指摘は出し切る(直す箇所を1回の実走で全部見せる)。ただし例外の行は ERROR
    (exception_line の WHY: 検査が走っていないのに通る事故を防ぐ)なので終了コードは 1 になる。"""
    out = []
    for name, mod in _MODULES:
        try:
            out += mod.run(inter_dir, feature)
        except Exception as e:  # noqa: BLE001, 検査の例外は握って ERROR の1行にする(上の WHY)
            out.append(exception_line(name, e))
    return out


def selftest() -> bool:
    """4モジュールの selftest を全部呼び、全部 True なら True。WHY: 1つ落ちても残りを呼ぶ(各モジュールと同じ理由)。"""
    results = [bool(mod.selftest()) for _, mod in _MODULES]
    results.append(_selftest_exception_is_error())
    return all(results)


def _selftest_exception_is_error() -> bool:
    """検査モジュールが例外を出したとき、run_all が [ERROR] の行(差し戻し先の印つき)を返し、残りのモジュールの指摘は続くことを確かめる。
    WHY(2026-10-15 run16): 例外が [WARN] だと、検査が走らないまま after が通る。壊れた模擬モジュールを _MODULES に差し替えて試す。呼出元: selftest。"""
    global _MODULES
    class Broken:
        @staticmethod
        def run(inter_dir, feature):
            return {"x": "str"}["x"].get("y")  # str の .get で AttributeError(run16 と同じ型の例外)

    class Fine:
        @staticmethod
        def run(inter_dir, feature):
            return ["[WARN] [t] w: m"]

    saved = _MODULES
    try:
        _MODULES = (("broken", Broken), ("fine", Fine))
        got = run_all("/nonexistent", "x")
    finally:
        _MODULES = saved
    return (len(got) == 2 and got[0].startswith("[ERROR] [検査の例外] broken: AttributeError") and "_vs_checks_ext.py:" in got[0]
            and got[0].endswith("差し戻し先 検査スクリプト") and got[1] == "[WARN] [t] w: m")
