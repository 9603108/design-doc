#!/usr/bin/env python3
"""
目的: 生成済み .docx 自体の構造を検査し、レイアウト起因の可読性問題を機械検出する。
意味合い: A8（2026-07-18）新設。品質を人の目視でなく機械ゲートで
          担保するための検証。A1〜A6の改修に対するリグレッション検知装置として、
          docx生成後・ユーザー送付前に必ず実行する（SKILL.md Phase 7.5）。
接続情報: 入力 = target/output/{機能名}.docx（Packerで生成された直後のファイル、コマンドライン引数で指定）
          仕様根拠 = 14_チェックリスト.md「docx レイアウト検証」節
標準ライブラリのみ使用（zipfile + xml.etree.ElementTree）、新規依存ゼロ。画面固有情報を一切持たない汎用スクリプト。
"""
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"

EMU_PER_DXA = 635  # 1 dxa (twip, 1/20 pt) = 635 EMU


def w(tag):
    return f"{{{W_NS}}}{tag}"


def wp(tag):
    return f"{{{WP_NS}}}{tag}"


def load_docx_parts(docx_path):
    with zipfile.ZipFile(docx_path) as z:
        names = set(z.namelist())
        parts = {}
        for name in ("word/document.xml", "word/settings.xml", "word/comments.xml",
                     "word/footnotes.xml", "word/endnotes.xml"):
            if name in names:
                parts[name] = z.read(name).decode("utf-8")
    return parts


def get_section_content_width(sect_pr):
    """本文幅(dxa) = pgSz.w - pgMar.left - pgMar.right"""
    pg_sz = sect_pr.find(w("pgSz"))
    pg_mar = sect_pr.find(w("pgMar"))
    if pg_sz is None or pg_mar is None:
        return None
    page_width = int(pg_sz.get(w("w")))
    left = int(pg_mar.get(w("left")))
    right = int(pg_mar.get(w("right")))
    return page_width - left - right


def check_image_widths(root, content_width_dxa):
    issues = []
    if content_width_dxa is None:
        return issues
    content_width_emu = content_width_dxa * EMU_PER_DXA
    for drawing in root.iter(w("drawing")):
        extent = drawing.find(f".//{wp('extent')}")
        if extent is None:
            continue
        cx = int(extent.get("cx"))
        docpr = drawing.find(f".//{wp('docPr')}")
        name = docpr.get("name") if docpr is not None else "?"
        if cx > content_width_emu:
            over_cm = (cx - content_width_emu) / 360000
            issues.append(("ERROR", f"画像 '{name}' の表示幅が本文幅を約{over_cm:.1f}cm 超過している"))
    return issues


def check_table_widths(root, content_width_dxa):
    issues = []
    if content_width_dxa is None:
        return issues
    for tbl in root.iter(w("tbl")):
        grid = tbl.find(w("tblGrid"))
        if grid is None:
            continue
        cols = grid.findall(w("gridCol"))
        col_count = len(cols)
        total_width = sum(int(c.get(w("w")) or 0) for c in cols)
        if col_count >= 7 and total_width > content_width_dxa:
            issues.append(("WARN",
                f"列数{col_count}の表が本文幅を超過（表幅{total_width}dxa > 本文幅{content_width_dxa}dxa）。"
                f"横向きセクション化（A6）の候補"))
    return issues


def check_bookmarks_and_hyperlinks(root):
    issues = []
    bookmark_names = [b.get(w("name")) for b in root.iter(w("bookmarkStart"))]
    dup = sorted({n for n in bookmark_names if bookmark_names.count(n) > 1})
    if dup:
        issues.append(("ERROR", f"Bookmark 名が重複している: {dup}"))
    bookmark_set = set(bookmark_names)
    for hl in root.iter(w("hyperlink")):
        anchor = hl.get(w("anchor"))
        if anchor and anchor not in bookmark_set:
            issues.append(("ERROR", f"InternalHyperlink が参照する anchor='{anchor}' に対応する Bookmark が存在しない"))
    return issues


def check_toc_update_fields(settings_xml):
    issues = []
    if settings_xml is None:
        issues.append(("INFO", "settings.xml が存在しない（TOC自動更新設定を確認できない）"))
        return issues
    if "updateFields" not in settings_xml:
        issues.append(("WARN", "TableOfContents の updateFields が設定されていない。Word で開いた直後は手動更新(F9)が必要"))
    return issues


def check_empty_template_parts(parts):
    issues = []
    for name in ("word/comments.xml", "word/footnotes.xml", "word/endnotes.xml"):
        xml_text = parts.get(name)
        if xml_text is None:
            continue
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            continue
        if len(list(root)) == 0:
            issues.append(("INFO", f"{name} の中身が空（テンプレート残骸、実害なし）"))
    return issues


def main():
    if len(sys.argv) < 2:
        print("Usage: verify_docx_layout.py <docx_path>", file=sys.stderr)
        sys.exit(2)
    docx_path = Path(sys.argv[1])
    if not docx_path.exists():
        print(f"FATAL: {docx_path} が存在しない", file=sys.stderr)
        sys.exit(1)

    parts = load_docx_parts(docx_path)
    doc_xml = parts.get("word/document.xml")
    if doc_xml is None:
        print("FATAL: word/document.xml が見つからない", file=sys.stderr)
        sys.exit(1)
    root = ET.fromstring(doc_xml)

    sect_prs = list(root.iter(w("sectPr")))
    content_width = get_section_content_width(sect_prs[0]) if sect_prs else None

    all_issues = []
    all_issues.extend(check_image_widths(root, content_width))
    all_issues.extend(check_table_widths(root, content_width))
    all_issues.extend(check_bookmarks_and_hyperlinks(root))
    all_issues.extend(check_toc_update_fields(parts.get("word/settings.xml")))
    all_issues.extend(check_empty_template_parts(parts))

    errors = [i for i in all_issues if i[0] == "ERROR"]
    warns = [i for i in all_issues if i[0] == "WARN"]
    infos = [i for i in all_issues if i[0] == "INFO"]

    print(f"=== docx レイアウト検証: ERROR {len(errors)} 件 / WARN {len(warns)} 件 / INFO {len(infos)} 件 ===")
    for lvl, msg in errors + warns + infos:
        print(f"  [{lvl}] {msg}")

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
