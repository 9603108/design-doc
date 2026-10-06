-- ============================================================
-- 架空サンプル「受注入力」のテーブル定義(SQLite で実行できる形)
--
-- 目的: サンプル画面「受注入力」が読み書きする4テーブルの DDL。
-- 意味合い: design-doc は DDL と SQL 文から物理名(テーブル名・列名)を拾い、
--           各列の行末コメントを論理名として使う。設定の書き方を真似るための
--           雛形であり、実在の業務データではない。
-- 接続情報: 少なくとも src/order-handler/handler.py の SQL 文がこの4テーブルを
--           参照する。接続は src/common/python/db_utils.py が返す。
-- ============================================================

-- 顧客
CREATE TABLE customers (
    customer_code TEXT PRIMARY KEY,  -- 顧客コード
    customer_name TEXT NOT NULL      -- 顧客名
);

-- 商品
CREATE TABLE items (
    item_code  TEXT PRIMARY KEY,     -- 商品コード
    item_name  TEXT NOT NULL,        -- 商品名
    unit_price INTEGER NOT NULL      -- 単価
);

-- 受注
CREATE TABLE orders (
    order_no      TEXT PRIMARY KEY,  -- 受注番号
    order_date    TEXT NOT NULL,     -- 受注日
    customer_code TEXT NOT NULL,     -- 顧客コード
    subtotal      INTEGER NOT NULL,  -- 小計
    tax           INTEGER NOT NULL,  -- 消費税
    total_amount  INTEGER NOT NULL,  -- 請求額
    FOREIGN KEY (customer_code) REFERENCES customers (customer_code)
);

-- 受注明細
CREATE TABLE order_items (
    order_no   TEXT NOT NULL,        -- 受注番号
    line_no    INTEGER NOT NULL,     -- 行番号
    item_code  TEXT NOT NULL,        -- 商品コード
    quantity   INTEGER NOT NULL,     -- 数量
    unit_price INTEGER NOT NULL,     -- 単価
    amount     INTEGER NOT NULL,     -- 金額
    PRIMARY KEY (order_no, line_no),
    FOREIGN KEY (order_no) REFERENCES orders (order_no),
    FOREIGN KEY (item_code) REFERENCES items (item_code)
);

-- ============================================================
-- ここから下は DDL ではなく、動作確認用の初期データ(架空の値)
-- ============================================================

INSERT INTO customers (customer_code, customer_name) VALUES
    ('C001', 'サンプル商事'),
    ('C002', 'テスト物産');

INSERT INTO items (item_code, item_name, unit_price) VALUES
    ('I001', 'ノート', 200),
    ('I002', 'ボールペン', 120),
    ('I003', 'クリアファイル', 80);
