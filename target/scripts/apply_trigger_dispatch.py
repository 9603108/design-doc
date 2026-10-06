#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_trigger_dispatch.py — 主要トリガーに dispatch[] を付与し、F00X 本体から呼出元依存の事前チェックを分離する（v23、v30 で外部設定化）

【目的】
trigger_groups[].triggers[].dispatch[] を手動定義（業務的判断が必要なため自動化困難）し、
対応する front_processes[].steps[] から呼出元依存の事前チェック部分を除去する。

【意味合い】
レビュー指摘（v23）「アクション = 動作種別、処理内容 = 呼出処理（事前チェック含む）」への対応。
F00X 本体には「呼ばれたら無条件で実行する純粋な処理」だけを残し、
呼出元固有のチェック（例: 閲覧モード判定）はトリガー側の dispatch[] に移動する。
これにより F00X が複数箇所から呼ばれた場合の矛盾を防ぐ。
v30 でハードコード辞書 TRIGGER_DISPATCH_MAP と find_trigger_for_event の keyword_map を
screen_config.json (trigger_dispatch) に外出しし、本スクリプトを画面非依存（汎用化）にした。

【接続情報】
- 入力: target/intermediate/{機能名}.json + target/intermediate/{機能名}_screen_config.json
- 出力: 同 JSON を上書き
- 仕様根拠: 15_中間JSONスキーマ.md「dispatch[]」セクション（v23 拡張）、SKILL.md「Phase 3-4 外部設定ファイル化」（v30）
"""
import json
import sys
from pathlib import Path

# v30: ハードコード辞書を screen_config.json に集約、_config_loader 経由で取得する
# 汎用化: sys.argv[1] / FEATURE_NAME 環境変数対応のため get_feature_name_from_argv 経由化
from _config_loader import get_feature_name_from_argv, get_trigger_dispatch

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
INTERMEDIATE_DIR = SKILL_DIR / "target" / "intermediate"

# v30: TRIGGER_DISPATCH_MAP の構造変更
#   旧: { event_code: { dispatch:[...], strip_step_keywords:[...] } }
#   新: screen_config.trigger_dispatch = { event_code: { trigger_match_keyword: str, dispatch:[...], strip_step_keywords:[...] } }
# trigger_match_keyword を JSON 側に同居させたのは、find_trigger_for_event の keyword_map も
# 同じ event_code を軸にした情報なので分散しないため。


def strip_step_chunks(step_description: str, keywords: list) -> str:
    """step.description から指定キーワードを含む先頭文を除去し、残りの「処理本体」だけを返す。

    例: "閲覧モードなら「閲覧モードでは確定できません」と表示して終了。入力チェック→..."
        → "入力チェック→..."
    """
    if not step_description:
        return step_description
    s = step_description
    # 最初の句点で分割し、キーワードを含む先頭文を除去
    sentences = s.split("。")
    filtered = []
    skipped = False
    for sent in sentences:
        if not skipped and any(kw in sent for kw in keywords):
            skipped = True
            continue
        filtered.append(sent)
    return "。".join(filtered).lstrip("。").lstrip()


def find_trigger_for_event(trigger_groups: list, event_code: str, dispatch_conf: dict) -> tuple:
    """event_code に対応する trigger を trigger_groups から探す（v30 外部設定化、v42 で auto/external 対応 + 検索フィールド拡張）。

    意味合い: 引数 dispatch_conf は screen_config.json の trigger_dispatch[event_code] 構造体で、
              trigger_match_keyword フィールドに「照合用キーワード」、trigger_match_kind フィールドに
              「対象とする trigger_group の kind」（既定: screen_operation、EV00 用は auto）を持つ。
              検索対象フィールドは location + action + description（業務語彙が入る任意フィールド）。
    """
    kw = (dispatch_conf or {}).get("trigger_match_keyword")
    if not kw:
        return None, None
    # v42: kind を dispatch_conf から取得（既定 screen_operation、EV00 用に auto も指定可能）
    target_kind = (dispatch_conf or {}).get("trigger_match_kind", "screen_operation")

    for g in trigger_groups:
        if g.get("kind") != target_kind:
            continue
        for t in g.get("triggers", []) or []:
            # v42: location + action + description を検索対象に拡張（業務語彙が入る任意フィールド）
            haystack = list(t.get("location") or []) + [t.get("action") or "", t.get("description") or ""]
            if any(isinstance(s, str) and kw in s for s in haystack):
                return g, t
    return None, None


def main():
    # 2026-10-05 汎用化: 機能名は第1引数（sys.argv[1]）か FEATURE_NAME 環境変数で決まる
    # 意味合い: 既定の機能名は持たない。どちらも無ければ get_feature_name_from_argv が止める
    #           （画面名を直書きしないことで、どの画面にも同じスクリプトを使える）
    # 接続情報: 解決した feature_name は下の intermediate_json と get_trigger_dispatch へ渡す
    feature_name = get_feature_name_from_argv()
    intermediate_json = INTERMEDIATE_DIR / f"{feature_name}.json"
    print(f"[INFO] target feature: {feature_name}")
    print(f"[INFO] intermediate JSON: {intermediate_json}")

    if not intermediate_json.exists():
        print(f"[ERROR] 中間JSONが見つかりません: {intermediate_json}", file=sys.stderr)
        sys.exit(1)

    with intermediate_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    trigger_groups = data.get("trigger_groups", []) or []
    front_processes = data.get("front_processes", []) or []
    fp_by_id = {fp["process_id"]: fp for fp in front_processes}

    # v30: trigger_dispatch_map を screen_config.json から取得（旧 TRIGGER_DISPATCH_MAP ハードコード廃止）
    # 汎用化: feature_name を明示的に伝搬
    trigger_dispatch_map = get_trigger_dispatch(feature_name)

    applied = 0
    for event_code, conf in trigger_dispatch_map.items():
        g, t = find_trigger_for_event(trigger_groups, event_code, conf)
        if t is None:
            print(f"  [WARN] {event_code} に対応する trigger が見つからない")
            continue
        # dispatch 付与
        t["dispatch"] = conf["dispatch"]
        applied += 1
        print(f"  [OK] {event_code} → group=[{g['kind_label']}] {g['category']} / trigger.location={t['location']}")
        for d in conf["dispatch"]:
            print(f"        - {d.get('condition')}: " + (
                f"→ [{d.get('then_call')}]" if d.get("then_call") else f"終了 + 「{d.get('then_message','')}」"
            ))

        # 対応する F00X の steps[] から事前チェック step を完全削除（v29: 空 description 残置の問題対応）
        keywords = conf.get("strip_step_keywords") or []
        if not keywords:
            continue
        for fid in [d.get("then_call") for d in conf["dispatch"] if d.get("then_call")]:
            fp = fp_by_id.get(fid)
            if not fp:
                continue
            # 1) steps から事前チェック step を完全削除
            old_steps = fp.get("steps", []) or []
            new_steps = []
            removed_count = 0
            for s in old_steps:
                desc = s.get("description", "") or ""
                # v29: 句点分解後の step は1文単位なので、キーワード一致したら step 全体を削除
                if any(kw in desc for kw in keywords):
                    removed_count += 1
                    continue
                new_steps.append(s)
            # step_no を再採番
            for i, s in enumerate(new_steps, 1):
                s["step_no"] = i
            if removed_count > 0:
                fp["steps"] = new_steps
                print(f"        ▸ {fid} の事前チェック step を {removed_count} 件削除（dispatch に移動済）")

            # 2) overview からも事前チェック先頭文を除去
            old_ov = fp.get("overview", "") or ""
            new_ov = strip_step_chunks(old_ov, keywords)
            if new_ov != old_ov:
                if new_ov:
                    fp["overview"] = new_ov
                else:
                    # overview 全体が事前チェック1文だった場合は、業務目的を別途設定
                    fp["overview"] = f"{fp.get('name', '')}を実行する"

    with intermediate_json.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print()
    print(f"[OK] {applied} 件の trigger に dispatch[] を付与")
    print(f"[OK] 保存先: {intermediate_json}")


if __name__ == "__main__":
    main()
