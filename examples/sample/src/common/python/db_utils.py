"""DB 接続の共通モジュール(架空サンプル「受注入力」用)。

【目的】
  バックエンドの関数が使う DB 接続を、1か所で作って返す。

【意味合い】
  接続先の決め方(環境変数 DB_PATH)と、行の受け取り方(列名で引ける
  sqlite3.Row)を、入口ファイルごとに書かずに済むようにする共通部品。
  このファイルはパスに common/ を含むので、設計書では「共通モジュール」
  として扱われる(バックエンドの入口ファイルとしては扱われない)。

【接続情報】
  呼出元: 少なくとも order-handler/handler.py が get_connection() を呼ぶ。
  接続先: 環境変数 DB_PATH が指す SQLite のデータベースファイル
          (テーブル定義はサンプル直下の schema.sql)。
"""
import os
import sqlite3


def get_connection() -> sqlite3.Connection:
    """DB 接続を1つ開いて返す。

    目的: 環境変数 DB_PATH が指すデータベースへの接続を返す。
    意味合い: DB_PATH が未設定のときは、既定の接続先へ黙って切り替えずに
        例外で止める(意図しないデータベースへ読み書きするのを防ぐため)。
        row_factory を sqlite3.Row にするので、呼出元は row['order_no'] の
        ように列名で値を取り出せる。
    接続情報: 少なくとも order-handler/handler.py から呼ばれる。
        接続を閉じるのは呼出元の責任。
    """
    db_path = os.environ.get("DB_PATH")
    if not db_path:
        raise RuntimeError("環境変数 DB_PATH が設定されていません。")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn
