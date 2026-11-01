# test_duty.py — 今日の仕事・申し送りの確認（python test_duty.py）
# 一時ファイルのSQLiteで動かすので、本番・手元の reservations.db には触れない
import os, sys, tempfile
from datetime import datetime

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['DATABASE_URL'] = 'sqlite:///' + _tmp.name.replace('\\', '/')
os.environ.pop('APP_PASSWORD', None)

import duty_db as ddb
import database as db
from app import app

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


client = app.test_client()
D1, D2, D3 = '2026-11-02', '2026-11-03', '2026-11-04'
items = {i['name']: i for i in ddb.list_items()}
sec = lambda s: [i for i in ddb.list_items(s)]

# ---- 項目（Excel 本人訂正版どおり） ----
check(len(sec('morning')) == 14, f'朝の仕事は14項目: {len(sec("morning"))}')
check(len(sec('evening')) == 5, f'夕方の仕事は5項目: {len(sec("evening"))}')
check(len(sec('detail')) == 8, f'細かい点検は8か所: {len(sec("detail"))}')
check(items['爪楊枝と塩の確認']['section'] == 'morning' and items['爪楊枝の補充']['section'] == 'evening',
      '爪楊枝・塩の確認は朝、爪楊枝の補充は夕方')
check(items['下駄箱の中']['section'] == 'detail', '下駄箱の中は朝の点検（要掃除の日だけ夜に）')
check(all(i['section'] == 'candidate' for i in sec('candidate')) and len(sec('candidate')) == 3,
      '追加候補3つは必須の仕事にしない（別の区分）')
ddb.init_duty_db()
check(len(ddb.list_items()) == 30, '2回目の初期化で項目が増えない')

# ---- 朝の仕事：確認者・完了・所要分 ----
floor = items['床の掃除']['id']
check(raises(ValueError, lambda: ddb.save_check(D1, floor, True, 20, '', 0)), '確認者を選ばないと完了にできない')
check(raises(ValueError, lambda: ddb.set_day_staff(D1, 'morning', '知らない人')), '朝の確認者は碇・原から選ぶ')
ddb.set_day_staff(D1, 'morning', '碇 静子')
ddb.save_check(D1, floor, True, 25, '碇 静子', 0)
st = ddb.get_state(D1)
c = st['checks'][str(floor)]
check(c['done'] == 1 and c['minutes'] == 25 and c['staff_name'] == '碇 静子', '朝の仕事：完了・25分・確認者が保存される')
check(st['counts']['morning_done'] == 1, '朝の完了数 1/14')
check(raises(ddb.Conflict, lambda: ddb.save_check(D1, floor, False, None, '原 美郷', 0)),
      '古い画面からの上書きは断る（同時操作で記録が消えない）')
check(ddb.get_state(D1)['checks'][str(floor)]['done'] == 1, '断ったあとも完了のまま')
check(raises(ValueError, lambda: ddb.save_check(D1, floor, True, 'あ', '碇 静子', 1)), '所要分が数字でなければ断る')
check(raises(ValueError, lambda: ddb.save_check(D1, items['障子の桟・間仕切りの縁']['id'], True, 5, '碇 静子', 0)),
      '点検の場所を朝の仕事として完了にはできない（混同しない）')

# ---- 朝の点検 → 夜の依頼 ----
shoji, geta, mado = (items[n]['id'] for n in ('障子の桟・間仕切りの縁', '下駄箱の中', '窓台・サッシの溝'))
ddb.save_inspection(D1, shoji, 'need', '碇 静子', 0)
ddb.save_inspection(D1, geta, 'need', '碇 静子', 0)
ddb.save_inspection(D1, mado, 'ok', '碇 静子', 0)
st = ddb.get_state(D1)
titles = [r['title'] for r in st['open_requests']]
check(titles == ['障子の桟・間仕切りの縁', '下駄箱の中'], f'要掃除の2か所だけが夜の依頼に並ぶ: {titles}')
check(st['inspections'][str(mado)]['state'] == 'ok', '問題なしは依頼にならず、判定は残る')
# 押し間違いの訂正：今朝の要掃除を問題なしに直すと、手付かずの依頼は取り下げ
ddb.save_inspection(D1, mado, 'need', '碇 静子', 1)
check(len(ddb.get_state(D1)['open_requests']) == 3, '要掃除にするともう1件')
ddb.save_inspection(D1, mado, 'ok', '碇 静子', 2)
check(len(ddb.get_state(D1)['open_requests']) == 2, '今朝の押し間違いを直すと、手付かずの依頼は取り下げ')

# 自由な仕事・申し送り文
ddb.add_request(D1, '2階座敷の網戸を拭く', '碇 静子')
ddb.add_note(D1, '碇 静子', '10番の障子の桟、ほこりが多いです')
st = ddb.get_state(D1)
check(len(st['open_requests']) == 3 and st['counts']['notes'] == 1, '自由な仕事と申し送り文が入る')
check(raises(ValueError, lambda: ddb.add_request(D1, '  ', '碇 静子')), '空の仕事は追加しない')

# ---- 夜の実行記録 ----
req = {r['title']: r for r in st['open_requests']}
r_shoji = req['障子の桟・間仕切りの縁']
check(raises(ValueError, lambda: ddb.complete_request(r_shoji['id'], r_shoji['version'], '', 10, D1)), '実行者の名前がないと完了にしない')
check(raises(ValueError, lambda: ddb.complete_request(r_shoji['id'], r_shoji['version'], '田中', '', D1)), '所要時間がないと完了にしない')
ddb.complete_request(r_shoji['id'], r_shoji['version'], '田中', 15, D1)
check(raises(ddb.Conflict, lambda: ddb.complete_request(r_shoji['id'], r_shoji['version'], '佐藤', 20, D1)),
      '2台で同時に完了：あとの方は断り、先の記録を消さない')
st = ddb.get_state(D1)
d = st['done_requests'][0]
check(d['executor'] == '田中' and d['minutes'] == 15 and d['status'] == 'done', '実行者・所要時間が保存される')
check(r_shoji['id'] not in [r['id'] for r in st['open_requests']], '完了した依頼は残りと分けて表示')
# 訂正
ddb.reopen_request(d['id'], d['version'], '田中', '押し間違い', D1)
st = ddb.get_state(D1)
check(any(r['id'] == d['id'] for r in st['open_requests']) and not st['done_requests'], '訂正で未完了に戻せる')
r = next(r for r in st['open_requests'] if r['id'] == d['id'])
check([l['action'] for l in r['logs']] == ['create', 'done', 'reopen'], '完了と訂正の履歴が残る')
ddb.complete_request(r['id'], r['version'], '田中', 15, D1)
# 下駄箱はできなかった
r_geta = next(r for r in ddb.get_state(D1)['open_requests'] if r['title'] == '下駄箱の中')
ddb.add_request_memo(r_geta['id'], '佐藤', '時間が足りずできませんでした', D1)

# ---- 翌日：未完了は残る・重複作成しない ----
st2 = ddb.get_state(D2)
open2 = {r['title']: r for r in st2['open_requests']}
check(set(open2) == {'下駄箱の中', '2階座敷の網戸を拭く'}, f'日付が変わっても未完了は消えない: {sorted(open2)}')
check(open2['下駄箱の中']['origin_date'] == D1 and st2['counts']['open_old'] == 2, '発生日つき（前の日から2件）')
check(any(l['memo'] == '時間が足りずできませんでした' for l in open2['下駄箱の中']['logs']), 'できなかった理由が残る')
check(st2['counts']['notes'] == 0 and st2['counts']['morning_done'] == 0, '翌日の朝の仕事・申し送り文は新しい日として始まる')
ddb.set_day_staff(D2, 'morning', '原 美郷')
ddb.save_inspection(D2, geta, 'need', '原 美郷', 0)
st2 = ddb.get_state(D2)
getas = [r for r in st2['open_requests'] if r['title'] == '下駄箱の中']
check(len(getas) == 1, '翌朝また要掃除にしても、依頼は1件のまま（重複作成しない）')
check(getas[0]['logs'][-1]['action'] == 'reflag' and '朝も要掃除' in getas[0]['logs'][-1]['memo'], '前からの依頼に「朝も要掃除」と書き足す')
ddb.save_inspection(D2, geta, 'ok', '原 美郷', 1)
ddb.save_inspection(D2, geta, 'need', '原 美郷', 2)
check(len([l for l in ddb.get_state(D2)['open_requests'] if l['title'] == '下駄箱の中'][0]['logs']) == len(getas[0]['logs']),
      '同じ日に何度押しても書き足しは1回')
check(len([r for r in ddb.get_state(D2)['open_requests'] if r['title'] == '下駄箱の中']) == 1,
      '前日からの依頼は、今朝の判定を直しても取り下げない')
r_geta = [r for r in ddb.get_state(D2)['open_requests'] if r['title'] == '下駄箱の中'][0]
ddb.complete_request(r_geta['id'], r_geta['version'], '佐藤', 30, D2)
st2 = ddb.get_state(D2)
check([r['title'] for r in st2['done_requests']] == ['下駄箱の中'] and st2['done_requests'][0]['origin_date'] == D1,
      '翌日に完了：完了日の一覧に発生日つきで出る')
check([r['title'] for r in ddb.get_state(D1)['done_requests']] == ['障子の桟・間仕切りの縁'], '前日の完了一覧は前日のまま')
# 依頼の取り消し
r_ami = [r for r in st2['open_requests'] if r['title'] == '2階座敷の網戸を拭く'][0]
ddb.void_request(r_ami['id'], r_ami['version'], '原 美郷', '別の日にやる', D2)
check(not ddb.get_state(D3)['open_requests'], '取り消した依頼は残りから消える')
hist = {r['title']: r for r in ddb.history(60)}
check(hist['2階座敷の網戸を拭く']['status'] == 'void' and hist['下駄箱の中']['status'] == 'done', '履歴に完了・取り消しが残る')

# ---- 日の切り替え（朝5時） ----
check(ddb.work_date(datetime(2026, 11, 3, 1, 30)) == '2026-11-02', '夜中の1時半は前の日の夜として扱う')
check(ddb.work_date(datetime(2026, 11, 3, 5, 0)) == '2026-11-03', '朝5時から新しい日')

# ---- 画面・API（複数タブレットは同じDBを見る） ----
h = client.get(f'/duty/?date={D1}')
check(h.status_code == 200 and '朝の仕事' in h.get_data(as_text=True), '画面が開く')
rev0 = client.get('/duty/api/rev').get_json()['rev']
res = client.post('/duty/api/request/add', json={'date': D3, 'title': '玄関マット', 'staff': '碇 静子'})
j = res.get_json()
check(res.status_code == 200 and j['ok'] and any(r['title'] == '玄関マット' for r in j['state']['open_requests']),
      'APIで追加すると最新の状態が返る')
check(client.get('/duty/api/rev').get_json()['rev'] > rev0, '更新の番号が進む（他のタブレットが気づける）')
s_other = client.get(f'/duty/api/state?date={D3}').get_json()
check(any(r['title'] == '玄関マット' for r in s_other['open_requests']), '別のタブレットから見ても同じ（再読み込みしても残る）')
r_m = [r for r in s_other['open_requests'] if r['title'] == '玄関マット'][0]
res = client.post(f'/duty/api/request/{r_m["id"]}/complete', json={'date': D3, 'version': r_m['version'] + 5,
                                                                   'executor': '田中', 'minutes': 5})
check(res.status_code == 409 and res.get_json()['ok'] is False, '古い版での完了は 409（完了と表示させない）')
res = client.post(f'/duty/api/request/{r_m["id"]}/complete', json={'date': D3, 'version': r_m['version'],
                                                                   'executor': '', 'minutes': 5})
check(res.status_code == 400 and res.get_json()['ok'] is False, '名前なしは 400')
res = client.post('/duty/api/check', json={'date': D3, 'item_id': floor, 'done': True, 'minutes': 10, 'staff': '', 'version': 0})
check(res.status_code == 400, '確認者なしの完了は 400')

# ---- 予約台帳への影響がない ----
before = db.get_reservations_by_date('2026-11-05')
rid = db.save_reservation({'name': 'テスト', 'phone': '0900000000', 'date': '2026-11-05', 'time_slot': '18:00',
                           'adults': 2, 'total_people': 2, 'children_info': [], 'duration_minutes': 120,
                           'private_room': 0, 'is_vip': 0, 'is_group': 0, 'assigned_tables': '[1]',
                           'status': 'reserved', 'notes': '備考'})
check(len(db.get_reservations_by_date('2026-11-05')) == len(before) + 1, '予約の登録はいつもどおり')
h = client.get('/?date=2026-11-05').get_data(as_text=True)
check('今日の仕事・申し送り' in h and 'テスト 様' in h, '予約一覧に入口が出て、予約も出る')
check(client.get('/timetable?date=2026-11-05').status_code == 200, 'タイムテーブルも開く')
orig = ddb.summary
ddb.summary = lambda: (_ for _ in ()).throw(RuntimeError('壊れた'))
h = client.get('/?date=2026-11-05')
check(h.status_code == 200 and 'テスト 様' in h.get_data(as_text=True) and 'duty-entry' not in h.get_data(as_text=True),
      '今日の仕事が壊れても、予約一覧は必ず出る')
ddb.summary = orig

from database import engine
engine.dispose()
try:
    os.unlink(_tmp.name)
except OSError:
    pass
print('\n' + ('すべて成功' if not failures else f'{failures} 件失敗'))
sys.exit(1 if failures else 0)
