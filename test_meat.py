# test_meat.py — 正肉在庫の計算と画面の確認（python test_meat.py）
# 一時ファイルのSQLiteで動かすので、本番・手元の reservations.db には触れない
import os, sys, tempfile
from datetime import date

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['DATABASE_URL'] = 'sqlite:///' + _tmp.name.replace('\\', '/')
os.environ.pop('APP_PASSWORD', None)
os.environ.pop('MEAT_ADMIN_PASSWORD', None)
os.environ.pop('ANTHROPIC_API_KEY', None)
os.environ['NLBC_AUTO_LOOKUP'] = '0'   # テストでは家畜改良センターに照会しない

import label_reader as lr
import cattle_lookup as cl
import meat_db as mdb
from app import app

failures = 0


def check(cond, msg):
    global failures
    print(('OK   ' if cond else 'FAIL ') + msg)
    if not cond:
        failures += 1


# ---- ラベル読み取りの補助（実物ラベルの値で確認） ----
ais = lr.parse_gs1(['(01)94547913161002 (3102)001114 (11)260911 (21)0000884021',
                    '(251)1680941492(7002)00001342111410'])
g = lr.gs1_values(ais)
check(g == {'label_kg': 11.14, 'processed_date': '2026-09-11', 'individual_no': '1680941492'},
      f'GS1 の読み解き（一ノ瀬畜産のラベル）: {g}')

piece = {'label_item_name': 'シンタマ', 'label_kg': '12.68', 'trade_kg': '12.6', 'individual_no': '1680941492',
         'number_type': '個体識別番号', 'best_before': '2026-10-25', 'processed_date': '2026-09-11',
         'barcode_texts': ['(251)1680941492', '(3102)001268(11)260911'], 'unreadable': []}
check(lr.check_piece(piece, date(2026, 10, 20)) == {}, '正しいラベルは警告なし')
bad = dict(piece, trade_kg='12.7', individual_no='168094149', best_before='2026-09-11')
w = lr.check_piece(bad, date(2026, 10, 20))
check({'trade_kg', 'individual_no', 'best_before'} <= set(w), f'取引値・番号・期限の誤りを警告: {sorted(w)}')
check('label_kg' in lr.check_piece(dict(piece, label_kg='12.86'), date(2026, 10, 20)),
      'バーコードの重量と食い違えば警告')
filled = lr.fill_from_gs1({'individual_no': '', 'barcode_texts': ['(251)1463224934(7002)0'],
                           'unreadable': ['individual_no']})
check(filled['individual_no'] == '1463224934' and not filled['unreadable'],
      '紙の破れで読めない個体識別番号をバーコードから補う（みえミートの写真6）')
check(lr.check_piece(dict(piece, number_type='荷口番号')).get('individual_no', '').startswith('荷口番号'),
      '荷口番号は照会できない旨を表示')
try:
    lr.read_label('xxx')
    check(False, 'APIキー未設定ならエラー')
except lr.LabelReadError as e:
    check('未設定' in str(e), 'APIキー未設定なら手入力を案内する')

check(cl.normalize_individual_no('１６８０９４－１４９２') == '1680941492', '個体識別番号の全角・ハイフンを除く')
check(cl.months_between(date(2023, 12, 20), date(2026, 8, 19)) == 31, '月齢（満）の計算')

# ---- 家畜改良センターの検索結果（2026-10-28 に実際に取得したページ。氏名は架空に置換） ----
sample = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_data', 'nlbc_result_sample.html'),
              encoding='utf-8').read()
info = cl.parse_result(sample, '1680941492')
check(info and info['birth_date'] == '2024-04-06' and info['sex'] == 'メス' and info['breed'] == '黒毛和種'
      and info['mother_no'] == '1377136347', '個体情報：出生日・雌雄・品種・母牛')
check(info['slaughter_date'] == '2026-09-07' and info['age_months'] == 29, f'と畜日と月齢 {info["age_months"]}か月')
check(info['producer_name'] == '株式会社 テスト肥育牧場' and info['producer_place'] == '佐賀県唐津市',
      '生産者＝と畜前に最後に転入した牧場')
check(info['breeder_name'] == '繁殖 太郎' and info['breeder_place'] == '鹿児島県出水郡長島町', '出生地（繁殖）')
check(len(info['moves']) == 8, '異動履歴8件')
check(cl.parse_result(sample, '1399098647') is None, '別の番号の結果は採用しない')
check(cl.parse_result('<html>エラー</html>') is None, 'エラー画面は None')

# ---- 品名の読み替え ----
mdb.init_meat_db()
parts = {p['name']: p['id'] for p in mdb.list_parts()}
# 2026-11-03 ウデとモモを分けた。前からの「ウデモモ」の記録は「ウデモモ（部位未分類）」に残る
parts['ウデモモ'] = parts['ウデモモ（部位未分類）']
check(mdb.resolve_alias('和牛　芯玉')[0] == parts['モモ'], '「和牛 芯玉」→ モモ（目利きの初期値）')
check(mdb.resolve_alias('ミスジ')[0] == parts['ウデ'], '「ミスジ」→ ウデ（目利きの初期値）')
check(mdb.resolve_alias('牛中バラ')[0] == parts['トモバラ'], '「牛中バラ」→ トモバラ')
check(mdb.resolve_alias('牛カッパ') == (parts['トモバラ'], 'カッパ'), '「牛カッパ」→ トモバラ（カッパ）')
check(mdb.resolve_alias('ネクタイ') == (None, None), '知らない品名は部位なし')
mdb.learn_alias('ネクタイ', '東和食品', parts['クラシタ'])
check(mdb.resolve_alias('ネクタイ', '東和食品')[0] == parts['クラシタ'], '人が選んだ部位を覚える')
check(len(mdb.list_menu_map()) == 93, 'グラム表（93行）を初期データに取り込み')
check(mdb.match_menu('特選カルビ（ヘッドバラ）タレ', ['特選カルビ（ヘッドバラ）', 'ロース']) == ('特選カルビ（ヘッドバラ）', 1),
      'マジレジの「タレ」付き商品名をメニューに突き合わせ')
check(mdb.match_menu('特選ロース', ['特選ロース', 'ロース']) == ('特選ロース', 1), '最長一致（ロース と 特選ロース）')
check(mdb.match_menu('【H】特選ヘッドバラ(タレ)', ['特選カルビ（ヘッドバラ）']) == ('特選カルビ（ヘッドバラ）', 0.5),
      '【H】特選ヘッドバラ → 特選カルビ（ヘッドバラ）の0.5食')
check(mdb.match_menu('【H】上ロース(塩)', ['上ロース', 'ロース']) == ('上ロース', 0.5), '【H】上ロース(塩) → 上ロースの0.5食')
check(mdb.match_menu('黄金和牛（ドリンクなし）', ['上ロース']) == (None, 0), 'グラム表に無いメニューは結び付けない')

# ---- マジレジの出数ファイル（実物と同じ形） ----
import io as _io, openpyxl as _ox
from meat import parse_sales_file
_wb = _ox.Workbook(); _ws = _wb.active; _ws.title = '出数データ'
for r in [['メニュー別累計'], ['日付:20260901-20260930'], ['品番', 'メニュー名', '売価', '売上数量', '金額'],
          [13, '上カルビ　タレ', 1800, 45, 81000], [15, '【H】上カルビ(タレ)', 980, 5, 4900]]:
    _ws.append(r)
_buf = _io.BytesIO(); _wb.save(_buf)
check(parse_sales_file(_buf.getvalue(), '2026.09.xlsx') == [('上カルビ　タレ', 45, 81000), ('【H】上カルビ(タレ)', 5, 4900)],
      'Dropbox の月毎集計 Excel（出数データ シート）を読む')
check(parse_sales_file('1,特選カルビ（ヘッドバラ）タレ,2650,29,76850\n2,ロース　塩,1750,3,5250\n'.encode('cp932'))
      == [('特選カルビ（ヘッドバラ）タレ', 29, 76850), ('ロース　塩', 3, 5250)], '見出しの無いマジレジCSV（cp932）を読む')

# ---- 流れ全体：目利き → 納品 → 捌き → 棚卸 → 原価 ----
sup = mdb.get_or_create_supplier('フード')
a = mdb.create_held_lot({'part_id': parts['ウデモモ'], 'supplier_id': sup, 'label_kg': '11.14', 'trade_kg': '11.1',
                         'unit_price': '4200', 'individual_no': '1680941492', 'label_item_name': 'シンタマ',
                         'best_before': '2026-10-25', 'promised_on': '2026-09-20'}, '社長')
b = mdb.create_held_lot({'part_id': parts['ウデモモ'], 'supplier_id': sup, 'label_kg': '12.68', 'trade_kg': '12.6',
                         'unit_price': '4200', 'promised_on': '2026-09-20'}, '社長')
c = mdb.create_held_lot({'part_id': parts['トモバラ'], 'supplier_id': sup, 'label_kg': '21.84', 'trade_kg': '21.8',
                         'unit_price': '2100', 'promised_on': '2026-09-20'}, '社長')
check(mdb.get_lot(a)['promised_amount'] == 46620, '金額＝取引値11.1×4,200＝46,620円')
s = mdb.stock_summary()
check(s['total_kg'] == 0 and len(s['held']) == 3, '業者預けは店内在庫に入らない')

slip, lines_total = mdb.receive_lots(sup, 'A-100', '2026-10-02', 46620 + 52920 + 45780, [a, b, c], '社長')
check(lines_total == 46620 + 52920 + 45780, f'納品：明細合計 {lines_total:,}円')
s = mdb.stock_summary()
check(abs(s['total_kg'] - (11.14 + 12.68 + 21.84)) < 1e-6, '納品後は店内在庫（実重量）に入る')

mdb.process_lot(a, 11.14, 8.7, 1.2, 1.24, '社長')
lot = mdb.get_lot(a)
check(lot['status'] == 'processed' and lot['current_kg'] == 8.7, '捌き：商品化済み・重量8.7kg')

# 前月（9月）は棚卸なし＝前繰0。10月末の棚卸
mdb.save_count('2026-10', [
    {'part_id': parts['ウデモモ'], 'lot_id': a, 'kg': 2.0, 'state': 'product'},
    {'part_id': parts['トモバラ'], 'lot_id': c, 'kg': 5.0, 'state': 'raw'},
], '社長', confirm=True, missing_lot_ids=[b])
check(mdb.get_lot(b)['status'] == 'consumed', '棚卸で「無い」にした塊は使い切り')
mdb.save_menu_sales('2026-10', [('麒麟の赤身肉タレ', 100, 160000), ('麒麟の上赤身塩', 50, 85000),
                                ('上カルビタレ', 120, 216000), ('謎の新メニュー', 3, 3000)])
r = mdb.compute_closing('2026-10')
row = {x['part_name']: x for x in r['rows']}
u = row['ウデモモ']
# 仕入 46,620＋52,920＝99,540、棚卸 2.0kg×4,200＝8,400 → 使用分 91,140、出数 150 → 608円
check(u['purchase_amount'] == 99540 and u['closing_amount'] == 8400 and u['usage_amount'] == 91140,
      f'ウデモモ 前繰0＋仕入99,540－棚卸8,400＝使用分 {u["usage_amount"]:,}円')
check(u['sales_qty'] == 150 and u['cost_per_sale'] == 608, f'ウデモモ 原価 {u["cost_per_sale"]}円／出数（91,140÷150）')
check(abs(u['theoretical_kg'] - 13.5) < 1e-9, '理論使用量 150食×90g＝13.5kg')
check(abs(u['usage_kg'] - (11.14 + 12.68 - 2.0)) < 1e-9 and u['loss_rate'] > 0, f'ロス率 {u["loss_rate"]:.1%}')
check([x['menu_name'] for x in r['unmatched']] == ['謎の新メニュー'], 'グラム表に無いメニューを警告')
akami = next(m for m in r['menus'] if m['menu_name'] == '麒麟の赤身肉')
check(round(akami['cost']) == 608, f'麒麟の赤身肉 1人前 {akami["cost"]:.0f}円')
mdb.confirm_closing('2026-10', '社長')
check(mdb.compute_closing('2026-10')['status']['closed'], '締めると確定済みになる')
try:
    mdb.save_count('2026-10', [], '社長')
    check(False, '確定済みの棚卸は変えられない')
except ValueError:
    check(True, '確定済みの棚卸は変えられない')
check(mdb.compute_closing('2026-11')['rows'][0]['opening_amount'] in (8400, 10500),
      '翌月の前繰＝10月の確定棚卸')

# ---- 週次決算（毎週月曜の閉店後に棚卸。火〜月の1週間） ----
check(mdb.period_bounds('2026-11-02') == ('2026-10-27', '2026-11-03'), '11/2(月)の週＝10/27(火)〜11/2(月)')
check(mdb.week_end(date(2026, 10, 29)) == date(2026, 11, 2) and mdb.week_end(date(2026, 11, 2)) == date(2026, 11, 2),
      '木曜→次の月曜、月曜→その日')
check(mdb.prev_period('2026-11-02') == '2026-10-26' and mdb.next_period('2026-11-02') == '2026-11-09', '前後の週')
check(mdb.period_label('2026-11-02') == '10/27(火)〜11/2(月)の週', f'週の名前 {mdb.period_label("2026-11-02")}')
check(mdb._same_day_period('2026-11-30') == '2026-11' and mdb._same_day_period('2026-11') == '2026-11-30'
      and mdb._same_day_period('2026-11-09') is None, '11/30は月曜で月末：週次と月次で同じ棚卸を使う')
weekly = {p['name']: p['count_weekly'] for p in mdb.list_parts()}
check(weekly['ウデ'] == 1 and weekly['モモ'] == 1 and weekly['ウデモモ（部位未分類）'] == 1 and weekly['ホルモン'] == 0 and weekly['トリ'] == 0, '毎週量る部位と月末だけの部位')

vp = mdb.valuation_prices('2026-10')
check(vp[parts['ミノ']] == 3700 and vp[parts['ウデモモ']] == 4200 and vp[parts['テール']] == 2000,
      '仕入の記録が無い部位は在庫アプリの単価で評価（テールは2,000円）')

# 11/30(月)の週の棚卸を1回すれば、11月の月次の期末にもなる
mdb.save_count('2026-11-30', [{'part_id': parts['トモバラ'], 'lot_id': None, 'kg': 1.0, 'state': 'raw'}], '社長',
               confirm=True)
check(mdb._count_totals('2026-11') == {parts['トモバラ']: (1.0, 2100)}, '月末の月曜の棚卸が月次にも使われる')
wk = mdb.compute_closing('2026-11-30')
check(all(r['part_name'] not in ('ホルモン', 'トリ') for r in wk['rows']), '週次の原価には月末だけの部位を出さない')

# ---- 画面（テンプレートが壊れていないか） ----
client = app.test_client()
for path in ['/meat/', '/meat/?tab=held', f'/meat/lot/{a}', f'/meat/lot/{c}', '/meat/new', '/meat/receive',
             '/meat/count/2026-10', '/meat/count/2026-11', '/meat/closing/2026-10', '/meat/closing/2026-11',
             '/meat/count/2026-11-09', '/meat/closing/2026-11-30', '/meat/closing/2026-11-09',
             '/meat/menus', '/meat/unlock']:
    res = client.get(path)
    check(res.status_code == 200, f'GET {path} → {res.status_code}')
check(client.get('/meat/count/2026-13').status_code == 404, 'あり得ない期間は404')
page = client.get('/meat/count/2026-11-09').get_data(as_text=True)
check('ホルモン は月末' not in page and '月末の棚卸だけで量ります' in page, '週次の棚卸は月末だけの部位を案内')

# 開けた塊を量り忘れたまま確定しようとすると止める
lot_open = mdb.get_lot(a)
check(lot_open['status'] == 'processed', '（前提）ウデモモ #a は開封済み')
res = client.post('/meat/count/2026-11-09', data={'action': 'confirm', f'lot_{a}': '', 'extra_count': '0'})
check(res.status_code == 302 and mdb.get_count('2026-11-09')['status'] == 'draft', '開けた塊の重さが無いと確定しない')
res = client.post('/meat/count/2026-11-09', data={'action': 'confirm', f'lot_{a}': '1.5', 'extra_count': '0'})
check(mdb.get_count('2026-11-09')['status'] == 'confirmed', '量って入れれば確定できる')
res = client.post('/meat/api/read-label', json={'image': 'data:image/jpeg;base64,AAAA'})
check(res.status_code == 200 and '未設定' in res.get_json().get('error', ''), '写真読み取り（キー未設定）は案内を返す')
res = client.post('/meat/new', data={'supplier_name': 'イシマル', 'piece_count': '1', 'p0_part_id': str(parts['ミノ']),
                                     'p0_label_kg': '3.2', 'p0_trade_kg': '3.2', 'p0_unit_price': '3700'})
check(res.status_code == 302 and any(l['part_name'] == 'ミノ' for l in mdb.list_lots(('held',))), '画面から目利き登録')
res = client.post(f'/meat/lot/{c}/cattle', data={'birth_date': '2023-12-20', 'slaughter_date': '2026-08-19',
                                                 'sex': 'メス', 'producer_name': 'テスト牧場'})
check(res.status_code == 404, '個体識別番号の無い塊には個体情報を入れられない')
res = client.post(f'/meat/lot/{a}/cattle', data={'birth_date': '2023-12-20', 'slaughter_date': '2026-08-19', 'sex': 'メス'})
check(mdb.get_lot(a)['cattle']['age_months'] == 31, '個体情報を保存すると月齢を計算')
check(client.get('/').status_code in (200, 302), '予約システムのトップも動く')

# 自動照会：照会は1頭1回だけ、失敗しても登録は止めない（照会そのものは偽物に差し替え）
import meat
calls = []
def fake_lookup(no):
    calls.append(no)
    if no == '1399098647':
        raise cl.CattleLookupError('この番号の牛は見つかりませんでした')
    return dict(cl.parse_result(sample, '1680941492'), individual_no=no, raw='<html/>')
real_lookup = cl.lookup
cl.lookup = fake_lookup
meat.NLBC_AUTO_LOOKUP = True
mdb.save_cattle('1680941492', {})          # 既に照会済みの番号として扱う
done, errs = meat.lookup_cattle(['1463224934', '1463224934', '1399098647', '1680941492', ''])
check(calls == ['1463224934', '1399098647'], f'未照会の番号だけ1回ずつ照会: {calls}')
check(done == 1 and len(errs) == 1 and mdb.get_cattle('1463224934')['age_months'] == 29, '照会結果を保存、失敗は案内')
calls.clear()
meat.lookup_cattle(['1463224934'])
check(calls == [], '同じ番号は二度と照会しない')
res = client.post(f'/meat/lot/{a}/lookup')
check(res.status_code == 302, '塊の画面から照会できる')
calls.clear()
j = client.get('/meat/api/cattle/1463224934').get_json()
check(j['age_months'] == 29 and j['slaughter_date'] == '2026-09-07' and calls == [],
      '番号を打った時点の照会：照会済みなら保存した値をすぐ返す')
j = client.get('/meat/api/cattle/1234567890').get_json()
check(calls == ['1234567890'] and j['producer_name'] == '株式会社 テスト肥育牧場', '未照会なら照会して返す')
check('error' in client.get('/meat/api/cattle/1399098647').get_json(), '見つからない番号は案内を返す')
check(client.get('/meat/api/cattle/123').status_code == 400, '10桁でなければエラー')
cl.lookup = real_lookup

# ---- 誤って納品にした塊を戻す／期間後の納品は棚卸に出ない ----
d = mdb.create_held_lot({'part_id': parts['ウデモモ'], 'supplier_id': sup, 'label_kg': '10.0', 'trade_kg': '10.0',
                         'unit_price': '4000', 'promised_on': '2026-10-20'}, '社長')
e = mdb.create_held_lot({'part_id': parts['ウデモモ'], 'supplier_id': sup, 'label_kg': '9.0', 'trade_kg': '9.0',
                         'unit_price': '4000', 'promised_on': '2026-10-20'}, '社長')
mdb.receive_lots(sup, 'B-1', '2026-12-05', 0, [d, e], '社長')
ids = [l['id'] for l in mdb.lots_in_house_for_count('2026-11')]
check(d not in ids and e not in ids, '期間の後に納品した塊は、その期間の棚卸に出ない')
check(d in [l['id'] for l in mdb.lots_in_house_for_count('2026-12')], '納品した月の棚卸には出る')
res = client.post(f'/meat/lot/{d}/unreceive', data={'memo': 'まだ届いていない'})
lot = mdb.get_lot(d)
check(res.status_code == 302 and lot['status'] == 'held' and lot['slip_id'] is None and lot['received_on'] is None,
      '納品取消：業者預けに戻り、伝票のひも付けが外れる')
check(d not in [l['id'] for l in mdb.lots_in_house_for_count('2026-12')], '戻した塊は棚卸に出ない')
check(d in [l['id'] for l in mdb.list_lots(('held',))], '戻した塊は納品の画面で選び直せる')
mdb.save_count('2026-12', [{'part_id': parts['ウデモモ'], 'lot_id': e, 'kg': 9.0, 'state': 'raw'}], '社長', confirm=True)
try:
    mdb.unreceive_lot(e, '社長'); ok = False
except ValueError:
    ok = True
check(ok and mdb.get_lot(e)['status'] == 'received', '確定済みの棚卸に入った塊は戻せない')
mdb.process_lot(e, 9.0, 7.0, 1.0, 1.0, '社長')
try:
    mdb.unreceive_lot(e, '社長'); ok = False
except ValueError:
    ok = True
check(ok, '捌いた塊は戻せない')

# ---- 棚卸の画面から目利きに戻す（読み直さずに戻すのでJSONで返す）・どの画面にもタブ ----
f_ = mdb.create_held_lot({'part_id': parts['クラシタ'], 'supplier_id': sup, 'label_kg': '20.0', 'trade_kg': '20.0',
                          'unit_price': '4200', 'promised_on': '2026-10-20'}, '社長')
mdb.receive_lots(sup, 'C-1', '2027-01-04', 0, [f_], '社長')
h = client.get('/meat/count/2027-01').get_data(as_text=True)
check(f'backToHeld({f_}' in h and '目利きに戻す' in h, '棚卸の画面：未開封の塊に「目利きに戻す」')
for url in ['/meat/', '/meat/?tab=held', '/meat/receive', '/meat/count/2027-01', '/meat/closing/2027-01', '/meat/new']:
    h = client.get(url).get_data(as_text=True)
    check('meat-tabs' in h and '予約へ' in h, f'タブがある：{url}')
r = client.post(f'/meat/lot/{f_}/unreceive', headers={'X-Requested-With': 'fetch'})
check(r.status_code == 200 and r.get_json() == {'ok': True} and mdb.get_lot(f_)['status'] == 'held', '画面から戻すとJSONで成功が返る')
r = client.post(f'/meat/lot/{f_}/unreceive', headers={'X-Requested-With': 'fetch'})
check(r.status_code == 400 and 'error' in r.get_json(), '戻せない時はエラー文が返る')

# ---- 納品の画面に月齢・生産者 ----
g_ = mdb.create_held_lot({'part_id': parts['クラシタ'], 'supplier_id': sup, 'label_kg': '20.0', 'trade_kg': '20.0',
                          'unit_price': '4200', 'promised_on': '2026-10-20', 'individual_no': '1463224934'}, '社長')
mdb.create_held_lot({'part_id': parts['クラシタ'], 'supplier_id': sup, 'label_kg': '20.0', 'trade_kg': '20.0',
                     'unit_price': '4200', 'promised_on': '2026-10-20', 'individual_no': '1111111118'}, '社長')
h = client.get('/meat/receive').get_data(as_text=True)
c_ = mdb.get_cattle('1463224934')
check('月齢 29か月' in h and (c_['producer_name'] or '（氏名の公表なし）') in h, '納品の画面に月齢・生産者が出る')
check('まだ照会していません' in h, '未照会の番号はその旨を出す')

# ---- 棚卸の「一覧に無い分」：欄を追加できる ----
h = client.get('/meat/count/2027-03').get_data(as_text=True)
check('欄を追加' in h and 'id="extra-row-template"' in h and 'extra___i___kg' in h, '棚卸の画面：欄を追加のボタンとひな形がある')
check('name="extra_count" id="extra-count" value="3"' in h, '何も入れていなければ空き欄は3つ')
res = client.post('/meat/count/2027-03', data={
    'action': 'save', 'extra_count': '6',
    'extra_0_part': str(parts['ミノ']), 'extra_0_state': 'raw', 'extra_0_kg': '1.5',
    # 追加した欄（4つ目より後）も保存され、同じ部位の行は足し算される
    'extra_5_part': str(parts['ミノ']), 'extra_5_state': 'product', 'extra_5_kg': '0.25',
    # kgが空の欄は保存しない
    'extra_4_part': str(parts['クラシタ']), 'extra_4_state': 'raw', 'extra_4_kg': ''})
check(res.status_code == 302, '欄を増やして保存できる')
saved_extras = [ln for ln in mdb.get_count('2027-03')['lines'] if not ln['lot_id']]
check(sorted(ln['kg'] for ln in saved_extras) == [0.25, 1.5], '追加した欄も保存され、kgが空の欄は消える')
check(mdb._count_totals('2027-03', confirmed_only=False)[parts['ミノ']][0] == 1.75, '同じ部位の行は合計される')
h = client.get('/meat/count/2027-03').get_data(as_text=True)
check('name="extra_count" id="extra-count" value="5"' in h, '読み直すと保存した2行＋空き3つ')
client.post('/meat/count/2027-03', data={
    'action': 'confirm', 'extra_count': '1',
    'extra_0_part': str(parts['ミノ']), 'extra_0_state': 'raw', 'extra_0_kg': '1.5'})
check(mdb.get_count('2027-03')['status'] == 'confirmed', '（前提）確定できた')
h = client.get('/meat/count/2027-03').get_data(as_text=True)
check('id="extra-row-template"' not in h and '欄を追加' not in h, '確定済みの棚卸には欄を追加のボタンを出さない')

from database import engine
engine.dispose()
try:
    os.unlink(_tmp.name)
except OSError:
    pass
print('\n' + ('すべて成功' if not failures else f'{failures} 件失敗'))
sys.exit(1 if failures else 0)
