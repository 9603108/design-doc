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
#          文言の〈〉は LLM が埋める印。接続: _robust_lines() が使う。推測では足さない(拾えない形は何も出さない)。
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
            lm = _API_LIT.match(text, i + len(name), e)
            if lm:
                if lm.group(2) not in acc["actions"]:
                    acc["actions"].append(lm.group(2))
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
        parts.append(f"// 出典: {path}\n{body}")
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
            parts.append(f"// 出典: {path}\n{text[m.start():end]}")
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
            acc = {"actions": [], "states": set(), "hit": False, "texts": []}
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
                "ui_label": _ui_label(t, tags, m, be),
                "empty": [(x, branches[x]) for x in sorted(acc["states"]) if x in branches],
            })
    # EV00(初期表示): マウント時には全ての useEffect が走るので、その呼出を出現順に集めて links の先頭に置く。
    # 意味合い: パイプラインの決まり(EV00 = 初期表示)に合わせる。接続: build_draft が attr="mount" を EV00 にする。
    # ponytail: effect 内の条件分岐(if で呼ばない場合)は見ない。呼ぶ可能性のある action を全部出す。
    mount = {"actions": [], "states": set(), "hit": False, "texts": []}
    for _deps, span in effects:
        _resolve(span, ctx, set(), mount)
    first = effects[0][1] if effects else None
    links.insert(0, {
        "file": str(first[0]) if first else "", "line": _line(first[1], first[2]) if first else 0,
        "element": "", "attr": "mount", "handler": "useEffect(マウント時)", "actions": mount["actions"],
        "texts": mount["texts"], "ui_label": "", "empty": [],
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


def _robust_lines(texts):
    """辿った本体の文字から、失敗時・連続操作・0件の下書き行を返す(拾えたものだけ)。

    呼出元: build_draft()。意味合い: API を呼ぶイベントの処理ステップに、画面の振る舞いの骨格を機械的に足す。
    """
    body = "\n".join(texts)
    lines = []
    if _FAIL_HINT.search(body):
        lines.append("取得に失敗したときは〈エラー表示の項目〉にメッセージを表示する")
    if _RACE_HINT.search(body):
        lines.append("連続して操作したときは、最後の要求の応答だけを反映する")
    if _EMPTY_HINT.search(body):
        lines.append("0件のときは〈該当なし表示〉を出す")
    return lines


def _detail_lines(link, called):
    """呼ぶ API があるイベントの details に足す描画の行。0件分岐は状態が一致した分だけ、分岐の文言つきに置き換える。

    呼出元: build_draft()。意味合い: 0件の行を、JSX に書かれた表示文言で具体化する(文言が無ければ〈〉の印のまま)。
    """
    if not called:
        return []
    lines = _robust_lines(link["texts"])
    if link.get("empty"):
        lines = [ln for ln in lines if not ln.startswith("0件のとき")]
        lines += [f"0件のときは〈該当なし表示〉を出す(画面の文言: 「{txt}」)" for _x, txt in link["empty"]]
    return lines


def build_draft(links, unresolved, screen_name):
    """解析結果を Phase 1 の下書き JSON(dict)に組む。

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
        })
        for act in link["actions"]:
            if act not in triggers:
                op_order.append(act)
                triggers[act] = []
            if code not in triggers[act]:
                triggers[act].append(code)

    db_operations = [
        {"api_endpoint": {"action": act, "trigger_events": triggers[act]}} for act in op_order
    ]
    event_processes = []
    for ev, link in zip(events, links):
        called = set(link["actions"])
        # EV00 以外は先頭に未記入の印を置く(details を空のまま merge させないため。verify_partials が残りを [未記入] にする)
        todo = [] if ev["event_code"] == "EV00" else [DETAIL_TODO]
        event_processes.append({
            "event_code": ev["event_code"],
            "details": todo + [f"・{act} をリクエストする" for act in op_order if act in called]
            + _detail_lines(link, called),
        })
    return {
        "events": events,
        "db_operations": db_operations,
        "event_processes": event_processes,
        "unresolved": unresolved,
    }


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
    draft = build_draft(links, unresolved, cfg.get("screen_name", ""))
    write_draft(draft, Path(args.out))
    # api.ts から抜いた断片は下書きと同じフォルダへ、<機能名>_api_excerpt.ts として書く(機能名は config の名前から)
    if excerpt:
        feature = Path(args.config).name.replace("_screen_config.json", "")
        (Path(args.out).parent / f"{feature}_api_excerpt.ts").write_text(excerpt, encoding="utf-8")
    print(f"[OK] events={len(draft['events'])} db_operations={len(draft['db_operations'])} "
          f"unresolved={len(unresolved)} -> {args.out}")
    return 1 if unresolved else 0


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
        assert len(unresolved) == 1 and "props.onSave" in unresolved[0]["reason"], f"unresolved: {unresolved}"
        draft = build_draft(links, unresolved, "テスト画面")
        assert [e["event_code"] for e in draft["events"]] == ["EV00", "EV01", "EV02"], "event_code(EV00 = 初期表示、以降 EV01…)"
        assert draft["events"][0]["trigger"] == "テスト画面の初期表示時", "EV00 の trigger"
        assert draft["db_operations"] == [
            {"api_endpoint": {"action": "load", "trigger_events": ["EV00", "EV01"]}},
            {"api_endpoint": {"action": "init", "trigger_events": ["EV00"]}},
            {"api_endpoint": {"action": "wrapped", "trigger_events": ["EV02"]}},
        ], f"db_operations: {draft['db_operations']}"
        assert draft["event_processes"][0]["details"] == ["・load をリクエストする", "・init をリクエストする"], "EV00 の details"
        assert draft["event_processes"][1]["details"] == [DETAIL_TODO, "・load をリクエストする"], "EV01 の details"
        assert draft["event_processes"][2]["details"] == [DETAIL_TODO, "・wrapped をリクエストする"], "EV02 の details"
    except AssertionError as e:
        print(f"[selftest NG] {e}")
        return 1
    print("[selftest OK] extract_react_draft")
    return 0


if __name__ == "__main__":
    sys.exit(main())
