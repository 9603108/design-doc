#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_config_loader.py — 画面別ハードコード集約 JSON (screen_config.json) を読み込む共通ヘルパー（v30）

【目的】
design-doc skill v30 で画面別ハードコード7種（areas / source_html / shared_modules / file_descriptions /
area_aliases / process_names / front_area_keywords / trigger_dispatch / diagrams / svg_layout）を
target/intermediate/{feature_name}_screen_config.json に集約した結果、各スクリプトから JSON を
読み込む共通アクセス経路を提供する。

【意味合い】
v29 までは各スクリプト（add_screen_areas / extract_source_files / migrate_v17_to_v18 /
normalize_screen_terminology / apply_trigger_dispatch / add_diagrams_to_intermediate /
build_ipo_flowchart_svg）にハードコード辞書が点在し、新画面追加時は7ファイル書換が必要だった。
v30 で screen_config.json 1ファイル集約により、新画面追加時は「JSON を1つ書く」だけで済む。
本モジュールはその JSON を「画面名 = feature_name」だけで取得できる単一窓口。

【接続情報】
- 入力: target/intermediate/{feature_name}_screen_config.json
- 利用元: target/scripts/ 配下の add_screen_areas.py 等、ハードコードを廃した7スクリプト
- 仕様根拠: SKILL.md「Phase 3-4 外部設定ファイル化」セクション（v30）
- design-doc 配下の中間 JSON と同居（target/intermediate/）させることで、
  「画面別アーティファクト = intermediate/ 配下」の一貫性を保つ
"""
# 汎用化: sys.argv[1] / FEATURE_NAME 環境変数対応のため resolve_feature_name() ヘルパー追加
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
INTERMEDIATE_DIR = SKILL_DIR / "target" / "intermediate"

# 2026-10-05 汎用化: 既定の機能名（モジュール定数）は廃止した。
# 意味合い: 特定画面の名前を既定値に持つと、指定漏れのまま別画面の設定で処理が進んでしまう。
#          機能名は第1引数か環境変数 FEATURE_NAME で必ず与え、無ければ resolve_feature_name() が止める。
# 接続情報: 各スクリプトは get_feature_name_from_argv() で機能名を1回解決し、アクセサへ明示で渡す


def resolve_feature_name(feature_name=None) -> str:
    """汎用 feature_name 解決ヘルパー（汎用化で新設）。

    解決順序:
        1. 明示引数 feature_name（関数呼出時に渡されたもの）
        2. 環境変数 FEATURE_NAME
        3. どちらも無ければ SystemExit で止める（2026-10-05: 既定値は廃止）

    意味合い: スキル本体の汎用化のため、特定画面名の既定値を全アクセサから排除し、
              呼出元から（または環境変数 / コマンドライン引数経由で）画面名を伝搬させる単一窓口。
              未指定のまま別画面の設定で処理が進むのを防ぐため、解決できなければ止める。
              本関数は sys.argv を見ない（argv を見るのは get_feature_name_from_argv()）。

    接続情報: 呼出元 = _config_loader.py 内の全アクセサ関数（get_screen_config 等の
              内部で渡される None 値を本ヘルパーで正規化）/ apply_*.py / build_*.py / verify_*.py の
              main() がコマンドライン引数 sys.argv[1] を本ヘルパー経由で渡す。
    """
    if feature_name is not None and feature_name != "":
        return feature_name
    env_name = os.environ.get("FEATURE_NAME")
    if env_name:
        return env_name
    # 2026-10-05 汎用化: 既定値へ落とさず止める。文言は get_feature_name_from_argv() 経由でも同じになる
    raise SystemExit("feature_name 未指定: 第1引数か環境変数 FEATURE_NAME を指定してください")


def get_feature_name_from_argv() -> str:
    """コマンドライン引数 sys.argv[1] / 環境変数 FEATURE_NAME から feature_name を取得する。

    意味合い: 各 apply_*.py / build_*.py / verify_*.py の main() で統一的に呼ぶための単一窓口。
              各スクリプトが個別に書いていた「sys.argv[1] が無ければ固定の画面名」というパターンを
              ヘルパー化し、どの画面（{機能名}）でも同じ手順で再利用可能にする。

    解決順序:
        1. sys.argv[1]（コマンドライン第1引数）
        2. 環境変数 FEATURE_NAME
        3. どちらも無ければ resolve_feature_name() が SystemExit で止める（2026-10-05: 既定値は廃止）

    接続情報: 呼出元 = apply_trigger_dispatch.py / apply_logging_steps.py /
              build_call_graph.py / verify_call_graph.py の main()
    """
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    return resolve_feature_name()

# キャッシュ（同一プロセス内で複数回読まれても1回で済むよう）
_CACHE: dict = {}


def get_config_path(feature_name: str = None) -> Path:
    """画面別 screen_config.json の絶対パスを返す。

    意味合い: 命名規約「{feature_name}_screen_config.json」を集約管理する単一窓口。
    汎用化: 引数 None で渡されたら resolve_feature_name() 経由で
                       FEATURE_NAME 環境変数を解決（無ければ SystemExit。sys.argv は見ない）。
    """
    fn = resolve_feature_name(feature_name)
    return INTERMEDIATE_DIR / f"{fn}_screen_config.json"


def load_screen_config(feature_name: str = None) -> dict:
    """画面別 screen_config.json を読み込み、辞書として返す。

    呼出例:
        from _config_loader import load_screen_config
        cfg = load_screen_config("{機能名}")
        areas = cfg["areas"]

    引数:
        feature_name: 画面の機能名（例: "{機能名}"）。
                      ファイル名は {feature_name}_screen_config.json に展開される。

    返り値:
        dict — screen_config.json をパース した辞書。

    例外:
        FileNotFoundError — 該当 JSON が存在しない（新画面準備未完了等）
        json.JSONDecodeError — JSON 構文不正

    キャッシュ仕様:
        同一プロセス内で同一 feature_name の load を複数回呼んでも、ファイル I/O は1回のみ実行する。
        テスト等で強制再読込したい場合は clear_cache() を呼ぶ。

    汎用化: 引数 None で渡されたら resolve_feature_name() で解決。
    """
    fn = resolve_feature_name(feature_name)
    if fn in _CACHE:
        return _CACHE[fn]

    path = get_config_path(fn)
    if not path.exists():
        raise FileNotFoundError(
            f"screen_config.json が見つかりません: {path}\n"
            f"画面別ハードコード集約 JSON は target/intermediate/{fn}_screen_config.json に配置してください。"
        )

    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    _CACHE[fn] = cfg
    return cfg


def clear_cache():
    """キャッシュを強制クリアする。テストや config 編集後の再読込に使用。"""
    _CACHE.clear()


# ----- セクション別アクセサ（型保証 + デフォルト値の集約） -----
# 各スクリプトから cfg["areas"] のように直接読んでも動くが、
# セクションが欠落しているケース（旧バージョン JSON 等）のフォールバックを集約するため、
# 専用アクセサを用意する。これにより呼出元はキー欠落を気にせず使える。

def get_areas(feature_name: str = None) -> list:
    """screen_layout.areas[] 用のエリア定義リストを返す。"""
    return load_screen_config(feature_name).get("areas", [])


def get_area_pattern_subdivisions(feature_name: str = None) -> dict:
    """area_no → 細分化定義（subdivision_axis_label + subdivisions[]）の辞書を返す（v30+ 拡張）。

    意味合い: 明細グリッドのエリアのように、1エリア内の項目を業務軸（例: 区分ごと。税込/税抜/非課税 等）で
              さらに細分化して docx の §4.3 表示する用途。subdivisions[].remarks_keyword で
              items[].remarks 内の該当項目を抽出する設計（既存 items 構造を変えずに分割可能）。
    """
    return load_screen_config(feature_name).get("area_pattern_subdivisions", {})


def get_source_html_path(feature_name: str = None) -> Path:
    """source HTML のパスを、環境変数と ~ を展開したうえで Path オブジェクトで返す。

    2026-10-05 汎用化: 設定値に os.path.expandvars → os.path.expanduser を掛ける。
    意味合い: 配布する設定例に利用者ごとの絶対パスを書けないため、screen_config の
              source_html_path を "${HOME}/..." や "~/..." の形で書けるようにする。
              展開するのはこの返り値だけで、screen_config の値そのものは書き換えない
              （設定値をそのまま転記する側は未展開のまま読む）。
              値が空文字のときは展開せず、従来どおり Path("") を返す。
    接続情報: 入力 = screen_config.json の source_html_path。
              少なくとも extract_source_files.py / build_call_graph.py / merge_partials.py が呼ぶ。
    """
    cfg = load_screen_config(feature_name)
    raw = cfg.get("source_html_path", "")
    if raw:
        raw = os.path.expanduser(os.path.expandvars(raw))
    return Path(raw)


def get_shared_modules(feature_name: str = None) -> list:
    """共通モジュール一覧（[{path, description}, ...]）を返す。"""
    return load_screen_config(feature_name).get("shared_modules", [])


def get_file_descriptions(feature_name: str = None) -> dict:
    """ファイル名 → 説明のマップ（{css: {...}, js: {...}}）を返す。"""
    return load_screen_config(feature_name).get("file_descriptions", {})


def get_item_name_aliases(feature_name: str = None) -> dict:
    """項目名エイリアス → 権威語彙の置換辞書を返す（v51 で新設、画面別）。

    意味合い: trigger 原文（events[].trigger）に出てくる画面項目の表現揺らぎを、
              screen_layout.items[].item_name の権威語彙にマッピング。
              例: 「注文入力チェックボックス」→「注文入力」 / 「商品CD入力欄」→「グリッド商品CD」。
              normalize_screen_terminology.py の resolve_item が読んで、cleaned に対する
              部分一致解決を強化する。area_aliases（エリア名）と分離した項目名専用辞書。

    返り値: {raw: 権威項目名} の dict。設定がなければ空 dict。
    """
    section = load_screen_config(feature_name).get("item_name_aliases", {})
    if isinstance(section, dict):
        return section.get("aliases", {}) if "aliases" in section else section
    return {}


def get_area_aliases(feature_name: str = None) -> dict:
    """エリア名エイリアス（原文 → 権威語彙）の辞書を返す。"""
    return load_screen_config(feature_name).get("area_aliases", {})


def get_screen_name_aliases(feature_name: str = None) -> dict:
    """画面名エイリアスの辞書を返す。"""
    return load_screen_config(feature_name).get("screen_name_aliases", {})


def get_screen_name(feature_name: str = None) -> str:
    """画面の正式名称（例: "{機能名}画面"）を返す。

    意味合い: フォールバック・既定値として「画面名」が必要なケース（trigger パース失敗時の location 先頭等）
              で参照する。screen_config.screen_name フィールドを直読みする。
    """
    return load_screen_config(feature_name).get("screen_name", "")


# ============================================================
# project_config.json アクセサ（v39 で新設、非機能横断要素を画面非依存に管理）
# ============================================================
# 意味合い: スキル本体は「ロギング/監査/認証の構造」を抽象的に扱い、具体的な規約（8観測点モデル等）は
#          project_config.json に集約。どのプロジェクトでも本ファイルだけ差し替えればスキル
#          パイプラインを再利用可能にする汎用性確保の単一拘束点。

_PROJECT_CACHE: dict = {}


def get_project_config_path(feature_name: str = None) -> Path:
    """画面別 project_config.json の絶対パスを返す。命名規約: {feature_name}_project_config.json
    汎用化: 引数 None で渡されたら resolve_feature_name() 経由で解決。
    """
    fn = resolve_feature_name(feature_name)
    return INTERMEDIATE_DIR / f"{fn}_project_config.json"


def load_project_config(feature_name: str = None) -> dict:
    """画面別 project_config.json を読み込み、辞書として返す。

    呼出例:
        cfg = load_project_config("{機能名}")
        obs_points = cfg["logging"]["observation_points"]

    例外: 該当 JSON が存在しない場合は空 dict を返す（project_config は任意設定、ロギング規約が
          無いプロジェクトでもスキルパイプライン自体は動作する設計）。

    汎用化: 引数 None で渡されたら resolve_feature_name() で解決。
    """
    fn = resolve_feature_name(feature_name)
    if fn in _PROJECT_CACHE:
        return _PROJECT_CACHE[fn]
    path = get_project_config_path(fn)
    if not path.exists():
        _PROJECT_CACHE[fn] = {}
        return {}
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    _PROJECT_CACHE[fn] = cfg
    return cfg


def get_logging_spec(feature_name: str = None) -> dict:
    """logging 規約（observation_points, third_layer_meta 等）を返す。

    意味合い: プロジェクト固有の観測点モデル（例: 8 観測点）が入る位置。プロジェクトごとに別の observation_points が入る。
              空 dict が返れば「ロギング規約なし」 → apply_logging_steps.py は何もしない。
    """
    return load_project_config(feature_name).get("logging", {})


def get_audit_logging_spec(feature_name: str = None) -> dict:
    """audit_logging 規約（監査ログの送信関数に関する規約）を返す。"""
    return load_project_config(feature_name).get("audit_logging", {})


def get_forbidden_terms_map(feature_name: str = None) -> dict:
    """禁止語彙 → 業務語彙の置換辞書を返す（v51 で新設、案件依存）。

    意味合い: 案件の規約「コード内で旧システムの名前に言及しない」を案件単位で機械担保する。
              レガシー言語からの移行案件では旧名（旧プログラム名・旧変数名等）が Agent 出力に
              滲み出てしまうため、project_config.json の forbidden_terms_map.terms{} で
              {旧名: 業務語彙} を定義し、apply_logical_name_substitution.py が一括置換する。
              業界標準の用語は除外（このマップに含めない）。

              別案件では旧システムの名前がなければ空のまま運用、別レガシー言語があれば
              そのマップを書く（汎用設計）。

    返り値: {旧名: 業務語彙} の dict。設定がなければ空 dict。
    """
    section = load_project_config(feature_name).get("forbidden_terms_map", {})
    return section.get("terms", {}) if isinstance(section, dict) else {}


def get_backend_entry_patterns(feature_name: str = None) -> list:
    """バックエンドの入口ファイルを見分けるパス断片の配列を返す（2026-10-05 汎用化で新設）。

    目的: project_config.json の backend_entry_patterns[] を返す。

    意味合い: パスに配列のいずれかを含めば（部分一致。any(p in path for p in patterns)）、
              そのファイルを「バックエンドの入口ファイル」（画面からの要求を受け、action で
              処理を振り分けるファイル）とみなす。以前は特定の実行基盤の入口ファイル名を
              スクリプトに直書きしていたため、案件ごとの名前を設定へ出した。
              既定は、特定の実行基盤に依らない一般的な名前の ["handler"]。
              入口ファイルが別の名前の案件は、その名前を配列で与える。

    接続情報: 少なくとも build_call_graph.py（入口ファイルの探索）と
              extract_source_files.py（meta.backend_files[].kind の判定）が読む。
              値の出どころは load_project_config（project_config.json が無ければ {} なので既定が返る）。

    返り値: 文字列の list。設定がなければ ["handler"]。
    """
    return load_project_config(feature_name).get("backend_entry_patterns", ["handler"])


# ============================================================
# id_scheme.json アクセサ（v43 で新設、スキル全体の ID 体系をスキル付属設定で管理）
# ============================================================
# 意味合い: ID 体系はプロジェクト固有ではなくスキル全体の規約。screen_config / project_config と異なり、
#          スキル付属設定として SKILL_DIR/id_scheme.json に配置。新カテゴリ追加時はこのファイルだけ更新

ID_SCHEME_PATH = SKILL_DIR / "id_scheme.json"
_ID_SCHEME_CACHE = {}


def load_id_scheme() -> dict:
    """スキル全体の ID 体系定義（id_scheme.json）を読む"""
    if "loaded" in _ID_SCHEME_CACHE:
        return _ID_SCHEME_CACHE["data"]
    if not ID_SCHEME_PATH.exists():
        _ID_SCHEME_CACHE["loaded"] = True
        _ID_SCHEME_CACHE["data"] = {}
        return {}
    with ID_SCHEME_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    _ID_SCHEME_CACHE["loaded"] = True
    _ID_SCHEME_CACHE["data"] = data
    return data


def get_id_scheme_categories() -> list:
    """id_scheme.json の categories[] を返す。"""
    return load_id_scheme().get("categories", []) or []


def get_id_scheme_format() -> dict:
    """id_scheme.json の format（padding / start_number 等）を返す。"""
    return load_id_scheme().get("format", {}) or {}


def get_id_prefix(category_key: str) -> str:
    """カテゴリキー（例: 'front_process'）から接頭辞（例: 'FR'）を返す。

    意味合い: スキル本体（migrate / apply_logging_steps / generate_docx 等）から
              ハードコード接頭辞を全廃するための単一拘束点。
    """
    for c in get_id_scheme_categories():
        if c.get("key") == category_key:
            return c.get("prefix", "")
    return ""


def get_id_display_name(category_key: str) -> str:
    """カテゴリキーから docx 表示名（例: 'フロント処理'）を返す。"""
    for c in get_id_scheme_categories():
        if c.get("key") == category_key:
            return c.get("display_name", category_key)
    return category_key


def format_id(category_key: str, number, sub_label: str = "") -> str:
    """カテゴリと連番から新体系 ID 文字列を生成する。

    意味合い: 1 始まり可変桁（例: 'FR1', 'FR49'）+ サブラベル付きは '<prefix>#-<sub>'（例: 'LG4-a'）。
              format.padding が 'none' 以外なら zero-padding する将来拡張余地あり。
    """
    prefix = get_id_prefix(category_key)
    if not prefix:
        return str(number)
    fmt = get_id_scheme_format()
    padding = fmt.get("padding", "none")
    if padding == "none" or not isinstance(padding, int):
        num_str = str(number)
    else:
        num_str = str(number).zfill(int(padding))
    if sub_label:
        sep = fmt.get("sub_label_separator", "-")
        return f"{prefix}{num_str}{sep}{sub_label}"
    return f"{prefix}{num_str}"


def get_area_keyword_fallback(feature_name: str = None) -> list:
    """パース失敗時のキーワード→エリア名フォールバック定義リストを返す。

    順序保証付き（リスト形式、上位がヒットしたら即終了）。
    """
    return load_screen_config(feature_name).get("area_keyword_fallback", [])


def get_process_names_by_event_code(feature_name: str = None) -> dict:
    """event_code → 業務担当者語彙の処理名マップを返す。"""
    return load_screen_config(feature_name).get("process_names_by_event_code", {})


def get_initial_processes(feature_name: str = None) -> list:
    """initial_processes[].processes を返す（v42 で新設、案 B 汎用化）。

    意味合い: 画面初期表示時の URL パラメータ別の分岐パターン（F001 / F001-EDIT / F001-VIEW / F001-MISC 等）を
              画面別 JSON に外出し。少なくとも migrate_v17_to_v18.py の build_front_processes() がこの定義を読んで N 個の
              独立処理 (F001-*) を生成する。
              v18/v23 「呼ぶ側で判定、呼ばれる側は処理本体に専念」設計思想と整合。各 F001-* は呼ばれたら
              無条件で実行する純粋処理、URL パラメータ判定は trigger_dispatch.EV00 が担当。
    """
    return load_screen_config(feature_name).get("initial_processes", {}).get("processes", []) or []


def get_process_overview_overrides(feature_name: str = None) -> dict:
    """process_id → 業務目的1文の overview 上書き辞書を返す（v30 で追加）。

    意味合い: _derive_short_overview の自動抽出では長すぎる/業務目的になりきれない処理を画面別 JSON で明示。
              「概要」と「分岐・呼出（steps）」の内容重複を防ぐため、概要は業務目的1文に絞る。
              F001 は initialization.overview の長文コピーを上書きする用途も兼ねる。
    辞書の `_purpose` キー（メタ情報）は process_id ではないため利用側で無視する設計。
    """
    return load_screen_config(feature_name).get("process_overview_overrides", {})


def get_front_area_keywords(feature_name: str = None) -> list:
    """フロント処理のエリア分類用キーワードマップ（順序保証）を返す。"""
    return load_screen_config(feature_name).get("front_area_keywords", [])


def get_trigger_dispatch(feature_name: str = None) -> dict:
    """event_code → dispatch 定義の辞書を返す。

    各エントリは { trigger_match_keyword, dispatch[], strip_step_keywords[] } を持つ。
    """
    return load_screen_config(feature_name).get("trigger_dispatch", {})


def get_diagram_spec(diagram_id: str, feature_name: str = None) -> dict:
    """指定 diagram の spec（process_structure / screen_structure / ipo_flowchart）を返す。

    返り値の構造: {type, title, doc_section, embed_size, spec, [svg_layout]}
    """
    diagrams = load_screen_config(feature_name).get("diagrams", {})
    if diagram_id not in diagrams:
        raise KeyError(f"diagram '{diagram_id}' が screen_config.json に未定義です。")
    return diagrams[diagram_id]


def get_all_diagram_ids(feature_name: str = None) -> list:
    """登録済み diagram_id の一覧を返す。"""
    return list(load_screen_config(feature_name).get("diagrams", {}).keys())


if __name__ == "__main__":
    # 動作確認: sys.argv[1] / FEATURE_NAME 環境変数のいずれかで決定された（どちらも無ければ SystemExit）
    # feature_name について各セクションを読んで件数表示する（汎用化）
    feature = get_feature_name_from_argv()
    cfg = load_screen_config(feature)
    print(f"[OK] target feature: {feature}")
    print(f"[OK] feature_name: {cfg.get('feature_name')}")
    print(f"[OK] screen_name: {cfg.get('screen_name')}")
    print(f"[OK] source_html_path: {cfg.get('source_html_path')}")
    print(f"[OK] shared_modules: {len(get_shared_modules(feature))} 件")
    print(f"[OK] areas: {len(get_areas(feature))} 件")
    print(f"[OK] area_aliases: {len(get_area_aliases(feature))} 件")
    print(f"[OK] area_keyword_fallback: {len(get_area_keyword_fallback(feature))} 件")
    print(f"[OK] process_names_by_event_code: {len(get_process_names_by_event_code(feature))} 件")
    print(f"[OK] front_area_keywords: {len(get_front_area_keywords(feature))} 件")
    print(f"[OK] trigger_dispatch: {len(get_trigger_dispatch(feature))} 件")
    print(f"[OK] diagrams: {get_all_diagram_ids(feature)}")
