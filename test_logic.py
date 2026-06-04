from logic import assign_seats

# ===== テスト1: 個室希望は10・11番のみ =====
r1 = {'time_slot':'18:00', 'total_people':2, 'duration_minutes':105, 'is_vip':0, 'private_room':1}
s1 = assign_seats(r1, [])
print('TEST1 【個室希望 2名】→', s1['note'], '| テーブル:', s1['tables'])
assert 10 in s1['tables'] or 11 in s1['tables'], 'ERROR: 個室希望が1-5番に案内された'

# ===== テスト2: 6番が埋まっているとき、次は7番 =====
existing = [{
    'id': 1, 'name': '田中', 'time_slot': '18:00',
    'duration_minutes': 105, 'assigned_tables': [6], 'is_group': 0
}]
r2 = {'time_slot':'18:00', 'total_people':2, 'duration_minutes':105, 'is_vip':0, 'private_room':0}
s2 = assign_seats(r2, existing)
print('TEST2 【6番使用中->次の2名】→', s2['note'], '| テーブル:', s2['tables'])
assert 7 in s2['tables'], 'ERROR: 7番に案内されなかった: ' + str(s2['tables'])

# ===== テスト3: 個室希望・満席時は警告 =====
existing2 = [
    {'id':2,'name':'A','time_slot':'18:00','duration_minutes':105,'assigned_tables':[10],'is_group':0},
    {'id':3,'name':'B','time_slot':'18:00','duration_minutes':105,'assigned_tables':[11],'is_group':0},
]
r3 = {'time_slot':'18:00', 'total_people':2, 'duration_minutes':105, 'is_vip':0, 'private_room':1}
s3 = assign_seats(r3, existing2)
print('TEST3 【個室希望・VIPエリア満席】→', s3['note'])
assert s3['tables'] == [], 'ERROR: 満席なのに席が割り当てられた'

# ===== テスト4: VIP + 個室希望なら通常席にフォールスルー =====
r4 = {'time_slot':'18:00', 'total_people':2, 'duration_minutes':105, 'is_vip':1, 'private_room':1}
s4 = assign_seats(r4, existing2)
print('TEST4 【VIP+個室・VIPエリア満席->通常席へ】→', s4['note'], '| テーブル:', s4['tables'])
assert s4['tables'] != [], 'ERROR: VIPなのに席が見つからなかった'

print()
print('= 全テスト通過 =')
