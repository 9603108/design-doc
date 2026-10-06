// 共通 API 呼出(架空サンプル「受注入力」用)
//
// 目的: 画面の JavaScript からバックエンドの API を呼ぶ窓口を、この1関数にまとめる。
// 意味合い: design-doc は画面側の API 呼出を「共通の関数を呼ぶ箇所」として検出する。
//           画面ごとに fetch を直接書いた呼出は、この関数の呼出としては拾われず、
//           HTTP メソッドとパスが空の間接的な呼出として記録される。
//           HTTP メソッドとパスを呼出の記録に残すため、呼出はこの関数を通す。
//           HTTP エラーは例外にして呼出元へ返す(失敗を成功扱いにしない)。
// 接続情報: 呼出元は画面の JavaScript(このサンプルでは js/order_detail.js)。
//           呼出先はバックエンドの入口ファイル(order-handler/handler.py)で、
//           処理の種類は body に入れた値で振り分けられる。
//           order_detail.html が script タグでこのファイルを先に読み込むため、
//           グローバル関数として定義している。

/**
 * バックエンドの API を呼び、応答の JSON を返す。
 *
 * @param {string} method HTTP メソッド(このサンプルでは 'POST')
 * @param {string} path   API のパス(例: '/api/orders')
 * @param {object} body   リクエスト本文。処理の種類と入力値を持つオブジェクト
 * @returns {Promise<object>} 応答の JSON
 * @throws {Error} HTTP の応答がエラー(res.ok でない)のとき
 */
async function apiCall(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`API 呼出に失敗しました: ${method} ${path} (HTTP ${res.status})`);
  }
  return res.json();
}
