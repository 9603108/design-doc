#!/usr/bin/env python3
# 目的: 中間JSON の content/overview/trigger 内の「発火」を日本のIT文脈に合う表現に一括置換する。
# 意味合い: レビュー指摘「『発火』は日本のIT現場で使わない用語」への対応。
#           "fire/trigger" の直訳的表現を、業務担当者にも読める「呼び出す」「実行する」「再表示」に置換。
# 接続情報: 入力 = target/intermediate/{機能名}.json / 出力 = 同 JSON を上書き保存
#           機能名は第1引数か環境変数 FEATURE_NAME で与える（_config_loader.get_feature_name_from_argv が解決）
# 置換ルール: 「を発火し」→「を呼び出し」/ 「を発火」→「を呼び出す」/ 「再発火」→「再表示」/ 残り「発火」→「実行」

import json
from pathlib import Path

# 2026-10-05 汎用化: 特定案件の絶対パスと機能名の直書きをやめ、機能名から中間 JSON を決める。
#   目的: どの画面・どの環境でも同じスクリプトで動かす（配布物に絶対パスと案件固有の機能名を残さない）。
#   意味合い: 機能名が未指定なら _config_loader 側の SystemExit で止まる（既定の機能名は持たない）。
#   接続情報: INTERMEDIATE_DIR / get_feature_name_from_argv は同ディレクトリの _config_loader が提供。
#             INTERMEDIATE は main() が読み込み・上書き保存・結果表示に使う。
from _config_loader import INTERMEDIATE_DIR, get_feature_name_from_argv

FEATURE_NAME = get_feature_name_from_argv()
INTERMEDIATE = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"

# 置換ルール（順序重要、長いパターンから先に）
REPLACEMENTS = [
    ("を発火し", "を呼び出し"),
    ("を発火する", "を呼び出す"),
    ("を発火", "を呼び出す"),
    ("再発火", "再表示"),
    ("発火する", "実行する"),
    ("発火", "実行"),
]


def fix(text):
    if not text:
        return text
    s = str(text)
    for old, new in REPLACEMENTS:
        s = s.replace(old, new)
    return s


def main():
    with INTERMEDIATE.open(encoding="utf-8") as f:
        data = json.load(f)

    changed = 0

    # events[]: trigger, content, action
    for e in data.get("events", []) or []:
        for k in ("trigger", "content", "action"):
            v = e.get(k)
            if v and "発火" in str(v):
                e[k] = fix(v)
                changed += 1

    # event_processes[]: overview, details, pattern
    for ep in data.get("event_processes", []) or []:
        for k in ("overview", "pattern"):
            v = ep.get(k)
            if v and "発火" in str(v):
                ep[k] = fix(v)
                changed += 1
        if "details" in ep and isinstance(ep["details"], list):
            new_details = []
            for d in ep["details"]:
                if d and "発火" in str(d):
                    new_details.append(fix(d))
                    changed += 1
                else:
                    new_details.append(d)
            ep["details"] = new_details

    # screen_layout.items[]: remarks
    for it in (data.get("screen_layout") or {}).get("items", []) or []:
        for k in ("remarks", "item_name"):
            v = it.get(k)
            if v and "発火" in str(v):
                it[k] = fix(v)
                changed += 1

    # messages[]: content
    for m in data.get("messages", []) or []:
        v = m.get("content")
        if v and "発火" in str(v):
            m["content"] = fix(v)
            changed += 1

    # api_spec[]: description, response_items.expression
    for a in data.get("api_spec", []) or []:
        for k in ("description",):
            v = a.get(k)
            if v and "発火" in str(v):
                a[k] = fix(v)
                changed += 1

    with INTERMEDIATE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(json.dumps({"intermediate": str(INTERMEDIATE), "changed_fields": changed}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
