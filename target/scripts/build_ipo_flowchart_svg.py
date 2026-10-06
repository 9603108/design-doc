#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_ipo_flowchart_svg.py — IPO データフロー図（物理テーブル別 Flowchart）の SVG/HTML を直接生成する（v26、v30 で外部設定化、2026-07-19 で真に画面別実行対応）

【目的】
diagram-design スキルが API overload 等で利用できないケースの代替パイプラインとして、
Python で SVG を直接構築する。物理テーブル単位の DB ノードを持つ IPO Flowchart を生成。

【意味合い】
レビュー指摘（v26）「複数テーブルをまとめた謎の集約DBになっている、正しいテーブル単位で1要素にすべき」への対応。
DB層を物理テーブル（およびキャッシュ等のデータストア）1つにつき1ノードへ分解して描画する。
どのテーブルを描くかは screen_config の svg_layout.dbs が画面ごとに与える（スクリプト側に表名は持たない）。
v30 でハードコードしていた SVG レイアウト座標（triggers/fronts/backends/dbs/transition + シナリオ別矢印定義）を
screen_config.json の diagrams.ipo_flowchart.svg_layout に外出しした。ただし v30 時点では座標データの
外出しに留まり、スクリプトの実行自体（OUTPUT_HTML 固定パス・<title> 文言・get_diagram_spec の呼出）は
当時の既定画面にハードコードされたままだった（この不整合が原因で、別画面の {feature_name}_ipo_flowchart.svg が
既定画面の図のコピーになる誤りが実際に発生していた）。2026-07-19 の本修正で main()/build_svg() に
feature_name を通し、出力パス・<title>・get_diagram_spec 呼出のすべてを画面別に切替えることで、
初めて真に画面非依存（汎用 SVG 組立エンジン）にした。COLOR 定義と SVG 描画プリミティブ
（cylinder/rect_node/pill_node/arrow）は画面共通のスタイルなので Python 側に残置。

【接続情報】
- 入力: target/intermediate/{feature_name}_screen_config.json (diagrams.ipo_flowchart.svg_layout)
        feature_name は sys.argv[1] または環境変数 FEATURE_NAME で必ず与える
        （2026-10-05: 既定値は廃止。どちらも無ければ _config_loader が SystemExit で止める）
- 出力: target/diagrams/{feature_name}_ipo_flowchart.html（standalone HTML、inline SVG 埋め込み）
        2026-10-05: 全画面で機能名の接頭辞を付ける（接頭辞なしの既定画面という分岐は廃止）。
        add_diagrams_to_intermediate.py が中間 JSON に書く {feature_name}_{diagram_id} と同じ stem。
        + target/diagrams/{feature_name}_ipo_flowchart.svg（後で extract_svg.py が抽出）
- 後段: extract_svg.py → svg_to_png.py → generate_docx.js が PNG を「2.2 IPO データフロー図」に埋め込む

【設定に ipo_flowchart が有るときだけ作る】
- 目的: screen_config の diagrams に ipo_flowchart が無い画面では、図を作らずに正常終了（exit 0）する。
- 意味合い: IPO 図は画面ごとに任意の図で、定義が無い画面は docx 側でも「2.2」の節ごと出ない。
            手順書は本スクリプトを全画面で実行するので、定義が無いことを異常終了（KeyError）にしない。
- 接続情報: 判定は main() の冒頭で _config_loader.get_all_diagram_ids を使う。
            get_diagram_spec は未定義の diagram_id に KeyError を投げるため、それを呼ぶ build_svg より前に置く。
- 仕様根拠: 15_中間JSONスキーマ.md「diagrams」セクション、SKILL.md「Phase 3-4 外部設定ファイル化」（v30）
"""
from pathlib import Path

# v30: ハードコード座標を screen_config.json に集約、_config_loader 経由で取得する
# 2026-07-19: 画面別実行対応のため get_feature_name_from_argv / get_screen_name を追加
# 2026-10-05 汎用化: 既定の機能名の定数は _config_loader 側で廃止されたため import しない
from _config_loader import get_all_diagram_ids, get_diagram_spec, get_feature_name_from_argv, get_screen_name

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
DIAGRAMS_DIR = SKILL_DIR / "target" / "diagrams"


# 色定義（既存 process_structure.html の neutral stone paper + rust accent を継承）
# 意味合い: スキン（配色）は画面共通の編集基準。画面別ハードコードではないため Python 側に残置。
# シナリオ識別子（color_key="link" 等）から COLOR 辞書のキーへのマッピングに使う。
COLOR = {
    "bg":          "#faf7f2",  # paper
    "ink":         "#2d3142",  # main text / ink stroke
    "muted":       "#4f5d75",  # secondary
    "accent":      "#b5523a",  # rust (focal scenario)
    "link":        "#2e5aa8",  # link blue (initial scenario)
    "scenario3":   "#2d3142",  # ink (transition scenario)
    "scenario4":   "#7a6b5e",  # muted brown (previous data scenario)
    "node_fill":   "#ffffff",
    "trigger_fill":"#e7eef9",
    "backend_fill":"#eeece6",
    "data_fill":   "#f4eee5",
    "border":      "#4f5d75"
}

# シナリオ color_key → 矢印マーカー id の対応（共通スタイル）
SCENARIO_COLOR_TO_MARKER = {
    "link":      "a_blue",
    "accent":    "a_accent",
    "scenario3": "a_ink",
    "scenario4": "a_muted",
}


def cylinder(x, y, w, h, fill, stroke, label_main, label_sub):
    """円柱形（データストア）。ラベル2行: 論理名（上）+ 物理名（下イタリック）"""
    rx = w / 2
    ry = 8
    top_ellipse = f'<ellipse cx="{x + rx}" cy="{y + ry}" rx="{rx}" ry="{ry}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    side = (
        f'<path d="M {x} {y + ry} '
        f'L {x} {y + h - ry} '
        f'A {rx} {ry} 0 0 0 {x + w} {y + h - ry} '
        f'L {x + w} {y + ry} '
        f'A {rx} {ry} 0 0 0 {x} {y + ry} Z" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    )
    bottom_ellipse = (
        f'<path d="M {x} {y + h - ry} '
        f'A {rx} {ry} 0 0 0 {x + w} {y + h - ry}" '
        f'fill="none" stroke="{stroke}" stroke-width="1.5"/>'
    )
    text_main = f'<text x="{x + rx}" y="{y + h/2 - 2}" text-anchor="middle" font-size="13" fill="{COLOR["ink"]}" font-weight="600">{label_main}</text>'
    text_sub  = f'<text x="{x + rx}" y="{y + h/2 + 16}" text-anchor="middle" font-size="10" fill="{COLOR["muted"]}" font-style="italic">{label_sub}</text>'
    return "\n".join([side, top_ellipse, bottom_ellipse, text_main, text_sub])


def rect_node(x, y, w, h, fill, stroke, label, rx=8):
    """通常の矩形ノード（処理 / 出力）"""
    body = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" ry="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    text = f'<text x="{x + w/2}" y="{y + h/2 + 4}" text-anchor="middle" font-size="12" fill="{COLOR["ink"]}" font-weight="600">{label}</text>'
    return body + "\n" + text


def pill_node(x, y, w, h, fill, stroke, label):
    """楕円トリガーノード（start）"""
    rx = h / 2
    body = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" ry="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    text = f'<text x="{x + w/2}" y="{y + h/2 + 4}" text-anchor="middle" font-size="11" fill="{COLOR["ink"]}" font-weight="600">{label}</text>'
    return body + "\n" + text


def arrow(x1, y1, x2, y2, color, marker_id, label=None, dashed=False, label_offset=(0, -4)):
    """矢印 + （任意）ラベル。**直線で描画**（v27 で L字折れ線を廃止）。

    v26→v27: L字折れ線（縦→水平→縦）は y_mid で複数矢印の水平線が重なって視認性が低下したため、
    起点から終点への直線（斜め）に変更。重なりは斜めにずれることで自然に解消される。

    v31 (フェーズB施策B7): label は従来の単一文字列に加え、list/tuple（複数行分）も受け付ける。
    意味合い: 同一 from/to への複数DB操作（例: 同じテーブルへの SELECT と UPSERT）を build_svg() 側
              で1本のエッジに統合した際、ラベルを縦積み表示するための拡張。単一文字列時の
              白背景ボックス描画（既存実装、変更禁止・二重着手回避対象）はそのまま else 分岐で温存し、
              複数行時のみ新設した if 分岐（高さ可変の白背景ボックス）を通す。
    接続情報: 呼出元は build_svg() 内のエッジ統合ロジック（B7）。
    """
    dash = ' stroke-dasharray="6,3"' if dashed else ''
    path = f'M {x1} {y1} L {x2} {y2}'
    line = f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6"{dash} marker-end="url(#{marker_id})"/>'
    label_svg = ""
    if label:
        lx = (x1 + x2) / 2 + label_offset[0]
        ly = (y1 + y2) / 2 + label_offset[1]
        if isinstance(label, (list, tuple)) and len(label) > 1:
            # B7 新設: 複数行ラベル（縦積み）。行数に応じて白背景ボックスの高さを拡張する。
            # 意味合い: from/to 統合エッジ（B7）専用の描画経路。単一行時の下記 else 分岐（既存実装）は
            #           一切変更しない（二重着手回避）。
            lines = [str(x) for x in label if x]
            line_h = 12
            text_len = max(max(len(l) * 7 for l in lines), 30)
            box_h = line_h * len(lines) + 4
            box_top = ly - box_h / 2
            texts = "".join(
                f'<text x="{lx}" y="{box_top + line_h * (i + 1) - 2}" text-anchor="middle" '
                f'font-size="9" fill="{color}" font-weight="500">{l}</text>'
                for i, l in enumerate(lines)
            )
            label_svg = (
                f'<rect x="{lx - text_len/2}" y="{box_top}" width="{text_len}" height="{box_h}" '
                f'fill="white" stroke="{color}" stroke-width="0.5" rx="2"/>'
                + texts
            )
        else:
            if isinstance(label, (list, tuple)):
                label = label[0] if label else ""
            # 白塗り背景 rect + ラベルテキスト（既存実装。B7の対象外・変更禁止＝二重着手回避）
            text_len = max(len(label) * 7, 30)
            label_svg = (
                f'<rect x="{lx - text_len/2}" y="{ly - 9}" width="{text_len}" height="14" '
                f'fill="white" stroke="{color}" stroke-width="0.5" rx="2"/>'
                f'<text x="{lx}" y="{ly + 2}" text-anchor="middle" font-size="9" fill="{color}" font-weight="500">{label}</text>'
            )
    return line + "\n" + label_svg


def build_svg(feature_name=None):
    """ipo_flowchart.svg を組み立てる（v30: 全レイアウト座標を screen_config.json から取得）。

    意味合い: 旧版は triggers/fronts/backends/dbs/transition の座標、シナリオ別矢印定義を
              全てこのモジュール内にハードコードしていた。v30 で screen_config.svg_layout に
              全て外出しし、本関数は「JSON から読み取った座標で SVG パーツを組み立てるエンジン」
              の役割に純化。次画面追加時は screen_config の diagrams.<id>.svg_layout に
              レイアウトを書くだけで本関数を再利用できる。
    接続情報: feature_name（2026-07-19 追加）は main() から渡され、get_diagram_spec に転送して
              画面別 screen_config.json（{feature_name}_screen_config.json）を参照する。
              None の場合は get_diagram_spec → load_screen_config 経由で _config_loader.resolve_feature_name が
              環境変数 FEATURE_NAME を解決し、無ければ SystemExit で止まる（2026-10-05: 既定値は廃止）。
    """
    # v30: SVG レイアウトを screen_config.json から取得
    # 2026-07-19: feature_name を転送し、画面別 screen_config.json を参照する
    layout = get_diagram_spec("ipo_flowchart", feature_name)["svg_layout"]
    view_w = layout["view"]["width"]
    view_h = layout["view"]["height"]
    lanes = layout["lanes"]

    trigger_lane = lanes["trigger"]
    front_lane = lanes["front"]
    backend_lane = lanes["backend"]
    db_lane = lanes["db"]
    triggers = layout["triggers"]
    fronts = layout["fronts"]
    backends = layout["backends"]
    dbs = layout["dbs"]
    # 2026-07-19: transition（出力ノード）は screen_config.json 側の静的定義データとして
    # 画面によって元々存在しない（例: マスタ画面・検索画面・詳細画面には「次画面への遷移」概念を持たないものがある）。
    # Orchestrator確認済み: add_diagrams_to_intermediate.py側のデータ欠落バグではなく、画面ごとの
    # 設計差異。.get() で None 許容にし、以降の coords 組立・出力ノード描画を条件分岐でスキップする。
    transition_layout = layout.get("transition")
    scenarios = layout["scenarios"]

    # 中心座標を辞書化（矢印の起点/終点に使う）— v30: JSON 経由で取得したレイアウトから動的計算
    coords = {}
    for t in triggers:
        coords[t["id"]] = {
            "cx": t["x"] + trigger_lane["node_w"]/2,
            "cy_top": trigger_lane["y"],
            "cy_bot": trigger_lane["y"] + trigger_lane["node_h"]
        }
    for f in fronts:
        coords[f["id"]] = {
            "cx": f["x"] + front_lane["node_w"]/2,
            "cy_top": front_lane["y"],
            "cy_bot": front_lane["y"] + front_lane["node_h"]
        }
    for b in backends:
        coords[b["id"]] = {
            "cx": b["x"] + backend_lane["node_w"]/2,
            "cy_top": backend_lane["y"],
            "cy_bot": backend_lane["y"] + backend_lane["node_h"]
        }
    for d in dbs:
        coords[d["id"]] = {
            "cx": d["x"] + db_lane["node_w"]/2,
            "cy_top": db_lane["y"],
            "cy_bot": db_lane["y"] + db_lane["node_h"]
        }
    # transition が存在する画面のみ coords に登録。存在しない画面では coords["transition"] が
    # 未登録のまま残り、後段の矢印生成ループ（"if from_id not in coords or to_id not in coords: continue"）
    # が transition 宛の矢印を自動的にスキップする（既存ロジックを変更せず安全に無効化できる）。
    if transition_layout:
        coords["transition"] = {
            "cx": transition_layout["x"] + transition_layout["w"]/2,
            "cy_top": transition_layout["y"],
            "cy_bot": transition_layout["y"] + transition_layout["h"]
        }

    # SVG パーツ組み立て
    # v26 修正: 描画順を「背景 → 矢印 → ノード」の3段階に分離。
    # 旧版は parts に背景 rect を含めて先に並べたため、後出しの arrows が前面に来るはずが
    # 結局 parts 全体が後出しになっていて背景 rect が矢印を覆っていた問題への対処。
    bg_parts = []      # 背景・層タイトルなど最背面要素
    node_parts = []    # ノード（矢印の前面に描画する）

    # ヘッダ + 背景（最背面）
    bg_parts.append(f'<rect width="100%" height="100%" fill="{COLOR["bg"]}"/>')

    # 層タイトル（左端ラベル）— v30: lanes 辞書から動的構築（旧版は固定リスト）
    for lane_def in (trigger_lane, front_lane, backend_lane, db_lane):
        y = lane_def["y"] + lane_def["node_h"]/2
        bg_parts.append(f'<text x="14" y="{y + 4}" font-size="10" fill="{COLOR["muted"]}" font-weight="500">{lane_def["title"]}</text>')

    # トリガーノード（pill）
    for t in triggers:
        node_parts.append(pill_node(t["x"], trigger_lane["y"], trigger_lane["node_w"], trigger_lane["node_h"],
                                    COLOR["trigger_fill"], COLOR["border"], t["label"]))

    # フロント処理ノード（focal フラグが付いていれば accent 色枠）
    for f in fronts:
        stroke = COLOR["accent"] if f.get("focal") else COLOR["border"]
        node_parts.append(rect_node(f["x"], front_lane["y"], front_lane["node_w"], front_lane["node_h"],
                                    COLOR["node_fill"], stroke, f["label"]))

    # バックエンド処理ノード
    for b in backends:
        stroke = COLOR["accent"] if b.get("focal") else COLOR["border"]
        node_parts.append(rect_node(b["x"], backend_lane["y"], backend_lane["node_w"], backend_lane["node_h"],
                                    COLOR["backend_fill"], stroke, b["label"]))

    # DB円柱ノード
    for d in dbs:
        stroke = COLOR["accent"] if d.get("focal") else COLOR["border"]
        node_parts.append(cylinder(d["x"], db_lane["y"], db_lane["node_w"], db_lane["node_h"],
                                   COLOR["data_fill"], stroke, d["label_main"], d["label_sub"]))

    # 出力ノード（transition）— transition_layout が None の画面（次画面遷移概念を持たない画面）では描画しない
    if transition_layout:
        node_parts.append(rect_node(transition_layout["x"], transition_layout["y"],
                                    transition_layout["w"], transition_layout["h"],
                                    COLOR["trigger_fill"], COLOR["border"], transition_layout["label"], rx=20))

    # 矢印マーカー定義（4色、画面共通スキンの編集基準）
    markers = []
    for mid, c in [("a_blue", COLOR["link"]),
                   ("a_accent", COLOR["accent"]),
                   ("a_ink", COLOR["scenario3"]),
                   ("a_muted", COLOR["scenario4"])]:
        markers.append(
            f'<marker id="{mid}" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">'
            f'<polygon points="0 0, 8 3, 0 6" fill="{c}"/></marker>'
        )

    # 矢印生成（シナリオ別、v30: JSON から動的構築）
    # 意味合い: scenarios[].color_key で COLOR 辞書を引き、SCENARIO_COLOR_TO_MARKER でマーカー id を引く。
    #          各 arrow の from/to は coords 辞書のノード id 参照。
    #
    # フェーズB施策B7: 同一 from/to ペアの複数 arrows[] エントリを1本のエッジに集約する。
    # 意味合い: あるマスタ画面の ipo_flowchart 図で実証された「ノード数は上限内でもエッジが過密で
    #           ラベルが重なり判読不能」問題への対応。同一DB対象への複数操作（例: 同じテーブルへの
    #           SELECT と UPSERT）は from/to 座標が完全一致するため、従来ロジックでは矢印線が
    #           2本重なって描画され、ラベルの白背景ボックスも同じ位置に重複していた。
    #           まず (from, to) をキーに全シナリオの arrows を集約し、その後1エッジ＝1矢印として
    #           描画する。色/マーカーは当該エッジに最初に出現したシナリオのものを採用（複数シナリオが
    #           同一エッジを共有するのは想定外だが、安全側でフォールバック）。
    # 接続情報: 呼出先は arrow()（101行付近）。白背景ボックスの単一行描画実装（既存、変更禁止）は
    #           そのまま利用し、ラベルが2件以上のときのみ list を渡して縦積み表示を発火させる。
    edge_groups = {}   # (from_id, to_id) -> {"color", "marker_id", "dashed", "labels"}
    edge_order = []    # 初出順（描画順の安定化に使う）
    for scenario in scenarios:
        color_key = scenario.get("color_key", "ink")
        color = COLOR.get(color_key, COLOR["ink"])
        marker_id = SCENARIO_COLOR_TO_MARKER.get(color_key, "a_ink")
        for a in scenario.get("arrows", []):
            from_id, to_id = a["from"], a["to"]
            if from_id not in coords or to_id not in coords:
                continue
            key = (from_id, to_id)
            if key not in edge_groups:
                edge_groups[key] = {
                    "color": color,
                    "marker_id": marker_id,
                    "dashed": a.get("dashed", False),
                    "labels": [],
                }
                edge_order.append(key)
            group = edge_groups[key]
            # 統合対象のいずれかが dashed 指定なら、集約後のエッジも点線で表示する
            group["dashed"] = group["dashed"] or a.get("dashed", False)
            label = a.get("label")
            if label and label not in group["labels"]:
                group["labels"].append(label)

    arrows = []
    for key in edge_order:
        from_id, to_id = key
        from_node = coords[from_id]
        to_node = coords[to_id]
        group = edge_groups[key]
        # ラベル0件: None（従来通り）/ 1件: 単一文字列（従来通り）/ 2件以上: list のまま渡し
        # arrow() 内の B7 新設分岐（縦積み表示）を発火させる。
        if not group["labels"]:
            label = None
        elif len(group["labels"]) == 1:
            label = group["labels"][0]
        else:
            label = group["labels"]
        arrows.append(arrow(
            from_node["cx"], from_node["cy_bot"],
            to_node["cx"],   to_node["cy_top"],
            group["color"], group["marker_id"],
            label=label,
            dashed=group["dashed"]
        ))

    # 凡例（下部、ノードと同層）— v30: scenarios リストから動的構築
    legend_y = view_h - 40
    lg_x = 40
    for scenario in scenarios:
        label = scenario.get("label", scenario.get("id", ""))
        color_key = scenario.get("color_key", "ink")
        color = COLOR.get(color_key, COLOR["ink"])
        node_parts.append(f'<line x1="{lg_x}" y1="{legend_y}" x2="{lg_x + 30}" y2="{legend_y}" stroke="{color}" stroke-width="2"/>')
        node_parts.append(f'<text x="{lg_x + 36}" y="{legend_y + 4}" font-size="11" fill="{COLOR["ink"]}">{label}</text>')
        lg_x += 160

    # SVG 全体組み立て
    # 描画順: 背景 → 矢印 → ノード（前面）。
    # 背景 rect は最初に置かないと、後出しの矢印を覆い隠してしまう（v26 で発覚した重ね順バグの対処）
    # v24: <style> タグで font-family を強制注入（SVG 単体抽出時の文字化け対策）
    style_block = '<style>text{font-family:"Noto Sans CJK JP",sans-serif;}</style>'
    svg = (
        f'<svg viewBox="0 0 {view_w} {view_h}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">'
        + style_block
        + '<defs>'
        + "".join(markers)
        + '</defs>'
        + "".join(bg_parts)      # 1. 最背面: 背景 + 層タイトル
        + "".join(arrows)        # 2. 中間: 矢印（ノードと重なる部分はノードで上書きされる）
        + "".join(node_parts)    # 3. 前面: ノード + 凡例
        + '</svg>'
    )
    return svg


def main():
    # 2026-07-19: feature_name をコマンドライン引数/環境変数から解決し、出力パスを画面別に切替える。
    # 意味合い: v30 まで OUTPUT_HTML が機能名の接頭辞なしの固定ファイル名だったため、既定画面以外で
    #           実行しても常に既定画面の出力先を上書きしていた（別画面の {feature_name}_ipo_flowchart.svg が
    #           既定画面データの誤コピーになっていたバグの直接原因）。
    # 接続情報: get_feature_name_from_argv/get_screen_name は _config_loader.py 既存実装。
    # 2026-10-05 汎用化: 「既定の画面だけ接頭辞なし」の分岐を廃止し、全画面で {機能名}_ipo_flowchart.html に統一。
    # 意味合い: 既定の機能名を廃止したため、特別扱いする画面が無くなった。
    # 接続情報: add_diagrams_to_intermediate.py が中間 JSON に書く html/svg/png のパス
    #           （{機能名}_{diagram_id}、diagram_id="ipo_flowchart"）と同じ stem に揃える。
    #           機能名が未指定なら get_feature_name_from_argv が SystemExit で止める。
    feature_name = get_feature_name_from_argv()
    # 目的: 設定の diagrams に ipo_flowchart が無い画面では、図を作らずに正常終了する。
    # 意味合い: IPO 図は任意の図。定義が無いことは誤りではないので、KeyError で止めずに省く旨だけ伝える。
    #           出力先（DIAGRAMS_DIR や html）を作る前に判定し、空の生成物を残さない。
    # 接続情報: get_all_diagram_ids は _config_loader の既存の関数（diagrams のキー一覧を list で返す）。
    #           return で抜けるので、呼出元（__main__）は終了コード 0 で終わる。
    if "ipo_flowchart" not in get_all_diagram_ids(feature_name):
        print(f"SKIP: IPO 図の定義が無いので省く（{feature_name}_screen_config.json の diagrams に ipo_flowchart が無い）")
        return
    output_html = DIAGRAMS_DIR / f"{feature_name}_ipo_flowchart.html"
    svg = build_svg(feature_name)
    screen_name = get_screen_name(feature_name)
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>IPO データフロー図（物理テーブル別、{screen_name}）</title>
<style>
  body {{ margin: 0; background: #f0eee8; font-family: "Noto Sans CJK JP", sans-serif; }}
  .frame {{ max-width: 1100px; margin: 24px auto; background: #faf7f2; padding: 16px; border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,0.05); }}
  svg {{ width: 100%; height: auto; display: block; }}
</style>
</head>
<body>
  <div class="frame">{svg}</div>
</body>
</html>
"""
    DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)
    output_html.write_text(html, encoding="utf-8")
    # v30: DB ノード件数とシナリオ件数を screen_config から動的に表示
    # 2026-07-19: feature_name を転送し、画面別 screen_config.json を参照する
    layout = get_diagram_spec("ipo_flowchart", feature_name)["svg_layout"]
    print(f"OK: {output_html} ({len(html.encode('utf-8'))} bytes)")
    print(f"  DB nodes: {len(layout['dbs'])} 物理テーブル (" + " / ".join(d["id"] for d in layout["dbs"]) + ")")
    print(f"  Scenarios: {len(layout['scenarios'])} (" + " / ".join(s.get("label", s["id"]) for s in layout["scenarios"]) + ")")


if __name__ == "__main__":
    main()
