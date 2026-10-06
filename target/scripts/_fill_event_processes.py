#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_fill_event_processes.py — events[].no に対応する event_processes[] が存在しない場合の仮埋め生成（v51 汎用化）

【目的】
event_processes[] を持たない event（verify_intermediate.py が「events[].no に対応する
event_processes が存在しない（仮埋めが未生成）」WARN を出す対象）について、対応する
trigger → front_process の overview を借用し、業務担当者の語彙で 1 行 overview を生成して
event_processes[] に追記する。
無条件に流しても安全（既に event_processes がある event には足さない冪等な補修。
続けて2回流しても2回目は 0 件追加）。Phase 5.85（migrate_v49_event_unify.py）より前に流す。

【意味合い】
v50 時点では、ある1画面専用のスクリプトとして画面名がハードコードされていたが、
5 画面目以降で同じ問題が出ても各画面で別スクリプトを増殖させる構造になっていた。
v51 で sys.argv[1] / FEATURE_NAME 環境変数の両対応にし、画面非依存の汎用スクリプトに昇格。
画面別の overview フォールバック（例: 特定画面の EV3/EV4 のような固有メッセージ）は
screen_config.json の新セクション `event_process_overview_fallback{}` から読む形に分離した。

【接続情報】
- 入力: target/intermediate/{feature_name}.json
        target/intermediate/{feature_name}_screen_config.json（任意セクション event_process_overview_fallback）
- 出力: target/intermediate/{feature_name}.json （event_processes[] に追記）
- 参照: 15_中間JSONスキーマ.md「event_processes[]」セクション（v50 拡張）
        generate_docx.js sectionEventProcesses
- 呼出元: SKILL.md Phase 4.5（migrate_v17_to_v18.py などの後、Phase 5.85 より前に無条件で実行）
- 後続: migrate_v49_event_unify.py（Phase 5.85）が、ここで付けた event_code を event_ref へ変換する

【引数仕様】
- sys.argv[1]: feature_name（画面名）。省略時は環境変数 FEATURE_NAME、どちらも無ければ止まる
- 例: python3 _fill_event_processes.py <画面名>
      FEATURE_NAME=<画面名> python3 _fill_event_processes.py

【後方互換】
- 1画面専用だった旧版スクリプトは廃止済み。新規画面追加時は本スクリプトを使う
"""
import json
import shutil
import sys
from pathlib import Path
from datetime import datetime

# 2026-10-05 汎用化: 独自の resolve_feature_name()（末尾で特定画面名を既定値として返していた）を廃止。
# 目的: 機能名の解決と「未指定なら SystemExit」の止め方を _config_loader の1か所に寄せる。
# 接続情報: get_feature_name_from_argv は第1引数 → 環境変数 FEATURE_NAME の順に解決し、
#           INTERMEDIATE_DIR は target/intermediate/ を指す。どちらも main() が使う。
from _config_loader import INTERMEDIATE_DIR, get_feature_name_from_argv


def load(p: Path) -> dict:
    """JSON ファイルを読み込み辞書として返す。"""
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump(p: Path, data: dict) -> None:
    """辞書を JSON 形式で書き出す（UTF-8 / インデント2）。"""
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_overview_from_event(ev: dict, fr_overviews: list, fallback_map: dict) -> str:
    """1 件の event について業務担当者語彙の1行 overview を生成する。

    意味合い: 第一の overview ソース = 対応する front_process の overview。
              無い場合は screen_config.json の event_process_overview_fallback[event_no] を参照。
              それも無ければ event.content + location + action から組成する。

    引数:
        ev: events[] の1要素
        fr_overviews: 対応する front_processes[].overview のリスト
        fallback_map: screen_config.event_process_overview_fallback{event_no: overview_str}

    返り値:
        1 行の overview 文字列
    """
    ev_no = ev.get("no", "")

    # 第一: 対応する front_process の overview を借用
    if fr_overviews:
        return "／".join(fr_overviews)

    # 第二: screen_config の event_process_overview_fallback を参照
    # 画面別の固有メッセージ（例: 外部連携ボタンや確定操作のように、画面ごとに言い回しが決まっているもの）を吸収
    # no が整数（Phase 5.85 より前）だと JSON の文字列キーと一致せず TypeError にもならず素通りするので、
    # 文字列にして引く。
    if str(ev_no) in fallback_map:
        return fallback_map[str(ev_no)]

    # 第三: event.content + location + action から組成（汎用フォールバック）
    content = (ev.get("content") or "").strip()
    loc = ev.get("location", [])
    loc_tail = loc[-1] if isinstance(loc, list) and loc else ""
    action = (ev.get("action") or "").strip()

    if loc_tail and action and content:
        return f"{loc_tail} を {action} した時の処理：{content}"
    if loc_tail and action:
        return f"{loc_tail} を {action} した時の処理（処理詳細は要追加抽出）"
    if content:
        return f"{content}（{action}）" if action else content
    return f"{ev_no}（処理詳細は要追加抽出）"


def main():
    # 画面名を解決（v51 汎用化）。未指定なら _config_loader 側の SystemExit で止まる（既定の画面名は持たない）
    feature_name = get_feature_name_from_argv()
    intermediate_json = INTERMEDIATE_DIR / f"{feature_name}.json"
    # 2026-10-05 汎用化: 置き場の組み立てを中間 JSON と同じ INTERMEDIATE_DIR に揃えた（指す場所は従来と同じ）。
    # 意味合い: target/intermediate/ の場所を知るのを _config_loader の1か所にし、自前の ROOT 定数を廃止する。
    # 接続情報: この main() が下で読む任意の設定ファイル（event_process_overview_fallback の出どころ）。
    screen_config_json = INTERMEDIATE_DIR / f"{feature_name}_screen_config.json"

    print(f"[info] feature_name = {feature_name}")
    print(f"[info] intermediate = {intermediate_json}")
    print(f"[info] screen_config = {screen_config_json}")

    if not intermediate_json.exists():
        print(f"[ERROR] 中間 JSON が見つかりません: {intermediate_json}", file=sys.stderr)
        sys.exit(1)

    # バックアップ（9p の chmod/utime 制限を回避するため、内容のみコピー）
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = intermediate_json.with_suffix(f".json.bak_fillep_{ts}")
    shutil.copyfile(intermediate_json, backup)
    print(f"[backup] {backup}")

    data = load(intermediate_json)
    # screen_config は任意。存在しなくても event.content + location + action フォールバックで動く
    sc = {}
    if screen_config_json.exists():
        sc = load(screen_config_json)
    else:
        print(f"[warn] screen_config.json が無いため event_process_overview_fallback は使用不可")

    # 画面別の固有 overview フォールバック（例: ある画面の周辺イベントや EV3/EV4 のような固有メッセージ）
    # screen_config.json に event_process_overview_fallback{} セクションがあれば利用
    fallback_map = sc.get("event_process_overview_fallback", {}) or {}
    if fallback_map:
        print(f"[info] event_process_overview_fallback: {len(fallback_map)} 件のフォールバック overview を読込")

    # 既存 event_processes の識別子を取得（重複防止 = 冪等化の要）
    # 目的: 既に event_processes がある event へ二重に足さない。
    # 意味合い: 識別子の形はパイプラインの時点で2通りある。
    #   - Phase 5.85（migrate_v49_event_unify.py）より前: ep は event_code（"EV01" 形式）を持ち event_ref は無い
    #   - 通過後の最終形: ep は event_ref（= events[].no。"EV2" 形式）を持ち event_code は無い
    #   event_ref は events[].no と、event_code は events[].event_code / legacy_event_code と、
    #   同じ名前空間どうしでだけ比べる。混ぜると、最終形の no "EV10" と別の event の旧コード "EV10" が
    #   衝突し、event_processes が無い event を「既にある」と誤判定する。
    # 接続情報: 下の events ループの既存判定が参照する。
    existing_refs = set()
    existing_codes = set()
    for ep in data.get("event_processes", []):
        if ep.get("event_ref") is not None:
            existing_refs.add(ep["event_ref"])
        if ep.get("event_code"):
            existing_codes.add(ep["event_code"])

    # trigger_groups から「トリガー文 + 操作」→ calls を引く
    # 目的: event に対応する trigger の calls（フロント処理の ID）を得て、その overview を借用する。
    # 意味合い: triggers[] は event_ref も event_code も持たない。migrate_v17_to_v18.py が
    #           description に events[].trigger（無ければ content）を、action に events[].action を
    #           写しているので、この2つの組で突き合わせる。同じ組が複数あるときは出現順に消費する。
    # 接続情報: 下の events ループが event ごとに1件ずつ取り出す。
    trig_calls_by_key = {}
    for tg in data.get("trigger_groups", []):
        for t in tg.get("triggers", []):
            trig_calls_by_key.setdefault((t.get("description"), t.get("action")), []).append(t.get("calls", []))

    # front_processes を pid で索引化（calls 先の overview を借用するため）
    fp_by_pid = {}
    for fp in data.get("front_processes", []):
        pid = fp.get("process_id") or fp.get("no")
        if pid:
            fp_by_pid[pid] = fp

    # 業務担当者語彙の overview を組み立てる
    # 目的: 各 event について、対応する front_process の overview を主軸とし、
    #       無い場合は event.content + location + action から1行で「業務的に何が起こるか」を記述する
    new_eps = []
    added = []
    for ev in data.get("events", []):
        ev_no = ev.get("no")
        # event_code は Phase 5.85 より前の名前、legacy_event_code は通過後に退避された同じ値
        ev_code = ev.get("event_code") or ev.get("legacy_event_code")

        # 対応する trigger の calls を取得（スキップする event の分も消費して、同じ組の順序を保つ）
        queue = trig_calls_by_key.get((ev.get("trigger") or ev.get("content"), ev.get("action")))
        calls = queue.pop(0) if queue else []

        if not ev_no or ev_no in existing_refs or ev_code in existing_codes:
            continue

        # 初期表示イベント (EV00) は initialization セクションで扱う、スキップ
        if ev_code == "EV00":
            continue

        # 新規 ep に付ける識別子を決める
        # 目的: この時点のデータの形に合う識別子だけを付ける。
        # 意味合い: events[].event_code があれば Phase 5.85 より前なので event_code を付ける
        #           （migrate_v49_event_unify.py が後で event_ref へ変換する）。無ければ最終形なので
        #           event_ref に no（"EV2" 形式の文字列）を付ける。整数の no は event_ref に入れない
        #           （Phase 5.85 は event_code しか変換せず、整数の event_ref はどの event も指さなくなる）。
        # 接続情報: 下の ep 組み立てが使う。
        if ev.get("event_code"):
            ep_key = {"event_code": ev["event_code"]}
        elif isinstance(ev_no, str):
            ep_key = {"event_ref": ev_no}
        else:
            print(f"[warn] events[].no={ev_no} は event_code を持たないため仮埋めを作らない（Phase 1 の出力に event_code が必要）")
            continue

        # 第一の overview ソース: 対応する front_process の overview
        fr_overviews = []
        fr_refs = []
        for cid in calls:
            fp = fp_by_pid.get(cid)
            if fp:
                ov = fp.get("overview")
                # overview が str / list 両対応
                if isinstance(ov, str) and ov.strip():
                    fr_overviews.append(ov.strip())
                elif isinstance(ov, list) and ov:
                    fr_overviews.append("。".join(str(x) for x in ov if x))
                fr_refs.append(cid)

        # overview 組み立て（fr_overviews → fallback_map → 汎用 fallback の三段階）
        overview = build_overview_from_event(ev, fr_overviews, fallback_map)

        # event_processes[] に追加する仮埋め構造（v50 スキーマ）
        # details / db_operations_ref / parameter_refs は空（仮埋め）
        ep = {
            "pattern": "§9-G",  # 既定値
            "overview": overview,
            "details": [],
            "db_operations_ref": [],
            "parameter_refs": [],
            **ep_key,
        }
        # api_refs は front_process に api_refs があれば借用
        api_refs = []
        for cid in fr_refs:
            fp = fp_by_pid.get(cid, {})
            for ar in fp.get("api_refs", []) or []:
                if ar not in api_refs:
                    api_refs.append(ar)
        if api_refs:
            ep["api_refs"] = api_refs

        new_eps.append(ep)
        added.append((ev_no, fr_refs, overview[:80]))

    # 既存 event_processes に追加
    data.setdefault("event_processes", []).extend(new_eps)

    # 結果書き出し
    dump(intermediate_json, data)
    print(f"[done] {len(new_eps)} 件追加")
    for ev_no, refs, ov in added:
        # no は Phase 5.85 より前は整数なので、文字列にしてから桁を揃える（整数に 's' 書式は使えない）
        print(f"  {str(ev_no):>12s} | calls={refs} | {ov}")


if __name__ == "__main__":
    main()
