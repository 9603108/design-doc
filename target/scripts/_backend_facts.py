#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_backend_facts.py — backend の SQL から集計関数・ウィンドウ関数を機械で拾う(C4)

【目的】
extract_backend_draft.py の下書きに、SQL の集計(SUM/COUNT/MIN/MAX/AVG/array_agg/string_agg)と
ウィンドウ(OVER (PARTITION BY ... ORDER BY ...))を、関数名・引数・FILTER 条件・別名・範囲
(本体の "main" か、囲む CTE の "cte <名前>")つきで出す。
意味合い: 店舗別の日数が count(*) でレジ単位に数えている等の事実を、LLM に読ませず下書きから見えるようにする。

【入出力】
  入力: 元の SQL 文字列 sql と、そのマスク済み文字列 m(extract_backend_draft._mask(sql) の戻り。位置は sql と同じ)
  出力: features(sql, m) → {"aggregates": [...], "windows": [...]}
        aggregates の要素: {func, arg, filter, alias, scope, cast}
        windows の要素: 同じ形 + over: {partition_by: [...], order_by: [...]}
【接続情報】
  - 呼出元: extract_backend_draft.extract()(SQL 1 本ごとに db_operations[].features、API ごとに apis[].features)
  - 呼出元: extract_backend_draft.selftest()(形の検査)
  - 呼出先: merge_features() は同じ extract() が API 単位の重ね合わせに使う。ほかに呼出先は無い(標準ライブラリの re のみ)

ponytail: 括弧は位置で閉じを探すだけ。文字列の中の括弧は _mask で消してある前提。HAVING と ORDER BY の中の集計は見ない。
"""
import re

# 集計の関数名。count(*) のような引数も括弧の中をそのまま arg に入れる
_AGG = re.compile(r"\b(SUM|COUNT|MIN|MAX|AVG|array_agg|string_agg)\s*\(", re.IGNORECASE)
# CTE の開き("名前 AS (")。範囲(scope)を決めるのに使う
_CTE_OPEN = re.compile(r"\b([A-Za-z_]\w*)\s+AS\s*\(", re.IGNORECASE)
_FILTER = re.compile(r"\s*FILTER\s*\(", re.IGNORECASE)
_OVER = re.compile(r"\s*OVER\s*\(", re.IGNORECASE)
_CAST = re.compile(r"\s*::\s*(\w+)")
_ALIAS = re.compile(r"\s*AS\s+([^\W\d]\w*)", re.IGNORECASE)


def _close(t, i):
    """t[i] の開き括弧に対応する閉じ括弧の位置(無ければ末尾)。"""
    depth = 0
    for j in range(i, len(t)):
        if t[j] == "(":
            depth += 1
        elif t[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(t)


def _split_top_commas(s):
    """文字列を深さ 0 のカンマで割り、各項の空白を詰めて返す(空の項は除く)。呼出元: features()。"""
    out, depth, st = [], 0, 0
    for i, c in enumerate(s):
        depth += (c == "(") - (c == ")")
        if c == "," and depth == 0:
            out.append(s[st:i])
            st = i + 1
    out.append(s[st:])
    return [" ".join(x.split()) for x in out if x.strip()]


def _scope(m, pos):
    """pos を囲む CTE の名前("cte <名前>")。どの CTE の中でもなければ "main"。呼出元: features()。"""
    name = "main"
    for c in _CTE_OPEN.finditer(m):
        if c.end() - 1 < pos < _close(m, c.end() - 1):
            name = "cte " + c.group(1)
    return name


def features(sql, m):
    """SQL 1 本の集計とウィンドウを {aggregates, windows} で返す。
    OVER が付く関数はウィンドウ、付かないものは集計に入れる(FILTER は両方に付く)。呼出元: extract_backend_draft.extract()。"""
    agg, win = [], []
    for mt in _AGG.finditer(m):
        name = mt.group(1)
        open_ = mt.end() - 1
        close = _close(m, open_)
        arg = " ".join(sql[open_ + 1:close].split())
        at = close + 1
        flt = None
        fm = _FILTER.match(m, at)
        if fm:
            fclose = _close(m, fm.end() - 1)
            flt = re.sub(r"^WHERE\s+", "", " ".join(sql[fm.end():fclose].split()), flags=re.IGNORECASE)
            at = fclose + 1
        over = None
        om = _OVER.match(m, at)
        if om:
            oclose = _close(m, om.end() - 1)
            body = sql[om.end():oclose]
            pm = re.search(r"PARTITION\s+BY\s+(.*?)(?=\bORDER\s+BY\b|$)", body, re.IGNORECASE | re.DOTALL)
            om2 = re.search(r"ORDER\s+BY\s+(.*)$", body, re.IGNORECASE | re.DOTALL)
            over = {"partition_by": _split_top_commas(pm.group(1)) if pm else [],
                    "order_by": _split_top_commas(om2.group(1)) if om2 else []}
            at = oclose + 1
        cm = _CAST.match(m, at)
        cast = cm.group(1) if cm else None
        if cm:
            at = cm.end()
        am = _ALIAS.match(m, at)
        item = {"func": name, "arg": arg, "filter": flt, "alias": am.group(1) if am else None,
                "scope": _scope(m, mt.start()), "cast": cast}
        if over is None:
            agg.append(item)
        else:
            item["over"] = over
            win.append(item)
    return {"aggregates": agg, "windows": win}


def merge_features(feats):
    """SQL ごとの features を API 単位に重ねる。同じ項目は1つだけ残す(呼出元: extract_backend_draft.extract())。"""
    out = {"aggregates": [], "windows": []}
    for f in feats:
        for k in out:
            out[k] += [x for x in f.get(k, []) if x not in out[k]]
    return out
