#!/usr/bin/env python3
"""design-doc の単位方式(委譲の分担)を機械化する。

WHY: メインが手で組んでいた「単位ごとの依頼文」「検査後の後段コマンド列」「差し戻しの再依頼」を
毎回同じ形で出すため。分担の正本は prompts/units.json で、ここはそれを読むだけ(分担を二重に持たない)。

サブコマンド:
  prompts <機能名> --out <json> [--src <path>]...  各単位の Agent に渡す依頼文を書く
  after <機能名>                                    検査→後段を順に流し、ERROR で止めて差し戻しを JSON で出す
  retry <機能名> <単位> --issues <json>             差し戻しの文面を足した依頼文を出す
  gate <機能名>                                     after・許可リスト照合・audit の合否を1つ出す(全部合格なら exit 0)
  save-run <機能名> <run名> [--log <path>]...  中間JSON・断片・検査ログ・docx を target/runs/<run名>/ に控える

接続: メインは Bash を使わないので、この script は tool-worker に流させる(メインが直に流さない)。
prompts の出力は呼出元(メイン)が Agent の prompt に渡し、担当は型 design-doc-unit で起動する
(model は units.json の値を Agent の model に明示する)。after は単位の書込みが終わった後に
tool-worker が流す。retry は after が出した sendbacks を受けて、その単位だけ呼び直す。
出力先(--out など)は target/ の配下に置く(/private/tmp の外)。
run の保存(save-run)も /private/tmp の片付けで消えないよう target/ の配下に置く。
標準ライブラリのみ。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
from datetime import datetime
from pathlib import Path

# WHY: スクリプトの場所(target/scripts)から2つ上を設計書スキルの根とみなす。絶対パスの埋め込みを避ける。
SKILL = Path(__file__).resolve().parents[2]
INTER = SKILL / "target" / "intermediate"
UNITS_JSON = SKILL / "prompts" / "units.json"
COMMON = SKILL / "prompts" / "_共通.md"

# 検査が差し戻し先を示す行の形: "... → 差し戻し先 S1"(複数は , 、 / で並ぶ)
SENDBACK_RE = re.compile(r"→ 差し戻し先 (\S+)")
UNIT_LIST_RE = re.compile(r"[A-Za-z0-9_,、/]+")  # 差し戻し先の直後の単位名の並び(説明の括弧は含めない)
DIAGRAM_MARK = "(構成図)"
# WHY: 欄を書く担当は専用の型 design-doc-unit で起動する(general-purpose は使わない)。起動できるのはメインだけ。出力の JSON に型を入れ、Agent の subagent_type へそのまま渡せるようにする。
SUBAGENT_TYPE = "design-doc-unit"


def _run_env(feat):
    """後段・検査の子プロセスに渡す環境。FEATURE_NAME をこの機能名に固定する(親の環境に別画面の名があっても引き継がない)。"""
    return {**os.environ, "FEATURE_NAME": feat}

# 後段コマンド列。順序は SKILL.md「実行順序」と units_prompts.json の after に従う。
# {feat} は機能名に置き換える。構成図の行は DIAGRAM_MARK で展開する(ipo_flowchart 以外の id を回す)。
AFTER = [
    "python3 target/scripts/verify_partials.py --units --feature {feat}",  # 断片は機能名に依らない固定名。機能名を必ず渡す(推測しない)
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
    # WHY: IDs(FR・BE)が確定した後に、共通ロジックの呼出元の名前を ID へ結ぶ(used_by_processes_ids)。
    "python3 target/scripts/fill_common_logic_callers.py {feat}",
    "python3 target/scripts/verify_vs_source.py {feat}",
    "python3 target/scripts/verify_intermediate.py {feat}",
    "python3 target/scripts/verify_call_graph.py {feat}",
    # WHY: 意味の照合の判定項目を作る。構造の検査(verify_call_graph)の後・docx の生成の前に置き、判定の差し戻しを docx の生成より前に出せるようにする
    "python3 target/scripts/build_audit_claims.py {feat}",
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
        "【あなたの道具】型 design-doc-unit(codeflow の find・view・edit・create と Bash と Write を持つ)。"
        "ファイルの読み書きは codeflow(mcp__codeflow__find / view / edit / create)で、絶対パスで行う。"
        "Bash は検査の python3 実行だけに使い、grep/cat/sed でソースを読まない。",
        "",
        "【パス(全て絶対パス)】",
        f"- スキルのフォルダ: {skill}(コマンドはここへ cd して流す)",
        f"- 共通の決まり: {COMMON}(最初に読む)",
        f"- あなたの手順書: {SKILL / spec['prompt']}(読んで従う)",
        f"- 分担表: {UNITS_JSON}",
        # WHY: H2 の手順書は 05_DB操作.md と 10_論理名変換.md を裸の名前で挙げ、依頼文に場所が無かったため H2 が見つけられなかった。
        #      スキル直下の実在パスを絶対パスで渡す(実在は selftest で確かめる)
        f"- スキル直下の文書(手順書が名前で挙げるもの): {SKILL / '05_DB操作.md'} と {SKILL / '10_論理名変換.md'}",
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
        # WHY: 断片 _unit_*.json は機能名に依らない固定名。別の画面の断片を取り違えないよう、書いた画面の機能名を断片に持たせる(無い・違うと検査と合成が ERROR)
        f"- 断片の最上位に `\"meta\": {{\"feature_name\": \"{feat}\"}}` を必ず書く(どの画面の断片かを示す印。設計書の欄ではないので owns には入らない)。機能名は上の {feat} をそのまま使う。",
        # WHY: run20 で H4 が「合成が付ける」を validations_frontend にも当てはめ、14行とも src を書かなかった(H4 手順書は全行に書けと決めており矛盾していた)。
        "- 【出典】下書きから合成が付ける行(events・event_processes・db_operations・api_spec・messages_backend・calculations)の src は書かない。validations_frontend の行は合成が付けないので、全行に、その検査・切替・絞り込みを実際に書いているソースの行範囲 {file,line_start,line_end} の配列を src に書く(手順書 H4 のとおり)。下書きに無い行(ソースを読んで足した行)も同様に、読んだ範囲を src に書く。",
        f"- 終わったら `cd {skill} && python3 target/scripts/verify_partials.py --units --unit {u} --feature {feat}` を流し、"
        f"行末が「→ 差し戻し先 {u}」の問題を直してから、3行以内で報告する(書いた行数・残った問題・判断できなかった所)。",
    ]
    # WHY: H1 の trigger と S2 の screen_item_name は S1 の画面項目の名前(item_name)を参照する。S1 と並行だと名前を推測して毎回20件前後ずれた(tana01・tana02)。
    # そこで units.json の after に S1 を持つ単位(H1・S2)は S1 を先に回し、書き終えた断片 _unit_S1.json の item_name をここで渡す。
    # S1 が無い・名前が無いのは順序の誤り(S1 より先に回した)なので黙って省かず止める。呼出元: main の prompts --phase 2・retry。
    if "S1" in (spec.get("after") or []):
        s1 = INTER / "_unit_S1.json"
        if not s1.exists():
            raise RuntimeError(f"{u} の依頼文には S1 の断片 {s1} が要る(S1 を先に回す)")
        d = json.loads(s1.read_text(encoding="utf-8"))
        names = list(dict.fromkeys(i["item_name"] for i in (d.get("screen_layout") or {}).get("items") or []
                                   if isinstance(i, dict) and i.get("item_name")))
        if not names:
            raise RuntimeError(f"{s1} の screen_layout.items に item_name が1件も無い(S1 が終わっていない)")
        lines += ["", "【画面項目名の一覧(S1 の断片の screen_layout.items の item_name。H1 は trigger と screen_item_name 、S2 は response_to_screen_mapping[].screen_item_name を、この名前をそのまま使う。推測で作らない)】"]
        lines += [f"- {x}" for x in names]
    if sendbacks:
        lines += [
            "",
            "【差し戻し】前回の検査が次を指した。既存の _unit_" + u + ".json を直す(新規に書き直さない)。",
        ]
        lines += [f"- {s}" for s in sendbacks]
    return "\n".join(lines) + "\n"


ERROR_LINE_RE = re.compile(r"\[ERROR\]|^\s*ERROR\b")
KEY_PATH_RE = re.compile(r"[a-z_]+(?:\[[^\]]*\])?(?:\.[a-z_]+(?:\[[^\]]*\])?)*")
# WHY: 合成後の中間 JSON の欄は、単位の owns と名前が違うものがある。合成・後段が写した元の欄へ読み替える。
# backend_processes[].overview は api_spec[].description の写し(POS会計レポートの中間 JSON で一致を確認)。
KEY_ALIAS = {
    "backend_processes.overview": "api_spec.description",
    "validations": "validations_frontend",
    "messages": "messages_backend",
    # front_processes[].steps[] は migrate_v17_to_v18.py が event_processes[].details から作る
    "front_processes.steps.description": "event_processes.details",
    # 画面操作の trigger(trigger_groups[].triggers[])は events[].trigger から migrate_v17_to_v18.py が作る。location の語彙違反は H1 の trigger の書き方
    # (検査の文中の「screen_layout の権威語彙」に引かれて S1 へ誤って振られていた)
    "trigger_groups.triggers": "events",
}
# WHY: 行が指す欄と直す欄が違う検査。「400 のエラーが validations に無い」は error_patterns(S2)はソースどおりで、
# 欠けているのは validations の行なので、validations を書く単位(H4)へ戻す。
MSG_TO_KEY = [(re.compile(r"validations に無い"), "validations_frontend"),
              # 画面への写しの欠けは S2 の欄(行の道筋が api_spec[0](action) の形で欄名を持たない古い文面も同じ単位へ)
              (re.compile(r"response_to_screen_mapping が空"), "api_spec.response_to_screen_mapping")]


def owner_of(line, units):
    """ERROR 行の対象の欄を units の owns と照らし、書いた単位を返す。引けなければ None。"""
    owns = [(o.replace("[]", ""), u) for u, spec in units.items() for o in spec["owns"]]
    keys = [k for rx, k in MSG_TO_KEY if rx.search(line)]
    keys += [re.sub(r"\[[^\]]*\]", "", m) for m in KEY_PATH_RE.findall(line)]
    for key in keys:
        for src, dst in KEY_ALIAS.items():
            if key == src or key.startswith(src + "."):
                key = dst + key[len(src):]
        hits = [(len(o), u) for o, u in owns if key == o or key.startswith(o + ".")]
        if hits:
            return max(hits)[1]
    return None


def parse_sendbacks(text, units=None):
    """検査の出力から「→ 差し戻し先 <単位>」の行を単位ごとに拾う。

    units を渡すと、差し戻し先の無い ERROR 行も owner_of で単位を引き、引けなければ "unknown" に入れる。
    units を渡したとき、units 表に無い名前(S0 など並行の単位の外・誤記)は retry できないので "unknown" に入れる。
    """
    out = {}
    for line in text.splitlines():
        m = SENDBACK_RE.search(line)
        if m:
            # WHY(2026-10-14 run12): 「→ 差し戻し先 S1（出典の行範囲を…に直す）」のように単位名の直後に説明の括弧が続く行は、
            # \S+ が括弧の中まで取って単位名が「S1（出典…」になり unknown に落ちた。先頭の単位名の並びだけを取る。
            # 単位名の並びで始まらない(「担当外の側」など)ときは従来どおり丸ごと取って unknown に残す。
            lead = UNIT_LIST_RE.match(m.group(1))
            targets = [u for u in re.split(r"[,、/]", lead.group(0) if lead else m.group(1)) if u]
            if units is not None:
                targets = [u if u in units else "unknown" for u in targets]
                targets = list(dict.fromkeys(targets))
        elif units is not None and ERROR_LINE_RE.search(line):
            targets = [owner_of(line, units) or "unknown"]
        else:
            continue
        for unit in targets:
            out.setdefault(unit, []).append(line.strip())
    return out


# 指摘らしい行: [種別] で始まる(INFO・OK を除く)・ERROR・Traceback・例外名・→ を含む
FINDING_LINE_RE = re.compile(r"^\s*\[(?!INFO\]|OK\])[^\]]+\]|ERROR|Traceback|Error\b|→")
INFO_LINE_RE = re.compile(r"^\s*\[(?:INFO|OK)\]")
FALLBACK_TAIL = 40  # 指摘らしい行が1つも取れないときに残す、出力の末尾の行数


def fallback_unknown(out, cmd):
    """終了コードが 0 でないのに parse_sendbacks が何も拾えなかったとき、unknown に残す行を返す。
    WHY(2026-10-15 run15): 以前は出力の最終行だけを残していた。検査の最終行は「[INFO] …ERROR 0 件」の集計行になりがちで、
    止めた本当の指摘([ERROR] の印も「→ 差し戻し先」も無い行)が消え、unknown の中身から原因が読めなかった。
    1) [INFO]・[OK] の行を除いた、指摘らしい行(FINDING_LINE_RE)をすべて残す。2) 無ければ出力の末尾 FALLBACK_TAIL 行(全出力)を残す。
    3) 出力が空なら起動したコマンド。呼出元: run_after。"""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    findings = [ln for ln in lines if not INFO_LINE_RE.match(ln) and FINDING_LINE_RE.search(ln)]
    return findings or lines[-FALLBACK_TAIL:] or [cmd]


def diagram_commands(feat):
    # WHY: 構成図の id は設定から得る(id を書き並べない)。ipo_flowchart は別経路なので除く。
    probe = (
        "import sys; sys.path.insert(0, 'target/scripts'); import _config_loader as c; "
        f"print('\\n'.join(c.get_all_diagram_ids('{feat}')))"
    )
    p = subprocess.run([sys.executable, "-c", probe], cwd=SKILL, capture_output=True, text=True, env=_run_env(feat))
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
        p = subprocess.run(cmd, shell=True, cwd=SKILL, capture_output=True, text=True, env=_run_env(feat))
        out = p.stdout + p.stderr
        # WHY: 成功時にも「ERROR 0 件」と出力する検査があるため、出力の文字では判定せず終了コードだけで判定する。
        # 各検査は ERROR があれば exit 1 を返す(verify_call_graph.py・verify_docx_layout.py で確認)。
        if p.returncode != 0:
            # WHY: 差し戻し先を拾えないまま止まると、メインが次に何をするか決まらない。最低でも unknown に残す。
            # 残すのは最終行ではなく、印の無い指摘らしい行(fallback_unknown)。最終行は「ERROR 0 件」の INFO 行になりがちで原因が消える(2026-10-15 run15)
            sendbacks = parse_sendbacks(out, load_units()) or {"unknown": fallback_unknown(out, cmd)}
            result = {
                "stopped_at": cmd,
                "sendbacks": sendbacks,
                "tail": out[-800:],
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps({"ok": True, "steps": len(steps)}, ensure_ascii=False))
    return 0


def run_gate(feat):
    """納品の合否を1つにまとめる関門。

    WHY: after(ERROR)・WARN の許可リスト照合・audit の差し戻しが別々に出ると、どれが残っているかをメインが読み分けることになる。
    全部そろったときだけ exit 0 にして、合格の条件(ERROR 0・説明のない WARN 0・audit の差し戻し 0・audit の unknown 0)を1か所に置く。
    同じ WARN は一覧と再出力に2回出るが、1回だけ数える。
    出力は {ok, after_ok, warn_unallowed, audit_ng, audit_unknown} に、不合格の中身(after・warn_skipped・audit_reason)と、
    どの WARN にも当たらなかった許可リストの登録(allow_unmatched。合否には影響しない)を足した JSON を1回。
    呼出元: main の gate。再利用: run_after・audit_sendbacks・load_units(subprocess の呼び方は run_after に任せ、複製しない)。"""
    import contextlib
    import io
    import _warn_allow
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        after_rc = run_after(feat)
    result = {"after_ok": after_rc == 0}
    if after_rc != 0:
        # run_after が出した JSON(stopped_at・sendbacks)をそのまま載せる。JSON でなければ出力の文字を残す
        try:
            result["after"] = json.loads(buf.getvalue())
        except ValueError:
            result["after"] = buf.getvalue()[-800:]
    # WHY: after が合格している(ERROR 0)ので、--warn-allow-strict の非0は「許可リストに無い WARN」と読める。
    # ERROR で止まっているときは WARN の判定が意味を持たないので評価しない。
    unallowed_warn = []
    all_warns = []  # 2 本の検査が出した WARN 行(許可に当たったものも含む)。「当たらない登録」の洗い出しに使う
    allow = None
    if after_rc == 0:
        # WHY: 理由の空な登録など不正な許可リストは load_allow が ValueError にする。例外で落とさず、不合格として
        # 理由を出し(JSON 出力と return 1 に必ず届かせる)、許可リストの裏付けが無いので 2 本の検査は流さない。
        data = _warn_allow.load_data(INTER / f"{feat}.json")  # owner・step_kind の登録の引き当て用
        try:
            allow = _warn_allow.load_allow(INTER / f"{feat}_warn_allow.json")
        except ValueError as e:
            allow = None
            unallowed_warn.append(f"許可リストが不正: {e}")
        for script in (("verify_vs_source.py", "verify_intermediate.py") if allow is not None else ()):
            cmd = f"python3 target/scripts/{script} {feat} --warn-allow-strict"
            p = subprocess.run(cmd, shell=True, cwd=SKILL, capture_output=True, text=True, env=_run_env(feat))
            out = p.stdout + p.stderr
            all_warns += _warn_allow.split_warns(out.splitlines())
            if p.returncode != 0:
                lines = _warn_allow.unallowed(_warn_allow.split_warns(out.splitlines()), allow, data) or fallback_unknown(out, cmd)
                # 一覧と再出力の両方に同じ [WARN] 行が入るので、順序を保って重複を除く
                unallowed_warn += [f"{script}: {ln}" for ln in dict.fromkeys(ln.strip() for ln in lines)]
    else:
        result["warn_skipped"] = True
    result["warn_unallowed"] = unallowed_warn
    # WHY: 回し直しで外れた登録(旧形式の key に番号・名前が入っているなど)を、合否とは別に見える形で出す。落とさない(直って要らなくなった登録もある)
    if allow:
        miss = _warn_allow.unmatched(list(dict.fromkeys(ln.strip() for ln in all_warns)), allow, data)
        if miss:
            result["allow_unmatched"] = [_warn_allow.describe_entry(e) for e in miss]
    # audit: 結果ファイルが無い・壊れているときは握りつぶさず不合格(audit 未実施)
    results_p = INTER / f"{feat}_audit_results.json"
    audit_ng = 0
    audit_unknown = 0
    try:
        rows = json.loads(results_p.read_text(encoding="utf-8"))
        audit_ng = len(audit_sendbacks(rows, load_units()))
        # WHY: unknown は「ソースと合っているか確かめられていない」で、再判定(sonnet)後も残る。差し戻し(ng)には入らないため、
        # 数えないと unknown が残ったまま合格する(run20 は 22 件のまま通りうる)。1件でもあれば不合格にし、件数を出す。
        audit_unknown = sum(1 for r in rows if isinstance(r, dict) and r.get("verdict") == "unknown")
    except (OSError, ValueError) as e:
        audit_ng = 1
        result["audit_reason"] = f"audit 未実施(結果を読めない: {results_p.name}: {e})"
    result["audit_ng"] = audit_ng
    result["audit_unknown"] = audit_unknown
    ok = result["after_ok"] and not unallowed_warn and audit_ng == 0 and audit_unknown == 0
    print(json.dumps({"ok": ok, **result}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def selftest():
    s = parse_sendbacks("x ERROR foo → 差し戻し先 S1\nok\n  bar → 差し戻し先 H2,H3\n")
    assert sorted(s) == ["H2", "H3", "S1"], s
    assert s["S1"] == ["x ERROR foo → 差し戻し先 S1"], s
    assert parse_sendbacks("ERROR 差し戻しの無い行") == {}
    units = load_units()
    assert len(units) == 7, sorted(units)
    # 差し戻し先の無い合成後の ERROR(2026-10-13 POS会計レポートの verify_intermediate)
    s = parse_sendbacks(
        "=== 検証結果: ERROR 3 件 ===\n--- ERROR（docx生成前に要修正） ---\n"
        "  [ERROR] api_spec[a].error_patterns: 400 のエラー MSE00001 が validations に無い（validations に1件書き）\n"
        "  [ERROR] v55.1 §10 補遺 概要本文に物理名残存: backend_processes[BE1].overview 検出=['UPPER:POS']\n"
        "  [ERROR] db_operations[DB1].dataset: 列「a・b」が括り表記\n"
        "  [ERROR] api_spec[a].response_items[k].display_name が空\n"
        "  [ERROR] 何の欄か分からない\n"
        "  [WARN] backend_processes[BE1].overview 長い\n", units)
    assert {u: len(v) for u, v in s.items()} == {"H4": 1, "S2": 1, "H2": 1, "H3": 1, "unknown": 1}, s
    # 2026-10-13: 画面への写しの欠けは S2。画面操作 trigger の location の語彙違反は H1(文中の「screen_layout の権威語彙」で S1 に誤振りしない)
    s = parse_sendbacks(
        "  [ERROR] api_spec[0].response_to_screen_mapping(list_pos_shops): response_items があるのに response_to_screen_mapping が空（x）\n"
        "  [ERROR] api_spec[0](list_pos_shops): response_items があるのに response_to_screen_mapping が空（古い文面）\n"
        "  [ERROR] events[EV7].location[2]='空欄項目も表示チェックボックス' が screen_layout の権威語彙にない\n"
        "  [ERROR] trigger_groups[kind=screen_operation].triggers[no=TR6].location[2]='空欄項目も表示チェックボックス' が screen_layout の権威語彙にない\n"
        "  [ERROR] screen_layout.items[2].example: 何か\n", units)
    assert {u: len(v) for u, v in s.items()} == {"S2": 2, "H1": 2, "S1": 1}, s
    assert parse_sendbacks("ERROR foo → 差し戻し先 S1", units) == {"S1": ["ERROR foo → 差し戻し先 S1"]}
    # G34: H1・S2(units.json の after に S1)の依頼文には S1 の断片 _unit_S1.json の item_name が入り、S1 の依頼文には入らない。
    # 断片が無いと RuntimeError(S1 より先に回した誤りを黙って省かない)
    import tempfile
    global INTER
    with tempfile.TemporaryDirectory() as td:
        Path(td, "_unit_S1.json").write_text(json.dumps({
            "screen_layout": {"items": [{"item_name": "店舗の選択欄"}, {"item_name": "総売上の表"}, {"item_name": "店舗の選択欄"}]}}, ensure_ascii=False), encoding="utf-8")
        old, INTER = INTER, Path(td)
        try:
            p = build_prompt(units, "テスト機能", "S2")
            ph = build_prompt(units, "テスト機能", "H1")
            ps1 = build_prompt(units, "テスト機能", "S1")
        finally:
            INTER = old
    for t in (p, ph):
        assert "- 店舗の選択欄\n" in t and "- 総売上の表\n" in t and t.count("- 店舗の選択欄\n") == 1, t
    assert "画面項目名の一覧" not in ps1, "S1 の依頼文に名前の一覧は入らない"
    with tempfile.TemporaryDirectory() as td:
        old, INTER = INTER, Path(td)
        try:
            try:
                build_prompt(units, "テスト機能", "H1")
                raise AssertionError("_unit_S1.json が無いのに H1 の依頼文を組めた")
            except RuntimeError:
                pass
        finally:
            INTER = old
    # units 表に無い差し戻し先(S0 など)は unknown に入れる。units を渡さなければ従来どおり名前のまま
    assert parse_sendbacks("[WARN] x → 差し戻し先 S0", units) == {"unknown": ["[WARN] x → 差し戻し先 S0"]}
    assert sorted(parse_sendbacks("x → 差し戻し先 S0,S1,H9", units)) == ["S1", "unknown"]
    assert list(parse_sendbacks("x → 差し戻し先 S0")) == ["S0"]
    # 2026-10-14 run12: 単位名の直後に説明の括弧が続く行は、括弧を単位名に含めない(S1（…）→ S1)。括弧の無い複数もそのまま
    s = parse_sendbacks("  [ERROR] initialization.items[62](注記): 初期値に項目 63 の初期値が含まれる → 差し戻し先 S1（出典の行範囲を、その項目の文言だけに直す）", units)
    assert list(s) == ["S1"], s
    assert sorted(parse_sendbacks("x → 差し戻し先 H2,H3（説明）", units)) == ["H2", "H3"]
    # 単位名で始まらない差し戻し先(担当外の側)は従来どおり丸ごと unknown に残す
    assert list(parse_sendbacks("[重複] x: 同じ欄を書いた → 差し戻し先 担当外の側", units)) == ["unknown"]
    # 印も「→ 差し戻し先」も無い指摘(2026-10-15 run15 の [列挙] の旧書式)は parse_sendbacks が拾えない。unknown には最終行(INFO)でなく指摘の行を残す
    old_enum = ("[INFO] 画面項目の照合: WARN 0 件\n"
                "[列挙] api_spec[a].description: 選択肢「レジ別・店舗別」のうち「レジ別」だけを書いている → 全部並べるか既定値を添える(差し戻し先 S2)\n"
                "[INFO] 例示値の行数・transform の書式の照合: ERROR 0 件\n")
    assert parse_sendbacks(old_enum, units) == {}, "印の無い行は parse_sendbacks では拾えない前提"
    fb = fallback_unknown(old_enum, "cmd")
    assert len(fb) == 1 and fb[0].startswith("[列挙]"), fb
    assert fallback_unknown("[INFO] ok 1\n[INFO] ERROR 0 件\n", "cmd") == ["[INFO] ok 1", "[INFO] ERROR 0 件"], "指摘らしい行が無ければ全出力(末尾)"
    assert fallback_unknown("", "cmd") == ["cmd"]
    assert fallback_unknown("x\nTraceback (most recent call last):\n  File a\nValueError: bad\n", "cmd")[0].startswith("Traceback")
    # 共通の形([ERROR] と「→ 差し戻し先 X」)なら拾える
    assert list(parse_sendbacks("[ERROR] [列挙] a: b → 差し戻し先 S2\n[INFO] 照合: ERROR 1 件", units)) == ["S2"]
    # retry は units 表に無い単位で KeyError にせず、分かる文言で終了コード 2
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["retry", "テスト機能", "S0", "--issues", "/dev/null"]) == 2
    assert "units 表に無い" in err.getvalue(), err.getvalue()
    text = build_prompt(units, "テスト機能", "S1", sendbacks=["ERROR 欄 → 差し戻し先 S1"])
    assert "_unit_S1.json" in text and "ERROR 欄 → 差し戻し先 S1" in text, text
    assert "【差し戻し】" not in build_prompt(units, "テスト機能", "S1")
    assert "【出典】" in build_prompt(units, "テスト機能", "S1"), "A3: 指示文に出典の1行"
    # 機能名の取り違えを防ぐ: 依頼文は断片の meta.feature_name を書かせ、検査のコマンドに機能名(--feature)を渡す。後段の子プロセスの FEATURE_NAME はこの機能名に固定する
    pf = build_prompt(units, "テスト機能", "H2")
    assert '"meta": {"feature_name": "テスト機能"}' in pf, "依頼文に meta.feature_name の指示が無い"
    assert "verify_partials.py --units --unit H2 --feature テスト機能" in pf, "依頼文の検査のコマンドに機能名が無い"
    assert AFTER[0] == "python3 target/scripts/verify_partials.py --units --feature {feat}", AFTER[0]
    old_env = os.environ.get("FEATURE_NAME")
    os.environ["FEATURE_NAME"] = "別画面"
    try:
        assert _run_env("テスト機能")["FEATURE_NAME"] == "テスト機能", "子プロセスに親の別画面の機能名を引き継いだ"
    finally:
        if old_env is None:
            os.environ.pop("FEATURE_NAME", None)
        else:
            os.environ["FEATURE_NAME"] = old_env
    # A3: .src の欄は _merge で和集合(同じ要素は1つ)。.src でない list は従来どおり上書き
    from _units import _merge
    a, b, c = {"file": "a", "line_start": 1, "line_end": 2}, {"file": "b", "line_start": 3, "line_end": 4}, {"file": "a", "line_start": 1, "line_end": 2}
    assert _merge({"src": [a]}, {"src": [b, c], "t": [1]}, "x") == {"src": [a, b], "t": [1]}
    assert _merge({"t": [1]}, {"t": [2]}, "x") == {"t": [2]}
    # 手順書が名前で挙げる文書(05・10)は、依頼文に絶対パスで出る・実在する(パスのずれの再発を止める)
    for doc in ("05_DB操作.md", "10_論理名変換.md"):
        assert (SKILL / doc).exists(), f"スキル直下に無い: {doc}"
        assert str(SKILL / doc) in build_prompt(units, "テスト機能", "H2"), f"依頼文に {doc} の絶対パスが無い"
    # audit: 各依頼の先頭行に part 番号(1 始まり)・書き先・判定項目の範囲。照合は、欠け・他の part の項目・重複・壊れた JSON を part の番号で返す
    cl = [{"id": f"AC{i:03d}", "kind": "remarks", "owner": "S1", "subject": f"主語{i}", "text": f"文{i}", "src_excerpt": "x", "src_ref": None}
          for i in range(1, 31)]
    with tempfile.TemporaryDirectory() as td:
        old, INTER = INTER, Path(td)
        try:
            ps = build_audit_prompts("機能", cl)
            head = ps[1]["prompt"].splitlines()[0]
            assert [p["part"] for p in ps] == [1, 2] and head.startswith("あなたは part 2") and "_audit_part_2.json" in head and "AC026〜AC030" in head, head
            assert "主語26" in ps[1]["prompt"] and ps[0]["claims"][0]["id"] == "AC001", "判定項目に主語・claims の写し"
            # G19: 上限はファイルごと。上限内のファイルは全文(見出し+行番号)、超えるファイルだけ項目ごとの抜粋の節。1項目に全文と抜粋が混ざる。同じ全文は1 part に1回だけ
            # 抜粋にすべきファイルの断片(src_segs)が無い項目だけは従来の text の抜粋
            small_f, big_f = Path(td) / "small.py", Path(td) / "big.py"
            small_f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
            big_f.write_text("x = 0\n" * (AUDIT_SRC_MAX_BYTES // 6 + 10), encoding="utf-8")  # 上限を超える大きさ
            ref = {"file": str(small_f), "line_start": 2, "line_end": 2}
            segs = [{"file": str(big_f), "line_start": 5000, "line_end": 5001, "label": None, "lines": ["main_a", "main_b"]},
                    {"file": str(big_f), "line_start": 9000, "line_end": 9001, "label": "fooApi", "lines": ["def fooApi():", "  return 1"]},
                    {"file": str(small_f), "line_start": 1, "line_end": 1, "label": None, "lines": ["SMALL_SEG"]}]
            gc = [{"id": cid, "kind": "remarks", "owner": "S1", "subject": f"全文{cid}", "text": f"文{cid}", "src_excerpt": f"抜粋{cid}", "src_excerpt_wide": f"広い抜粋{cid}",
                   "src_ref": ref, "src_files": files, "src_segs": sg}
                  for cid, files, sg in (("AC901", [str(small_f)], []), ("AC902", [str(small_f)], []), ("AC903", [str(big_f)], segs[:2]),
                                         ("AC904", [str(small_f), str(big_f)], segs), ("AC905", [str(big_f)], []))]
            gp = build_audit_prompts("機能", gc)[0]["prompt"]
            gitems = json.loads(gp[gp.index("- 判定項目:\n") + len("- 判定項目:\n"):])
            assert f"==== ソース: {small_f}(全 3 行)====" in gp and "   2| b = 2" in gp, "全文の節と見出しと行番号"
            assert gp.count(f"==== ソース: {small_f}") == 1 and f"==== ソース: {big_f}" not in gp, "同じ全文は1 part に1回、上限超えの全文は載せない"
            assert all("text" not in i["src_excerpt"] for i in gitems[:4]) and gitems[0]["src_files"] == [str(small_f)], "節で渡した項目は text を持たない"
            # 上限超えの api.ts 型のファイルだけ抜粋の節。その項目の小さなファイルは全文のまま(混在)
            assert gp.count(f"==== 抜粋: AC903 {big_f}") == 1 and gp.count(f"==== 抜粋: AC904 {big_f}") == 1 and f"全 {AUDIT_SRC_MAX_BYTES // 6 + 10} 行のうち" in gp, "抜粋の節は項目ごと"
            assert "   3| def fooApi():" in gp and "---- 断片: fooApi(ファイルの 9000-9001 行)----" in gp, "抜粋の節の行番号は節の先頭を1とする通し番号(見出し行は数えない)"
            assert "SMALL_SEG" not in gp and f"==== 抜粋: AC904 {small_f}" not in gp, "上限内のファイルの断片は抜粋の節にしない"
            assert gitems[2]["src_files"] == [] and gitems[2]["src_excerpt_files"] == [str(big_f)], "抜粋の節だけの項目"
            assert gitems[3]["src_files"] == [str(small_f)] and gitems[3]["src_excerpt_files"] == [str(big_f)], "全文と抜粋が混ざる項目"
            assert "src_excerpt_files" not in gitems[0], "全文だけの項目は src_excerpt_files を持たない"
            assert gitems[4]["src_excerpt"].get("text") == "抜粋AC905" and "src_files" not in gitems[4], "抜粋の断片が無い項目は従来の text の抜粋"
            assert "evidence_file" in gp and "の両方が根拠になる" in gp and "載せたソースの外を根拠にしない" in gp, "根拠は全文の節と抜粋の節の両方・外は根拠にしない・evidence_file の書き分け"
            # 再判定の依頼文も同じ作り(節・項目の形・決まり)。legacy の項目だけ予算を広げた抜粋の text
            rp = build_audit_retry_prompt("機能", gc, Path(td) / "r.json")
            ritems = json.loads(rp[rp.index("- 判定項目:\n") + len("- 判定項目:\n"):])
            assert f"==== ソース: {small_f}(全 3 行)====" in rp and f"==== 抜粋: AC904 {big_f}" in rp and "   3| def fooApi():" in rp, "再判定も全文の節と抜粋の節"
            assert ritems[3]["src_files"] == [str(small_f)] and ritems[3]["src_excerpt_files"] == [str(big_f)], "再判定も全文と抜粋が混ざる"
            assert ritems[4]["src_excerpt"].get("text") == "広い抜粋AC905", "再判定の legacy は広い抜粋"
            assert all(r in rp for r in _audit_src_rules()), "再判定も audit と同じ根拠・evidence_line の決まり"
            assert "==== ソース:" not in ps[0]["prompt"] and "==== 抜粋:" not in ps[0]["prompt"] and "- ソース(全文の節と抜粋の節" not in ps[0]["prompt"], "src_files 無しの claims は全文の節も抜粋の節も出さない"

            def write(n, ids, extra=None):
                rows = [{"id": i, "verdict": "ok", "evidence_line": 1, "reason": "r"} for i in ids] + (extra or [])
                (Path(td) / f"機能_audit_part_{n}.json").write_text(json.dumps(rows), encoding="utf-8")
            write(1, ps[0]["ids"])
            v, pr = audit_part_problems("機能", ps)
            assert sorted(pr) == [2] and "書かれていない" in pr[2][0] and len(v) == 25, (pr, len(v))
            write(2, ps[0]["ids"][:5])  # 呼出元が part 1 の依頼を2体に渡した形(part 2 の名前に part 1 の項目を書く)
            v, pr = audit_part_problems("機能", ps)
            assert sorted(pr) == [2] and any("項目でない id" in m for m in pr[2]) and any("欠けている" in m for m in pr[2]), pr
            write(2, ps[1]["ids"], extra=[{"id": "AC030", "verdict": "ok"}])  # 重複
            assert "重複" in " ".join(audit_part_problems("機能", ps)[1][2])
            write(2, ps[1]["ids"])
            v, pr = audit_part_problems("機能", ps)
            assert pr == {} and len(v) == 30, pr
        finally:
            INTER = old
    # save_run: 一時ディレクトリに置いた成果物を控え、他機能・既存の run・不正な run 名・無いログを区別して止める
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "i").mkdir()
        (t / "o").mkdir()
        for name in ("テスト機能.json", "テスト機能_audit_results.json", "_unit_S1.json", "_partial_backend.json", "他機能.json", "テスト機能長.json"):
            (t / "i" / name).write_text("{}", encoding="utf-8")
        (t / "o" / "詳細設計書_テスト機能.docx").write_text("d", encoding="utf-8")
        (t / "a.log").write_text("log", encoding="utf-8")
        kw = {"runs_root": t / "r", "inter": t / "i", "output_dir": t / "o"}
        dest, copied, skipped = save_run("テスト機能", "runX", logs=[t / "a.log"], **kw)
        assert dest == t / "r" / "runX" and skipped == [], (dest, skipped)
        for rel in ("json/テスト機能.json", "json/テスト機能_audit_results.json", "fragments/_unit_S1.json", "fragments/_partial_backend.json", "docx/詳細設計書_テスト機能.docx", "logs/a.log"):
            assert rel in copied and (dest / rel).is_file(), (rel, copied)
        assert not (dest / "json" / "他機能.json").exists() and not (dest / "json" / "テスト機能長.json").exists(), "他機能を拾った"
        (t / "o" / "詳細設計書_テスト機能.docx").unlink()
        assert "docx" in save_run("テスト機能", "runY", **kw)[2], "docx が無いときは skipped に出る"
        for call, exc in ((lambda: save_run("テスト機能", "runX", **kw), FileExistsError),
                          (lambda: save_run("テスト機能", "../x", **kw), ValueError),
                          (lambda: save_run("テスト機能", "runZ", logs=[t / "無い.log"], **kw), FileNotFoundError)):
            try:
                call()
            except exc:
                continue
            raise AssertionError(f"{exc.__name__} にならない")
        # logs/ だけの run には控えを書ける。空ディレクトリ・logs 以外もある run は従来どおり断る
        (t / "r" / "runL" / "logs").mkdir(parents=True)
        assert "logs/a.log" in save_run("テスト機能", "runL", logs=[t / "a.log"], **kw)[1], "logs だけの run に控えられない"
        # logs/ と仕分けの記録(.md)だけの run にも控えを書け、.md は残る。.md でも成果物の入る json/ があれば断る
        (t / "r" / "runT" / "logs").mkdir(parents=True)
        (t / "r" / "runT" / "warn_triage.md").write_text("仕分け", encoding="utf-8")
        assert "logs/a.log" in save_run("テスト機能", "runT", logs=[t / "a.log"], **kw)[1], "logs と .md だけの run に控えられない"
        assert (t / "r" / "runT" / "warn_triage.md").read_text(encoding="utf-8") == "仕分け", ".md が消えた"
        (t / "r" / "runJ" / "json").mkdir(parents=True)
        (t / "r" / "runJ" / "warn_triage.md").write_text("x", encoding="utf-8")
        try:
            save_run("テスト機能", "runJ", **kw)
        except FileExistsError:
            pass
        else:
            raise AssertionError("成果物のある run に控えられた")
        (t / "r" / "runE").mkdir()
        (t / "r" / "runM" / "logs").mkdir(parents=True)
        (t / "r" / "runM" / "x.txt").write_text("x", encoding="utf-8")
        for rn in ("runE", "runM"):
            try:
                save_run("テスト機能", rn, **kw)
            except FileExistsError:
                continue
            raise AssertionError(f"{rn} を断らない")
        # archive_stale_fragments: 断片が無ければ None。あれば _stale_ へ移し(消さない)、断片以外は動かさない
        assert archive_stale_fragments("テスト機能", runs_root=t / "r2", inter=t / "i0") is None
        (t / "i" / "_audit_part_1.json").write_text("{}", encoding="utf-8")
        sd, moved = archive_stale_fragments("テスト機能", runs_root=t / "r2", inter=t / "i")
        assert sd.parent == t / "r2" and sd.name.startswith("_stale_"), sd
        assert sorted(moved) == ["_audit_part_1.json", "_partial_backend.json", "_unit_S1.json"], moved
        assert all((sd / m).is_file() and not (t / "i" / m).exists() for m in moved), "移っていない"
        assert (t / "i" / "テスト機能.json").is_file(), "断片以外を動かした"
    # run_gate: run_after・subprocess.run・INTER を差し替え、4つの合否の組を見る。差し替えは必ず戻す
    class _Proc:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""
    ng_row = [{"id": "AC001", "kind": "remarks", "owner": "S1", "text": "t", "verdict": "ng", "reason": "r"}]
    ok_row = [dict(ng_row[0], verdict="ok")]
    g = globals()
    old_after, old_run, old_inter = g["run_after"], subprocess.run, INTER
    try:
        with tempfile.TemporaryDirectory() as td:
            INTER = Path(td)
            audit_p = Path(td) / "機能_audit_results.json"

            def gate(after_rc, warn_rc, rows):
                g["run_after"] = lambda feat: (print(json.dumps({"stopped_at": "x", "sendbacks": {}})) or after_rc)
                subprocess.run = lambda cmd, **kw: _Proc(warn_rc, "[WARN] db_operations[DB1] 空\n[ERROR] 許可リストに無い WARN 1 件")
                if rows is None:
                    audit_p.unlink(missing_ok=True)
                else:
                    audit_p.write_text(json.dumps(rows), encoding="utf-8")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = run_gate("機能")
                return rc, json.loads(buf.getvalue())
            rc, o = gate(1, 0, ok_row)
            assert rc == 1 and not o["ok"] and not o["after_ok"] and o.get("warn_skipped") and o["warn_unallowed"] == [], o
            rc, o = gate(0, 1, ok_row)
            assert rc == 1 and o["after_ok"] and any("db_operations[DB1]" in w for w in o["warn_unallowed"]), o
            rc, o = gate(0, 0, ng_row)
            assert rc == 1 and o["audit_ng"] == 1 and o["warn_unallowed"] == [], o
            rc, o = gate(0, 0, ok_row)
            assert rc == 0 and o["ok"] and o["audit_ng"] == 0 and o["audit_unknown"] == 0, o
            # unknown は ng ではないが、1件でもあれば不合格にし、件数を出す(G13)
            rc, o = gate(0, 0, ok_row + [dict(ng_row[0], id="AC002", verdict="unknown")] * 2)
            assert rc == 1 and not o["ok"] and o["audit_ng"] == 0 and o["audit_unknown"] == 2, o
            rc, o = gate(0, 0, None)
            assert rc == 1 and "audit 未実施" in o["audit_reason"], o
            # 理由の空な許可リストは例外で落とさず不合格(JSON が読め ok が偽、検査の subprocess は流れない)
            bad_p = Path(td) / "機能_warn_allow.json"
            bad_p.write_text(json.dumps({"allow": [{"key": "x", "reason": "", "date": "2026-10-16"}]}), encoding="utf-8")
            try:
                rc, o = gate(0, 0, ok_row)
            finally:
                bad_p.unlink()
            assert rc == 1 and not o["ok"] and any("許可リストが不正" in w for w in o["warn_unallowed"]), o
            # 当たらない登録は合否に影響させず allow_unmatched に出す(旧形式の key・新形式の owner。当たる登録は出さない)
            ok_p = Path(td) / "機能_warn_allow.json"
            ok_p.write_text(json.dumps({"allow": [
                {"key": "db_operations[DB1]", "reason": "r", "date": "2026-10-16"},
                {"key": "front_processes[FR8].steps[step_no=4]", "reason": "r", "date": "2026-10-16"},
                {"kind": "period", "targets": ["steps"], "owner": "CSV出力:クリック", "reason": "r", "date": "2026-10-16"}]}), encoding="utf-8")
            try:
                rc, o = gate(0, 0, ok_row)
            finally:
                ok_p.unlink()
            assert rc == 0 and o["ok"] and len(o["allow_unmatched"]) == 2 and o["allow_unmatched"][0].startswith("key=front_processes[FR8]"), o
            assert "allow_unmatched" not in gate(0, 0, ok_row)[1]
            # 一覧と再出力で同じ [WARN] 行が2回出ても、同じ script 由来の行は1回だけ数える
            g["run_after"] = lambda feat: (print(json.dumps({"stopped_at": "x", "sendbacks": {}})) or 0)
            subprocess.run = lambda cmd, **kw: _Proc(1, "[WARN] db_operations[DB1] 空\n[ERROR] 許可リストに無い WARN 1 件\n[WARN] db_operations[DB1] 空")
            audit_p.write_text(json.dumps(ok_row), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as dup_buf:
                run_gate("機能")
            o = json.loads(dup_buf.getvalue())
            dups = [w for w in o["warn_unallowed"] if w.startswith("verify_vs_source.py:") and "db_operations[DB1]" in w]
            assert len(dups) == 1, o
    finally:
        g["run_after"], subprocess.run, INTER = old_after, old_run, old_inter
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


def audit_sendbacks(rows, units):
    """判定結果の行(id・kind・owner・text・verdict・reason)のうち ng を、owner ごとの差し戻し文面にする。

    WHY: 既存の retry が読む issues と同じ形({"sendbacks": {単位: [行]}})にそろえ、意味の判定の差し戻しも
    構造の検査と同じ経路で単位へ戻す。owner が units 表に無い件は unknown に入れ、retry がその行を読ませる。
    ok と unknown は差し戻ししない(unknown は main 側で sonnet に判定し直させる)。
    接続: 呼出元 = main の audit-check 分岐。出力は after と同じ「→ 差し戻し先 <単位>」の行の形。
    """
    out = {}
    for r in rows:
        if r.get("verdict") != "ng":
            continue
        unit = r.get("owner") if r.get("owner") in units else "unknown"
        line = f"[ERROR] 意味の照合({r.get('kind')}) {r.get('id')} {r.get('text')}: {r.get('reason') or ''} → 差し戻し先 {unit}"
        out.setdefault(unit, []).append(line)
    return out


AUDIT_BATCH = 25  # WHY: 1依頼あたりの判定項目の上限。Haiku の1回の読みを小さく保ち、part を分けて書かせる
# WHY: 1ファイルの全文を依頼文に載せる上限(バイト)。超えたファイルだけ、その項目が参照する関数・定義の抜粋の節にする。
# 同じ項目の上限内のファイルは全文のまま載せる(G18 → G19: 1ファイルが大きいだけで項目全体が抜粋に戻り、unknown が増えたため)
AUDIT_SRC_MAX_BYTES = 60000


def _audit_excerpt_section(cid, f, total, segs):
    """全文を載せられないファイル f の、項目 cid 用の抜粋の節を返す。segs は claim の src_segs のうち f の断片。
    行頭の番号は、節の本文の通し番号(抜粋の先頭を1。見出し行・断片の見出し行は数えない)。evidence_line はこの番号で書かせる。"""
    size = f"全 {total} 行のうち" if total else ""
    out = [f"==== 抜粋: {cid} {f}({size}参照する部分。行頭の番号は抜粋の先頭を1とする通し番号)===="]
    n = 0
    for s in segs:
        out.append(f"---- 断片: {s.get('label') or '主抜粋'}(ファイルの {s.get('line_start')}-{s.get('line_end')} 行)----")
        for ln in s["lines"]:
            n += 1
            out.append(f"{n:4d}| {ln}")
    return "\n".join(out)


def _audit_sources(batch):
    """batch の判定項目が参照するソースを、依頼文に載せる形にする。(text, plan) を返す。
    WHY(G19): 上限はファイルごとに判定する。上限内のファイルは全文を行番号つきで1回ずつ載せ、上限を超える(または読めない)ファイルだけ、
    その項目が参照する断片(claim の src_segs)を項目ごとの抜粋の節にする。1つの項目に全文と抜粋が混ざってよい。
    marionet-pilot の api.ts(約361KB)を参照する項目が、他の小さなファイルまで抜粋に戻り unknown になっていた(run22 part 4 の25件)。
    plan は {項目の id: {"full": [全文の節のファイル], "excerpt": [抜粋の節のファイル]}}。
    src_files が空、または抜粋にすべきファイルの断片(src_segs)が無い項目は plan に入れない(従来どおり text の抜粋で渡す)。
    呼出元: build_audit_prompts(audit の依頼文)と、audit-check の再判定の依頼文。"""
    blocks, big = {}, {}  # blocks: 全文の節(ファイル→文)、big: 全文を載せられないファイル→行数(読めなければ None)
    for c in batch:
        for f in c.get("src_files") or []:
            if f in blocks or f in big:
                continue
            try:
                raw = Path(f).read_bytes()
            except OSError:
                big[f] = None
                continue
            lines = raw.decode("utf-8", errors="replace").splitlines()
            if len(raw) > AUDIT_SRC_MAX_BYTES:
                big[f] = len(lines)
                continue
            blocks[f] = f"==== ソース: {f}(全 {len(lines)} 行)====\n" + "\n".join(f"{i:4d}| {ln}" for i, ln in enumerate(lines, 1))
    plan, sections = {}, []
    for c in batch:
        files = c.get("src_files") or []
        if not files:
            continue
        exc = [f for f in files if f not in blocks]
        secs = []
        for f in exc:
            fs = [s for s in (c.get("src_segs") or []) if s.get("file") == f and s.get("lines")]
            if not fs:
                break
            secs.append(_audit_excerpt_section(c["id"], f, big.get(f), fs))
        else:
            plan[c["id"]] = {"full": [f for f in files if f in blocks], "excerpt": exc}
            sections.extend(secs)
    used = {f for p in plan.values() for f in p["full"]}  # 全文で渡す項目が使うファイルだけ載せる
    return "\n\n".join([b for f, b in blocks.items() if f in used] + sections), plan


def _audit_src_rules():
    """依頼文に全文の節・抜粋の節を載せるときの、根拠と evidence_line の書き方。audit と再判定の依頼文が同じ文面を使う(G19)。"""
    return [
        "- 判定の根拠は、この依頼文に載せたソースだけ。全文の節(見出し `==== ソース: <ファイル>(全 N 行)====`)と、"
        "抜粋の節(見出し `==== 抜粋: <項目の id> <ファイル>(…)====`。大きすぎて全文を載せなかったファイルの、その項目が参照する部分)の両方が根拠になる。"
        "載せたソースの外を根拠にしない。確かめられなければ unknown",
        "- 判定項目の src_files は全文の節に載るファイル、src_excerpt_files はその項目の id の抜粋の節に載るファイル。"
        "text を持つ項目は従来どおり text が根拠",
        "- evidence_line: 全文の節ならファイルの行番号(行頭の番号)、抜粋の節ならその節の行頭の通し番号(抜粋の先頭を1。見出し行は数えない)、"
        "text なら text の先頭を1とする。どのファイルの行かを evidence_file に書く(全文の節・抜粋の節の見出しのファイル。text の項目と根拠なしは null)",
    ]


def _audit_item(c, plan=None):
    """判定項目 1 件を、判定の担当に渡す形(id・主語・文・抜粋)にする。audit の依頼文と再判定の依頼文が同じ形を使う。
    WHY: 文だけでは何の記述か分からない項目(「点数と金額の2列。左段」)があり、担当が unknown を返していた。主語(subject)を添える。
    plan(_audit_sources の項目ごとの割り当て)があるとき(ソースを依頼文の上に載せた項目)は、src_excerpt を text なしの出典(file・line_start・line_end)にし、
    全文の節のファイルを src_files、抜粋の節のファイルを src_excerpt_files で指す。plan が無いときは従来どおり text(抜粋)を持つ。"""
    ref = c.get("src_ref") or {}
    if plan is not None:
        it = {"id": c["id"], "subject": c.get("subject") or "", "statement": c["text"], "src_excerpt": dict(ref), "src_files": list(plan["full"])}
        if plan["excerpt"]:
            it["src_excerpt_files"] = list(plan["excerpt"])
        return it
    return {"id": c["id"], "subject": c.get("subject") or "", "statement": c["text"],
            "src_excerpt": {**ref, "text": c.get("src_excerpt") or ""}}


def build_audit_prompts(feat, claims):
    """判定項目を AUDIT_BATCH 件ずつに分け、Haiku の判定の依頼文の配列を返す。

    WHY: 判定の入力を part ごとに渡し、結果も part ごとに書かせて audit-check で1つにまとめる。
    各依頼文の先頭に「あなたは part N。書き先は …_audit_part_N.json。判定項目は ACxxx〜ACyyy」を明記する。
    WHY(2026-10-15 run13): 呼出元が「配列の n 番目(0 始まり)」で依頼を渡し、2体が同じ part_1 を書いて AC101〜125 の part が欠けた。
    part の番号は 1 始まりで、配列の位置とは別に各要素が持ち(part)、本文にも書く。audit-check はこの part と ids を正として照合する。
    各要素の claims は、audit が判定に渡した判定項目の写し。audit-check はこれを正とする
    (claims.json が audit の後で作り直されて id がずれても、判定を別の項目に結ばないため)。
    WHY(G18): 抜粋を参照の識別子で継ぎ足す方式では、run21 でも142件中31件が「根拠が抜粋の外」で unknown のまま残った。
    そこで各 part が参照するソースファイルの全文を行番号つきで1回ずつ判定項目の前に載せ、全文を載せた項目は出典の行範囲で指す。
    WHY(G19): 上限(AUDIT_SRC_MAX_BYTES)はファイルごとに判定する。上限を超えるファイルだけ、その項目が参照する断片を抜粋の節にし、
    同じ項目の他のファイルは全文のまま載せる。判定の根拠は、載せた全文の節と抜粋の節の両方。
    接続: 呼出元 = main の audit 分岐。手順書は prompts/AU_意味の照合.md(入力の形は判定項目の subject・statement・src_excerpt)。
    """
    au = SKILL / "prompts" / "AU_意味の照合.md"
    chunks = [claims[s:s + AUDIT_BATCH] for s in range(0, len(claims), AUDIT_BATCH)]
    prompts = []
    for n, batch in enumerate(chunks, 1):
        out = INTER / f"{feat}_audit_part_{n}.json"
        first, last = batch[0]["id"], batch[-1]["id"]
        src_text, plans = _audit_sources(batch)
        src_part = ([*_audit_src_rules(), "- ソース(全文の節と抜粋の節。行番号つき):", src_text] if src_text else [])
        prompt = "\n".join([
            f"あなたは part {n}(全 {len(chunks)} part)。書き先は {out}。判定項目は {first}〜{last} の {len(batch)} 件。",
            f"design-doc の意味の照合(haiku)。機能名: {feat}。",
            f"- 手順書: {au}(読んで従う。正解はこの依頼文に載せたソース(全文の節・抜粋の節・text)だけ)",
            f"- 書くのは {out} の1ファイルだけ。他の part のファイルは書かない。"
            f"中身は判定の JSON 配列(id・verdict・evidence_line・evidence_file・reason)を、下の判定項目と同じ順・同じ {len(batch)} 件で。id は {first}〜{last} のものだけ",
            *src_part,
            "- 判定項目:",
            json.dumps([_audit_item(c, plans.get(c["id"])) for c in batch], ensure_ascii=False, indent=1),
        ]) + "\n"
        prompts.append({"part": n, "subagent_type": SUBAGENT_TYPE, "model": "haiku", "out": str(out), "first_id": first, "last_id": last,
                        "ids": [c["id"] for c in batch], "claims": batch, "prompt": prompt})
    return prompts


def build_audit_retry_prompt(feat, unknown, retry_out):
    """Haiku で決められなかった判定項目(unknown)だけを sonnet に1回判定させる依頼文を返す。
    WHY(G19): audit の依頼文と同じ作りにする。ソースは _audit_sources が返す全文の節(上限内のファイル)と抜粋の節(上限を超えるファイルだけ)で、
    根拠の決まりと evidence_line の書き分けも _audit_src_rules を共有する。plan の無い項目(src_files 無し・旧形式)は、予算を広げた抜粋
    (src_excerpt_wide。無ければ src_excerpt)の text で渡す(G14 run20)。
    呼出元: main の audit-check 分岐。"""
    src_text, plans = _audit_sources(unknown)
    items = []
    for c in unknown:
        it = _audit_item(c, plans.get(c["id"]))
        if c["id"] not in plans and c.get("src_excerpt_wide"):
            it["src_excerpt"]["text"] = c["src_excerpt_wide"]
        items.append(it)
    src_part = [*_audit_src_rules(), "- ソース(全文の節と抜粋の節。行番号つき):", src_text] if src_text else []
    return "\n".join([
        f"design-doc の意味の照合の再判定(sonnet)。機能名: {feat}。",
        f"- 手順書: {SKILL / 'prompts' / 'AU_意味の照合.md'}(読んで従う。正解はこの依頼文に載せたソース(全文の節・抜粋の節・text)だけ)",
        f"- 書くのは {retry_out} の1ファイルだけ。中身は判定の JSON 配列(id・verdict・evidence_line・evidence_file・reason)",
        "- 決まり: 載せたソースの外を根拠に ok にしない。確かめられなければ unknown のまま返す",
        *src_part,
        "- 判定項目:",
        json.dumps(items, ensure_ascii=False, indent=1),
    ]) + "\n"


def audit_part_problems(feat, prompts):
    """audit_prompts の各 part について、書かれた判定(_audit_part_<n>.json)が過不足なく1回ずつかを確かめる。

    返り値 (verdict の辞書 {id: 行}, 問題 {part の番号: [文面]})。
    WHY: 欠け(担当が書かなかった part)・他の part の項目を書いた・同じ id の重複・壊れた JSON を、判定の集計の前に止める。
    問題のある part の行は辞書に入れない(別の項目の判定を今回の id に結ばないため)。
    """
    verdicts, problems = {}, {}
    for p in prompts:
        n, expected = p["part"], list(p["ids"])
        part_p = INTER / f"{feat}_audit_part_{n}.json"
        msgs, rows, readable = [], [], False
        if not part_p.exists():
            msgs.append(f"書かれていない: {part_p.name}")
        else:
            try:
                data = json.loads(part_p.read_text(encoding="utf-8"))
                rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
                readable = isinstance(data, list)
                if not readable or len(rows) != len(data):
                    msgs.append("JSON の配列でない行がある")
            except (OSError, ValueError):
                msgs.append(f"JSON として読めない: {part_p.name}")
        ids = [str(r.get("id")) for r in rows]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        stray = [i for i in dict.fromkeys(ids) if i not in expected]
        lost = [i for i in expected if i not in ids] if readable else []
        if dup:
            msgs.append(f"同じ id が重複: {','.join(dup[:8])}")
        if stray:
            msgs.append(f"この part(part {n}: {p['first_id']}〜{p['last_id']})の項目でない id: {','.join(stray[:8])}" + ("…" if len(stray) > 8 else ""))
        if lost:
            msgs.append(f"判定が欠けている id: {','.join(lost[:8])}" + (f"…(計 {len(lost)} 件)" if len(lost) > 8 else ""))
        if msgs:
            problems[n] = msgs
            continue
        for r in rows:
            verdicts[str(r.get("id"))] = r
    return verdicts, problems


def save_run(feat, run, logs=(), runs_root=None, inter=None, output_dir=None):
    """run の成果物(中間JSON・断片・検査ログ・docx)を target/runs/<run名>/ に控える。

    WHY: /private/tmp の片付けで run17・run18 が消えたため。回し直しの比較をするには過去の run が残っている必要がある。
    他の run を壊さないよう保存先が既にあれば FileExistsError。ただし logs/ と仕分けの記録(直下の .md)だけの run は成果物が無いので控えを許す
    (検査ログ・仕分けの記録だけ先に置かれた run への控え。logs/ の同名ファイルは copy2 で置き換わり、.md は触らない)。無い対象は黙って飛ばさず skipped に返す。
    呼出元: main の save-run(メインの tool-worker が after と audit-check の後に流す)。
    戻り: (dest, copied(dest からの相対パス), skipped(控えられなかった対象の名前))。"""
    if not run or "/" in run or ".." in run:
        raise ValueError(f"run 名が不正(空・'/'・'..' は不可): {run!r}")
    inter = Path(inter) if inter else INTER
    output_dir = Path(output_dir) if output_dir else SKILL / "target" / "output"
    dest = (Path(runs_root) if runs_root else SKILL / "target" / "runs") / run
    # WHY: 控え済みの成果物(json・fragments・docx)を上書きしないための検査。logs/ と、仕分けの記録(warn_triage.md など直下の .md)は
    # 成果物ではないので、それだけが在る run には控えを許す。空ディレクトリは従来どおり断る(何の run か分からない)
    if dest.exists():
        existing = list(dest.iterdir())
        if not existing or not all((p.is_dir() and p.name == "logs") or (p.is_file() and p.suffix == ".md") for p in existing):
            raise FileExistsError(f"既にある run は上書きしない: {dest}")
    log_paths = [Path(x) for x in logs]
    for lp in log_paths:
        if not lp.is_file():
            raise FileNotFoundError(f"検査ログが無い: {lp}")
    # 名前は feat と完全一致か feat_ 始まりに絞る(名前が feat で始まる他機能を拾わない)
    groups = [
        ("json", "中間JSON", [inter / f"{feat}.json"] + sorted(inter.glob(f"{feat}_*.json"))),
        ("fragments", "断片(_unit_*.json・_partial_*.json)", sorted(inter.glob("_unit_*.json")) + sorted(inter.glob("_partial_*.json"))),
        ("docx", "docx", [output_dir / f"詳細設計書_{feat}.docx"]),
        ("logs", "検査ログ", log_paths),
    ]
    copied, skipped = [], []
    for folder, label, files in groups:
        files = [f for f in files if f.is_file()]
        if not files:
            skipped.append(label)
            continue
        (dest / folder).mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, dest / folder / f.name)
            copied.append(f"{folder}/{f.name}")
    dest.mkdir(parents=True, exist_ok=True)
    return dest, copied, skipped


def archive_stale_fragments(feat, runs_root=None, inter=None):
    """INTER 直下の _unit_*.json・_partial_*.json・_audit_part_*.json を target/runs/_stale_<日時>/ へ移す(消さない)。

    WHY: 前の run の断片が残ったまま依頼文を作ると、担当が古い断片を読んだ例があったため。
    呼出元: main の prompts(units 読込の前)。feat は呼出しの形をそろえるための引数(断片の名前は機能名を含まない)。
    戻り: 断片が無ければ None。あれば (退避先 Path, 移した相対名のリスト)。"""
    inter = Path(inter) if inter else INTER
    found = []
    for pat in ("_unit_*.json", "_partial_*.json", "_audit_part_*.json"):
        found += sorted(inter.glob(pat))
    if not found:
        return None
    dest = (Path(runs_root) if runs_root else SKILL / "target" / "runs") / ("_stale_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    dest.mkdir(parents=True)
    for f in found:
        shutil.move(str(f), str(dest / f.name))
    return dest, [f.name for f in found]


def main(argv=None):
    ap = argparse.ArgumentParser(description="design-doc の単位方式を機械化する")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p1 = sub.add_parser("prompts", help="各単位の依頼文を書く")
    p1.add_argument("feature")
    p1.add_argument("--out", required=True)
    p1.add_argument("--src", action="append", default=[], help="入力のソース(絶対パス)。複数可")
    p1.add_argument("--phase", type=int, choices=(1, 2), default=1,
                    help="1=units.json の after を持たない単位(S1 S3 H2 H3 H4)、2=after を持つ単位(H1 S2)。phase 2 は S1 の _unit_S1.json が要る")
    p2 = sub.add_parser("after", help="検査と後段を順に流す")
    p2.add_argument("feature")
    p3 = sub.add_parser("retry", help="差し戻しを足した依頼文を書く")
    p3.add_argument("feature")
    p3.add_argument("unit")
    p3.add_argument("--issues", required=True, help="after の出力 JSON(sendbacks を読む)")
    p4 = sub.add_parser("audit-check", help="意味の照合の判定を集めて結果と差し戻しにする")
    p4.add_argument("feature")
    p5 = sub.add_parser("audit", help="判定項目を作り、判定の依頼文(25件ずつ)を書く")
    p5.add_argument("feature")
    p6 = sub.add_parser("gate", help="after・許可リスト照合・audit の合否を1つ出す")
    p6.add_argument("feature")
    p7 = sub.add_parser("save-run", help="中間JSON・断片・検査ログ・docx を target/runs/<run名>/ に控える")
    p7.add_argument("feature")
    p7.add_argument("run")
    p7.add_argument("--log", action="append", default=[], help="検査ログのパス。複数可")
    a = ap.parse_args(argv)

    if a.selftest:
        selftest()
        print("selftest OK")
        return 0
    if a.cmd == "prompts":
        # WHY: phase 2 で退避すると phase 1 が書いた S1 の断片を消すので、退避は phase 1 だけ
        if a.phase == 1:
            moved = archive_stale_fragments(a.feature)
            if moved:
                print(f"前の run の断片 {len(moved[1])} 件を退避: {moved[0]}")
        units = load_units()
        data = {"units": [
            {"unit": u, "subagent_type": SUBAGENT_TYPE, "model": units[u]["model"], "prompt": build_prompt(units, a.feature, u, a.src)}
            for u in units if bool(units[u].get("after")) == (a.phase == 2)
        ]}
        Path(a.out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {len(data['units'])} units (phase {a.phase}) to {a.out}")
        return 0
    if a.cmd == "after":
        return run_after(a.feature)
    if a.cmd == "gate":
        return run_gate(a.feature)
    if a.cmd == "retry":
        units = load_units()
        # WHY: units 表に無い名前(S0・unknown・誤記)は単位ではない。KeyError の追跡でなく、何を直すかの文言で止める。
        if a.unit not in units:
            print(f"単位 {a.unit} は units 表に無いので retry できない(単位: {'・'.join(units)})。"
                  "unknown や S0 などの差し戻しは、after が出した行の内容を読んで、スクリプトの不具合か、"
                  "S0・R1 のような並行の単位の外の担当(units.json の outside_fragments)かを判断して直す", file=sys.stderr)
            return 2
        with open(a.issues, encoding="utf-8") as f:
            issues = json.load(f)
        # WHY: after の出力(sendbacks の辞書)か、差し戻し文面の一覧の両方を受ける。
        lines = issues.get("sendbacks", {}).get(a.unit, []) if isinstance(issues, dict) else issues
        if not lines:
            print(f"差し戻しの文面が無い: {a.unit}", file=sys.stderr)
            return 1
        print(json.dumps({"unit": a.unit, "subagent_type": SUBAGENT_TYPE, "model": units[a.unit]["model"],
                          "prompt": build_prompt(units, a.feature, a.unit, sendbacks=lines)},
                         ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "audit":
        # WHY: 判定項目の書き出し(build_audit_claims.py)を流し、その結果を25件ずつの Haiku の依頼文にして書く。
        # 中間JSONが無いときは、前回の判定項目を新しいものと取り違えないよう止める(build_audit_claims は警告で終了コード0のため)。
        if not (INTER / f"{a.feature}.json").exists():
            print(f"中間JSONが無いので判定項目を作れない: {INTER / (a.feature + '.json')}", file=sys.stderr)
            return 1
        p = subprocess.run([sys.executable, str(SKILL / "target" / "scripts" / "build_audit_claims.py"), a.feature],
                           cwd=SKILL, capture_output=True, text=True, env=_run_env(a.feature))
        print(p.stdout + p.stderr, file=sys.stderr, end="")
        claims_p = INTER / f"{a.feature}_audit_claims.json"
        if p.returncode != 0 or not claims_p.exists():
            print(f"判定項目を作れなかった: {claims_p}", file=sys.stderr)
            return 1
        claims = json.loads(claims_p.read_text(encoding="utf-8"))
        # WHY: 判定項目の id は毎回 AC001 から振り直されるため、前回の part が残ると audit-check が別の項目の判定を今回の id に結び付けてしまう。依頼を書く前に part を消す
        for stale in INTER.glob(f"{a.feature}_audit_part_*.json"):
            stale.unlink()
        prompts = build_audit_prompts(a.feature, claims)
        out = INTER / f"{a.feature}_audit_prompts.json"
        out.write_text(json.dumps(prompts, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"判定項目 {len(claims)} 件を {len(prompts)} 依頼に分けて {out} に書いた")
        # WHY: 呼出元が part の番号を取り違えないよう、番号と範囲を並べて見せる(配列の位置ではなく各要素の part を使う)
        for p in prompts:
            print(f"  part {p['part']}: {p['first_id']}〜{p['last_id']}({len(p['ids'])}件) → {Path(p['out']).name}")
        return 0
    if a.cmd == "audit-check":
        # WHY: 意味の照合(audit)の判定(part)を集めて結果にまとめ、ng は owner ごとの差し戻しにする。
        # unknown は sonnet で1回だけ判定し直す依頼文を出す(2回目は回さない)。判定項目の書き出しは build_audit_claims.py の段。
        units = load_units()
        claims_p = INTER / f"{a.feature}_audit_claims.json"
        if not claims_p.exists():
            print(f"判定項目が無い: {claims_p}(build_audit_claims.py を先に流す)", file=sys.stderr)
            return 1
        claims = json.loads(claims_p.read_text(encoding="utf-8"))
        # WHY: audit が書いた audit_prompts.json の各 part(ids と claims の写し)を正とし、全判定項目が1回ずつ判定されたかを確かめる。
        # claims.json は after の再実行などで作り直され、前回 ok の項目が除かれて id が振り直されうる(2026-10-15 run13 で本文が4件ずれていた)。
        # 判定は audit が渡した項目に結ぶので、claims.json でなく audit_prompts.json の claims を使い、食い違いは警告する。
        plan_p = INTER / f"{a.feature}_audit_prompts.json"
        plan = json.loads(plan_p.read_text(encoding="utf-8")) if plan_p.exists() else None
        if plan and all("claims" in p for p in plan):
            frozen = [c for p in plan for c in p["claims"]]
            if {c["id"]: c["text"] for c in frozen} != {c["id"]: c["text"] for c in claims}:
                print(f"[警告] {claims_p.name} が audit の後で作り直されている。判定は audit_prompts.json の判定項目に結ぶ"
                      "(判定項目を作り直したなら audit からやり直す)", file=sys.stderr)
            claims = frozen
            verdicts, problems = audit_part_problems(a.feature, plan)
            if problems:
                # WHY: 欠け・重複・他の part の項目があるまま集計すると、欠けが unknown に化けて sonnet の再判定に回り、原因(担当の取り違え)が隠れる。
                # 集計せず、やり直す part の番号と、その part の依頼文の場所を返す
                print(json.dumps({
                    "ok": False, "redo_parts": sorted(problems), "problems": {str(n): m for n, m in sorted(problems.items())},
                    "hint": f"{plan_p} の part N の prompt に従って、その part だけ担当を起動し直す(書き先は各 part の out)。直したら audit-check をもう一度流す",
                    "prompts": str(plan_p)}, ensure_ascii=False, indent=1))
                return 1
        else:
            # 古い形の audit_prompts.json(claims の写しが無い)・または audit を通っていない場合は従来どおり、番号つきの part を全部読む
            verdicts = {}
            for part in sorted(INTER.glob(f"{a.feature}_audit_part_*.json")):
                if re.fullmatch(r"\d+", part.stem.rsplit("_", 1)[-1]):
                    for row in json.loads(part.read_text(encoding="utf-8")):
                        verdicts[str(row.get("id"))] = row
        # WHY: 再判定の結果(_retry.json)は最後に読み、先の判定を上書きする
        retry_p = INTER / f"{a.feature}_audit_part_retry.json"
        if retry_p.exists():
            ids_now = {c["id"] for c in claims}
            for row in json.loads(retry_p.read_text(encoding="utf-8")):
                if str(row.get("id")) in ids_now:
                    verdicts[str(row.get("id"))] = row
        results = []
        for c in claims:
            row = verdicts.get(c["id"])
            if row is None:
                verdict, reason = "unknown", "判定が返っていない"
            elif row.get("verdict") in ("ok", "ng", "unknown"):
                verdict, reason = row["verdict"], str(row.get("reason") or "")
            else:
                verdict, reason = "unknown", "判定の値が不正"
            results.append({
                "id": c["id"], "kind": c.get("kind"), "owner": c.get("owner"), "subject": c.get("subject"),
                "text": c.get("text"), "src_excerpt": c.get("src_excerpt"),
                "verdict": verdict, "evidence_line": (row or {}).get("evidence_line"),
                "evidence_file": (row or {}).get("evidence_file"), "reason": reason,
            })
        # WHY: 前回の結果の ok のうち今回の判定に無い行は末尾に残す。build_audit_claims.load_prior_ok が前回の ok をキャッシュとして読むため、
        # 今回の claims の分だけで上書きすると ok が次の回に消え、判定を回すたびに全件の Haiku 判定をやり直すことになる。
        # 残す行は差し戻し・unknown の計算には混ぜない(sendbacks と retry は今回の results だけから作る)。
        results_p = INTER / f"{a.feature}_audit_results.json"
        carried = []
        if results_p.exists():
            try:
                prior = json.loads(results_p.read_text(encoding="utf-8"))
                now = {(r.get("text"), json.dumps(r.get("src_excerpt"), sort_keys=True)) for r in results}
                carried = [r for r in prior if r.get("verdict") == "ok"
                           and (r.get("text"), json.dumps(r.get("src_excerpt"), sort_keys=True)) not in now]
            except (OSError, ValueError, AttributeError):
                carried = []
        results_p.write_text(json.dumps(results + carried, ensure_ascii=False, indent=1), encoding="utf-8")
        sendbacks = audit_sendbacks(results, units)
        out = {"ok": not sendbacks, "total": len(results), "sendbacks": sendbacks}
        unknown = [c for c, r in zip(claims, results) if r["verdict"] == "unknown"]
        # WHY: 再判定の依頼は _audit_part_retry.json が無いときだけ出す。再判定の結果でも unknown の件は unknown のまま残し、差し戻しには入れない(2回目は回さない)
        if unknown and not (INTER / f"{a.feature}_audit_part_retry.json").exists():
            # WHY: Haiku で決められなかった件(unknown)だけを sonnet に1回判定させる。ok を含む全件は読み直さない
            retry_out = INTER / f"{a.feature}_audit_part_retry.json"
            # WHY(G19): 再判定も audit と同じ作り(上限内のファイルは全文、超えるファイルだけ抜粋の節)。根拠は依頼文に載せた全文の節と抜粋の節の両方
            prompt = build_audit_retry_prompt(a.feature, unknown, retry_out)
            out["retry"] = {"subagent_type": SUBAGENT_TYPE, "model": "sonnet", "out": str(retry_out), "unknown": len(unknown), "prompt": prompt}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 1 if sendbacks else 0
    if a.cmd == "save-run":
        # WHY: run の成果物を /private/tmp の片付けで失わないよう target/runs/ に控える。呼出元: メインの tool-worker(after と audit-check の後)。
        # 飛ばしがあっても控えたことは成立するので 0。上書き・不正な run 名・無いログは握りつぶさず 2。
        try:
            dest, copied, skipped = save_run(a.feature, a.run, logs=a.log)
        except (FileExistsError, ValueError, FileNotFoundError) as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"保存先: {dest}\n控えた: {len(copied)} 件\n飛ばした: {'・'.join(skipped) or 'なし'}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
