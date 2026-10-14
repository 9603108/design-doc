#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_react_draft.py — React(TSX) の画面から Phase 1 の events / 呼出の対応を機械的に下書きする

【目的】
screen_config の source_files に並ぶ TSX を静的に解析し、
JSX のイベント属性(onClick/onChange 等) → ハンドラ関数 → 状態の更新 → その状態を依存配列に持つ useEffect
→ project_config.api_call_functions の呼出(api.ts 等のラッパー関数経由を含む) を結んで、
次の下書きを JSON で出す。
  - events[]: 先頭は EV00(初期表示。マウント時に走る useEffect の呼出を集める)。続けて JSX のイベント属性
    1件を1イベントとし、ソースの出現順に EV01 形式の連番を振る(パイプラインの決まり: EV00 = 初期表示、以降 EV01…)
  - db_operations[].api_endpoint.trigger_events: action ごとに、それを呼ぶ event_code の配列
  - event_processes[].details: イベントごとの呼出の行。並びは db_operations の並びに一致させる
  - unresolved[]: 解析できなかった箇所 {file, line, reason}。推測で埋めず、ここにだけ出す

【意味合い】
Phase 1 は LLM の読解でイベントと API の対応を書いていたため、実行ごとに対応がぶれた。
対応の骨格(どの操作がどの action を呼ぶか)を機械で出し、LLM は業務語彙の文面だけを書く分担にする。
details の呼出の行は 04_機能別処理.md 5.2 の backend_call 推定語(「リクエスト」)を含む形で出し、
migrate_v17_to_v18.py が db_operations の出現順で backend_id を前から割り当てる取り決めに合わせる。

【入出力】
  入力: --config <screen_config.json>(source_files / screen_name を読む)
        --project-config <project_config.json>(省略時は config と同じフォルダの
        {feature}_project_config.json。api_call_functions を読む)
  出力: --out <JSON>。キーは { events, db_operations, event_processes, unresolved }
  終了コード: 0=未解決なし / 1=unresolved が1件以上(下書きは書く) / 2=入力不正

【接続情報】
  - 解析は analyze_sources()(解析関数群。本体は後続の変更で足す)。本ファイルの出力側は
    その戻りの links / unresolved だけに依存する
  - 出力の検査: verify_partials.py(event_code の連番・trigger_events の参照先・details の並び)
  - 呼出の行の記法: 04_機能別処理.md 5.2 / 5.3、キー: 15_中間JSONスキーマ.md
  例: python3 target/scripts/extract_react_draft.py --config <screen_config.json> \
        --out target/intermediate/<screen>_react_draft.json
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from verify_partials import DETAIL_TODO
import _react_facts  # 目的: C2 の失敗時の state の事実を下書きに足す。接続: main() の draft.update(...)

# JSX のイベント属性 → events[].action の動作語。表に無い属性は属性名をそのまま出す(推測で訳さない)
ACTION_WORDS = {
    "onClick": "クリック",
    "onChange": "変更",
    "onSubmit": "送信",
    "onBlur": "フォーカス外し",
    "onKeyDown": "キー押下",
}


# ---- 解析関数群 ----------------------------------------------------------------
# 意味合い: TS/TSX を構文木にせず、文字列・コメントを飛ばした括弧の対応だけで定義・本体・依存配列を切り出す。
#          stdlib だけで動かすための割り切りで、取れない形は unresolved に出して推測で埋めない。
# ponytail: JSX の地の文の ' や正規表現リテラル /"/ は文字列の始まりと誤認しうる。誤認が実害になったら
#           JSX を除いてから走査する形に上げる
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_STATE = re.compile(r"\bconst\s*\[\s*([A-Za-z_$][\w$]*)\s*,\s*([A-Za-z_$][\w$]*)\s*\]\s*=\s*useState\b")
_FUNC = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*(?:<[^>(]*>)?\s*\(")
_CONST = re.compile(r"\bconst\s+([A-Za-z_$][\w$]*)\s*(?::[^=;]+)?=(?![=>])\s*")
_ARROW = re.compile(r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]*)?=>")
_EFFECT = re.compile(r"\buseEffect\s*\(")
_EVENT_ATTR = re.compile(r"\s(on[A-Z]\w*)=\{")
_TAG = re.compile(r"<([A-Za-z][\w.]*)")
# call<T>("action", ...) の第1引数の文字列リテラル。型引数は複数行にまたがりうる
_API_LIT = re.compile(r"\s*(?:<[\s\S]*?>)?\s*\(\s*([\"'])([^\"'\n]+)\1")


# 意味合い: 失敗時・連続操作・0件の分岐を TSX の文字から機械的に拾う目印。拾えた場合だけ details の下書き行に足す。
#          失敗時の行は「エラー表示」で埋めて出す(〈〉を残すと合成後の穴検査が止める)。0件の行も同様に穴の無い文で出す。接続: _robust_lines() が使う。推測では足さない(拾えない形は何も出さない)。
# ponytail: 目印は文字列の一致だけ。catch や setError 等が別の用途で出ると、要らない行が1つ付く
_FAIL_HINT = re.compile(r"\bcatch\s*\(|\bset\w*(?:Err|err)\w*\s*\(")
_RACE_HINT = re.compile(r"\bAbortController\b|\w*(?:reqId|requestId|stale)\w*|\+\+\s*\w+\.current\b", re.IGNORECASE)  # 末尾: useRef の連番(++seq.current)
_EMPTY_HINT = re.compile(r"\.length\s*(?:===|==)\s*0\b")
# 意味合い: JSX の 0件分岐(`!x.length && <p>文言</p>` / `x.length === 0 ? <p>文言</p>`)から、x と表示文言を拾う。
#          接続: analyze_sources() が画面の状態 x と照合し、link["empty"] に積む。
# ponytail: `!x.length && !y && (` のように間に別の && が挟まる形も拾う(挟む式に < や ; が無い範囲だけ)
_EMPTY_AND = re.compile(r"!\s*([A-Za-z_$][\w$]*)\.length\b[^<;]*?&&\s*\(?\s*<[^>]*>\s*([^<{}]+?)\s*<")
_EMPTY_TERN = re.compile(r"\b([A-Za-z_$][\w$]*)\.length\s*===?\s*0\s*\?\s*\(?\s*<[^>]*>\s*([^<{}]+?)\s*<")
# 型宣言(interface / type)の先頭行。excerpt で参照される型を抜くのに使う
_TYPE_DECL = re.compile(r"^(?:export\s+)?(?:interface|type)\s+([A-Za-z_$][\w$]*)\b", re.M)


def _walk(text, i, end=None):
    """text[i:end] を、文字列・コメントを飛ばして (位置, 文字, 括弧の深さ) で返す。深さは i 起点の相対値。"""
    n = len(text) if end is None else end
    depth = 0
    while i < n:
        c = text[i]
        if c in "\"'`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        yield i, c, depth
        i += 1


def _close(text, open_i):
    """open_i の開き括弧に対応する閉じ括弧の位置を返す。"""
    for i, _c, d in _walk(text, open_i):
        if d == 0:
            return i
    return len(text)


def _expr_end(text, i):
    """i から始まる式の終わり(深さ0の ; か、外側の閉じ括弧)の位置を返す。"""
    for j, c, d in _walk(text, i):
        if d < 0 or (c == ";" and d == 0):
            return j
    return len(text)


def _top_split(text, s, e):
    """text[s:e] を深さ0のカンマで割り、各部分の (開始, 終了) を返す。"""
    parts, start = [], s
    for i, c, d in _walk(text, s, e):
        if c == "," and d == 0:
            parts.append((start, i))
            start = i + 1
    parts.append((start, e))
    return parts


def _idents(text, s, e):
    """text[s:e] の識別子を (名前, 位置) で返す。x.y の y は除き、...spread は残す。"""
    for m in _IDENT.finditer(text, s, e):
        i = m.start()
        if i > 0 and text[i - 1] == "." and text[i - 3:i] != "...":
            continue
        if i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_$"):
            continue
        yield m.group(0), i


def _line(text, i):
    return text.count("\n", 0, i) + 1


def _span(path, text, s, e):
    """文字位置 s〜e(e は含まない)を出典の {file, line_start, line_end}(1始まり・両端を含む)にする。

    呼出元: analyze_sources()・build_draft() の src。意味合い: 下書きの要素が指すソースの行を機械で付ける
    (数字は LLM に書かせない)。接続: 合成側(_units の _fill_src、A3)が同じ形で読む。
    """
    return {"file": str(path), "line_start": _line(text, s), "line_end": _line(text, max(s, e - 1))}


def _deps_of(path, text, s, e):
    """引数並び text[s:e] の最後が配列 [...] ならその識別子の集合、無ければ None。"""
    last_s, last_e = _top_split(text, s, e)[-1]
    seg = text[last_s:last_e].strip()
    if not seg.startswith("["):
        return None
    return {n for n, _ in _idents(text, last_s, last_e)}


def _collect_defs(path, text, funcs, setters, derived, effects, unresolved):
    """1ファイルから関数の本体・状態の setter・派生値の依存・useEffect を集める。

    funcs: 名前 → (path, text, 本体の開始, 終了)。全ファイルから集め、先に見た定義を優先する
    setters/derived/effects は画面のファイル(.tsx/.jsx)からだけ集める(api.ts の局所変数を混ぜない)
    """
    is_view = path.suffix in (".tsx", ".jsx")
    for m in _FUNC.finditer(text):
        pc = _close(text, m.end() - 1)
        bs = text.find("{", pc)
        if bs >= 0:
            funcs.setdefault(m.group(1), (path, text, bs, _close(text, bs) + 1))
    for m in _CONST.finditer(text):
        name, s = m.group(1), m.end()
        e = _expr_end(text, s)
        hook = re.match(r"(useCallback|useMemo)\s*\(", text[s:e])
        if hook:
            po = s + hook.end() - 1
            pe = _close(text, po)
            fs, fe = _top_split(text, po + 1, pe)[0]
            if hook.group(1) == "useCallback":
                funcs.setdefault(name, (path, text, fs, fe))
            if is_view:
                derived[name] = _deps_of(path, text, po + 1, pe) or set()
        elif _ARROW.match(text, s):
            funcs.setdefault(name, (path, text, s, e))
        elif is_view:
            derived[name] = {n for n, _ in _idents(text, s, e)}
    if not is_view:
        return
    for m in _STATE.finditer(text):
        setters[m.group(2)] = m.group(1)
    for m in _EFFECT.finditer(text):
        po = m.end() - 1
        pe = _close(text, po)
        deps = _deps_of(path, text, po + 1, pe)
        if deps is None:
            unresolved.append({"file": str(path), "line": _line(text, m.start()),
                               "reason": "依存配列の無い useEffect は呼ばれる契機を決められない"})
        else:
            # 目的: useEffect は依存配列によらず、マウント時に必ず1回走る。[] の effect も含めて全部を集め、
            #       analyze_sources が EV00(初期表示)の呼出にする。接続: 操作の連鎖(deps & changed)では
            #       [] は何とも交わらないので、従来どおり連鎖には入らない。
            fs, fe = _top_split(text, po + 1, pe)[0]
            effects.append((deps, (path, text, fs, fe)))


def _resolve(span, ctx, seen, acc):
    """本体 span から、呼ぶ action(呼ぶ順)と更新する状態を acc に積む。局所関数・ラッパーは辿る。"""
    path, text, s, e = span
    # 目的: 失敗時・連続操作・0件の目印を拾うため、辿った本体の文字を acc に積む(_robust_lines が使う)
    acc["texts"].append(text[s:e])
    for name, i in _idents(text, s, e):
        # 目的: 画面が使う API 関数の定義(api.ts 側)を、excerpt に抜くために覚える。接続: _excerpt_chunks()
        if name in ctx["funcs"] and ctx["funcs"][name][0].suffix not in (".tsx", ".jsx"):
            ctx["used"].add(name)
        if name in ctx["api"]:
            if re.search(r"\bfunction\s*$", text[max(0, i - 20):i]):
                continue  # 呼出関数そのものの定義
            # 目的: 第1引数が文字列リテラルでも無くても、API 関数を呼べば取得が起きる。接続: links の "fetch" → _continuous_ops()
            acc["fetch"] = True
            lm = _API_LIT.match(text, i + len(name), e)
            if lm:
                if lm.group(2) not in acc["actions"]:
                    acc["actions"].append(lm.group(2))
                # 目的: 呼出位置を出典つきで残す(db_operations の call_sites・event_processes の calls)。同じ呼出は1回だけ積む
                call = {"action": lm.group(2), "src": [_span(path, text, i, lm.end())]}
                if call not in acc["calls"]:
                    acc["calls"].append(call)
            else:
                ctx["unresolved"].append({"file": str(path), "line": _line(text, i),
                                          "reason": f"{name} の第1引数が文字列リテラルでない"})
        elif name in ctx["funcs"]:
            if name not in seen:
                seen.add(name)
                _resolve(ctx["funcs"][name], ctx, seen, acc)
            acc["hit"] = True
        elif name in ctx["setters"]:
            acc["states"].add(ctx["setters"][name])
            acc["hit"] = True


def _excerpt_chunks(used, funcs):
    """画面が使う API 関数の定義と、その本体・ヘッダに名前が出る型宣言を、出典つきの TS 断片として返す。

    呼出元: analyze_sources()。意味合い: api.ts(330KB)は LLM が全文で読めないため、要る断片だけを機械で抜く。
    接続: main() が <機能名>_api_excerpt.ts として書く。型は名前が断片に出たものだけ(推測で足さない)。
    """
    if not used:
        return ""
    parts, joined = [], []
    for name in sorted(used):
        path, text, s, e = funcs[name]
        hs = text.rfind("\n", 0, text.rfind(name, 0, s)) + 1
        body = text[hs:e]
        joined.append(body)
        sp = _span(path, text, hs, e)
        parts.append(f"// 出典: {sp['file']}:{sp['line_start']}-{sp['line_end']}\n{body}")
    all_text = "\n".join(joined)
    seen = set()
    for name in sorted(used):
        path, text = funcs[name][0], funcs[name][1]
        if str(path) in seen:
            continue
        seen.add(str(path))
        for m in _TYPE_DECL.finditer(text):
            if not re.search(rf"\b{re.escape(m.group(1))}\b", all_text):
                continue
            brace, nl = text.find("{", m.end()), text.find("\n", m.end())
            if brace >= 0 and (nl < 0 or brace < nl):
                end = _close(text, brace) + 1
            else:
                end = len(text) if nl < 0 else nl
            sp = _span(path, text, m.start(), end)
            parts.append(f"// 出典: {sp['file']}:{sp['line_start']}-{sp['line_end']}\n{text[m.start():end]}")
    return "\n\n".join(parts) + "\n"


def analyze_sources(source_files, api_call_functions):
    """TSX を解析し、(links, unresolved) を返す。

    契約(出力側 build_draft が依存する形):
      links: 先頭は attr="mount" の1件(EV00 初期表示。マウント時に走る useEffect 全部の呼出を、ソースの出現順に集める)。
             続けて JSX のイベント属性1件ごとの dict をソースの出現順に並べる。
             { file, line, element(要素の表示名), attr(例 onClick), handler(関数名), actions([action,...] 呼ぶ順) }
      unresolved: [{ file, line, reason }]
      excerpt: 画面が使う API 関数と参照する型の断片(TS の文字列。無ければ空文字)。main() が書き出す
    辿り方: イベント属性の式 → 局所関数(useCallback を含む)・api_call_functions の直接呼出 → setter で変わる状態
            → その状態に依存する派生値(const・useCallback/useMemo の依存配列)を閉包 → 依存配列が交わる useEffect の本体。
    参照先を1つも解決できないイベント属性(子の props 経由の呼出など)は links に入れず unresolved に出す。
    """
    texts = {p: p.read_text(encoding="utf-8") for p in source_files
             if p.suffix in (".ts", ".tsx", ".js", ".jsx")}
    funcs, setters, derived, effects, unresolved = {}, {}, {}, [], []
    for p, t in texts.items():
        _collect_defs(p, t, funcs, setters, derived, effects, unresolved)
    ctx = {"api": set(api_call_functions), "funcs": funcs, "setters": setters, "unresolved": unresolved, "used": set()}
    # 目的: JSX の 0件分岐を画面の状態名で引けるように、状態 → 表示文言 を先に集める。接続: 下の links 組立の "empty"
    branches = {}
    for p, t in texts.items():
        if p.suffix in (".tsx", ".jsx"):
            for rx in (_EMPTY_AND, _EMPTY_TERN):
                for bm in rx.finditer(t):
                    branches.setdefault(bm.group(1), bm.group(2))
    links = []
    for p, t in texts.items():
        if p.suffix not in (".tsx", ".jsx"):
            continue
        for m in _EVENT_ATTR.finditer(t):
            bo = m.end() - 1
            be = _close(t, bo)
            acc = {"actions": [], "states": set(), "hit": False, "texts": [], "fetch": False, "calls": []}
            _resolve((p, t, bo + 1, be), ctx, set(), acc)
            expr = " ".join(t[bo + 1:be].split())[:80]
            if not acc["hit"] and not acc["actions"]:
                unresolved.append({"file": str(p), "line": _line(t, m.start()),
                                   "reason": f"{m.group(1)} の参照先を解決できない: {expr}"})
                continue
            changed = set(acc["states"])
            grown = True
            while grown:
                grown = False
                for name, deps in derived.items():
                    if name not in changed and deps & changed:
                        changed.add(name)
                        grown = True
            for deps, span in effects:
                if deps & changed:
                    _resolve(span, ctx, set(), acc)
            tags = list(_TAG.finditer(t, 0, m.start()))
            links.append({
                "file": str(p), "line": _line(t, m.start()),
                "element": tags[-1].group(1) if tags else "",
                "attr": m.group(1), "handler": expr, "actions": acc["actions"], "texts": acc["texts"],
                "calls": acc["calls"],
                "ui_label": _ui_label(t, tags, m, be),
                # 目的: ui_label が取れない要素(select 等)の連続操作の表示名を、隣の label・操作対象の state から引く。接続: _continuous_ops()
                "op_label": _op_label(t, tags[-1].start() if tags else m.start(), acc["states"]),
                # 目的: 開きタグの文字(disabled の有無)を連続操作の判定に渡す。接続: _continuous_ops()
                "tag": t[(tags[-1].start() if tags else m.start()):max(be, t.find(">", be))],
                "empty": [(x, branches[x]) for x in sorted(acc["states"]) if x in branches],
                # 目的: 取得を起こすかどうかを連続操作の判定に渡す(文字列リテラルの action が取れなくても取得は起きる)。接続: _continuous_ops()
                "fetch": acc["fetch"],
                # 目的: イベント属性の式の出典(下書きの events[].src の元)。接続: build_draft()
                "src": [_span(p, t, bo + 1, be)],
            })
    # EV00(初期表示): マウント時には全ての useEffect が走るので、その呼出を出現順に集めて links の先頭に置く。
    # 意味合い: パイプラインの決まり(EV00 = 初期表示)に合わせる。接続: build_draft が attr="mount" を EV00 にする。
    # ponytail: effect 内の条件分岐(if で呼ばない場合)は見ない。呼ぶ可能性のある action を全部出す。
    mount = {"actions": [], "states": set(), "hit": False, "texts": [], "calls": []}
    for _deps, span in effects:
        _resolve(span, ctx, set(), mount)
    first = effects[0][1] if effects else None
    links.insert(0, {
        "file": str(first[0]) if first else "", "line": _line(first[1], first[2]) if first else 0,
        "element": "", "attr": "mount", "handler": "useEffect(マウント時)", "actions": mount["actions"],
        "texts": mount["texts"], "calls": mount["calls"], "ui_label": "", "empty": [],
        # 目的: 初期表示の出典は先頭の useEffect の本体。接続: build_draft() の EV00 の src
        "src": [_span(first[0], first[1], first[2], first[3])] if first else [],
    })
    seen_u, uniq = set(), []
    for u in unresolved:
        key = (u["file"], u["line"], u["reason"])
        if key not in seen_u:
            seen_u.add(key)
            uniq.append(u)
    return links, uniq, _excerpt_chunks(ctx["used"], funcs)


def _ui_label(t, tags, m, be):
    """イベント属性を持つ要素の表示名を返す。aria-label・label・placeholder の属性、無ければ開きタグ後の子テキスト。

    呼出元: analyze_sources()。意味合い: 操作の名前をソースの文言のまま下書きに残す(推測で訳さない)。
    ponytail: 開きタグの終わりは属性値に '>' が無い前提で最初の '>' とする。=> を含む後続の属性があると取り違えうる
    """
    gt = t.find(">", be)
    if gt < 0:
        return ""
    start = tags[-1].start() if tags else m.start()
    am = re.search(r"\b(?:aria-label|label|placeholder)=\{?[\"']([^\"']+)[\"']", t[start:gt])
    if am:
        return am.group(1)
    lt = t.find("<", gt)
    txt = t[gt + 1:lt if lt >= 0 else len(t)].strip()
    if txt and "{" not in txt:
        return txt
    if m.group(1) == "onSubmit":
        # 目的: 送信(form の onSubmit)の表示名は、その form の submit ボタンの文言。接続: </form> までを見る
        end = t.find("</form>", be)
        sm = re.search(r"type=\"submit\"[^>]*>\s*([^<{}]+?)\s*<", t[be:end if end >= 0 else len(t)])
        return sm.group(1) if sm else ""
    return ""


def _op_label(t, start, states):
    """連続操作の表示名の候補。要素の直前の <label>文言</label>、無ければ value/checked が操作対象の state の要素の直前の label。

    呼出元: analyze_sources()。意味合い: 画面項目の名前(ソースの label 文言)をそのまま使う。取れなければ空文字。
    """
    lab = r"<label\b[^>]*>\s*([^<{}]+?)\s*</label>\s*"
    m = re.search(lab + r"\Z", t[max(0, start - 300):start])
    if m:
        return m.group(1)
    for s in sorted(states):
        sm = re.search(lab + r"<(?:select|input|textarea)\b[^>]*?(?:value|checked)=\{" + re.escape(s) + r"\}", t)
        if sm:
            return sm.group(1)
    return ""


def _failure_line(fe):
    """failure_effects の1件(_react_facts.extract の要素)から、失敗時の行を作る。state を書く先が無ければ None。

    呼出元: _robust_lines()。意味合い: 失敗時に書く state と書かない state(前の値が残る)をソースの書き込みから文にする。
    """
    if not fe["on_failure"]:
        return None
    s = "取得に失敗したときは、" + "・".join(fe["on_failure"]) + "にエラーの内容"
    if fe["failure_text"]:
        s += f"({fe['failure_text']})"
    s += "を設定する"
    if fe["untouched_on_failure"]:
        s += "。" + "・".join(fe["untouched_on_failure"]) + "は更新しない(前の表示が残る)"
    if fe["in_finally"]:
        s += "。処理の終わりに " + "・".join(fe["in_finally"]) + " にする"
    return s


def _robust_lines(texts, failures=()):
    """辿った本体の文字から、失敗時・連続操作・0件の下書き行を返す(拾えたものだけ)。

    呼出元: _detail_lines()。意味合い: API を呼ぶイベントの処理ステップに、画面の振る舞いの骨格を機械的に足す。
    failures はそのイベントの failure_effects。取れた分は失敗時の一般文の代わりに事実の行を出す(取れなければ一般文のまま)。
    """
    body = "\n".join(texts)
    lines = []
    facts = [ln for ln in (_failure_line(f) for f in failures) if ln]
    if facts:
        lines += facts
    elif _FAIL_HINT.search(body):
        lines.append("取得に失敗したときはエラー表示にメッセージを表示する")
    if _RACE_HINT.search(body):
        lines.append("連続して操作したときは、最後の要求の応答だけを反映する")
    if _EMPTY_HINT.search(body):
        lines.append("0件のときは該当なしの旨を表示する")
    return lines


def _detail_lines(link, called, failures=()):
    """呼ぶ API があるイベントの details に足す描画の行。0件分岐は状態が一致した分だけ、分岐の文言つきに置き換える。

    呼出元: build_draft()。意味合い: 0件の行を、JSX に書かれた表示文言で具体化する(文言が無ければ穴の無い一般文のまま)。
    """
    if not called:
        return []
    lines = _robust_lines(link["texts"], failures)
    if link.get("empty"):
        lines = [ln for ln in lines if not ln.startswith("0件のとき")]
        lines += [f"0件のときは該当なしの旨を表示する(画面の文言: 「{txt}」)" for _x, txt in link["empty"]]
    return lines


# ---- 事実の行(案 B5)----------------------------------------------------------
# 目的: 取得の開始時・成功時・失敗時・0件・CSV・連続操作の事実を、details とは別の fact_lines として出す。
# WHY: 並びと内容は TSX の書き込みから機械で決まる事実なので、モデルに書かせず下書きが持つ。
#      B6(apply_react_draft)が position(before_calls・after_calls・end)どおりに強制し、match の語で
#      モデルが書いた同趣旨の行を落とす。
_FACT_MATCH = {
    "start": ["取得の開始時"], "success": ["取得に成功"], "failure": ["取得に失敗"], "empty": ["0件のとき"],
    "continuous.stale": ["後から届く"], "continuous.loading": ["最初に返った応答"],
}
# WHY: 空に戻す書き込み(null・空配列・空文字)は「空にする」と書く。値の表記の違いを文に持ち込まない
_EMPTY_VALUES = ("null", "[]", '""', "''", "undefined")


def _event_codes(links):
    """links と同じ順の event_code(attr=mount は EV00、それ以外は EV01 から連番)。build_draft と同じ振り方。"""
    out, n = [], 0
    for link in links:
        if link["attr"] == "mount":
            out.append("EV00")
        else:
            n += 1
            out.append(f"EV{n:02d}")
    return out


def _state_names(sources):
    """state 名 → 画面項目の名前(bind された入力要素の label が1種類に決まるときだけ)。呼出元: main()。

    意味合い: 「shop を…」を画面で見える項目の名前(「店舗」等)に言い換える対。決まらない state は名前のまま使う。
    """
    labels = {}
    for g in _react_facts.jsx_elements(sources)["jsx_elements"]:
        if g.get("bind") and g.get("label"):
            labels.setdefault(g["bind"], set()).add(g["label"])
    names = {k: next(iter(v)) for k, v in labels.items() if len(v) == 1}
    # WHY: state の中の欄(applied.from)は、その欄へ入れた state の画面項目名で言い換える(表示の確定で入った値を辿る)
    for key, st in _react_facts.object_fields(sources).items():
        if st in names:
            names[key] = names[st]
    return names


def _state_name(st, names):
    """state 名を画面項目の名前に置き換える。対が無ければ state 名のまま。"""
    return names.get(st) or st


def _set_phrase(pair, names):
    """before_call の `state=値` を「state を値にする」「state を空にする」の文にする。"""
    st, _, val = pair.partition("=")
    name = _state_name(st, names)
    return f"{name} を空にする" if val in _EMPTY_VALUES else f"{name} を {val} にする"


def _event_facts(link, failures, names):
    """1イベントの事実の行 [{key, text, position, match, src}]。failures はそのイベントの failure_effects の要素。

    呼出元: build_draft()。意味合い: 開始時(before_calls)・成功時(after_calls)・失敗時(after_calls)・0件(end)の行。
    同じ text は1回だけ出す。拾えない欄は出さない(推測で足さない)。
    """
    facts = []

    def add(key, text, position, src):
        if not any(f["text"] == text for f in facts):
            facts.append({"key": key, "text": text, "position": position, "match": _FACT_MATCH[key], "src": src})

    for fe in failures:
        if fe["before_call"]:
            add("start", "取得の開始時に、" + "・".join(_set_phrase(x, names) for x in fe["before_call"]),
                "before_calls", fe["src"])
        if fe["on_success"]:
            add("success", "取得に成功したときは、" + "・".join(_state_name(s, names) for s in fe["on_success"])
                + " を応答の内容に置き換える", "after_calls", fe["src"])
        fl = _failure_line(fe)
        if fl:
            add("failure", fl, "after_calls", fe["src"])
    for _st, txt in link.get("empty") or []:
        add("empty", f"0件のときは該当なしの旨を表示する(画面の文言: 「{txt}」)", "end", [])
    return facts


# WHY: CSV の処理順(見出し → データ行 → 合計行 → 二重引用符 → Blob → ファイル名)を seq の固定番号にする。
#      apply_react_draft(T5)が seq の昇順に並べ、モデルの同趣旨の行は match の語で落とす。
#      行が無いときは番号を詰めない(seq は処理順の位置そのもの)。
_CSV_SEQ_MATCH = {1: ["見出し"], 2: ["データ行"], 3: ["合計行"], 4: ["二重引用符"],
                  5: ["文字コード", "BOM", "区切り", "改行", "Blob"], 6: ["ファイル名", "ダウンロード"]}
_SEP_WORDS = {" ": "半角スペース区切り", ",": "カンマ区切り", chr(9): "タブ区切り"}
# WHY: 合計行の when の式は画面の言い方にする(表示行数の判定)。型に当たらない式は式のまま出す
_WHEN_WORDS = [(r"\s*\w+\.length\s*>\s*1\s*", "表示が2行以上"),
               (r"\s*\w+\.length\s*>\s*0\s*", "表示が1行以上")]


def _sep_text(sep):
    """区切り文字を画面の言い方にする。表に無い区切りは「x」区切りと書く(推測で訳さない)。"""
    return _SEP_WORDS.get(sep, "「" + sep + "」区切り")


def _when_text(when):
    """合計行の when を画面の言い方にする。型に当たらない式は式のまま返す。"""
    for pat, words in _WHEN_WORDS:
        if re.fullmatch(pat, when):
            return words
    return when


def _file_name(tpl, names):
    """ファイル名の ${式} を読める文にする。式が画面項目名に決まれば「(<項目名>の値)」(ドットを含む式は「(確定済みの<項目名>の値)」)、決まらなければ「(式: <式>)」。

    WHY: ${…} を残すと合成後の穴検査が ERROR にする。式のまま出すと、ファイル名の中身が画面から読めない。
         names は _state_names(state 名と、state の欄 applied.from の両方)。推測で項目名を当てない。
    """
    def rep(m):
        expr = m.group(1)
        name = names.get(expr)
        if not name:
            return f"(式: {expr})"
        # WHY: ドットを含む式(applied.from 等)は表示の確定で入った値なので「確定済みの」と書く(単純な state は従来どおり)
        return f"(確定済みの{name}の値)" if "." in expr else f"({name}の値)"
    return re.sub(r"\$\{([^}]+)\}", rep, tpl)


def _spread_arrays(raw, display_rows, aliases):
    """見出しの展開(spread)の元の配列名の並び。表示行に在る名前はそのまま、別名の配列は元の配列名の並びに解く。

    呼出元: _csv_items()・main()(header.spread[].source)。意味合い: CSV_SPECS = [...LEFT, ...RIGHT] のような
    別名の配列を、表示行の実物(LEFT・RIGHT)に戻す。展開の形が取れなければ空。
    """
    m = re.match(r"\.\.\.\s*(\w+)", raw or "")
    if not m:
        return []
    name = m.group(1)
    if any(row.get("array") == name for row in display_rows or []):
        return [name]
    return list((aliases or {}).get(name, [name]))


def _csv_items(c, display_rows, aliases=None):
    """見出しの展開(spread)の元の配列の項目(表示行の specs)。展開が無い・元が表示行に無いときは空。

    別名の配列は _spread_arrays で解いた配列の specs を、解いた並びどおりに連結する。
    """
    spread = (c.get("header") or {}).get("spread") or []
    names = _spread_arrays(spread[0].get("raw", ""), display_rows, aliases) if spread else []
    return [s for n in names for row in display_rows or [] if row.get("array") == n for s in row.get("specs", [])]


def _alias_arrays(sources):
    """モジュール直下の別名の配列 const X = [...A, ...B](中身が配列の展開だけ)を {X: [A, B]} で返す。

    呼出元: main()。意味合い: CSV の見出しが別名の配列を展開するとき、元の配列名に解くための対。
    接続: _spread_arrays()。展開だけでない配列(項目の直書き等)は含めない(推測で足さない)。
    """
    out = {}
    pat = re.compile(r"^const\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*\[", re.M)
    for p in sources:
        if p.suffix not in (".tsx", ".jsx"):
            continue
        t = p.read_text(encoding="utf-8")
        for m in pat.finditer(t):
            names = []
            for part in t[m.end():_close(t, m.end() - 1)].split(","):
                sm = re.fullmatch(r"\s*\.\.\.\s*([A-Za-z_$][\w$]*)\s*", part)
                if sm is None:
                    names = []
                    break
                names.append(sm.group(1))
            if names:
                out[m.group(1)] = names
    return out


_NULL_FALLBACK = re.compile(r"\?\?\s*(\"\"|''|[^\s)]+)")
_FORMAT_CALL = re.compile(r"toLocaleString\s*\(|Intl\.NumberFormat|\.toFixed\s*\(")
_DECL = re.compile(r"\b(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)")
_IMPORT = re.compile(r"^import\s+([^;]+?)\s+from\s+[\"']", re.M)


def _fixed_col_text(name, col):
    """データ行の固定列の言い方。値の取り出しが `?? <リテラル>` の列は、値が無い(null)ときの値を文に足す。

    WHY: `r.reji_no ?? ""` の列は null のとき空文字で出る。CSV の空と null を取り違えないよう、その事実を書く。
    """
    fb = _NULL_FALLBACK.search(col.get("row_value") or "")
    notes = ["二重引用符で囲む"] if col.get("quoted") else []
    if fb:
        lit = fb.group(1)
        notes.append("値が無い(null)ときは空文字" if lit in ('""', "''") else f"値が無い(null)ときは「{lit}」")
    return name + ("(" + "。".join(notes) + ")" if notes else "")


def _same_total_text(name, col, names):
    """合計行の同じ式の列の言い方。表示の確定の欄(applied.from)は「確定済みの<項目名>の値」、決まらない式は式のまま出す。"""
    expr = col.get("expr") or ""
    if "." in expr and names.get(expr):
        return f"{name}は確定済みの{names[expr]}の値"
    return f"{name}は(式: {expr})の値"


def _csv_seq_facts(c, names, display_rows, aliases=None):
    """1つの CSV 出力の処理順の事実(seq 1〜6)を返す。取れない欄は行ごと出さない(推測で埋めない)。"""
    facts, src = [], c.get("src", [])
    fixed = (c.get("header") or {}).get("fixed") or []
    cols = (c.get("row_builder") or {}).get("columns") or []

    def add(seq, key, text):
        facts.append({"key": key, "text": text, "position": "seq", "seq": seq,
                      "match": _CSV_SEQ_MATCH[seq], "src": src})

    # 1 見出し行: 固定列の後に、展開元の項目(表示行の全項目。dead も含む。点数があれば2列)
    items, dead = [], False
    for spec in _csv_items(c, display_rows, aliases):
        nm = spec.get("csv") or spec.get("label")
        dead = dead or bool(spec.get("dead"))
        items += [nm + "(点数)", nm + "(金額)"] if spec.get("cnt") else [nm]
    if fixed or items:
        text = "見出し行: " + "、".join(fixed + items) + "の順に並べる"
        if dead:
            text += "(表示の切替に関係なく全項目を出す)"
        add(1, "csv.header", text)

    # 2 データ行: 表示中の行ごとに固定列の値、続けて見出しの項目の値。区切りは機械で出た分だけ書く
    parts = []
    for i, f in enumerate(fixed):
        col = cols[i] if i < len(cols) else {}
        expr = col.get("expr") or ""
        # WHY: 確定済みの欄(applied.from のようにドットを含む式)は、表示の確定で入った値だと分かる言い方にする
        if col.get("kind") == "same" and "." in expr and names.get(expr):
            parts.append(f"{f}(確定済みの{names[expr]}の値)")
        else:
            parts.append(_fixed_col_text(f, col))
    spread_expr = " ".join((x.get("expr") or "") for x in cols if x.get("kind") == "spread")
    has_cnt = re.search(r"\.cnt\s*\?", spread_expr)
    has_calc = re.search(r"\.calc\s*\?", spread_expr)
    # WHY: 展開の三項(cnt=点数あり・calc=計算式あり)は、項目ごとの値の並びとして書く。無ければ従来の文
    if has_cnt and has_calc:
        per_item = "続けて項目ごとに、点数のある項目は点数と金額の2列、無い項目は金額の1列の値を並べる(計算式のある項目は計算結果)"
    elif has_cnt:
        per_item = "続けて項目ごとに、点数のある項目は点数と金額の2列、無い項目は金額の1列の値を並べる"
    elif has_calc:
        per_item = "続けて項目ごとに、計算式のある項目は計算結果の値を並べる"
    else:
        per_item = "見出しの項目の値を並べる"
    text = "データ行: 表示中の行ごとに" + ("、".join(parts) + "の値を並べ、" if parts else "") + per_item
    seps = []
    vs = (c.get("row_builder") or {}).get("value_separator")
    if vs is not None:
        seps.append("列の区切りは" + _sep_text(vs))
    # WHY: header の join は全体の区切り(seq 5 で書く)、[…] は行の並びの join なので、列の中の値の区切りだけ書く
    for j in c.get("joins") or []:
        if j.get("target") not in ("[…]", "header"):
            seps.append(j["target"] + "の値は" + _sep_text(j["separator"]) + "で連結する")
    if seps:
        text += "。" + "。".join(seps)
    # WHY: CSV の関数が整形の関数を呼ばないとき(formats_numbers が false)は、画面の桁区切りを CSV に持ち込まないと書く
    if c.get("formats_numbers") is False:
        text += "。数値は桁区切りにせず、そのまま出す"
    add(2, "csv.row", text)

    # 3 合計行: total_row がある時だけ。空欄の列と、空でない合計の値を分けて書く
    tr = c.get("total_row")
    if tr is not None:
        text = "合計行: " + _when_text(tr.get("when", "")) + "のとき最後に1行足す"
        blank_q, blank_plain, vals = [], [], []
        for i, col in enumerate(cols):
            kind = col.get("kind")
            name = fixed[i] if i < len(fixed) and not any(x.get("kind") == "spread" for x in cols[:i]) else f"{i + 1}列目"
            # WHY: 同じ式の列(開始日・終了日)と展開の列(各項目の合算)も合計行の値なので書く。式と合計の呼出から読める分だけ
            if kind == "same":
                vals.append(_same_total_text(name, col, names))
                continue
            if kind == "spread":
                agg = ((c.get("row_builder") or {}).get("total_call") or {}).get("agg_expr")
                if agg:
                    vals.append("続く各項目は全行の合算" if "sum" in agg.lower() else f"続く各項目は{agg}で求めた値")
                continue
            if kind != "ternary":
                continue
            tv = col.get("total_value", "")
            if tv in _EMPTY_VALUES + ("",):
                # WHY: 引用符つきの空(q(""))と無記入は CSV の上で別の値なので、分けて書く
                (blank_q if col.get("quoted") else blank_plain).append(name)
            else:
                lit = tv[1:-1] if len(tv) >= 2 and tv[0] == tv[-1] and tv[0] in "\"'`" else None
                vals.append(name + "は" + (f"「{lit}」" if lit is not None else tv))
        grp = []
        if blank_q:
            grp.append("・".join(blank_q) + " は引用符つきの空")
        if blank_plain:
            grp.append("・".join(blank_plain) + " は無記入")
        if grp:
            text += "。合計行では " + "、".join(grp) + "にする"
        if vals:
            text += "。合計行の値は " + "、".join(vals)
        add(3, "csv.total_blank" if (blank_q or blank_plain) else "csv.total", text)

    # 4 二重引用符の重ね方(quote.escape がある時だけ)
    if (c.get("quote") or {}).get("escape"):
        add(4, "csv.escape", "値の中の二重引用符は2つ重ねる")

    # 5 全体の連結・文字コード・Blob
    bits = []
    if c.get("separator") is not None:
        bits.append("区切りは「" + c["separator"] + "」")
    if c.get("line_break"):
        bits.append("改行は " + c["line_break"])
    text = "全体を連結する" + ("(" + "、".join(bits) + ")" if bits else "")
    if c.get("bom"):
        text += "。先頭に BOM を付ける"
    text += "。" + (c["mime"] + " の " if c.get("mime") else "") + "Blob にする"
    add(5, "csv.blob", text)

    # 6 ファイル名
    if c.get("file_name_template"):
        add(6, "csv.filename", "ファイル名は「" + _file_name(c["file_name_template"], names) + "」で保存する")
    return facts


def _csv_fact_map(csv_exports, display_rows=(), names=None, aliases=None):
    """CSV 出力の処理順の事実を、trigger_events の event_code ごとの fact_lines(position=seq)で返す。呼出元: main()。

    WHY: 見出し・データ行・合計行・引用符・Blob・ファイル名の順は TSX の書き方から機械で決まるので、モデルに書かせず下書きが持つ。
    """
    out = {}
    for c in csv_exports:
        facts = _csv_seq_facts(c, names or {}, display_rows, aliases)
        for code in c.get("trigger_events", []):
            out.setdefault(code, []).extend(facts)
    return out


def _decl_body(text, i):
    """宣言の名前の後ろ(i)から本体の終わりまでを返す。本体は深さ0の { の対、無ければ深さ0の ; まで。

    WHY: 引数の型注釈の { で本体を切らないよう、( と [ の中は深さに数える。
    """
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0 and c == ";":
            return text[i:j]
        elif depth == 0 and c == "{":
            return text[i:_close(text, j) + 1]
    return text[i:]


def _imported_names(text):
    """import 文で持ち込む名前の集まり。別ファイルの関数を CSV が呼ぶ形を見分けるのに使う。"""
    out = set()
    for m in _IMPORT.finditer(text):
        for part in re.split(r"[,{}]", m.group(1)):
            w = part.strip().split(" as ")[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", w):
                out.add(w)
    return out


def _formatter_names(sources):
    """TSX・JSX の中で数値を桁区切り・小数に整形する関数の名前の集まり。呼出元: _csv_formats_numbers()。

    整形の元は toLocaleString( ・Intl.NumberFormat・.toFixed( を本体に持つ宣言。それを呼ぶ宣言も、本体の呼出を辿って含める(fmt を包む関数など)。
    WHY: 整形の関数は画面の中に入れ子で書かれることが多い(Card の中の fmt)。モジュール直下だけを見ると CSV の呼出を取り逃す。
    """
    decls = {}
    for p in sources:
        if Path(p).suffix not in (".tsx", ".jsx"):
            continue
        text = Path(p).read_text(encoding="utf-8")
        for m in _DECL.finditer(text):
            decls.setdefault(m.group(1), []).append(_decl_body(text, m.end()))
    fmt = {n for n, bodies in decls.items() if any(_FORMAT_CALL.search(b) for b in bodies)}
    grew = True
    while grew:
        grew = False
        for n, bodies in decls.items():
            if n not in fmt and any(re.search(rf"\b{re.escape(f)}\s*\(", b) for b in bodies for f in fmt):
                fmt.add(n)
                grew = True
    return fmt


def _csv_formats(c, fmt):
    """1つの CSV 出力の関数本体が数値を整形するか。True・False、判定できないときは None。

    WHY: 別ファイルから import した関数を CSV が呼ぶと、その中で整形しているかは見えない。False と言い切らず None にする。
    """
    src = (c.get("src") or [None])[0]
    if not src:
        return None
    text = Path(src["file"]).read_text(encoding="utf-8")
    body = "\n".join(text.splitlines()[src["line_start"] - 1:src["line_end"]])
    if _FORMAT_CALL.search(body) or any(re.search(rf"\b{re.escape(f)}\s*\(", body) for f in fmt):
        return True
    if any(re.search(rf"\b{re.escape(n)}\s*\(", body) for n in _imported_names(text) - fmt):
        return None
    return False


def _csv_formats_numbers(csv_exports, sources):
    """各 CSV 出力に formats_numbers を足す(true=値を整形する / false=整形しない / null=判定できない)。呼出元: main()。

    WHY: 画面の桁区切りを CSV に持ち込むかは、CSV の関数が整形の関数を呼ぶかで決まる。モデルに推測させず、下書きの事実にする(V5・V7 が読む)。
    """
    fmt = _formatter_names(sources)
    for c in csv_exports:
        c["formats_numbers"] = _csv_formats(c, fmt)


def _continuous_fact_map(ops):
    """連続操作の後から届く応答・読込中表示の事実(outcomes)を、その操作のイベントの fact_lines の形で返す。呼出元: main()。"""
    out = {}
    for i, op in enumerate(ops, 1):
        for o in op.get("outcomes", []):
            key = "continuous.stale" if o["kind"] == "stale_overwrite" else "continuous.loading"
            out.setdefault(op["event"], []).append({"key": f"continuous.{i}", "text": o["fact"], "position": "end",
                                                    "match": _FACT_MATCH[key], "src": o.get("src", [])})
    return out


def _attach_facts(draft, extra):
    """extra(event_code → 事実の行)を event_processes[].fact_lines に足す。同じ text は1回だけ。呼出元: main()。"""
    for ep in draft["event_processes"]:
        cur = ep.setdefault("fact_lines", [])
        for f in extra.get(ep["event_code"], []):
            if not any(x["text"] == f["text"] for x in cur):
                cur.append(f)


def build_draft(links, unresolved, screen_name, failure_effects=(), state_names=None):
    """解析結果を Phase 1 の下書き JSON(dict)に組む。

    failure_effects は _react_facts.extract の戻りの一覧。イベントごとの details の失敗時の行に使う(省略時は一般文)。

    db_operations は action の初出順に1件ずつ作り、各イベントの details の呼出の行は
    その db_operations の並びで出す(verify_partials.py がこの一致を検査する)。
    content は業務語彙が要るので空のまま出す(LLM が書く)。
    """
    events = []
    op_order = []  # action の初出順 = db_operations の並び
    triggers = {}  # action -> [event_code,...]
    n = 0  # EV01 からの連番。attr="mount" の link だけ EV00(初期表示)で、連番を進めない
    for link in links:
        if link["attr"] == "mount":
            code, trigger, action_word = "EV00", f"{screen_name}の初期表示時", "初期表示"
        else:
            n += 1
            code = f"EV{n:02d}"
            action_word = ACTION_WORDS.get(link["attr"], link["attr"])
            trigger = f"{screen_name}の{link['element']}を{action_word}する。"
        events.append({
            "event_code": code,
            "trigger": trigger,
            "action": action_word,
            "content": "",
            "classification": "サーバー" if link["actions"] else "クライアント",
            "ui_label": link.get("ui_label", ""),
            "source": {"file": link["file"], "line": link["line"], "handler": link["handler"]},
            "src": link.get("src", []),
        })
        for act in link["actions"]:
            if act not in triggers:
                op_order.append(act)
                triggers[act] = []
            if code not in triggers[act]:
                triggers[act].append(code)

    # 目的: 操作ごとの呼出位置(全イベントの呼出を集めて重複を除く)。接続: db_operations[].api_endpoint.call_sites
    call_sites = {}
    for link in links:
        for c in link["calls"]:
            lst = call_sites.setdefault(c["action"], [])
            for sp in c["src"]:
                if sp not in lst:
                    lst.append(sp)
    db_operations = [
        {"api_endpoint": {"action": act, "trigger_events": triggers[act], "call_sites": call_sites.get(act, [])}}
        for act in op_order
    ]
    event_processes = []
    for ev, link in zip(events, links):
        called = set(link["actions"])
        # EV00 以外は先頭に未記入の印を置く(details を空のまま merge させないため。verify_partials が残りを [未記入] にする)
        todo = [] if ev["event_code"] == "EV00" else [DETAIL_TODO]
        event_processes.append({
            "event_code": ev["event_code"],
            "src": ev["src"],
            # 目的: このイベントが呼ぶ API 呼出の位置(action と出典)。接続: 合成側が details の行と照合する
            "calls": link["calls"],
            "details": todo + [f"・{act} をリクエストする" for act in op_order if act in called]
            + _detail_lines(link, called, [f for f in failure_effects if f["event"] == ev["event_code"]]),
            # 目的: 開始時・成功時・失敗時・0件の事実の行(案 B5)。接続: B6 が position どおりに強制する
            "fact_lines": _event_facts(link, [f for f in failure_effects if f["event"] == ev["event_code"]],
                                       state_names or {}),
        })
    return {
        "events": events,
        "db_operations": db_operations,
        "event_processes": event_processes,
        "unresolved": unresolved,
    }


_OBJ_LIT = re.compile(r"\{([^{}]*)\}")


def _row_specs(body, count, path=None, text=None, base=0):
    """定数配列の中の {label, csv, kin, cnt} の並びを specs にする。count は全配列を通した label の出現数。

    呼出元: _display_rows()。意味合い: 同じ label(「小計」等)の行が複数あるため、名前だけでは結ぶ先が決まらない。
    nth(同じ label の何番目か)を持たせ、合成が画面の項目を番号で結べるようにする(案 B5 d)。
    note(画面項目の備考の元の文)と src(その spec のオブジェクトリテラルの行範囲)も持たせる(U6・U11 が読む)。
    body は text の base 位置から始まる部分。path と text が無いときは src を空にする。
    """
    note_re = re.compile(r"\bnote\s*:\s*((?:['\"][^'\"\n]*['\"]\s*\+?\s*)+)")
    specs = []
    for om in _OBJ_LIT.finditer(body):
        ob = om.group(1)
        lm = re.search(r"\b(?:label|header|title)\s*:\s*['\"]([^'\"]+)['\"]", ob)
        if not lm:
            continue
        label = lm.group(1)
        count[label] = count.get(label, 0) + 1

        def attr(name):
            am = re.search(rf"\b{name}\s*:\s*['\"]([^'\"]*)['\"]", ob)
            return am.group(1) if am else None

        # WHY: dead の項目は画面の表示切替で隠れても、CSV の見出しには全項目出る(表示と CSV の差の事実)
        # WHY: 複数行の文字列連結(note: "a" + "b")は連結して1つの文にする。note が無ければ null
        nm = note_re.search(ob)
        note = "".join(re.findall(r"['\"]([^'\"]*)['\"]", nm.group(1))) if nm else None
        specs.append({"label": label, "csv": attr("csv"), "kin": attr("kin"), "cnt": attr("cnt"),
                      "nth": count[label], "dead": ("dead: true" in ob) or ("dead:true" in ob),
                      "note": note,
                      "src": [_span(path, text, base + om.start(), base + om.end())] if path is not None else []})
    return specs


def _display_rows(sources):
    """TSX の モジュール直下の定数配列(const X = [ {label: '...'}, ... ])から、表示行の名前を集める。

    呼出元: main()。意味合い: 画面の表示行(左右の欄など)を下書きに残すため、配列の名前と label/header/title の文言を出す。
    接続: 出力は draft["display_rows"]。配列の中身は文字列リテラルの label/header/title だけを拾う(推測で足さない)。
    """
    rows = []
    count = {}  # 目的: 同じ label の出現数(全配列を通す)。nth の元
    for p in sources:
        if p.suffix not in (".tsx", ".jsx"):
            continue
        t = p.read_text(encoding="utf-8")
        for m in re.finditer(r"^const\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*\[", t, re.M):
            end = _close(t, m.end() - 1)
            labels = [x.group(1) for x in re.finditer(r"\b(?:label|header|title)\s*:\s*['\"]([^'\"]+)['\"]", t[m.end():end])]
            if labels:
                # 目的: 表示行の出典(配列の宣言の範囲)。接続: build_draft ではなく main() の display_rows に出る
                rows.append({"array": m.group(1), "labels": labels, "src": [_span(p, t, m.start(), end)],
                             "specs": _row_specs(t[m.end():end], count, p, t, m.end())})
    return rows


def _continuous_ops(links, sources=()):
    """データ取得を起こす操作(イベント属性・要素を問わず)ごとに、読込中の無効化・判定・取消処理の有無を判定する。

    呼出元: main()。意味合い: 連続操作(読込中にもう一度起こせるか、起こしたら前の要求を止めるか)を機械で出す。
    取得を起こす操作には onChange 等の state 変更も含む(useEffect の依存を経由して actions が付いたもの)。
    接続: links の tag(開きタグ)と texts(辿った本体)を見る。判定は文字の一致だけ(推測で足さない)。
    防止の仕組みが1つも無いときは prevention を「防止なし」にし、fact にもその旨を書く。
    """
    ops = []
    codes = _event_codes(links)
    for link, code in zip(links, codes):
        # 目的: mount(初期表示)は利用者の操作ではないので外す。接続: actions が空の属性は取得を起こさない
        if link["attr"] == "mount" or not (link["actions"] or link.get("fetch")):
            continue
        # 目的: 表示名にコード片・HTML 実体参照(&gt; など)を入れない。ui_label → 隣の label/state の画面項目名 → 「<要素>の変更」の順
        label = link.get("ui_label") or link.get("op_label") or f"{link['element'] or '要素'}の変更"
        body = "\n".join(link["texts"])
        disabled = bool(re.search(r"disabled=\{[^}]*(?:loading|busy|pending|fetch)", link.get("tag", ""), re.I))
        guard = bool(re.search(r"\bif\s*\(\s*!?\s*\w*(?:loading|busy|pending|fetching)\w*\s*\)", body, re.I))
        cancel = bool(re.search(r"AbortController|\.abort\(", body))
        prevented = disabled or guard or cancel
        fact = (f"操作「{label}」({link['element']} の {link['attr']})は読込中に"
                f"{'無効化される' if disabled else '無効化されない'}。取消処理は{'ある' if cancel else '無い'}。")
        if not prevented:
            # WHY: 「防止なし」は仕組みが無いことの事実。S3 の文と同じ言い方に揃える(案 B5 c)
            fact += "読込中の再操作を防ぐ仕組みは無い(無効化・loading の判定・中断が無い)。"
        ops.append({"event": code, "button": label, "disabled_while_loading": disabled, "guard": guard, "cancel": cancel,
                    "prevention": "あり" if prevented else "防止なし", "fact": fact,
                    # 目的: 後から届く応答・読込中表示の事実(C6)。接続: S3 の details に1行ずつ写す
                    "outcomes": _react_facts.outcomes(link, sources)})
    return ops


def write_draft(draft, out_path):
    """下書きを UTF-8 の JSON で書く(親フォルダは作る)。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_json(path):
    """JSON を読む。読めなければ None(呼出元が終了コード2にする)。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[入力不正] {path}: {e}", file=sys.stderr)
        return None


def main(argv=None):
    """CLI の入口。入力を検め、解析 → 組立 → 書出しを行い、終了コード 0/1/2 を返す。"""
    p = argparse.ArgumentParser(description="React(TSX) の画面から Phase 1 の下書き JSON を作る")
    p.add_argument("--config", help="screen_config.json のパス")
    p.add_argument("--project-config", help="project_config.json のパス(省略時は config の隣)")
    p.add_argument("--out", help="出力する下書き JSON のパス")
    p.add_argument("--selftest", action="store_true", help="組込みの検査を走らせる(--config/--out は不要)")
    args = p.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.config or not args.out:
        print("[入力不正] --config と --out が必要", file=sys.stderr)
        return 2

    cfg = _load_json(args.config)
    if cfg is None:
        return 2
    pc_path = args.project_config or str(args.config).replace("_screen_config.json", "_project_config.json")
    pcfg = _load_json(pc_path)
    if pcfg is None:
        return 2

    # source_files の展開は _config_loader.get_source_files と同じ取り決め(expandvars → expanduser)
    sources = [Path(os.path.expanduser(os.path.expandvars(s))) for s in cfg.get("source_files", []) if s]
    missing = [str(s) for s in sources if not s.is_file()]
    if not sources or missing:
        print(f"[入力不正] source_files が空か、無いファイルがある: {missing}", file=sys.stderr)
        return 2

    links, unresolved, excerpt = analyze_sources(sources, pcfg.get("api_call_functions", []))
    # 目的: 取得失敗時に変える state・残る state を下書きに足し(既存キーは変えない)、details の失敗時の行にも使う。接続: _react_facts.extract
    facts = _react_facts.extract(links, sources)
    # 目的: 画面部品の名前(bind された state の画面項目名)を、開始時・成功時の文の言い換えに使う。接続: _state_names
    names = _state_names(sources)
    draft = build_draft(links, unresolved, cfg.get("screen_name", ""), facts["failure_effects"], names)
    draft.update(facts)
    # 目的: CSV 出力の書式(BOM・区切り・改行・見出し・合計行)を下書きに足す(C1)。接続: _react_facts.csv_exports
    draft.update(_react_facts.csv_exports(links, sources))
    # 目的: 各 CSV 出力が数値を整形するか(formats_numbers)を下書きに足す。データ行の文の「桁区切りにしない」と、V7 の検査が読む
    _csv_formats_numbers(draft["csv_exports"], sources)
    # 目的: 計算式の候補(定数配列の calc)を下書きに足し、verify_vs_source.check_calcs が H4 の calculations と突き合わせる(B1)。接続: _react_facts.calc_candidates
    draft.update(_react_facts.calc_candidates(sources))
    # 目的: 入力・操作系の JSX 要素(label・種別の目安)を下書きに足し、verify_vs_source.check_items が画面項目と突き合わせる(B2)。接続: _react_facts.jsx_elements
    draft.update(_react_facts.jsx_elements(sources))
    # 目的: 処理の種別(表示切替・活性切替・絞り込み・入力検査)を下書きに足し、verify_vs_source.check_kinds が H4/S3 の書き方と突き合わせる(B5)。接続: _react_facts.behaviors
    draft.update(_react_facts.behaviors(sources))
    # 目的: 画面部品の中で使う応答項目(r.shop_name 等)の出どころを下書きに足し、S2 の response_to_screen_mapping の土台にする(C5)。接続: _react_facts.field_usage
    draft.update(_react_facts.field_usage(sources))
    # 目的: 画面の文言(JSX の文言・日本語の文字列リテラル)を下書きに足し、verify_vs_source.main の文言の照合に使う(B4)。接続: _react_facts.ui_strings
    draft.update(_react_facts.ui_strings(sources))
    # 目的: 初期表示の取得が並行か順かを EV00(events[0])に足す。接続: _react_facts.fetch_plan
    # 不具合の修正: api_call_functions は共通ラッパー(call)だけで、画面の useEffect が呼ぶ fetchXxx を含まない。
    #   画面が使う API 関数は excerpt(api.ts から抜いた断片)の定義名なので、その名前も渡す。
    api_names = list(pcfg.get("api_call_functions", [])) + re.findall(
        r"^(?:export\s+)?(?:async\s+)?(?:function\s+|const\s+)(\w+)", excerpt, re.M)
    plan = _react_facts.fetch_plan(sources, api_names)
    if plan:
        draft["events"][0]["fetch_plan"] = plan
    # 目的: 初期表示で呼ぶ API の一覧(initial_fetches)と、useState の初期値・結ぶ画面項目(state_initials)を下書きに足す。接続: _react_facts.initial_fetches / state_initials
    draft.update(_react_facts.initial_fetches(sources, api_names))
    draft.update(_react_facts.state_initials(sources))
    # 目的: 派生配列(useMemo・素の filter 等で作る配列)を下書きに足す。接続: _react_facts.derived_arrays。B4 の配線(B5 が担当)
    draft.update(_react_facts.derived_arrays(sources))
    # 目的: 表示行の名前と連続操作の判定を、下書きの追加キーとして出す(既存キーは変えない)。接続: _display_rows / _continuous_ops
    draft["display_rows"] = _display_rows(sources)
    draft["continuous_operation"] = _continuous_ops(links, sources)
    # 目的: CSV と連続操作の事実の行を、trigger の event_processes[].fact_lines に入れる(案 B5 b・c)
    # 目的: CSV の処理順(seq 1〜6)を、表示行の全項目(dead を含む)とファイル名の式の画面項目名で組む。接続: _csv_fact_map
    # 目的: 見出しの展開元(別名の配列 CSV_SPECS は元の LEFT・RIGHT に解く)を header.spread[].source に出し、事実の行の項目にも使う。接続: _spread_arrays / _alias_arrays
    aliases = _alias_arrays(sources)
    for c in draft.get("csv_exports") or []:
        for sp in (c.get("header") or {}).get("spread") or []:
            sp["source"] = _spread_arrays(sp.get("raw", ""), draft["display_rows"], aliases)
    _attach_facts(draft, _csv_fact_map(draft["csv_exports"], draft["display_rows"], names, aliases))
    _attach_facts(draft, _continuous_fact_map(draft["continuous_operation"]))
    write_draft(draft, Path(args.out))
    # api.ts から抜いた断片は下書きと同じフォルダへ、<機能名>_api_excerpt.ts として書く(機能名は config の名前から)
    if excerpt:
        feature = Path(args.config).name.replace("_screen_config.json", "")
        (Path(args.out).parent / f"{feature}_api_excerpt.ts").write_text(excerpt, encoding="utf-8")
    print(f"[OK] events={len(draft['events'])} db_operations={len(draft['db_operations'])} "
          f"unresolved={len(unresolved)} -> {args.out}")
    return 1 if unresolved else 0


def _selftest_b5():
    """事実の行(開始時・成功時・失敗時・CSV・連続操作)・表示行の specs・派生配列を、CLI の main() を通して assert する。

    呼出元: selftest()。意味合い: B5 の出力が下書きの欄に入り、B6 が読む形で出ることを確かめる。終了コード 0=OK / 1=NG。
    """
    import json
    import tempfile
    tsx = r'''import { useState, useEffect, useMemo } from "react";
import { call } from "./api";
const COLS = [
  { label: "小計", csv: "小計(税込)", kin: "s3" },
  { label: "小計", kin: "s4" },
];
export default function Screen() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [shop, setShop] = useState("");
  const visible = useMemo(() => rows.filter((r) => r.days > 0), [rows]);
  const load = () => {
    setLoading(true);
    setError(null);
    call("list").then((d) => setRows(d.rows)).catch((e) => setError(String(e))).finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, []);
  const exportCsv = () => {
    const q = (v) => `"${String(v).replaceAll('"', '""')}"`;
    const header = ["店舗", "日付"];
    const line = (r, agg) => [q(r ? r.shop : "合計"), r ? r.days : ""].join(",");
    const lines = rows.map((r, i) => line(r, i));
    if (rows.length > 1) lines.push(line(null, 0));
    const blob = new Blob([[header.join(","), ...lines].join("\r\n")], { type: "text/csv" });
  };
  return (
    <div>
      <label>店舗</label>
      <input type="text" value={shop} onChange={(e) => { setShop(e.target.value); load(); }} />
      <button onClick={exportCsv}>CSV出力</button>
    </div>
  );
}
'''
    try:
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            (dp / "S.tsx").write_text(tsx, encoding="utf-8")
            (dp / "S_screen_config.json").write_text(json.dumps(
                {"screen_name": "テスト画面", "source_files": [str(dp / "S.tsx")]}, ensure_ascii=False), encoding="utf-8")
            (dp / "S_project_config.json").write_text(json.dumps(
                {"api_call_functions": ["call"]}, ensure_ascii=False), encoding="utf-8")
            out = dp / "draft.json"
            main(["--config", str(dp / "S_screen_config.json"), "--project-config", str(dp / "S_project_config.json"),
                  "--out", str(out)])
            draft = json.loads(out.read_text(encoding="utf-8"))
        ep = {e["event_code"]: e for e in draft["event_processes"]}
        facts = {k: [(f["text"], f["position"], f["key"]) for f in v["fact_lines"]] for k, v in ep.items()}
        start = "取得の開始時に、loading を true にする・error を空にする"
        assert (start, "before_calls", "start") in facts["EV01"], f"EV01 の開始時: {facts['EV01']}"
        assert (start, "before_calls", "start") in facts["EV00"], f"EV00 の開始時: {facts['EV00']}"
        assert ("取得に成功したときは、rows を応答の内容に置き換える", "after_calls", "success") in facts["EV01"], facts["EV01"]
        assert any(t.startswith("取得に失敗したときは、error") and p == "after_calls" for t, p, _ in facts["EV01"]), facts["EV01"]
        assert any(k.startswith("continuous.") and p == "end" and "上書きする" in t for t, p, k in facts["EV01"]), facts["EV01"]
        assert ("値の中の二重引用符は2つ重ねる", "seq", "csv.escape") in facts["EV02"], facts["EV02"]
        assert any(k == "csv.total_blank" and p == "seq" and "日付 は無記入にする" in t
                   for t, p, k in facts["EV02"]), facts["EV02"]
        assert draft["continuous_operation"][0]["event"] == "EV01", draft["continuous_operation"]
        assert "読込中の再操作を防ぐ仕組みは無い" in draft["continuous_operation"][0]["fact"], draft["continuous_operation"][0]
        got_specs = [{k: s[k] for k in ("label", "csv", "kin", "cnt", "nth", "dead")} for s in draft["display_rows"][0]["specs"]]
        assert got_specs == [
            {"label": "小計", "csv": "小計(税込)", "kin": "s3", "cnt": None, "nth": 1, "dead": False},
            {"label": "小計", "csv": None, "kin": "s4", "cnt": None, "nth": 2, "dead": False},
        ], draft["display_rows"]
        assert [s["note"] for s in draft["display_rows"][0]["specs"]] == [None, None], draft["display_rows"]
        assert any(a.get("name") == "visible" and a.get("source") == "rows" for a in draft.get("derived_arrays", [])), \
            f"derived_arrays: {draft.get('derived_arrays')}"
    except AssertionError as e:
        print(f"[selftest NG] B5 {e}")
        return 1
    except Exception as e:
        print(f"[selftest NG] B5 例外: {e!r}")
        return 1
    return 0


def _selftest_t4():
    """CSV の処理順(seq 1〜6)・dead の項目・点数の2列・合計行の when の訳・ファイル名の画面項目名を、_csv_fact_map に直接渡して assert する。

    呼出元: selftest()。意味合い: 1つの CSV 出力から seq の事実が機械で組まれることを確かめる。終了コード 0=OK / 1=NG。
    """
    dr = [{"array": "LEFT", "labels": ["小計", "旧項目"], "src": [], "specs": [
        {"label": "小計", "csv": "小計1(値引前)", "kin": "s1", "cnt": "c1", "nth": 1, "dead": False},
        {"label": "旧項目", "csv": None, "kin": "s2", "cnt": None, "nth": 1, "dead": True}]}]
    cv = {"function": "exp", "trigger_events": ["EV07"], "file_name_template": "pos_${applied.from}.csv",
          "mime": "text/csv;charset=utf-8", "bom": True, "separator": ",", "line_break": "CRLF",
          "quote": {"fn": "q", "escape": '"→""', "uses_count": 1},
          "header": {"fixed": ["開始日", "店舗"],
                     "spread": [{"raw": "...LEFT.filter(c => showDead || !c.dead).map(c => c.label)", "cnt_suffixes": []}]},
          "total_row": {"when": "rows.length > 1"},
          "row_builder": {"columns": [
              {"kind": "ternary", "expr": "r ? r.days : ''", "quoted": False, "row_value": "r.days", "total_value": ""},
              {"kind": "ternary", "expr": "r ? shop : '合計'", "quoted": True, "row_value": "shop", "total_value": '"合計"'},
              {"kind": "spread", "expr": "...", "quoted": False}],
              "value_separator": " "},
          "joins": [{"target": "dates", "separator": " "}], "src": []}
    try:
        got = _csv_fact_map([cv], dr, {"applied.from": "集計開始日"})["EV07"]
        f = {x["seq"]: x for x in got}
        assert sorted(f) == [1, 2, 3, 4, 5, 6], f"seq: {sorted(f)}"
        assert all(x["position"] == "seq" for x in got), "position は seq"
        assert "小計1(値引前)(点数)、小計1(値引前)(金額)" in f[1]["text"], f[1]["text"]
        assert "旧項目" in f[1]["text"] and "表示の切替に関係なく" in f[1]["text"], f[1]["text"]
        assert "半角スペース区切り" in f[2]["text"], f[2]["text"]
        assert "表示が2行以上のとき" in f[3]["text"] and "開始日 は無記入にする" in f[3]["text"], f[3]["text"]
        assert "店舗は「合計」" in f[3]["text"], f[3]["text"]
        assert f[5]["text"].startswith("全体を連結する"), f[5]["text"]
        assert f[6]["text"] == "ファイル名は「pos_(確定済みの集計開始日の値).csv」で保存する", f[6]["text"]
        # 決まらない式は「(式: …)」で出す(${…} の穴は残さない)
        assert _file_name("a_${rows.length}.csv", {}) == "a_(式: rows.length).csv", _file_name("a_${rows.length}.csv", {})
        # 欄の写像は表示の確定の setter の引数から読む(setApplied({ from, to }) → applied.from は from の項目)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "P.tsx"
            tp.write_text("export default function P() {\n"
                          "  const [from, setFrom] = useState(\"\");\n"
                          "  const [to, setTo] = useState(\"\");\n"
                          "  const [applied, setApplied] = useState({ from: \"\", to: \"\" });\n"
                          "  const show = () => setApplied({ from, to });\n"
                          "  return <div><label>集計開始日</label><input value={from} onChange={(e) => setFrom(e.target.value)} />"
                          "<label>集計終了日</label><input value={to} onChange={(e) => setTo(e.target.value)} /></div>;\n"
                          "}\n", encoding="utf-8")
            nm = _state_names([tp])
        assert nm.get("applied.from") == "集計開始日" and nm.get("applied.to") == "集計終了日", nm
        assert _file_name("pos_${applied.from}_${applied.to}.csv", nm) == "pos_(確定済みの集計開始日の値)_(確定済みの集計終了日の値).csv", nm
    except AssertionError as e:
        print(f"[selftest NG] T4 {e}")
        return 1
    return 0


def _selftest_u6():
    """別名の配列の展開・データ行の値の言い方・合計行の引用符つきの空と無記入・確定済みの欄・spec の note と src を assert する。

    呼出元: selftest()。意味合い: 実データ(CSV_SPECS = [...LEFT, ...RIGHT]・applied.from・複数行の note 連結)と
    同じ形を小さく再現して確かめる。終了コード 0=OK / 1=NG。
    """
    import tempfile
    tsx = (
        'const LEFT = [\n'
        '  { label: "小計", csv: "小計1", kin: "s1", cnt: "c1" },\n'
        '  { label: "旧項目", kin: "s2", dead: true,\n'
        '    note: "前の" +\n'
        '      "行の注記" },\n'
        '];\n'
        'const RIGHT = [\n'
        '  { label: "現金", kin: "k1", note: "最終計上 2015-06-15" },\n'
        '];\n'
        'const CSV_SPECS = [...LEFT, ...RIGHT];\n'
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "P.tsx"
            tp.write_text(tsx, encoding="utf-8")
            aliases = _alias_arrays([tp])
            rows = _display_rows([tp])
        assert aliases == {"CSV_SPECS": ["LEFT", "RIGHT"]}, f"別名の配列: {aliases}"
        byname = {r["array"]: r for r in rows}
        assert set(byname) == {"LEFT", "RIGHT"}, f"表示行: {list(byname)}"
        left = byname["LEFT"]["specs"]
        assert left[0]["note"] is None and left[1]["note"] == "前の行の注記", f"note: {left}"
        assert byname["RIGHT"]["specs"][0]["note"] == "最終計上 2015-06-15", byname["RIGHT"]["specs"]
        assert left[0]["src"] == [{"file": str(tp), "line_start": 2, "line_end": 2}], left[0]["src"]
        assert left[1]["src"] == [{"file": str(tp), "line_start": 3, "line_end": 5}], left[1]["src"]
        assert _spread_arrays("...LEFT.filter((c) => c.dead)", rows, aliases) == ["LEFT"], "表示行の名前はそのまま"
        cv = {"trigger_events": ["EV09"], "file_name_template": "x_${applied.from}.csv", "bom": False,
              "separator": ",", "line_break": "CRLF", "quote": {"escape": '"→""'},
              "header": {"fixed": ["開始日", "店舗", "日数", "取込欠損疑い日"],
                         "spread": [{"raw": "...CSV_SPECS.flatMap((s) => (s.cnt ? [a, b] : [b]))"}]},
              "total_row": {"when": "rows.length > 1"},
              "row_builder": {"columns": [
                  {"kind": "same", "expr": "applied.from", "quoted": False},
                  {"kind": "ternary", "expr": "q(r ? r.shop : \"合計\")", "quoted": True, "total_value": "\"合計\""},
                  {"kind": "ternary", "expr": "r ? r.days : \"\"", "quoted": False, "total_value": "\"\""},
                  {"kind": "ternary", "expr": "q(r ? r.warn : \"\")", "quoted": True, "total_value": "\"\""},
                  {"kind": "spread", "expr": "...CSV_SPECS.flatMap((s) => (s.cnt ? [a, b] : [b]))", "quoted": False}],
                  "value_separator": ","},
              "src": []}
        got = _csv_fact_map([cv], rows, {"applied.from": "集計開始日"}, aliases)["EV09"]
        f = {x["seq"]: x["text"] for x in got}
        assert "小計1(点数)、小計1(金額)、旧項目、現金" in f[1], f"見出しの項目(別名を解いた並び): {f[1]}"
        assert "表示の切替に関係なく全項目を出す" in f[1], f[1]
        assert "開始日(確定済みの集計開始日の値)" in f[2], f"データ行の確定済みの欄: {f[2]}"
        assert "点数のある項目は点数と金額の2列、無い項目は金額の1列の値を並べる" in f[2], f[2]
        assert "合計行では 取込欠損疑い日 は引用符つきの空、日数 は無記入にする" in f[3], f[3]
        assert "ファイル名は「x_(確定済みの集計開始日の値).csv」で保存する" == f[6], f[6]
    except AssertionError as e:
        print(f"[selftest NG] U6 {e}")
        return 1
    except Exception as e:
        print(f"[selftest NG] U6 例外: {e!r}")
        return 1
    return 0


def _selftest_v5():
    """合計行の同じ式の列・展開の列、データ行の `?? リテラル` の既定値、CSV の整形の判定(formats_numbers)を assert する。

    呼出元: selftest()。意味合い: 合計行が同じ式の列と展開の列まで書かれること、null のときの値を書くこと、
    CSV の関数が整形の関数を呼ぶか(入れ子の関数の辿り・直の呼出・別ファイルの import)を小さな TSX で確かめる。終了コード 0=OK / 1=NG。
    """
    import tempfile
    names = {"applied.from": "集計開始日"}
    # fallback-ok: 実データの列の式(r.reji_no ?? "")を再現する試験の入力。null のとき空文字になることを書く事実の確認
    fallback_expr = 'r ? r.reji_no ?? "" : ""'
    cv = {"function": "exp", "trigger_events": ["EV08"], "file_name_template": None,
          "header": {"fixed": ["開始日", "レジNO"], "spread": [{"raw": "...LEFT", "cnt_suffixes": []}]},
          "total_row": {"when": "rows.length > 1"},
          "row_builder": {"columns": [
              {"kind": "same", "expr": "applied.from", "quoted": False},
              {"kind": "ternary", "expr": fallback_expr, "quoted": False,
               "row_value": 'r.reji_no ?? ""', "total_value": '""'},
              {"kind": "spread", "expr": "...", "quoted": False}],
              "value_separator": " ", "total_call": {"lines": "lines", "agg_arg": "rows", "agg_expr": "sumAggs(rows)"}},
          "joins": [], "src": []}
    try:
        got = {x["seq"]: x for x in _csv_fact_map([cv], [], names)["EV08"]}
        assert "開始日(確定済みの集計開始日の値)、レジNO(値が無い(null)ときは空文字)" in got[2]["text"], got[2]["text"]
        assert "桁区切り" not in got[2]["text"], "formats_numbers が無いのに整形の文が出た"
        assert "開始日は確定済みの集計開始日の値" in got[3]["text"], got[3]["text"]
        assert "続く各項目は全行の合算" in got[3]["text"], got[3]["text"]
        assert got[3]["key"] == "csv.total_blank" and "レジNO は無記入にする" in got[3]["text"], got[3]
        cv["row_builder"]["total_call"]["agg_expr"] = "tally(rows)"
        got = {x["seq"]: x for x in _csv_fact_map([cv], [], names)["EV08"]}
        assert "続く各項目はtally(rows)で求めた値" in got[3]["text"], got[3]["text"]
        cv["formats_numbers"] = False
        got = {x["seq"]: x for x in _csv_fact_map([cv], [], names)["EV08"]}
        assert got[2]["text"].endswith("。数値は桁区切りにせず、そのまま出す"), got[2]["text"]
        # 整形の判定: 入れ子の関数の辿り・直の呼出は true、別ファイルの import は判定できず null、整形なしは false
        csv_head = "  const exportCsv = () => {"
        cases = {
            "transitive": (["const fmt = (n) => n.toLocaleString();", "export default function P() {",
                            "  function rowsToCsv(rows) {", "    return rows.map((r) => fmt(r.kin)).join(\",\");", "  }"],
                           ["rowsToCsv(rows);"], True),
            "direct": (["export default function P() {"], ["rows.map((r) => r.kin.toLocaleString()).join(\",\");"], True),
            "imported": (['import { fmtYen } from "./yen";', "export default function P() {"],
                         ["rows.map((r) => fmtYen(r.kin)).join(\",\");"], None),
            "plain": (["const money = (n) => n.toLocaleString();", "export default function P() {"],
                      ["rows.map((r) => r.kin).join(\",\");"], False),
        }
        with tempfile.TemporaryDirectory() as td:
            for case, (head, call, want) in cases.items():
                tp = Path(td) / f"{case}.tsx"
                lines = head + [csv_head, f"    const body = {call[0]}", "    const blob = new Blob([body]);", "  };",
                                "  return null;", "}"]
                tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                a = lines.index(csv_head)
                c = {"src": [{"file": str(tp), "line_start": a + 1, "line_end": a + 4}]}
                _csv_formats_numbers([c], [tp])
                assert c["formats_numbers"] is want, f"{case}: {c['formats_numbers']!r} != {want!r}"
    except AssertionError as e:
        print(f"[selftest NG] V5 {e}")
        return 1
    except Exception as e:
        print(f"[selftest NG] V5 例外: {e!r}")
        return 1
    return 0


def selftest():
    """最小の api.ts と TSX(ラッパー経由・useEffect 連鎖・解決不能の1件)で、解析と組立の期待値を assert する。

    呼出元: main() の --selftest。終了コード 0=OK / 1=NG。
    意味合い: 解析の骨格(どのイベントがどの action を呼ぶか・unresolved に出るか)が崩れたら止まるための試験。
    """
    import tempfile
    api_src = (
        'export function call(action: string) {\n'
        '  return fetch("/api/" + action);\n'
        '}\n'
    )
    view_src = (
        'import { useState, useEffect } from "react";\n'
        'import { call } from "./api";\n'
        'function fetchX() {\n'
        '  return call("wrapped");\n'
        '}\n'
        'export default function Screen() {\n'
        '  const [count, setCount] = useState(0);\n'
        '  const handleClick = () => { setCount(count + 1); };\n'
        '  useEffect(() => { call("load"); }, [count]);\n'
        '  useEffect(() => { call("init"); }, []);\n'
        '  return (\n'
        '    <div>\n'
        '      <button onClick={handleClick}>go</button>\n'
        '      <input onChange={() => fetchX()} />\n'
        '      <span onClick={props.onSave}>save</span>\n'
        '    </div>\n'
        '  );\n'
        '}\n'
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            (dp / "api.ts").write_text(api_src, encoding="utf-8")
            (dp / "Screen.tsx").write_text(view_src, encoding="utf-8")
            links, unresolved, _excerpt = analyze_sources([dp / "api.ts", dp / "Screen.tsx"], ["call"])
        assert [k["attr"] for k in links] == ["mount", "onClick", "onChange"], f"links の attr: {[k['attr'] for k in links]}"
        assert links[0]["actions"] == ["load", "init"], f"mount の actions(全 useEffect・出現順): {links[0]['actions']}"
        assert links[1]["actions"] == ["load"], f"button の actions: {links[1]['actions']}"
        assert links[2]["actions"] == ["wrapped"], f"input の actions: {links[2]['actions']}"
        assert "export function call" in _excerpt, "excerpt に api.ts の定義が出ない"
        assert "Screen.tsx" not in _excerpt, "画面の関数が excerpt に混ざった"
        assert re.search(r"// 出典: \S*api\.ts:\d+-\d+", _excerpt), "excerpt の見出しが 出典: path:行-行 になっていない"
        assert len(unresolved) == 1 and "props.onSave" in unresolved[0]["reason"], f"unresolved: {unresolved}"
        draft = build_draft(links, unresolved, "テスト画面")
        assert [e["event_code"] for e in draft["events"]] == ["EV00", "EV01", "EV02"], "event_code(EV00 = 初期表示、以降 EV01…)"
        assert draft["events"][0]["trigger"] == "テスト画面の初期表示時", "EV00 の trigger"
        # 目的: src の行番号が TSX の実際の行と一致すること(load の effect は 9 行目、onClick は 13 行目)
        assert draft["events"][0]["src"][0]["line_start"] == 9, f"EV00 の src: {draft['events'][0]['src']}"
        assert draft["events"][1]["src"][0] == {"file": str(dp / "Screen.tsx"), "line_start": 13, "line_end": 13}, "EV01 の src"
        assert draft["db_operations"] == [
            {"api_endpoint": {"action": "load", "trigger_events": ["EV00", "EV01"],
                              "call_sites": [{"file": str(dp / "Screen.tsx"), "line_start": 9, "line_end": 9}]}},
            {"api_endpoint": {"action": "init", "trigger_events": ["EV00"],
                              "call_sites": [{"file": str(dp / "Screen.tsx"), "line_start": 10, "line_end": 10}]}},
            {"api_endpoint": {"action": "wrapped", "trigger_events": ["EV02"],
                              "call_sites": [{"file": str(dp / "Screen.tsx"), "line_start": 4, "line_end": 4}]}},
        ], f"db_operations: {draft['db_operations']}"
        assert draft["event_processes"][0]["details"] == ["・load をリクエストする", "・init をリクエストする"], "EV00 の details"
        assert draft["event_processes"][1]["details"] == [DETAIL_TODO, "・load をリクエストする"], "EV01 の details"
        assert draft["event_processes"][2]["details"] == [DETAIL_TODO, "・wrapped をリクエストする"], "EV02 の details"
    except AssertionError as e:
        print(f"[selftest NG] {e}")
        return 1
    if _selftest_b5():
        return 1
    if _selftest_t4():
        return 1
    if _selftest_u6():
        return 1
    if _selftest_v5():
        return 1
    print("[selftest OK] extract_react_draft")
    return 0


if __name__ == "__main__":
    sys.exit(main())
