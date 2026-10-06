#!/usr/bin/env python3
# 目的: 中間JSON events[].trigger（例: 「サンプル画面のワークフローステッパー[5]「確定」ボタンをクリックする。」）を
#       解析し、各 event に location（階層配列）と action（動作短文）を追加する。
# 意味合い: 設計書の§5/§7のイベント表で「トリガー1列で長文」を「発生階層列 + アクション列」に分離し、可読性を上げる。
#           ユーザーフィードバック：「サンプル画面 / ワークフローステッパー[5] / 「確定」ボタン」の階層表示を実現
# 接続情報: 入力 = target/intermediate/{機能名}.json の events[].trigger
#           出力 = 同 JSON に events[].location と events[].action を追加（trigger は維持、後方互換）
#           次工程 = generate_docx.js が location/action を読んで §5/§7 表に出力
# 過去経緯: v7 で「明細タブ（[1]〜[3]の各タブ）」が3階層に誤分解 → v8 で丸括弧内の [N]/「の」マスクを追加

import json
import re
from pathlib import Path

# 2026-10-05 汎用化: 中間 JSON の絶対パス直書きをやめ、機能名から組み立てる。
# 目的: 特定案件の置き場・画面名に依存せず、どの機能の中間 JSON でも処理できるようにする。
# 接続情報: 機能名は第1引数か環境変数 FEATURE_NAME（どちらも無ければ _config_loader 側が SystemExit で止める）。
#           INTERMEDIATE は main() が読み込みと上書き保存の両方に使う。
from _config_loader import INTERMEDIATE_DIR, get_feature_name_from_argv

FEATURE_NAME = get_feature_name_from_argv()
INTERMEDIATE = INTERMEDIATE_DIR / f"{FEATURE_NAME}.json"

# 末尾の動作動詞句 → action ラベルへの対応表（先に登場するほど優先）
ACTION_PATTERNS = [
    (r'を?(ダブルクリック)する$', 'ダブルクリック'),
    (r'を?クリックする$',           'クリック'),
    (r'を?変更する$',               '変更'),
    (r'を?切替する$',               '切替'),
    (r'からフォーカスを外す$',       'フォーカス外し'),
    (r'を?チェックする$',           'チェック'),
    (r'を?(押す|押下)$',             '押下'),
    (r'を?スクロールする$',         'スクロール'),
    (r'(初期表示時|画面表示時)$',   '初期表示'),
    (r'を?入力する$',               '入力'),
    (r'を?選択する$',               '選択'),
    (r'を?タップする$',             'タップ'),
    (r'を?ドラッグする$',           'ドラッグ'),
    (r'を?ホバーする$',             'ホバー'),
    (r'を?(リロード|更新|再表示)する$', '更新'),
    (r'を?切り替える$',             '切替'),
    (r'を?(呼び出す|呼出す|呼出)$', '呼出'),
    (r'を?(送信|サブミット)する$',   '送信'),
    (r'を?キャンセルする$',         'キャンセル'),
    (r'を?(削除|消去)する$',         '削除'),
    (r'を?開く$',                   '開く'),
    (r'を?閉じる$',                 '閉じる'),
]


def _mask_in_parens(text: str) -> str:
    """丸括弧 `（...）` 内の `[N]` と `の` を一時マスクする。
    意味合い: 「明細タブ（[1]〜[3]の各タブ）」のような括弧内の修飾語が、
              [N]区切りや「の」区切りで誤分解されるのを防ぐ。括弧内は1階層として保持する設計。"""
    def repl(m):
        inner = m.group(0)
        inner = re.sub(r'\[(\d+)\]', r'__BR_\1__', inner)
        inner = inner.replace('の', '__NO__')
        return inner
    return re.sub(r'（[^）]*）', repl, text)


def _unmask(text: str) -> str:
    """_mask_in_parens で施したマスクを元に戻す"""
    text = re.sub(r'__BR_(\d+)__', r'[\1]', text)
    text = text.replace('__NO__', 'の')
    return text


# source_kind 分類用ルール
# 意味合い: 「画面トリガー」(業務担当者が直接操作) と「内部処理」(裏で呼ばれる) を機械的に判定
# 過去経緯: ユーザー要望「§5を5.1画面トリガー/5.2内部処理に分割」 → action と location からヒューリスティック分類
SCREEN_ACTIONS = {
    'クリック', '変更', 'ダブルクリック', '押下', 'フォーカス外し',
    'チェック', 'タップ', '初期表示', '切替', '入力', '選択',
    'ドラッグ', 'ホバー', '開く', '閉じる', 'キャンセル',
}
INTERNAL_ACTIONS = {
    '実行', '受信', '更新', '呼出', '送信', 'サブミット', '削除',
}
INTERNAL_LOC_HINTS = ['DevTools', 'コンソール', '通知', '自動', 'WebSocket', '裏側', '内部', 'ブラウザ']


def classify_source_kind(action: str, location: list, trigger: str = '', content: str = '') -> str:
    """action と location/trigger/content から source_kind ('screen' or 'internal') を自動判定する。
    意味合い: §5.1 画面トリガー（業務担当者の明示操作）と §5.2 内部処理（裏で呼ばれる関数）の分類根拠。
    過去経緯（v15）: location のみ判定では WebSocket受信系（trigger に「WebSocket経由で...通知を受信」と書かれる）が
                     誤って screen に落ちる事例多発 → trigger と content も判定対象に追加。
    フォールバック: 判定不能時は 'screen'（業務担当者が見るデフォルト）"""
    if action in SCREEN_ACTIONS:
        return 'screen'
    if action in INTERNAL_ACTIONS:
        return 'internal'
    # location + trigger + content の全文に対して internal ヒント検索（v15 で範囲拡大）
    full_text = ' '.join((location or []) + [trigger or '', content or ''])
    if any(h in full_text for h in INTERNAL_LOC_HINTS):
        return 'internal'
    return 'screen'


def parse_trigger(trigger: str) -> dict:
    """trigger 文字列を action（動作）と location（階層配列）に分解する。
    意味合い: 「サンプル画面のワークフローステッパー[5]「確定」ボタンをクリックする。」を
              ['サンプル画面', 'ワークフローステッパー[5]', '「確定」ボタン'] + 'クリック' に分解。
    フォールバック: 解析できなかった場合、全文を location[0] に入れ、action は空文字。"""
    if not trigger or not isinstance(trigger, str):
        return {"action": "", "location": [], "raw": trigger or ""}

    # 末尾の句点と空白を除去
    s = trigger.rstrip("。 ").strip()
    raw = trigger

    # 1. action 抽出（末尾の動詞句）
    action = ''
    rest = s
    for pat, label in ACTION_PATTERNS:
        m = re.search(pat, s)
        if m:
            action = label
            rest = s[:m.start()].rstrip()
            break

    # 2. 末尾の助詞「を」「で」を除去
    rest = re.sub(r'[をで]$', '', rest).strip()

    parts = []

    # 3. 画面名（「...画面(...)」）を最初に抽出
    m = re.match(r'^(.+?画面(?:（[^）]+）)?)(?:の|で)(.+)$', rest)
    if m:
        parts.append(m.group(1))
        rest = m.group(2)
    elif rest.endswith('画面') or '画面' in rest:
        # 「で」「の」が無くても画面名で終わる場合（初期表示時など）
        parts.append(rest)
        rest = ''

    # 4. 残り部分を [N] 区切りで分解（丸括弧内の [N] と「の」は事前マスクで保護）
    #    例: 「ワークフローステッパー[5]「確定」ボタン」
    #    → ['ワークフローステッパー[5]', '「確定」ボタン']
    #    丸括弧の例: 「明細タブ（[1]〜[3]の各タブ）」 → ['明細タブ（[1]〜[3]の各タブ）']（1階層）
    if rest:
        masked_rest = _mask_in_parens(rest)
        tokens = re.split(r'(\[\d+(?:/\[\d+\])*\])', masked_rest)
        merged = []
        i = 0
        while i < len(tokens):
            tok = tokens[i].strip()
            if not tok:
                i += 1
                continue
            if i + 1 < len(tokens) and tokens[i + 1].startswith('['):
                merged.append(tok + tokens[i + 1])
                i += 2
            else:
                merged.append(tok)
                i += 1

        # 5. 各 merged 要素を「の」で分割（括弧内の「の」はマスク済なので保護される）
        for chunk in merged:
            for sub in chunk.split('の'):
                sub_unmasked = _unmask(sub).strip()
                if sub_unmasked:
                    parts.append(sub_unmasked)

    # 末尾クリーンアップ（助詞「の」「を」「で」を末尾から除去、「サンプル画面の」→「サンプル画面」）
    parts = [re.sub(r'[をでの]+$', '', p).strip() for p in parts if p.strip()]

    return {
        "action": action,
        "location": parts,
        "raw": raw,
    }


def main():
    if not INTERMEDIATE.exists():
        raise FileNotFoundError(f"中間JSON が見つからない: {INTERMEDIATE}")
    with INTERMEDIATE.open(encoding="utf-8") as f:
        d = json.load(f)

    events = d.get("events", []) or []
    converted = 0
    failed = 0
    samples = []

    for e in events:
        trigger = e.get("trigger", "")
        result = parse_trigger(trigger)
        e["location"] = result["location"]
        e["action"] = result["action"]
        e["source_kind"] = classify_source_kind(
            result["action"], result["location"],
            trigger=e.get("trigger", ""), content=e.get("content", "")
        )

        if result["location"]:
            converted += 1
        else:
            failed += 1

        if len(samples) < 5:
            samples.append({
                "code": e.get("event_code", ""),
                "trigger": trigger[:60],
                "location": result["location"],
                "action": result["action"],
                "source_kind": e["source_kind"],
            })

    with INTERMEDIATE.open("w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "total_events": len(events),
        "converted": converted,
        "failed": failed,
        "samples": samples,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
