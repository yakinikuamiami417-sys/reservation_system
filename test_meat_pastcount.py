# test_meat_pastcount.py — 10月末の棚卸を、11月に入ってから入れる（さかのぼり）の確認（python test_meat_pastcount.py）
# 一時ファイルのSQLiteで動かすので、本番・手元の reservations.db には触れない
import os, sys, tempfile

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['DATABASE_URL'] = 'sqlite:///' + _tmp.name.replace('\\', '/')
os.environ.pop('APP_PASSWORD', None)
os.environ.pop('MEAT_ADMIN_PASSWORD', None)
os.environ['NLBC_AUTO_LOOKUP'] = '0'

import meat_db as mdb
from database import engine
from app import app

failures = 0


def check(cond, msg):
    global failures
    print(('OK   ' if cond else 'FAIL ') + msg)
    if not cond:
        failures += 1


client = app.test_client()
P = {p['name']: p['id'] for p in mdb.list_parts()}
sup = mdb.get_or_create_supplier('フード')


def lot(part, kg):
    return mdb.create_held_lot({'part_id': P[part], 'supplier_id': sup, 'label_kg': str(kg), 'trade_kg': str(kg),
                                'unit_price': '4200', 'promised_on': '2026-10-01'}, '社長')


A, B, D, E = lot('ウデ', 16.0), lot('モモ', 12.0), lot('クラシタ', 20.0), lot('トモバラ', 40.0)
mdb.receive_lots(sup, 'A-1', '2026-10-03', 0, [A, B, D, E], '社長')
C = lot('ウデ', 18.0)
mdb.receive_lots(sup, 'A-2', '2026-11-02', 0, [C], '社長')
# 11月に入ってからの動き（10月末の棚卸はまだ入れていない）
mdb.weigh_lot(A, 10.0, '社長', '11月に量った')
mdb.consume_lot(B, '社長', '11月に使い切り')
check(mdb.get_lot(A)['current_kg'] == 10.0 and mdb.get_lot(B)['status'] == 'consumed', '（前提）A は11月に10kg、B は11月に使い切り')

# ---- さかのぼって開ける ----
h = client.get('/meat/count/2026-11').get_data(as_text=True)
check('‹ 前の月' in h and '10月末' in h and '11月末' in h, '11月の棚卸から「前の月」「10月末」で戻れる')
h = client.get('/meat/count/2026-10').get_data(as_text=True)
check('2026年10月' in h and 'あとから入れています' in h, '10月末の棚卸が開き、あとから入れている案内が出る')
ids = {l['id'] for l in mdb.lots_in_house_for_count('2026-10')}
check({A, B, D, E} <= ids and C not in ids, '10月末の一覧：10月までに納品した塊（11月に使い切った B も含む）、11/2 納品の C は出ない')
check('に使い切り' in h, '11月に使い切った塊には「〇/〇に使い切り」と出る')
check(C in {l['id'] for l in mdb.lots_in_house_for_count('2026-11')} and B not in {l['id'] for l in mdb.lots_in_house_for_count('2026-11')},
      '11月の一覧：C は出る。11月に使い切った B は11月末には無いので出ない')
h = client.get('/meat/count/2026-10-26').get_data(as_text=True)
check('‹ 前の週' in h and '次の週 ›' in h, '週次も前の週・次の週へ移れる')

# ---- 空欄は0kgにしない ----
form = {'action': 'confirm', 'extra_count': '0', f'lot_{A}': '15.0', f'lot_{B}': '', f'lot_{D}': '20.0', f'lot_{E}': '38.0'}
client.post('/meat/count/2026-10', data=form)
check(mdb.get_count('2026-10')['status'] == 'draft', '11月に使い切った塊の重さが空欄なら、確定しない（0kgとして数えない）')

# ---- 確定 ----
form[f'lot_{B}'] = '5.0'
del form[f'lot_{E}']
form[f'lot_{E}_missing'] = '1'
r = client.post('/meat/count/2026-10', data=form)
head = mdb.get_count('2026-10')
check(head['status'] == 'confirmed', '10月末の棚卸を確定できる')
kg = {ln['lot_id']: ln['kg'] for ln in head['lines']}
check(kg.get(A) == 15.0 and kg.get(B) == 5.0 and kg.get(D) == 20.0 and E not in kg,
      f'10月末の棚卸の数字：A 15kg・B 5kg・D 20kg（E は無い）: {kg}')
check(mdb.get_lot(A)['current_kg'] == 10.0, '11月に量った A は、今の重さ 10kg のまま（10月の 15kg で上書きしない）')
check(mdb.get_lot(B)['status'] == 'consumed', '11月に使い切った B は使い切りのまま')
check(mdb.get_lot(D)['current_kg'] == 20.0, '11月に動きの無い D は、量った重さが今の重さになる')
check(mdb.get_lot(E)['status'] == 'consumed', '「無い」にした E は使い切り')
r = mdb.compute_closing('2026-10')
row = {x['part_name']: x for x in r['rows']}
check(abs(row['ウデモモ']['closing_kg'] - 20.0) < 1e-9, f'10月の原価：ウデモモの棚卸 15＋5＝20kg: {row["ウデモモ"]["closing_kg"]}')
check(row['ウデモモ']['purchase_kg'] == 28.0, '10月の仕入に 11/2 の C（18kg）は入らない')
nov = {x['part_name']: x for x in mdb.compute_closing('2026-11')['rows']}
check(abs(nov['ウデモモ']['opening_kg'] - 20.0) < 1e-9 and nov['ウデモモ']['purchase_kg'] == 18.0,
      '11月の原価：前繰＝10月末の 20kg、仕入＝11/2 の 18kg')

# ---- 予約機能 ----
check(client.get('/').status_code == 200 and client.get('/timetable').status_code == 200, '予約一覧・タイムテーブルが開く')

engine.dispose()
try:
    os.unlink(_tmp.name)
except OSError:
    pass
print('\n' + ('すべて成功' if not failures else f'{failures} 件失敗'))
sys.exit(1 if failures else 0)
