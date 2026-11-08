# test_sales_target.py — 売上目標と目標客数（予約台帳の札・厨房モニターの帯）の確認（python test_sales_target.py）
# 一時ファイルのSQLiteで動かすので、本番・手元のデータには触れない
import os, sys, tempfile
from datetime import datetime

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['DATABASE_URL'] = 'sqlite:///' + _tmp.name.replace('\\', '/')
for k in ('APP_PASSWORD', 'MEAT_ADMIN_PASSWORD', 'ANTHROPIC_API_KEY'):
    os.environ.pop(k, None)
os.environ['NLBC_AUTO_LOOKUP'] = '0'

import app as appmod
import meat as meat_module
import sales_target_db as stdb
from database import save_reservation, set_reservation_status, cancel_reservation, update_reservation, get_reservation

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


# 「今」を 2026-11-08 18:30 に固定する
NOW = datetime(2026, 11, 8, 18, 30)
appmod.now_jst = lambda: NOW
TODAY = '2026-11-08'
client = appmod.app.test_client()


def add(people, time_slot='19:00', day=TODAY, name='テスト'):
    return save_reservation({'date': day, 'time_slot': time_slot, 'name': name, 'phone': '090', 'adults': people,
                             'children_info': '[]', 'total_people': people, 'duration_minutes': 120,
                             'private_room': 0, 'is_vip': 0, 'is_group': 0, 'assigned_tables': '[]'})


def goal():
    return client.get('/api/kitchen-monitor-data').get_json()['goal']


# ---- 計算 ----
g = stdb.goal_for(367553, 5400, 64, 60, 4)
check(g['goal'] == 69 and g['short'] == 5 and g['status'] == 'short', '367,553円 → 目標69名、見込み64名ならあと5名')
check(stdb.goal_for(5400, 5400, 0, 0, 0)['goal'] == 1 and stdb.goal_for(5401, 5400, 0, 0, 0)['goal'] == 2, '切り上げ（5,400円＝1名、5,401円＝2名）')
g = stdb.goal_for(367553, 5400, 72, 70, 2)
check(g['status'] == 'achieved' and g['surplus'] == 3 and g['short'] == 0, '見込みが目標を超えたら目標達成（＋3名）')
check(stdb.goal_for(367553, 5400, 69, 69, 0)['status'] == 'achieved', 'ちょうど目標人数でも目標達成')
g = stdb.goal_for(367553, 5400, 72, 70, 2)
check((g['next_pct'], g['next_goal'], g['to_next']) == (110, 76, 4) and 'ありがとうございます' in g['cheer'],
      '達成したら、ねぎらいと「次は110%（76名）まであと4名」')
g = stdb.goal_for(367553, 5400, 77, 77, 0)   # 111%
check((g['next_pct'], g['next_goal'], g['to_next']) == (120, 83, 6) and '大きく超え' in g['cheer'], '110%を超えたら次は120%')
g = stdb.goal_for(367553, 5400, 76, 76, 0)   # ちょうど110%（76名＝110.1%）
check(g['next_pct'] == 120 and g['to_next'] > 0, '段にちょうど届いたら次の段を目指す（あと0名にはしない）')
g = stdb.goal_for(367553, 5400, 90, 90, 0)   # 130%
check(g['next_pct'] == 140 and 'すばらしい' in g['cheer'] and g['to_next'] > 0, '130%を超えても次の目安が出る')
check('cheer' not in stdb.goal_for(367553, 5400, 60, 60, 0), '未達の時はねぎらいを出さない')
check(stdb.goal_for(None, 5400, 10, 5, 5)['status'] == 'unset' and 'goal' not in stdb.goal_for(None, 5400, 10, 5, 5),
      '目標が無ければ未設定（目標の数字を出さない）')

# ---- 目標の保存 ----
check(stdb.get_target(TODAY) is None, '最初は未設定')
stdb.save_targets({TODAY: '367,553円'}, 'manual', '社長')
check(stdb.get_target(TODAY) == 367553, '「367,553円」と入れても保存できる')
for bad in ('abc', '-5', '0', '12.5'):
    check(raises(lambda: stdb.save_targets({TODAY: bad}, 'manual', 'x')) and stdb.get_target(TODAY) == 367553,
          f'おかしな金額は保存せず、前の目標も消さない：{bad!r}')
stdb.save_targets({'2026-11-09': '300000'}, 'manual', '社長')
stdb.save_targets({'2026-11-09': ''}, 'manual', '社長')
check(stdb.get_target('2026-11-09') is None, '空にすると未設定に戻る')
check(stdb.get_unit_price() == 5400, '客単価は最初 5,400円')
check(raises(lambda: stdb.set_unit_price('0', 'x')) and stdb.get_unit_price() == 5400, 'おかしな客単価は入らない')

# ---- 一覧の取り込み ----
found, skipped = stdb.parse_pasted('日付\t売上目標\n2026/11/10\t310,000\n11/11\t298000円\n2026年11月12日 ¥320,500\n\nメモ 合計 928,500', 2026)
check(found == {'2026-11-10': '310,000', '2026-11-11': '298000', '2026-11-12': '320,500'}, f'いろいろな書き方の日付・金額を読める {found}')
check(len(skipped) == 2, '見出しやメモの行は飛ばす')
found, skipped = stdb.parse_pasted('2026/11/13 100,000 200,000\n2026/2/30 100000', 2026)
check(not found and len(skipped) == 2, '金額が2つある行・無い日付は取り込まない')
res = client.post('/sales-targets/import', data={'month': '2026-11', 'pasted': '2026/11/10\t310,000\n2026/11/11\t298,000'})
check(res.status_code == 302 and stdb.get_target('2026-11-11') == 298000, '画面から取り込める')
check(stdb.month_targets('2026-11')['2026-11-10']['source'] == 'import', '取り込んだ目標は「取り込み」と記録')

# ---- 厨房モニター：人数の扱い ----
stdb.save_targets({TODAY: '367553'}, 'manual', '社長')
for i in range(15):
    set_reservation_status(add(4, '17:30', name=f'来店済み{i}'), 'visited')   # 60名 来店済み
r_late = add(4, '20:00', name='これから')                                    # 4名 これから
add(10, '19:00', day='2026-11-09', name='別の日')                             # 別の日は入らない
cancel_reservation(add(6, '19:00', name='キャンセル'))                        # キャンセルは入らない
g = goal()
check((g['expected'], g['visited'], g['upcoming'], g['goal'], g['short']) == (64, 60, 4, 69, 5),
      f'例の通り：見込み64名＝来店済み60＋これから4、目標69名・あと5名 {g}')

r_new = add(3, '20:30', name='当日の追加予約')
g = goal()
check((g['expected'], g['upcoming'], g['short']) == (67, 7, 2), '当日の追加予約で見込みとこれからが増え、あと2名')

res = client.post('/api/reservation/walkin', json={'adults': 2, 'assigned_tables': [1]})
g = goal()
check(res.get_json()['ok'] and (g['expected'], g['visited'], g['upcoming'], g['status'], g['short']) == (69, 62, 7, 'achieved', 0),
      'フリー来店は来店済みに入り、これからは変わらない → 69名で目標達成')

set_reservation_status(r_late, 'visited')
g = goal()
check((g['expected'], g['visited'], g['upcoming']) == (69, 66, 3), '来店受付すると、これから→来店済みに移り、見込みは変わらない（重ねて足さない）')

d = get_reservation(r_new); d['total_people'] = d['adults'] = 6
update_reservation(r_new, d)
g = goal()
check((g['expected'], g['surplus']) == (72, 3), '人数変更（3→6名）が反映される → 目標達成 ＋3名')

cancel_reservation(r_new)
g = goal()
check((g['expected'], g['short']) == (66, 3), 'キャンセルすると見込みから外れる')

r_move = add(5, '21:00', name='日付変更')
d = get_reservation(r_move); d['date'] = '2026-11-10'
update_reservation(r_move, d)
g = goal()
check(g['expected'] == 66, '別の日に移した予約は今日から外れる')
h = client.get('/?date=2026-11-10').get_data(as_text=True)
check('目標58名' in h, '移した先の日の札に入る（310,000円 → 58名）')

# 予定時刻を過ぎて未受付（押し忘れ・無断キャンセル）
add(2, '18:00', name='押し忘れ')    # 18:30 時点で30分過ぎ
add(3, '18:20', name='まだ15分以内')
g = goal()
check(g['overdue'] == 2 and g['expected'] == 71, '予定時刻を15分過ぎて未受付の人数を別に出す（見込みには含めたまま）')

# 今までの表示の数字は変わらない
data = client.get('/api/kitchen-monitor-data').get_json()
check(data['total_people'] == g['expected'] and data['visited_people'] == g['visited']
      and data['remaining_people'] == g['upcoming'], '今までのタイルの数字と同じ数え方')

# ---- 目標未設定・読み込み失敗 ----
stdb.save_targets({TODAY: ''}, 'manual', '社長')
check(goal()['status'] == 'unset', '目標を消すと未設定')
h = client.get('/').get_data(as_text=True)
check('目標未設定' in h and '目標達成' not in h, '予約台帳の札は「目標未設定」（0円や目標達成とは出さない）')
real_get = stdb.get_target
stdb.get_target = lambda d: (_ for _ in ()).throw(RuntimeError('DB'))
check(goal()['status'] == 'error' and client.get('/').status_code == 200, '目標を読めなくても画面は出て、読み込み失敗と出す')
check('読み込み失敗' in client.get('/').get_data(as_text=True), '札に読み込み失敗と出る')
stdb.get_target = real_get

# ---- 画面 ----
stdb.save_targets({TODAY: '367553'}, 'manual', '社長')
h = client.get('/').get_data(as_text=True)
check('目標69名' in h and '来店済み' in h and '人数の目安' in h, '予約台帳に札と内訳が出る')
h = client.get('/kitchen-monitor').get_data(as_text=True)
check('goalBand' in h and 'renderGoal' in h, '厨房モニターに目標の帯がある')
h = client.get('/sales-targets/?month=2026-11').get_data(as_text=True)
check('367,553' in h and '69名' in h and '保存する' in h, '売上目標の画面に金額と目標客数が出る')
res = client.post('/sales-targets/save', data={'month': '2026-11', 'd_2026-11-20': '400,000', 'd_2026-11-21': 'abc'})
check(stdb.get_target('2026-11-20') is None, '画面の保存も、おかしな金額が1つでもあれば保存しない')
client.post('/sales-targets/unit-price', data={'month': '2026-11', 'unit_price': '6000'})
check(goal()['goal'] == 62, '客単価を 6,000円に変えると目標客数が変わる（367,553÷6,000＝62名）')

# ---- 管理者でなければ見るだけ ----
meat_module.MEAT_ADMIN_PASSWORD = 'pw'
guest = appmod.app.test_client()
h = guest.get('/sales-targets/?month=2026-11').get_data(as_text=True)
check('367,553' in h and '保存する' not in h and '管理者パスワード' in h, '管理者でなくても見られるが、入力欄は使えない')
guest.post('/sales-targets/save', data={'month': '2026-11', f'd_{TODAY}': '1'})
guest.post('/sales-targets/import', data={'month': '2026-11', 'pasted': f'{TODAY}\t1'})
guest.post('/sales-targets/unit-price', data={'month': '2026-11', 'unit_price': '1'})
check(stdb.get_target(TODAY) == 367553 and stdb.get_unit_price() == 6000, '管理者でなければ目標も客単価も変えられない')
meat_module.MEAT_ADMIN_PASSWORD = ''

# ---- バックアップに入る ----
import io
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(client.get('/backup/xlsx').data))
check('売上目標_日別' in wb.sheetnames and '売上目標_設定' in wb.sheetnames, 'バックアップに売上目標が入る')

from database import engine
engine.dispose()
try:
    os.unlink(_tmp.name)
except OSError:
    pass
print('\n' + ('すべて成功' if not failures else f'{failures} 件失敗'))
sys.exit(1 if failures else 0)
