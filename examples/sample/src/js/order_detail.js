// 受注入力画面(order_detail.html)の処理。
// 目的: 受注ヘッダと明細の表示・入力・保存を行う。
// 意味合い: design-doc スキルの架空サンプル。画面は素の JavaScript で、
//   API は common/js/api.js のグローバル関数 apiCall で呼ぶ。
//   どの呼出も、メソッド・パス・action を文字列リテラルで直接書く
//   (スキルの呼出グラフ解析が、この書き方を手がかりに画面と API を突き合わせるため)。
// 接続情報: 呼出先は /api/orders(order-handler/handler.py が action で処理を振り分ける)。
//   画面項目の id・class は order_detail.html と対応する。
//   apiCall は「成功時は応答の本体(オブジェクト)を返し、失敗時は例外を投げる」前提で使う。

// メッセージ欄に文言を表示する(空文字で消去)。
function showMessage(text) {
  document.getElementById('messageArea').textContent = text;
}

// id で指定した入力欄へ値を入れる(顧客名・小計・消費税・請求額などの読取専用の欄で使う)。
function setValue(id, value) {
  document.getElementById(id).value = value;
}

// 入力文字列を数値にする。空欄は 0 とみなさず NaN を返す(金額を黙って 0 にしないため)。
function toNumber(text) {
  return text.trim() === '' ? NaN : Number(text);
}

// 明細1行の入力値(商品コード・数量・単価)を文字列のまま取り出す。
function readRow(row) {
  return {
    item_code: row.querySelector('.item-code').value.trim(),
    quantity: row.querySelector('.quantity').value.trim(),
    unit_price: row.querySelector('.unit-price').value.trim()
  };
}

// 何も入力されていない明細行かどうかを返す(未入力の行は計算・保存の対象にしない)。
function isBlankRow(values) {
  return values.item_code === '' && values.quantity === '' && values.unit_price === '';
}

// 明細に1行を追加する。item を渡すと、その値を初期表示する(受注の読込時)。
// 行の作りは order_detail.html の tbody#itemRows にある最初の行と同じにしてある。
function addRow(item) {
  const row = document.createElement('tr');
  row.innerHTML =
    '<td><input type="text" class="item-code"></td>' +
    '<td><input type="text" class="item-name" readonly></td>' +
    '<td><input type="number" class="quantity num" min="1" step="1"></td>' +
    '<td><input type="number" class="unit-price num" min="0" step="1"></td>' +
    '<td><input type="text" class="amount num" readonly></td>';
  if (item) {
    row.querySelector('.item-code').value = item.item_code;
    row.querySelector('.item-name').value = item.item_name;
    row.querySelector('.quantity').value = item.quantity;
    row.querySelector('.unit-price').value = item.unit_price;
  }
  document.getElementById('itemRows').appendChild(row);
}

// 金額・小計・消費税・請求額を再計算する。
// 数量・単価が空欄または数値でない行があれば、メッセージを出して何も書き換えない。
function recalculate() {
  const rows = Array.from(document.querySelectorAll('#itemRows tr'));
  const amounts = [];
  for (const row of rows) {
    const values = readRow(row);
    if (isBlankRow(values)) {
      amounts.push(null);
      continue;
    }
    const quantity = toNumber(values.quantity);
    const unitPrice = toNumber(values.unit_price);
    if (Number.isNaN(quantity) || Number.isNaN(unitPrice)) {
      showMessage('数量と単価は数値で入力してください。');
      return;
    }
    amounts.push(quantity * unitPrice);
  }
  let subtotal = 0;
  rows.forEach(function (row, index) {
    const amount = amounts[index];
    row.querySelector('.amount').value = amount === null ? '' : amount;
    if (amount !== null) {
      subtotal += amount;
    }
  });
  // 消費税は小計の 10%(1円未満切捨て)。請求額は小計 + 消費税。
  const tax = Math.floor(subtotal / 10);
  setValue('subtotal', subtotal);
  setValue('tax', tax);
  setValue('totalAmount', subtotal + tax);
  showMessage('');
}

// 受注番号で受注データを取得し、ヘッダと明細を表示する(初期表示と検索ボタンの共通処理)。
// 取得に失敗したときは、メッセージを出して画面を書き換えない。
async function loadOrder(orderNo) {
  let order;
  try {
    order = await apiCall('POST', '/api/orders', { action: 'order_get', order_no: orderNo });
  } catch (error) {
    showMessage('受注データを取得できませんでした: ' + error.message);
    return;
  }
  document.getElementById('orderNo').value = order.order_no;
  document.getElementById('orderDate').value = order.order_date;
  document.getElementById('customerCode').value = order.customer_code;
  setValue('customerName', order.customer_name);
  document.getElementById('itemRows').innerHTML = '';
  order.items.forEach(addRow);
  recalculate();
}

// EV00: 初期表示
// URL パラメータ order_no があれば受注データを取得する。無ければ空の明細を1行用意する。
function onInit() {
  document.getElementById('customerCode').addEventListener('change', onCustomerCodeChange);
  document.getElementById('itemRows').addEventListener('change', function (event) {
    // 明細行は後から増えるので、行ごとではなく tbody で受けて class で振り分ける。
    if (event.target.classList.contains('item-code')) {
      onItemCodeChange(event.target.closest('tr'));
    } else if (event.target.classList.contains('quantity') || event.target.classList.contains('unit-price')) {
      onQuantityOrPriceChange();
    }
  });
  document.getElementById('btnSearch').addEventListener('click', onSearchClick);
  document.getElementById('btnAddRow').addEventListener('click', onAddRowClick);
  document.getElementById('btnSave').addEventListener('click', onSaveClick);
  document.getElementById('btnClear').addEventListener('click', onClearClick);

  const orderNo = new URLSearchParams(location.search).get('order_no');
  if (orderNo) {
    loadOrder(orderNo);
  } else if (document.getElementById('itemRows').children.length === 0) {
    addRow();
  }
}

// EV01: 顧客コード入力
// 顧客コードから顧客名を取得して表示する。取得に失敗したら顧客名を空にしてメッセージを出す。
async function onCustomerCodeChange() {
  let customer;
  try {
    customer = await apiCall('POST', '/api/orders', {
      action: 'customer_get',
      customer_code: document.getElementById('customerCode').value.trim()
    });
  } catch (error) {
    setValue('customerName', '');
    showMessage('顧客を取得できませんでした: ' + error.message);
    return;
  }
  setValue('customerName', customer.customer_name);
  showMessage('');
}

// EV02: 商品コード入力
// 行の商品コードから商品名と単価を取得し、その行に初期セットする。
async function onItemCodeChange(row) {
  let item;
  try {
    item = await apiCall('POST', '/api/orders', {
      action: 'item_get',
      item_code: row.querySelector('.item-code').value.trim()
    });
  } catch (error) {
    row.querySelector('.item-name').value = '';
    showMessage('商品を取得できませんでした: ' + error.message);
    return;
  }
  row.querySelector('.item-name').value = item.item_name;
  row.querySelector('.unit-price').value = item.unit_price;
  showMessage('');
  // 数量が入力済みの行だけ再計算する(未入力のうちは「数値で入力」のメッセージを出さない)。
  if (row.querySelector('.quantity').value.trim() !== '') {
    recalculate();
  }
}

// EV03: 数量・単価変更
// 金額・小計・消費税・請求額を再計算する(API は呼ばない)。
function onQuantityOrPriceChange() {
  recalculate();
}

// EV04: 検索ボタン
// 入力された受注番号で受注データを取得する。
function onSearchClick() {
  loadOrder(document.getElementById('orderNo').value.trim());
}

// EV05: 行追加
// 明細の末尾に空の行を追加する。
function onAddRowClick() {
  addRow();
}

// 入力チェック済みの受注を保存する(保存ボタンから呼ぶ)。
// 目的: order_save の呼出を、入力チェックとは別の関数に分ける。
// 意味合い: スキルの呼出グラフ解析は、apiCall の直前にある行頭の「名前(」を呼出元の関数名とみなす。
//   入力チェックの showMessage(...) や if (...) が apiCall の手前に並ぶと、呼出元を取り違えるため、
//   この関数の中では apiCall より前に関数呼出や if を書かない(loadOrder と同じ作り)。
// 接続情報: 呼出元は onSaveClick。呼出先は /api/orders の order_save(order-handler/handler.py)。
//   保存に失敗したときは、メッセージを出して保存済みの扱いにしない。
async function saveOrder(order) {
  let saved;
  try {
    saved = await apiCall('POST', '/api/orders', {
      action: 'order_save',
      order_no: order.order_no,
      order_date: order.order_date,
      customer_code: order.customer_code,
      items: order.items
    });
  } catch (error) {
    showMessage('保存できませんでした: ' + error.message);
    return;
  }
  document.getElementById('orderNo').value = saved.order_no;
  showMessage('保存しました。');
}

// EV06: 保存
// 入力チェックを行い、エラーが無ければ受注を保存する(保存の呼出は saveOrder)。
// チェックエラーは、メッセージを出して保存しない。
// チェックの条件は order-handler/handler.py の order_save の入力チェックと揃えてある
// (受注番号・受注日・顧客コードは必須、明細の商品コードは必須、数量は1以上の整数、
//  単価は0以上の整数、明細は1行以上)。文言も同じだが、商品コードの空欄だけは
// 画面が「商品コードを入力してください。」、API が「指定された商品コードは存在しません。」と異なる。
// 商品コードが items に存在するかどうかの確認は API(order_save)だけが行う。
function onSaveClick() {
  const orderNo = document.getElementById('orderNo').value.trim();
  if (orderNo === '') {
    showMessage('受注番号を入力してください。');
    return;
  }
  const orderDate = document.getElementById('orderDate').value;
  if (orderDate === '') {
    showMessage('受注日を入力してください。');
    return;
  }
  const customerCode = document.getElementById('customerCode').value.trim();
  if (customerCode === '') {
    showMessage('顧客コードを入力してください。');
    return;
  }
  const items = [];
  for (const row of document.querySelectorAll('#itemRows tr')) {
    const values = readRow(row);
    if (isBlankRow(values)) {
      continue;
    }
    // 商品コードの空欄は画面で止める(存在するかどうかの確認は API が行う)。
    if (values.item_code === '') {
      showMessage('商品コードを入力してください。');
      return;
    }
    const quantity = toNumber(values.quantity);
    if (!Number.isInteger(quantity) || quantity < 1) {
      showMessage('数量は1以上の整数で入力してください。');
      return;
    }
    // 単価は整数だけを受ける(テーブルの列が整数で、API も整数以外をエラーにするため)。
    const unitPrice = toNumber(values.unit_price);
    if (!Number.isInteger(unitPrice) || unitPrice < 0) {
      showMessage('単価は0以上の整数で入力してください。');
      return;
    }
    items.push({ item_code: values.item_code, quantity: quantity, unit_price: unitPrice });
  }
  if (items.length === 0) {
    showMessage('明細を1行以上入力してください。');
    return;
  }
  saveOrder({ order_no: orderNo, order_date: orderDate, customer_code: customerCode, items: items });
}

// EV07: クリア
// 全項目を空にし、明細を空の1行に戻す。
function onClearClick() {
  document.getElementById('orderNo').value = '';
  document.getElementById('orderDate').value = '';
  document.getElementById('customerCode').value = '';
  setValue('customerName', '');
  setValue('subtotal', '');
  setValue('tax', '');
  setValue('totalAmount', '');
  document.getElementById('itemRows').innerHTML = '';
  addRow();
  showMessage('');
}

document.addEventListener('DOMContentLoaded', onInit);
