#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_source_files.py — HTML を解析して frontend_files[] を自動抽出し、meta を v21 形式に更新する（v30 で外部設定化）

【目的】
画面を構成する全フロントエンドファイル（HTML本体 + CSS + JS + 外部CDN）を網羅した一覧を
meta.frontend_files[] に生成する。あわせて backend_files[] を string[] から object[] に拡張する。
meta.source_html_path（個人ローカル絶対パス）は削除する。

【意味合い】
v20 まで meta は「機能名 + ソースHTML + HTML絶対パス + バックエンドファイル列挙」のみだった。
レビュー指摘「HTMLパスは人によって異なるので不要、CSSも含めて構成しているファイルを網羅した一覧が必要」
への対応で v21 では meta を「構成ファイル網羅型」に拡張する。
v30 でハードコード辞書（SOURCE_HTML / 共通モジュール一覧 / _describe_css・_describe_js 辞書）を
screen_config.json に外出しし、本スクリプトを画面非依存（汎用化）にした。

【接続情報】
- 機能名: 第1引数か環境変数 FEATURE_NAME（_config_loader.get_feature_name_from_argv。未指定なら SystemExit）
- 入力: target/intermediate/{機能名}.json + target/intermediate/{機能名}_screen_config.json + 解析対象HTMLファイル
        + target/intermediate/{機能名}_project_config.json（任意。shared_module_patterns と backend_entry_patterns を upgrade_backend_files が読む）
        + screen_config の source_files（任意。画面を構成するソースのパスの配列。_config_loader.get_source_files で ${HOME}/~ 展開。
          あれば HTML 抽出結果の後ろに重複なく追記し、source_html_path が空なら HTML 解析を飛ばして source_files だけで作る）
- 出力: 同 JSON を上書き
- 仕様根拠: 15_中間JSONスキーマ.md「meta」セクション（v21 拡張）、SKILL.md「Phase 3-4 外部設定ファイル化」（v30）
"""
import json
import os
import re
import sys
from pathlib import Path

# v30: ハードコード辞書を screen_config.json に集約、_config_loader 経由で取得する
from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    load_project_config,
    get_source_html_path,
    get_shared_modules,
    get_file_descriptions,
    get_backend_entry_patterns,
    # 2026-10-09 source_files 対応: 目的/意味合い/接続情報は _source_file_entries と main のコメントを参照
    load_screen_config,
    get_source_files,
)

# 2026-10-05 汎用化: 既定の機能名（_config_loader の旧 DEFAULT 定数）を廃止。
# 目的: 機能名を第1引数か環境変数 FEATURE_NAME からモジュール先頭で1回だけ解決する（未指定なら SystemExit）。
# 意味合い: _config_loader のアクセサは引数なしだと sys.argv を見ないため、
#           本ファイルのアクセサ呼出すべてに FEATURE_NAME を明示で渡す（argv 指定だけでも動かすため）。
# 接続情報: _describe_css / _describe_js / _build_shared_module_desc_map / upgrade_backend_files / main が参照する。
FEATURE_NAME = get_feature_name_from_argv()
INTERMEDIATE_JSON = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"
# v30: SOURCE_HTML / 共通モジュール一覧のハードコードを廃止し、screen_config.json 経由で取得


def classify_file(href_or_src: str) -> tuple:
    """ファイルパスから (kind, description) を判定する。

    判定ルール:
    - http(s):// で始まる → external_cdn
    - .css → css
    - .js / type=module + .js → js
    - .html → html
    - .png/.jpg/.gif/.svg → image
    """
    if href_or_src.startswith(("http://", "https://")):
        return ("external_cdn", _describe_cdn(href_or_src))
    # クエリストリング（?v=...）と fragment（#...）を除去して拡張子判定
    path_only = href_or_src.split("?", 1)[0].split("#", 1)[0]
    lower = path_only.lower()
    if lower.endswith(".css"):
        return ("css", _describe_css(href_or_src))
    # 2026-10-09 React(TSX)対応:
    # 目的: .ts / .tsx を kind=js と判定する。
    # 意味合い: kind 列挙(html/css/js/external_cdn/image)の外の 'other' を出さないため。
    #           TypeScript は JS 系のソースとして扱い、description は _describe_js で引く。
    # 接続情報: 少なくとも extract_frontend_files(React の index.html が script src で
    #           参照する main.tsx)と、main の source_files 経由の frontend_files 構築が依存する。
    if lower.endswith((".js", ".ts", ".tsx")) or "/js/" in lower:
        return ("js", _describe_js(href_or_src))
    if lower.endswith(".html"):
        return ("html", "HTMLファイル")
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg")):
        return ("image", "画像ファイル")
    return ("other", "")


def _describe_cdn(url: str) -> str:
    """CDN URL から短い説明を生成"""
    if "tailwindcss" in url:
        return "TailwindCSS（CDN）"
    if "decimal.js" in url:
        return "decimal.js 任意精度演算ライブラリ（CDN）"
    if "accounts.google.com" in url:
        return "Google Identity Services（CDN）"
    if "jsdelivr" in url:
        return "jsDelivr CDN ライブラリ"
    return "外部CDN"


def _describe_css(path: str) -> str:
    """CSS パスから短い説明を生成（v30: screen_config.json から取得）

    意味合い: ファイル名は画面共通だが、画面固有 CSS（例: {画面名}-input.css）の説明は画面別 JSON に書く。
    fallback は「スタイルシート」（不明 CSS でも壊さない）。
    """
    name = path.rsplit("/", 1)[-1].split("?", 1)[0]
    descriptions = get_file_descriptions(FEATURE_NAME).get("css", {})
    return descriptions.get(name, "スタイルシート")


def _describe_js(path: str) -> str:
    """JS パスから短い説明を生成（v30: screen_config.json から取得）

    意味合い: 画面固有 JS（例: {画面名}-detail.js）の説明は画面別 JSON に書く。
    fallback は「スクリプト」。
    """
    name = path.rsplit("/", 1)[-1].split("?", 1)[0]
    descriptions = get_file_descriptions(FEATURE_NAME).get("js", {})
    return descriptions.get(name, "スクリプト")


def extract_frontend_files(html_path: Path) -> list:
    """HTML を解析して frontend_files[] を生成する。

    抽出対象:
    - <link rel="stylesheet" href="..."> → kind=css
    - <script src="..."> → kind=js or external_cdn
    - HTML 本体は先頭に追加（kind=html）

    抽出順は HTML 内の出現順（ロード順）を保持。バージョンクエリ ?v=... も保持する（cache buster の確認に役立つ）。
    """
    if not html_path.exists():
        print(f"[WARN] HTMLが見つかりません: {html_path}", file=sys.stderr)
        return []

    text = html_path.read_text(encoding="utf-8", errors="ignore")

    files = []
    # HTML 本体（パスはファイル名のみ、絶対パスは出さない）
    files.append({
        "path": html_path.name,
        "kind": "html",
        "description": "メイン画面HTML"
    })

    # <link rel="stylesheet" href="...">
    for m in re.finditer(r'<link\s+[^>]*?rel=["\']stylesheet["\'][^>]*?href=["\']([^"\']+)["\']', text):
        href = m.group(1)
        kind, desc = classify_file(href)
        files.append({"path": href, "kind": kind, "description": desc})

    # <link rel="..." href="..." rel="stylesheet">（逆順属性）
    for m in re.finditer(r'<link\s+[^>]*?href=["\']([^"\']+)["\'][^>]*?rel=["\']stylesheet["\']', text):
        href = m.group(1)
        # 既に追加済みかチェック
        if not any(f["path"] == href for f in files):
            kind, desc = classify_file(href)
            files.append({"path": href, "kind": kind, "description": desc})

    # <script src="...">
    for m in re.finditer(r'<script\s+[^>]*?src=["\']([^"\']+)["\']', text):
        src = m.group(1)
        # 既に追加済みかチェック
        if not any(f["path"] == src for f in files):
            kind, desc = classify_file(src)
            files.append({"path": src, "kind": kind, "description": desc})

    return files


def _source_file_entries(source_files: list, existing: list) -> list:
    """screen_config.source_files（展開済み Path のリスト）を frontend_files の要素に変換する。

    2026-10-09 source_files 対応:
    目的: 各ファイルを {path, kind, description} にする。kind/description は classify_file
          （CSS は _describe_css、JS・TS・TSX は _describe_js が file_descriptions を引く）に任せる。
    意味合い: path に個人の絶対パスを出さないため、screen_config.project_root（展開後）の配下なら
              その相対パス、配下でない・project_root 無しならファイル名だけにする。
              existing と path が一致するものは重複として飛ばし、存在しないファイルは [WARN] を出して飛ばす
              （extract_frontend_files と同じ流儀）。
    接続情報: 少なくとも main が呼ぶ。入力は _config_loader.get_source_files の返り値と screen_config.project_root。
    """
    raw_root = load_screen_config(FEATURE_NAME).get("project_root", "")
    root = Path(os.path.expanduser(os.path.expandvars(raw_root))) if raw_root else None
    seen = {f["path"] for f in existing}
    entries = []
    for p in source_files:
        if not p.exists():
            print(f"[WARN] source_files のファイルが見つかりません: {p}", file=sys.stderr)
            continue
        path = p.relative_to(root).as_posix() if root and p.is_relative_to(root) else p.name
        if path in seen:
            continue
        seen.add(path)
        kind, desc = classify_file(path)
        entries.append({"path": path, "kind": kind, "description": desc})
    return entries


def _build_shared_module_desc_map() -> dict:
    """共通モジュールの「ファイル名 → 説明」マップを screen_config.json から構築する（v30）。

    意味合い: 旧版では upgrade_backend_files / 共通モジュール一覧の定数の2箇所に同じ説明が
              ハードコードされていた。screen_config.shared_modules[].path から basename を抽出して
              辞書化することで、設定の単一情報源化（DRY）を実現する。
    """
    desc_map = {}
    for m in get_shared_modules(FEATURE_NAME):
        name = m["path"].rsplit("/", 1)[-1]
        desc_map[name] = m.get("description", "共通モジュール")
    return desc_map


def upgrade_backend_files(old_backend: list) -> list:
    """backend_files を string[] から object[] に変換する。

    入力例: ["sample-handler/handler.py", "common/python/db_utils.py"]
    出力例: [{"path": "...", "kind": "backend", "description": "..."}, ...]

    v30: 共通モジュールの説明辞書は screen_config.json から動的構築（_build_shared_module_desc_map）。
    """
    # 既に object[] になっている場合はそのまま返す（冪等性）
    if old_backend and isinstance(old_backend[0], dict):
        return old_backend

    descs = _build_shared_module_desc_map()
    # 2026-10-05 汎用化: 共通モジュール判定のパス断片の直書きを廃止し、設定から読む。
    # 目的: パスに shared_module_patterns のいずれかの文字列を含めば共通モジュール（kind=shared_module）とする。
    # 意味合い: 共通モジュールの置き場・名前はプロジェクトごとに違うため。未設定なら既定 ["common/"]。
    # 接続情報: {機能名}_project_config.json の shared_module_patterns を、本関数が読む。
    shared_patterns = load_project_config(FEATURE_NAME).get("shared_module_patterns", ["common/"])
    # 2026-10-05 汎用化: バックエンドの入口ファイルの判定を、特定の実行基盤の入口ファイル名の直書きから設定へ出した。
    # 目的: パスに backend_entry_patterns のいずれかの文字列を含めば、バックエンドの入口ファイル（kind=backend）とする。
    # 意味合い: 入口ファイルの名前はプロジェクトごとに違うため。未設定なら既定 ["handler"]。
    #           判定の順序は、共通モジュールが先、入口が次、どちらでもなければ other（従来どおり）。
    #           以前あった「パスが -handler で終わる」の枝は、既定 ["handler"] の部分一致に含まれるので削除した。
    # 接続情報: {機能名}_project_config.json の backend_entry_patterns を、_config_loader.get_backend_entry_patterns 経由で読む。
    #           付けた kind は meta.backend_files[].kind に入り、少なくとも generate_docx.js が表の種別列にそのまま表示する。
    entry_patterns = get_backend_entry_patterns(FEATURE_NAME)
    result = []
    for path in old_backend or []:
        if not path:
            continue
        if any(p in path for p in shared_patterns):
            kind = "shared_module"
            name = path.rsplit("/", 1)[-1]
            desc = descs.get(name, "共通モジュール")
        elif any(p in path for p in entry_patterns):
            kind = "backend"
            desc = f"バックエンド関数: {path.split('/')[0]}"
        else:
            kind = "other"
            desc = ""
        result.append({"path": path, "kind": kind, "description": desc})

    return result


def main():
    if not INTERMEDIATE_JSON.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {INTERMEDIATE_JSON}", file=sys.stderr)
        sys.exit(1)

    with INTERMEDIATE_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    meta = data.get("meta", {}) or {}

    # 1. frontend_files[] を HTML 解析で生成（v30: source_html_path は screen_config.json 経由）
    # 2026-10-09 source_files 対応:
    # 目的: screen_config.source_files があれば、HTML 抽出結果の後ろへその各ファイルを追記する。
    # 意味合い: React(TSX) の画面は index.html が main.tsx しか参照せず、画面本体の tsx・api.ts・CSS は
    #           source_files からしか取れない。source_html_path が空（Path("") は IsADirectoryError になる）なら
    #           HTML 解析を通さず source_files だけで作る。source_files が [] なら従来と同一の出力。
    # 接続情報: _config_loader.get_source_files / load_screen_config を読み、_source_file_entries で要素化する。
    #           結果は meta.frontend_files に入り、少なくとも generate_docx.js の §1.1 が表に出す。
    source_files = get_source_files(FEATURE_NAME)
    if not source_files:
        source_html = get_source_html_path(FEATURE_NAME)
        frontend_files = extract_frontend_files(source_html)
    else:
        if load_screen_config(FEATURE_NAME).get("source_html_path"):
            frontend_files = extract_frontend_files(get_source_html_path(FEATURE_NAME))
        else:
            frontend_files = []
        frontend_files += _source_file_entries(source_files, frontend_files)

    # 2. backend_files[] を object[] に拡張、共通モジュールを追加
    old_backend = meta.get("backend_files", []) or []
    new_backend = upgrade_backend_files(old_backend)

    # v30: 当該画面のバックエンドが実 import している共通モジュールを screen_config.json から追加（重複回避）
    # 意味合い: 共通モジュール一覧のハードコードを廃止し、画面別 JSON（shared_modules）に外出し
    existing_paths = {b["path"] for b in new_backend}
    for m in get_shared_modules(FEATURE_NAME):
        sm_path = m["path"]
        if sm_path not in existing_paths:
            new_backend.append({
                "path": sm_path,
                "kind": "shared_module",
                "description": m.get("description", "共通モジュール")
            })

    # 3. meta から source_html_path を削除（個人パスのため設計書として不要）
    if "source_html_path" in meta:
        del meta["source_html_path"]

    # 4. 反映
    meta["frontend_files"] = frontend_files
    meta["backend_files"] = new_backend
    data["meta"] = meta

    with INTERMEDIATE_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[OK] frontend_files: {len(frontend_files)} 件")
    for f in frontend_files:
        print(f"  [{f['kind']:12s}] {f['path']:60s} {f['description']}")
    print()
    print(f"[OK] backend_files: {len(new_backend)} 件")
    for f in new_backend:
        print(f"  [{f['kind']:14s}] {f['path']:50s} {f['description']}")
    print()
    print(f"[OK] meta.source_html_path を削除")
    print(f"[OK] 保存先: {INTERMEDIATE_JSON}")


if __name__ == "__main__":
    main()
