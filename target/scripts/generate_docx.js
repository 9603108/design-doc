/**
 * 目的: 中間JSON（target/intermediate/{機能名}.json）を読み、詳細設計書 .docx を生成する。
 * 意味合い: design-doc スキル Step 4（docx 出力）の実体。16_docx出力ハンドオフ.md のセクション構成 + 公式 docx SKILL.md の Critical Rules を遵守。
 *           画面横断の汎用スクリプト（A1改修、2026-07-18）。対象画面は環境変数 INPUT_JSON / DOCX_OUTPUT_PATH で指定する。
 * 接続情報:
 *   入力: target/intermediate/{機能名}.json（環境変数 INPUT_JSON で指定）
 *   出力: target/output/詳細設計書_{機能名}.docx（環境変数 DOCX_OUTPUT_PATH で指定）
 *   仕様: 16_docx出力ハンドオフ.md / 公式 docx SKILL.md
 *
 * フォーマット憲法 v55.12 準拠 (B4改修・画面遷移図/データフロー図の埋め込み対応: §2.3 screen_transition + §5.6冒頭 data_flow、2026-07-19)
 * 適用憲法: スキルのルート直下の 16_docx出力ハンドオフ.md §20 バージョン管理規約
 *   - 中間 JSON `_meta.format_version` フィールドからバージョンを読み取り、生成 docx の文書のプロパティ（description）に入れる（2026-10-06 から本文には出さない）
 *   - `_meta.format_version` が存在しない場合は FORMAT_CONSTITUTION_VERSION 定数 (本ファイル内) を使う
 *   - docx ファイル名は `_v51.docx` 固定 (互換性ポリシー、リンク切れ防止)。バージョン番号は内部表記でのみ扱う
 */

// =============================================================================
// フォーマット憲法バージョン (v55.12 = 2026-07-19 B4改修・17_構成図生成.md「対象となる図」カタログの
// screen_transition / data_flow 図種を §2.3 画面遷移図 / §5.6冒頭 データフロー図として docx へ反映した時点。
// 既存4図種（process_structure/screen_structure/ipo_flowchart/derivation_chain）と同一の
// PNG存在確認→埋め込み→buildFigureCaption→FIGURE_READING_NOTESパターンを踏襲。diagrams[]に該当
// エントリが無い画面（screen_transition や data_flow を定義していない画面）では
// 図ブロック自体を出力しない)
// 中間 JSON `_meta.format_version` が無い場合のフォールバック値として使用される
// 接続情報: 16_docx出力ハンドオフ.md §20 バージョン管理規約 / §9 表現揺れ防止辞書 / §23 図ガバナンス /
//          §21 視覚表現規則 / §25 表の高度化ルール（2階層ヘッダー・縦セル結合・表キャプション） /
//          17_構成図生成.md「対象となる図」カタログ（screen_transition/data_flow 行）
// =============================================================================
const FORMAT_CONSTITUTION_VERSION = "v55.12";

const fs = require('fs');
const path = require('path');
const {
  Document, Packer, Paragraph: _OriginalParagraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, PageOrientation,
  TabStopType, TabStopPosition,
  TableOfContents, HeadingLevel, BorderStyle, WidthType, ShadingType,
  VerticalAlign, PageNumber, PageBreak, LevelFormat, ImageRun,
  Bookmark, InternalHyperlink,
  // B2（2026-07-19）: buildTable() の縦セル結合（opts.verticalMergeCol）で使う列挙値。
  // 意味合い: docx@9.6.1 の TableCellProperties は options.verticalMerge に
  //          VerticalMergeType.RESTART('restart') / CONTINUE('continue') を渡すと
  //          <w:vMerge w:val="restart|continue"/> を出力する（node_modules/docx/dist/index.d.ts で確認済）。
  // 接続情報: 呼出元 = buildTable() 内の縦結合セル生成ロジック
  VerticalMergeType
} = require('docx');

// =============================================================================
// 定数定義
// =============================================================================

// 2026-10-05 汎用化: 入力 JSON / 出力 docx は環境変数 INPUT_JSON / DOCX_OUTPUT_PATH で必ず指定する（既定パスは持たない）
// 目的: 1 つの generate_docx.js を画面ごと・プロジェクトごとに使い回す（複数画面の docx 並列生成にも使う）
// 意味合い: 特定環境の絶対パスを既定値に持つと、未指定のまま別画面の JSON を黙って処理してしまうため、未指定は Error で止める
// 接続: 呼出元 = Phase 7 docx 出力（SKILL.md Phase 7 / 16_docx出力ハンドオフ.md）
//       INPUT_JSON の参照先 = loadProjectConfig()（同じディレクトリの project_config を読む）/ main()
//       OUTPUT_DOCX の参照先 = main() の書き出し
if (!process.env.INPUT_JSON) {
  throw new Error('環境変数 INPUT_JSON 未指定: 中間 JSON（target/intermediate/{機能名}.json）のパスを指定してください');
}
if (!process.env.DOCX_OUTPUT_PATH) {
  throw new Error('環境変数 DOCX_OUTPUT_PATH 未指定: 出力する .docx のパスを指定してください');
}
const INPUT_JSON = process.env.INPUT_JSON;
const OUTPUT_DOCX = process.env.DOCX_OUTPUT_PATH;

// =============================================================================
// id_scheme.json ロード（v48）
// =============================================================================

/**
 * 目的: スキル付属の id_scheme.json をロードし、各カテゴリの header_self / header_ref を表ヘッダ生成に使えるようにする。
 * 意味合い: v47 までは「No」「Step」「エリア#」「イベントNo」を generate_docx.js 内で固定文字列としてハードコードしていたが、
 *           v48 で id_scheme.json の各カテゴリに header_self（自表 ID 列）と header_ref（他カテゴリ参照列）を追加し、
 *           本関数経由でロードすることで「別案件で表記を変更したい時は id_scheme.json 1 ファイルを書き換えれば足りる」を実現。
 * 接続情報: 入力 = スキルのルート直下の id_scheme.json（本ファイルから2階層上。2026-10-05 汎用化で絶対パスの直書きをやめた）
 *           参照元 = getIdHeaderLabel() / 各 sectionXxx() の表ヘッダ生成箇所
 *           仕様根拠 = id_scheme.json._header_label_design
 */
const ID_SCHEME_PATH = path.join(__dirname, '..', '..', 'id_scheme.json');
let _ID_SCHEME_CACHE = null;
function loadIdScheme() {
  if (_ID_SCHEME_CACHE) return _ID_SCHEME_CACHE;
  try {
    const raw = fs.readFileSync(ID_SCHEME_PATH, 'utf-8');
    _ID_SCHEME_CACHE = JSON.parse(raw);
  } catch (e) {
    console.warn(`[warn] id_scheme.json ロード失敗 (${ID_SCHEME_PATH}): ${e.message}. ハードコードヘッダにフォールバック`);
    _ID_SCHEME_CACHE = { categories: [] };
  }
  return _ID_SCHEME_CACHE;
}

/**
 * 目的: id_scheme.json の指定カテゴリの表ヘッダラベルを返す。
 * 意味合い: mode='self' = 自カテゴリの ID 列ヘッダ（例: §4.3 画面項目表の『No』、§6.7 観測点ログ表の『Step』）。
 *           mode='ref'  = 他カテゴリから参照される時のヘッダ（例: §4.3 画面項目表の『イベントNo』列、§5.1 トリガー表の『エリア#』列）。
 * 接続情報: 呼出元 = sectionScreenLayout / sectionEvents / sectionProcessDetails 等の表ヘッダ生成箇所
 *           仕様根拠 = id_scheme.json categories[].header_self / header_ref
 * フォールバック: id_scheme.json に該当キーがない場合は fallback 引数を返す（後方互換維持）
 */
function getIdHeaderLabel(key, mode = 'self', fallback = 'No') {
  const scheme = loadIdScheme();
  const cats = (scheme && scheme.categories) || [];
  const cat = cats.find(c => c && c.key === key);
  if (!cat) return fallback;
  if (mode === 'self') return cat.header_self || fallback;
  if (mode === 'ref') return cat.header_ref || cat.header_self || fallback;
  return fallback;
}

/**
 * 目的: id_scheme.json から指定カテゴリの prefix（2 文字接頭辞）を取得する。v50.5 で新設。
 * 意味合い: 「F001〜の連番ID」「→ B00X」のような旧形式ハードコードを排除し、id_scheme.json
 *           の prefix 変更だけで全 docx 出力テキストが追従する単一拘束点を提供する。
 *           例: getCategoryPrefix('front_process') === 'FR'、getCategoryPrefix('backend_process') === 'BE'
 * 接続情報: 呼出元 = sectionFrontProcesses / sectionBackendProcesses 等の説明文 p() 内
 *           getIdHeaderLabel は表ヘッダラベル取得用、本関数は説明文中の動的接頭辞取得用（用途分離）
 * 仕様根拠: id_scheme.json categories[].prefix
 * フォールバック: 該当キーがない場合は fallback 引数を返す（後方互換維持、v50.5 以前の挙動と整合）
 */
function getCategoryPrefix(key, fallback = '') {
  const scheme = loadIdScheme();
  const cats = (scheme && scheme.categories) || [];
  const cat = cats.find(c => c && c.key === key);
  if (!cat) return fallback;
  return cat.prefix || fallback;
}

// A4縦サイズ（DXA単位、1440 DXA = 1 inch）
const PAGE_WIDTH = 11906;
const PAGE_HEIGHT = 16838;
// A6（2026-07-19）: 余白を 360→720 DXA（0.5inch、Word「狭い」プリセット相当）に回復。
// 経緯: v50.5 で「§6.3/§6.5 表がページ幅を超えてはみ出す」対症療法として 720→360 に圧縮したが、
// 印刷時の非印字域を侵す狭さだった（調査1指摘）。広幅表（列数7以上）は根治策として横向き
// セクション（A6, LANDSCAPE）に分離し、余白圧縮に頼らない。
const MARGIN = 720;
const CONTENT_WIDTH = PAGE_WIDTH - MARGIN * 2; // 10466 DXA

// v34: 段落番号ごとのインデント幅（1階層あたり、240 dxa = 8 pt = 1文字分）
// 意味合い: H2=0, H3=240, H4=480, H5=720。本文 paragraph と表もこの値に追従させ、視覚的階層感を出す。
//          レビュー指摘「段落番号ごとにインデント入れて読みやすく」への対応（v34）。
const INDENT_PER_LEVEL = 240;
// h*() を呼ぶたびに更新され、後続の p() / buildTable() が参照するモジュールスコープ変数。
// 並列化不可（generate_docx.js は単一プロセス1回実行なので問題なし）。
let currentIndent = 0;

// =============================================================================
// A2（2026-07-18）: ID相互参照リンク化（ナビゲーション網）
// =============================================================================

/**
 * 目的: 本文・見出し中に現れる [FR14] 等の ID 参照のうち、実在する処理系 ID の集合を事前収集する。
 * 意味合い: main() 冒頭で1回だけ実行し、以降の見出し Bookmark 化・本文中 ID の InternalHyperlink 化の
 *           判定に使う。見出し生成の実行順序（§4トリガーが§5処理詳細より先に出力される等）に関わらず
 *           全 ID を事前に把握しておくことで、参照が定義より前に出現してもリンク化できる。
 * 接続情報: 呼出元 = main() / 参照先 = headingChildren() / linkifyIdRefs()
 */
function collectAllProcessIds(data) {
  const ids = new Set();
  const collectFrom = (arr, ...fields) => {
    (arr || []).forEach(item => {
      for (const f of fields) {
        if (item && item[f]) { ids.add(String(item[f])); return; }
      }
    });
  };
  // 注意（A2, 2026-07-18）: validations[] / messages[] は個別見出し(h3〜h6)を持たず表1本で
  // 完結するカテゴリのため、意図的に収集対象から外している。Bookmark を打つ見出しが存在しない
  // カテゴリを含めると、本文・巻末索引からのリンクが「存在しない Bookmark を指す」壊れたリンクになる
  // （verify_docx_layout.py の bookmark 存在チェックで実際に検出・是正した）。
  collectFrom(data.front_processes, 'process_id');
  collectFrom(data.backend_processes, 'process_id');
  collectFrom(data.calculations, 'no', 'id');
  collectFrom(data.common_logic, 'no', 'id');
  collectFrom(data.common_logging_processes, 'process_id', 'no', 'id');
  return ids;
}

// collectAllProcessIds() の結果を main() 冒頭で格納するモジュールスコープ変数。
// bookmarkedIds は「実際に Bookmark を打ったか」の記録（同一 ID の見出しが複数回出現した場合の重複防止用）。
let _allKnownIds = new Set();
const _bookmarkedIds = new Set();

const HEADING_ID_PATTERN = /\[([A-Z]{2}\d+(?:-[\w]+)?)\]/;
const ID_REF_PATTERN = /\[([A-Z]{2}\d+(?:-[\w]+)?)\]/g;

/**
 * 目的: 見出しテキストから [FR14] 等の ID を検出し、Bookmark（内部リンクの着地点）でラップする。
 * 意味合い: 呼出元の h3〜h6() は「[FR14] データを確定する」形式のテキストを渡すだけでよく、
 *           個別に ID を意識する必要がない（既存呼出し箇所を変更しない単一拘束点設計）。
 *           ID が見つからない見出し（章見出し等）や、既に Bookmark 済みの ID（response_mapping 等で
 *           同じ ID が複数回見出しに現れるケース）はプレーンテキストのまま返す。
 */
function headingChildren(text) {
  const m = text.match(HEADING_ID_PATTERN);
  if (!m || !_allKnownIds.has(m[1]) || _bookmarkedIds.has(m[1])) {
    return [new TextRun({ text })];
  }
  _bookmarkedIds.add(m[1]);
  return [new Bookmark({ id: m[1], children: [new TextRun({ text })] })];
}

/**
 * 目的: 本文・表セル中の [FR14] 等の ID 参照を、実在する Bookmark への InternalHyperlink に変換する。
 * 意味合い: §5.1.5.3 のような章番号手打ち参照はズレるが、ID 参照は文書内で一意なため
 *           InternalHyperlink 化すればクリックジャンプで確実に該当見出しへ辿り着ける。
 * 接続情報: 呼出元 = splitByPeriod()（p() / tdCell() が内部で使用）
 */
function linkifyIdRefs(text, runOpts) {
  if (!text || !ID_REF_PATTERN.test(text)) return [new TextRun({ text, ...runOpts })];
  ID_REF_PATTERN.lastIndex = 0;
  const parts = [];
  let lastIndex = 0;
  let m;
  while ((m = ID_REF_PATTERN.exec(text)) !== null) {
    if (m.index > lastIndex) {
      parts.push(new TextRun({ text: text.slice(lastIndex, m.index), ...runOpts }));
    }
    const id = m[1];
    if (_allKnownIds.has(id)) {
      parts.push(new InternalHyperlink({
        anchor: id,
        children: [new TextRun({ text: m[0], ...runOpts, color: '1D4ED8', underline: {} })]
      }));
    } else {
      parts.push(new TextRun({ text: m[0], ...runOpts }));
    }
    lastIndex = m.index + m[0].length;
  }
  if (lastIndex < text.length) {
    parts.push(new TextRun({ text: text.slice(lastIndex), ...runOpts }));
  }
  return parts.length > 0 ? parts : [new TextRun({ text, ...runOpts })];
}

// v35 fix: `new Paragraph({...})` を直接呼んでいる箇所（【リクエスト仕様】【内部処理】【レスポンス仕様】等 20+ 箇所）が
//          currentIndent を反映しないという指摘を受け、Paragraph コンストラクタをクラスラッパーに置換。
//          indent が明示指定されていない場合のみ currentIndent を自動付与。明示指定（{ left: ... } 等）は尊重する。
// 意味合い: 既存コードの個別 Edit を回避しつつ、全 Paragraph 生成箇所で階層インデントが効くようにする「単一拘束点」設計。
class Paragraph extends _OriginalParagraph {
  constructor(opts = {}) {
    if (opts && opts.indent === undefined) {
      super({ ...opts, indent: { left: currentIndent } });
    } else {
      super(opts);
    }
  }
}

// 本文フォント: Meiryo UI（密度重視・Windows標準）、フォールバック Meiryo / MS Gothic
// 2026-10-05 汎用化: const → let。プロジェクトごとにフォントを変えられるよう、main() が
//   loadProjectConfig の直後に project_config の docx_output.font で上書きする（未設定ならこの既定値のまま）。
//   モジュール先頭の文では使わず、関数内でだけ参照すること（main() の代入より前に評価されると既定値で固定される）。
let FONT_BODY = 'Meiryo UI';
const FONT_MONO = 'Consolas';

// キャッシュ層の呼び名（cache_op の参照文字列「→ {呼び名} {操作} {キー} (TTL: …)」の先頭に出す語）
// 2026-10-05 汎用化:
//   目的: キャッシュ製品の名前をスクリプトに直書きしない。既定は製品に依らない「キャッシュ」。
//   意味合い: 製品名で書き分けたいプロジェクトは project_config の docx_output.cache_label に製品名を入れる。
//   接続情報: main() が loadProjectConfig の直後に上書きする（FONT_BODY と同じ持ち方）。
//             sectionBackendProcesses の cache_op の参照文字列が関数内で参照する。
let CACHE_LABEL = 'キャッシュ';

// フォントサイズ（half-points: 16 = 8pt、密度UP用にデフォルト8pt採用）
// 2026-10-05 汎用化: const → let。main() が project_config の docx_output.font_size_pt（pt 単位）を
//   half-points（pt * 2）に直して上書きする。未設定なら 16（8pt）のまま。
//   効くのは本文（SIZE_BODY）だけで、見出し（SIZE_H1〜H6）・SIZE_TABLE_HEADER・SIZE_MONO は追従しない。
let SIZE_BODY = 16;
const SIZE_MONO = 18;        // 9pt
const SIZE_H1 = 36;          // 18pt
const SIZE_H2 = 28;          // 14pt
const SIZE_H3 = 24;          // 12pt
const SIZE_H4 = 20;          // 10pt（v17 で追加。§6 配下の §6.X.N 用）
// A5（2026-07-18）: H5/H6 は旧版で本文と同じ SIZE_BODY(8pt) だったため、見出しなのに本文と区別が
// つかない問題があった（改善計画の調査で判明）。本文比 +0.5pt に留め、密度重視の設計思想は維持する。
const SIZE_H5 = 17;          // 8.5pt
const SIZE_H6 = 17;          // 8.5pt
const SIZE_TABLE_HEADER = 17; // 8.5pt（表ヘッダー行、本文8ptとの微差用）

// A5（2026-07-18）: 見出し階層を色でも表現する（承認カラーパレット内、blue系=見出し／gray系=テキストの用途区分に準拠）。
// H1-H3 は青系の濃淡、H4-H6 はグレー系で「大分類/カタログ内訳」の違いを示す。
const COLOR_H1 = '1E3A8A'; // blue-900
const COLOR_H2 = '1E40AF'; // blue-800
const COLOR_H3 = '1D4ED8'; // blue-700
const COLOR_H4 = '374151'; // gray-700
const COLOR_H5 = '374151'; // gray-700
const COLOR_H6 = '374151'; // gray-700

// 罫線・色
const HEADER_FILL = 'DBEAFE';  // blue-100（承認カラー）

// A4（2026-07-18）: 罫線階層 — 内側は薄いグレー、ヘッダー行下端だけ濃色で強調する。
// 意味合い: 旧版は全罫線が同一(1pt CCCCCC)で表の中身が単調だった問題への対応（承認パレット内、gray系）。
const BORDER_COLOR_INNER = 'D1D5DB';   // gray-300 相当、0.5pt
const BORDER_COLOR_HEADER_BOTTOM = '475569'; // slate-600 相当、ヘッダー下端の強調線（1.5pt）

// A4（2026-07-18）: 行の意味的な背景色（承認パレット内）。表の「種別」列の値に応じて行全体を淡色で塗る。
// 1 表につき意味色は 3 色までという上限を 16_docx出力ハンドオフ.md に明記（色の意味過多を防ぐ）。
const FILL_ERROR = 'FEF2F2';   // red-50
const FILL_WARNING = 'FEFCE8'; // yellow-50
const FILL_HIGHLIGHT = 'FEFCE8'; // yellow-50（列定義表の変換ロジック行強調にも流用）

// =============================================================================
// ヘルパー関数
// =============================================================================

/**
 * 標準罫線オブジェクト（全方向同一）
 * 意味合い: 表セル全体に細い灰色ボーダーを引く（読みやすさ重視）
 */
// A4（2026-07-18）: 罫線を旧 size:1（0.125pt、極細）→ size:4（0.5pt、gray-300）に変更。
// 意味合い: ヘッダー下端（headerBottomBorder, 1.5pt）との太さの差で階層を視認できるようにする。
const border = { style: BorderStyle.SINGLE, size: 4, color: BORDER_COLOR_INNER };
const cellBorders = { top: border, bottom: border, left: border, right: border };

/**
 * A4（2026-07-18）: 罫線階層 — ヘッダーセル専用ボーダー（下端だけ濃色・太めにして本文行との境界を強調）
 * 意味合い: 旧版は全罫線が同一で、369表すべてが同じ見た目になり単調だった問題への対応。
 *          データセル同士の境界（内側）は薄いグレーのまま、ヘッダー/ボディの区切りだけ視認できるようにする。
 */
const headerBottomBorder = { style: BorderStyle.SINGLE, size: 12, color: BORDER_COLOR_HEADER_BOTTOM }; // 1.5pt
const headerCellBorders = { top: border, bottom: headerBottomBorder, left: border, right: border };

/**
 * セル内マージン（DXA）
 * 意味合い: 読みやすい余白を全セルに統一適用
 * v50.5 phase 2.5.1: 左右マージンを 120→80 に縮小。長文セルでページ幅オーバー感を緩和する。
 *                    （レビュー指摘「§6.3 / §6.5 表がページ幅を超えてはみ出している」対応）
 */
const cellMargins = { top: 80, bottom: 80, left: 80, right: 80 };

/**
 * 業務語彙の最終チェック: 旧システムの物理名が残っていないかを軽くサニタイズ
 * 接続情報: 16_docx出力ハンドオフ.md「業務語彙の最終確認」セクション
 */
// 2026-10-05 汎用化: 禁止語（設計書に出してはいけない旧物理名）の一覧。
//   目的: 特定プロジェクトの旧名を本ファイルに直書きしない。
//   接続情報: main() が loadProjectConfig の直後に、project_config の forbidden_terms_map.terms
//             （{旧名: 業務語彙} の dict）のキーを代入する。キーが無ければ空のままで、置換は行わない。
//             読むのは直下の sanitizeBusinessTerms。
let FORBIDDEN_TERMS = [];

function sanitizeBusinessTerms(text) {
  if (text == null) return '';
  let s = String(text);
  // 中間JSONの段階で除去されているはずだが、念のため最終チェック
  for (const term of FORBIDDEN_TERMS) {
    // 単純な単語マッチ（大小区別なし）。設定のキーは記号（- など）を含みうるので RegExp 特殊文字をエスケープする
    const re = new RegExp(`\\b${term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'gi');
    s = s.replace(re, '[業務語彙]');
  }
  return s;
}

/**
 * 標準テキスト Run（本文用、Meiryo UI 8pt）
 */
function tr(text, opts = {}) {
  return new TextRun({
    text: sanitizeBusinessTerms(text),
    font: FONT_BODY,
    size: SIZE_BODY,
    ...opts
  });
}

/**
 * テキストを「。」区切りで TextRun 配列に分解する（句点ごとに内部改行を入れる）。
 * 意味合い: ユーザー要望「。 のあと全て改行を入れる」(v13) に対応。docx-js では Paragraph 内の改行は
 *           `new TextRun({ break: 1 })` で実現できるため、句点で分割した各文の間に挟む。
 * 接続情報: p() / tdCell() から呼ばれ、TextRun 配列を返す
 *           sanitizeBusinessTerms（旧システムの物理名フィルタ）も内部で適用
 */
function splitByPeriod(text, runOpts = {}) {
  const base = { font: FONT_BODY, size: SIZE_BODY, ...runOpts };
  if (text == null || text === '') {
    return [new TextRun({ text: '', ...base })];
  }
  const s = sanitizeBusinessTerms(String(text));
  // 「。」の直後で分割（句点は前の文に保持される）
  // 2026-10-06: 「。」の直後が閉じ括弧（）」』)）なら分割しない。
  //   意味合い: 「…してください。」と表示 や（…します。）の閉じ括弧だけが次の行頭に送られていた。
  //   接続情報: 少なくとも p / tdCell / tdCellWithDetails がこの関数を経由するので、全箇所に効く。
  const parts = s.split(/(?<=。)(?![）」』)])/).filter(x => x !== '');
  if (parts.length <= 1) {
    // A2（2026-07-18）: [FR14] 等の ID 参照を InternalHyperlink 化する
    return linkifyIdRefs(s, base);
  }
  const runs = [];
  parts.forEach((part, i) => {
    runs.push(...linkifyIdRefs(part, base));
    if (i < parts.length - 1) {
      // 同一 Paragraph 内で改行（段落分割ではないため間隔が詰まる）
      runs.push(new TextRun({ break: 1, ...base }));
    }
  });
  return runs;
}

/**
 * 本文段落（標準スタイル）
 * v13: テキスト内の「。」毎に内部改行（splitByPeriod）
 */
function p(text, opts = {}) {
  // v34: currentIndent を参照して本文段落にも階層インデントを適用
  return new Paragraph({
    children: splitByPeriod(text, opts.runOpts || {}),
    spacing: { before: 60, after: 60 },
    indent: { left: currentIndent },
    ...(opts.paraOpts || {})
  });
}

/**
 * 見出し H1
 */
function h1(text) {
  // v34: H1 は最上位なのでインデントなし。currentIndent もリセット
  // A5（2026-07-18）: TextRun はテキストのみを持ち、フォント・サイズ・色・太字は
  // styles.paragraphStyles の Heading1 定義に一元化（単一拘束点）。
  currentIndent = 0;
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    // A2（2026-07-18）: [FR14] 等の ID を含む見出しは自動的に Bookmark 化される（headingChildren）。
    children: headingChildren(text),
    spacing: { before: 240, after: 240 },
    indent: { left: 0 }
  });
}

/**
 * 見出し H2（「1.」「2.」等の大セクション）
 * v13: 前に空行を入れて視覚的な区切りを強化（spacing.before を 240 → 720 に拡大）。
 *       ユーザー要望「一番上の項目番号の前に改行を入れて可読性を上げる」への対応。
 */
function h2(text) {
  // v34: H2 = 大セクション、インデントなし。currentIndent リセット
  // v13 ルール: 一番上の段落番号前で改行する（可読性向上、ユーザー要望）
  // v49 fix: spacing.before: 720 だけでは表直後で効きにくいケースがあるため、
  //          break: 1 の TextRun を見出しの前に挿入して強制改行を入れる（レビュー指摘
  //          「2. 改訂履歴 が 1. に続いている」対応）。これで表直後の H2 でも空行が確実に挿入される。
  //          A1調査（2026-07-18）で意図的な実装と確認済み、削除しない（表直後の密着を防ぐ唯一の手段）。
  // A5（2026-07-18）: 見出しテキストの TextRun はテキストのみ、書式は styles.paragraphStyles の
  //          Heading2 定義に一元化。break 用の TextRun は空行の高さ調整のため本文サイズを維持する。
  currentIndent = 0;
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    children: [
      new TextRun({ break: 1, font: FONT_BODY, size: SIZE_BODY }),
      new TextRun({ text })
    ],
    spacing: { before: 720, after: 180 },
    pageBreakBefore: false,
    indent: { left: 0 }
  });
}

/**
 * 見出し H3
 */
function h3(text) {
  // v34: H3 = 1階層インデント（240 dxa）。後続の p()/buildTable() がこの値を参照する
  // A5（2026-07-18）: TextRun はテキストのみ、書式は styles.paragraphStyles の Heading3 定義に一元化。
  currentIndent = INDENT_PER_LEVEL;
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    // A2（2026-07-18）: [FR14] 等の ID を含む見出しは自動的に Bookmark 化される（headingChildren）。
    children: headingChildren(text),
    spacing: { before: 180, after: 120 },
    indent: { left: currentIndent }
  });
}

/**
 * 見出し H4（v17 で追加）
 *
 * 目的: §6 配下の §6.X.N 用（例: §6.10 API仕様 内の各APIエンドポイント §6.10.1〜）。
 * 意味合い: 「§10 計算式 / §11 API仕様 / §12 DB操作 等は処理の一種だから §6 配下にあるべき」というレビュー指摘（v17）により、
 *           各カタログ章を §6 配下のサブセクションとして取り込んだ際、章内のエンドポイント/SQL/計算式単位の見出しを h4 で表現する必要が生じた。
 * 接続情報: 入力 = text / 出力 = Paragraph（HEADING_4）
 *           docx の navigation/TOC は HeadingLevel.HEADING_4 として認識される（TOC range は 1-3 のため TOC には出ない、本文構造のみ）
 */
function h4(text) {
  // v34: H4 = 2階層インデント（480 dxa）
  // A5（2026-07-18）: TextRun はテキストのみ、書式は styles.paragraphStyles の Heading4 定義に一元化。
  currentIndent = INDENT_PER_LEVEL * 2;
  return new Paragraph({
    heading: HeadingLevel.HEADING_4,
    // A2（2026-07-18）: [FR14] 等の ID を含む見出しは自動的に Bookmark 化される（headingChildren）。
    children: headingChildren(text),
    spacing: { before: 120, after: 80 },
    indent: { left: currentIndent }
  });
}

/**
 * 見出し H5（v19 で追加）
 *
 * 目的: §6.1.X.N / §6.2.X.N 用（フロント/バックエンド処理を area でグルーピングし、各処理を H5 で並べる）。
 * 意味合い: レビュー指摘（v19）「§6.1 フロント処理配下にもう1層エリアカテゴリが欲しい」への対応で、
 *           h3=§6.1 / h4=§6.1.X area / h5=§6.1.X.N [F00X] name の3階層構造を実現する。
 * 接続情報: HeadingLevel.HEADING_5 として認識される（TOC range 1-3 には出ない）
 */
function h5(text) {
  // v34: H5 = 3階層インデント（720 dxa）
  // A5（2026-07-18）: 旧版は本文と同じ SIZE_BODY(8pt) で見出しと本文の区別がつかなかった問題を解消。
  //          TextRun はテキストのみ、書式は styles.paragraphStyles の Heading5 定義（8.5pt + gray-700 + 太字）に一元化。
  currentIndent = INDENT_PER_LEVEL * 3;
  return new Paragraph({
    heading: HeadingLevel.HEADING_5,
    // A2（2026-07-18）: [FR14] 等の ID を含む見出しは自動的に Bookmark 化される（headingChildren）。
    children: headingChildren(text),
    spacing: { before: 100, after: 60 },
    indent: { left: currentIndent }
  });
}

/**
 * 見出し H6（v49 で新設、§6.1.X.Y.Z 形式の最深サブ小節用）
 * 意味合い: §6.1 フロント処理の response_mapping kind step ごとに「[BE#] レスポンス → 画面項目マッピング」小節を出すために必要
 * 接続情報: 呼出元 = sectionFrontProcesses の response_mapping 展開ループ
 */
function h6(text) {
  // v49: H6 = 4階層インデント（960 dxa）
  // A5（2026-07-18）: 旧版は本文と同じ SIZE_BODY(8pt) だった問題を解消。TextRun はテキストのみ、
  //          書式は styles.paragraphStyles の Heading6 定義（8.5pt + gray-700 + 太字）に一元化。
  currentIndent = INDENT_PER_LEVEL * 4;
  return new Paragraph({
    heading: HeadingLevel.HEADING_6,
    // A2（2026-07-18）: [FR14] 等の ID を含む見出しは自動的に Bookmark 化される（headingChildren）。
    children: headingChildren(text),
    spacing: { before: 80, after: 50 },
    indent: { left: currentIndent }
  });
}

/**
 * ASCII図（モノスペース）用の段落配列を生成
 * 意味合い: 改行を Paragraph 分割で表現（docx-js の `\n` 禁止ルール）
 */
function asciiBlock(text) {
  if (!text) return [p('（ASCII図なし）')];
  const lines = String(text).split('\n');
  return lines.map(line => new Paragraph({
    children: [new TextRun({ text: line || ' ', font: FONT_MONO, size: SIZE_MONO })],
    spacing: { line: 240, before: 0, after: 0 }
  }));
}

/**
 * 表ヘッダーセル（背景blue-100 + 太字）
 * A5（2026-07-18）: フォントサイズを本文比 +0.5pt（SIZE_TABLE_HEADER）にし、本文セルとの微差で階層を出す。
 * B2（2026-07-19）: opts.columnSpan / opts.rowSpan を追加。
 *   意味合い: buildTable() の2階層ヘッダー機能（columnSpanで列結合、rowSpanで子見出しの無いグループ見出し・
 *            素の文字列見出しを2行目まで縦結合）を実現するための拡張。両方とも省略時は undefined のまま
 *            TableCell に渡り docx 側で無視されるため、単一階層ヘッダーの出力は完全に不変（後方互換）。
 *   接続情報: 呼出元 = buildTable()（単一階層ヘッダー行 / 2階層ヘッダーの1行目・2行目の両方）
 */
function thCell(text, width, opts = {}) {
  return new TableCell({
    // A4（2026-07-18）: ヘッダー下端だけ濃色の headerCellBorders を使い、本文行との境界を強調する。
    borders: headerCellBorders,
    width: { size: width, type: WidthType.DXA },
    shading: { fill: HEADER_FILL, type: ShadingType.CLEAR },
    margins: cellMargins,
    verticalAlign: VerticalAlign.CENTER,
    columnSpan: opts.columnSpan,
    rowSpan: opts.rowSpan,
    // v37 fix: 表セル内の Paragraph は明示的に indent=0 を指定。
    // Paragraph ラッパーが currentIndent を自動付与する仕様（v35）の例外として、
    // 表は既に table 全体がインデント済なのでセル内テキストまでインデントすると二重になり右にずれる。
    children: [new Paragraph({
      children: [new TextRun({ text: sanitizeBusinessTerms(text), font: FONT_BODY, size: SIZE_TABLE_HEADER, bold: true })],
      alignment: AlignmentType.CENTER,
      indent: { left: 0 }
    })]
  });
}

/**
 * 概要 + 詳細配列を bullet 箇条書きで段落として描画 (v51 §19 1 文 1 概念ルール対応)
 *
 * 目的: 中間 JSON の `description / overview / summary / remarks 等 + *_details[]` を
 *       「概要 1 文 + 詳細 bullet 行群」の Paragraph 配列に変換する。
 * 意味合い: v51 §19 記述粒度ルール (1 文 1 概念原則) の docx 描画側機械強制点。
 *           1 行に複数のロジックを詰め込むと読みにくくなるため（§5.5 共通ビジネスロジックで顕著）、
 *           「1 行 1 ステートメント、または概要化」を全体のルールとして描画側で保証する。
 * 接続情報: 入力 = item (中間 JSON のオブジェクト) /
 *           field (概要フィールド名、デフォルト 'description') /
 *           detailsField (詳細配列フィールド名、デフォルト field + '_details') /
 *           labelPrefix (例「概要: 」「備考: 」、空文字なら付けない) /
 *           出力 = Paragraph[] (本文段落 + bullet 段落群)
 *           呼出元 = sectionFrontProcesses / sectionBackendProcesses / sectionCommonLogic /
 *                  sectionValidations / sectionCalculations / sectionDbOperations /
 *                  sectionCommonLoggingProcesses /
 *           中間 JSON 仕様 = 16_docx出力ハンドオフ.md §19
 *           ヘルパー使用 = p() (本文段落) / splitByPeriod() (内部改行)
 */
function renderDescriptionWithDetails(item, field = 'description', detailsField = null, labelPrefix = '') {
  // 入力ガード: item が無い / 文字列フィールドが空なら何も返さない
  if (!item || typeof item !== 'object') return [];
  const text = item[field];
  const dField = detailsField || (field + '_details');
  const details = item[dField];

  const paragraphs = [];
  // 詳細配列がある場合: 概要 (先頭) + 詳細 bullet (2 件目以降)
  if (Array.isArray(details) && details.length > 0) {
    // 1 件目: 概要 (本文段落)
    const summary = (typeof text === 'string' && text.trim() !== '') ? text : details[0];
    if (summary) {
      paragraphs.push(p(`${labelPrefix}${summary}`));
    }
    // 2 件目以降: bullet 箇条書き
    //  意味合い: docx-js の bullet 機能ではなく「・」プレフィックス + インデントで表現 (シンプルかつ表セル内でも使える)
    //  接続情報: detailsField の各要素を「・<内容>」として別段落で出力
    for (let i = 1; i < details.length; i++) {
      const d = details[i];
      if (d && typeof d === 'string' && d.trim() !== '') {
        paragraphs.push(p(`・${d}`));
      }
    }
    return paragraphs;
  }
  // 詳細配列がない場合: 概要のみ (従来通り)
  if (typeof text === 'string' && text.trim() !== '') {
    paragraphs.push(p(`${labelPrefix}${text}`));
  }
  return paragraphs;
}

/**
 * 表セル内に「概要 1 文 + 詳細 bullet 配列」を複数 Paragraph で描画する (v51 §19 対応)
 *
 * 目的: Step 表等の「処理内容」セル内で 1 セルに概要 + bullet 詳細を表現する
 * 意味合い: v51 §19 1 文 1 概念ルールを表セル内にも適用。
 *           step.description + step.description_details[] を「概要行 + ・詳細行群」として
 *           1 つの TableCell 内に複数 Paragraph として展開する。
 * 接続情報: 入力 = item (step オブジェクト) / field (概要フィールド) /
 *                detailsField (詳細配列フィールド、デフォルト field + '_details') /
 *                width (列幅 DXA) /
 *           出力 = TableCell
 *           呼出元 = sectionCommonLogic (processing_steps) /
 *                  sectionFrontProcesses (steps) /
 *                  sectionBackendProcesses (processing) /
 *                  sectionProcessDetails (steps)
 */
function tdCellWithDetails(item, field, width, opts = {}) {
  // 入力ガード
  const text = (item && typeof item === 'object') ? item[field] : null;
  const dField = (item && typeof item === 'object') ? ((opts.detailsField) || (field + '_details')) : null;
  const details = (item && typeof item === 'object' && dField) ? item[dField] : null;

  const paragraphs = [];
  const runOpts = opts.runOpts || {};

  if (Array.isArray(details) && details.length > 0) {
    // 1 件目: 概要 (text 優先、空なら details[0])
    const summary = (typeof text === 'string' && text.trim() !== '') ? text : (details[0] || '');
    paragraphs.push(new Paragraph({
      children: splitByPeriod(summary || ' ', runOpts),
      spacing: { before: 0, after: 0 },
      indent: { left: 0 }
    }));
    // 2 件目以降: bullet 箇条書き
    for (let i = 1; i < details.length; i++) {
      const d = details[i];
      if (d && typeof d === 'string' && d.trim() !== '') {
        paragraphs.push(new Paragraph({
          children: splitByPeriod(`・${d}`, runOpts),
          spacing: { before: 20, after: 0 },
          indent: { left: 0 }
        }));
      }
    }
  } else {
    // 詳細配列なし: 従来通りの単一段落
    const s = String(text == null ? '' : text);
    const lines = s.split('\n');
    lines.forEach((line, idx) => {
      paragraphs.push(new Paragraph({
        children: splitByPeriod(line || ' ', runOpts),
        spacing: { before: idx === 0 ? 0 : 20, after: 0 },
        indent: { left: 0 }
      }));
    });
  }

  return new TableCell({
    borders: cellBorders,
    width: { size: width, type: WidthType.DXA },
    shading: { fill: 'FFFFFF', type: ShadingType.CLEAR },
    margins: cellMargins,
    verticalAlign: VerticalAlign.CENTER,
    children: paragraphs.length > 0 ? paragraphs : [new Paragraph({ children: [new TextRun(' ')], indent: { left: 0 } })]
  });
}

/**
 * 表データセル（標準）
 * B2（2026-07-19）: opts.verticalMerge を追加（'restart' | 'continue'、VerticalMergeType定数を渡す想定）。
 *   意味合い: buildTable() の opts.verticalMergeCol による縦セル結合を実現するための拡張。
 *            結合開始行は restart（実データ表示）、結合継続行は continue（Wordが前行内容を表示するため
 *            本関数側は text=null で空白セルを描画すればよい）を渡す。省略時は undefined のまま
 *            TableCellProperties 側で無視される（`if (options.verticalMerge) {...}` のみ実行）ため、
 *            単独セルの出力は完全に不変（後方互換）。
 *   接続情報: 呼出元 = buildTable()（opts.verticalMergeCol 指定時のみ verticalMerge を渡す）
 */
function tdCell(text, width, opts = {}) {
  // \n で段落分割（既存）に加え、各段落内の「。」毎に内部改行を入れる（v13）
  // 過去経緯: 表セル内の長文（特に処理概要・備考列）が読みにくいフィードバック → 句点改行で1文ごとに視認性UP
  // v37 fix: 表セル内の Paragraph は明示的に indent=0（thCell と同じ理由）
  const lines = String(text == null ? '' : text).split('\n');
  const paragraphs = lines.map((line, idx) => new Paragraph({
    children: splitByPeriod(line || ' ', opts.runOpts || {}),
    spacing: { before: idx === 0 ? 0 : 20, after: 0 },
    indent: { left: 0 }
  }));
  return new TableCell({
    borders: cellBorders,
    width: { size: width, type: WidthType.DXA },
    // A4（2026-07-18）: opts.fill で行単位の意味的背景色を指定可能に（省略時は白、後方互換）。
    shading: { fill: opts.fill || 'FFFFFF', type: ShadingType.CLEAR },
    margins: cellMargins,
    verticalAlign: VerticalAlign.CENTER,
    verticalMerge: opts.verticalMerge,
    children: paragraphs.length > 0 ? paragraphs : [new Paragraph({ children: [new TextRun(' ')], indent: { left: 0 } })]
  });
}

/**
 * 表を構築するヘルパー
 * @param {string[]} headers - ヘッダー行ラベル
 * @param {string[][]} rows - データ行（各行は文字列配列）
 * @param {number[]} colRatios - 列幅比率（合計はCONTENT_WIDTHに対する重み）
 */
/**
 * 中間JSON の logical_names[] から「論理名 → 物理名」の逆引き Map を構築する。
 * 意味合い: DB操作テーブル等で「論理名[物理名]」併記形式を実現する基盤。
 * 接続情報: 入力 = data 全体 / 出力 = { colMap: 論理列→物理列, tableMap: 論理表→物理表 }
 *           呼出元 = main() で1回だけ構築し、各 sectionXxx に渡す
 */
/**
 * PNG ファイルの実寸 (width, height) を取得する。IHDR チャンクから直接読む（軽量）。
 * 意味合い: ImageRun に渡す transformation の height を「PNGの実寸アスペクト比」から計算するため。
 *           docx の transformation で width/height を独自に指定すると元PNGと比率が違って横伸び/縦伸びが起きる。
 * 接続情報: 入力 = PNG絶対パス / 出力 = {width, height} ピクセル
 *           PNG 仕様: バイト 16-19 = width (big-endian uint32), 20-23 = height
 */
function getPngDimensions(filePath) {
  const buf = fs.readFileSync(filePath);
  return {
    width: buf.readUInt32BE(16),
    height: buf.readUInt32BE(20),
  };
}

/**
 * PNG の実寸からアスペクト比を保ったまま、表示幅 px に合わせた height を計算する。
 * A3（2026-07-19）: 本文幅（現在の currentIndent を考慮したページ内印字幅）を px 換算した値を
 * 上限とし、指定 displayWidthPx がそれを超える場合は自動的に縮小する（画像はみ出し防止、
 * 調査で「IPOデータフロー図が本文幅を約6.7cm超過」と判明した問題への対応）。
 * 96dpi 換算: 1 dxa (1/20pt) = 96/1440 px。
 * 接続情報: 入力 = PNGパス + 表示希望幅(px) / 出力 = { width, height } 表示用ピクセル
 */
function calcAspectFit(pngPath, displayWidthPx) {
  const dim = getPngDimensions(pngPath);
  const ratio = dim.height / dim.width;
  const maxWidthPx = Math.floor((CONTENT_WIDTH - currentIndent) * 96 / 1440);
  const width = Math.min(displayWidthPx, maxWidthPx);
  return {
    width,
    height: Math.round(width * ratio),
  };
}

// =============================================================================
// A3（2026-07-19）: 図番号・キャプション・読み方定型文
// =============================================================================

// 章番号ごとの図連番カウンタ（モジュールスコープ、generate_docx.js は単一プロセス1回実行のため問題ない）。
const _figureCounters = {};

/**
 * 目的: 図（ImageRun）の直後に「図2-1 処理構成図」形式のキャプション段落を生成する。
 * 意味合い: 図が本文から独立して見え、参照する手段がなかった問題への対応（調査1・調査3で判明）。
 *           文書は毎回全再生成されるため、Word の SEQ フィールドは使わず章内連番を静的に採番する。
 * 接続情報: 呼出元 = sectionProcessFlow / sectionScreenLayout の各図埋め込み箇所
 */
function buildFigureCaption(chapterNo, title) {
  _figureCounters[chapterNo] = (_figureCounters[chapterNo] || 0) + 1;
  const label = `図${chapterNo}-${_figureCounters[chapterNo]}`;
  const paragraph = new Paragraph({
    children: [new TextRun({ text: `${label} ${title}`, font: FONT_BODY, size: SIZE_BODY, color: '6B7280', italics: true })],
    alignment: AlignmentType.CENTER,
    spacing: { before: 40, after: 120 },
    indent: { left: 0 }
  });
  return { label, paragraph };
}

// 図種別ごとの汎用「読み方」定型文（17_構成図生成.md 図種一覧と対応）。
// 意味合い: 業務担当者が図を見る前に「何が読み取れる図か」を把握できるようにする（外部ベストプラクティス調査対応）。
// B1（2026-07-19）: derivation_chain（計算導出チェーン図）を追加。17_構成図生成.md「derivation_chain の生成条件」参照。
// B4（2026-07-19）: screen_transition（画面遷移図）/ data_flow（データフロー図）を追加。17_構成図生成.md
//  「対象となる図」カタログの当該2行に対応。sectionProcessFlow §2.3 / sectionDbOperations §5.6冒頭から参照される。
const FIGURE_READING_NOTES = {
  process_structure: '上図は画面操作から処理完了までの一連の流れを、開始から終了まで上から下に示します。',
  ipo_flowchart: '上図は画面操作・フロント処理・バックエンド処理・DB操作の対応関係を、シナリオ単位に矢印でまとめたものです。',
  screen_structure: '上図は当画面の論理的なエリア構成（配置ブロック）を示します。実際の画面デザインとは異なる場合があります。',
  derivation_chain: '上図は入力値から最終結果までの計算過程を、各計算式の適用順に矢印で示します。',
  screen_transition: '上図は当画面から遷移可能な画面と、遷移のきっかけとなる画面操作の対応関係を矢印で示します。',
  data_flow: '上図は画面・API・DBテーブルの間でデータがどのように受け渡されるかを、テーブル単位に矢印でまとめたものです。'
};

/**
 * 目的: calculations[]/common_logic[] の各エントリ（no + name）に対応する derivation_chain 図を検索する。
 * 意味合い: B1施策（改善計画 フェーズB、2026-07-19）。17_構成図生成.md で新設された
 *           derivation_chain 図種を §5.4 計算式 / §5.5 共通ビジネスロジックの該当エントリへ埋め込むための
 *           紐付けロジック。図種の判定は id の prefix 一致（`derivation_chain_` で始まるか）のみで行う。
 *           type は他の図種（process_structure 等）と同じ diagram-design レンダリングエンジン種別
 *           （'flowchart'）であり、図種の識別には使わない（15_中間JSONスキーマ.md diagrams[].id
 *           フィールド説明、17_構成図生成.md 生成条件と統一。2026-07-19 の修正で
 *           type='derivation_chain'／id固定値方式との表記不一致を解消）。
 *           1機能内に該当エントリが複数あれば derivation_chain 図も複数生成されうるため、id は
 *           `derivation_chain_{一意な識別子}` 形式で一意化される前提（固定値だと2件目以降が
 *           html_path/svg_path を含めて衝突するため）。本関数は candidates を id prefix で複数件
 *           抽出したうえで、entryNo/entryName で個別エントリに紐付けることで複数図に対応する。
 * 接続情報: 入力 = data.diagrams[] / entryNo（例: "CA3"）/ entryName（計算名・ロジック名）
 *           出力 = 一致した diagram、なければ undefined
 *           呼出元 = sectionCalculations（§5.4）/ sectionCommonLogic（§5.5）
 */
function findDerivationChainDiagram(diagrams, entryNo, entryName) {
  const candidates = (diagrams || []).filter(d => d && typeof d.id === 'string' && d.id.startsWith('derivation_chain'));
  if (candidates.length === 0) return undefined;
  // fix（2026-07-19、Wave2検証で発見）: 旧実装の haystack.includes(entryNo) は部分文字列一致のため、
  // "CA1" が "CA11"/"CA12" 等の部分文字列となり誤埋込を起こしていた（CA1 に CA11 の図が二重埋込される等）。
  // id は build_derivation_chain_spec.py が `derivation_chain_{no.toLowerCase()}` 形式で確定的に生成するため、
  // まず完全一致を試み、フォールバックとしてのみ title を単語境界付き正規表現で照合する。
  if (entryNo) {
    const expectedId = `derivation_chain_${String(entryNo).toLowerCase()}`;
    const exact = candidates.find(d => d.id === expectedId);
    if (exact) return exact;
  }
  const escapeRe = s => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return candidates.find(d => {
    const title = d.title || '';
    if (entryNo) {
      const pat = new RegExp(`(^|[^A-Za-z0-9])${escapeRe(entryNo)}(?![0-9])`);
      if (pat.test(title)) return true;
    }
    if (entryName && title.includes(entryName)) return true;
    return false;
  });
}

/**
 * 目的: derivation_chain 図（計算導出チェーン図）を計算式/共通ロジックの個別小節に埋め込む共通処理。
 * 意味合い: B1施策（改善計画 フェーズB、2026-07-19）。sectionProcessFlow/sectionScreenLayout
 *           と同じ「PNG存在確認 → 埋め込み → buildFigureCaption でキャプション → FIGURE_READING_NOTES で
 *           読み方定型文」パターンを踏襲する（17_構成図生成.md「実装パイプライン」参照）。
 *           生成失敗（PNG不在・読み込みエラー）時は空配列を返すのみとし、呼出元が既に出力済みの
 *           表形式の計算式表示（【入力】【出力】等の既存テキスト表記）だけで完結させる
 *           （フォールバック必須要件、改善計画で明示したリスク対策）。
 * 接続情報: 呼出元 = sectionCalculations（§5.4）/ sectionCommonLogic（§5.5）
 *           入力 = diagram（data.diagrams[] の該当エントリ）/ chapterNo（figureキャプションの章番号、例: '5'）
 *           出力 = Paragraph[]（参照文 + 画像 + キャプション + 読み方定型文）。埋め込み失敗時は空配列。
 */
function buildDerivationChainEmbed(diagram, chapterNo) {
  if (!diagram || !diagram.png_path || !fs.existsSync(diagram.png_path)) return [];
  try {
    const imgData = fs.readFileSync(diagram.png_path);
    const displayWidth = (diagram.embed_size && diagram.embed_size.width_px) || 640;
    const { width: w, height: h } = calcAspectFit(diagram.png_path, displayWidth);
    // B3（2026-07-19）: diagram.caption が指定されていればキャプションのタイトル部分に優先使用し、
    //  未指定なら従来通り diagram.title にフォールバック（15_中間JSONスキーマ.md diagrams[].caption 新設対応）。
    const captionTitle = diagram.caption || diagram.title || '計算導出チェーン図';
    const fig = buildFigureCaption(chapterNo, captionTitle);
    const blocks = [
      p(`計算の導出過程を${fig.label}に示します。`),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new ImageRun({
          type: 'png', data: imgData, transformation: { width: w, height: h },
          altText: {
            title: captionTitle,
            description: `${captionTitle}（入力値から最終結果までの導出過程）`,
            name: diagram.id
          }
        })]
      }),
      fig.paragraph
    ];
    // B3（2026-07-19）: diagram.reading_note が指定されていれば FIGURE_READING_NOTES の汎用文の代わりに使用、
    //  未指定なら従来通り FIGURE_READING_NOTES.derivation_chain にフォールバック。
    const readingNote = diagram.reading_note || FIGURE_READING_NOTES.derivation_chain;
    if (readingNote) {
      blocks.push(p(readingNote));
    }
    return blocks;
  } catch (e) {
    console.warn(`[warn] derivation_chain 図埋め込み失敗 (${diagram.png_path}): ${e.message}. テキスト表記のみで継続`);
    return [];
  }
}

function buildLogicalNameMaps(data) {
  const colMap = new Map();
  const tableMap = new Map();
  for (const ln of ((data && data.logical_names) || [])) {
    if (ln.logical_column && ln.physical_column) {
      colMap.set(ln.logical_column, ln.physical_column);
    }
    if (ln.logical_table && ln.physical_table) {
      tableMap.set(ln.logical_table, ln.physical_table);
    }
  }
  return { colMap, tableMap };
}

/**
 * 論理名を「論理名[物理名]」形式に変換する。
 * 意味合い: ユーザー指示「物理名と論理名を統一して読みやすくする」への対応（16_docx出力ハンドオフ.md 準用）。
 * 接続情報: 入力 = 論理名 + kind('column'|'table') + buildLogicalNameMaps の戻り値
 *           出力 = `論理名[物理名]` 文字列（物理名未登録時は論理名のみ）
 */
function withPhysical(logical, kind, maps) {
  if (!logical) return '';
  if (!maps) return logical;
  const m = kind === 'table' ? maps.tableMap : maps.colMap;
  const phys = m.get(logical);
  return phys ? `${logical}[${phys}]` : logical;
}

/**
 * イベント発生場所の階層配列を「└ 」付き複数行文字列に整形する。
 * 意味合い: §5/§7 イベント表の「発生」列で、画面→領域→要素の階層を視覚的に表示する。
 *           例: ['サンプル画面', 'ワークフローステッパー[5]', '「データ確定」ボタン']
 *               ↓
 *               サンプル画面
 *               └ ワークフローステッパー[5]
 *                 └ 「データ確定」ボタン
 * 接続情報: 入力 = events[].location（convert_events_location_action.py が生成）/ 出力 = 改行区切り文字列
 *           tdCell が `\n` を Paragraph 分割してくれるため、セル内で階層表示される
 */
function formatLocationHierarchy(location) {
  if (!Array.isArray(location) || location.length === 0) return '';
  return location.map((part, idx) => {
    if (idx === 0) return part;
    // 1段目以降は全角スペースでインデント + 「└ 」プレフィックス
    const indent = '　'.repeat(idx - 1);
    return `${indent}└ ${part}`;
  }).join('\n');
}

/**
 * v37: items[].screen_no を screen_layout.areas[].area_no に変換するヘルパー
 *
 * 意味合い: §6.3 初期表示画面項目 や § API レスポンス→画面表示先マッピング で screen_no（1/2/...）を
 *           表示していたが、§4.2/§4.3 では area_no（A1/A2/...）を使っており表記揺れがあった。
 *           v37 で全画面参照を area_no に統一するため、screen_no を area_no に変換する。
 * 接続情報: areas[].screen_no と areas[].area_no は一対一対応（add_screen_areas.py で定義）
 */
function formatAreaNo(screenNo, areas) {
  if (screenNo === undefined || screenNo === null || screenNo === '') return '';
  const sn = String(screenNo);
  const found = (areas || []).find(a => String(a.screen_no) === sn);
  return found ? found.area_no : sn;
}

// =============================================================================
// B2（2026-07-19）: 表番号・キャプション（フェーズB「表の高度化」施策）
// =============================================================================

// 章番号ごとの表連番カウンタ（図のカウンタ _figureCounters とは独立、モジュールスコープ）。
const _tableCounters = {};

/**
 * 目的: 主要一覧表（画面項目一覧・トリガー一覧・メッセージ一覧）の直前に「表3-1 画面項目一覧」形式の
 *       キャプション段落を生成する。
 * 意味合い: buildFigureCaption（図）と同じ課題認識 — 表も本文から独立して見え、章内で「何表目か」を
 *          参照する手段が無かった。JIS X 0111 等の一般的な文書慣行に倣い、表キャプションは表の直前（上）
 *          に置く（図キャプションは画像の直後＝下に置く方針との非対称。16_docx出力ハンドオフ.md §21参照）。
 *          全表（一覧表以外の細かい表も含め文書全体で数百表規模）へ機械的に付与するとノイズになるため、
 *          本関数自体は汎用だが、呼出元は主要一覧表（画面項目一覧・トリガー一覧・メッセージ一覧）のみに
 *          限定して呼び出す方針とする（buildFigureCaption と異なり全箇所への強制ではない）。
 * 接続情報: 呼出元 = sectionScreenLayout（画面項目一覧）/ sectionTriggerGroups（トリガー一覧）/
 *          sectionMessages（メッセージ一覧）の該当箇所
 */
function buildTableCaption(chapterNo, title) {
  _tableCounters[chapterNo] = (_tableCounters[chapterNo] || 0) + 1;
  const label = `表${chapterNo}-${_tableCounters[chapterNo]}`;
  const paragraph = new Paragraph({
    children: [new TextRun({ text: `${label} ${title}`, font: FONT_BODY, size: SIZE_BODY, color: '6B7280', italics: true })],
    alignment: AlignmentType.CENTER,
    spacing: { before: 120, after: 40 },
    indent: { left: 0 }
  });
  return { label, paragraph };
}

/**
 * 表を構築するヘルパー
 *
 * @param {(string|{label: string, span: number, children?: string[]})[]} headers - ヘッダー行ラベル。
 *   文字列のみの配列であれば単一階層ヘッダー（既存動作のまま、完全後方互換）。
 *   B2（2026-07-19）: 要素に {label, span, children?} オブジェクトを混在させると2階層ヘッダーに対応する。
 *     - span: 結合する列数（対応する colWidths のうち連続する span 個分の幅を合算してセル幅にする）
 *     - children 省略時: グループ見出しを rowSpan で2行目まで縦結合する（2行目にサブ見出しは出ない。
 *       例: [{label:'項目定義', span:3}, '備考'] のように「列を束ねるだけで良い」場合に使う）
 *     - children 指定時（文字列配列、長さ = span）: 2行目にサブ見出しを個別表示する正式な2階層ヘッダーになる
 *   headers 配列内にオブジェクトが1つも無ければ従来通りの単一階層ヘッダーが生成される。
 * @param {string[][]} rows - データ行（各行は文字列配列。TableCell インスタンス混在も可、既存仕様のまま）
 * @param {number[]} colRatios - 列幅比率（合計は表幅に対する重み。配列長 = 末端列数と一致必須）
 * @param {object} [opts]
 * @param {(row: string[], rowIndex: number) => (string|undefined)} [opts.rowFill] - A4: 行全体の背景色フック
 * @param {number} [opts.verticalMergeCol] - B2（2026-07-19）: このインデックス（0始まり、rows内の列）で
 *   値が連続して同じ行を縦結合する。3行以上の連続にも対応（run先頭の行=restart、以降の行=continue）。
 *   前後どちらの行とも値が異なる単独行は結合マーク無しの通常セルになる（既存表示と同一）。
 *   verticalMergeCol 省略時は一切ロジックが働かず完全に既存動作のまま（後方互換）。
 */
function buildTable(headers, rows, colRatios, opts = {}) {
  // v34: currentIndent に追従して表のインデントと有効幅を調整
  // 意味合い: 見出しが §4.3.5.1 のように深い階層にあるとき、表もその階層に揃えてインデント。
  //          表幅は CONTENT_WIDTH - currentIndent に縮小し、右端見切れを防止。
  const tableWidth = CONTENT_WIDTH - currentIndent;
  // 列幅をDXA配分（合計=tableWidth）
  const totalRatio = colRatios.reduce((a, b) => a + b, 0);
  const colWidths = colRatios.map(r => Math.floor(tableWidth * r / totalRatio));
  // 端数を最終列に寄せる
  const sumWidths = colWidths.reduce((a, b) => a + b, 0);
  colWidths[colWidths.length - 1] += (tableWidth - sumWidths);

  // B2（2026-07-19）: headers に {label,span[,children]} オブジェクトが含まれるかで単一階層/2階層ヘッダーを分岐。
  // 意味合い: 既存呼出元（文字列配列のみ）は完全に従来のヘッダー行1本のまま（後方互換必須）。トリガー一覧・
  //          メッセージ一覧等、他の広幅表で「グループ見出し＋サブ見出し」の2階層ヘッダーを汎用的に組めるように
  //          するためのフェーズB施策B2追加（A6で行った項目定義/表示仕様の2表分割方針は維持・撤回しない）。
  // 接続情報: 呼出元は headers 配列の要素を string | {label, span, children?} で渡す。
  const hasGroupedHeader = headers.some(h => h && typeof h === 'object');
  let headerRows;
  if (!hasGroupedHeader) {
    // 既存動作: 単一階層ヘッダー（headers が文字列配列のみの場合、v55.9 までと100%同一の出力）
    headerRows = [new TableRow({
      tableHeader: true,
      children: headers.map((h, i) => thCell(h, colWidths[i]))
    })];
  } else {
    // children を持つグループが1つでもあれば2行目（サブ見出し行）を実際に作る。
    // 無ければ（=span指定のみで子見出しが無い）列結合だけの単一行ヘッダーに留める。
    // 意味合い: 2行目が存在しないのに rowSpan:2 を付けると、docx の Table 側が「次の行」＝先頭データ行を
    //          誤って継続セル扱いしてしまう事故になるため、rowSpan は2行目が実在する場合のみ許可する。
    const needsSecondRow = headers.some(h => h && typeof h === 'object' && Array.isArray(h.children) && h.children.length > 0);
    const row1Cells = [];
    const row2Cells = [];
    let colIdx = 0;
    for (const h of headers) {
      if (h && typeof h === 'object') {
        const span = h.span || 1;
        const widthSum = colWidths.slice(colIdx, colIdx + span).reduce((a, b) => a + b, 0);
        if (Array.isArray(h.children) && h.children.length > 0) {
          // 正式な2階層: 1行目=グループ見出し(columnSpan)、2行目=サブ見出し個別セル
          row1Cells.push(thCell(h.label, widthSum, { columnSpan: span }));
          h.children.forEach((c, j) => row2Cells.push(thCell(c, colWidths[colIdx + j])));
        } else if (needsSecondRow) {
          // 他のグループにchildrenがあり2行目が実在するケース: このグループは rowSpan で2行分を占有する。
          // 接続情報: docx-js の Table 実装（node_modules/docx/dist/index.d.ts 準拠）は rowSpan>1 のセルを
          //          検出すると次の行へ verticalMerge:'continue' の空セルを自動挿入する仕様を利用している。
          row1Cells.push(thCell(h.label, widthSum, { columnSpan: span, rowSpan: 2 }));
        } else {
          // 2行目が存在しない（=全グループがchildren省略）: 列結合のみの単一行ヘッダー
          row1Cells.push(thCell(h.label, widthSum, { columnSpan: span }));
        }
      } else {
        // 文字列見出し: 2行目が実在する場合のみ rowSpan で縦結合、無ければ通常の単一行セル
        row1Cells.push(needsSecondRow ? thCell(h, colWidths[colIdx], { rowSpan: 2 }) : thCell(h, colWidths[colIdx]));
      }
      colIdx += (h && typeof h === 'object') ? (h.span || 1) : 1;
    }
    headerRows = needsSecondRow
      ? [new TableRow({ tableHeader: true, children: row1Cells }), new TableRow({ tableHeader: true, children: row2Cells })]
      : [new TableRow({ tableHeader: true, children: row1Cells })];
  }

  // A4（2026-07-18）: opts.rowFill = (row, rowIndex) => fillColor|undefined。行の意味（種別列の値等）に
  // 応じて行全体を淡色で塗るためのフック。省略時は undefined を返す関数扱いで、後方互換（白のまま）。
  const rowFill = opts.rowFill || (() => undefined);

  // B2（2026-07-19）: opts.verticalMergeCol 指定時、同列で連続する同値行を verticalMerge(restart/continue)で
  // 縦結合する。意味合い: トリガー一覧等、同一グループ値が複数行に渡って繰り返される広幅表で、グループ列を
  // 視覚的に1セルへ統合し可読性を上げるための機能追加。run境界判定は「直前行と同値か／直後行と同値か」の
  // 単純比較（前後どちらとも異なる単独行は結合マーク無しの通常セル、空文字/undefinedは結合対象外）。
  // 接続情報: 入力 = opts.verticalMergeCol（0始まり列インデックス）/ rows（生データ、直前直後比較に使用）
  const vMergeCol = (typeof opts.verticalMergeCol === 'number') ? opts.verticalMergeCol : null;

  // v51 §19 1 文 1 概念ルール対応: row 要素が TableCell インスタンスならそのまま使う
  //  意味合い: tdCellWithDetails() で概要 + bullet 詳細を表現済の TableCell を渡せるよう拡張。
  //           従来の「文字列配列を tdCell で包む」挙動は維持 (後方互換)。
  //  接続情報: docx-js の TableCell は constructor.name === 'TableCell' で識別
  //           呼出例: rows = [[String, String, tdCellWithDetails(step, 'description', colWidths[2])]]
  const dataRows = rows.map((row, rowIndex) => {
    const fill = rowFill(row, rowIndex);
    return new TableRow({
      children: row.map((cell, i) => {
        // 既に TableCell インスタンス (tdCellWithDetails 等で生成) ならそのまま使う
        if (cell && typeof cell === 'object' && cell.constructor && cell.constructor.name === 'TableCell') {
          return cell;
        }
        // B2（2026-07-19）: 縦結合対象列かつ結合可能な値（空文字/undefined以外）の場合のみ run 判定を行う
        if (vMergeCol !== null && i === vMergeCol && cell !== undefined && cell !== null && cell !== '') {
          const prevRow = rowIndex > 0 ? rows[rowIndex - 1] : null;
          const nextRow = rowIndex < rows.length - 1 ? rows[rowIndex + 1] : null;
          const continuesPrev = !!prevRow && prevRow[i] === cell;
          const startsRun = !continuesPrev && !!nextRow && nextRow[i] === cell;
          if (continuesPrev) {
            // 継続行: Word は結合セルの先頭行の内容を表示するため、本セルは空欄でよい
            return tdCell(null, colWidths[i], Object.assign({}, fill ? { fill } : {}, { verticalMerge: VerticalMergeType.CONTINUE }));
          }
          if (startsRun) {
            return tdCell(cell, colWidths[i], Object.assign({}, fill ? { fill } : {}, { verticalMerge: VerticalMergeType.RESTART }));
          }
          // 前後どちらとも異なる単独行はここを通らず下の通常セル生成にフォールスルー
        }
        // それ以外 (文字列 or null、または結合対象外) は従来通り tdCell で包む
        return tdCell(cell, colWidths[i], fill ? { fill } : {});
      })
    });
  });

  return new Table({
    width: { size: tableWidth, type: WidthType.DXA },
    columnWidths: colWidths,
    indent: { size: currentIndent, type: WidthType.DXA },
    rows: [...headerRows, ...dataRows]
  });
}

/**
 * 目的: §5.x 各カテゴリ（FR/BE/CA/CL 等）のトップ見出し直下に「ID・処理名・概要1行」の一覧表を挿入する。
 * 意味合い: 個別処理の見出しは h4/h5（TOC範囲外）のため、目次だけでは各カテゴリに何件・どんな処理が
 *           あるか俯瞰できない問題への対応（A2）。ID 列は本文中の [FR14] 表記がそのまま
 *           linkifyIdRefs() で InternalHyperlink 化されるため、この関数側で個別にリンクを組む必要はない。
 * 接続情報: 呼出元 = sectionFrontProcesses / sectionBackendProcesses / sectionCalculations / sectionCommonLogic
 */
function buildProcessIndexTable(items, idField, nameField, overviewField) {
  if (!items || items.length === 0) return [];
  const rows = items.map(item => [
    `[${item[idField] || ''}]`,
    item[nameField] || '',
    ((item[overviewField] || '').split('\n')[0] || '').slice(0, 40)
  ]);
  return [buildTable(['ID', '処理名', '概要'], rows, [2, 4, 9])];
}

/**
 * 目的: front_processes[]/backend_processes[] 各エントリの notes[]（業務担当者への注意書き）を、
 *       yellow-50 背景 + 左太罫線の 1 セル表（callout 風ボックス）として描画する。
 * 意味合い: フェーズB施策B3（15_中間JSONスキーマ.md Wave4 新設フィールド）対応。notes[] は
 *          `{kind: '注意'|'補足'|'制約', text}` の配列で、本文段落に埋没させず視覚的に独立した
 *          ボックスとして目立たせることで、業務担当者が読み飛ばしにくくする狙い。
 *          背景色は既存 FILL_ERROR/FILL_WARNING と同じ「A4（2026-07-18）行の意味的背景色」定数群の
 *          FILL_WARNING（'FEFCE8' = yellow-50、承認済みカラーパレット）をそのまま再利用し、
 *          新規色を導入しない（共通の規則の承認済みカラーパレット厳守。1 表 3 色までの上限にも抵触しない）。
 *          左太罫線の色は本ファイル既存の図表キャプション文字色（buildFigureCaption/buildTableCaption
 *          で使用中の '6B7280' = gray-500、承認済み gray-100〜700 の範囲内）を流用し、こちらも新規色を
 *          増やさない。kind ごとに「【注意】」「【補足】」「【制約】」の接頭辞ラベルを付与し種別を明示する。
 * 接続情報: 入力 = notes[]（front_processes[].notes / backend_processes[].notes）
 *           出力 = Table[]（1セル1表、notes が空/未指定なら空配列）
 *           呼出元 = sectionFrontProcesses（§5.1 各処理の概要直後）/ sectionBackendProcesses（§5.2 同上）
 */
function buildNotesCallout(notes) {
  if (!Array.isArray(notes) || notes.length === 0) return [];
  const tableWidth = CONTENT_WIDTH - currentIndent;
  // 左太罫線: 通常セル罫線（0.5pt, gray-300）より太い 3pt。色は本ファイル既存の gray-500（'6B7280'）を流用。
  const calloutLeftBorder = { style: BorderStyle.SINGLE, size: 24, color: '6B7280' };
  const paragraphs = notes.map((n, idx) => new Paragraph({
    children: splitByPeriod(`【${(n && n.kind) || '補足'}】${(n && n.text) || ''}`),
    spacing: { before: idx === 0 ? 0 : 60, after: 0 },
    indent: { left: 0 }
  }));
  const cell = new TableCell({
    borders: { top: border, bottom: border, left: calloutLeftBorder, right: border },
    width: { size: tableWidth, type: WidthType.DXA },
    shading: { fill: FILL_WARNING, type: ShadingType.CLEAR },
    margins: cellMargins,
    verticalAlign: VerticalAlign.CENTER,
    children: paragraphs
  });
  return [new Table({
    width: { size: tableWidth, type: WidthType.DXA },
    columnWidths: [tableWidth],
    indent: { size: currentIndent, type: WidthType.DXA },
    rows: [new TableRow({ children: [cell] })]
  })];
}

/**
 * generated_at（ISO 8601 + TZ）を 'YYYY/MM/DD HH:MM' に変換
 */
function formatGeneratedAt(iso) {
  if (!iso) return '';
  // 例: '2026-01-01T09:00:00+09:00'
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return iso;
  return `${m[1]}/${m[2]}/${m[3]} ${m[4]}:${m[5]}`;
}

// =============================================================================
// セクション生成
// =============================================================================

/**
 * H2: 文書情報（meta から表で表示）
 *
 * v21 変更: source_html_path（個人ローカル絶対パス）を削除し、
 *           §1.1 文書情報サマリ / §1.2 フロントエンド構成ファイル / §1.3 バックエンド構成ファイル の3部構成に拡張。
 *           CSS/JS/外部CDN まで網羅した構成ファイル一覧を表示する（レビュー指摘 v21 対応）。
 */
function sectionDocInfo(meta) {
  // v49: §1.1 文書情報サマリは廃止（レビュー指摘）
  //   理由: (a) ソースHTML は §1.1 (旧 §1.2) フロントエンド構成ファイル の HTML エントリと重複、
  //         (b) プロジェクト構成パターンは「フロント+バックエンド分離型」で実質固定値、
  //         (c) 生成日時は実質固定値で業務的に役立たない、
  //         (d) 機能名は冒頭タイトル/ヘッダで明示済。
  //   よって sumRows 表は削除、§1.1 フロントエンド構成ファイル / §1.2 バックエンド構成ファイル に章番号繰上げ。
  //   meta.feature_name / meta.source_html / meta.project_pattern / meta.generated_at は中間 JSON 上には残置
  //   （他スクリプト・帳票出力タイトル等で参照される可能性のため）。docx 出力からのみ削除。
  //
  // 2026-10-06: v55.0 で §1 章見出し直後に出していた「フォーマット憲法バージョン: vX.Y 準拠」の段落を廃止。
  //   意味合い: 生成器の内部の用語で、設計書の読者には意味が通らないため。
  //   接続情報: 版の値は main が Document の description（文書のプロパティ）へ入れる。
  const blocks = [
    h2('1. 文書情報')
  ];

  // §1.1 フロントエンド構成ファイル（v49 で §1.2 から繰上げ）
  const frontendFiles = meta.frontend_files || [];
  if (frontendFiles.length > 0) {
    blocks.push(h3('1.1 フロントエンド構成ファイル'));
    blocks.push(p('当画面を構成するフロントエンドファイル（HTML本体・CSS・JavaScript・外部CDN）の一覧です。HTML 内の <link rel="stylesheet"> および <script src="..."> から自動抽出されています。'));
    const frontRows = frontendFiles.map(f => [
      f.kind || '',
      f.path || '',
      f.description || ''
    ]);
    blocks.push(buildTable(
      ['種別', 'パス', '説明'],
      frontRows,
      [2, 7, 6]
    ));
  }

  // §1.2 バックエンド構成ファイル（v49 で §1.3 から繰上げ）
  const backendFiles = meta.backend_files || [];
  if (backendFiles.length > 0) {
    blocks.push(h3('1.2 バックエンド構成ファイル'));
    blocks.push(p('当画面が呼び出すバックエンドAPIを構成するファイル（バックエンド本体・共通モジュール）の一覧です。'));
    // backend_files が v20 以前の string[] でも動作するよう両対応
    const backRows = backendFiles.map(f => {
      if (typeof f === 'string') return ['—', f, ''];
      return [f.kind || '', f.path || '', f.description || ''];
    });
    blocks.push(buildTable(
      ['種別', 'パス', '説明'],
      backRows,
      [2, 7, 6]
    ));
  }

  // §1.3 本書の色の読み方（A4新設、2026-07-18）
  // 意味合い: 表内の意味的な背景色（メッセージ一覧のエラー/警告行等）が何を示すかを明示する凡例。
  //           16_docx出力ハンドオフ.md「1 表につき意味色は 3 色まで」の規定に対応した凡例表。
  blocks.push(h3('1.3 本書の色の読み方'));
  blocks.push(p('本書内の表で使用する背景色の意味は次のとおりです。色の意味は本書全体で統一しています。'));
  blocks.push(buildTable(
    ['色', '意味'],
    [
      ['（この行の背景色）', 'エラー・異常系のメッセージ／処理'],
      ['（この行の背景色）', '警告・注意が必要なメッセージ／処理']
    ],
    [3, 10],
    { rowFill: (row, idx) => (idx === 0 ? FILL_ERROR : FILL_WARNING) }
  ));

  return blocks;
}

/**
 * H2: 改訂履歴（空テンプレ）
 */
function sectionRevisionHistory() {
  const rows = [
    ['1.0', formatGeneratedAt(new Date().toISOString()).substring(0, 10), '—', '初版作成']
  ];
  return [
    h2('2. 改訂履歴'),
    buildTable(['版数', '改訂日', '改訂者', '改訂内容'], rows, [1, 2, 2, 5])
  ];
}

/**
 * 目的: 構成図PNGの埋め込みに失敗した場合のフォールバック表示を生成する。
 * 意味合い: v52 まで画面固有のASCII業務フロー図をハードコードしていたが、
 *           他画面で使い回すとPNG生成失敗時に誤った画面の内容が出力される事故を招く（A1改修）。
 *           events[] 実データから汎用的な簡易リストを動的生成し、画面固有情報を持たない。
 * 接続情報: 呼出元 = sectionProcessFlow（processStructureEmbedded が false の場合）
 */
function buildProcessFlowFallback(events) {
  if (!events || events.length === 0) {
    return [p('（構成図PNGの生成に失敗しました。target/diagrams/ 配下の処理構成図PNGを確認してください。）')];
  }
  // 目的: 一覧の各行を「- [<events[].no>] <名称>」の形にする。
  // 意味合い: パイプライン通過後の events[] は overview / name を持たず、業務語の1文は trigger に入る。
  //           overview / name だけを読むと行が ID だけになり、何のイベントか読み取れない。
  //           trigger、無ければ content、無ければ location（配列）と action の連結の順で名称を取る。
  // 接続情報: 入力 = sectionProcessFlow が渡す data.events の要素（no / trigger / content / location / action）
  //           出力 = asciiBlock へ渡す行
  const lines = events
    .map(e => {
      // fallback-ok: 表示用の名称の取得順（trigger → content → location+action → 旧形の overview / name）。業務値ではない
      const name = e.trigger || e.content || [].concat(e.location, e.action).filter(Boolean).join(' ') || e.overview || e.name || '';
      return `- ${e.no ? `[${e.no}] ` : ''}${name}`;
    })
    .filter(line => line.trim() !== '-');
  return [
    p('（構成図PNGの埋め込みに失敗したため、主要な業務イベントを一覧表示します。）'),
    ...asciiBlock(lines.join('\n'))
  ];
}

/**
 * H2: 処理構成図（events + event_processes から ASCII フロー図を生成）
 */
function sectionProcessFlow(data) {
  const meta = data.meta || {};
  const featureName = meta.feature_name || '対象画面';
  // v50: event_code 廃止に伴い legacy_event_code で判定（後方互換）。フォールバック生成時のみ使用。
  const events = (data.events || []).filter(e => /^EV\d{2}$/.test(e.legacy_event_code || ''));

  // v51: §2 構成図（旧 §3 処理構成図）。§2.1 業務フロー全体 + §2.2 IPO データフロー + §2.3 画面構成図
  const result = [
    h2('2. 構成図'),
    h3('2.1 業務フロー全体')
  ];
  result.push(p(`${featureName}画面の業務フローを以下に示します。画面表示から主要な業務操作の完了までの一連の操作シーケンスを表現します。`));

  // §3.1 処理構成図 PNG 埋め込み
  const diagram = (data.diagrams || []).find(d => d && d.id === 'process_structure');
  let processStructureEmbedded = false;
  if (diagram) {
    // A3（2026-07-19）: 図番号を先に採番し、図の直前に参照文（「〜を図2-1に示す」）を入れる。
    // B3（2026-07-19）: diagram.caption が指定されていれば優先使用、未指定なら diagram.title にフォールバック
    //  （15_中間JSONスキーマ.md diagrams[].caption 新設対応、既存動作維持）。
    const processStructureCaptionTitle = diagram.caption || diagram.title || '処理構成図';
    // 2026-10-06: 採番と参照文は PNG の読込みが済んでから行う（下の calcAspectFit の後）。
    //   意味合い: PNG が無い・読めないと代替表示（buildProcessFlowFallback）になるが、旧版は先に
    //             「業務フローの全体像を図2-1に示します。」を出し、無い図を指していた。図番号も空費していた。
    //   接続情報: buildFigureCaption（_figureCounters を進める）/ 後続の IPO・画面遷移図の図番号。
    if (diagram.png_path && fs.existsSync(diagram.png_path)) {
      try {
        const imgData = fs.readFileSync(diagram.png_path);
        const displayWidth = (diagram.embed_size && diagram.embed_size.width_px) || 600;
        const { width: w, height: h } = calcAspectFit(diagram.png_path, displayWidth);
        const fig = buildFigureCaption('2', processStructureCaptionTitle);
        result.push(p(`業務フローの全体像を${fig.label}に示します。`));
        result.push(new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new ImageRun({
            type: 'png', data: imgData, transformation: { width: w, height: h },
            altText: { title: processStructureCaptionTitle, description: `${featureName}画面の業務フロー`, name: diagram.id }
          })]
        }));
        result.push(fig.paragraph);
        // B3（2026-07-19）: diagram.reading_note があれば FIGURE_READING_NOTES の代わりに使用（未指定時は従来通りフォールバック）。
        const processStructureReadingNote = diagram.reading_note || FIGURE_READING_NOTES.process_structure;
        if (processStructureReadingNote) {
          result.push(p(processStructureReadingNote));
        }
        processStructureEmbedded = true;
      } catch (e) {
        console.warn(`[warn] 処理構成図PNG埋め込み失敗 (${diagram.png_path}): ${e.message}`);
      }
    }
  }
  if (!processStructureEmbedded) {
    result.push(...buildProcessFlowFallback(events));
  }

  // §3.2 IPO データフロー図（v26: Flowchart 採用に確定、Swimlane / Sequence は廃止）
  // ユーザー比較結果「Flowchart 全シナリオ集約形式が最も読みやすい」に基づき、Flowchart のみに簡略化
  const ipoDiagram = (data.diagrams || []).find(d => d && d.id === 'ipo_flowchart');
  if (ipoDiagram) {
    result.push(h3('2.2 IPO データフロー図'));
    result.push(p('画面入力（操作）→ フロント処理 → API → バックエンド処理 → DB の主要シナリオにおけるデータの流れを1枚に集約します。DB 層は物理テーブル単位で分解し、各シナリオがどのテーブルに何の操作（SELECT / UPSERT / UPDATE / INSERT / DELETE）を行うかを矢印ラベルで明示します。'));
    if (ipoDiagram.png_path && fs.existsSync(ipoDiagram.png_path)) {
      // A3（2026-07-19）: 図番号を先に採番し、図の直前に参照文を入れる。
      // B3（2026-07-19）: diagram.caption が指定されていれば優先使用、未指定なら diagram.title にフォールバック。
      const ipoCaptionTitle = ipoDiagram.caption || ipoDiagram.title || 'IPO データフロー図';
      const fig = buildFigureCaption('2', ipoCaptionTitle);
      result.push(p(`データの流れを${fig.label}に示します。`));
      try {
        const imgData = fs.readFileSync(ipoDiagram.png_path);
        const dispW = (ipoDiagram.embed_size && ipoDiagram.embed_size.width_px) || 800;
        const { width: w, height: h } = calcAspectFit(ipoDiagram.png_path, dispW);
        result.push(new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new ImageRun({
            type: 'png', data: imgData, transformation: { width: w, height: h },
            altText: { title: ipoCaptionTitle, description: '画面→フロント→バックエンド→DB のデータフロー、物理テーブル別', name: ipoDiagram.id }
          })]
        }));
        result.push(fig.paragraph);
        // B3（2026-07-19）: diagram.reading_note があれば FIGURE_READING_NOTES の代わりに使用。
        const ipoReadingNote = ipoDiagram.reading_note || FIGURE_READING_NOTES.ipo_flowchart;
        if (ipoReadingNote) {
          result.push(p(ipoReadingNote));
        }
      } catch (e) {
        result.push(p(`（IPO データフロー図 PNG 埋め込み失敗: ${e.message}）`));
      }
    }
  }

  // §2.3 画面遷移図（screen_transition）PNG 埋め込み
  // B4（2026-07-19）: 17_構成図生成.md「対象となる図」カタログの screen_transition 行に対応。
  // 意味合い: events[] の画面遷移パターン（§9-C）から生成される図で、画面遷移が発生する画面
  //           のみ生成対象（画面遷移が発生しない画面には diagrams[] にエントリが存在しない想定、
  //           17_構成図生成.md 受入条件）。既存4図種（process_structure/ipo_flowchart/screen_structure/
  //           derivation_chain）と同一の「PNG存在確認→埋め込み→buildFigureCaption→FIGURE_READING_NOTES」
  //           パターンを踏襲する。diagrams[] に該当エントリが無い、または PNG が存在しない場合は見出しごと
  //           出力しない（ASCII フォールバックが無い図種のため、Silent fallback ではなく「図自体を出さない」
  //           ことが正しい既定動作。derivation_chain の失敗時空配列パターンと同一の設計思想）。
  // 接続情報: 入力 = data.diagrams[].id === 'screen_transition' / 出力 = §2.3 見出し + 画像 + 図キャプション + 読み方定型文
  const transitionDiagram = (data.diagrams || []).find(d => d && d.id === 'screen_transition');
  if (transitionDiagram && transitionDiagram.png_path && fs.existsSync(transitionDiagram.png_path)) {
    try {
      const imgData = fs.readFileSync(transitionDiagram.png_path);
      const displayWidth = (transitionDiagram.embed_size && transitionDiagram.embed_size.width_px) || 640;
      const { width: w, height: h } = calcAspectFit(transitionDiagram.png_path, displayWidth);
      // B3系と同様: diagram.caption / diagram.reading_note があれば優先使用、未指定なら title / FIGURE_READING_NOTES にフォールバック。
      const transitionCaptionTitle = transitionDiagram.caption || transitionDiagram.title || '画面遷移図';
      const fig = buildFigureCaption('2', transitionCaptionTitle);
      const transitionReadingNote = transitionDiagram.reading_note || FIGURE_READING_NOTES.screen_transition;
      result.push(h3('2.3 画面遷移図'));
      result.push(p('当画面から遷移可能な画面と、遷移のきっかけとなる画面操作の対応関係を示します。'));
      result.push(p(`画面遷移の流れを${fig.label}に示します。`));
      result.push(new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new ImageRun({
          type: 'png', data: imgData, transformation: { width: w, height: h },
          altText: { title: transitionCaptionTitle, description: `${featureName}画面から遷移先画面への画面遷移パターン`, name: transitionDiagram.id }
        })]
      }));
      result.push(fig.paragraph);
      if (transitionReadingNote) {
        result.push(p(transitionReadingNote));
      }
    } catch (e) {
      // 装置: try 内で見出し等の result.push を行っていないため、途中失敗時も §2.3 セクション自体が
      // 出力されない（半端なセクションが残らない）。既存4図種の catch ブロックと同じ「テキスト表記のみで継続」方針。
      console.warn(`[warn] 画面遷移図PNG埋め込み失敗 (${transitionDiagram.png_path}): ${e.message}`);
    }
  }

  return result;
}

/**
 * H2: 画面レイアウト
 */
function sectionScreenLayout(sl, data) {
  // 接続情報: data.diagrams[].id === 'screen_structure' が存在すれば SVG優先で §4.1 に埋め込む（17_構成図生成.md）
  // 過去経緯: v7 で §5 から EV連番を廃止したのに §4.2 はまだ EV01 表記が残っていた →
  //          v9 で event_code → event no の逆引きマップで「イベントNo.」列に変更（§5 と整合）
  const items = (sl && sl.items) || [];
  const ascii = (sl && sl.diagram_ascii) || '';

  // v50: items[].event_ref は events[].no を直接参照（v49 までの event_code → no 変換は不要）
  // 互換用に events[].no の存在確認のみ実施（無効な event_ref は空表示にフォールバック）
  const validEventNos = new Set(((data && data.events) || []).map(e => e.no).filter(Boolean));

  // v22: items テーブルから「画面#」列を削除（エリア#で一意識別、画面#は冗長というレビュー指摘対応）
  // v31: §4.3 をエリアごとに分割表示。各エリア表は item.no を通し番号として保持し、エリア#列は削除。
  //       画面内の位置は area_ref（=area_no）で表現する。screen_no データは内部互換のため残置するが docx に出さない。

  const result = [
    h2('3. 画面レイアウト'),
    h3('3.1 画面構成図')
  ];

  const diagram = ((data && data.diagrams) || []).find(d => d && d.id === 'screen_structure');
  let svgEmbedded = false;
  // 接続情報: SVG ではなく PNG 経由で埋め込む（Wordバージョン互換性問題の対処、svg_to_png.py で生成）
  if (diagram && diagram.png_path && fs.existsSync(diagram.png_path)) {
    try {
      // A3（2026-07-19）: 図番号を先に採番し、図の直前に参照文を入れる。
      // B3（2026-07-19）: diagram.caption が指定されていれば優先使用、未指定なら diagram.title にフォールバック。
      const screenStructureCaptionTitle = diagram.caption || diagram.title || '画面構成図';
      const fig = buildFigureCaption('3', screenStructureCaptionTitle);
      result.push(p(`当画面の論理エリア構成を${fig.label}に示します。`));
      const imgData = fs.readFileSync(diagram.png_path);
      // PNG実寸からアスペクト比固定で h 算出（横伸び問題対策、v8）
      const displayWidth = (diagram.embed_size && diagram.embed_size.width_px) || 640;
      const { width: w, height: h } = calcAspectFit(diagram.png_path, displayWidth);
      result.push(new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new ImageRun({
          type: 'png',
          data: imgData,
          transformation: { width: w, height: h },
          altText: {
            title: screenStructureCaptionTitle,
            description: `${((data && data.meta && data.meta.feature_name) || '対象画面')}画面の論理ブロック構成（diagram-design 生成 SVG）`,
            name: diagram.id
          }
        })]
      }));
      result.push(fig.paragraph);
      // B3（2026-07-19）: diagram.reading_note があれば FIGURE_READING_NOTES の代わりに使用。
      const screenStructureReadingNote = diagram.reading_note || FIGURE_READING_NOTES.screen_structure;
      if (screenStructureReadingNote) {
        result.push(p(screenStructureReadingNote));
      }
      svgEmbedded = true;
    } catch (e) {
      console.warn(`[warn] 画面構成図SVG埋め込み失敗 (${diagram.png_path}): ${e.message}. ASCIIフォールバック`);
    }
  }
  if (!svgEmbedded) {
    result.push(...asciiBlock(ascii));
  }

  // v20: §4.2 画面エリア一覧（areas[] の権威定義表示）
  // v22: 「画面#」列を削除（area_no と screen_no が一対一で冗長というレビュー指摘対応）
  const areas = (sl && sl.areas) || [];
  if (areas.length > 0) {
    result.push(h3('3.2 画面エリア一覧'));
    result.push(p('当画面の論理エリアを定義します。§4 トリガー や §5 処理詳細 で画面項目に言及する際は、ここに定義された「エリア名」「エリア#」と一致した語彙で記述されます。'));
    const areaRows = areas.map(a => [
      String(a.area_no || ''),
      a.area_name || '',
      a.description || ''
    ]);
    // v48: ヘッダ「エリア#」を id_scheme.json area カテゴリの header_self 経由で取得
    result.push(buildTable(
      [getIdHeaderLabel('area', 'self'), 'エリア名', '説明'],
      areaRows,
      [1, 4, 11]
    ));
  }

  // v31: §4.3 画面項目一覧をエリアごとに分割表示（旧版は1表に全項目をまとめており縦に長すぎたレビュー指摘対応）
  // 意味合い: §4.3.1 [A1] ヘッダエリア画面項目一覧 / §4.3.2 [A2] ワークフローステッパー画面項目一覧 / ...
  //          area_no 順に H4 見出し + 表を出す。No 列は items[].no（画面全体の通し番号）をそのまま使う。
  //          エリア#列は H4 見出しに昇格するため表から削除。
  // 接続情報: 出力 = h3('4.3') + h4('4.3.X [A?] エリア名画面項目一覧') x area数 + 各 buildTable
  result.push(h3('3.3 画面項目一覧'));
  result.push(p('画面エリアごとに項目を一覧します。No は画面全体の通し番号で、§4 トリガー／§5 処理詳細から画面項目に言及する際の参照キーになります。'));

  // area_no → items[] のグループ化マップ
  const itemsByArea = new Map();
  for (const a of areas) itemsByArea.set(a.area_no, []);
  for (const it of items) {
    const aref = it.area_ref || '';
    if (itemsByArea.has(aref)) {
      itemsByArea.get(aref).push(it);
    } else {
      // area_ref が areas[] と一致しないフォールバック先（業務的には WARN だが docx 出力は継続）
      if (!itemsByArea.has('_unassigned')) itemsByArea.set('_unassigned', []);
      itemsByArea.get('_unassigned').push(it);
    }
  }

  // v32: area_pattern_subdivisions が定義されたエリアは項目を業務軸（取引の種別等）で更に分割
  // 意味合い: 例 明細のエリアで、項目の remarks に区分（例: 税込/税抜/非課税）のキーワードが書かれている。
  //          共通項目（remarks にキーワードなし）を親見出しに、各パターン固有項目を §4.3.{X}.{n} に出す。
  const subdivisionsMap = (sl && sl.area_pattern_subdivisions) || {};

  // A6（2026-07-19）: 旧8列単一表（No/項目名/種別/イベントNo/フォーマット/型/サイズ/備考）は
  // No/型/サイズ列が1文字分程度しか確保できず窮屈だった問題への対応。「項目定義」（識別・種別・
  // 起動イベント）と「表示仕様」（フォーマット・型・備考）の2表に分割し、No列で対応関係を保つ。
  // 横向きセクション（landscape）化はページ構成の再検証コストが高いため見送り、2表分割で根治する
  // （16_docx出力ハンドオフ.md §24「列幅設計の原則」参照）。
  // B2（2026-07-19）: withCaption=true の呼出時のみ「表3-1 画面項目一覧（項目定義）」形式のキャプションを
  // 表の直前に挿入する。意味合い: 主要一覧表への表番号キャプション試験導入（全エリア×全表に付与すると
  // ノイズになるため、呼出元側で最初の1回だけ true を渡す方針。16_docx出力ハンドオフ.md §21参照）。
  function buildItemTables(itemList, withCaption = false) {
    const defHeaders = [getIdHeaderLabel('item', 'self'), '項目名', '種別', getIdHeaderLabel('event', 'ref')];
    const defWidths = [1, 5, 3, 3];
    // B3（2026-07-19）: 「例示値」列を追加（screen_layout.items[].example、フェーズB施策B3新設）。
    //  意味合い: 業務担当者が入力例・表示例を一目で把握できるようにする。未指定項目は「-」で明示。
    //  接続情報: 15_中間JSONスキーマ.md screen_layout.items[].example 参照。列幅は既存合計18を維持したまま
    //           備考列（旧10 → 7）を圧縮し例示値列（3）を新設、全体バランスを崩さないよう配分。
    const specHeaders = [getIdHeaderLabel('item', 'self'), 'フォーマット', '型', 'サイズ', '備考', '例示値'];
    const specWidths = [1, 3, 2, 2, 7, 3];
    const defRows = itemList.map(it => {
      const evNo = it.event_ref && validEventNos.has(it.event_ref) ? it.event_ref : '';
      return [String(it.no || ''), it.item_name || '', it.item_type || '', evNo === '' ? '' : String(evNo)];
    });
    const specRows = itemList.map(it => [
      String(it.no || ''), it.format || '', it.data_type || '', String(it.length || ''), it.remarks || '', it.example || '-'
    ]);
    const blocks = [p('項目定義（識別・種別・起動イベント）:')];
    if (withCaption) blocks.push(buildTableCaption('3', '画面項目一覧（項目定義）').paragraph);
    blocks.push(buildTable(defHeaders, defRows, defWidths));
    blocks.push(p('表示仕様（フォーマット・型・備考）:'));
    if (withCaption) blocks.push(buildTableCaption('3', '画面項目一覧（表示仕様）').paragraph);
    blocks.push(buildTable(specHeaders, specRows, specWidths));
    return blocks;
  }

  // B2（2026-07-19）: 画面項目一覧の表キャプションは「最初に描画される1組（項目定義表+表示仕様表）」
  // にのみ付与する（全エリア×全パターンに付与すると369表規模でノイズになるため）。
  let itemTableCaptionUsed = false;

  let areaSubNo = 1;
  for (const a of areas) {
    const areaItems = itemsByArea.get(a.area_no) || [];
    if (areaItems.length === 0) continue;

    const subdiv = subdivisionsMap[a.area_no];
    if (subdiv && subdiv.subdivisions && subdiv.subdivisions.length > 0) {
      // v32: subdivision 定義あり（例 A5）→ 共通項目 + パターン固有項目に分割
      // 共通項目 = remarks に subdivisions の remarks_keyword が一つも含まれない項目
      // パターン固有項目 = remarks に該当 keyword を含む項目（複数パターンに該当する項目は各表に重複表示）
      const allKeywords = subdiv.subdivisions.map(s => s.remarks_keyword).filter(Boolean);
      const commonItems = areaItems.filter(it => {
        const rem = it.remarks || '';
        return !allKeywords.some(kw => rem.includes(kw));
      });
      // 親見出し（パターン共通）
      result.push(h4(`3.3.${areaSubNo} [${a.area_no}] ${a.area_name}画面項目一覧（パターン共通）`));
      result.push(p(`${a.area_name}は${subdiv.subdivision_axis_label || '業務軸'}によって表示項目が変わります。ここでは全${subdiv.subdivision_axis_label || 'パターン'}で共通に表示される項目を一覧します。${subdiv.subdivision_axis_label || 'パターン'}ごとに追加表示される項目は §3.3.${areaSubNo}.1 以降を参照してください。`));
      if (commonItems.length > 0) {
        result.push(...buildItemTables(commonItems, !itemTableCaptionUsed));
        itemTableCaptionUsed = true;
      } else {
        result.push(p('共通項目はありません（すべての項目が特定パターンに依存）。'));
      }
      // 各 subdivision の固有項目（remarks に該当 keyword を含む項目）
      let subIdx = 0;
      for (const sd of subdiv.subdivisions) {
        subIdx++;
        const kw = sd.remarks_keyword || '';
        if (!kw) continue;
        const patternItems = areaItems.filter(it => (it.remarks || '').includes(kw));
        result.push(h5(`3.3.${areaSubNo}.${subIdx} [${a.area_no}-${sd.subdivision_id}] ${sd.subdivision_id}${sd.name}で追加表示される項目`));
        if (patternItems.length > 0) {
          result.push(...buildItemTables(patternItems, !itemTableCaptionUsed));
          itemTableCaptionUsed = true;
        } else {
          result.push(p(`${sd.subdivision_id}${sd.name}で追加表示される項目はありません。`));
        }
      }
    } else {
      // subdivision なし → 従来通り §4.3.X [A?] エリア名画面項目一覧（N件）の単一表
      result.push(h4(`3.3.${areaSubNo} [${a.area_no}] ${a.area_name}画面項目一覧`));
      result.push(...buildItemTables(areaItems, !itemTableCaptionUsed));
      itemTableCaptionUsed = true;
    }
    areaSubNo++;
  }

  // 未割当 items（area_ref が areas[] と未対応）が残っている場合は末尾に出す（業務的レアケース）
  const unassigned = itemsByArea.get('_unassigned') || [];
  if (unassigned.length > 0) {
    result.push(h4(`3.3.${areaSubNo} [未割当] エリア未対応画面項目一覧`));
    result.push(...buildItemTables(unassigned));
  }

  return result;
}

/**
 * H2: イベント一覧
 */
function sectionEvents(events) {
  // 意味合い: §5 を「5.1 画面トリガー」と「5.2 内部処理」の2段に分割。業務担当者は 5.1 だけ
  //           読めば日常業務が完結する構成。5.2 は技術参考。
  // 接続情報: events[].source_kind ('screen' or 'internal') で振り分け、convert_events_location_action.py が自動分類
  //           location が空の場合は trigger フィールドにフォールバック（互換性のため）
  // 過去経緯: v9 までは1つの§5表、ユーザー要望(β) で 2セクション分割に変更（v10）
  // v14: EV00（画面初期表示）は §6 初期処理セクションに専念させるため、§5 一覧から除外する。
  //       レビュー指摘「画面の初期表示はトリガーではなく初期処理の別枠に書いた方が読みやすい」への対応。
  // v50: legacy_event_code === 'EV00' で初期表示イベントを判定（event_code 廃止）
  const all = (events || []).filter(e => (e.legacy_event_code || '') !== 'EV00');
  const screenEvents   = all.filter(e => (e.source_kind || 'screen') === 'screen');
  const internalEvents = all.filter(e => e.source_kind === 'internal');

  function buildRow(e) {
    const loc = (e.location || []).length > 0
      ? formatLocationHierarchy(e.location)
      : (e.trigger || '');
    return [
      String(e.no || ''),
      loc,
      e.action || '',
      e.content || '',
      e.classification || ''
    ];
  }

  const blocks = [
    h2('5. イベント一覧'),
    p('画面で発生する全イベントを「画面トリガー」（業務担当者が直接操作するもの）と「内部処理」（裏で呼ばれる処理）に分けて一覧します。')
  ];

  // 5.1 画面トリガー
  // v48: ヘッダ「No」は event カテゴリの header_self
  blocks.push(h3(`5.1 画面トリガー（${screenEvents.length}件）`));
  if (screenEvents.length > 0) {
    blocks.push(buildTable(
      [getIdHeaderLabel('event', 'self'), '発生', 'アクション', '処理内容', '分類'],
      screenEvents.map(buildRow),
      [1, 5, 2, 7, 1]
    ));
  } else {
    blocks.push(p('画面トリガーは登録されていません。'));
  }

  // 5.2 内部処理
  blocks.push(h3(`5.2 内部処理（${internalEvents.length}件）`));
  if (internalEvents.length > 0) {
    blocks.push(p('業務担当者が直接操作するものではなく、別の処理から呼ばれる裏側のイベントです（WebSocket通知、サジェスト、テスト機能 等）。'));
    blocks.push(buildTable(
      [getIdHeaderLabel('event', 'self'), '発生', 'アクション', '処理内容', '分類'],
      internalEvents.map(buildRow),
      [1, 5, 2, 7, 1]
    ));
  } else {
    blocks.push(p('内部処理は登録されていません。'));
  }

  return blocks;
}

// =============================================================================
// v18 セクション: 呼ぶ側／呼ばれる側 体系
// =============================================================================

/**
 * H2: 処理実行条件（v18 で新設、呼ぶ側）
 *
 * 目的: docx §5 で処理を起動するトリガーを「画面操作 / 自動 / 外部」の3種に分類して網羅する。
 *       各トリガーは calls=[F00X] で呼出先のフロント処理ID を明示する（呼ぶ側と呼ばれる側の対応）。
 *
 * 意味合い: 業務担当者が「いつ、どこで、何を呼ぶか」を網羅的に把握できる構成。
 *           §5 を全て読めば、起動経路の漏れがないことが保証される。
 *
 * 接続情報: 入力 = data.trigger_groups (15_中間JSONスキーマ.md「trigger_groups」セクション)
 *           出力 = H2「5. 処理実行条件」 + 3つのH3（5.1 画面操作 / 5.2 自動 / 5.3 外部）+ H4（category別）+ 表（トリガー一覧）
 */
function sectionTriggerGroups(data) {
  const groups = data.trigger_groups || [];
  if (groups.length === 0) {
    // フォールバック: v17 互換、events[] から旧 sectionEvents 形式で表示
    return sectionEvents(data.events);
  }

  // v23: front_processes の id→name マップ（呼出処理表記で name を併記するため）
  const frontMap = new Map();
  for (const fp of (data.front_processes || [])) {
    if (fp.process_id) frontMap.set(fp.process_id, fp);
  }

  const blocks = [
    h2('4. トリガー'),
    // v50.5: 旧形式 [F00X] ハードコードを id_scheme.json 経由で動的化（sectionFrontProcesses / sectionBackendProcesses と同方針）
    p(`当画面で処理を起動するトリガーを「画面操作」「自動」「外部」の3種に分類して網羅します。各トリガーの「処理内容」列には、事前チェックと呼出先のフロント処理ID（→ [${getCategoryPrefix('front_process', 'FR')}#]）を記述します。詳細は §5.1 フロント処理 を参照してください。`)
  ];

  // kind 順に H3 で並べる
  const kindOrder = [
    // v51 憲法: §4 トリガー配下に §4.1 画面操作 / §4.2 自動 / §4.3 外部
    { kind: 'screen_operation', subNo: '4.1', label: '画面操作トリガー', desc: '業務担当者が画面上で操作することによって起動されるトリガーです。画面エリアごとに表を分けて一覧します。' },
    { kind: 'auto', subNo: '4.2', label: '自動トリガー', desc: '画面表示・タイマー等、業務担当者の明示操作なしに自動実行されるトリガーです。' },
    { kind: 'external', subNo: '4.3', label: '外部トリガー', desc: 'WebSocket受信や外部システム呼出など、当画面の外から到来するトリガーです。' }
  ];

  // v32: screen_operation トリガーをエリア別に分割するため、画面エリア定義を取得
  // 意味合い: trigger_groups の screen_operation は v18-v30 で「サンプル画面」1グループに 40 件集約されていた
  //          → レビュー指摘「エリアごとに分けたほうが見やすい」への対応で、location[1] のエリア名から area_no を引いて分割
  const areas = (data.screen_layout && data.screen_layout.areas) || [];
  const areaByName = new Map(areas.map(a => [a.area_name, a]));

  // B2（2026-07-19）: トリガー一覧の表キャプションは章内で最初に描画される1表にのみ付与する
  // （画面操作/自動/外部の全エリア・全分類に付与すると数十表規模でノイズになるため）。
  let triggerTableCaptionUsed = false;

  for (const ko of kindOrder) {
    const kindGroups = groups.filter(g => g.kind === ko.kind);
    if (kindGroups.length === 0) continue;
    blocks.push(h3(`${ko.subNo} ${ko.label}`));
    blocks.push(p(ko.desc));

    if (ko.kind === 'screen_operation') {
      // v32: 画面操作トリガーはエリア別 H4 分割（旧版は category='サンプル画面' で1表40件だった）
      // 全 screen_operation トリガーを集約し、各トリガーの location[1] (エリア名) で area_no を解決してバケット化
      const allTriggers = [];
      for (const g of kindGroups) {
        for (const t of (g.triggers || [])) allTriggers.push(t);
      }
      // area_no → triggers のマップ（areas[] 出現順を保持）
      const byArea = new Map(areas.map(a => [a.area_no, []]));
      const unassigned = [];
      for (const t of allTriggers) {
        const loc = t.location || [];
        const areaName = loc[1] || '';
        const a = areaByName.get(areaName);
        if (a) byArea.get(a.area_no).push(t);
        else unassigned.push(t);
      }
      // areas 順に H4 + 表を出す。「発生場所」列は location[2:]（項目名以降）に短縮
      // （画面名とエリア名は H3/H4 見出しに昇格済のため重複表示を回避）
      let subNo = 0;
      for (const a of areas) {
        const triggers = byArea.get(a.area_no) || [];
        if (triggers.length === 0) continue;
        subNo++;
        blocks.push(h4(`${ko.subNo}.${subNo} [${a.area_no}] ${a.area_name}`));
        const rows = triggers.map(t => {
          const loc = t.location || [];
          const detail = loc.length > 2 ? formatLocationHierarchy(loc.slice(2)) : '';
          return [
            String(t.trigger_no || ''),
            detail,
            t.action || '',
            formatTriggerDispatch(t, frontMap)
          ];
        });
        // v48: ヘッダ「No」は trigger カテゴリの header_self
        // B2（2026-07-19）: 章内最初の1表にのみ「表4-1 トリガー一覧（画面操作）」形式のキャプションを付与
        if (!triggerTableCaptionUsed) {
          blocks.push(buildTableCaption('4', 'トリガー一覧（画面操作）').paragraph);
          triggerTableCaptionUsed = true;
        }
        blocks.push(buildTable(
          [getIdHeaderLabel('trigger', 'self'), '項目', 'アクション', '処理内容'],
          rows,
          [1, 4, 2, 10]
        ));
      }
      // v50.5 phase 2.5.1: 「未割当」グループの docx 出力を廃止。
      // 意味合い: 業務的に screen_operation の trigger は必ず画面エリアに属するべきで、area_ref 未設定は
      //          Phase 1 Agent の抽出不足。設計書に「未割当」を出すと品質が低く見え、
      //          業務担当者の信頼を損なう（レビュー指摘）。
      // 接続情報: verify_intermediate.py に「screen_operation kind の trigger は area_ref 必須」の
      //          ERROR チェックを追加し、未割当が残ったまま生成されないよう機械保証する。
      // (旧: unassigned.length > 0 で h4 + table を出していたが v50.5 で削除)
    } else {
      // auto / external は従来通り category（画面表示時 / WebSocket受信 等）で H4 分割
      // v50.5 phase 2.5.1: 見出しに [自動] / [外部] prefix を付与（業務担当者が一目で kind を識別できるように）
      // 意味合い: レビュー指摘「全ての No が接頭辞 ID になっていない」への対応の一環。
      //          §5.2.1 / §5.3.1 等が「画面表示時」「その他」だけだと kind が見出しで分からないため、
      //          ラベルから「トリガー」を取り除いた kind 名を [自動] / [外部] として明示する。
      const kindPrefix = (ko.label || '').replace('トリガー', '');
      let cIdx = 0;
      for (const g of kindGroups) {
        cIdx++;
        blocks.push(h4(`${ko.subNo}.${cIdx} [${kindPrefix}] ${g.category || '（分類なし）'}`));
        const rows = (g.triggers || []).map(t => [
          String(t.trigger_no || ''),
          (t.location || []).length > 0 ? formatLocationHierarchy(t.location) : '',
          t.action || '',
          formatTriggerDispatch(t, frontMap)
        ]);
        // v48: ヘッダ「No」は trigger カテゴリの header_self（auto/external 表）
        blocks.push(buildTable(
          [getIdHeaderLabel('trigger', 'self'), '発生場所', 'アクション', '処理内容'],
          rows,
          [1, 5, 2, 10]
        ));
      }
    }
  }
  return blocks;
}

/**
 * trigger の「処理内容」セル文字列を生成（v23 で新設）
 *
 * 目的: アクションと処理内容の役割分担を明確にする。
 *       - アクション列: 動作種別のみ（クリック / 変更 / フォーカス外し 等）
 *       - 処理内容列: 呼出処理（事前チェック → [F00X] name）を記述、アクションの言い換えは禁止
 *
 * ロジック:
 *   1. dispatch[] があれば、条件分岐付き呼出を整形（「条件A → [F00X] / 条件B → 終了 + メッセージ」形式）
 *   2. dispatch[] が無く calls[] のみなら、単純呼出（「→ [F00X] name」）
 *   3. calls[] も空なら「—」
 *
 * 接続情報: 仕様根拠 = 15_中間JSONスキーマ.md「dispatch[]」セクション
 */
function formatTriggerDispatch(trigger, frontMap) {
  const dispatch = trigger.dispatch || [];
  // v50 Phase 2: trigger.front_refs（新名）優先、旧名 trigger.calls フォールバック
  const calls = trigger.front_refs || trigger.calls || [];

  // dispatch があれば条件分岐表記
  if (dispatch.length > 0) {
    const lines = dispatch.map(d => {
      const cond = d.condition || '（条件未設定）';
      if (d.then_call) {
        const fp = frontMap.get(d.then_call);
        const name = fp ? `（${fp.name}）` : '';
        return `${cond} → [${d.then_call}]${name}`;
      }
      if (d.then_terminate) {
        const msg = d.then_message ? `: ${d.then_message}` : '';
        return `${cond} → 終了${msg}`;
      }
      if (d.then_message) {
        return `${cond} → ${d.then_message}`;
      }
      return `${cond} → （未定義）`;
    });
    return lines.join('\n');
  }

  // dispatch が無く calls のみ
  if (calls.length > 0) {
    const lines = calls.map(fid => {
      const fp = frontMap.get(fid);
      const name = fp ? `（${fp.name}）` : '';
      return `→ [${fid}]${name}`;
    });
    return lines.join('\n');
  }

  return '—';
}

/**
 * フロント処理 area の表示順序定義（v19 で追加）
 *
 * 意味合い: §6.1 配下の H4 グループの並び順を業務的に意味のある順序で固定する。
 *           「画面を開いてから順に何が起きるか」に近い順序: 初期化 → 操作 → 編集 → 出力 → 内部処理
 *           リスト未掲載の area は末尾にアルファベット順で並ぶ。
 */
const FRONT_AREA_ORDER = [
  '初期処理',
  'ヘッダ操作',
  'ワークフロー操作',
  'パターン切替',
  'データ編集',
  'サジェスト',
  'ダイアログ操作',
  '画面遷移',
  '帳票出力',
  '編集権制御',
  '動作検証',
  'その他操作'
];

/**
 * バックエンド処理 area の表示順序定義（v19 で追加）
 *
 * 意味合い: §6.2 配下の H4 グループの並び順を業務的に意味のある順序で固定する。
 *           「読み取り → マスタ → 排他 → 更新」の自然な順序。
 */
const BACKEND_AREA_ORDER = [
  'データ取得',
  'マスタ参照',
  '排他制御',
  'データ更新',
  'その他'
];

/**
 * area 順序付きで処理リストをグルーピングするヘルパー（v19 で追加）
 *
 * 接続情報: 入力 = processes[], areaOrder（標準順序リスト）
 *           出力 = [{area, items}] の配列（areaOrder の順 + 未掲載 area はアルファベット順で末尾）
 */
function groupByAreaOrdered(processes, areaOrder) {
  const grouped = new Map();
  for (const p of processes) {
    const area = p.area || 'その他';
    if (!grouped.has(area)) grouped.set(area, []);
    grouped.get(area).push(p);
  }
  // areaOrder の順序で並べ、未掲載 area は末尾アルファベット順
  const result = [];
  for (const a of areaOrder) {
    if (grouped.has(a)) {
      result.push({ area: a, items: grouped.get(a) });
      grouped.delete(a);
    }
  }
  // 残った area をアルファベット順で追加
  for (const a of [...grouped.keys()].sort()) {
    result.push({ area: a, items: grouped.get(a) });
  }
  return result;
}

/**
 * H3 サブ: フロント処理一覧（v19 で area 階層化対応）
 *
 * 目的: §6.1 で F001〜F0NN のフロント処理を、area（業務操作カテゴリ）でグルーピング → H4 で見出し化し、
 *       各 area 配下の処理を H5 で並べる3階層構造で表示する。
 *
 * 接続情報: 入力 = data.front_processes / 出力 = h3('5.1 フロント処理') + h4(area) + h5(FR#) + 表
 *           backend_call ステップでは backend_id を「→ B00X」として参照リンク表示
 * 過去経緯: v18 まで H4 で各 F00X を直列に並べていたが「area 単位でまとめてほしい」というレビュー指摘で v19 で階層化
 */
function sectionFrontProcesses(frontProcesses, opts = {}) {
  const { prefix = '5.1' } = opts;
  if (!frontProcesses || frontProcesses.length === 0) return [];
  const blocks = [
    h3(`${prefix} フロント処理`),
    // v50.5: 旧形式 F001〜 / B00X ハードコードを排除、id_scheme.json prefix 経由で動的化。
    //         意味合い: id_scheme.json で prefix を変更したら docx 説明文も自動追従する単一拘束点設計
    // v51 憲法: 章構成 §4 トリガー / §5 処理詳細 / §5.2 バック処理
    p(`業務担当者が起動した処理（§4 トリガー）に対し、画面側で実行される処理本体です。各処理は ${getCategoryPrefix('front_process', 'FR')}1〜の連番 ID で識別され、業務操作カテゴリ（エリア）でグルーピングしています。API 実行（→ ${getCategoryPrefix('backend_process', 'BE')}#）で §5.2 バック処理 と連結されます。`),
    // A2（2026-07-18）: h4/h5 は目次(TOC)範囲外のため、カテゴリ内の全処理を俯瞰する一覧表を追加。
    ...buildProcessIndexTable(frontProcesses, 'process_id', 'name', 'overview')
  ];

  // area グルーピング
  const grouped = groupByAreaOrdered(frontProcesses, FRONT_AREA_ORDER);
  let areaIdx = 0;
  for (const { area, items } of grouped) {
    areaIdx++;
    blocks.push(h4(`${prefix}.${areaIdx} ${area}`));

    let procIdx = 0;
    for (const fp of items) {
      procIdx++;
      blocks.push(h5(`${prefix}.${areaIdx}.${procIdx} [${fp.process_id}] ${fp.name || ''}`));
      // v51 §19 1 文 1 概念ルール: overview + overview_details[] を bullet 描画
      //  意味合い: 概要が句点 2+ で「。」連結マルチステートメント状態だった旧式を解消
      //  接続情報: fp.overview / fp.overview_details / 16_docx出力ハンドオフ.md §19
      for (const para of renderDescriptionWithDetails(fp, 'overview', null, '概要: ')) {
        blocks.push(para);
      }

      // B3（2026-07-19）: notes[]（業務担当者への注意書き）を概要直後に callout ボックスで表示
      // 接続情報: 15_中間JSONスキーマ.md front_processes[].notes[] 新設対応、buildNotesCallout() 参照
      for (const noteBlock of buildNotesCallout(fp.notes)) {
        blocks.push(noteBlock);
      }

      // 起動トリガー（called_by_triggers）— v51 憲法: §4 トリガー配下
      if (fp.called_by_triggers && fp.called_by_triggers.length > 0) {
        const triggerRefs = fp.called_by_triggers.map(cb => {
          const kindLabel = cb.group_kind === 'screen_operation' ? '4.1' : (cb.group_kind === 'auto' ? '4.2' : '4.3');
          return `§${kindLabel} ${cb.group_category} (No.${cb.trigger_no})`;
        }).join(' / ');
        blocks.push(p(`呼出元: ${triggerRefs}`));
      }

      // 前提・事後条件
      if (fp.preconditions && fp.preconditions.length > 0) {
        blocks.push(p(`前提条件: ${fp.preconditions.join(' / ')}`));
      }
      if (fp.postconditions && fp.postconditions.length > 0) {
        blocks.push(p(`正常終了時: ${fp.postconditions.join(' / ')}`));
      }

      // 処理ステップ
      const steps = fp.steps || [];
      if (steps.length > 0) {
        // v49: response_mapping kind の出現順を採番（§6.1.X.Y.Z 形式のサブ番号生成用）
        let respMapSubNo = 0;
        const respMapSubNoByStep = new Map();
        for (const s of steps) {
          if (s.kind === 'response_mapping') {
            respMapSubNo += 1;
            respMapSubNoByStep.set(s, respMapSubNo);
          }
        }

        const rows = steps.map(s => {
          let extra = '';
          // 2026-10-06: response_mapping の参照行の本文（下の content の重複判定に使う）
          let respMapText = '';
          if (s.branches && s.branches.length > 0) {
            extra = s.branches.map(b => `${b.condition || ''} → ${b.then || ''}`).join('\n');
          } else if (s.kind === 'backend_call' && s.backend_call) {
            const bc = s.backend_call;
            const respLines = (bc.response_handling || []).map(r => `${r.on || ''} → ${r.then || ''}`).join('\n');
            // v50 Phase 2: 新名 bc.backend_ref 優先、旧名 bc.backend_id フォールバック
            extra = `→ [${bc.backend_ref || bc.backend_id || ''}] (§5.2 参照)` +
                    (bc.request_summary ? `／リクエスト: ${bc.request_summary}` : '') +
                    (respLines ? `\n${respLines}` : '');
          } else if (s.kind === 'response_mapping' && s.response_mapping) {
            // v49: response_mapping step は専用の小節（§6.1.X.Y.Z）への参照リンクを処理内容に表示
            const sub = respMapSubNoByStep.get(s) || '?';
            // v50 Phase 2: 新名 backend_ref 優先、旧名 backend_id フォールバック
            const bid = s.response_mapping.backend_ref || s.response_mapping.backend_id || '';
            respMapText = `[${bid}] のレスポンスを画面項目にマッピング`;
            extra = `→ ${respMapText} (§${prefix}.${areaIdx}.${procIdx}.${sub} 参照)`;
          } else if (s.kind === 'logging_call' && (s.logging_ref || s.call_id)) {
            // v50 Phase 2: 新名 s.logging_ref 優先、旧名 s.call_id フォールバック
            extra = `→ [${s.logging_ref || s.call_id}] (§5.7 参照)`;
          } else if (s.kind === 'transition') {
            const m = (s.description || '').match(/[\w-]+\.html(?:\?[^\s。、]+)?/);
            extra = m ? `→ ${m[0]}（画面遷移）` : '→ （画面遷移）';
          } else if (s.kind === 'api_call' && (s.api_path || s.endpoint)) {
            extra = `→ ${s.api_path || s.endpoint}（API）`;
          }
          // fallback-ok: description 未設定の step は空の処理内容として表示する（表示用。業務値ではない）
          const desc = s.description || '';
          // 2026-10-06: response_mapping で description が参照行の本文（「[BE1] のレスポンスを画面項目にマッピング」）
          //   を含むなら、参照行だけを出す。意味合い: 同じ文が「→ … (§… 参照)」付きで2行続いていた。
          //   接続情報: extra は上の response_mapping 分岐で作る。他の kind は従来どおり includes で重複判定する。
          const content = (respMapText && desc.includes(respMapText)) ? extra
            : (extra && !desc.includes(extra.split('\n')[0])) ? `${desc}\n${extra}` : desc;
          // v51 §19 1 文 1 概念ルール: description_details があれば tdCellWithDetails で bullet 描画
          //  意味合い: step.description は元々 extra 情報を末尾連結する 1 セル内で複雑な構造。
          //           description_details の bullet は概要行の後に出すため、合成オブジェクトを作る。
          //  接続情報: tdCellWithDetails で「概要 + bullet 詳細」を表現、extra は概要に \n 連結済
          const stepWidthRatios = [1, 2, 13];
          const stepTableWidth = CONTENT_WIDTH - currentIndent;
          const _tot = stepWidthRatios.reduce((a, b) => a + b, 0);
          const _ws = stepWidthRatios.map(r => Math.floor(stepTableWidth * r / _tot));
          _ws[_ws.length - 1] += (stepTableWidth - _ws.reduce((a, b) => a + b, 0));
          const descCell = (Array.isArray(s.description_details) && s.description_details.length > 0)
            ? tdCellWithDetails({ description: content, description_details: s.description_details }, 'description', _ws[2])
            : content;
          return [
            String(s.step_no || ''),
            mapKindToLabel(s.kind),
            descCell
          ];
        });
        blocks.push(buildTable(
          // v47: 3 列構造（Step / 種別 / 処理内容）。呼出・参照は処理内容に統合
          // v48: 「Step」は step カテゴリの header_self（フロント処理表）
          [getIdHeaderLabel('step', 'self'), '種別', '処理内容'],
          rows,
          [1, 2, 13]
        ));

        // v49: response_mapping step ごとに専用小節「§6.1.X.Y.Z [BE#] レスポンス → 画面項目マッピング」を出力
        // 意味合い: バック処理章 §6.2 BEX が「JSON 構造」までを担当し、画面への変換はフロント章 §6.1 FRX に責務移譲（v49 本質変更）。
        //          表は 4 列構成: { No / 画面項目（AR# / IT# / 名称統合） / API フィールド / JS 変換 }
        for (const s of steps) {
          if (s.kind !== 'response_mapping' || !s.response_mapping) continue;
          const sub = respMapSubNoByStep.get(s) || '?';
          const rm = s.response_mapping;
          // v50 Phase 2: 新名 rm.backend_ref 優先、旧名 rm.backend_id フォールバック
          const bid = rm.backend_ref || rm.backend_id || '';
          const mappings = rm.mappings || [];
          blocks.push(h6(`${prefix}.${areaIdx}.${procIdx}.${sub} [${bid}] レスポンス → 画面項目マッピング`));
          blocks.push(p(`[${bid}] が返した JSON レスポンス（→ §5.2 該当バック処理の出力表を参照）の各キーを画面項目にどう載せるか。フロント側の変換のみ記載（書式整形・抽出・配列展開等）。バック側の変換は §5.2 該当 [BE#] 出力表「変換」列を参照。`));
          if (mappings.length === 0) {
            blocks.push(p('（マッピング未定義）'));
            continue;
          }
          const mapRows = mappings.map((m, i) => {
            // 画面項目列: AR# / IT# / 名称 を 1 列に統合（v49 4 列構造）
            const ar = m.screen_area_no || '';
            // v50 Phase 2: 新名 m.item_ref 優先、旧名 m.item_ref_no フォールバック
            const it = m.item_ref || m.item_ref_no || '';
            const nm = m.screen_item_name || '—';
            const screenItemCell = [ar, it, nm].filter(x => x).join(' / ');
            return [
              String(i + 1),
              screenItemCell,
              m.api_field_path || '',
              m.transform || ''
            ];
          });
          blocks.push(buildTable(
            ['No', '画面項目', 'APIフィールド', 'JS 変換'],
            mapRows,
            [1, 6, 5, 4]
          ));
        }
      }

      // バリデーション/計算式/共通ロジック の参照
      const refs = [];
      // v51 憲法: 章番号を §5.3 / §5.4 / §5.5 に。「使用カタログ」→「呼出先」
      if (fp.validation_refs && fp.validation_refs.length > 0) refs.push(`バリデーション: ${fp.validation_refs.join(', ')} (§5.3 参照)`);
      if (fp.calculation_refs && fp.calculation_refs.length > 0) refs.push(`計算式: ${fp.calculation_refs.join(', ')} (§5.4 参照)`);
      if (fp.common_logic_refs && fp.common_logic_refs.length > 0) refs.push(`共通ロジック: ${fp.common_logic_refs.join(', ')} (§5.5 参照)`);
      if (refs.length > 0) blocks.push(p(`呼出先カタログ: ${refs.join(' / ')}`));
    }
  }
  return blocks;
}

/**
 * H3 サブ: バックエンド処理一覧（v18 で新設、§6.2 として呼ばれる）
 *
 * 目的: §6.2 で B001〜B0NN のバックエンド処理を H4 で展開し、各処理のエンドポイント／リクエスト仕様／内部処理／レスポンス仕様 を示す。
 *
 * 接続情報: 入力 = data.backend_processes / 出力 = h3('5.2 バックエンド処理') + h4(BE#) + 表
 *           called_by_front[] からフロント呼出元（§6.1 F00X）への逆参照リンク表示
 *           1 endpoint = 1 process が原則
 */
function sectionBackendProcesses(backendProcesses, maps, opts = {}) {
  const { prefix = '5.2' } = opts;
  if (!backendProcesses || backendProcesses.length === 0) return [];
  const blocks = [
    h3(`${prefix} バックエンド処理`),
    // v50.5: 旧形式 B001〜 ハードコードを id_scheme.json prefix 経由で動的化
    p(`フロント処理（§5.1）から呼ばれるバック側の処理本体です。各処理は ${getCategoryPrefix('backend_process', 'BE')}1〜の連番 ID で識別され、業務機能カテゴリ（エリア）でグルーピングしています。1 API エンドポイント (method + path + action) = 1 バック処理 の原則で対応します。`),
    // A2（2026-07-18）: h4/h5 は目次(TOC)範囲外のため、カテゴリ内の全処理を俯瞰する一覧表を追加。
    ...buildProcessIndexTable(backendProcesses, 'process_id', 'name', 'overview')
  ];

  // v19: area グルーピング
  const grouped = groupByAreaOrdered(backendProcesses, BACKEND_AREA_ORDER);
  let areaIdx = 0;
  for (const { area, items } of grouped) {
    areaIdx++;
    blocks.push(h4(`${prefix}.${areaIdx} ${area}`));

    let procIdx = 0;
    for (const bp of items) {
      procIdx++;
      blocks.push(h5(`${prefix}.${areaIdx}.${procIdx} [${bp.process_id}] ${bp.name || ''}`));
      // v51 §19 1 文 1 概念ルール: overview + overview_details[] を bullet 描画
      //  意味合い: バック処理概要が句点 2+ で「。」連結マルチステートメント状態だった旧式を解消
      //  接続情報: bp.overview / bp.overview_details / 16_docx出力ハンドオフ.md §19
      for (const para of renderDescriptionWithDetails(bp, 'overview', null, '概要: ')) {
        blocks.push(para);
      }

      // B3（2026-07-19）: notes[]（業務担当者への注意書き）を概要直後に callout ボックスで表示
      // 接続情報: 15_中間JSONスキーマ.md backend_processes[].notes[] 新設対応、buildNotesCallout() 参照
      for (const noteBlock of buildNotesCallout(bp.notes)) {
        blocks.push(noteBlock);
      }

    // 呼出元フロント処理（called_by_front）
    if (bp.called_by_front && bp.called_by_front.length > 0) {
      blocks.push(p(`呼出元フロント処理: ${bp.called_by_front.join(', ')}（§5.1 参照）`));
    }

    // エンドポイント
    const ep = bp.endpoint || {};
    blocks.push(p(`エンドポイント: ${ep.method || ''} ${ep.path || ''} [${ep.action || ''}] / 認証: ${ep.auth || ''}`));

    // v49: 【リクエスト仕様】→【①リクエスト受信】に再構成
    // 意味合い: §6.2 BEX を 5 段業務フロー（①受信 / ②DB問合せ / ③JSON組立 / ⑤画面反映 / 画面側変換 = §6.1 へ）に整理。
    //          ④ は ③ JSON 構造そのものなので独立小節を持たず ③ に含む。⑤ はフロント側 §6.1 FRX 配下に移動済。
    const reqParams = (bp.request_spec && bp.request_spec.params) || [];
    if (reqParams.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【①リクエスト受信】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 60, after: 40 }
      }));
      const reqRows = reqParams.map(r => [
        r.name || '', r.type || '', r.required ? '○' : '—',
        r.location || '', r.validation || '', r.description || ''
      ]);
      blocks.push(buildTable(
        ['名称', '型', '必須', '場所', '検証', '説明'],
        reqRows,
        [3, 2, 1, 1, 4, 4]
      ));
    }

    // v49: 【内部処理】→【②DB問合せ】に再構成
    //      内容は db_operation step のみに絞る。観測点ログ呼出 step は §6.7 への参照リンクを末尾サマリにまとめる。
    //      response_build step は ③ JSON レスポンス表の transform_backend 列で表現するため独立表示しない。
    //      バリデーション step は VL# 参照（使用カタログ）で代替するため独立表示しない。
    const processing = bp.processing || [];
    const dbOpSteps = processing.filter(s => s.kind === 'db_operation' && s.db_op_detail);
    if (dbOpSteps.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【②DB問合せ】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 60, after: 40 }
      }));
      blocks.push(p(`${dbOpSteps.length} 本の DB 操作を順次実行する。各操作の SQL 詳細（SELECT項目 / FROM・JOIN / WHERE / 集計・計算 / ORDER BY）を以下の小節（②-1 ②-2 ...）で展開する。`));
      // 各 db_operation を ②-N サブ番号で展開
      // 意味合い: db_op_detail.sub_no（migrate_v48_to_v49.py で採番）を「②-N」表示に組み立てる。
      //          SQL 識別子は v49 で DB# 廃止、§6.2 BEX 内の章内サブ番号で完結。
      for (const s of dbOpSteps) {
        const dbOp = s.db_op_detail || {};
        const subNo = dbOp.sub_no || '?';
        const subTitle = dbOp.dataset_name || s.description || `データ操作 ${subNo}`;
        const opType = dbOp.operation_type || '';
        blocks.push(new Paragraph({
          children: [new TextRun({ text: `②-${subNo} ${subTitle}${opType ? `（${opType}）` : ''}`, font: FONT_BODY, size: SIZE_BODY, bold: true })],
          spacing: { before: 60, after: 30 }
        }));
        renderDbOperationDetail(blocks, dbOp, maps);
      }
    }

    // 内部処理の旧表（v48 までの【内部処理】3 列構造）は v49 で削除。
    // 観測点ログ呼出のサマリを末尾【観測点ログ】で表示する（後述）。
    if (false) {  // v48 旧コード（無効化、後で削除予定）
      const procRows = processing.map(s => {
        // v47: フロント表と同じく「処理内容」に呼出・参照を統合（3 列構造）
        let ref = '';
        if (s.kind === 'cache_op' && s.cache_op) {
          const co = s.cache_op;
          // 2026-10-05 汎用化: 固定の製品名をやめ、CACHE_LABEL（project_config の docx_output.cache_label、既定「キャッシュ」）を出す。
          // fallback-ok: 既存の表示用の既定値（任意項目 operation / key_pattern / ttl_sec が無いときの空文字・「—」）。今回の変更は先頭の語だけ
          ref = `→ ${CACHE_LABEL} ${co.operation || ''} ${co.key_pattern || ''} (TTL: ${co.ttl_sec || '—'})`;
        } else if (s.kind === 'db_operation' && s.db_op_detail) {
          const dbo = s.db_op_detail;
          const dbRef = dbo.id ? `[${dbo.id}] ` : '';
          ref = `→ ${dbRef}${dbo.operation_type || ''} on ${dbo.dataset_name || ''} (§5.6 参照)`;
        } else if (s.kind === 'logging_call' && (s.logging_ref || s.call_id)) {
          // v50 Phase 2: 新名 s.logging_ref 優先、旧名 s.call_id フォールバック
          ref = `→ [${s.logging_ref || s.call_id}] (§5.7 参照)`;
        }
        const desc = s.description || '';
        const content = (ref && !desc.includes(ref.split('\n')[0])) ? `${desc}\n${ref}` : desc;
        return [
          String(s.step_no || ''),
          mapBackendKindToLabel(s.kind),
          content
        ];
      });
      blocks.push(buildTable(
        // v47: フロント表と同じ 3 列構造（Step / 種別 / 処理内容）
        // v48: 「Step」は step カテゴリの header_self（バックエンド処理表）
        [getIdHeaderLabel('step', 'self'), '種別', '処理内容'],
        procRows,
        [1, 2, 13]
      ));

      // DB操作のSQL詳細（db_op_detail がある場合、各DB操作についてサブ表示）
      for (const s of processing) {
        if (s.kind === 'db_operation' && s.db_op_detail && (s.db_op_detail.dataset || s.db_op_detail.insert_values || s.db_op_detail.update_values)) {
          renderDbOperationDetail(blocks, s.db_op_detail, maps);
        }
      }
    }

    // v49: 【レスポンス仕様】→【③JSONレスポンス】に再構成
    //      列構成を v48 から拡張: { No / JSON キー / 表示名 / 型 / データ起源 / バック側の加工 }
    //      「データ起源」「バック側の加工」列で SQL → バック側の加工チェーンを業務語彙で辿れるようにする（v49 本質変更）
    //      意味合い: 見出しと直下の固定文は、実装言語と問合せの本数に依らない言い方にする
    //               （問合せが1本の処理や更新系の処理でも事実と食い違わないため）。
    //      接続情報: 見出しは下の buildTable の第1引数、固定文は【③JSONレスポンス】直下の p()。
    const respSpec = bp.response_spec || {};
    const respItems = respSpec.items || [];
    if (respItems.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: `【③JSONレスポンス】 ${respSpec.pattern || ''}`, font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 60, after: 40 }
      }));
      blocks.push(p('DB 問合せの結果を統合・加工してフロントに返す JSON 構造。「データ起源」列で「どの SELECT のどの列から来たか」「バック側の算出か」を辿れる。フロント側の画面表示変換は §5.1 該当フロント処理を参照。'));
      const respRows = respItems.map(r => {
        // v49: source / transform_backend が migrate_v48_to_v49 で追加される。
        //      未設定の場合は空文字でフォールバック（後段 Agent or 手動編集で埋める前提）。
        const src = r.source || {};
        const srcType = src.type || '';
        const srcRef = src.source_ref || '';
        const srcCol = src.source_column || '';
        // 「データ起源」列の組立: type / source_ref / source_column を組み合わせる
        let srcText;
        if (srcType === 'request') {
          srcText = 'リクエスト透過';
        } else if (srcType === 'db' && srcRef) {
          srcText = srcCol ? `${srcRef} / ${srcCol}` : srcRef;
        } else if (srcType === 'calculated') {
          srcText = srcRef ? `${srcRef} + バック計算` : 'バック計算';
        } else if (srcType === 'merged') {
          srcText = srcRef ? `${srcRef}（複数 SELECT 統合）` : '複数 SELECT 統合';
        } else if (srcType === 'const') {
          srcText = '固定値（バック定数）';
        } else {
          srcText = srcRef || srcCol || (r.expression ? '計算式' : '');
        }
        const transformBackend = r.transform_backend || (srcType === 'request' || srcType === '' ? '' : '—');
        return [
          String(r.no || ''),
          r.key || '',
          r.display_name || '',
          r.data_type || '',
          srcText,
          transformBackend
        ];
      });
      // v48: 「No」は api カテゴリの header_self
      // v49: 列構成を 5 → 6 列に拡張（データ起源 / バック側の加工 追加）
      blocks.push(buildTable(
        [getIdHeaderLabel('api', 'self'), 'JSON キー', '表示名', '型', 'データ起源', 'バック側の加工'],
        respRows,
        [1, 4, 2, 1, 4, 4]
      ));
    }

    // v49: 【レスポンスの画面表示先】表は §6.1 FRX に移動済（migrate_v48_to_v49.py が response_mapping kind step として挿入）
    //      よって §6.2 BEX からは削除。


    // エラーパターン
    const errPatterns = bp.error_patterns || [];
    if (errPatterns.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【エラーパターン】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 60, after: 40 }
      }));
      const errRows = errPatterns.map(e => [
        e.condition || '',
        // fallback-ok: 旧い中間データは status_code を http_status という名前で持つ。どちらも無ければ空欄
        String(e.status_code ?? e.http_status ?? ''),
        e.message_id || ''
      ]);
      blocks.push(buildTable(
        ['発生条件', 'ステータス', 'メッセージID'],
        errRows,
        [7, 2, 3]
      ));
    }
    }  // for items の閉じ
  }  // for grouped (area) の閉じ
  return blocks;
}

/**
 * processing[].kind を日本語ラベルに変換（バックエンド側）
 * v49: ラベル統一規約に合わせて整理。観測点ログ等の補助 step は §6.7 参照に委譲
 */
function mapBackendKindToLabel(kind) {
  const map = {
    validation: 'バリデーション',
    db_operation: 'DB問合せ',  // v49: 「DB操作」→「DB問合せ」（業務担当者語彙）
    cache_op: 'キャッシュ操作',
    calculation: '計算',
    response_build: 'レスポンス組立',
    transaction: 'トランザクション',
    error_handling: 'エラー処理',
    logging: '操作ログ',
    logging_call: 'ログ呼出'
  };
  return map[kind] || kind || '—';
}

/**
 * DB操作の SQL 詳細をブロックに追加（v18: backend_processes 内の SELECT/INSERT/UPDATE/DELETE 詳細）
 *
 * 意味合い: 各 backend_processes[].processing[].db_op_detail の dataset / joins / where / order_by /
 *           insert_values / update_values を v17 sectionDbOperations と同じ表形式で表示する。
 *           backend_processes 内に統合されたため、独立した §12 章は v18 で廃止。
 */
function renderDbOperationDetail(blocks, dbOp, maps) {
  if (!dbOp) return;
  // dataset
  if (dbOp.dataset && dbOp.dataset.length > 0) {
    blocks.push(new Paragraph({
      children: [new TextRun({ text: `  ▸ ${dbOp.id || ''} データセット定義`, font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 40, after: 30 }
    }));
    const rows = dbOp.dataset.map(d => [
      String(d.no || ''),
      withPhysical(d.logical_table, 'table', maps),
      withPhysical(d.logical_column, 'column', maps)
    ]);
    // v48: dbOp.dataset の「No」は db_operation カテゴリの header_self（データセット定義表）
    blocks.push(buildTable([getIdHeaderLabel('db_operation', 'self'), 'テーブル（論理[物理]）', '列（論理[物理]）'], rows, [1, 4, 5]));
  }
  // joins
  if (dbOp.joins && dbOp.joins.length > 0) {
    const rows = dbOp.joins.map(j => [
      j.type || '', withPhysical(j.logical_table, 'table', maps), j.condition || ''
    ]);
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '  ▸ 結合条件', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 40, after: 30 }
    }));
    blocks.push(buildTable(['結合', 'テーブル', '条件'], rows, [2, 4, 6]));
  }
  // where
  if (dbOp.where && dbOp.where.length > 0) {
    const rows = dbOp.where.map(w => [
      w.and_or || '', withPhysical(w.logical_table, 'table', maps),
      withPhysical(w.logical_column, 'column', maps),
      w.operator || '', w.param_no || '', w.param_name || ''
    ]);
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '  ▸ 抽出条件', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 40, after: 30 }
    }));
    blocks.push(buildTable(
      ['AND/OR', 'テーブル', '列', '条件', 'param#', 'パラメータ'],
      rows,
      [1, 3, 3, 1, 1, 3]
    ));
  }
  // insert_values
  if (dbOp.insert_values && dbOp.insert_values.length > 0) {
    const rows = dbOp.insert_values.map(v => [
      String(v.no || ''), withPhysical(v.logical_column, 'column', maps), v.value || ''
    ]);
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '  ▸ 登録値', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 40, after: 30 }
    }));
    // v48: dbOp.insert_values の「No」は db_operation カテゴリの header_self（登録値表）
    blocks.push(buildTable([getIdHeaderLabel('db_operation', 'self'), '列', '値'], rows, [1, 4, 7]));
  }
  // update_values
  if (dbOp.update_values && dbOp.update_values.length > 0) {
    const rows = dbOp.update_values.map(v => [
      String(v.no || ''), withPhysical(v.logical_column, 'column', maps), v.value || ''
    ]);
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '  ▸ 更新値', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 40, after: 30 }
    }));
    // v48: dbOp.update_values の「No」は db_operation カテゴリの header_self（更新値表）
    blocks.push(buildTable([getIdHeaderLabel('db_operation', 'self'), '列', '値'], rows, [1, 4, 7]));
  }
}

/**
 * H2: 処理詳細（v18 完成版：呼ばれる側 = フロント/バックエンド処理 + カタログ）
 *
 * 目的: docx §6 で「呼ばれる側」の全体を表現。
 *       §6.1 フロント処理（F001〜）/ §6.2 バックエンド処理（B001〜）/ §6.3 バリデーション / §6.4 計算式 / §6.5 共通ロジック / §6.6 パラメータセット
 *
 * 接続情報: 入力 = data (front_processes / backend_processes / validations / calculations / common_logic / parameters)、maps
 *           v17 までの process_flows[] による業務フェーズ縦糸構造は v18 で廃止（呼ぶ側＝トリガー網羅で代替）
 */
function sectionProcesses(data, maps) {
  // 導入文は、各章を組み立て終えた後に見出しの直後へ差し込む（この関数の末尾を参照）。
  const blocks = [
    h2('5. 処理詳細')
  ];
  // §6.1 フロント処理
  blocks.push(...sectionFrontProcesses(data.front_processes, { prefix: '5.1' }));
  // §6.2 バックエンド処理
  blocks.push(...sectionBackendProcesses(data.backend_processes, maps, { prefix: '5.2' }));
  // §6.3 バリデーション
  blocks.push(...sectionValidations(data.validations, { prefix: '5.3', level: 'h3' }));
  // §6.4 計算式（B1: derivation_chain 図埋め込みのため data.diagrams を opts 経由で渡す）
  blocks.push(...sectionCalculations(data.calculations, { prefix: '5.4', level: 'h3', diagrams: data.diagrams }));
  // §6.5 共通ビジネスロジック（B1: derivation_chain 図埋め込みのため data.diagrams を opts 経由で渡す）
  const commonLogicBlocks = sectionCommonLogic(data.common_logic, { prefix: '5.5', level: 'h3', diagrams: data.diagrams });
  blocks.push(...commonLogicBlocks);
  // §5.6 DB 操作（v51 復活、SQL 構造化表で出力）
  const dbOperationBlocks = sectionDbOperations(data.db_operations, maps, data, { prefix: '5.6', level: 'h3' });
  blocks.push(...dbOperationBlocks);
  // §5.7 観測点ログ処理（共通） — [LG1]-[LG8] + サブ [LG4-a]-[LG4-f]
  const hasLogging = !!(data.common_logging_processes && data.common_logging_processes.length > 0);
  if (hasLogging) {
    blocks.push(...sectionCommonLoggingProcesses(data.common_logging_processes, { prefix: '5.7' }));
  }
  // v51: §6.6 パラメータは廃止（処理の入力表に統合済）
  // 目的: 導入文のカタログ列挙を、実際に出力した章だけにする。
  // 意味合い: §5.5 共通ロジック / §5.6 DB 操作 / §5.7 観測点ログは、データが有るときだけ章が出る。
  //           常に3つとも書くと、章が出ない文書で指し先の無い章番号を読者に見せてしまう。
  //           3つとも出る文書では、従来と同じ文面になる。
  //           各章の関数を呼ぶ順序を変えないため、組み立て後に見出しの直後へ差し込む。
  // 接続情報: 判定の元 = 上の sectionCommonLogic / sectionDbOperations の戻り値が空かどうかと、
  //           data.common_logging_processes の有無。差し込み先 = blocks の先頭（h2）の直後
  const catalogs = [
    'バリデーション §5.3',
    '計算式 §5.4',
    commonLogicBlocks.length > 0 && '共通ロジック §5.5',
    dbOperationBlocks.length > 0 && 'DB 操作 §5.6',
    hasLogging && '観測点ログ §5.7'
  ].filter(Boolean).join(' / ');
  blocks.splice(1, 0, p(`§4 で起動された各処理の本体です。フロント側（§5.1）とバック側（§5.2）に責務を分離し、フロント処理の backend_call ステップで両者が連結されます。処理が呼出すカタログ（${catalogs}）が続きます。`));
  return blocks;
}

/**
 * H2: 処理詳細（v16 で §6「初期処理」から「処理詳細（業務フェーズ構造）」に拡張）
 *
 * 目的: docx §6 を業務フェーズ単位（6.1 初期処理 → 6.2 データ編集 → … → 6.7 帳票出力）の縦糸構造で表現する。
 *       業務担当者が画面を時系列で読めるようにする「俯瞰索引」を提供する。
 *
 * 意味合い: process_flows[] が縦糸（業務フェーズ別）、event_processes[] が横糸（事象別個別シート）。
 *           本関数は縦糸を担当し、詳細は events[]/event_processes[]/api_spec[]/db_operations[] への参照で委譲する（二重記述しない）。
 *
 * 接続情報: 入力 = data.process_flows[] / data.initialization
 *           - process_flows[] があれば業務フェーズ構造で展開（v16〜）
 *           - process_flows[] が空 or 未定義なら従来動作（initialization のみ）にフォールバック
 *           - 初期処理フェーズ（phase_no=6.1 or process_flows[0]）に initialization.items の画面項目テーブルを末尾追加
 *           - 仕様根拠: 15_中間JSONスキーマ.md「process_flows」、03_初期処理.md「§6 処理詳細 抽出サブスキル」
 *
 * v17 で追加: フェーズ末尾に §6.8 バリデーション / §6.9 計算式 / §6.10 API仕様 / §6.11 DB操作 /
 *             §6.12 パラメータセット / §6.13 共通ビジネスロジック を「処理に使う部品カタログ」として配下統合。
 *             これにより §6 だけで処理が完結し、§10/§11/§12/§14 等の独立章を廃止。
 *             レビュー指摘「§10 計算式は処理だから §6 配下にあるべき、§14 共通ビジネスロジックも同様」への対応。
 */
function sectionProcessDetails(data, maps) {
  const init = data.initialization || {};
  const flows = data.process_flows || [];
  const blocks = [
    h2('5. 処理詳細'),
    p('当画面の処理を業務フェーズ単位で時系列に整理し（6.1〜6.7）、その後に処理で使う部品カタログ（バリデーション・計算式・API仕様・DB操作・パラメータセット・共通ビジネスロジック、6.8〜6.13）を続けます。')
  ];

  if (flows.length === 0) {
    // フォールバック: process_flows が空なら旧構造（初期処理単体）で表示
    blocks.push(h3('6.1 初期処理の概要'));
    blocks.push(p(init.overview || '—'));
    blocks.push(h3('6.2 初期化の経路'));
    blocks.push(p(`パターン: ${init.pattern || '—'}（外部アプリ連携／DB取得／基本 のいずれか）`));
    if (init.items && init.items.length > 0) {
      blocks.push(h3('6.3 初期表示画面項目'));
      blocks.push(p('初期化完了後における各画面項目の活性／表示／初期値を示します。'));
      // v37: 「画面#」を「エリア#」に統一（§4.2/§4.3 表記と整合）
      const _areas63 = (data.screen_layout && data.screen_layout.areas) || [];
      const rows = init.items.map(it => [
        String(it.no || ''),
        formatAreaNo(it.screen_no, _areas63),
        it.item_name || '',
        it.active || '',
        it.visible || '',
        it.initial_value || ''
      ]);
      // v48: 初期表示画面項目表 — 「No」は item カテゴリの header_self、「エリア#」は area カテゴリの header_ref
      blocks.push(buildTable(
        [getIdHeaderLabel('item', 'self'), getIdHeaderLabel('area', 'ref'), '項目名', '活性', '表示', '初期値'],
        rows,
        [1, 1, 5, 1, 1, 3]
      ));
    }
    return blocks;
  }

  // 業務フェーズ縦糸構造（v16〜）
  // 各フェーズ: H3 名前 / 概要 / 前提・事後条件（任意） / トリガーイベント一覧 / 処理ステップ表
  for (const flow of flows) {
    const phaseHeader = `${flow.phase_no || ''} ${flow.phase_name || '（フェーズ名未設定）'}`;
    blocks.push(h3(phaseHeader));
    blocks.push(p(flow.overview || '—'));

    // 前提条件
    if (flow.preconditions && flow.preconditions.length > 0) {
      blocks.push(p('【前提条件】'));
      for (const pre of flow.preconditions) {
        blocks.push(p(`・${pre}`));
      }
    }
    // 事後条件
    if (flow.postconditions && flow.postconditions.length > 0) {
      blocks.push(p('【正常終了時の状態】'));
      for (const post of flow.postconditions) {
        blocks.push(p(`・${post}`));
      }
    }

    // トリガーイベント
    if (flow.trigger_events && flow.trigger_events.length > 0) {
      blocks.push(p(`【トリガーイベント】 ${flow.trigger_events.join(', ')}`));
    }

    // 処理ステップ表
    const steps = flow.steps || [];
    if (steps.length > 0) {
      // v51 §19 1 文 1 概念ルール: 処理内容セルは tdCellWithDetails で bullet 描画
      //  意味合い: legacy process_flows フォールバック表でも step.description_details[] を反映
      //  接続情報: s.description / s.description_details / 16_docx出力ハンドオフ.md §19
      const procColRatios = [1, 2, 7, 5, 5];
      const procTableWidth = CONTENT_WIDTH - currentIndent;
      const _totP = procColRatios.reduce((a, b) => a + b, 0);
      const procColWidths = procColRatios.map(r => Math.floor(procTableWidth * r / _totP));
      procColWidths[procColWidths.length - 1] += (procTableWidth - procColWidths.reduce((a, b) => a + b, 0));

      const rows = steps.map(s => {
        // 分岐の表示: 「条件1: then1 / 条件2: then2」形式
        let branchText = '';
        if (s.branches && s.branches.length > 0) {
          branchText = s.branches.map(b => {
            const elsePart = b.else ? ` / 該当外: ${b.else}` : '';
            return `${b.condition || ''} → ${b.then || ''}${elsePart}`;
          }).join('\n');
        }

        // 参照情報の連結（API / DB / イベント / 画面更新 / メッセージ）
        const refs = [];
        if (s.event_ref) refs.push(`イベント: ${s.event_ref}`);
        if (s.api_ref) refs.push(`API: ${s.api_ref}`);
        // v50 Phase 2: 新名 s.db_op_refs 優先、旧名 s.db_op_ref フォールバック
        const _dbOpList = s.db_op_refs || s.db_op_ref || [];
        if (_dbOpList.length > 0) refs.push(`DB操作: ${_dbOpList.join(', ')}`);
        if (s.screen_updates_ref) refs.push(`画面更新: ${s.screen_updates_ref}`);
        if (s.message_ref) refs.push(`メッセージ: ${s.message_ref}`);

        const descCell = (Array.isArray(s.description_details) && s.description_details.length > 0)
          ? tdCellWithDetails(s, 'description', procColWidths[2])
          : (s.description || '');

        return [
          String(s.step_no || ''),
          mapKindToLabel(s.kind),
          descCell,
          branchText || '—',
          refs.length > 0 ? refs.join('\n') : '—'
        ];
      });

      // v48: 旧 5 列構造（process_flows フォールバック）— 「Step」は step カテゴリの header_self
      blocks.push(buildTable(
        [getIdHeaderLabel('step', 'self'), '種別', '処理内容', '条件分岐', '参照（イベント・API・DB操作・画面更新）'],
        rows,
        procColRatios
      ));
    }

    // 初期処理フェーズ（6.1 相当）の末尾に既存 initialization.items の画面項目テーブルを追加
    // 判定: phase_no === "6.1" または trigger_events に "EV00" を含む
    const isInitPhase = (flow.phase_no === '6.1') ||
                       (flow.trigger_events || []).includes('EV00');
    if (isInitPhase && init.items && init.items.length > 0) {
      blocks.push(p(''));  // 空行スペーサ
      blocks.push(p('【初期表示画面項目】 初期化完了後（業務担当者が操作開始できる状態）における各画面項目の活性／表示／初期値を示します。初期値は業務的な条件で変わるケースがあり、その場合は「データあり時／データなし時」のように条件分岐表現で記載します。'));
      // v37: 「画面#」を「エリア#」に統一
      const _areas61init = (data.screen_layout && data.screen_layout.areas) || [];
      const itemRows = init.items.map(it => [
        String(it.no || ''),
        formatAreaNo(it.screen_no, _areas61init),
        it.item_name || '',
        it.active || '',
        it.visible || '',
        it.initial_value || ''
      ]);
      // v48: 初期表示画面項目表（初期処理フェーズ末尾、process_flows 経路）— item self + area ref
      blocks.push(buildTable(
        [getIdHeaderLabel('item', 'self'), getIdHeaderLabel('area', 'ref'), '項目名', '活性', '表示', '初期値'],
        itemRows,
        [1, 1, 5, 1, 1, 3]
      ));
    }
  }

  // v17: §6 配下に「処理で使う部品カタログ」を統合（6.8〜6.13）
  // 業務フェーズ縦糸（6.1〜6.7）が組立図、ここからは部品カタログという役割分担を章構成で明示する
  // v49: §6.11 DB操作 セクション廃止。SQL 詳細は §6.2 BEX 内の【②DB問合せ】で展開され、§6.11 への参照は不要。
  //       章番号も繰上げ（6.11 → 6.11 パラメータ、6.12 → 6.12 共通ビジネスロジック）。
  blocks.push(...sectionValidations(data.validations, { prefix: '6.8', level: 'h3' }));
  blocks.push(...sectionCalculations(data.calculations, { prefix: '6.9', level: 'h3', diagrams: data.diagrams }));
  blocks.push(...sectionApiSpec(data.api_spec, { prefix: '6.10', level: 'h3', areas: (data.screen_layout && data.screen_layout.areas) || [] }));
  // v49: sectionDbOperations 廃止（§6.2 BEX 内に展開済）
  blocks.push(...sectionParameters(data.parameters, { prefix: '6.11', level: 'h3' }));
  blocks.push(...sectionCommonLogic(data.common_logic, { prefix: '6.12', level: 'h3', diagrams: data.diagrams }));

  return blocks;
}

/**
 * process_flows[].steps[].kind を日本語ラベルに変換するヘルパー
 *
 * 接続情報: 入力 = kind (string) / 出力 = 業務担当者語彙のラベル
 *           15_中間JSONスキーマ.md「front_processes（呼ばれるフロント処理）」節のフィールド表の steps[].kind の行と整合する変換表
 */
function mapKindToLabel(kind) {
  const map = {
    data_load: 'データ読込',
    branch: '条件分岐',
    validation: 'バリデーション',
    confirmation: '確認ダイアログ',
    // v49: backend_call / api_call の表示ラベルを「API実行」に統一（業務担当者語彙）。
    //      内部実装上の kind 値（backend_call / api_call）は互換性のため維持、ラベルだけ統合。
    api_call: 'API実行',
    backend_call: 'API実行',
    screen_update: '画面更新',
    response_mapping: '画面表示変換',  // v49: バックレスポンス → 画面項目の変換 step。§6.1.X.X.X マッピング表へのリンク種別
    state_update: '状態更新',
    calculation: '計算',
    transition: '画面遷移',
    event_emit: '通知発信',
    error_handling: 'エラー処理',
    logging: '操作ログ',
    logging_call: 'ログ呼出'
  };
  return map[kind] || kind || '—';
}

// v15 互換用エイリアス（既存呼出は sectionInitialization のまま動作する）
const sectionInitialization = (init) => sectionProcessDetails({ initialization: init, process_flows: [] });


/**
 * §6.7 観測点ログ処理（共通） — v40 で新設
 *
 * 目的: 観測点ログ処理を [L1]-[L8] + サブ [L4a]-[L4f] の独立した共通処理として表示。
 *       各 F00X / B00X の steps からは「[LN] を呼ぶ」と参照するだけで、本セクションが処理本体を担う。
 *
 * 意味合い: レビュー指摘「ログ処理が業務処理 step 内に直書きされて重複している」への対応。
 *           ロギングは業務処理とは別の独立した責務であり、共通処理として一度定義し各業務処理から呼出する構造。
 *
 * 接続情報: 入力 = data.common_logging_processes / 出力 = h3('5.7 観測点ログ処理（共通）') + h4(各 [LG#])
 *           参照: apply_logging_steps.py / project_config.json logging.observation_points
 */
function sectionCommonLoggingProcesses(processes, opts = {}) {
  // v50.5 phase 2.5.1: フォーマットを §6.1 フロント処理 / §6.2 バックエンド処理 と統一。
  // 意味合い: レビュー指摘「§6.7 観測点ログ処理（共通）は他とフォーマットが違う」への対応。
  //          旧: 各 LG# が「概要 段落 / タイミング 段落 / 記録項目 段落 / 呼出元 段落 / 実行層 段落」と
  //              段落の連続だったが、§6.1/§6.2 は「概要 段落 + 表形式」のため不揃いだった。
  //          新: 「概要 段落 + 詳細表（項目 / 内容 の 2 列）」に統一し、視覚的に他章と揃える。
  // 接続情報: 入力 = data.common_logging_processes / 出力 = h3 + h4 + 表
  const prefix = opts.prefix || '5.7';
  const blocks = [
    h3(`${prefix} 観測点ログ処理（共通）`),
    p('業務追跡ログの観測点を独立した共通処理として定義します。各業務処理（§5.1 フロント処理 / §5.2 バック処理）の steps から「[LG#] を呼ぶ」の1行で参照され、ここで処理本体（記録内容・タイミング・業務的役割）を確認できます。')
  ];
  // [LG1]-[LG8] 本体 / サブ [LG4-a]-[LG4-f] を h4 単位で並べる
  let idx = 0;
  for (const proc of (processes || [])) {
    idx++;
    const callId = proc.process_id || '';
    const name = proc.name || '';
    blocks.push(h4(`${prefix}.${idx} [${callId}] ${name}`));
    // 概要段落（§6.1/§6.2 と同じ書き方）
    // v51 §19 1 文 1 概念ルール: overview + overview_details[] を bullet 描画
    //  意味合い: 共通ログ処理概要が句点 2+ で「。」連結マルチステートメント状態だった旧式を解消
    //  接続情報: proc.overview / proc.overview_details / 16_docx出力ハンドオフ.md §19
    for (const para of renderDescriptionWithDetails(proc, 'overview', null, '概要: ')) {
      blocks.push(para);
    }
    // 詳細を 2 列表（項目 / 内容）に集約。タイミング・記録項目・呼出元・実行層を 1 表で一覧。
    // 意味合い: 段落連発だと視覚的に他章と揃わないため、表形式で「項目ラベル / 内容」を並べる。
    const detailRows = [];
    if (proc.triggered_when) detailRows.push(['タイミング', proc.triggered_when]);
    const recorded = proc.recorded_fields || [];
    if (recorded.length > 0) detailRows.push(['記録項目', recorded.join(' / ')]);
    if (proc.called_by) detailRows.push(['呼出元', proc.called_by]);
    if (proc.layer) {
      const layerLabel = proc.layer === 'frontend' ? 'フロント' : (proc.layer === 'backend' ? 'バックエンド' : proc.layer);
      detailRows.push(['実行層', layerLabel]);
    }
    if (detailRows.length > 0) {
      blocks.push(buildTable(['項目', '内容'], detailRows, [2, 11]));
    }
  }
  return blocks;
}

/**
 * H2 x N: 各イベント処理（event_processes をループ）
 * H3: Overview / H3: 機能説明
 */
function sectionEventProcesses(eventProcesses, events) {
  // 意味合い: 「べた書きで見にくい」フィードバックの根本対処として、全イベントを「1枚の表」に統合する。
  //           サマリと詳細の二段構成は冗長と判断し、概要フル + 処理詳細を同じ表に並べる。
  //           1行 = 1イベント。詳細未充実時は該当セルに「（充実化予定）」のグレー注記。
  // 接続情報: 入力 = event_processes[] + events[] / 出力 = blocks（H2 + p + 1枚の表）
  //           参照 = 16_docx出力ハンドオフ.md「イベント処理詳細（§7）の構造（現行確定）」
  // v50: event_code 廃止に伴い、events を no（EV1, EV2, ...）でマッピング。
  //      event_processes 側は event_ref で events[].no を参照する形式（migrate_v49_event_unify.py で変換済）。
  const eventMap = new Map();
  for (const e of (events || [])) {
    eventMap.set(e.no, e);
  }

  // 意味合い: §7 は (B) 索引表に縮約。業務担当者が「このイベントはどのAPI/SQLを呼ぶか」を辿るためだけに残す。
  //           overview と details は §5 と重複するため廃止。画面トリガーのみ対象（内部処理は対象外）。
  // 接続情報: eventMap → events[].source_kind / ep.db_operations_ref → db_operations[].id / 中間JSON db_operations から API endpoint 引く

  // db_operations の id → api_endpoint マップ
  const dbOpMap = new Map();
  const apiByDbOp = new Map();

  // 画面トリガーのイベントのみを対象
  // v50: event_processes[].event_code → event_processes[].event_ref に rename 済（events[].no を参照）
  const targetProcesses = (eventProcesses || []).filter(ep => {
    const ev = eventMap.get(ep.event_ref || ep.event_code) || {};  // event_code 後方互換（移行中）
    return (ev.source_kind || 'screen') === 'screen';
  });

  const rows = targetProcesses.map((ep, idx) => {
    const ev = eventMap.get(ep.event_ref || ep.event_code) || {};
    const loc = (ev.location || []).length > 0
      ? formatLocationHierarchy(ev.location)
      : (ev.trigger || '');
    const refsDb  = (ep.db_operations_ref || []).join(', ') || '—';
    const refsApi = (ep.api_refs || []).join(', ') || '—';
    return [
      String(idx + 1),
      loc,
      ep.pattern || '§9-G',
      refsApi,
      refsDb
    ];
  });

  return [
    h2('7. イベント処理索引'),
    p('§5.1 画面トリガーの各イベントが、どの処理パターンに該当し、どの API / DB操作を呼ぶかを索引で示します。「関連API」「関連DB操作」列の値は §6 / §12 への参照キーです。内部処理は対象外（§5.2 を参照）。'),
    // v48: §7 イベント処理索引 — 「No」は event カテゴリの header_self
    buildTable(
      [getIdHeaderLabel('event', 'self'), '発生', 'パターン', '関連API', '関連DB操作'],
      rows,
      [1, 6, 1, 4, 5]
    )
  ];
}

/**
 * バリデーション
 *
 * v17: §6 処理詳細 配下のサブセクションとして組み込めるよう、prefix / level を引数化。
 *      デフォルト（prefix='8', level='h2'）は旧バージョン互換、§6 配下版は (prefix='6.8', level='h3') で呼ぶ。
 */
function sectionValidations(validations, opts = {}) {
  // 目的: §6.3 / §6.8 でバリデーションルール一覧を 5 列表（No / 対象項目 / ルール / 適用条件 / メッセージID）で出力する。
  // 意味合い: 業務担当者が「どの画面項目に、どんなルールが、いつ適用され、どのメッセージで弾かれるか」を 1 行で読み取れる形にする。
  //           v50.5 で中間 JSON の実キー (field / rule / applied_when / message_id) と整合（旧: target_item / check_type / format / content）。
  // 接続情報: 入力 = data.validations / 出力 = h3 or h2 + 表
  //           中間 JSON 仕様 = 15_中間JSONスキーマ.md「validations[]」セクション
  const { prefix = '8', level = 'h2' } = opts;
  if (!validations || validations.length === 0) return [];
  // v50.5: 中間 JSON 実キーに整合。field=対象項目、rule=チェックルール（形式・必須・マスタ照合等の複合）、applied_when=適用タイミング・拒否時挙動。
  //         旧体系の check_type / format / content は中間 JSON に存在しなかったため全件空白で表示されていた（再発防止: A1 修正）
  // v51 §19 1 文 1 概念ルール: rule / applied_when に details 配列があれば bullet 描画
  //  意味合い: 旧式の「。」連結マルチステートメント記述を解消、業務担当者が条件を 1 行ずつ読める形にする
  //  接続情報: v.rule / v.rule_details / v.applied_when / v.applied_when_details / 16_docx出力ハンドオフ.md §19
  const colRatios = [1, 3, 5, 5, 2];
  const tableWidth = CONTENT_WIDTH - currentIndent;
  const _totV = colRatios.reduce((a, b) => a + b, 0);
  const colWidths = colRatios.map(r => Math.floor(tableWidth * r / _totV));
  colWidths[colWidths.length - 1] += (tableWidth - colWidths.reduce((a, b) => a + b, 0));
  const rows = validations.map(v => {
    const ruleCell = (Array.isArray(v.rule_details) && v.rule_details.length > 0)
      ? tdCellWithDetails(v, 'rule', colWidths[2])
      : (v.rule || '');
    const appliedCell = (Array.isArray(v.applied_when_details) && v.applied_when_details.length > 0)
      ? tdCellWithDetails(v, 'applied_when', colWidths[3])
      : (v.applied_when || '');
    return [
      String(v.no || ''),
      v.field || '',
      ruleCell,
      appliedCell,
      v.message_id || ''
    ];
  });
  const heading = level === 'h3' ? h3(`${prefix} バリデーション`) : h2(`${prefix}. バリデーション`);
  return [
    heading,
    // v48: バリデーション表 — 「No」は validation カテゴリの header_self
    buildTable(
      // v50.5 phase 2.5.1: 列名を業務担当者語彙の平易な日本語に変更
      // 旧「適用タイミング・拒否時挙動」→ 新「適用条件と違反時の挙動」（レビュー指摘）
      [getIdHeaderLabel('validation', 'self'), '対象項目', 'ルール', '適用条件と違反時の挙動', 'メッセージID'],
      rows,
      colRatios
    )
  ];
}

/**
 * メッセージ一覧
 *
 * v17: §6 統合後の章番号は §7。prefix / level を引数化。
 */
function sectionMessages(messages, opts = {}) {
  // 目的: §7 でシステムが発する全メッセージ一覧を「No / コード / 種別 / 本文 / 発行 API」の 5 列で出力。
  // 意味合い: 業務担当者が画面で見るメッセージの「正規ID（新体系 MS#）」「業務コード（MSI/MSE 系）」「重大度（info/error）」「文言」「どの API が発するか」を一望できる形。
  //           v50.5 で中間 JSON の実キー (message_id=新体系 MS#, code=旧システム由来コード, message=本文, severity=重大度, emitted_by_api=発行 API 配列) と整合。
  //           旧体系の m.id は存在せず、§7 が「ID 列空白 + 本文列空白」状態だった（再発防止: A5 修正）。
  // 接続情報: 入力 = data.messages / 出力 = h2 + 表
  //           中間 JSON 仕様 = 15_中間JSONスキーマ.md「messages[]」セクション
  //           validations[].message_id 等から参照される（参照側は v50.5 時点ではコード "MSG001" 形式の残存があり整合課題あり、Phase 2 で解消予定）
  const { prefix = '9', level = 'h2' } = opts;
  if (!messages || messages.length === 0) return [];
  // v50.5: 新体系 message_id (MS#) を主 ID として表示。code (MSI/MSE 由来) は業務追跡用に併記。
  const rows = messages.map(m => [
    String(m.message_id || ''),
    m.code || '',
    m.severity || '',
    m.message || '',
    (m.emitted_by_api || []).join('\n')
  ]);
  const heading = level === 'h3' ? h3(`${prefix} メッセージ一覧`) : h2(`${prefix}. メッセージ一覧`);
  return [
    heading,
    // B2（2026-07-19）: 主要一覧表への表番号キャプション試験導入（画面項目一覧・トリガー一覧と同方針）
    buildTableCaption(prefix, 'メッセージ一覧').paragraph,
    // v50.5: 5 列構成。新体系 message_id は message カテゴリの header_self、コード列は「コード」（旧システム由来のコード）
    // A4（2026-07-18）: 種別列（rows[i][2]）の値で行全体を淡色に。error=赤系/warning=黄系/info=白のまま。
    buildTable(
      [getIdHeaderLabel('message', 'self'), 'コード', '種別', '本文', '発行 API'],
      rows,
      [2, 3, 2, 7, 4],
      { rowFill: (row) => (row[2] === 'error' ? FILL_ERROR : row[2] === 'warning' ? FILL_WARNING : undefined) }
    )
  ];
}

/**
 * 目的: 巻末「付録A ID索引」用に、全処理系IDを種別ごとに集約する。
 * 意味合い: §5.1〜§5.7 個別処理の見出しは h4/h5（TOC範囲外）のため、目次からは辿れない。
 *           ID列は本文中の [FR14] 表記と同じくlinkifyIdRefs() で自動的にInternalHyperlink化される
 *           ため、この関数はテキスト（"[FR14]" 形式）を並べるだけでよい。
 * 接続情報: 呼出元 = sectionIdIndex
 */
function buildIdIndexRows(data) {
  const rows = [];
  const pushAll = (items, idField, nameField, categoryLabel) => {
    (items || []).forEach(item => {
      const id = item && item[idField];
      if (!id) return;
      const name = String((item[nameField] || '')).split('\n')[0].slice(0, 40);
      rows.push([`[${id}]`, categoryLabel, name]);
    });
  };
  // 注意（A2, 2026-07-18）: validations[] / messages[] は個別見出しを持たず表1本で完結するため、
  // collectAllProcessIds() と同じ理由で索引の対象外（章内の表がそのまま一覧になっている）。
  pushAll(data.front_processes, 'process_id', 'name', 'フロント処理');
  pushAll(data.backend_processes, 'process_id', 'name', 'バックエンド処理');
  pushAll(data.calculations, 'no', 'name', '計算式');
  pushAll(data.common_logic, 'no', 'name', '共通ビジネスロジック');
  pushAll(data.common_logging_processes, 'process_id', 'name', '観測点ログ処理');
  return rows;
}

/**
 * H2: 巻末 ID 索引（A2、2026-07-18 新設）
 * 意味合い: 「ID→章番号」の対応をユーザーが目で探す代わりに、ID をクリックすれば直接ジャンプできる
 *           逆引き表を用意する（16_docx出力ハンドオフ.md §12「章番号と ID 順は一致しないことがある」問題への補助）。
 */
function sectionIdIndex(data) {
  const rows = buildIdIndexRows(data);
  if (rows.length === 0) return [];
  return [
    h2('付録A. ID索引'),
    p('本書で使用する全 ID の一覧です。ID 列はクリックすると該当箇所へジャンプします。'),
    buildTable(['ID', '種別', '業務名称'], rows, [2, 3, 10])
  ];
}

/**
 * 計算式（v17: §6 配下のサブセクション化に対応、prefix/level 引数化）
 *
 * 意味合い: 計算式は「処理（合計=単価×数量 等）」なので §6 配下が論理的に妥当（v17 改修方針）。
 *           level='h3' で呼ぶと §6.X 計算式 / §6.X.N 結果名 として展開される。
 */
function sectionCalculations(calculations, opts = {}) {
  // 目的: §6.4 / §6.9 で計算式一覧を「結果名 + 基本式 + 入力（名前・由来）+ 出力（名前・出力先）+ 備考」の構造で出力する。
  // 意味合い: 業務担当者が「この計算で何が必要（入力）」「結果は何（出力）」「どう計算する（式）」「業務上の注意点（備考）」を一目で把握できる形。
  //           v50.5 で中間 JSON の実キー (name / formula / inputs[]/outputs[] / remarks) と整合。
  //           旧体系の result_name / expression / conditions[] / applicable_patterns は中間 JSON に存在せず、全 19 件が空表示になっていた（再発防止: A2 修正）。
  // 接続情報: 入力 = data.calculations / 出力 = h2 or h3 + 各エントリの小節
  //           中間 JSON 仕様 = 15_中間JSONスキーマ.md「calculations[]」セクション
  //           inputs[].source / outputs[].destination は業務担当者語彙、画面項目・マスタ列名等を業務的に書く
  // B1（2026-07-19）: opts.diagrams / chapterNo は derivation_chain 図埋め込み用。
  //   接続情報: opts.diagrams = data.diagrams（呼出元 sectionProcesses が丸ごと転送）
  //             chapterNo は図キャプション「図{章番号}-{連番}」の章番号。prefix（例: '5.4'）の
  //             先頭セグメントを使い、§5配下のサブ章（5.4/5.5）で連番を共有する（16_docx出力ハンドオフ.md §23 準拠）。
  const { prefix = '10', level = 'h2', diagrams = [] } = opts;
  const chapterNo = String(prefix).split('.')[0];
  if (!calculations || calculations.length === 0) return [];
  const topHeading = level === 'h3' ? h3(`${prefix} 計算式`) : h2(`${prefix}. 計算式`);
  // A2（2026-07-18）: 見出しに [CA#] ID を含めていなかったバグを修正（verify_docx_layout.py が
  // 「anchor='CA1' に対応する Bookmark が存在しない」で検出。FR/BE/CL は既に ID 込みだった）。
  const subHeadingFn = level === 'h3'
    ? (i, name, id) => h4(`${prefix}.${i} [${id}] ${name}`)
    : (i, name, id) => h3(`${prefix}.${i} [${id}] ${name}`);
  // v50.5 phase 2.5.1: 章先頭に「位置付け」段落を追加。
  // 意味合い: レビュー指摘「§6.4 計算式の参照元不明」への対応。§6.1/§6.2 で参照される計算式カタログである旨を明示。
  const blocks = [
    topHeading,
    p('§5.1 フロント処理 / §5.2 バック処理 の各処理仕様表内で「合計金額を計算」「税額を計算」等として参照される、業務計算式の個別仕様カタログです。各計算式の【入力】【出力】【備考】に「業務的にどの場面・どの処理で使われるか」を記述しています。'),
    // A2（2026-07-18）: h3/h4 は目次(TOC)範囲外のため、カテゴリ内の全計算式を俯瞰する一覧表を追加。
    ...buildProcessIndexTable(calculations, 'no', 'name', 'formula')
  ];
  let idx = 0;
  for (const calc of calculations) {
    idx++;
    // v50.5: 結果名 (name) と基本式 (formula) を表示。
    blocks.push(subHeadingFn(idx, calc.name || '', calc.no || ''));
    blocks.push(p(`基本式: ${calc.formula || ''}`));

    // v50.5: 入力（inputs[]）一覧。各入力に「業務名」と「データ由来」が紐付く。
    //         意味合い: 計算に何が必要かを業務担当者が即座に把握できるようにする。
    if (calc.inputs && calc.inputs.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【入力】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 80, after: 40 }
      }));
      const inputRows = calc.inputs.map(i => [i.name || '', i.source || '']);
      blocks.push(buildTable(['入力名', '由来'], inputRows, [4, 8]));
    }

    // v50.5: 出力（outputs[]）一覧。各出力に「業務名」と「出力先（画面項目・DB列・帳票列等）」が紐付く。
    //         意味合い: 計算結果が業務的にどこへ反映されるかを明示。
    if (calc.outputs && calc.outputs.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【出力】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 80, after: 40 }
      }));
      const outputRows = calc.outputs.map(o => [o.name || '', o.destination || '']);
      blocks.push(buildTable(['出力名', '出力先'], outputRows, [4, 8]));
    }

    // v50.5: 備考（remarks）に「条件分岐」「丸めモード」「適用パターン」「業務的注意」等が記述される。
    //         意味合い: 旧 conditions[]/applicable_patterns 列は v50 までに remarks へ統合済。
    // v51 §19 1 文 1 概念ルール: remarks + remarks_details[] を bullet 描画
    //  意味合い: 備考が句点 2+ で「。」連結マルチステートメント状態だった旧式を解消
    //  接続情報: calc.remarks / calc.remarks_details / 16_docx出力ハンドオフ.md §19
    if (calc.remarks) {
      for (const para of renderDescriptionWithDetails(calc, 'remarks', null, '備考: ')) {
        blocks.push(para);
      }
    }

    // B1（2026-07-19）: derivation_chain 図（導出チェーン長3以上または分岐ありの計算式のみ対象、
    // 17_構成図生成.md 生成条件）を該当計算式の直後に埋め込む。図が未生成/埋め込み失敗の場合は
    // buildDerivationChainEmbed が空配列を返すため、上記【入力】【出力】【備考】の既存テキスト表記
    // のみで完結する（フォールバック必須要件）。
    const chainDiagram = findDerivationChainDiagram(diagrams, calc.no, calc.name);
    if (chainDiagram) {
      blocks.push(...buildDerivationChainEmbed(chainDiagram, chapterNo));
    }
  }
  return blocks;
}

/**
 * H2: API仕様（エンドポイントごとにH3）
 *
 * 意味合い: §11 API仕様 の各エントリに、業務担当者が「このAPIの裏で何が起こり、レスポンスが画面のどこに出るか」を辿るための情報を集約する。
 *
 * v15 で追加: 「実行するDB操作」（§12 への参照リンク）
 * v16 で追加: 「操作するキャッシュ」（排他ロック等のキャッシュ層操作）／「レスポンスの画面表示先」（response_to_screen_mapping）
 *
 * 接続情報: api.related_db_operations / api.related_cache_operations / api.response_to_screen_mapping を表示
 *           仕様根拠: 15_中間JSONスキーマ.md「api_spec」セクション
 */
function sectionApiSpec(apiSpec, opts = {}) {
  // v17: §6 統合に対応するため prefix/level を引数化
  const { prefix = '11', level = 'h2' } = opts;
  if (!apiSpec || apiSpec.length === 0) return [];
  const topHeading = level === 'h3' ? h3(`${prefix} API仕様`) : h2(`${prefix}. API仕様`);
  const subHeadingFn = level === 'h3'
    ? (i, title) => h4(`${prefix}.${i} ${title}`)
    : (i, title) => h3(`${prefix}.${i} ${title}`);
  const blocks = [topHeading];
  let idx = 0;
  for (const api of apiSpec) {
    idx++;
    const title = `${api.method} ${api.path} [${api.action}]`;
    blocks.push(subHeadingFn(idx, title));
    blocks.push(p(`説明: ${api.description || ''}`));
    blocks.push(p(`認証: ${api.auth || ''}`));
    // v15: 実行するDB操作（§12 への参照）
    const dbRefs = (api.related_db_operations || []).filter(Boolean);
    if (dbRefs.length > 0) {
      blocks.push(p(`実行するDB操作: ${dbRefs.join(' → ')}（§12 参照）`));
    } else {
      blocks.push(p('実行するDB操作: なし'));
    }

    // v16: 操作するキャッシュ（排他ロック等）
    const cacheOps = (api.related_cache_operations || []).filter(Boolean);
    if (cacheOps.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【操作するキャッシュ】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 80, after: 40 }
      }));
      const cacheRows = cacheOps.map(c => [
        c.operation || '',
        c.key_pattern || '',
        c.ttl_sec !== null && c.ttl_sec !== undefined ? `${c.ttl_sec}秒` : '—',
        c.purpose || ''
      ]);
      blocks.push(buildTable(
        ['操作', 'キーパターン', 'TTL', '目的'],
        cacheRows,
        [3, 4, 2, 6]
      ));
    }

    if (api.request_params && api.request_params.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【リクエストパラメータ】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 80, after: 40 }
      }));
      const rows = api.request_params.map(r => [
        r.name || '', r.type || '', r.required ? '○' : '—',
        r.location || '', r.validation || '', r.description || ''
      ]);
      blocks.push(buildTable(
        ['名称', '型', '必須', '場所', '検証', '説明'],
        rows,
        [3, 2, 1, 1, 4, 4]
      ));
    }

    if (api.response_items && api.response_items.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: `【レスポンス】 ${api.response_pattern || ''}`, font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 80, after: 40 }
      }));
      const rows = api.response_items.map(r => [
        String(r.no || ''), r.key || '', r.display_name || '',
        r.data_type || '', r.expression || ''
      ]);
      // v48: API仕様 response_items の「No」は api カテゴリの header_self
      blocks.push(buildTable(
        [getIdHeaderLabel('api', 'self'), 'キー', '表示名', '型', '式・補足'],
        rows,
        [1, 5, 3, 1, 6]
      ));
    }

    // v16: レスポンスの画面表示先（response_to_screen_mapping）
    // 業務担当者が「APIレスポンスの値が画面のどこに出るか」を辿れるようにする
    const screenMapping = (api.response_to_screen_mapping || []).filter(Boolean);
    if (screenMapping.length > 0) {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【レスポンスの画面表示先】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 80, after: 40 }
      }));
      // v37: 「画面#」を「エリア#」に統一（opts.areas 経由）
      const _apiAreas = opts.areas || [];
      const mapRows = screenMapping.map(m => [
        m.api_field_path || '',
        m.screen_no ? formatAreaNo(m.screen_no, _apiAreas) : '—',
        m.screen_item_name || '—',
        m.transform || ''
      ]);
      // v48: API レスポンスの画面表示先 — 「エリア#」は area カテゴリの header_ref
      blocks.push(buildTable(
        ['APIフィールド', getIdHeaderLabel('area', 'ref'), '画面項目', '変換・補足'],
        mapRows,
        [6, 2, 4, 6]
      ));
    }
  }
  return blocks;
}

/**
 * DB操作（v17: §6 配下対応で prefix/level 引数化）
 *
 * 接続情報: maps = buildLogicalNameMaps(data) の戻り値。「論理名[物理名]」形式統一に使用
 *           data.events を参照して「呼出元イベント」no を逆引き表示（v15、§7廃止に伴い）
 * 過去経緯: v14 以前は物理名/論理名混在で読みにくい → v15「論理名[物理名]」統一
 *          v15: §7 廃止 → §12 で「このSQLは何のイベントで実行されるか」完結把握、呼出元イベント no 表示
 *          v17: §6 配下に統合（処理の一種だから）。prefix='6.11'、level='h3' で呼ぶ
 */
/**
 * sectionDbOperations: §5.6 DB 操作セクション (v51 新体系対応)
 *
 * 目的: 中間 JSON db_operations[] (v51 §8 構造化表で書き戻し済) を読み、
 *       業務担当者が SQL 内容を業務語彙で読める H4 個別小節として出力する。
 * 意味合い: v51 憲法 §3 共通 7 項目 + §5 DB 出力表 + §8 SQL 構造化表 + §9 揺れ防止辞書 の機械強制点。
 *           旧体系 (op.joins / op.where / op.dataset / op.insert_values / op.update_values / op.tables / op.sql_outline)
 *           は v51 移行で完全廃止。中間 JSON は新フィールド名 (from_tables / where_conditions /
 *           select_items / insert_columns / update_set / group_by / having / order_by) で書き戻し済のため、
 *           旧フィールドへのフォールバックは不要 (フォールバックを残すと「動くが業務担当者には空白」を再発させる)。
 * 接続情報: 呼出元 = sectionProcessDetails / 中間 JSON .db_operations[] /
 *           プロジェクト側の DB スキーマ資料（参考） /
 *           16_docx出力ハンドオフ.md §3 共通 7 項目 / §5 DB 出力表 / §8 SQL 構造化表 / §9 揺れ防止辞書 / §13 件数表記禁止 / §14 廃止用語
 */
/**
 * sectionDbOperations (v51 §5 §8 §9 + DB タイトル業務語彙化対応版)
 *
 * 目的: 中間 JSON db_operations[] を業務担当者が読める業務語彙で出力する。
 *   (1) 章タイトル: business_title を主軸とし、(operation on 主テーブル) を副題で併記
 *   (2) SELECT 項目表: aggregate_function (実引数表記) + business_expression (業務的意味) を併記
 *   (3) 出力表: output_role を「役割」列で多様化、business_purpose を「用途」列で業務語彙化
 * 意味合い: v51 §5 §8 §9 §15 の機械強制点 + レビュー指摘 3 点 (タイトル / SUBSTR 抽象 / 出力欄固定)
 *   解消。「SELECT on テーブル名」「SUBSTR(...)」「結果セット列として返却」固定の SQL DSL 風表現
 *   を排除し、業務担当者が読める表現に置換する。
 * 接続情報:
 *   呼出元 = main() (generate_docx.js)
 *   入力 = 中間 JSON .db_operations[] (target/intermediate/{機能名}.json)
 *   仕様 = 16_docx出力ハンドオフ.md §5 §8 §9 §15 (DB 章タイトル規約 / DB 出力表 5 列 / 揺れ防止辞書)
 *   共通の規則の物理名禁止 + コメント規約 (目的 / 意味合い / 接続情報の 3 点必須)
 *
 * @param {Array<Object>} dbOps - 中間 JSON db_operations[]
 * @param {Object} maps - 論理名マップ (現状未使用、将来拡張用)
 * @param {Object} data - 中間 JSON 全体（B4改修・2026-07-19で data.diagrams[] 参照に使用開始。id==='data_flow' の
 *   図を §5.6 冒頭に埋め込む用途）
 * @param {Object} opts - { prefix, level } 章番号と見出しレベル
 * @returns {Array<Paragraph|Table>} docx ブロック配列
 */
function sectionDbOperations(dbOps, maps, data, opts = {}) {
  const { prefix = '5.6', level = 'h2' } = opts;
  // B4-fix（2026-07-19、総括レビューで発見）: dbOps が空でも diagrams[].id==='data_flow'
  // が存在する画面（top-level db_operations[] を持たない画面）では図だけ出力する必要がある。
  // 早期 return を dataFlowDiagram 判定より後に移し、data_flow 単独でもセクション自体は生成されるようにする。
  const dataFlowDiagram = ((data && data.diagrams) || []).find(d => d && d.id === 'data_flow');
  const hasDataFlow = !!(dataFlowDiagram && dataFlowDiagram.png_path && fs.existsSync(dataFlowDiagram.png_path));
  // 目的: db_operations が有っても、この章の概要部が読む項目（概要 / 業務タイトル / 動作場所 / 呼出元 / 操作種別）を
  //       全件が1つも持たないときは、個別 DB# の展開を行わない（図も無ければ見出しごと章を省く）。
  // 意味合い: これらの項目を持たない形の db_operations をそのまま展開すると、全 DB# が「（未指定）」「（該当なし）」
  //           だけの空の骨組みになり、読み手に中身の無い章を見せてしまう。1件でも持てば従来どおり全件を展開する。
  //           db_operations が空・未定義の場合も偽になる（従来の早期 return と同じ扱い）。
  //           data_flow 図だけを出す既存の動作（上の B4-fix）は変えない。
  // 接続情報: 入力 = op.summary / op.business_title / op.runtime_location / op.used_by_api / op.operation
  //           （下の DB# ループの「(0) H4 見出し + 概要表」が読む項目と同じ）/ 利用先 = 直下の早期 return と DB# ループ
  const hasDbContent = Array.isArray(dbOps) && dbOps.some(op =>
    ['summary', 'business_title', 'runtime_location', 'used_by_api', 'operation']
      .some(k => (Array.isArray(op[k]) ? op[k].length > 0 : !!op[k])));
  if (!hasDbContent && !hasDataFlow) return [];
  // B4（2026-07-19）: 図キャプション「図{章番号}-{連番}」の章番号。prefix（例: '5.6'）の先頭セグメントを
  // 使う設計は sectionCalculations/sectionCommonLogic の chapterNo 算出と同一（buildFigureCaption 呼出元）。
  const chapterNo = String(prefix).split('.')[0];

  // §5.6 トップ見出し: level='h2' なら "5.6. DB操作"、'h3' なら "5.6 DB操作"
  // 意味合い: 章番号体系は v51 §1 で §5.6 が DB 操作カテゴリと固定。呼出元から prefix='5.6' で受ける
  const topHeading = level === 'h3' ? h3(`${prefix} DB操作`) : h2(`${prefix}. DB操作`);
  // 個別 DB# は H4 で展開 (§5.6.1 / §5.6.2 / ...)
  const subHeadingFn = (i, title) => h4(`${prefix}.${i} ${title}`);

  const blocks = [topHeading];
  // §5.6 セクション直下の導入文 (業務担当者向け、SQL を業務語彙で読む方針を明示)
  blocks.push(p('バック処理から発行される SQL を、業務担当者が読める語彙で構造化して定義します。各 DB# は「概要表 → 入力 → 処理 (SQL 構造化表) → 出力 → エラー」の順で展開されます。'));

  // §5.6 冒頭データフロー図（data_flow）PNG 埋め込み
  // B4（2026-07-19）: 17_構成図生成.md「対象となる図」カタログの data_flow 行に対応。
  // 意味合い: screen_layout + db_operations + api_spec から生成される、画面・API・DBテーブル間の
  //           データフロー俯瞰図。diagrams[] にエントリがある画面のみ生成対象（参照テーブル数が少ない
  //           画面には diagrams[] にエントリが存在しない想定、17_構成図生成.md
  //           生成条件）。既存4図種と同一の「PNG存在確認→埋め込み→buildFigureCaption→
  //           FIGURE_READING_NOTES」パターンを踏襲し、DB#ループ開始前（個別 DB# 一覧より先）に置く
  //           ことで、業務担当者が個々の DB# を読む前にテーブル間の全体像を把握できるようにする。
  //           diagrams[] に該当エントリが無い、または PNG が存在しない場合は何も出力しない
  //           （derivation_chain の失敗時空配列パターンと同一の Silent-fallback ではない設計）。
  // 接続情報: 入力 = data.diagrams[].id === 'data_flow' / 出力 = 画像 + 図キャプション + 読み方定型文
  // (dataFlowDiagram/hasDataFlow は関数冒頭で計算済み、早期return判定と共用)
  if (hasDataFlow) {
    try {
      const imgData = fs.readFileSync(dataFlowDiagram.png_path);
      const displayWidth = (dataFlowDiagram.embed_size && dataFlowDiagram.embed_size.width_px) || 640;
      const { width: w, height: h } = calcAspectFit(dataFlowDiagram.png_path, displayWidth);
      const dataFlowCaptionTitle = dataFlowDiagram.caption || dataFlowDiagram.title || 'データフロー図';
      const fig = buildFigureCaption(chapterNo, dataFlowCaptionTitle);
      const dataFlowReadingNote = dataFlowDiagram.reading_note || FIGURE_READING_NOTES.data_flow;
      blocks.push(p(`画面・API・DBテーブル間のデータの流れを${fig.label}に示します。`));
      blocks.push(new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new ImageRun({
          type: 'png', data: imgData, transformation: { width: w, height: h },
          altText: {
            title: dataFlowCaptionTitle,
            description: `${(data && data.meta && data.meta.feature_name) || '対象画面'}画面のデータフロー（画面・API・DBテーブル間）`,
            name: dataFlowDiagram.id
          }
        })]
      }));
      blocks.push(fig.paragraph);
      if (dataFlowReadingNote) {
        blocks.push(p(dataFlowReadingNote));
      }
    } catch (e) {
      // 装置: try 内で blocks.push を行っていないため、途中失敗時も見出しの無いこのセクションに
      // 半端な内容が残らない。既存4図種の catch ブロックと同じ「テキスト表記のみで継続」方針。
      console.warn(`[warn] データフロー図PNG埋め込み失敗 (${dataFlowDiagram.png_path}): ${e.message}`);
    }
  }

  let idx = 0;
  // hasDbContent が偽（全件が概要部の項目を持たない）で図だけを出す場合は、個別 DB# を展開しない（関数冒頭の判定と共用）
  for (const op of (hasDbContent ? dbOps : [])) {
    idx++;
    // ========================================================================
    // (0) H4 見出し + 概要表 (共通 7 項目の前半: 概要 / 動作場所 / 呼出元 / 操作種別)
    // ========================================================================
    // 目的: 業務担当者が「この DB 操作は何のため/どこで動く/誰が呼ぶ/SELECT か INSERT か」を冒頭 4 行で把握できる
    // 意味合い: v51 §3 共通 7 項目 (前半 4 項目) + §3 カテゴリ固有 (DB は操作種別) を必須出力
    //           + v51 §5 補遺 DB 章タイトル規約: 「[DB#] <business_title> (operation on 主テーブル)」形式
    //           SQL DSL 風 (旧「SELECT on テーブル名」) を業務語彙化
    // 接続情報: op.business_title / op.summary / op.runtime_location / op.used_by_api / op.operation /
    //           op.from_tables[0].table (主テーブル)
    const opType = op.operation || '';
    const dbId = op.id || op.op_id || '';
    // テーブル名は from_tables[0] (FROM 句のメインテーブル) を業務語彙で取得
    const mainTable = (op.from_tables && op.from_tables.length > 0 && op.from_tables[0].table) || '';
    // 業務タイトル (主軸) + 副題 (operation on 主テーブル)
    // 意味合い: v51 補遺 - business_title を主軸とし、SQL DSL 風を排除
    //           business_title が「(調整要)」または欠落の場合は副題のみで従来形式に近づける
    const bizTitle = op.business_title || '';
    let titleStr;
    if (bizTitle && bizTitle !== '(調整要)') {
      // 業務タイトル主軸 + 副題で SQL 操作を併記 (例: 「[DB1] 明細初期取得 (SELECT on 数量データ)」)
      const subtitle = mainTable ? ` (${opType} on ${mainTable})` : (opType ? ` (${opType})` : '');
      titleStr = `[${dbId}] ${bizTitle}${subtitle}`;
    } else {
      // フォールバック: business_title 欠落時は従来形式を維持
      const titleSuffix = mainTable ? ` ${opType} on ${mainTable}` : (opType ? ` ${opType}` : '');
      titleStr = `[${dbId}]${titleSuffix}`;
    }
    blocks.push(subHeadingFn(idx, titleStr));

    // v51 §19 1 文 1 概念ルール: summary + summary_details[] を bullet 描画
    //  意味合い: DB 操作概要が句点 2+ で「。」連結マルチステートメント状態だった旧式を解消
    //  接続情報: op.summary / op.summary_details / 16_docx出力ハンドオフ.md §19
    for (const para of renderDescriptionWithDetails(op, 'summary', null, '概要: ')) {
      blocks.push(para);
    }

    // 概要表: 動作場所 / 呼出元 / 操作種別の 3 項目 (概要は上の p で表現済)
    const overviewRows = [
      ['動作場所', op.runtime_location || '（未指定）'],
      ['呼出元', (op.used_by_api && op.used_by_api.length > 0) ? op.used_by_api.join(', ') : '（該当なし）'],
      ['操作種別', opType || '（未指定）']
    ];
    blocks.push(buildTable(['項目', '内容'], overviewRows, [2, 10]));

    // ========================================================================
    // (1) 入力ブロック: SQL パラメータ表 (No / 種別 / 項目・名称 / 型 / 用途)
    // ========================================================================
    // 目的: バック処理から DB へ渡される値 (リクエスト由来) を一覧化
    // 意味合い: v51 §4 共通列 + §9 揺れ防止辞書「入力種別: プレースホルダ」固定
    //           where_conditions / insert_columns / update_set の value_expr に
    //           "リクエストの XXX" / "プレースホルダ XXX" が含まれる行を抽出してパラメータ表として再構成する
    //           値式そのものは §8 SQL 構造化表側で全文表示されるので、こちらは入力一覧の視点を提供
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '(1) 入力', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 120, after: 40 }
    }));
    // 入力パラメータ抽出: value_expr に "リクエスト" / "プレースホルダ" を含む where/insert/update を拾う
    // 意味合い: 純粋な定数 ('0' / 現在日付 等) は入力ではないので除外、業務担当者の「画面から渡した値」視点で列挙
    const inputRows = [];
    let inputNo = 0;
    const collectInputs = (items, sourceLabel) => {
      for (const it of (items || [])) {
        const valExpr = it.value_expr || '';
        // リクエスト由来 (プレースホルダ) を入力として扱う。"現在日付" / "EXCLUDED.xxx" / 純定数は入力対象外
        if (/リクエスト|プレースホルダ|引数/.test(valExpr)) {
          inputNo++;
          inputRows.push([
            String(inputNo),
            'プレースホルダ',
            `${sourceLabel}: ${it.column || ''}`,
            '—',
            valExpr
          ]);
        }
      }
    };
    collectInputs(op.where_conditions, 'WHERE 条件');
    collectInputs(op.insert_columns, 'INSERT 値');
    collectInputs(op.update_set, 'UPDATE 値');

    if (inputRows.length > 0) {
      blocks.push(buildTable(
        ['No', '種別', '項目・名称', '型', '用途'],
        inputRows,
        [1, 2, 5, 1, 5]
      ));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // ========================================================================
    // (2) 処理ブロック: SQL 構造化表 (v51 §8)
    // ========================================================================
    // 目的: SQL を 6 種類の構造化表 (FROM/JOIN / WHERE / GROUP BY / HAVING / SELECT / INSERT or UPDATE / ORDER BY) に分解
    // 意味合い: v51 §8 生 SQL 廃止規範の機械強制点。各表は §9 揺れ防止辞書の語彙のみ許容
    //           operation により表示する表を選別 (SELECT: SELECT/ORDER BY 必須、INSERT: INSERT 必須、UPDATE: UPDATE 必須)
    // 接続情報: op.from_tables / op.where_conditions / op.group_by / op.having /
    //           op.select_items / op.insert_columns / op.update_set / op.order_by
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '(2) 処理', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 120, after: 40 }
    }));

    // ----- 取得テーブルと結合 (FROM/JOIN) -----
    // 目的: SELECT/INSERT/UPDATE/DELETE 全てに必要な「対象テーブル」と結合関係を業務語彙で定義
    // 意味合い: v51 §8 取得テーブルと結合表 + §9 結合種別辞書 (INNER/LEFT/RIGHT/FULL OUTER JOIN)
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '【取得テーブルと結合】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 60, after: 40 }
    }));
    if (op.from_tables && op.from_tables.length > 0) {
      const ftRows = op.from_tables.map(ft => [
        String(ft.no || ''),
        ft.table || '',
        ft.alias || '',
        ft.join_kind || '（FROM 句メイン）',  // no=1 で join_kind null は FROM 句の起点テーブル
        ft.join_condition || '（該当なし）'
      ]);
      blocks.push(buildTable(
        ['No', 'テーブル', 'エイリアス', '結合種別', '結合条件'],
        ftRows,
        [1, 3, 2, 2, 6]
      ));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // ----- 絞込条件 (WHERE) -----
    // 目的: 行を絞り込む条件式を業務語彙で定義
    // 意味合い: v51 §8 絞込条件表 + §9 演算子辞書 (= / <> / < / <= / > / >= / IN / NOT IN / LIKE / IS NULL / IS NOT NULL)
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '【絞込条件 (WHERE)】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 60, after: 40 }
    }));
    if (op.where_conditions && op.where_conditions.length > 0) {
      const wcRows = op.where_conditions.map(wc => [
        String(wc.no || ''),
        wc.column || '',
        wc.operator || '',
        wc.value_expr || ''
      ]);
      blocks.push(buildTable(
        ['No', '列', '演算子', '値式'],
        wcRows,
        [1, 4, 2, 7]
      ));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // ----- グルーピング (GROUP BY) -----
    // 目的: 集約のグルーピング列を業務語彙で定義
    // 意味合い: v51 §8 グルーピング表。空配列の場合は「（該当なし）」と明記 (§3 空白禁止)
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '【グルーピング (GROUP BY)】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 60, after: 40 }
    }));
    if (op.group_by && op.group_by.length > 0) {
      const gbRows = op.group_by.map(gb => [
        String(gb.no || ''),
        gb.column || ''
      ]);
      blocks.push(buildTable(['No', '列'], gbRows, [1, 11]));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // ----- グルーピング後絞込 (HAVING) -----
    // 目的: GROUP BY の結果に対する絞込条件を業務語彙で定義
    // 意味合い: v51 §8 グルーピング後絞込表。空配列の場合は「（該当なし）」と明記
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '【グルーピング後絞込 (HAVING)】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 60, after: 40 }
    }));
    if (op.having && op.having.length > 0) {
      const hvRows = op.having.map(hv => [
        String(hv.no || ''),
        hv.column || '',
        hv.operator || '',
        hv.value_expr || ''
      ]);
      blocks.push(buildTable(['No', '列', '演算子', '値式'], hvRows, [1, 4, 2, 7]));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // ----- SELECT 項目 (operation='SELECT' のみ意味あり、INSERT/UPDATE/DELETE は表自体を出さない) -----
    // 目的: 結果セット列の構成を業務語彙 + 別名 + 集約・関数・式 (実引数) + 業務的意味 で定義
    // 意味合い: v51 §8 SELECT 項目表 + §9 集約・関数・式辞書 (実引数表記必須)
    //           v51 補遺で「集約・関数・式」列を拡張し「業務的意味 (business_expression)」を併記
    //           抽象「(...)」表記は中間 JSON 側で実引数化済 (例: SUBSTR(商品マスタ.商品コード, 1, 2))
    // 接続情報: op.select_items[].aggregate_function (実引数) / .business_expression (業務語彙説明)
    if (opType === 'SELECT') {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【SELECT 項目】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 60, after: 40 }
      }));
      if (op.select_items && op.select_items.length > 0) {
        const siRows = op.select_items.map(si => {
          // 集約・関数・式 + 業務的意味: 実引数表記と業務語彙説明を 1 セルに併記
          // 意味合い: 意図 (2) - 「商品コード / 商品グループコード / SUBSTR(...)」では何もわからない
          //           問題への対処。aggregate_function (実引数) と business_expression (業務的意味) を 2 行で表示
          let aggCell = si.aggregate_function || '';
          if (si.business_expression && si.business_expression !== '(調整要)') {
            // 業務的意味がある場合は改行して併記 (集約関数なしでも business_expression のみ表示する場合あり)
            aggCell = aggCell ? `${aggCell}\n（${si.business_expression}）` : `（${si.business_expression}）`;
          }
          return [
            String(si.no || ''),
            si.column || '',
            si.alias || '',
            aggCell
          ];
        });
        blocks.push(buildTable(
          ['No', '列', '別名', '集約・関数・式（業務的意味）'],
          siRows,
          [1, 4, 2, 7]
        ));
      } else {
        blocks.push(p('（該当なし）'));
      }
    }

    // ----- INSERT 項目 (operation='INSERT' のみ) -----
    // 目的: INSERT する列と値式を業務語彙で定義
    // 意味合い: v51 §8 INSERT 項目表 (生 SQL 廃止、列名 + 値式の 3 列構造)
    if (opType === 'INSERT') {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【INSERT 項目】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 60, after: 40 }
      }));
      if (op.insert_columns && op.insert_columns.length > 0) {
        const icRows = op.insert_columns.map(ic => [
          String(ic.no || ''),
          ic.column || '',
          ic.value_expr || ''
        ]);
        blocks.push(buildTable(['No', '列', '値式'], icRows, [1, 4, 7]));
      } else {
        blocks.push(p('（該当なし）'));
      }
      // INSERT の場合 ON CONFLICT 用に update_set が併設されることがある (UPSERT パターン)
      // 意味合い: PostgreSQL UPSERT (INSERT ... ON CONFLICT DO UPDATE) は INSERT 操作だが UPDATE 部分も持つ
      //           DB4 が該当 (データ UPSERT)
      if (op.update_set && op.update_set.length > 0) {
        blocks.push(new Paragraph({
          children: [new TextRun({ text: '【ON CONFLICT 時の更新項目】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
          spacing: { before: 60, after: 40 }
        }));
        const usRows = op.update_set.map(us => [
          String(us.no || ''),
          us.column || '',
          us.value_expr || ''
        ]);
        blocks.push(buildTable(['No', '列', '新値式'], usRows, [1, 4, 7]));
      }
    }

    // ----- UPDATE 項目 (operation='UPDATE' のみ) -----
    // 目的: UPDATE で書き換える列と新値式を業務語彙で定義
    // 意味合い: v51 §8 UPDATE 項目表 (生 SQL 廃止、列名 + 新値式の 3 列構造)
    if (opType === 'UPDATE') {
      blocks.push(new Paragraph({
        children: [new TextRun({ text: '【UPDATE 項目】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
        spacing: { before: 60, after: 40 }
      }));
      if (op.update_set && op.update_set.length > 0) {
        const usRows = op.update_set.map(us => [
          String(us.no || ''),
          us.column || '',
          us.value_expr || ''
        ]);
        blocks.push(buildTable(['No', '列', '新値式'], usRows, [1, 4, 7]));
      } else {
        blocks.push(p('（該当なし）'));
      }
    }

    // ----- 並び順 (ORDER BY) -----
    // 目的: 結果セットの並び順を業務語彙で定義
    // 意味合い: v51 §8 並び順表 + §9 並び順方向辞書 (昇順 / 降順)
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '【並び順 (ORDER BY)】', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 60, after: 40 }
    }));
    if (op.order_by && op.order_by.length > 0) {
      const obRows = op.order_by.map(ob => [
        String(ob.no || ''),
        ob.column || '',
        ob.direction || ''
      ]);
      blocks.push(buildTable(['No', '列', '方向'], obRows, [1, 8, 3]));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // ========================================================================
    // (3) 出力ブロック: 結果セット列 (v51 §5 DB 出力表 5 列構造) - operation='SELECT' のみ
    // ========================================================================
    // 目的: SELECT の結果セット列を「No / 役割 / 項目・名称 / 型 / 用途」で定義
    // 意味合い: v51 §5 補遺 DB 出力表 5 列構造 (旧「種別=結果セット列」「用途=結果セット列として返却」固定を廃止)
    //           - 役割列: §9 結果列の業務役割辞書 8 分類 (表示用/業務識別子/マスタ参照キー/計算入力/集計値/判定フラグ/期間値/業務分類)
    //           - 用途列: business_purpose (業務語彙の 1 文説明)
    //           レビュー指摘 (3) - 種別/用途固定文言の解消
    // 接続情報: op.select_items[].output_role / .business_purpose (中間 JSON で業務語彙化済)
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '(3) 出力', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 120, after: 40 }
    }));
    if (opType === 'SELECT') {
      if (op.select_items && op.select_items.length > 0) {
        const outRows = op.select_items.map(si => {
          // 項目・名称: alias があれば「列名 (別名: alias)」、なければ「列名」
          // 意味合い: 業務担当者は alias 経由で結果を受け取ることが多いので alias を明示
          const itemName = si.alias ? `${si.column || ''} (別名: ${si.alias})` : (si.column || '');
          // 役割列: output_role (8 分類)。欠落時は「(調整要)」明示で固定文言を避ける
          // 意味合い: v51 §5 補遺 + §9 結果列の業務役割辞書
          const role = si.output_role || '(調整要)';
          // 用途列: business_purpose (業務語彙の 1 文)。欠落時は「(調整要)」明示
          // 意味合い: 「結果セット列として返却」固定文言を廃止し、業務担当者が読める表現に
          const purpose = si.business_purpose || '(調整要)';
          return [
            String(si.no || ''),
            role,
            itemName,
            '—',  // 型は中間 JSON に未定義 (将来拡張) のため暫定
            purpose
          ];
        });
        blocks.push(buildTable(
          ['No', '役割', '項目・名称', '型', '用途'],
          outRows,
          [1, 2, 5, 1, 5]
        ));
      } else {
        blocks.push(p('（該当なし）'));
      }
    } else {
      // INSERT / UPDATE / DELETE は結果セットを持たない (RETURNING 句があれば将来対応)
      // 意味合い: v51 §3 空白禁止規範 → 明示的に「該当なし」と記す
      blocks.push(p('（該当なし）'));
    }

    // ========================================================================
    // (4) エラーブロック: エラー条件 / メッセージ ID / 業務的影響
    // ========================================================================
    // 目的: DB 操作で発生しうるエラーと業務的影響を定義
    // 意味合い: v51 §3 共通 7 項目「エラー」 + §15 責務分離原則 (DB 層は呼出元の業務文脈を持たない、汎用記述)
    //           中間 JSON に DB レベルのエラー定義がないため、現時点では「（該当なし）」固定
    //           将来 op.errors[] を追加する場合はここで展開
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '(4) エラー', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 120, after: 40 }
    }));
    blocks.push(p('（該当なし）'));
  }
  return blocks;
}

/**
 * パラメータセット（v17: §6 配下対応で prefix/level 引数化）
 */
function sectionParameters(parameters, opts = {}) {
  // 目的: §6.6 / §6.11 で業務パラメータ一覧を「No / 名称 / 型 / 受渡先 API / 説明」の 5 列で出力。
  // 意味合い: 「対象年月（YYYYMM）」「取引の種別」等の業務単位キーが、どの API でどう使われるかを業務担当者が一覧で把握できる形。
  //           v50.5 で中間 JSON 構造（flat parameters[] 配列、各エントリ no/name/type/passed_between[]/description）と整合。
  //           旧構造の parameters.post_body_params[].items[] ネストは存在せず（中間 JSON は flat 配列）、§6.6 が常に空表示だった（再発防止: A4 修正）。
  // 接続情報: 入力 = data.parameters / 出力 = h3 or h2 + 表
  //           中間 JSON 仕様 = 15_中間JSONスキーマ.md「parameters[]」セクション
  const { prefix = '13', level = 'h2' } = opts;
  // v50.5: flat 配列対応。空判定は配列自体の長さで行う。
  if (!parameters || !Array.isArray(parameters) || parameters.length === 0) return [];
  const heading = level === 'h3' ? h3(`${prefix} パラメータ`) : h2(`${prefix}. パラメータ`);
  // v50.5: 各パラメータの No / 名称 / 型 / 受渡先 API 一覧 / 業務的説明 を 1 行で表示。
  //         passed_between[] は「API001 get_session_data」等の文字列配列、改行区切りで結合。
  const rows = parameters.map(p => [
    String(p.no || ''),
    p.name || '',
    p.type || '',
    (p.passed_between || []).join('\n'),
    p.description || ''
  ]);
  return [
    heading,
    // v48: パラメータ — 「No」は parameter カテゴリの header_self
    buildTable(
      [getIdHeaderLabel('parameter', 'self'), '名称', '型', '受渡先API', '説明'],
      rows,
      [1, 3, 3, 5, 6]
    )
  ];
}

/**
 * 共通ビジネスロジック（v17: §6 配下対応で prefix/level 引数化）
 */
function sectionCommonLogic(commonLogic, opts = {}) {
  // 目的: §5.5 共通ビジネスロジック (CL) を v51 §3 共通 7 項目 + §4 共通列で出力する。
  //       FR/BE/DB と同じ「H4 個別小節 + 概要表 + (1)入力 + (2)処理 + (3)出力 + (4)エラー」5 ブロック構造。
  // 意味合い: v51 §3 共通 7 項目「概要 / 動作場所 / 呼出元 / 呼出先 / 入力 / 処理 / 出力 / エラー」の機械強制点。
  //           v50.5 までの 4 列カタログ表 (No / ロジック名 / 説明 / 使用箇所) を廃止し、
  //           Investigator 確定の「v51 共通 7 項目フィールドが構造として無い」問題を docx 出力側で構造解決する。
  //           §16 「CL = システム全体スコープ限定」違反疑い (_classification_review='CA 統合候補') の
  //           エントリは概要表に「分類見直し」欄を明示表示して業務担当者がレビューで識別可能にする。
  // 接続情報: 入力 = data.common_logic[] (v51 共通 7 項目フィールドを持つ形を想定。持たない項目は「（未指定）」等の表示になる)
  //           出力 = h2/h3 + 各 CL エントリの h4 小節 + 5 表
  //           中間 JSON 仕様 = 15_中間JSONスキーマ.md「common_logic[]」セクション
  //           憲法仕様 = 16_docx出力ハンドオフ.md §3 §4 §9 §10 §15 §16
  //           参照実装 = sectionDbOperations (§5.6 / generate_docx.js 同関数)
  // B1（2026-07-19）: opts.diagrams / chapterNo は derivation_chain 図埋め込み用。sectionCalculations 側と同じ設計。
  const { prefix = '5.5', level = 'h2', diagrams = [] } = opts;
  const chapterNo = String(prefix).split('.')[0];
  if (!commonLogic || commonLogic.length === 0) return [];

  // §5.5 トップ見出し: level='h2' なら "5.5. 共通ビジネスロジック"、'h3' なら "5.5 共通ビジネスロジック"
  // 意味合い: 章番号体系は v51 §1 で §5.5 が CL カテゴリと固定。呼出元から prefix='5.5' で受ける
  const topHeading = level === 'h3' ? h3(`${prefix} 共通ビジネスロジック`) : h2(`${prefix}. 共通ビジネスロジック`);
  // 個別 CL# は H4 で展開 (§5.5.1 / §5.5.2 / ...)
  // 意味合い: §6.5 / §6.12 互換のため呼出元が h3 を要求しても個別小節は h4 で出す (CL はネストが深くなるため)
  const subHeadingFn = (i, title) => h4(`${prefix}.${i} ${title}`);

  const blocks = [topHeading];
  // §5.5 セクション直下の導入文 (業務担当者向け、共通ビジネスロジックの位置付けを明示)
  // 意味合い: §5.1 FR / §5.2 BE の各処理仕様表から「小数の精度を保つ計算」「端数処理」等として
  //          参照される業務横断ロジックの個別仕様カタログである旨を明示。
  //          例示は実装言語・特定の型名に依らない言い方にする（対象システムの言語を問わず読めるため）。
  // 接続情報: sectionCommonLogic の章見出し直後に出す導入文。
  blocks.push(p('§5.1 フロント処理 / §5.2 バック処理 の各処理仕様表内で「小数の精度を保つ計算」「端数処理」等として参照される、業務横断で使われる共通ビジネスロジックの個別仕様カタログです。各 CL は「概要表 → 入力 → 処理 → 出力 → エラー」の順で展開されます。'));
  // A2（2026-07-18）: h4 は目次(TOC)範囲外のため、カテゴリ内の全 CL を俯瞰する一覧表を追加。
  blocks.push(...buildProcessIndexTable(commonLogic, 'no', 'name', 'description'));

  let idx = 0;
  for (const cl of commonLogic) {
    idx++;
    // ========================================================================
    // (0) H4 見出し + 概要表 (共通 7 項目の前半: 概要 / 動作場所 / 呼出元 / 呼出先 [+ 分類見直し])
    // ========================================================================
    // 目的: 業務担当者が「この CL は何のため/どこで動く/誰が呼ぶ/どこを呼ぶ/分類見直しの要否」を冒頭で把握できる
    // 意味合い: v51 §3 共通 7 項目 (前半 4 項目) + §16 CL 定義の機械強制点。
    //           _classification_review='CA 統合候補' のエントリは「分類見直し」欄でマーク表示 (本タスクでは物理移動せず docx 上でマーク)
    // 接続情報: cl.no / cl.name / cl.description / cl.runtime_location /
    //           cl.used_by_processes / cl.used_by_processes_ids /
    //           cl.called_logic / cl.called_calc / cl.called_db /
    //           cl._classification_review
    const clId = cl.no || cl.logic_id || '';
    blocks.push(subHeadingFn(idx, `[${clId}] ${cl.name || ''}`));

    // v51 §19 1 文 1 概念ルール: 概要 + description_details[] bullet 箇条書きで描画
    //  意味合い: 1 行に複数のロジックを詰め込むと読みにくくなる問題（1 行 1 ステートメントのルール）
    //           への対応。renderDescriptionWithDetails で「概要 1 文 + ・詳細 bullet」を描画する
    //  接続情報: cl.description / cl.description_details / 16_docx出力ハンドオフ.md §19
    for (const para of renderDescriptionWithDetails(cl, 'description', null, '概要: ')) {
      blocks.push(para);
    }

    // 概要表: 動作場所 / 呼出元 / 呼出先 / [分類見直し]
    // 意味合い: 呼出元は ID 配列 (used_by_processes_ids) + 業務名併記 (used_by_processes) で両方の視点を提供
    //           呼出先は called_logic + called_calc + called_db を結合表示
    // 呼出元表示: ID 配列があれば「[FR1] [BE2] ...」、業務名のみは末尾に併記
    const callerIds = (cl.used_by_processes_ids || []).map(x => `[${x}]`).join(' ');
    const callerNames = (cl.used_by_processes || []).filter(s => {
      // ID 形式 (FR1 / BE1 等) は ID 表示と重複するので業務名側からは除外
      return !/^(FR|BE|CA|CL|DB|VL|MS|LG|AR|IT|EV|TR)\d+/.test(String(s).trim());
    });
    let callerDisplay = '';
    if (callerIds && callerNames.length > 0) {
      callerDisplay = `${callerIds}\n業務名: ${callerNames.join(' / ')}`;
    } else if (callerIds) {
      callerDisplay = callerIds;
    } else if (callerNames.length > 0) {
      callerDisplay = `業務名: ${callerNames.join(' / ')}`;
    } else {
      callerDisplay = '（該当なし）';
    }

    // 呼出先: called_logic (CL#) + called_calc (CA#) + called_db (DB#) を ID 配列で結合
    // 意味合い: 中間 JSON でこれらが空配列でも「（該当なし）」と明示 (§3 空白禁止規範)
    const calleeParts = [];
    if (Array.isArray(cl.called_logic) && cl.called_logic.length > 0) {
      calleeParts.push(`CL: ${cl.called_logic.map(x => `[${x}]`).join(' ')}`);
    }
    if (Array.isArray(cl.called_calc) && cl.called_calc.length > 0) {
      calleeParts.push(`CA: ${cl.called_calc.map(x => `[${x}]`).join(' ')}`);
    }
    if (Array.isArray(cl.called_db) && cl.called_db.length > 0) {
      calleeParts.push(`DB: ${cl.called_db.map(x => `[${x}]`).join(' ')}`);
    }
    const calleeDisplay = calleeParts.length > 0 ? calleeParts.join(' / ') : '（該当なし）';

    const overviewRows = [
      ['動作場所', cl.runtime_location || '（未指定）'],
      ['呼出元', callerDisplay],
      ['呼出先', calleeDisplay]
    ];
    // 分類見直し欄: _classification_review='CA 統合候補' の場合のみ表示
    // 意味合い: v51 §16 「CL = システム全体スコープ限定」違反疑いを docx 上でマーク。
    //           業務担当者が CL → CA 物理移動の要否をレビュー可能にする。
    if (cl._classification_review === 'CA 統合候補') {
      overviewRows.push([
        '分類見直し',
        'CA 統合候補（当画面固有計算の疑い、v51 §16「CL = システム全体スコープ限定」違反疑い）'
      ]);
    } else if (cl._classification_review === 'システム全体スコープ確認済') {
      overviewRows.push(['分類見直し', 'システム全体スコープ確認済']);
    }
    blocks.push(buildTable(['項目', '内容'], overviewRows, [2, 10]));

    // ========================================================================
    // (1) 入力ブロック: §4 共通列 (No / 種別 / 項目・名称 / 型 / 用途)
    // ========================================================================
    // 目的: CL が呼出元から受け取る入力値 (引数 / プレースホルダ 等) を一覧化
    // 意味合い: v51 §4 共通列 + §9 揺れ防止辞書「入力種別」の機械強制点。
    //           cl.inputs[] が空であれば §3 空白禁止規範に従い「（該当なし）」明示。
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '(1) 入力', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 120, after: 40 }
    }));
    if (Array.isArray(cl.inputs) && cl.inputs.length > 0) {
      // v51 §19 1 文 1 概念ルール: inputs[].purpose も bullet 詳細対応
      //  意味合い: 入力表「用途」列で実装語彙混入 / マルチステートメント記述を bullet 化
      //  接続情報: it.purpose / it.purpose_details / 16_docx出力ハンドオフ.md §19
      const inputColRatios = [1, 2, 5, 1, 5];
      const inputTableWidth = CONTENT_WIDTH - currentIndent;
      const _totI = inputColRatios.reduce((a, b) => a + b, 0);
      const inputColWidths = inputColRatios.map(r => Math.floor(inputTableWidth * r / _totI));
      inputColWidths[inputColWidths.length - 1] += (inputTableWidth - inputColWidths.reduce((a, b) => a + b, 0));
      const inputRows = cl.inputs.map(it => {
        const purposeCell = (Array.isArray(it.purpose_details) && it.purpose_details.length > 0)
          ? tdCellWithDetails(it, 'purpose', inputColWidths[4])
          : (it.purpose || '');
        return [
          String(it.no || ''),
          it.kind || '引数',
          it.item_name || '',
          it.type || '—',
          purposeCell
        ];
      });
      blocks.push(buildTable(
        ['No', '種別', '項目・名称', '型', '用途'],
        inputRows,
        inputColRatios
      ));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // ========================================================================
    // (2) 処理ブロック: Step 表 (No / 種別 / 処理内容)
    // ========================================================================
    // 目的: CL の処理ステップを §7 Step ルールに従って一覧化
    // 意味合い: v51 §7 Step ルール + §9 揺れ防止辞書「Step 種別」の機械強制点。
    //           cl.processing_steps[] が空であれば「（該当なし）」明示。
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '(2) 処理', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 120, after: 40 }
    }));
    if (Array.isArray(cl.processing_steps) && cl.processing_steps.length > 0) {
      // v51 §19 1 文 1 概念ルール: 処理内容セルは tdCellWithDetails で概要 + bullet 詳細を描画
      //  意味合い: cl.processing_steps[].description + .description_details[] で 1 セル内に複数行表示
      //  接続情報: buildTable は TableCell インスタンスを行要素として受け取れるよう拡張済
      const tableWidth = CONTENT_WIDTH - currentIndent;
      const colRatios = [1, 2, 9];
      const totalRatio = colRatios.reduce((a, b) => a + b, 0);
      const colWidths = colRatios.map(r => Math.floor(tableWidth * r / totalRatio));
      const sumWidths = colWidths.reduce((a, b) => a + b, 0);
      colWidths[colWidths.length - 1] += (tableWidth - sumWidths);

      const stepRows = cl.processing_steps.map(st => [
        String(st.no || ''),
        st.kind || '演算',
        tdCellWithDetails(st, 'description', colWidths[2])
      ]);
      blocks.push(buildTable(
        ['No', '種別', '処理内容'],
        stepRows,
        colRatios
      ));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // ========================================================================
    // (3) 出力ブロック: §4 共通列 (No / 種別 / 項目・名称 / 型 / 用途)
    // ========================================================================
    // 目的: CL が呼出元へ返す出力値 (戻り値 / レスポンス / ログ 等) を一覧化
    // 意味合い: v51 §4 共通列 + §9 揺れ防止辞書「出力種別」の機械強制点。
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '(3) 出力', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 120, after: 40 }
    }));
    if (Array.isArray(cl.outputs) && cl.outputs.length > 0) {
      // v51 §19 1 文 1 概念ルール: outputs[].purpose も bullet 詳細対応
      //  意味合い: 出力表「用途」列で実装語彙混入 / マルチステートメント記述を bullet 化
      //  接続情報: o.purpose / o.purpose_details / 16_docx出力ハンドオフ.md §19
      const outColRatios = [1, 2, 5, 1, 5];
      const outTableWidth = CONTENT_WIDTH - currentIndent;
      const _totO = outColRatios.reduce((a, b) => a + b, 0);
      const outColWidths = outColRatios.map(r => Math.floor(outTableWidth * r / _totO));
      outColWidths[outColWidths.length - 1] += (outTableWidth - outColWidths.reduce((a, b) => a + b, 0));
      const outRows = cl.outputs.map(o => {
        const purposeCell = (Array.isArray(o.purpose_details) && o.purpose_details.length > 0)
          ? tdCellWithDetails(o, 'purpose', outColWidths[4])
          : (o.purpose || '');
        return [
          String(o.no || ''),
          o.kind || '戻り値',
          o.item_name || '',
          o.type || '—',
          purposeCell
        ];
      });
      blocks.push(buildTable(
        ['No', '種別', '項目・名称', '型', '用途'],
        outRows,
        outColRatios
      ));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // ========================================================================
    // (4) エラーブロック: エラー条件 / メッセージ ID / 業務的影響
    // ========================================================================
    // 目的: CL の処理で発生しうるエラーと業務的影響を定義
    // 意味合い: v51 §3 共通 7 項目「エラー」 + §15 責務分離原則 (呼出元の業務文脈は持たない、汎用記述)
    //           cl.errors[] が空であれば「（該当なし）」明示。
    blocks.push(new Paragraph({
      children: [new TextRun({ text: '(4) エラー', font: FONT_BODY, size: SIZE_BODY, bold: true })],
      spacing: { before: 120, after: 40 }
    }));
    if (Array.isArray(cl.errors) && cl.errors.length > 0) {
      const errRows = cl.errors.map(e => [
        String(e.local_no || ''),
        e.condition || '',
        e.message_ref || '—',
        e.business_reason || ''
      ]);
      blocks.push(buildTable(
        ['No', '条件', 'メッセージ ID', '業務的影響'],
        errRows,
        [1, 5, 2, 5]
      ));
    } else {
      blocks.push(p('（該当なし）'));
    }

    // B1（2026-07-19）: derivation_chain 図（導出チェーン長3以上または分岐ありのロジックのみ対象、
    // 17_構成図生成.md 生成条件）を該当ロジックの直後に埋め込む。図が未生成/埋め込み失敗の場合は
    // buildDerivationChainEmbed が空配列を返すため、上記(1)〜(4)の既存テキスト表記のみで完結する
    // （フォールバック必須要件）。
    const chainDiagram = findDerivationChainDiagram(diagrams, cl.no, cl.name);
    if (chainDiagram) {
      blocks.push(...buildDerivationChainEmbed(chainDiagram, chapterNo));
    }
  }
  return blocks;
}

/**
 * 論理名対応表（v17: §6 統合後は §8 に章番号繰上げ、prefix 引数化）
 */
function sectionLogicalNames(logicalNames, opts = {}) {
  const { prefix = '15', level = 'h2' } = opts;
  if (!logicalNames || logicalNames.length === 0) return [];
  const rows = logicalNames.map(l => [
    l.physical_table || '', l.logical_table || '',
    l.physical_column || '', l.logical_column || ''
  ]);
  const heading = level === 'h3' ? h3(`${prefix} 論理名対応表`) : h2(`${prefix}. 論理名対応表`);
  return [
    heading,
    p('物理名（テーブル・カラム）と業務用論理名の対応表です。SQLや実装で参照する際に用います。'),
    buildTable(
      ['物理テーブル', '論理テーブル', '物理カラム', '論理カラム'],
      rows,
      [3, 3, 4, 4]
    )
  ];
}

/**
 * 目的: {機能名}_project_config.json を読み込む（存在すれば）。
 * 意味合い: A2（2026-07-18）で toc_depth（目次に含める見出しレベル）のオプション化に使用。
 *           project_config.json はプロジェクト横断の非機能規約を持つファイル（SKILL.md 参照）で、
 *           存在しない/読み込み失敗時は空オブジェクトを返し既定値にフォールバックする（後方互換）。
 * 接続情報: パス規則 = target/intermediate/{機能名}_project_config.json（INPUT_JSON と同じディレクトリ）
 */
function loadProjectConfig(featureName) {
  const configPath = path.join(path.dirname(INPUT_JSON), `${featureName}_project_config.json`);
  try {
    if (fs.existsSync(configPath)) {
      return JSON.parse(fs.readFileSync(configPath, 'utf-8'));
    }
  } catch (e) {
    console.warn(`[warn] project_config.json ロード失敗 (${configPath}): ${e.message}. 既定値にフォールバック`);
  }
  return {};
}

// =============================================================================
// メイン
// =============================================================================

function main() {
  // 中間JSON 読込
  if (!fs.existsSync(INPUT_JSON)) {
    throw new Error(`Input JSON not found: ${INPUT_JSON}`);
  }
  const raw = fs.readFileSync(INPUT_JSON, 'utf-8');
  const data = JSON.parse(raw);

  const meta = data.meta || {};
  // 2026-10-05 汎用化: 機能名の既定値を持たない。
  //   理由: 既定値があると、meta.feature_name が欠けた中間 JSON でも別画面の名前で設計書が出来てしまう。
  //   接続情報: 少なくとも直後の loadProjectConfig（設定ファイル名の解決）と表題・ヘッダー・Document.title が使う。
  //             verify_intermediate.py も meta.feature_name の空を ERROR として扱う。
  if (!meta.feature_name) {
    throw new Error(`meta.feature_name 未設定: 中間 JSON（${INPUT_JSON}）の meta.feature_name に機能名を入れてください`);
  }
  const featureName = meta.feature_name;
  // A2（2026-07-18）: project_config.json の docx_output.toc_depth を目次深度に反映（既定 '1-3'、現行互換）。
  const projectConfig = loadProjectConfig(featureName);
  // 2026-10-05 汎用化: 本文フォント・本文サイズ・禁止語を project_config から決める。
  //   目的: プロジェクト固有の値をスクリプトに直書きしない。未設定なら定義箇所の既定値（'Meiryo UI' / 16 / 空）のまま。
  //   接続情報: FONT_BODY / SIZE_BODY / FORBIDDEN_TERMS はモジュール先頭の let。以降の build* / section* が
  //             関数内で参照するので、docx の部品を組み立て始める前のこの位置で代入する。
  if (projectConfig.docx_output && projectConfig.docx_output.font) FONT_BODY = projectConfig.docx_output.font;
  if (projectConfig.docx_output && projectConfig.docx_output.font_size_pt) SIZE_BODY = projectConfig.docx_output.font_size_pt * 2;
  // 2026-10-05 汎用化: キャッシュ層の呼び名を project_config の docx_output.cache_label から決める（未設定なら既定「キャッシュ」のまま）。
  //   接続情報: CACHE_LABEL はモジュール先頭の let。フォントと同じ理由で、部品を組み立て始める前のこの位置で代入する。
  if (projectConfig.docx_output && projectConfig.docx_output.cache_label) CACHE_LABEL = projectConfig.docx_output.cache_label;
  FORBIDDEN_TERMS = Object.keys((projectConfig.forbidden_terms_map && projectConfig.forbidden_terms_map.terms) || {});
  const tocDepth = (projectConfig.docx_output && projectConfig.docx_output.toc_depth) || '1-3';
  // 2026-10-05 汎用化: 文書種別名を project_config の docx_output.doc_title から決める（既定 '詳細設計書'、現行互換）。
  //   接続情報: この main の中で、表題（H1）・ヘッダー・Document.title の「{機能名} {文書種別名}」に使う。
  const docTitle = (projectConfig.docx_output && projectConfig.docx_output.doc_title) || '詳細設計書';
  // v55.0: フォーマット憲法バージョンを _meta.format_version から resolve
  //   優先順: 中間 JSON `_meta.format_version` > FORMAT_CONSTITUTION_VERSION 定数フォールバック
  //   2026-10-06: 本文には出さず、Document の description（文書のプロパティ）にだけ残す（sectionDocInfo のコメント参照）
  const formatVersion = (data._meta && data._meta.format_version) || FORMAT_CONSTITUTION_VERSION;
  // 「論理名[物理名]」表記用の逆引きマップを1回だけ構築し、各セクションに渡す
  const maps = buildLogicalNameMaps(data);

  // A2（2026-07-18）: 全処理系 ID を事前収集し、見出しの Bookmark 化・本文中 ID 参照の
  // InternalHyperlink 化の判定に使う（実行順序に関わらず全 ID を先に把握しておくため）。
  _allKnownIds = collectAllProcessIds(data);

  // 全セクションを順次組み立て
  const children = [];

  // H1 + TOC
  children.push(h1(`${featureName} ${docTitle}`));
  children.push(new Paragraph({
    children: [new TextRun({
      text: '目次',
      font: FONT_BODY, size: SIZE_H2, bold: true
    })],
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 240, after: 120 }
  }));
  // v30: 目次の動的フィールド仕様の案内（docx-js は TOC をフィールド形式で出力するため、
  // 生成直後の docx を Word で開くと目次のページ番号が全て「1」になる。
  // ユーザーは目次を右クリック → 「フィールドを更新」または F9 で正しいページ番号に更新する）
  children.push(new Paragraph({
    children: [new TextRun({
      text: '※ Word で開いた後、目次を右クリック → 「フィールドを更新」（または F9 キー）を実行してページ番号を反映してください。',
      font: FONT_BODY, size: 18, italics: true, color: '666666'
    })],
    spacing: { before: 0, after: 120 }
  }));
  children.push(new TableOfContents('Table of Contents', {
    hyperlink: true,
    headingStyleRange: tocDepth
  }));
  // TOC直後でページ区切り
  children.push(new Paragraph({ children: [new PageBreak()] }));

  // 各セクション
  // v51 章構成憲法:
  //   §1 文書情報 / §2 構成図 / §3 画面レイアウト / §4 トリガー / §5 処理詳細 / §6 メッセージ
  // 旧 §2 改訂履歴 は廃止（git log で代替、設計書本文に含めない）。
  // 旧 §3 処理構成図 は §2 構成図 にリネーム（IPO/画面構成図含む）。
  children.push(...sectionDocInfo(meta));
  children.push(...sectionProcessFlow(data));          // §2 構成図（旧 §3）
  children.push(...sectionScreenLayout(data.screen_layout, data));  // §3 画面レイアウト（旧 §4）
  // v51: trigger_groups[] を §4 トリガー（旧 §5）として出力
  children.push(...sectionTriggerGroups(data));
  // v51: 処理詳細を §5（旧 §6）として出力
  // sectionProcesses 内部で §5.1 FR / §5.2 BE / §5.3 VL / §5.4 CA / §5.5 CL / §5.6 DB / §5.7 LG
  children.push(...sectionProcesses(data, maps));
  // v51: メッセージは §6 独立章（旧 §7）
  children.push(...sectionMessages(data.messages, { prefix: '6' }));
  // A2（2026-07-18）: 巻末 ID 索引。目次(TOC)が個別処理(h4/h5)を拾わない代わりに、
  // 全 ID をクリック1回で該当箇所へジャンプできる逆引き表として提供する。
  children.push(...sectionIdIndex(data));

  // ヘッダー/フッター
  // ヘッダー: 右寄せに「{機能名} {文書種別名}」（文書種別名は docTitle = docx_output.doc_title）
  const docHeader = new Header({
    children: [new Paragraph({
      children: [
        new TextRun({ text: '\t', font: FONT_BODY, size: SIZE_BODY }),
        new TextRun({ text: `${featureName} ${docTitle}`, font: FONT_BODY, size: SIZE_BODY, italics: true })
      ],
      tabStops: [{ type: TabStopType.RIGHT, position: CONTENT_WIDTH }],
      alignment: AlignmentType.LEFT
    })]
  });

  // フッター: 左に生成日時、中央にページ番号
  const generatedAtStr = formatGeneratedAt(meta.generated_at);
  const docFooter = new Footer({
    children: [new Paragraph({
      children: [
        new TextRun({ text: `生成日時: ${generatedAtStr}`, font: FONT_BODY, size: SIZE_BODY }),
        new TextRun({ text: '\t', font: FONT_BODY, size: SIZE_BODY }),
        new TextRun({ children: [PageNumber.CURRENT], font: FONT_BODY, size: SIZE_BODY }),
        new TextRun({ text: ' / ', font: FONT_BODY, size: SIZE_BODY }),
        new TextRun({ children: [PageNumber.TOTAL_PAGES], font: FONT_BODY, size: SIZE_BODY }),
        new TextRun({ text: '\t', font: FONT_BODY, size: SIZE_BODY })
      ],
      tabStops: [
        { type: TabStopType.CENTER, position: Math.floor(CONTENT_WIDTH / 2) },
        { type: TabStopType.RIGHT, position: CONTENT_WIDTH }
      ]
    })]
  });

  // Document
  const doc = new Document({
    creator: 'design-doc skill',
    title: `${featureName} ${docTitle}`,
    // 2026-10-06: フォーマットの版は本文から外し、文書のプロパティ（docProps/core.xml の dc:description）に残す。
    description: `フォーマット ${formatVersion}`,
    // A2（2026-07-18）: TOC を開いた瞬間に自動更新させる。旧版は「F9 で更新してください」という
    // 案内文のみで、目次のページ番号が全て「1」のまま開かれる問題があった。
    features: { updateFields: true },
    styles: {
      default: {
        document: { run: { font: FONT_BODY, size: SIZE_BODY } },
        // A5（2026-07-18）: Heading1-6 を styles.default 経由でカスタマイズする（docx-js 実測で確認した
        // 正しい API — styles.paragraphStyles 配列に id:'Heading1' 等の同名エントリを追加しても
        // ビルトインの既定スタイルに上書きされて無視される。styles.default.heading1〜heading6 が
        // ビルトイン見出しスタイルを直接カスタマイズする公式な方法）。
        // h1〜h6() 側の TextRun は文字列のみを持ち、フォント・サイズ・色・太字は全てここで一元管理する。
        heading1: {
          run: { size: SIZE_H1, bold: true, font: FONT_BODY, color: COLOR_H1 },
          paragraph: { spacing: { before: 240, after: 240 }, outlineLevel: 0 }
        },
        heading2: {
          run: { size: SIZE_H2, bold: true, font: FONT_BODY, color: COLOR_H2 },
          paragraph: { spacing: { before: 240, after: 180 }, outlineLevel: 1 }
        },
        heading3: {
          run: { size: SIZE_H3, bold: true, font: FONT_BODY, color: COLOR_H3 },
          paragraph: { spacing: { before: 180, after: 120 }, outlineLevel: 2 }
        },
        heading4: {
          run: { size: SIZE_H4, bold: true, font: FONT_BODY, color: COLOR_H4 },
          paragraph: { spacing: { before: 120, after: 80 }, outlineLevel: 3 }
        },
        heading5: {
          run: { size: SIZE_H5, bold: true, font: FONT_BODY, color: COLOR_H5 },
          paragraph: { spacing: { before: 100, after: 60 }, outlineLevel: 4 }
        },
        heading6: {
          run: { size: SIZE_H6, bold: true, font: FONT_BODY, color: COLOR_H6 },
          paragraph: { spacing: { before: 80, after: 50 }, outlineLevel: 5 }
        }
      }
    },
    sections: [{
      properties: {
        page: {
          size: { width: PAGE_WIDTH, height: PAGE_HEIGHT },
          // A6（2026-07-19）: 上下も MARGIN（720dxa）に統一（旧版は180dxa固定で余白の均衡が崩れていた）。
          margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN }
        }
      },
      children
    }]
  });

  // パッキング
  return Packer.toBuffer(doc).then(buffer => {
    // 出力ディレクトリ確認
    const outDir = path.dirname(OUTPUT_DOCX);
    if (!fs.existsSync(outDir)) {
      fs.mkdirSync(outDir, { recursive: true });
    }
    fs.writeFileSync(OUTPUT_DOCX, buffer);
    const sizeKB = (buffer.length / 1024).toFixed(1);
    console.log(`OK: ${OUTPUT_DOCX} (${sizeKB} KB)`);
  });
}

main().catch(err => {
  console.error('ERROR:', err && err.stack || err);
  process.exit(1);
});
