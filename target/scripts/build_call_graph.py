#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_call_graph.py — フロント JS の apiCall とバックエンドの入口ファイルの action ディスパッチを突合し
                       中間 JSON の呼出グラフを補完する（v41 で新設、汎用）

【目的】
ソースに書かれている呼出関係をスキルが見逃し、実際には呼ばれている B00X を「呼ばれていない」と扱ってしまう問題への
本質対応。Phase 1 ソース解析が呼出グラフ（apiCall / action ディスパッチ / 観測点ログ呼出 / 監査ログ呼出）を
網羅抽出できていないため、本スクリプトでソースを直接スキャンして補完する。

【意味合い】
スキルの汎用性確保のため、本スクリプトは「対象 HTML / バックエンドの入口ファイル（Python）のパス」を screen_config.json から
取得し、固有名（画面名・案件名・共通部品のクラス名等）は持たない。別プロジェクトでも screen_config / project_config を
書き換えるだけで再利用可能。

【接続情報】
- 入力: target/intermediate/{機能名}.json + target/intermediate/{機能名}_screen_config.json + {機能名}_project_config.json
  - screen_config.project_root（任意）: プロジェクトルート。無ければ _detect_project_root(html_path) で自動検出（main が読む）
  - screen_config.source_files（任意、既定 []）: 画面を構成するソースのパス。非空なら .js/.ts/.tsx の実在分を走査対象にし、HTML の script src は辿らない（2026-10-09、main が _config_loader.get_source_files で読む）
  - project_config.api_wrapper_classes（任意、既定 []）: `new クラス名(` で API を呼ぶ共通部品のクラス名（main が読む）
  - project_config.api_call_functions（任意、既定 []）: `call('<action>', ...)` の形で API を呼ぶ関数名。第1引数の文字列リテラルを action として拾う（main が読む、2026-10-09）
- 出力: 同 JSON を上書き
  - backend_processes[].called_by_front_processes[]: { js_file, action, line_no } の参照配列
  - backend_processes[].api_callsite_count: フロントでの呼出箇所数
  - front_processes[].api_calls[]: { method, path, action, target_backend_id }（後段で steps.backend_call と紐付け）
- 仕様根拠: SKILL.md Phase 1 改訂（v41）「呼出グラフ・観測点ログ・監査ログを必ず抽出」

【設計原則】
- 汎用: HTML/JS/バックエンドの入口ファイルのパスは screen_config.json source_html_path + meta.backend_files から動的取得
- 冪等: 既に補完済の called_by_front_processes は上書き
- 正規表現ベース（AST より単純、JS/Python 両対応）
- project_config が空ならログ呼出抽出をスキップ（スキル汎用性確保）
"""
import json
import os
import re
import sys
from pathlib import Path
from collections import defaultdict

# 汎用化: sys.argv[1] / FEATURE_NAME 環境変数対応のため get_feature_name_from_argv 経由化
# 2026-10-05 汎用化: INTERMEDIATE_DIR は _config_loader の定義に寄せた（同値の自前定義を廃止、置き場の二重管理を避ける）。
#   load_screen_config / load_project_config は main が project_root / api_wrapper_classes を読むために使う
#   get_backend_entry_patterns は main が入口ファイルの判定語（project_config.backend_entry_patterns）を読むために使う
from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    get_source_html_path,
    get_source_files,
    get_logging_spec,
    get_audit_logging_spec,
    get_backend_entry_patterns,
    load_screen_config,
    load_project_config,
)

# プロジェクトルート（HTML 内の /common/js/xxx.js → 実パス解決用）
# 意味合い: source_html_path の親ディレクトリ階層から推定。汎用化のため複数の親候補を試す
def _detect_project_root(html_path: Path) -> Path:
    """source_html_path から「/common/js/xxx.js」の '/' が指すルートディレクトリを推定する。

    意味合い: HTML 内の絶対パス参照（/で始まるパス）は通常プロジェクトのルートからの相対。
              HTML ファイルの親ディレクトリを遡って common/ ディレクトリを持つ階層を探す。
    """
    p = html_path.parent
    while p != p.parent:
        if (p / "common").exists():
            return p
        p = p.parent
    # フォールバック: HTML の親
    return html_path.parent


def _resolve_script_path(src: str, html_path: Path, project_root: Path) -> Path:
    """HTML 内の script src を実ファイルパスに解決"""
    src_clean = src.split("?")[0].split("#")[0]
    if src_clean.startswith(("http://", "https://")):
        return None
    if src_clean.startswith("/"):
        return project_root / src_clean.lstrip("/")
    return (html_path.parent / src_clean).resolve()


def find_frontend_js_files(html_path: Path, project_root: Path) -> list:
    """HTML から script src を抽出して実 JS ファイルパス一覧を返す。

    意味合い: HTML の <script src="..."> に列挙された全 JS（業務 JS + 共通モジュール JS）を抽出。
              共通モジュール（common.js 等）には fetch 直叩きや共通部品クラス
              （project_config.api_wrapper_classes）経由の API 呼出が含まれるため、v48 で 2 段階解析の対象に含める。
    接続情報: project_root は main が決めて渡す（screen_config.project_root、無ければ _detect_project_root）。
              `/` 始まりの script src を _resolve_script_path がこのルートから解決する。
              2026-10-05: 自前の自動検出をやめた（設定した project_root が script src の解決に効かず、
              バックエンド側・相対化とルートが2系統になっていたため）。
    """
    if not html_path.exists():
        print(f"[WARN] HTML が見つかりません: {html_path}", file=sys.stderr)
        return []
    text = html_path.read_text(encoding="utf-8", errors="ignore")
    js_files = []
    for m in re.finditer(r'<script\s+[^>]*?src=["\']([^"\']+)["\']', text):
        cand = _resolve_script_path(m.group(1), html_path, project_root)
        if cand and cand.exists() and cand.suffix == ".js":
            js_files.append(cand)
    return js_files


def extract_action_references(js_path: Path, wrapper_classes: list) -> list:
    """JS ファイルから action: 'xxx' の参照を網羅抽出する（v48、2 段階解析の第 2 段階）。

    意味合い: フロント JS の apiCall(...) だけでなく、以下のような『apiCall ラッパーを経由しない』
              呼出経路も backend_processes への呼出として記録する：
              (1) fetch(endpoint, { ... body: JSON.stringify({ action: 'xxx', ... }) }) — 共通モジュール
                  （common.js 等）の関数が apiCall を通さず fetch で直接呼ぶ形
              (2) 共通部品クラス（wrapper_classes）に config として渡される { action: 'xxx' }
                  — new SomeWidget({ types: { item: { action: 'item_search' }}}) 等
              (3) その他 action: 'xxx' を含む全ての文字列リテラル参照

    接続情報: 呼出元 = main() の Stage 2 抽出ループ / 出力 = backend_processes.called_by_front_processes
              （extract_apicalls の結果に含まれないもののみを indirect 経路として追加）
              wrapper_classes = project_config.api_wrapper_classes（main が読んで渡す。既定 []）
    """
    text = js_path.read_text(encoding="utf-8", errors="ignore")
    pattern_action = re.compile(r"action\s*:\s*['\"]([a-z_][a-z0-9_]*)['\"]")
    # 2026-10-05 汎用化: 共通部品のクラス名を直書きせず wrapper_classes から検出パターンを作る。
    # 意味合い: 空配列のときはパターンを作らない（空の alternation は全ての `new (` に一致してしまうため）
    pattern_wrapper = (
        re.compile(r"new\s+(" + "|".join(re.escape(c) for c in wrapper_classes) + r")\s*\(")
        if wrapper_classes else None
    )
    results = []
    for m in pattern_action.finditer(text):
        line_no = text[:m.start()].count("\n") + 1
        # 呼出元関数名を周辺コードから推定（直前 1500 文字内、最後の関数定義）
        head = text[max(0, m.start() - 1500):m.start()]
        # メソッド定義（`async functionName(...) {`）や `function name()`、`const name = function`、
        # `name: function` 等のパターンを広めにキャプチャ
        candidates = list(re.finditer(
            r"(?:^|\n)\s*(?:async\s+)?(?:function\s+|(?:const|let|var)\s+)?([a-zA-Z_$][a-zA-Z0-9_$]*)\s*[(:]",
            head
        ))
        caller = candidates[-1].group(1) if candidates else ""

        # 呼出コンテキスト判定（fetch / 共通部品クラス / その他のラッパー）
        # 直前 500 文字を見て「fetch(」「new 共通部品クラス名(」等のキーワードがあれば context として記録
        ctx_head = text[max(0, m.start() - 500):m.start()]
        wrapper_match = pattern_wrapper.search(ctx_head) if pattern_wrapper else None
        context = ""
        if re.search(r"\bfetch\s*\(", ctx_head):
            context = "fetch_direct"
        elif wrapper_match:
            # 一致したクラス名をそのまま context に入れる（called_by_front_processes[].context に出る値）
            context = wrapper_match.group(1)
        elif re.search(r"\bapiCall\s*\(", ctx_head):
            context = "apiCall"  # 既存 extract_apicalls で拾っているはず、重複除外用
        else:
            context = "other"

        results.append({
            "action": m.group(1),
            "js_file": str(js_path),
            "line_no": line_no,
            "caller_hint": caller,
            "context": context
        })
    return results


def find_backend_entry_files(meta: dict, project_root: Path, entry_patterns: list) -> list:
    """meta.backend_files からバックエンドの入口ファイル（Python）の実ファイルパス一覧を返す。

    意味合い: backend_files の path はプロジェクトルートからの相対（例: xxx-handler/handler.py）。
              project_root を経由して実パスに変換。
              2026-10-05 汎用化: 入口かどうかは、パスに entry_patterns のいずれかを含むか（部分一致）で決める。
              特定の実行基盤のファイル名を直書きしていた判定を設定へ出した。
              入口ファイルだけを残すのは、action ディスパッチを持つのが入口だけで、
              共通モジュールを extract_backend_actions に掛けても意味が無いため。
    接続情報: project_root は main が決めて渡す（screen_config.project_root、無ければ _detect_project_root）。
              entry_patterns は main が _config_loader.get_backend_entry_patterns で読んで渡す
              （project_config.backend_entry_patterns、既定 ["handler"]）。
              戻り値は main が extract_backend_actions と logging / audit の呼出箇所の走査に使う。
    """
    result = []
    for bf in (meta.get("backend_files") or []):
        if isinstance(bf, dict):
            path_str = bf.get("path", "")
        else:
            path_str = str(bf)
        if not path_str or not path_str.endswith(".py"):
            continue
        if not any(p in path_str for p in entry_patterns):
            continue  # 共通モジュールは除外、バックエンドの入口ファイルのみ
        cand = project_root / path_str
        if cand.exists():
            result.append(cand)
    return result


def extract_apicalls(js_path: Path) -> list:
    """JS ファイルから apiCall(...) 呼出を抽出する。

    意味合い: 複数の引数順序パターンに汎用対応。想定する代表形は
              `Xxx.apiCall(ENDPOINT_VAR_OR_STR, 'METHOD', { action: 'xxx', ... })` 形式。
              第1引数（endpoint）は変数名 or 文字列、第2引数は HTTP method、第3引数 body 内に action がある。
              別プロジェクトでは引数順序が異なる可能性があるため、apiCall を含む関数呼出を広めに
              取って action: 'xxx' を後方探索する方式（順序非依存）にする。
    """
    text = js_path.read_text(encoding="utf-8", errors="ignore")
    # 汎用パターン: apiCall を含む関数呼出全般（Xxx.apiCall / apiCall / fetchJson 等は別途調整）
    pattern_call = re.compile(r"(?:[a-zA-Z_][a-zA-Z0-9_]*\.)?apiCall\s*\(")
    pattern_action = re.compile(r"action\s*:\s*['\"]([a-z_][a-z0-9_]*)['\"]")
    pattern_method = re.compile(r"['\"]([A-Z]+)['\"]")  # GET/POST/PUT/DELETE 等
    pattern_path = re.compile(r"['\"](/[^'\"]+)['\"]")  # /api/xxx
    # 目的: apiCall の「定義」と「代入」を呼出として数えない（除外だけ。pattern_call 自体は広げない）。
    # 意味合い: `async function apiCall(method, path, body) {` のような定義行は action を持たず、
    #           main() の「Stage 1 (apiCall 直接): n/N 件マッチ」の分母 N だけを 1 増やしてしまう。
    #           一致位置と同じ行の手前が `function`（async function を含む）で終わる場合と、
    #           同じ行の手前に `apiCall =` の代入がある場合を除く。
    #           クラスのメソッド定義（`async apiCall(url, ...) {`）は呼出と字面で区別できないため除外しない。
    # 接続情報: 呼出元 = main() の apiCall 抽出ループ（all_apicalls）。除外した行は Stage 1 の
    #           apicall_sites にも入らないので、Stage 2 の ±15 行の重複判定の基準からも外れる。
    pattern_def_head = re.compile(r"\bfunction\s*$|\bapiCall\s*=(?![=>])")
    results = []
    for m in pattern_call.finditer(text):
        line_head = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        if pattern_def_head.search(line_head):
            continue
        # 呼出全体の引数範囲（次の閉じカッコまで、対応カッコ深さ管理）
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
            if i - start > 1500:  # 暴走防止
                break
        body = text[start:i - 1] if depth == 0 else text[start:start + 800]
        # action 抽出
        action_match = pattern_action.search(body)
        action = action_match.group(1) if action_match else ""
        # method 抽出（最初の HTTP method 文字列）
        method_match = pattern_method.search(body)
        method = method_match.group(1) if method_match else ""
        # path 抽出（最初の /xxx 形式の文字列、変数の場合は空）
        path_match = pattern_path.search(body)
        path = path_match.group(1) if path_match else ""
        line_no = text[:m.start()].count("\n") + 1
        # 呼出元関数名を周辺コードから推定（直前 800 文字内の関数定義/メソッド）
        head = text[max(0, m.start() - 800):m.start()]
        candidates = list(re.finditer(r"(?:^|\n)\s*(?:async\s+)?(?:function\s+|(?:const|let|var)\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s*[(:]", head))
        caller = candidates[-1].group(1) if candidates else ""
        results.append({
            "method": method,
            "path": path,
            "action": action,
            "js_file": str(js_path),
            "line_no": line_no,
            "caller_hint": caller
        })
    return results


def extract_wrapper_function_calls(js_path: Path, func_names: list) -> list:
    """関数ラッパー形式の API 呼出 `call('<action>', ...)` を抽出する（2026-10-09 追加）。

    目的: React(TSX) 等で API 呼出を `call<T>('list_sales_report', {...})` のような関数で包む構成から、
          第1引数の文字列リテラルを action として拾う。func_names は project_config.api_call_functions。
    意味合い: extract_apicalls は apiCall(...) 固定で body 内の `action:` を探す方式のため、第1引数に
              action を置く関数ラッパーを拾えない（marionet-pilot の api.ts で 0 件だった）。
              action は静的に決まるもの（第1引数が引用符の文字列リテラルで、直後が `,` か `)`）だけを採る。
              テンプレートリテラル・連結（`'x' + y`）・三項・変数など静的に決まらないものは捨てる。
              関数名は単語境界つきで探し、`xxx.call(` のメソッド呼出や `recall(` の部分一致は拾わない。
              関数名の直後の型引数 `<...>` は `<` `>` の深さで読み飛ばす（複数行可。`=>` の `>` は数えない）。
              関数の定義行（直前が `function`、または `const 名 =` 等の代入）は呼出として数えない。
              await / return / `const x =` が前に付く呼出はそのまま拾う。
    接続情報: 戻りは extract_apicalls と同形の dict（method・path は空文字）。少なくとも main() の
              all_apicalls 抽出ループが、api_call_functions が非空のときに呼び、all_apicalls へ合流させる
              （source:"apiCall" の付与と apicall_sites への登録は main 側の既存処理に任せる）。
    """
    text = js_path.read_text(encoding="utf-8", errors="ignore")
    pattern_action = re.compile(r"[a-z_][a-z0-9_]*")
    pattern_def_head = re.compile(r"\bfunction\s*$|\b(?:const|let|var)\s+$")
    results = []
    for name in func_names:
        # 直前が識別子文字・`.`・`$` でない位置の name（メソッド呼出・部分一致を除く）
        pattern_call = re.compile(r"(?<![\w.$])" + re.escape(name) + r"\b")
        for m in pattern_call.finditer(text):
            line_head = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
            if pattern_def_head.search(line_head):
                continue
            i = m.end()
            while i < len(text) and text[i].isspace():
                i += 1
            # 型引数 `<...>` の読み飛ばし（`=>` の `>` は深さに数えない。暴走防止に 2000 文字で打ち切る）
            if i < len(text) and text[i] == "<":
                depth = 0
                start = i
                while i < len(text) and i - start <= 2000:
                    ch = text[i]
                    if ch == "<":
                        depth += 1
                    elif ch == ">" and text[i - 1] != "=":
                        depth -= 1
                        if depth == 0:
                            break
                    i += 1
                if depth != 0:
                    continue
                i += 1
                while i < len(text) and text[i].isspace():
                    i += 1
            if i >= len(text) or text[i] != "(":
                continue
            i += 1
            while i < len(text) and text[i].isspace():
                i += 1
            # 第1引数が引用符の文字列リテラルで始まるときだけ（バッククォート・識別子始まりは捨てる）
            if i >= len(text) or text[i] not in "'\"":
                continue
            end = text.find(text[i], i + 1)
            if end < 0:
                continue
            literal = text[i + 1:end]
            if not pattern_action.fullmatch(literal):
                continue
            j = end + 1
            while j < len(text) and text[j].isspace():
                j += 1
            # 直後が `,` か `)` でなければ連結・三項などの式の一部なので捨てる
            if j >= len(text) or text[j] not in ",)":
                continue
            line_no = text[:m.start()].count("\n") + 1
            # 呼出元関数名の推定は extract_apicalls と同じ（直前 800 文字内の関数定義/メソッド）
            head = text[max(0, m.start() - 800):m.start()]
            candidates = list(re.finditer(r"(?:^|\n)\s*(?:async\s+)?(?:function\s+|(?:const|let|var)\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s*[(:]", head))
            caller = candidates[-1].group(1) if candidates else ""
            results.append({
                "method": "",
                "path": "",
                "action": literal,
                "js_file": str(js_path),
                "line_no": line_no,
                "caller_hint": caller
            })
    return results


def extract_backend_actions(py_path: Path) -> list:
    """バックエンドの入口ファイル（Python）から action ディスパッチを抽出する。

    意味合い: if action == 'xxx': / elif action == 'xxx': / 辞書ディスパッチ {'xxx': handler} を網羅抽出。
              汎用化のため複数のパターンに対応。
    """
    text = py_path.read_text(encoding="utf-8", errors="ignore")
    results = []
    # パターン1: if/elif action == 'xxx':
    pattern1 = re.compile(
        r"(?:if|elif)\s+(?:body\.get\(['\"]action['\"][\s,)]|body\[['\"]action['\"]\]|action)\s*==\s*['\"]([^'\"]+)['\"]"
    )
    for m in pattern1.finditer(text):
        line_no = text[:m.start()].count("\n") + 1
        results.append({"action": m.group(1), "py_file": str(py_path), "line_no": line_no, "pattern": "if-elif"})
    # パターン2: 辞書ディスパッチ {'xxx': handler_func} / 'xxx': handle_xxx
    pattern2 = re.compile(r"['\"]([a-z_][a-z0-9_]*)['\"]\s*:\s*(?:handle_|process_|action_)[a-z_]+", re.IGNORECASE)
    for m in pattern2.finditer(text):
        line_no = text[:m.start()].count("\n") + 1
        # 既存と重複しないものだけ追加
        if not any(r["action"] == m.group(1) and r["py_file"] == str(py_path) for r in results):
            results.append({"action": m.group(1), "py_file": str(py_path), "line_no": line_no, "pattern": "dict-dispatch"})
    return results


def extract_logging_calls(file_path: Path, trigger_functions: list) -> list:
    """ファイル内で指定された trigger_functions の呼出箇所を抽出。

    意味合い: project_config.logging.observation_points[].trigger_function の各関数名を
              ソースファイル全体で grep し、呼出箇所（line_no + 関数名）を返す。
              フロント JS / バック Python 両対応。
    """
    if not file_path.exists() or not trigger_functions:
        return []
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    results = []
    for fn in trigger_functions:
        if not fn:
            continue
        # 関数名のドット表記（例: Xxx.recordBusinessAction）にも対応
        fn_escaped = re.escape(fn)
        pattern = re.compile(rf"\b{fn_escaped}\s*\(")
        for m in pattern.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            results.append({"function": fn, "file": str(file_path), "line_no": line_no})
    return results


def main():
    # 汎用化: sys.argv[1] / FEATURE_NAME 環境変数のいずれかで決定（どちらも無ければ _config_loader が SystemExit）
    # 接続情報: 機能名のハードコードは持たない。以降のアクセサ呼出には feature_name を明示で渡す
    feature_name = get_feature_name_from_argv()
    intermediate_json = INTERMEDIATE_DIR / f"{feature_name}.json"
    print(f"[INFO] target feature: {feature_name}")
    print(f"[INFO] intermediate JSON: {intermediate_json}")

    if not intermediate_json.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {intermediate_json}", file=sys.stderr)
        sys.exit(1)

    with intermediate_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. 対象ファイル一覧
    # 汎用化: feature_name を明示伝搬
    html_path = get_source_html_path(feature_name)
    # 2026-10-05 汎用化: プロジェクトルートはここで一度だけ決める（絶対パスの直書きを廃止）。
    # 意味合い: screen_config.project_root があればそれ、無ければ HTML の位置から自動検出。
    # 接続情報: find_frontend_js_files（`/` 始まりの script src の実パス解決）、
    #          find_backend_entry_files（backend_files の実パス解決）、下の js_file / file の
    #          相対化（root_prefix の除去）が同じ値を使う
    configured_root = load_screen_config(feature_name).get("project_root")
    # 2026-10-05 汎用化: 設定値の環境変数と `~` を展開する（expandvars → expanduser の順）。
    # 意味合い: 配布物の設定例に絶対パスを書けないため、`${HOME}/...` の形で書けるようにする。
    #          値が空（未設定・空文字）のときは展開せず、従来どおり自動検出へ落ちる。
    # 接続情報: _config_loader.get_source_html_path が source_html_path に掛ける展開と同じ取り決め
    if configured_root:
        configured_root = os.path.expanduser(os.path.expandvars(configured_root))
    project_root = Path(configured_root) if configured_root else _detect_project_root(html_path)
    root_prefix = f"{project_root}/"
    # 接続情報: project_config.api_wrapper_classes（既定 []）→ extract_action_references の context 判定
    wrapper_classes = load_project_config(feature_name).get("api_wrapper_classes", [])
    # 2026-10-09 関数ラッパー対応: project_config.api_call_functions（既定 []）を読む。
    # 目的: `call('<action>', ...)` のような関数呼出形の API 呼出を Stage 1 で拾う関数名を受ける。
    # 意味合い: React 等で API を関数ラッパーで包む構成に対応する。キーが無い・空なら抽出関数を呼ばず、
    #          all_apicalls・件数ログ・Stage 1 の分母・apicall_sites は従来と同じになる。
    # 接続情報: 下の「2. フロント apiCall 抽出」ループで extract_wrapper_function_calls に渡す。
    #          extract_action_references の context 判定（wrapper_classes）には渡さず、影響しない
    call_functions = load_project_config(feature_name).get("api_call_functions", [])
    # 2026-10-09 React(TSX) 対応: js_files の出所を screen_config.source_files で切り替える。
    # 目的: source_files が非空なら、その中の .js/.ts/.tsx で実在するものを設定の並び順で走査対象にする。
    # 意味合い: find_frontend_js_files は HTML の script src の .js しか辿らず、React の index.html からは
    #          main.tsx しか出ない（画面本体の tsx と api.ts が走査されない）。source_files 経路では HTML を
    #          読まないので source_html_path は空でよい。キーが無ければ従来経路で、出力は変わらない。
    #          meta.frontend_files は読まない（extract_source_files.py の実行順に依存しないため）。
    # 接続情報: _config_loader.get_source_files（展開済み Path のリスト、キー無しは []）。
    #          少なくとも下の Stage 1 / Stage 2 / ログ呼出の走査が js_files に依存する
    source_files = get_source_files(feature_name)
    if source_files:
        js_files = []
        for sf in source_files:
            if sf.suffix not in (".js", ".ts", ".tsx"):
                continue
            if not sf.exists():
                print(f"[WARN] source_files のパスが存在しません: {sf}", file=sys.stderr)
                continue
            js_files.append(sf)
    else:
        js_files = find_frontend_js_files(html_path, project_root)
    # 2026-10-05 汎用化: 入口ファイルの判定語を設定から読む（特定の実行基盤のファイル名の直書きを廃止）。
    # 目的: project_config.backend_entry_patterns（既定 ["handler"]）を読み、入口ファイルの探索へ渡す。
    # 意味合い: 入口ファイルの名前は案件の実行基盤で決まるため、スクリプトに持たず設定で受ける。
    # 接続情報: _config_loader.get_backend_entry_patterns → find_backend_entry_files の entry_patterns。
    #          少なくとも extract_source_files.py が同じアクセサで meta.backend_files[].kind を決める
    entry_patterns = get_backend_entry_patterns(feature_name)
    py_files = find_backend_entry_files(data.get("meta", {}), project_root, entry_patterns)
    # 2026-10-09: source_files 経路では HTML を読まないので、HTML 件数の代わりに source_files 件数を出す
    # （従来経路の文言は変えない。接続情報: 上の js_files の出所切替と対）
    if source_files:
        print(f"[INFO] 解析対象: source_files {len(source_files)} 件、フロント JS {len(js_files)} 件、バックエンド入口 {len(py_files)} 件")
    else:
        print(f"[INFO] 解析対象: HTML 1 件、フロント JS {len(js_files)} 件、バックエンド入口 {len(py_files)} 件")

    # 2. フロント apiCall 抽出
    all_apicalls = []
    for js in js_files:
        all_apicalls.extend(extract_apicalls(js))
        # 2026-10-09 関数ラッパー対応: api_call_functions が非空のときだけ関数呼出形を合流させる。
        # 目的: `call('<action>', ...)` の呼出を apiCall と同じ all_apicalls に入れる。
        # 意味合い: 合流した呼出は Stage 1 と同じ扱い（source "apiCall"）で apicall_sites に入り、
        #          Stage 2 の ±15 行の重複除外の基準にもなる。同名を apiCall 側と両方に設定しない前提。
        # 接続情報: 呼出先 = extract_wrapper_function_calls / 出力 = backend_processes の
        #          called_by_front_processes と api_callsite_count
        if call_functions:
            all_apicalls.extend(extract_wrapper_function_calls(js, call_functions))
    print(f"[INFO] apiCall 呼出箇所: {len(all_apicalls)} 件")

    # 3. バック action ディスパッチ抽出
    all_backend_actions = []
    for py in py_files:
        all_backend_actions.extend(extract_backend_actions(py))
    print(f"[INFO] バック action ディスパッチ: {len(all_backend_actions)} 件")

    # 4. backend_processes の action → backend_id マップ
    action_to_backend_id = {}
    for bp in (data.get("backend_processes") or []):
        ep = bp.get("endpoint") or {}
        action = ep.get("action")
        if action:
            action_to_backend_id[action] = bp["process_id"]

    # 5. backend_processes に called_by_front_processes を補完
    # v48: source 列を導入し、直接 apiCall 経路 ("apiCall") と間接経路 ("indirect_action") を区別
    backend_callers = defaultdict(list)  # backend_id → [{ js_file, action, line_no, source }]
    matched_apicalls = 0
    apicall_sites = set()  # (js_file_str, line_no) — Stage 2 で重複除外する用
    for ac in all_apicalls:
        bid = action_to_backend_id.get(ac["action"])
        # apiCall 経路の line_no を記録（apiCall の呼出開始行）。Stage 2 では action の出現行で照合するため
        # line_no が ±15 程度ずれる可能性に備え、(file, line_no) を ±15 範囲で重複判定するため別途集合に保存
        apicall_sites.add((ac["js_file"], ac["line_no"]))
        if bid:
            backend_callers[bid].append({
                "js_file": ac["js_file"].replace(root_prefix, ""),
                "method": ac["method"],
                "path": ac["path"],
                "action": ac["action"],
                "line_no": ac["line_no"],
                "caller_hint": ac["caller_hint"],
                "source": "apiCall"
            })
            matched_apicalls += 1

    # 5.5. Stage 2 — 共通モジュール経由 / fetch 直叩き / 共通部品クラスの config 等の indirect 呼出を抽出
    # 意味合い: 共通モジュールの fetch 直叩きで呼ばれる action や、共通部品クラスの config に書かれた
    #          action は apiCall を通らないため、Stage 1 で漏れていた。その問題の本質対応。
    # 接続情報: extract_action_references で全 JS の `action: 'xxx'` を網羅抽出し、Stage 1 で拾った
    #          (file, line_no) ペアに ±15 行以内で対応する重複は除外、残りを indirect 経路として追加
    all_action_refs = []
    for js in js_files:
        all_action_refs.extend(extract_action_references(js, wrapper_classes))
    matched_indirect = 0
    skipped_dup = 0
    for ref in all_action_refs:
        # 重複除外: Stage 1 で同ファイル内 ±15 行以内に apiCall 呼出があれば、これは apiCall 経路の
        # 一部（apiCall(endpoint, 'POST', { action: 'xxx', ... }) 内の action）なのでスキップ
        # 距離 15 行は extract_apicalls が呼出本体を 1500 文字以内で見ている設計と整合
        is_dup = any(
            (ac_file == ref["js_file"]) and abs(ac_line - ref["line_no"]) <= 15
            for (ac_file, ac_line) in apicall_sites
        )
        if is_dup:
            skipped_dup += 1
            continue
        bid = action_to_backend_id.get(ref["action"])
        if not bid:
            continue
        backend_callers[bid].append({
            "js_file": ref["js_file"].replace(root_prefix, ""),
            "method": "",
            "path": "",
            "action": ref["action"],
            "line_no": ref["line_no"],
            "caller_hint": ref["caller_hint"],
            "source": "indirect_action",
            "context": ref["context"]  # fetch_direct / api_wrapper_classes のクラス名 / other 等
        })
        matched_indirect += 1

    # 反映
    for bp in (data.get("backend_processes") or []):
        bid = bp["process_id"]
        callers = backend_callers.get(bid, [])
        bp["called_by_front_processes"] = callers
        bp["api_callsite_count"] = len(callers)
    print(f"[OK] backend_processes.called_by_front_processes 補完:")
    print(f"  Stage 1 (apiCall 直接):  {matched_apicalls}/{len(all_apicalls)} 件マッチ")
    print(f"  Stage 2 (indirect 経路): {matched_indirect} 件追加（重複 {skipped_dup} 件除外、全 action 参照 {len(all_action_refs)} 件中）")

    # 6. 観測点ログ・監査ログ呼出を抽出して logging_callsites を補完（プロジェクト固有、project_config 経由）
    # 汎用化: feature_name を明示伝搬
    logging_spec = get_logging_spec(feature_name)
    obs_points = logging_spec.get("observation_points", []) or []
    log_trigger_fns = []
    for op in obs_points:
        if op.get("trigger_function"):
            log_trigger_fns.append(op["trigger_function"])
        for sub in (op.get("sub_observations", []) or []):
            if sub.get("trigger_function"):
                log_trigger_fns.append(sub["trigger_function"])
    # 重複除去 + 関数名だけ取り出す（"recordApiResponse / recordApiError" のような複合は分割）
    expanded_fns = []
    for fn in log_trigger_fns:
        # 空白 / / , + 等で分割（複合文字列を関数名候補に分解）
        for part in re.split(r"[\s/,+]+", fn):
            part = part.strip().strip("（）()")
            if part:
                expanded_fns.append(part)
    # v49: 純粋な関数名（識別子）のみ通すフィルタを追加
    # 意味合い: project_config.json の trigger_function に「recordApiRequest + apiCall」「xxx-logger Function」
    #          「（個別 Handler 内）」等の複合文字列・説明語が混入していると、分割後に「+」「Function」「内）」等の
    #          ゴミトークンが expanded_fns に残り、verify_call_graph.py で「呼出箇所 0 件」WARN を多発させていた。
    #          関数名は ASCII 識別子（英数字 / _ / .）のみ許容 + 名詞語ブラックリストで除外する。
    # 接続情報: project_config.json の logging.observation_points[].trigger_function（推移先: extract_logging_calls の検出対象）
    IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
    # 2026-10-05 汎用化: 無視語から特定の実行基盤の製品名を外し、案件ごとの語は設定で足す形にした。
    # 目的: 基本語（どの案件でも関数名にならない一般名詞）に、project_config.verify_overrides.noise_words（既定 []）を足す。
    # 意味合い: trigger_function の説明語に製品名などを書く案件では、その語が識別子として通り
    #          「呼出箇所 0 件」WARN の原因になる。語は案件によって違うので、スクリプトに直書きせず設定で受ける。
    # 接続情報: 読む値 = project_config.verify_overrides.noise_words。
    #          少なくとも verify_call_graph.py の verify_logging_callsites が同じ集合を作る（食い違うと WARN が出る）。
    NOISE_WORDS = {"Function", "Module", "Handler", "Method", "Class", "Object"} | set(
        (load_project_config(feature_name).get("verify_overrides") or {}).get("noise_words", [])
    )
    expanded_fns = [fn for fn in expanded_fns if IDENTIFIER_PATTERN.match(fn) and fn not in NOISE_WORDS]
    expanded_fns = list(dict.fromkeys(expanded_fns))

    all_logging_calls = []
    for f in (js_files + py_files):
        all_logging_calls.extend(extract_logging_calls(f, expanded_fns))
    # logging_callsites を中間 JSON に格納（後段の検証が突合に使用）
    data["logging_callsites"] = [
        {
            "function": lc["function"],
            "file": lc["file"].replace(root_prefix, ""),
            "line_no": lc["line_no"]
        } for lc in all_logging_calls
    ]
    print(f"[OK] 観測点ログ呼出: {len(all_logging_calls)} 箇所抽出（対象関数: {len(expanded_fns)} 種）")

    # 7. 監査ログ呼出を抽出
    # 汎用化: feature_name を明示伝搬
    audit_spec = get_audit_logging_spec(feature_name)
    audit_fn = audit_spec.get("trigger_function", "")
    audit_calls = []
    if audit_fn:
        for f in py_files:
            audit_calls.extend(extract_logging_calls(f, [audit_fn]))
    data["audit_callsites"] = [
        {
            "function": ac["function"],
            "file": ac["file"].replace(root_prefix, ""),
            "line_no": ac["line_no"]
        } for ac in audit_calls
    ]
    print(f"[OK] 監査ログ呼出: {len(audit_calls)} 箇所抽出（対象関数: {audit_fn}）")

    with intermediate_json.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[OK] 保存先: {intermediate_json}")


if __name__ == "__main__":
    main()
