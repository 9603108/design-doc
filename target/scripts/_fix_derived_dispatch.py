#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_fix_derived_dispatch.py — 派生処理（出力メニュー / 外部システム連携 / ファイル出力等）を trigger_dispatch に追加（v51 汎用化）

【目的】
中間 JSON の trigger_groups[].triggers[] に「派生処理（出力メニュー / 外部システム連携 / ファイル出力等）」用の
trigger エントリを追加し、dispatch[] で「本画面は参考表示のみ・実行は別画面で行う」もしくは
「派生先の calls + then_message」を明示する。派生元を derived_from フィールドにマークする。

【意味合い】
v50 時点では特定の1画面専用のスクリプトとして、その画面の派生イベント2件をハードコードしていたが、
同じ「派生処理（共通 UI から呼び出されるが本画面では参考表示のみ）」のパターンは他画面でも発生する。
v51 で screen_config.json の新セクション `derived_processes[]` から読む形に汎用化。
新画面追加時は screen_config.json に派生処理を1エントリ書くだけで対応可能になる。

【接続情報】
- 入力: target/intermediate/{feature_name}.json
        target/intermediate/{feature_name}_screen_config.json（derived_processes[] セクション必須）
- 出力: target/intermediate/{feature_name}.json （trigger_groups[].triggers[] に追記）
- 参照: 15_中間JSONスキーマ.md「trigger_groups[]」セクション + 「派生処理（derived_from）」
        15/16 仕様書にも同じ schema を反映済み

【引数仕様】
- sys.argv[1]: feature_name（画面名）。省略時は環境変数 FEATURE_NAME、どちらも無ければ止まる
- 例: python3 _fix_derived_dispatch.py <機能名>

【screen_config.json の derived_processes[] スキーマ】
派生処理の定義は screen_config.json に以下の形で書く:

```json
{
  "derived_processes": [
    {
      "event_ref": "EV12",
      "trigger_group_kind": "screen_operation",
      "area_ref": "AR2",
      "location": ["ある入力画面", "ワークフローステッパー", "ステッパー：外部連携"],
      "action": "クリック",
      "description": "業務担当者がステッパー「外部連携」を押下する（参考表示のみ）",
      "derived_from": "出力メニュー（共通UI・別の詳細画面で実行）",
      "calls": [],
      "dispatch": [
        {
          "condition": "本画面では押下しても処理を実行しない（参考表示のみ）",
          "then_terminate": true,
          "then_message": "別の詳細画面で実行（本画面は参考表示のみ）"
        }
      ]
    }
  ]
}
```

【後方互換】
- 旧版（特定の1画面専用だったスクリプト。末尾に .deprecated_20260526 が付く）は同ディレクトリに残置済
- 新規画面追加時は本スクリプトを使い、screen_config.json に derived_processes[] を書く
"""
import json
import shutil
import sys
from pathlib import Path
from datetime import datetime

# 2026-10-05 汎用化: 独自の機能名解決関数(特定画面を既定値に返していた)を廃止した。
# 目的: 機能名の解決規約を _config_loader の1か所に寄せ、既定の画面を持たない。
# 接続情報: get_feature_name_from_argv() は 第1引数 → 環境変数 FEATURE_NAME の順で解決し、
#           どちらも無ければ SystemExit で止まる。INTERMEDIATE_DIR は target/intermediate。
#           どちらも main() が使う。
#           パスは INTERMEDIATE_DIR だけで組むため、このファイルは自前のディレクトリ定数を持たない
#           （参照が無くなった定数は取り除いた。Path は load / dump の型注釈で使う）。
from _config_loader import INTERMEDIATE_DIR, get_feature_name_from_argv


def load(p: Path) -> dict:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump(p: Path, data: dict) -> None:
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def next_trigger_no(triggers: list) -> str:
    """既存 trigger_no（TRn 形式）の最大値 + 1 を返す。"""
    max_n = 0
    for t in triggers:
        tn = t.get("trigger_no", "")
        if tn.startswith("TR"):
            try:
                max_n = max(max_n, int(tn[2:]))
            except ValueError:
                pass
    return f"TR{max_n + 1}"


def main():
    # 機能名は main 冒頭で1回だけ解決し、以降はこの値を使う（argv・環境変数を二重に読まない）
    feature_name = get_feature_name_from_argv()
    intermediate_json = INTERMEDIATE_DIR / f"{feature_name}.json"
    screen_config_json = INTERMEDIATE_DIR / f"{feature_name}_screen_config.json"

    print(f"[info] feature_name = {feature_name}")
    print(f"[info] intermediate = {intermediate_json}")
    print(f"[info] screen_config = {screen_config_json}")

    if not intermediate_json.exists():
        print(f"[ERROR] 中間 JSON が見つかりません: {intermediate_json}", file=sys.stderr)
        sys.exit(1)

    if not screen_config_json.exists():
        print(f"[ERROR] screen_config.json が見つかりません: {screen_config_json}", file=sys.stderr)
        sys.exit(1)

    sc = load(screen_config_json)
    derived_processes = sc.get("derived_processes", []) or []

    if not derived_processes:
        print(f"[info] screen_config.json に derived_processes[] が無いため、追加対象なし（正常）")
        return

    print(f"[info] derived_processes[]: {len(derived_processes)} 件の派生処理定義を読込")

    # バックアップ
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = intermediate_json.with_suffix(f".json.bak_peridisp_{ts}")
    shutil.copyfile(intermediate_json, backup)
    print(f"[backup] {backup}")

    data = load(intermediate_json)

    added_count = 0
    skipped_count = 0
    for entry in derived_processes:
        # 1. ターゲット trigger_group を選定（kind == entry.trigger_group_kind、既定 screen_operation）
        target_kind = entry.get("trigger_group_kind", "screen_operation")
        target_group = None
        for tg in data.get("trigger_groups", []):
            if tg.get("kind") == target_kind:
                target_group = tg
                break
        if target_group is None:
            print(f"  [ERROR] {entry.get('event_ref')}: trigger_group kind={target_kind} が見つかりません")
            continue

        triggers = target_group.setdefault("triggers", [])

        # 2. 既存 event_ref があれば skip（重複防止）
        existing_event_refs = {t.get("event_ref") for t in triggers}
        ev_ref = entry.get("event_ref")
        if not ev_ref:
            print(f"  [WARN] event_ref が空のエントリをスキップ: {entry}")
            continue
        if ev_ref in existing_event_refs:
            print(f"  [skip] {ev_ref}: 既に trigger 化されています")
            skipped_count += 1
            continue

        # 3. 新 trigger 構築
        tn = next_trigger_no(triggers)
        new_trigger = {
            "trigger_no": tn,
            "area_ref": entry.get("area_ref", ""),
            "location": entry.get("location", []),
            "action": entry.get("action", "クリック"),
            "description": entry.get("description", ""),
            "event_ref": ev_ref,
            "calls": entry.get("calls", []),
            # 派生処理の派生元を明示（v50/v51 trigger スキーマの拡張フィールド）
            "derived_from": entry.get("derived_from", ""),
            "dispatch": entry.get("dispatch", []),
        }
        triggers.append(new_trigger)
        added_count += 1
        print(f"  [add] {tn} | event_ref={ev_ref} | location={'/'.join(new_trigger['location']) if new_trigger['location'] else '(none)'}")
        print(f"        derived_from={new_trigger['derived_from']}")
        for d in new_trigger["dispatch"]:
            if d.get("then_call"):
                print(f"        dispatch: {d.get('condition','')} → {d.get('then_call')}")
            elif d.get("then_terminate"):
                print(f"        dispatch: {d.get('condition','')} → 終了 + 「{d.get('then_message','')}」")

    if added_count == 0:
        print(f"[info] 追加対象 0 件（既に trigger 化済み: {skipped_count} 件）")
    else:
        dump(intermediate_json, data)
        print(f"[done] {added_count} 件追加・{skipped_count} 件スキップ・保存完了: {intermediate_json}")


if __name__ == "__main__":
    main()
