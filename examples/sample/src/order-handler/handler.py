"""受注入力画面(架空のサンプル)のバックエンド入口ファイル。

【目的】
  受注入力画面からの POST /api/orders を1つの入口 handler() で受け、
  リクエスト本文の action の値で処理を振り分ける。

【意味合い】
  design-doc スキルが解析する対象の見本。スクリプトが今検出できる形
  (action を変数へ代入してから if / elif の等値比較で振り分ける形)に合わせてある。
  辞書による振り分け・match 文・in 比較は検出の対象外なので使っていない。
  動く完成品ではなく、読んで作りが分かる最小のソースである。

【接続情報】
  呼出元: 画面の js/order_detail.js が、共通の common/js/api.js の
          apiCall('POST', '/api/orders', { action: '...', ... }) で呼ぶ。
  呼出先: common/python/db_utils.py の get_connection()(DB 接続)。
  永続化先: テーブル orders / order_items(読み書き)、customers / items(読むだけ)。
            列の定義はサンプル直下の schema.sql。
"""
import os
import sqlite3
import sys

# 共通モジュール(common/python)を、このファイルの位置から相対で探す。
# 置き場所(作業ディレクトリ)に依らず import できるようにするため。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common', 'python'))

from db_utils import get_connection  # noqa: E402

# 消費税率(%)。消費税 = 小計 × 税率 ÷ 100 を整数で切り捨てる。
TAX_RATE_PERCENT = 10

# ---- SQL(プレースホルダは ?)----------------------------------------------
# 受注ヘッダと顧客名を1件取得する(order_get)。
SQL_SELECT_ORDER = (
    "SELECT o.order_no, o.order_date, o.customer_code, c.customer_name,"
    " o.subtotal, o.tax, o.total_amount"
    " FROM orders o"
    " LEFT JOIN customers c ON c.customer_code = o.customer_code"
    " WHERE o.order_no = ?"
)
# 受注明細と商品名を行番号順に取得する(order_get)。
SQL_SELECT_ORDER_ITEMS = (
    "SELECT d.line_no, d.item_code, i.item_name, d.quantity, d.unit_price, d.amount"
    " FROM order_items d"
    " LEFT JOIN items i ON i.item_code = d.item_code"
    " WHERE d.order_no = ?"
    " ORDER BY d.line_no"
)
# 顧客名を取得する(customer_get と、order_save の顧客コードの存在チェック)。
SQL_SELECT_CUSTOMER = "SELECT customer_code, customer_name FROM customers WHERE customer_code = ?"
# 商品名と標準単価を取得する(item_get と、order_save の商品コードの存在チェック)。
SQL_SELECT_ITEM = "SELECT item_code, item_name, unit_price FROM items WHERE item_code = ?"
# 受注を保存する(order_save)。同じ受注番号の旧データを消してから入れ直す。
SQL_DELETE_ORDER_ITEMS = "DELETE FROM order_items WHERE order_no = ?"
SQL_DELETE_ORDER = "DELETE FROM orders WHERE order_no = ?"
SQL_INSERT_ORDER = (
    "INSERT INTO orders (order_no, order_date, customer_code, subtotal, tax, total_amount)"
    " VALUES (?, ?, ?, ?, ?, ?)"
)
SQL_INSERT_ORDER_ITEM = (
    "INSERT INTO order_items (order_no, line_no, item_code, quantity, unit_price, amount)"
    " VALUES (?, ?, ?, ?, ?, ?)"
)

# ---- エラーメッセージ(エラー応答の本文 message に入れる文言)----------------
# 画面にそのまま出るわけではない。このサンプルの common/js/api.js の apiCall は、
# HTTP の応答がエラーのとき応答の本文を読まずに例外を投げるので、画面に出るのは
# js/order_detail.js が組み立てる文言(HTTP ステータスつき)である。
MSG_UNKNOWN_ACTION = "不明な処理が指定されました。"
MSG_ORDER_NO_REQUIRED = "受注番号を入力してください。"
MSG_ORDER_DATE_REQUIRED = "受注日を入力してください。"
MSG_ORDER_NOT_FOUND = "指定された受注番号は存在しません。"
MSG_CUSTOMER_CODE_REQUIRED = "顧客コードを入力してください。"
MSG_CUSTOMER_NOT_FOUND = "指定された顧客コードは存在しません。"
MSG_ITEM_NOT_FOUND = "指定された商品コードは存在しません。"
MSG_ITEMS_REQUIRED = "明細を1行以上入力してください。"
MSG_QUANTITY_INVALID = "数量は1以上の整数で入力してください。"
MSG_UNIT_PRICE_INVALID = "単価は0以上の整数で入力してください。"
MSG_SAVE_FAILED = "受注の保存に失敗しました。"


def handler(body: dict) -> dict:
    """入口。action の値で処理を振り分け、{'status': 整数, 'body': dict} を返す。

    目的: 画面からの1回の POST を、対応する処理へ渡す。
    意味合い: 振り分けをこの関数の if / elif に集めることで、
              「どの action がどの処理か」を1か所で読めるようにしている。
    接続情報: 呼出元は画面の apiCall('POST', '/api/orders', {...})。
              呼出先は下の _order_get / _customer_get / _item_get / _order_save。
    応答の形: 戻り値の status は HTTP ステータスに、body は応答の本文(JSON)になる前提。
              画面側の apiCall が返すのは body の中身そのもの(例: order.order_no)である。
              status と body を HTTP の応答へ写す層(Web サーバ)は、このサンプルに含まない。
    """
    action = body.get('action')
    conn = get_connection()
    try:
        if action == 'order_get':
            return _order_get(conn, body)
        elif action == 'customer_get':
            return _customer_get(conn, body)
        elif action == 'item_get':
            return _item_get(conn, body)
        elif action == 'order_save':
            return _order_save(conn, body)
        else:
            return _error(400, MSG_UNKNOWN_ACTION)
    finally:
        conn.close()


def _error(status: int, message: str) -> dict:
    """エラー応答を作る。

    目的: エラー時の応答の形を {'status': ..., 'body': {'message': ...}} に揃える。
    意味合い: エラーの理由を、応答の本文の message という決まった場所に入れる。
              このサンプルの画面側(common/js/api.js の apiCall)は、エラー応答の本文を
              読まないので、この message は今は画面に表示されない。
    接続情報: このファイルの handler と各処理関数から呼ばれる。
    """
    return {'status': status, 'body': {'message': message}}


def _is_int(value) -> bool:
    """整数かどうか(真偽値は整数とみなさない)。

    目的: 数量・単価の入力チェックで使う。
    意味合い: 空欄・文字列・小数を 0 などへ読み替えず、エラーにするための判定。
    接続情報: _order_save から呼ばれる。
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _order_get(conn, body: dict) -> dict:
    """受注1件(ヘッダと明細)を返す。

    目的: 初期表示と検索ボタンで、受注番号から受注データを画面へ返す。
    意味合い: 顧客名・商品名は名称のテーブルから引き、画面で引き直さなくて済むようにする。
    接続情報: 呼出元は handler(action が order_get のとき)。
              orders・customers・order_items・items を読む。
    """
    order_no = body.get('order_no')
    if not order_no:
        return _error(400, MSG_ORDER_NO_REQUIRED)
    order = conn.execute(SQL_SELECT_ORDER, (order_no,)).fetchone()
    if order is None:
        return _error(404, MSG_ORDER_NOT_FOUND)
    rows = conn.execute(SQL_SELECT_ORDER_ITEMS, (order_no,)).fetchall()
    return {'status': 200, 'body': {
        'order_no': order[0],
        'order_date': order[1],
        'customer_code': order[2],
        'customer_name': order[3],
        'subtotal': order[4],
        'tax': order[5],
        'total_amount': order[6],
        'items': [
            {'line_no': r[0], 'item_code': r[1], 'item_name': r[2],
             'quantity': r[3], 'unit_price': r[4], 'amount': r[5]}
            for r in rows
        ],
    }}


def _customer_get(conn, body: dict) -> dict:
    """顧客コードから顧客名を返す。

    目的: 顧客コードの入力時に、顧客名を画面へ返す。
    意味合い: 存在しないコードをその場で知らせ、保存時まで誤りを持ち越さない。
    接続情報: 呼出元は handler(action が customer_get のとき)。customers を読む。
    """
    customer_code = body.get('customer_code')
    if not customer_code:
        return _error(400, MSG_CUSTOMER_CODE_REQUIRED)
    row = conn.execute(SQL_SELECT_CUSTOMER, (customer_code,)).fetchone()
    if row is None:
        return _error(404, MSG_CUSTOMER_NOT_FOUND)
    return {'status': 200, 'body': {'customer_code': row[0], 'customer_name': row[1]}}


def _item_get(conn, body: dict) -> dict:
    """商品コードから商品名と標準単価を返す。

    目的: 明細の商品コードの入力時に、商品名と単価の初期値を画面へ返す。
    意味合い: 単価は画面で書き換えられるので、ここで返すのは初期値である。
    接続情報: 呼出元は handler(action が item_get のとき)。items を読む。
    """
    row = conn.execute(SQL_SELECT_ITEM, (body.get('item_code'),)).fetchone()
    if row is None:
        return _error(404, MSG_ITEM_NOT_FOUND)
    return {'status': 200, 'body': {'item_code': row[0], 'item_name': row[1], 'unit_price': row[2]}}


def _order_save(conn, body: dict) -> dict:
    """入力チェックのうえ、受注ヘッダと明細を保存する。

    目的: 保存ボタンで、画面の入力内容を orders と order_items へ書く。
    意味合い: 金額(小計・消費税・請求額)は画面の計算結果を信用せず、
              数量と単価からここで計算し直す。ヘッダと明細は1つのトランザクションで書き、
              途中で失敗したら全部を取り消して 500 を返す(一部だけ保存された状態を残さない)。
    接続情報: 呼出元は handler(action が order_save のとき)。
              customers・items を読み、orders・order_items を書く。
    """
    order_no = body.get('order_no')
    order_date = body.get('order_date')
    customer_code = body.get('customer_code')
    items = body.get('items')

    if not order_no:
        return _error(400, MSG_ORDER_NO_REQUIRED)
    if not order_date:
        return _error(400, MSG_ORDER_DATE_REQUIRED)
    if not customer_code:
        return _error(400, MSG_CUSTOMER_CODE_REQUIRED)
    if not items:
        return _error(400, MSG_ITEMS_REQUIRED)
    for item in items:
        if not _is_int(item.get('quantity')) or item['quantity'] < 1:
            return _error(400, MSG_QUANTITY_INVALID)
        if not _is_int(item.get('unit_price')) or item['unit_price'] < 0:
            return _error(400, MSG_UNIT_PRICE_INVALID)
    if conn.execute(SQL_SELECT_CUSTOMER, (customer_code,)).fetchone() is None:
        return _error(400, MSG_CUSTOMER_NOT_FOUND)
    # 明細の商品コードが items にあることを確かめる(空欄・未指定も「存在しない」になる)。
    # schema.sql は order_items.item_code を必須かつ items への参照と定めているが、
    # 接続(db_utils.get_connection)は外部キーの検査を有効にしていないので、ここで止める。
    for item in items:
        if conn.execute(SQL_SELECT_ITEM, (item.get('item_code'),)).fetchone() is None:
            return _error(400, MSG_ITEM_NOT_FOUND)

    # 小計 = Σ(数量 × 単価)、消費税 = 小計の10%(切り捨て)、請求額 = 小計 + 消費税
    subtotal = sum(item['quantity'] * item['unit_price'] for item in items)
    tax = subtotal * TAX_RATE_PERCENT // 100
    total_amount = subtotal + tax

    try:
        conn.execute(SQL_DELETE_ORDER_ITEMS, (order_no,))
        conn.execute(SQL_DELETE_ORDER, (order_no,))
        conn.execute(SQL_INSERT_ORDER, (order_no, order_date, customer_code, subtotal, tax, total_amount))
        for line_no, item in enumerate(items, start=1):
            conn.execute(SQL_INSERT_ORDER_ITEM, (
                order_no, line_no, item.get('item_code'),
                item['quantity'], item['unit_price'], item['quantity'] * item['unit_price'],
            ))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return _error(500, MSG_SAVE_FAILED)
    return {'status': 200, 'body': {
        'order_no': order_no, 'subtotal': subtotal, 'tax': tax, 'total_amount': total_amount,
    }}
