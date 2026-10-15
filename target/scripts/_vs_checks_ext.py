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
import _vs_db
import _vs_example
import _vs_flow
import _vs_map

# WHY: 並びは出力の順になる。写し → 例示値 → 手順 → DB の順で固定する
_MODULES = (("_vs_map", _vs_map), ("_vs_example", _vs_example), ("_vs_flow", _vs_flow), ("_vs_db", _vs_db))


def run_all(inter_dir, feature) -> list:
    """4モジュールの run を順に呼んで連結する。1モジュールが例外を出しても、その1行だけ出して残りを続ける。
    WHY: 追加の検査が1つ壊れても、既存の検査と他の追加検査の結果は止めない([WARN] なので終了コードも変えない)。"""
    out = []
    for name, mod in _MODULES:
        try:
            out += mod.run(inter_dir, feature)
        except Exception as e:  # noqa: BLE001, 検査の例外は握って1行にする(上の WHY)
            out.append(f"[WARN] [検査の例外] {name}: {e}")
    return out


def selftest() -> bool:
    """4モジュールの selftest を全部呼び、全部 True なら True。WHY: 1つ落ちても残りを呼ぶ(各モジュールと同じ理由)。"""
    results = [bool(mod.selftest()) for _, mod in _MODULES]
    return all(results)
