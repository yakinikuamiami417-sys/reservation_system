# test_meat_split.py — 「ウデモモ」を ウデ・モモ・部位未分類 に分ける変更の確認（python test_meat_split.py）
# 分ける前の形のデータ（10月末の棚卸・締め、11/2 の仕入）を作ってから分け、数量・金額が動かないことを確かめる。
# 一時ファイルのSQLiteで動かすので、本番・手元の reservations.db には触れない
import os, sys, tempfile

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['DATABASE_URL'] = 'sqlite:///' + _tmp.name.replace('\\', '/')
os.environ.pop('APP_PASSWORD', None)
os.environ.pop('MEAT_ADMIN_PASSWORD', None)
os.environ['NLBC_AUTO_LOOKUP'] = '0'

from sqlalchemy import text
import meat_db as mdb
from database import engine

failures = 0


def check(cond, msg):
    global failures
    print(('OK   ' if cond else 'FAIL ') + msg)
    if not cond:
        failures += 1


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    return False


def q(sql, **kw):
    with engine.begin() as con:
        return con.execute(text(sql), kw).all()


# ---- 分ける前の形（今の本番と同じ）を作る ----
real_split = mdb._split_udemomo
mdb._split_udemomo = lambda: None
mdb.init_meat_db()
mdb._split_udemomo = real_split
parts = {p['name']: p['id'] for p in mdb.list_parts()}
check('ウデモモ' in parts and 'ウデ' not in parts, '（前提）分ける前は「ウデモモ」1つ')
UM, TB = parts['ウデモモ'], parts['トモバラ']
sup = mdb.get_or_create_supplier('フード')


def lot(part, kg, price, promised='2026-10-01', sub=''):
    return mdb.create_held_lot({'part_id': part, 'supplier_id': sup, 'label_kg': str(kg), 'trade_kg': str(kg),
                                'unit_price': str(price), 'promised_on': promised, 'label_item_name': sub}, '社長')


L1 = lot(UM, 16.4, 4200, sub='シンタマ')   # 名前はモモらしいが、名前だけで振り分けてはいけない
L2 = lot(UM, 16.7, 4200, sub='ウデ')
L3 = lot(TB, 40.8, 2100)
mdb.receive_lots(sup, 'A-1', '2026-10-03', 0, [L1, L2, L3], '社長')
mdb.process_lot(L2, 16.7, 13.0, 2.0, 1.7, '社長')
# 10月末の棚卸（確定）。L2 は捌いた肉をまとめて量った分（一覧に無い分）として 5.0kg
mdb.save_count('2026-10', [
    {'part_id': UM, 'lot_id': L1, 'kg': 16.4, 'state': 'raw'},
    {'part_id': UM, 'lot_id': None, 'kg': 5.0, 'state': 'product'},
    {'part_id': TB, 'lot_id': L3, 'kg': 30.0, 'state': 'raw'},
], '社長', confirm=True)
mdb.save_menu_sales('2026-10', [('麒麟の赤身肉タレ', 100, 160000), ('上カルビタレ', 50, 90000)])
mdb.confirm_closing('2026-10', '社長')
# 11/2 の仕入（10月末の棚卸とは別の日付の記録）
L4 = lot(UM, 20.0, 4300, promised='2026-10-30')
mdb.receive_lots(sup, 'A-2', '2026-11-02', 0, [L4], '社長')
mdb.save_menu_sales('2026-11', [('麒麟の赤身肉タレ', 40, 64000)])

before = {
    'lots': q('SELECT id, part_id, status, label_kg, promised_amount, received_on, current_kg FROM meat_lots ORDER BY id'),
    'lines': q('SELECT id, count_id, part_id, lot_id, kg, unit_price, amount FROM meat_stock_count_lines ORDER BY id'),
    'closings': q('SELECT * FROM meat_period_closings ORDER BY period, part_id'),
    'oct': {r['part_name']: r for r in mdb.compute_closing('2026-10')['rows']},
    'nov': {r['part_name']: r for r in mdb.compute_closing('2026-11')['rows']},
    'counts': {t: q(f'SELECT COUNT(*) FROM {t}')[0][0] for t in mdb.SPLIT_BACKUP_TABLES},
}

# ---- 分ける ----
mdb.init_meat_db()
parts = {p['name']: p for p in mdb.list_parts()}
UDE, MOMO, UNC = parts['ウデ']['id'], parts['モモ']['id'], parts['ウデモモ（部位未分類）']['id']
check(UNC == UM, '前からの「ウデモモ」は番号そのままで「ウデモモ（部位未分類）」になる')
check(all(parts[n]['cost_group'] == 'ウデモモ' for n in ('ウデ', 'モモ', 'ウデモモ（部位未分類）')), '3つとも集計分類は「ウデモモ」')
check([r for r in q('SELECT name FROM meat_parts')].count(('ウデ',)) == 1, 'ウデは1つだけ')
mdb.init_meat_db()
check(len(mdb.list_parts()) == len(parts), 'もう一度初期化しても部位は増えない（1回だけ分ける）')

# 控えの表
for t in mdb.SPLIT_BACKUP_TABLES:
    n = q(f'SELECT COUNT(*) FROM {mdb.SPLIT_BACKUP_PREFIX}{t}')[0][0]
    check(n == before['counts'][t], f'控え {mdb.SPLIT_BACKUP_PREFIX}{t}：{n}件（分ける前と同じ件数）')
check(q(f"SELECT name FROM {mdb.SPLIT_BACKUP_PREFIX}meat_parts WHERE id = :i", i=UM)[0][0] == 'ウデモモ',
      '控えには分ける前の名前「ウデモモ」が残る（元に戻せる）')

# 既存の記録は1件も動かない
check(q('SELECT id, part_id, status, label_kg, promised_amount, received_on, current_kg FROM meat_lots ORDER BY id') == before['lots'],
      '塊の記録（部位・数量・金額・日付）は1件も変わらない')
check(q('SELECT id, count_id, part_id, lot_id, kg, unit_price, amount FROM meat_stock_count_lines ORDER BY id') == before['lines'],
      '棚卸の記録は1件も変わらない')
check(q('SELECT * FROM meat_period_closings ORDER BY period, part_id') == before['closings'], '10月の締めは変わらない')
check(mdb.get_lot(L1)['part_name'] == 'ウデモモ（部位未分類）', '名前がモモらしくても、自動では振り分けない（部位未分類）')

oct_ = {r['part_name']: r for r in mdb.compute_closing('2026-10')['rows']}
nov = {r['part_name']: r for r in mdb.compute_closing('2026-11')['rows']}
keys = ('opening_amount', 'purchase_amount', 'closing_amount', 'usage_amount', 'opening_kg', 'purchase_kg',
        'closing_kg', 'usage_kg', 'sales_qty', 'cost_per_sale')
check(all(oct_['ウデモモ'][k] == before['oct']['ウデモモ'][k] for k in keys), '10月（締め済み）のウデモモ原価は分ける前と同じ')
check(all(nov['ウデモモ'][k] == before['nov']['ウデモモ'][k] for k in keys), '11月のウデモモ原価は分ける前と同じ')
check(nov['ウデモモ']['opening_kg'] == 21.4 and nov['ウデモモ']['purchase_kg'] == 20.0,
      '11月：前繰＝10月末の棚卸 21.4kg、仕入＝11/2 の 20.0kg（別の日付として扱う）')
check(oct_['ウデモモ']['purchase_kg'] == 16.4 + 16.7, '10月の仕入に11/2の分は入らない')
check(not any(n in nov for n in ('ウデ', 'モモ', 'ウデモモ（部位未分類）')), '原価はウデモモ1行（ウデ・モモ別の行は作らない）')
check(nov['ウデモモ']['sales_qty'] == 40, f'出数はウデモモに1回だけ（ウデとモモに二重に足さない）: {nov["ウデモモ"]["sales_qty"]}')

# 棚卸の評価単価：3つとも同じ（ウデモモの最後の仕入単価）
vp = mdb.valuation_prices('2026-11')
check(vp[UNC] == 4300, f'部位未分類の評価単価＝自分の最後の仕入単価 4,300円（11/2）: {vp[UNC]}')
check(vp[UDE] == vp[MOMO] == 4300, 'ウデ・モモに仕入の記録がまだ無い時は、ウデモモの最後の仕入単価（0円にしない）')
check(mdb.valuation_prices('2026-10')[UNC] == 4200, '10月末の評価単価は10月までの仕入単価 4,200円（11/2 の分は入れない）')
check(parts['ウデ']['min_stock_kg'] == 0 and parts['モモ']['min_stock_kg'] == 0 and
      parts['ウデモモ（部位未分類）']['min_stock_kg'] == 0, '下限は部位ごとに 0kg から')

# ---- ① ウデの入庫ではウデだけが増える ----
s0 = mdb.stock_summary()
p0 = {p['name']: p['total_kg'] for p in s0['parts']}
L5 = lot(UDE, 12.0, 4400, promised='2026-11-04')
mdb.receive_lots(sup, 'A-3', '2026-11-05', 0, [L5], '社長')
s1 = mdb.stock_summary()
p1 = {p['name']: p['total_kg'] for p in s1['parts']}
check(abs(p1['ウデ'] - p0['ウデ'] - 12.0) < 1e-9 and p1['モモ'] == p0['モモ'] and p1['ウデモモ（部位未分類）'] == p0['ウデモモ（部位未分類）'],
      f'ウデを12kg納品：ウデだけ増える（ウデ {p0["ウデ"]}→{p1["ウデ"]}、モモ {p1["モモ"]} のまま）')
pur = mdb.purchases_in_period('2026-11')
check(pur.get(UDE, (0, 0))[0] == 12.0 and MOMO not in pur, '11月の仕入：ウデ 12kg、モモは無し')
vp = mdb.valuation_prices('2026-11')
check(vp[UDE] == 4400 and vp[UNC] == 4300, f'ウデを仕入れたら、ウデだけ自分の単価 4,400円（未分類は 4,300円のまま）: {vp[UDE]}, {vp[UNC]}')

# ---- ② ウデ・モモ・未分類の合計＝ウデモモ ----
g = {x['name']: x for x in s1['groups']}['ウデモモ']
check(abs(g['total_kg'] - (p1['ウデ'] + p1['モモ'] + p1['ウデモモ（部位未分類）'])) < 1e-9, f'在庫：3つの合計＝ウデモモ合計 {g["total_kg"]:.2f}kg')
nov = {r['part_name']: r for r in mdb.compute_closing('2026-11')['rows']}
u = nov['ウデモモ']
for k in ('opening', 'purchase', 'closing'):
    check(abs(sum(b[k + '_kg'] for b in u['breakdown']) - u[k + '_kg']) < 1e-9 and
          sum(b[k + '_amount'] for b in u['breakdown']) == u[k + '_amount'], f'原価：内訳の合計＝ウデモモ（{k}）')

# ---- ③ 未分類を分類しても総数量・総原価は変わらない ----
tot_before = (u['opening_kg'], u['opening_amount'], u['purchase_kg'], u['purchase_amount'], u['usage_amount'])
stock_before = g['total_kg'], g['value']
check(raises(ValueError, lambda: mdb.reclassify_lot(L1, TB, '社長')), 'ウデモモ以外（トモバラ）には付け替えられない')
mdb.reclassify_lot(L1, MOMO, '社長', 'ラベルがシンタマ')
mdb.reclassify_lot(L4, UDE, '社長')
nov2 = {r['part_name']: r for r in mdb.compute_closing('2026-11')['rows']}['ウデモモ']
check((nov2['opening_kg'], nov2['opening_amount'], nov2['purchase_kg'], nov2['purchase_amount'], nov2['usage_amount']) == tot_before,
      '分類後もウデモモの 前繰・仕入・使用分（数量・金額）は同じ')
b = {x['part_name']: x for x in nov2['breakdown']}
check(b['モモ']['opening_kg'] == 16.4 and b['ウデモモ（部位未分類）']['opening_kg'] == 5.0,
      '内訳：10月末の L1 16.4kg がモモへ、まとめて量った 5.0kg は部位未分類のまま')
g2 = {x['name']: x for x in mdb.stock_summary()['groups']}['ウデモモ']
check((g2['total_kg'], g2['value']) == stock_before, '分類後も在庫の合計（kg・塊の金額）は同じ')
vp2 = mdb.valuation_prices('2026-11')
check(vp2[UDE] == 4400 and vp2[MOMO] == 4200,
      f'分類後の評価単価は部位ごと（モモ＝L1の4,200円、ウデ＝最後の4,400円）: ウデ {vp2[UDE]}、モモ {vp2[MOMO]}')
lines = q('SELECT kg, unit_price, amount FROM meat_stock_count_lines WHERE lot_id = :l', l=L1)
check(lines == [(16.4, 4200, 68880)], '10月末の棚卸の数量・単価・金額は残る（部位だけ付け替え）')
ev = mdb.get_lot(L1)['events'][-1]
check(ev['event_type'] == 'reclassify' and 'ウデモモ（部位未分類） → モモ' in ev['memo'], '分類した記録が履歴に残る')
oct2 = {r['part_name']: r for r in mdb.compute_closing('2026-10')['rows']}['ウデモモ']
check(all(oct2[k] == before['oct']['ウデモモ'][k] for k in keys), '締め済みの10月は分類しても変わらない')

# ---- ④ 保存後に開き直しても残る ----
engine.dispose()
check(mdb.get_lot(L1)['part_name'] == 'モモ' and mdb.get_lot(L4)['part_name'] == 'ウデ', '開き直しても分類が残る')

# ---- 画面 ----
from app import app
client = app.test_client()
h = client.get('/meat/new').get_data(as_text=True)
check('>ウデ　（ウデモモ）<' in h and '>モモ　（ウデモモ）<' in h and '部位未分類' not in h,
      '目利きの入力：ウデ・モモを選べ、部位未分類は選ばせない')
h = client.get(f'/meat/lot/{L2}').get_data(as_text=True)
check('部位を分類する' in h and '>\n      ウデ</button>' in h.replace('\r', ''), '未分類の塊の画面に「ウデ」「モモ」の分類ボタン')
r = client.post(f'/meat/lot/{L2}/reclassify', data={'part_id': UDE})
check(r.status_code == 302 and mdb.get_lot(L2)['part_name'] == 'ウデ', '画面から分類できる')
h = client.get('/meat/').get_data(as_text=True)
check('ウデモモ（合計）' in h, '在庫一覧にウデモモの合計')
h = client.get('/meat/closing/2026-11').get_data(as_text=True)
check('└ ウデ' in h and '└ モモ' in h, '原価の画面にウデ・モモの内訳')

# ---- 未入力を在庫ゼロとして扱わない ----
form = {'action': 'confirm', 'extra_count': '0'}
for l in mdb.lots_in_house_for_count('2026-11'):
    form[f'lot_{l["id"]}'] = str(round(l['kg_now'], 2))
form[f'lot_{L5}'] = ''   # 未開封のウデの重さを消してしまった
r = client.post('/meat/count/2026-11', data=form)
head = mdb.get_count('2026-11')
check(head and head['status'] == 'draft', '未開封の箱の重さが空欄なら、棚卸を確定しない')
check(mdb.get_lot(L5)['status'] == 'received', '空欄の箱を使い切り（0kg）にしない')

# ---- 予約機能 ----
import database as db
rid = db.save_reservation({'name': '確認', 'phone': '0900000000', 'date': '2026-11-06', 'time_slot': '18:00',
                           'adults': 2, 'total_people': 2, 'children_info': [], 'duration_minutes': 120,
                           'private_room': 0, 'is_vip': 0, 'is_group': 0, 'assigned_tables': '[1]', 'status': 'reserved'})
check(any(x['id'] == rid for x in db.get_reservations_by_date('2026-11-06')), '予約の登録はいつもどおり')
check(client.get('/?date=2026-11-06').status_code == 200 and client.get('/timetable?date=2026-11-06').status_code == 200,
      '予約一覧・タイムテーブルが開く')

engine.dispose()
try:
    os.unlink(_tmp.name)
except OSError:
    pass
print('\n' + ('すべて成功' if not failures else f'{failures} 件失敗'))
sys.exit(1 if failures else 0)
