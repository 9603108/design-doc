#!/usr/bin/env python3
"""design-doc の単位方式(委譲の分担)を機械化する。

WHY: メインが手で組んでいた「単位ごとの依頼文」「検査後の後段コマンド列」「差し戻しの再依頼」を
毎回同じ形で出すため。分担の正本は prompts/units.json で、ここはそれを読むだけ(分担を二重に持たない)。

サブコマンド:
  prompts <機能名> --out <json> [--src <path>]...  各単位の Agent に渡す依頼文を書く
  after <機能名>                                    検査→後段を順に流し、ERROR で止めて差し戻しを JSON で出す
  retry <機能名> <単位> --issues <json>             差し戻しの文面を足した依頼文を出す

接続: prompts の出力は呼出元(メイン)が Agent の prompt に渡す。after は単位の書込みが
終わった後にメインが呼ぶ。retry は after が出した sendbacks を受けて、その単位だけ呼び直す。
標準ライブラリのみ。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# WHY: スクリプトの場所(target/scripts)から2つ上を設計書スキルの根とみなす。絶対パスの埋め込みを避ける。
SKILL = Path(__file__).resolve().parents[2]
INTER = SKILL / "target" / "intermediate"
UNITS_JSON = SKILL / "prompts" / "units.json"
COMMON = SKILL / "prompts" / "_共通.md"

# 検査が差し戻し先を示す行の形: "... → 差し戻し先 S1"(複数は , 、 / で並ぶ)
SENDBACK_RE = re.compile(r"→ 差し戻し先 (\S+)")
DIAGRAM_MARK = "(構成図)"

# 後段コマンド列。順序は SKILL.md「実行順序」と units_prompts.json の after に従う。
# {feat} は機能名に置き換える。構成図の行は DIAGRAM_MARK で展開する(ipo_flowchart 以外の id を回す)。
AFTER = [
    "python3 target/scripts/verify_partials.py --units",
    "FEATURE_NAME={feat} python3 target/scripts/merge_partials.py {feat} --compose-units",
    "python3 target/scripts/apply_react_draft.py {feat} --stage partial --backend-draft "
    "target/intermediate/{feat}_backend_draft.json",
    "python3 target/scripts/merge_partials.py {feat}",
    "python3 target/scripts/apply_react_draft.py {feat} --stage merge",
    "python3 target/scripts/convert_events_location_action.py {feat}",
    "python3 target/scripts/add_screen_areas.py {feat}",
    "python3 target/scripts/extract_source_files.py {feat}",
    "python3 target/scripts/migrate_v17_to_v18.py {feat}",
    "python3 target/scripts/normalize_screen_terminology.py {feat}",
    "python3 target/scripts/apply_trigger_dispatch.py {feat}",
    "python3 target/scripts/_fill_event_processes.py {feat}",
    "python3 target/scripts/_fix_derived_dispatch.py {feat}",
    "python3 target/scripts/add_diagrams_to_intermediate.py {feat}",
    "python3 target/scripts/build_ipo_flowchart_svg.py {feat}",
    DIAGRAM_MARK,
    "python3 target/scripts/apply_logical_name_substitution.py {feat}",
    "python3 target/scripts/build_call_graph.py {feat}",
    # WHY: logging.observation_points が無い案件ではスクリプト自身が「スキップ」で正常終了する。
    "python3 target/scripts/apply_logging_steps.py {feat}",
    "python3 target/scripts/apply_id_scheme.py {feat}",
    "python3 target/scripts/migrate_v49_event_unify.py {feat}",
    "python3 target/scripts/migrate_v48_to_v49.py {feat}",
    "python3 target/scripts/verify_intermediate.py {feat}",
    "python3 target/scripts/verify_call_graph.py {feat}",
    "INPUT_JSON=target/intermediate/{feat}.json DOCX_OUTPUT_PATH=target/output/詳細設計書_{feat}.docx "
    "NODE_PATH=$(npm root -g) node target/scripts/generate_docx.js",
    "python3 target/scripts/verify_docx_layout.py target/output/詳細設計書_{feat}.docx",
]


def load_units():
    # WHY: 分担表(units.json)が正本。ここに単位の一覧を書き写さない。
    with open(UNITS_JSON, encoding="utf-8") as f:
        return json.load(f)["units"]


def build_prompt(units, feat, u, srcs=(), sendbacks=()):
    """単位 u の依頼文を組む。sendbacks があれば差し戻しを足して既存の _unit_<u>.json を直させる。"""
    spec = units[u]
    owns = "、".join(spec["owns"])
    inter = str(INTER)
    skill = str(SKILL)
    lines = [
        f"design-doc スキルの委譲の単位 {u}({spec['title']})を行う。機能名(FEATURE_NAME): {feat}。",
        "",
        "【あなたの道具】cbm-serena(Serena と Bash を持ち、Read/Write は無い)。"
        "最初に mcp__codebase-memory-mcp__list_projects を1回呼ぶこと。"
        "ファイルの読み書きは Serena(mcp__serena__read_file / create_text_file / replace_content 等)で、絶対パスで行う。"
        "Bash は検査の python3 実行だけに使い、grep/cat/sed でソースを読まない。",
        "",
        "【パス(全て絶対パス)】",
        f"- スキルのフォルダ: {skill}(コマンドはここへ cd して流す)",
        f"- 共通の決まり: {COMMON}(最初に読む)",
        f"- あなたの手順書: {SKILL / spec['prompt']}(読んで従う)",
        f"- 分担表: {UNITS_JSON}",
        f"- 中間データの置き場: {inter}/",
        f"- 下書き: {inter}/{feat}_react_draft.json と {inter}/{feat}_backend_draft.json",
        f"- 設定: {inter}/{feat}_screen_config.json と {inter}/{feat}_project_config.json",
    ]
    if srcs:
        lines.append("- 入力のソース: " + " と ".join(str(Path(s).resolve()) for s in srcs))
    lines += [
        "",
        "【やること】",
        "- 読むのは _共通.md、手順書、手順書が挙げた入力だけ。SKILL.md は読まない。",
        f"- 書くのは {inter}/_unit_{u}.json の1ファイルだけ。他のファイル(partial・中間 JSON・他の単位の _unit_*.json・screen_config)は書かない。"
        f"書いてよい欄は units.json の {u} の owns だけ: {owns}",
        "- 前回の設計書や過去の partial の中身は参照しない(ソースと下書きから書く)。",
        f"- 終わったら `cd {skill} && python3 target/scripts/verify_partials.py --units --unit {u}` を流し、"
        f"行末が「→ 差し戻し先 {u}」の問題を直してから、3行以内で報告する(書いた行数・残った問題・判断できなかった所)。",
    ]
    if sendbacks:
        lines += [
            "",
            "【差し戻し】前回の検査が次を指した。既存の _unit_" + u + ".json を直す(新規に書き直さない)。",
        ]
        lines += [f"- {s}" for s in sendbacks]
    return "\n".join(lines) + "\n"


def parse_sendbacks(text):
    """検査の出力から「→ 差し戻し先 <単位>」の行を単位ごとに拾う。"""
    out = {}
    for line in text.splitlines():
        m = SENDBACK_RE.search(line)
        if not m:
            continue
        for unit in re.split(r"[,、/]", m.group(1)):
            if unit:
                out.setdefault(unit, []).append(line.strip())
    return out


def diagram_commands(feat):
    # WHY: 構成図の id は設定から得る(id を書き並べない)。ipo_flowchart は別経路なので除く。
    probe = (
        "import sys; sys.path.insert(0, 'target/scripts'); import _config_loader as c; "
        f"print('\\n'.join(c.get_all_diagram_ids('{feat}')))"
    )
    p = subprocess.run([sys.executable, "-c", probe], cwd=SKILL, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("get_all_diagram_ids failed: " + p.stderr[-400:])
    return diagram_steps(feat, p.stdout.split())


def diagram_steps(feat, ids):
    # WHY: SKILL.md Phase 5 の2つのループと同じ形。build は ipo_flowchart 以外(IPO は AFTER の build_ipo_flowchart_svg)、
    # extract_svg / svg_to_png は ipo_flowchart を含む全 id を target/diagrams/{feat}_{id}.html/.svg/.png で回す。
    cmds = [f"python3 target/scripts/build_generic_diagram_svg.py {feat} {did}" for did in ids if did != "ipo_flowchart"]
    for did in ids:
        base = f"target/diagrams/{feat}_{did}"
        cmds.append(f"python3 target/scripts/extract_svg.py {base}.html {base}.svg")
        cmds.append(f"python3 target/scripts/svg_to_png.py {base}.svg {base}.png 1400")
    return cmds


def run_after(feat):
    steps = []
    for tmpl in AFTER:
        if tmpl == DIAGRAM_MARK:
            steps += diagram_commands(feat)
        else:
            steps.append(tmpl.replace("{feat}", feat))
    for i, cmd in enumerate(steps, 1):
        print(f"[{i}/{len(steps)}] {cmd[:80]}", file=sys.stderr)
        p = subprocess.run(cmd, shell=True, cwd=SKILL, capture_output=True, text=True)
        out = p.stdout + p.stderr
        # WHY: 成功時にも「ERROR 0 件」と出力する検査があるため、出力の文字では判定せず終了コードだけで判定する。
        # 各検査は ERROR があれば exit 1 を返す(verify_call_graph.py・verify_docx_layout.py で確認)。
        if p.returncode != 0:
            result = {
                "stopped_at": cmd,
                "sendbacks": parse_sendbacks(out),
                "tail": out[-800:],
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps({"ok": True, "steps": len(steps)}, ensure_ascii=False))
    return 0


def selftest():
    s = parse_sendbacks("x ERROR foo → 差し戻し先 S1\nok\n  bar → 差し戻し先 H2,H3\n")
    assert sorted(s) == ["H2", "H3", "S1"], s
    assert s["S1"] == ["x ERROR foo → 差し戻し先 S1"], s
    assert parse_sendbacks("ERROR 差し戻しの無い行") == {}
    units = load_units()
    assert len(units) == 7, sorted(units)
    text = build_prompt(units, "テスト機能", "S1", sendbacks=["ERROR 欄 → 差し戻し先 S1"])
    assert "_unit_S1.json" in text and "ERROR 欄 → 差し戻し先 S1" in text, text
    assert "【差し戻し】" not in build_prompt(units, "テスト機能", "S1")
    check_step_args("テスト機能")


def check_step_args(feat):
    """各段のスクリプトが在り、`len(sys.argv) < N` で求める位置引数の数を満たすかを見る。

    ponytail: スクリプト本文の `len(sys.argv) < N` だけを見る。argparse の必須引数は見ない(要れば --help を流す)。
    """
    steps = [s for t in AFTER if t != DIAGRAM_MARK for s in [t.replace("{feat}", feat)]]
    steps += diagram_steps(feat, ["ipo_flowchart", "process_structure"])
    for cmd in steps:
        toks = cmd.split()
        i = next(k for k, t in enumerate(toks) if t.endswith((".py", ".js")))
        script = SKILL / toks[i]
        assert script.exists(), cmd
        argc = 1 + len([t for t in toks[i + 1:] if not t.startswith("-")])
        need = [int(n) for n in re.findall(r"len\(sys\.argv\)\s*<\s*(\d+)", script.read_text(encoding="utf-8"))]
        assert all(argc >= n for n in need), f"引数が足りない: {cmd} (要 {max(need)}、有 {argc})"


def main(argv=None):
    ap = argparse.ArgumentParser(description="design-doc の単位方式を機械化する")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p1 = sub.add_parser("prompts", help="各単位の依頼文を書く")
    p1.add_argument("feature")
    p1.add_argument("--out", required=True)
    p1.add_argument("--src", action="append", default=[], help="入力のソース(絶対パス)。複数可")
    p2 = sub.add_parser("after", help="検査と後段を順に流す")
    p2.add_argument("feature")
    p3 = sub.add_parser("retry", help="差し戻しを足した依頼文を書く")
    p3.add_argument("feature")
    p3.add_argument("unit")
    p3.add_argument("--issues", required=True, help="after の出力 JSON(sendbacks を読む)")
    a = ap.parse_args(argv)

    if a.selftest:
        selftest()
        print("selftest OK")
        return 0
    if a.cmd == "prompts":
        units = load_units()
        data = {"units": [
            {"unit": u, "model": units[u]["model"], "prompt": build_prompt(units, a.feature, u, a.src)}
            for u in units
        ]}
        Path(a.out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {len(data['units'])} units to {a.out}")
        return 0
    if a.cmd == "after":
        return run_after(a.feature)
    if a.cmd == "retry":
        units = load_units()
        with open(a.issues, encoding="utf-8") as f:
            issues = json.load(f)
        # WHY: after の出力(sendbacks の辞書)か、差し戻し文面の一覧の両方を受ける。
        lines = issues.get("sendbacks", {}).get(a.unit, []) if isinstance(issues, dict) else issues
        if not lines:
            print(f"差し戻しの文面が無い: {a.unit}", file=sys.stderr)
            return 1
        print(json.dumps({"unit": a.unit, "model": units[a.unit]["model"],
                          "prompt": build_prompt(units, a.feature, a.unit, sendbacks=lines)},
                         ensure_ascii=False, indent=1))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
