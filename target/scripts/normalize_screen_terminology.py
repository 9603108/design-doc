#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize_screen_terminology.py — events[].trigger を screen_layout.areas/items 語彙に正規化する（v20、v30 で外部設定化）

【目的】
events[].trigger の原文は「{画面名}のワークフローステッパー[5]「確定」ボタンをクリックする。」の形式で、
[N] は items[].no への参照、エリア名は areas[].area_name の略称or別名 になっている。
これを screen_layout の権威語彙（areas[].area_name / items[].item_name）に正規化し、
events[].location / area_ref / item_ref_no を整合性ルール 18-20 に従って書き換える。

【意味合い】
v19 まで events[].location は trigger 原文を機械パースした結果で、screen_layout に存在しない用語
（「ワークフローステッパー[5]」「テーブル[43]」等）が含まれていた。
v20 では cross-reference 整合性を全体的に保証するため、screen_layout を権威ソースに据えて
events / trigger_groups / front_processes / backend_processes の画面項目参照を全て正規化する。
v30 でハードコード辞書（AREA_ALIAS / SCREEN_NAME_ALIAS / area_keyword_map）を screen_config.json
に外出しし、本スクリプトを画面非依存（汎用化）にした。

【接続情報】
- 入力: target/intermediate/{機能名}.json + target/intermediate/{機能名}_screen_config.json
- 出力: 同ファイルを上書き
- 仕様根拠: 15_中間JSONスキーマ.md「screen_layout」セクション（v20 拡張）、整合性ルール 18-20、SKILL.md「Phase 3-4 外部設定ファイル化」（v30）
- 副作用: events[].location / trigger_groups[].triggers[].location を権威語彙に置換、
          events[].area_ref / events[].item_ref_no を新規付与、
          画面操作トリガー（kind == "screen_operation"）で area_ref が空のものへ、
          対応する events[] の area_ref / location を写す（既に area_ref があるものは触らない）
"""
import json
import re
import sys

# v30: ハードコード辞書を screen_config.json に集約、_config_loader 経由で取得する
# v51: item_name_aliases も画面別 JSON から取得（項目名表現揺らぎを権威語彙に吸収）
from _config_loader import (
    INTERMEDIATE_DIR,
    get_feature_name_from_argv,
    get_area_aliases,
    get_screen_name_aliases,
    get_area_keyword_fallback,
    get_screen_name,
    get_item_name_aliases,
)

# 2026-10-05 汎用化: 既定の機能名を廃止し、機能名はここで1回だけ解決する
# 目的: 第1引数 > 環境変数 FEATURE_NAME の順で機能名を確定する（どちらも無ければ _config_loader が SystemExit）
# 意味合い: _config_loader のアクセサは引数なしだと sys.argv を見ないため、第1引数だけで渡された機能名を
#          拾えない。直後のモジュール先頭アクセサ（import 時に実行）より前に解決し、明示で渡す
# 接続情報: 中間 JSON は _config_loader.INTERMEDIATE_DIR / "{機能名}.json"、
#          設定は同ディレクトリの "{機能名}_screen_config.json"（アクセサ経由で読む）
FEATURE_NAME = get_feature_name_from_argv()
INTERMEDIATE_JSON = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"

# v30: AREA_ALIAS / SCREEN_NAME_ALIAS は screen_config.json から動的取得（モジュールロード時に1回）
# 意味合い: 画面別ハードコードを画面別 JSON に外出しすることで、本スクリプトは画面非依存の汎用処理になる
AREA_ALIAS = get_area_aliases(FEATURE_NAME)
SCREEN_NAME_ALIAS = get_screen_name_aliases(FEATURE_NAME)
# v51: 項目名エイリアス（「検索条件チェックボックス」→「検索条件」のような表現揺らぎ吸収）
ITEM_NAME_ALIAS = get_item_name_aliases(FEATURE_NAME)


def build_authority(data):
    """画面用語の権威集合を返す: {area_name: area_dict}, {item_no: item_dict}, {item_name: item_dict}"""
    sl = data.get("screen_layout", {})
    areas_by_name = {a["area_name"]: a for a in sl.get("areas", [])}
    items_by_no = {it["no"]: it for it in sl.get("items", [])}
    items_by_name = {}
    for it in sl.get("items", []):
        name = it.get("item_name", "")
        if name:
            items_by_name.setdefault(name, []).append(it)
    return areas_by_name, items_by_no, items_by_name


def resolve_area(raw_area: str, areas_by_name: dict) -> dict:
    """エリア名の原文（"ワークフローステッパー[5]" 等）から area dict を解決する。"""
    if not raw_area:
        return None
    # [N] / (N) の番号サフィックスを除去
    cleaned = re.sub(r'\s*[\[\(]\d+[\]\)].*$', '', raw_area).strip()
    # エイリアス → 権威語彙
    if cleaned in areas_by_name:
        return areas_by_name[cleaned]
    if cleaned in AREA_ALIAS:
        auth_name = AREA_ALIAS[cleaned]
        return areas_by_name.get(auth_name)
    # 部分一致フォールバック（「ワークフローステッパーで「①確定」」等の長文）
    for alias, auth in AREA_ALIAS.items():
        if alias in cleaned:
            return areas_by_name.get(auth)
    return None


def resolve_item(item_no: int, item_name_raw: str, items_by_no: dict, items_by_name: dict) -> dict:
    """item の権威表記を解決する。item_no が最優先、なければ item_name 部分一致 → エイリアス辞書順。

    v51 改修: cleaned が直接ヒットしない場合、screen_config.json の item_name_aliases を引いて
              権威項目名に変換してから再検索する。これで「検索条件チェックボックス」→「検索条件」/
              「商品CD入力欄」→「グリッド商品CD」等の表現揺らぎを screen_config.json 経由で吸収可能。
    """
    if item_no is not None and item_no in items_by_no:
        return items_by_no[item_no]
    # name 部分一致（"「確定」ボタン" → items[item_name="確定"]）
    if item_name_raw:
        cleaned = re.sub(r'^「|」(ボタン|タブ|リンク|フィールド)?$|ボタン$|タブ$|リンク$', '', item_name_raw).strip()
        cleaned = cleaned.strip('「」')
        if cleaned in items_by_name and items_by_name[cleaned]:
            return items_by_name[cleaned][0]
        # 完全一致
        if item_name_raw in items_by_name and items_by_name[item_name_raw]:
            return items_by_name[item_name_raw][0]
        # v51: エイリアス辞書を引く（raw と cleaned 両方試す）
        for key in (item_name_raw, cleaned):
            if key in ITEM_NAME_ALIAS:
                auth_name = ITEM_NAME_ALIAS[key]
                if auth_name in items_by_name and items_by_name[auth_name]:
                    return items_by_name[auth_name][0]
        # v51: エイリアスを部分一致で試す（長文 trigger 等のフォールバック）
        for alias, auth_name in ITEM_NAME_ALIAS.items():
            if alias and alias in item_name_raw:
                if auth_name in items_by_name and items_by_name[auth_name]:
                    return items_by_name[auth_name][0]
    return None


# trigger パース用パターン
# 2026-10-05 汎用化: 画面名の直書きをやめ、screen_config.screen_name から組み立てる
# 目的: 「{画面名}の…」形の trigger を、どの画面の設定でも同じ3パターンで解析する
# 意味合い: 画面名は第1キャプチャグループ（parse_trigger が m.group(1) を screen に使う）。
#          「（編集モード）」の任意部分と後続部分は従来のまま（動作を変えない）
# 接続情報: screen_name は _config_loader.get_screen_name(FEATURE_NAME) が {機能名}_screen_config.json から読む。
#          空だと「^(...)の」が画面名なしの文にも一致してしまうため、ここで止める
_SCREEN = re.escape(get_screen_name(FEATURE_NAME))
if not _SCREEN:
    raise SystemExit(f"screen_name 未設定: {FEATURE_NAME}_screen_config.json の screen_name を指定してください")
# 1. {画面名}の{エリア}[{no}]{項目}を{動作}する
PATTERN_AREA_ITEM = re.compile(r'^(' + _SCREEN + r'(?:（編集モード）)?)の([^\[]+?)\[(\d+)\](.+?)を(.+?)(?:する)?[。.]?\s*$')
# 2. {画面名}の[{no}]{項目}を{動作}する（エリア略）
PATTERN_ITEM_ONLY = re.compile(r'^(' + _SCREEN + r'(?:（編集モード）)?)の\[(\d+)\](.+?)を(.+?)(?:する)?[。.]?\s*$')
# 3. {画面名}の{エリア+項目混在}を{動作}する（番号なし）
PATTERN_FREE = re.compile(r'^(' + _SCREEN + r'(?:（編集モード）)?)の(.+?)を(.+?)(?:する)?[。.]?\s*$')


def parse_trigger(trigger: str):
    """trigger 原文から (screen_name, area_raw, item_no, item_name_raw, action) を抽出。
    解析できないパターンは None を返す。"""
    if not trigger:
        return None
    trigger = trigger.strip()

    m = PATTERN_AREA_ITEM.match(trigger)
    if m:
        return {
            "screen": m.group(1),
            "area_raw": m.group(2),
            "item_no": int(m.group(3)),
            "item_name_raw": m.group(4),
            "action_raw": m.group(5)
        }

    m = PATTERN_ITEM_ONLY.match(trigger)
    if m:
        return {
            "screen": m.group(1),
            "area_raw": None,
            "item_no": int(m.group(2)),
            "item_name_raw": m.group(3),
            "action_raw": m.group(4)
        }

    m = PATTERN_FREE.match(trigger)
    if m:
        return {
            "screen": m.group(1),
            "area_raw": m.group(2),
            "item_no": None,
            "item_name_raw": None,
            "action_raw": m.group(3)
        }

    return None


def normalize_event(ev: dict, areas_by_name: dict, items_by_no: dict, items_by_name: dict) -> dict:
    """events[].location を権威語彙に正規化、area_ref / item_ref_no を付与。

    v20 拡張: 「{画面名}の...」パターンに当てはまらない長文 trigger は、
    特殊カテゴリ（WebSocket受信 / DevToolsコンソール / 業務フロー など）として
    location を簡潔に再構成する。これにより整合性検証で screen_layout 厳密一致対象外と区別できる。
    """
    trigger = ev.get("trigger", "") or ""
    source_kind = ev.get("source_kind", "screen")
    parsed = parse_trigger(trigger)

    new_location = []
    area_ref = None
    item_ref_no = None
    item_name_normalized = None

    # 特殊カテゴリの先取り処理（source_kind=internal 等）
    if source_kind == "internal" or not parsed:
        # WebSocket 受信系
        if "WebSocket" in trigger or "websocket" in trigger.lower():
            # 「他ユーザーが...editing通知を受信」「他ユーザーが...保存」等
            sub_label = trigger.strip().rstrip("。").rstrip("する")
            # 短くする (60文字以内)
            if len(sub_label) > 60:
                sub_label = sub_label[:60] + "…"
            new_location = ["WebSocket受信", sub_label]
            ev["location"] = new_location
            ev["external_trigger_kind"] = "websocket"
            return ev
        # DevTools / 開発者コンソール
        if "DevTools" in trigger or "コンソール" in trigger or "runIntegrationTests" in trigger or "runE2EScenarios" in trigger:
            # 「ブラウザのDevToolsコンソールで `runIntegrationTests()` を実行する」
            m = re.search(r'`([^`]+)`', trigger)
            func = m.group(1) if m else "（関数名不明）"
            new_location = ["DevToolsコンソール", func]
            ev["location"] = new_location
            ev["external_trigger_kind"] = "devtools"
            return ev

    if parsed:
        # 画面名は権威語彙に統一（「{画面名}（編集モード）」→「{画面名}」）
        screen_name = SCREEN_NAME_ALIAS.get(parsed["screen"], parsed["screen"])
        new_location.append(screen_name)

        # エリア解決
        area = resolve_area(parsed.get("area_raw") or "", areas_by_name) if parsed.get("area_raw") else None
        # area が無くて item_no があれば、item の area_ref から逆引き
        if area is None and parsed.get("item_no") is not None:
            it = items_by_no.get(parsed["item_no"])
            if it and it.get("area_ref"):
                # area_ref から area dict を取得
                for an, ad in areas_by_name.items():
                    if ad.get("area_no") == it.get("area_ref"):
                        area = ad
                        break
        if area:
            new_location.append(area["area_name"])
            area_ref = area["area_no"]

        # 項目解決
        item = resolve_item(parsed.get("item_no"), parsed.get("item_name_raw") or "", items_by_no, items_by_name)
        if item:
            new_location.append(item["item_name"])
            item_ref_no = item["no"]
            item_name_normalized = item["item_name"]
        elif parsed.get("item_name_raw"):
            # 解決できなかったが原文を残す（自由記述 → ERROR候補）
            cleaned = parsed["item_name_raw"].strip().strip("「」").rstrip("ボタンタブリンク").strip()
            # v51: 末尾に残った [N] / /[N] / 数字単独パターンを除去（item_name_aliases で吸収不可なノイズ）
            cleaned = re.sub(r'/\s*\[\d+\]\s*$', '', cleaned).strip()
            cleaned = re.sub(r'^/\s*', '', cleaned).strip()
            # v51: cleaned が `[N]` 単独/数字のみ/空文字なら append しない（ERROR の根本原因）
            if cleaned and not re.fullmatch(r'\[\d+\]|/\[\d+\]|\d+|/+', cleaned):
                # 改めて aliases / items_by_name で再解決を試す（v51: cleanup 後の再ヒット）
                if cleaned in items_by_name:
                    new_location.append(items_by_name[cleaned][0]["item_name"])
                    item_ref_no = items_by_name[cleaned][0]["no"]
                elif cleaned in ITEM_NAME_ALIAS:
                    auth = ITEM_NAME_ALIAS[cleaned]
                    if auth in items_by_name:
                        new_location.append(items_by_name[auth][0]["item_name"])
                        item_ref_no = items_by_name[auth][0]["no"]
                    else:
                        new_location.append(auth)
                else:
                    new_location.append(cleaned)

    else:
        # パース失敗時: trigger 本文からキーワード抽出してエリアを推定（v20 フォールバック、v30 で外部設定化）
        # 意味合い: 「ステッパー」「業務フロー」「サジェスト」「出力メニュー」等の頻出キーワードに対応。
        #          画面別キーワードを screen_config.json area_keyword_fallback に集約。
        # 画面名は screen_config.screen_name フィールドを使用（v30 でハードコードの画面名を排除）
        # 2026-10-05 汎用化: アクセサは引数なしだと sys.argv を見ないため、解決済みの FEATURE_NAME を明示で渡す
        # 画面名が空の場合の代替表記は置かない: モジュール先頭の _SCREEN 組み立てで screen_name が空なら
        # SystemExit するため、ここへ来た時点で get_screen_name(FEATURE_NAME) は必ず非空
        new_location = [get_screen_name(FEATURE_NAME)]
        area_keyword_map = [
            (entry["keywords"], entry["area_name"])
            for entry in get_area_keyword_fallback(FEATURE_NAME)
        ]
        matched_area = None
        for kws, area_name in area_keyword_map:
            if any(kw in trigger for kw in kws):
                matched_area = areas_by_name.get(area_name)
                break
        if matched_area:
            new_location.append(matched_area["area_name"])
            area_ref = matched_area["area_no"]

    # v51: location[] 全要素を最終クリーンアップ
    #   意味合い: [N] 単独要素や末尾 /[N] のような数字ノイズを除去、
    #            aliases にヒットすれば権威語彙に置換、これにより verify_intermediate ERROR 0 を目指す
    new_location = _cleanup_location(new_location, items_by_name)

    # ev に書き戻し
    ev["location"] = new_location
    if area_ref:
        ev["area_ref"] = area_ref
    if item_ref_no is not None:
        ev["item_ref_no"] = item_ref_no
    return ev


def _cleanup_location(loc: list, items_by_name: dict) -> list:
    """location[] の各要素を権威語彙に揃え、ノイズを削除する（v51 で新設）。

    意味合い: convert_events_location_action.py が分解した location[] の各要素には
              「[1]」「/[11]」のような数字ノイズや「業務フロー」「担当者が承認依頼」
              のような自由記述が残ることがある。これを screen_config.json の item_name_aliases
              / area_aliases を引いて権威語彙に揃える単一の通過点。
    """
    cleaned = []
    for x in loc:
        if not isinstance(x, str):
            cleaned.append(x); continue
        s = x.strip()
        # 数字ノイズ削除: [N] / /[N] / 数字単独
        s = re.sub(r'/\s*\[\d+\]\s*$', '', s).strip()
        s = re.sub(r'^/\s*', '', s).strip()
        if not s or re.fullmatch(r'\[\d+\]|/\[\d+\]|\d+|/+', s):
            continue
        # aliases ヒットで権威語彙に置換
        if s in ITEM_NAME_ALIAS:
            auth = ITEM_NAME_ALIAS[s]
            cleaned.append(auth)
            continue
        if s in AREA_ALIAS:
            cleaned.append(AREA_ALIAS[s]); continue
        # 既に権威語彙ならそのまま
        cleaned.append(s)
    return cleaned


def normalize_trigger_groups(data, areas_by_name, items_by_no, items_by_name):
    """trigger_groups[].triggers[].location も同じロジックで正規化、category も画面名 or areas 名に揃える。"""
    for g in data.get("trigger_groups", []) or []:
        # category 正規化: screen_operation の場合は画面名 or area_name に揃える
        if g.get("kind") == "screen_operation":
            cat = g.get("category", "")
            if cat in SCREEN_NAME_ALIAS:
                g["category"] = SCREEN_NAME_ALIAS[cat]
            elif cat in areas_by_name:
                pass  # area_name と一致 → そのまま
            else:
                # 「閲覧者として画面を開いたときに...」のような長文 → {画面名}に統合
                # 2026-10-05 汎用化: 統合先の画面名は screen_config.screen_name（_config_loader.get_screen_name 経由）
                g["category"] = get_screen_name(FEATURE_NAME)
        for t in g.get("triggers", []) or []:
            # location 全要素を権威語彙へ
            new_loc = []
            for elem in t.get("location", []) or []:
                if not isinstance(elem, str):
                    new_loc.append(elem); continue
                s = elem.strip()
                # v51: 数字ノイズ除去（[N] / /[N] / 数字単独）
                s = re.sub(r'/\s*\[\d+\]\s*$', '', s).strip()
                s = re.sub(r'^/\s*', '', s).strip()
                if not s or re.fullmatch(r'\[\d+\]|/\[\d+\]|\d+|/+', s):
                    continue
                if s in SCREEN_NAME_ALIAS:
                    new_loc.append(SCREEN_NAME_ALIAS[s])
                elif s in areas_by_name:
                    new_loc.append(s)
                elif s in [it["item_name"] for it in items_by_no.values()]:
                    new_loc.append(s)
                elif s in ITEM_NAME_ALIAS:
                    # v51: 項目名エイリアス
                    auth = ITEM_NAME_ALIAS[s]
                    if auth in items_by_name:
                        new_loc.append(items_by_name[auth][0]["item_name"])
                    else:
                        new_loc.append(auth)
                else:
                    # エリア解決 → area_name
                    area = resolve_area(s, areas_by_name)
                    if area:
                        new_loc.append(area["area_name"])
                    else:
                        # 項目解決 → item_name（item_name_aliases も含めて解決）
                        item = resolve_item(None, s, items_by_no, items_by_name)
                        if item:
                            new_loc.append(item["item_name"])
                        else:
                            # v51: area_keyword_fallback で最後の救済を試す
                            matched = None
                            for entry in get_area_keyword_fallback(FEATURE_NAME):
                                kws = entry.get("keywords", [])
                                if any(kw in s for kw in kws):
                                    matched = entry.get("area_name")
                                    break
                            if matched and matched in areas_by_name:
                                new_loc.append(matched)
                            else:
                                # 解決不能、原文維持（検証で WARN/ERROR になる）
                                new_loc.append(s)
            t["location"] = new_loc


def normalize_screen_mapping(data, items_by_name):
    """backend_processes[].response_to_screen_mapping[].screen_item_name を items.item_name と完全一致させる。"""
    for bp in data.get("backend_processes", []) or []:
        respspec = bp.get("response_spec", {}) or {}
        mapping = respspec.get("response_to_screen_mapping", []) or []
        for m in mapping:
            sin = m.get("screen_item_name")
            if not sin or sin in items_by_name:
                continue
            # 「グリッド全体」のような疑似項目は補正不可能 → null にする or 残す
            # 完全一致しないものはそのまま（疑似項目として許容）


def main():
    if not INTERMEDIATE_JSON.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {INTERMEDIATE_JSON}", file=sys.stderr)
        sys.exit(1)

    with INTERMEDIATE_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    areas_by_name, items_by_no, items_by_name = build_authority(data)

    # 1. events[] 正規化
    normalized_count = 0
    failed_parse = 0
    for ev in data.get("events", []) or []:
        before = list(ev.get("location", []) or [])
        normalize_event(ev, areas_by_name, items_by_no, items_by_name)
        if ev.get("location") != before:
            normalized_count += 1

    # 2. trigger_groups[] 正規化
    normalize_trigger_groups(data, areas_by_name, items_by_no, items_by_name)

    # 2.5 画面操作トリガーへ events[] の area_ref / location を写す
    # 目的: kind == "screen_operation" で area_ref が空の trigger に、対応する event の area_ref と
    #       正規化済みの location を写す。写した件数を copied_area_ref に数える
    # 意味合い: trigger_groups は migrate_v17_to_v18.py の build_trigger_groups が events[] から作り直すが、
    #          その時点の events[] には area_ref がまだ無い（上の「1. events[] 正規化」で初めて付く）。
    #          写さないと画面操作トリガーが画面エリアに属さないままになり、発生場所にもエリアが出ない。
    #          既に area_ref を持つ trigger は area_ref も location も触らない（人手や後続の補修で
    #          event と違う値を持たせている場合があるため）。auto / external は画面エリアに属さない
    #          トリガーなので写さない。event 側に area_ref が無いときは何も書かない（値を作らない）
    # 接続情報: 突き合わせキーは (trigger.description, trigger.action)。description は build_trigger_groups が
    #          「events[].trigger、無ければ content」で作るので、event 側も同じ式で組む。
    #          同じキーが複数あるときは出現順に1件ずつ対応させる（対象外の trigger でも1件進める）。
    #          写した area_ref は verify_intermediate.py の画面操作トリガーの area_ref 検査が読む
    events_by_key = {}
    for ev in data.get("events", []) or []:
        key = (ev.get("trigger", "") or ev.get("content", ""), ev.get("action", ""))
        events_by_key.setdefault(key, []).append(ev)
    copied_area_ref = 0
    for g in data.get("trigger_groups", []) or []:
        for t in g.get("triggers", []) or []:
            candidates = events_by_key.get((t.get("description", ""), t.get("action", "")))
            if not candidates:
                continue
            ev = candidates.pop(0)
            if g.get("kind") != "screen_operation" or t.get("area_ref") or not ev.get("area_ref"):
                continue
            t["area_ref"] = ev["area_ref"]
            t["location"] = list(ev.get("location", []) or [])
            copied_area_ref += 1

    # 3. response_to_screen_mapping 正規化（基本そのまま）
    normalize_screen_mapping(data, items_by_name)

    with INTERMEDIATE_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[OK] events[] 正規化: {normalized_count} 件 / {len(data.get('events', []))}")
    print(f"[OK] trigger_groups[] / response_to_screen_mapping[] も正規化済")
    print(f"[OK] 画面操作トリガーへ events[] の area_ref / location を写した件数: {copied_area_ref}")
    print(f"[OK] 保存先: {INTERMEDIATE_JSON}")


if __name__ == "__main__":
    main()
