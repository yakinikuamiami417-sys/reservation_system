# test_prices.py — 基本の価格表と、塊の単価を直す機能の確認（python test_prices.py）
# 一時ファイルのSQLiteで動かすので、本番・手元のデータには触れない
import os, sys, tempfile
from datetime import date, timedelta

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['DATABASE_URL'] = 'sqlite:///' + _tmp.name.replace('\\', '/')
for k in ('APP_PASSWORD', 'MEAT_ADMIN_PASSWORD', 'ANTHROPIC_API_KEY'):
    os.environ.pop(k, None)
os.environ['NLBC_AUTO_LOOKUP'] = '0'

import meat as meat_module
import meat_db as mdb
import price_db as pdb
from app import app

failures = 0


def check(cond, msg):
    global failures
    print(('OK   ' if cond else 'FAIL ') + msg)
    if not cond:
        failures += 1


def raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


client = app.test_client()
today = meat_module._today()
parts = {p['name']: p['id'] for p in mdb.list_parts()}
items = {it['name']: it for it in pdb.list_items('肉', today)}

# ---- 初期値：今の部位が今までの単価で載る ----
check(items['ヒレ']['current']['unit_price'] == 11000 and items['ヒレ']['unit'] == 'kg' and items['ヒレ']['supplier_name'] == 'フード',
      'ヒレが 11,000円/kg・フードで載る')
check(items['ウデ']['current']['unit_price'] == 4200 and items['モモ']['current']['unit_price'] == 4200, 'ウデ・モモは 4,200円')
check(items['ハム']['unit'] == 'パック' and items['ハム']['current']['unit_price'] == 298, 'ハムは 298円/パック')
check('ウデモモ（部位未分類）' not in items, '部位未分類は載せない')
n = len(items)
pdb.set_price(items['ハム']['id'], 320, today.isoformat(), '社長')
mdb.init_meat_db()
after = {it['name']: it for it in pdb.list_items('肉', today)}
check(len(after) == n and after['ハム']['current']['unit_price'] == 320, 'もう一度起動しても増えず、変えた値段も初期値に戻らない')

# ---- 棚卸の評価：今までと同じ数字。仕入の無い部位だけ価格表を使う ----
check(mdb.valuation_prices('2026-10')[parts['ヒレ']] == 11000, '仕入が無ければ価格表の値段')
check(mdb.valuation_prices('2026-10')[parts['ウデモモ（部位未分類）']] == 4200, '部位未分類も今までと同じ 4,200円')
check(mdb.valuation_prices('2026-10')[parts['ハム']] == 298, '過去の棚卸は当時の値段（今日の値上げを入れない）')
future = date.fromordinal(today.toordinal() + 400)
check(mdb.valuation_prices(future.strftime('%Y-%m'))[parts['ハム']] == 320, '値上げ後の棚卸は新しい値段')

# ---- 値段の変更：履歴が残る・予定が入れられる ----
fid = items['ヒレ']['id']
res = client.post(f'/meat/prices/{fid}', data={'dept': '肉', 'unit_price': '11,500', 'effective_from': today.isoformat(), 'memo': '値上げ'})
check(res.status_code == 302, '値段を変更できる')
tomorrow = (today + timedelta(days=30)).isoformat()
client.post(f'/meat/prices/{fid}', data={'dept': '肉', 'unit_price': '12000', 'effective_from': tomorrow})
hire = next(it for it in pdb.list_items('肉', today) if it['id'] == fid)
check(hire['current']['unit_price'] == 11500 and hire['previous']['unit_price'] == 11000, '今と前回の値段が分かる')
check(hire['upcoming']['unit_price'] == 12000 and len(hire['history']) == 3, 'これからの値段は予定として入り、履歴が残る')
check(mdb.base_part_prices()[parts['ヒレ']] == 11500, '目利きの初期値は今日の値段（予定はまだ使わない）')
client.post(f'/meat/prices/{fid}', data={'dept': '肉', 'unit_price': '11800', 'effective_from': today.isoformat()})
hire = next(it for it in pdb.list_items('肉', today) if it['id'] == fid)
check(hire['current']['unit_price'] == 11800 and len(hire['history']) == 3, '同じ適用日に入れ直すと、その日の値段を直す')
for bad in ('0', '-5', 'abc', '12.5', ''):
    check(raises(lambda: pdb.set_price(fid, bad, today.isoformat(), 'x')), f'おかしな値段は入らない：{bad!r}')
check(raises(lambda: pdb.set_price(fid, 100, '2026-13-01', 'x')), 'おかしな日付は入らない')

# ---- 画面 ----
h = client.get('/meat/prices').get_data(as_text=True)
check('基本の価格表' in h and 'ヒレ' in h and '前回より +800円' in h, '価格表に値段と前回との差が出る')
check('meat-tabs' in h and '価格表</a>' in h, 'タブに価格表がある')
check(client.get('/meat/prices?dept=なし').status_code == 404, '無い部門は404')
h = client.get('/meat/new').get_data(as_text=True)
check(f'data-base="11800"' in h, '目利きの部位に基本の値段が入る')

# ---- 肉以外の部門 ----
res = client.post('/meat/prices/add', data={'dept': 'ドリンク', 'name': 'コカコーラ', 'unit': 'ケース', 'supplier_name': 'コカコーラ',
                                            'unit_price': '15704', 'effective_from': today.isoformat()})
drinks = pdb.list_items('ドリンク', today)
check(len(drinks) == 1 and drinks[0]['current']['unit_price'] == 15704 and drinks[0]['part_id'] is None, 'ドリンクの品目を足せる')
check('コカコーラ' in client.get('/meat/prices?dept=ドリンク').get_data(as_text=True), 'ドリンクのタブに出る')
check('コカコーラ' not in client.get('/meat/prices').get_data(as_text=True), '肉のタブには出ない')
check(raises(lambda: pdb.add_item('ドリンク', 'コカコーラ', 'ケース', '', 1, today.isoformat(), 'x')), '同じ品目は二重に足せない')
check(raises(lambda: pdb.add_item('肉', '牛すじ', 'kg', '', 1, today.isoformat(), 'x')), '肉は手で足さない（部位から載る）')
check(all(r for r in [mdb.valuation_prices('2026-10')]), '肉以外を足しても棚卸の評価は動く')

# ---- 塊の単価を直す ----
sup = mdb.get_or_create_supplier('フード')
lot = mdb.create_held_lot({'part_id': parts['ウデ'], 'supplier_id': sup, 'label_kg': '18.45', 'trade_kg': '18.4', 'unit_price': '4200',
                           'promised_on': '2027-02-01'}, '社長')
check(mdb.get_lot(lot)['promised_amount'] == 77280, '（前提）金額 77,280円')
res = client.post(f'/meat/lot/{lot}/price', data={'unit_price': '4500', 'memo': 'フード値上げ'})
got = mdb.get_lot(lot)
check(res.status_code == 302 and got['promised_unit_price'] == 4500 and got['promised_amount'] == 82800 and got['status'] == 'held',
      '目利き後に単価を直すと金額も変わる（状態は変わらない）')
check(any('4200円 → 4500円' in (e['memo'] or '') for e in got['events']), '直した記録が残る')
check(mdb.last_unit_price(parts['ウデ']) == 4500, '「前回の単価」も新しい値になる')
check('単価を直す' in client.get(f'/meat/lot/{lot}').get_data(as_text=True), '塊の画面に単価を直す欄がある')
check(raises(lambda: mdb.update_lot_price(lot, 'abc', 'x')) and raises(lambda: mdb.update_lot_price(lot, '0', 'x')), 'おかしな単価は入らない')

# 仕入は単価を直すと計算に反映され、締めた期間では止まる
mdb.receive_lots(sup, 'S-1', '2027-02-10', 0, [lot], '社長')
check(mdb.purchases_in_period('2027-02')[parts['ウデ']][1] == 82800, '（前提）2月の仕入 82,800円')
mdb.update_lot_price(lot, 4600, '社長')
check(mdb.purchases_in_period('2027-02')[parts['ウデ']][1] == 84640, '納品後に直すと仕入の金額も変わる')
client.post('/meat/count/2027-02', data={'action': 'confirm', f'lot_{lot}': '18.4', 'extra_count': '0'})
client.post('/meat/closing/2027-02', data={'action': 'confirm'})
check(mdb.compute_closing('2027-02')['status']['closed'], '（前提）2月を締めた')
check(raises(lambda: mdb.update_lot_price(lot, 5000, 'x')) and mdb.get_lot(lot)['promised_unit_price'] == 4600, '締めた期間の単価は直せない')
client.post('/meat/closing/2027-02', data={'action': 'reopen', 'reason': '単価の間違い'})
mdb.update_lot_price(lot, 5000, '社長')
check(mdb.get_lot(lot)['promised_unit_price'] == 5000, '締めを解除すれば直せる')

# ---- 管理者でなければ見るだけ ----
meat_module.MEAT_ADMIN_PASSWORD = 'pw'
guest = app.test_client()
h = guest.get('/meat/prices').get_data(as_text=True)
check('ヒレ' in h and '値段を変更' not in h, '管理者でなくても見られるが、変更の欄は出ない')
guest.post(f'/meat/prices/{fid}', data={'dept': '肉', 'unit_price': '1', 'effective_from': today.isoformat()})
check(mdb.base_part_prices()[parts['ヒレ']] == 11800, '管理者でなければ値段を変えられない')
guest.post(f'/meat/lot/{lot}/price', data={'unit_price': '1'})
check(mdb.get_lot(lot)['promised_unit_price'] == 5000, '管理者でなければ塊の単価を直せない')
guest.post('/meat/prices/add', data={'dept': 'ドリンク', 'name': 'いたずら', 'unit': '本', 'unit_price': '1', 'effective_from': today.isoformat()})
check(len(pdb.list_items('ドリンク', today)) == 1, '管理者でなければ品目を足せない')
meat_module.MEAT_ADMIN_PASSWORD = ''

# ---- バックアップ：正肉在庫と価格表が入る ----
import io
from openpyxl import load_workbook
from sqlalchemy import inspect, text
import backup_export
from database import engine
with engine.begin() as con:
    con.execute(text("UPDATE meat_lots SET memo = :m WHERE id = :i"), {'m': '制御\x01文字', 'i': lot})
    con.execute(text("INSERT INTO meat_photos (mime, data_b64, created_at) VALUES ('image/jpeg', 'WFhY', '2026-11-06')"))
res = client.get('/backup/xlsx')
check(res.status_code == 200, 'バックアップのExcelを取れる')
wb = load_workbook(io.BytesIO(res.data))
check({'予約全件', '顧客集計', 'POS売上明細'} <= set(wb.sheetnames), '予約のシートは今まで通り')
index = {r[1]: (r[0], r[2]) for r in wb['正肉・価格表の目次'].iter_rows(min_row=2, values_only=True)}
all_tables = {t for t in inspect(engine).get_table_names() if t.startswith(('meat_', 'price_'))}
check(set(index) == all_tables - {'meat_photos'}, f'写真以外の表が全部入る（{len(index)}表）')
check('meat_photos' not in index and not any('WFhY' in str(c) for ws in wb for row in ws.iter_rows(values_only=True) for c in row),
      '写真は入らない')
with engine.begin() as con:
    ok = all(con.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar() == n and wb[s].max_row - 1 == n for t, (s, n) in index.items())
check(ok, '件数が元の表と一致する')
rows = list(wb['価格表_履歴'].iter_rows(values_only=True))
check(11800 in [r[rows[0].index('unit_price')] for r in rows[1:]], '価格の履歴が入る')
rows = list(wb['正肉_塊'].iter_rows(values_only=True))
row = next(r for r in rows[1:] if r[0] == lot)
check(row[rows[0].index('promised_unit_price')] == 5000 and row[rows[0].index('memo')] == '制御文字', '塊の単価が入り、制御文字があっても壊れない')
check('価格表_品目' in wb.sheetnames and '正肉_締めの解除' in wb.sheetnames, '日本語のシート名になる')

engine.dispose()
try:
    os.unlink(_tmp.name)
except OSError:
    pass
print('\n' + ('すべて成功' if not failures else f'{failures} 件失敗'))
sys.exit(1 if failures else 0)
