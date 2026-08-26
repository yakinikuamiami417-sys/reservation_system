# logic.py — 席割り当てロジック & 競合チェック
# キリン屋 予約管理システム
import json, re
from datetime import datetime, timedelta, date

# ============================================================
# テーブル構成定義
# ============================================================
TABLES = {
    1:  {"capacity": 6, "zone": "メインフロア", "adjacents": [2],    "is_buffer": False, "label": "1番"},
    2:  {"capacity": 6, "zone": "メインフロア", "adjacents": [1, 3], "is_buffer": False, "label": "2番"},
    3:  {"capacity": 6, "zone": "メインフロア", "adjacents": [2, 4], "is_buffer": False, "label": "3番"},
    4:  {"capacity": 6, "zone": "メインフロア", "adjacents": [3, 5], "is_buffer": False, "label": "4番"},
    5:  {"capacity": 6, "zone": "メインフロア", "adjacents": [4],    "is_buffer": False, "label": "5番"},
    6:  {"capacity": 4, "zone": "サイドA",      "adjacents": [7],    "is_buffer": False, "label": "6番"},
    7:  {"capacity": 4, "zone": "サイドA",      "adjacents": [6],    "is_buffer": False, "label": "7番"},
    8:  {"capacity": 4, "zone": "サイドB",      "adjacents": [9],    "is_buffer": False, "label": "8番"},
    9:  {"capacity": 4, "zone": "サイドB",      "adjacents": [8],    "is_buffer": False, "label": "9番"},
    10: {"capacity": 6, "zone": "VIPエリア",    "adjacents": [11],   "is_buffer": True,  "label": "10番"},
    11: {"capacity": 8, "zone": "VIPエリア",    "adjacents": [10],   "is_buffer": False, "label": "11番"},
}

MAIN_TABLES = [1, 2, 3, 4, 5]   # 団体時に隣接ブロックが発生するテーブル群

# ============================================================
# 時間ユーティリティ
# ============================================================
def _pt(t_str):
    """'HH:MM' → datetime"""
    return datetime.strptime(t_str, '%H:%M')

def _overlap(s1, d1, s2, d2):
    """2つの時間帯（開始時刻, 分）が重複するか"""
    e1 = s1 + timedelta(minutes=int(d1))
    e2 = s2 + timedelta(minutes=int(d2))
    return s1 < e2 and s2 < e1

def _to_list(val):
    """JSON文字列 or リスト → リスト"""
    if isinstance(val, list):
        return val
    if val is None or val == '':
        return []
    try:
        return json.loads(val)
    except Exception:
        return []

# ============================================================
# 競合チェック（入店時間分散）
# ============================================================
def check_time_conflict(new_res, existing_list):
    """
    新規予約の入店時刻が既存予約と15分未満の場合を検出。
    同一日付の予約のみ対象。
    返り値: 競合する予約のリスト
    """
    conflicts = []
    try:
        new_start = _pt(new_res['time_slot'])
    except Exception:
        return []

    new_id = new_res.get('id')

    for ex in existing_list:
        if ex.get('id') == new_id:
            continue
        try:
            ex_start = _pt(ex['time_slot'])
        except Exception:
            continue
        diff_min = abs((new_start - ex_start).total_seconds() / 60)
        if diff_min < 15:
            conflicts.append(ex)

    return conflicts

# ============================================================
# 使用中 & ブロック席の取得
# ============================================================
def _get_occupied_and_blocked(time_slot, duration, existing_list, exclude_id=None):
    """
    指定時間帯に使用中のテーブルと、
    団体予約による隣接ブロックテーブルのセットを返す。
    ※ テーブルIDは必ず int に統一して比較する
    """
    try:
        new_start = _pt(time_slot)
    except Exception:
        return set(), set()

    occupied = set()
    blocked  = set()

    for ex in existing_list:
        if exclude_id is not None and ex.get('id') == exclude_id:
            continue
        try:
            ex_start = _pt(ex['time_slot'])
        except Exception:
            continue
        ex_dur = int(ex.get('duration_minutes', 105))

        if _overlap(new_start, int(duration), ex_start, ex_dur):
            tables = _to_list(ex.get('assigned_tables', []))
            for t in tables:
                occupied.add(int(t))          # ← int に統一（str混入対策）
            # メインフロアの団体予約 → 隣接席をブロック
            if ex.get('is_group'):
                for t in tables:
                    if int(t) in MAIN_TABLES:
                        for adj in TABLES[int(t)]['adjacents']:
                            blocked.add(int(adj))

    return occupied, blocked

# ============================================================
# 席割り当て提案
# ============================================================
def assign_seats(reservation, existing_list):
    """
    予約内容に基づき最適なテーブルを提案する。

    優先順位:
    1-4名  : サイドA (6・7番) 単席
    1-6名  : メインフロア (1-5番) 単席
    5-8名  : サイドA連結 (6+7) / 11番単席 / サイドB連結 (8+9) ※最終手段
    9-14名 : VIPエリア連結 (10+11)
    15名以上: メイン連結 or 要手動確認

    席容量チェックは「大人人数」ベース:
    子供（お子様）は席を占有しないものとして扱う。
    大人が席に収まれば子供連れでも同テーブルを提案する。

    返り値: {"tables": [int...], "capacity": int, "note": str}
    """
    total      = int(reservation.get('total_people', 1))
    adults     = int(reservation.get('adults', total))   # 席容量チェック用（大人のみ）
    duration   = int(reservation.get('duration_minutes', 105))
    time_slot  = reservation.get('time_slot', '18:00')
    is_vip     = bool(reservation.get('is_vip', 0))
    wants_priv = bool(reservation.get('private_room', 0))
    excl_id    = reservation.get('id')

    occupied, blocked = _get_occupied_and_blocked(time_slot, duration, existing_list, excl_id)

    def avail(t_id):
        return t_id not in occupied and t_id not in blocked

    def res(tables, capacity, note):
        return {"tables": tables, "capacity": capacity, "note": note}

    # ── 個室希望: VIPエリア(10・11番)のみ案内。1〜5番には絶対に配置しない ──
    if wants_priv:
        if adults <= 6 and avail(10):
            return res([10], 6, "10番【個室エリア】6席")
        if adults <= 8 and avail(11):
            return res([11], 8, "11番【個室エリア】8席")
        if adults <= 14 and avail(10) and avail(11):
            return res([10, 11], 14, "10・11番連結【個室エリア】14席")
        if adults <= 6 and avail(11):          # 10番満席時の代替
            return res([11], 8, "11番【個室エリア代替】8席")
        if not is_vip:
            # VIPフラグがない個室希望は、空きなければ通常席に案内しない
            return res([], 0, "⚠️ 個室エリア(10・11番)が満席です。VIPフラグをONにすると他の席へ案内できます")

    # ── VIP特例: VIPエリア優先、なければ通常席へフォールスルー ──
    if is_vip:
        if adults <= 6 and avail(10):
            return res([10], 6, "10番【VIPエリア】6席")
        if adults <= 8 and avail(11):
            return res([11], 8, "11番【VIPエリア】8席")
        if adults <= 14 and avail(10) and avail(11):
            return res([10, 11], 14, "10・11番連結【VIPエリア】14席")
        # VIPは通常席へフォールスルー（警告なし）

    # ============================================================
    # 提案優先順位（オーナー指定）:
    #   単席: 6 → 7 → 8 → 9 → 1 → 3 → 5 → 2 → 4 → 11
    #   連結: サイドA(6+7) → サイドB(8+9) → メイン2席連結
    # 容量チェックは大人人数ベース（子供は席を占有しない扱い）
    # ============================================================

    SINGLE_PRIORITY = [6, 7, 8, 9, 1, 3, 5, 2, 4, 11]

    # ── 1. 単席で収まる人数: 優先順に空席を探す（大人人数で容量チェック）──
    for t in SINGLE_PRIORITY:
        if avail(t) and TABLES[t]['capacity'] >= adults:
            note = f"{t}番テーブル（{TABLES[t]['capacity']}席）"
            if total > adults:
                note += f" ※お子様{total - adults}名含む合計{total}名"
            return res([t], TABLES[t]['capacity'], note)

    # ── 2. 中人数（5-8名）: サイドA連結（6+7）──
    if adults <= 8 and avail(6) and avail(7):
        return res([6, 7], 8, "6・7番連結（8席）サイドA")

    # ── 3. 中人数（5-8名）: サイドB連結（8+9）──
    if adults <= 8 and avail(8) and avail(9):
        return res([8, 9], 8, "8・9番連結（8席）サイドB")

    # ── 4. 大人数（9-14名）: VIPエリア連結（10+11）──
    if adults <= 14 and avail(10) and avail(11):
        return res([10, 11], 14, "10・11番連結（14席）VIPエリア")

    # ── 5. 大人数（〜12名）: メインフロア隣接2席連結 ──
    MAIN_PAIRS = [(1,2),(2,3),(3,4),(4,5),(1,3),(2,4),(3,5)]
    if adults <= 12:
        for t1, t2 in MAIN_PAIRS:
            if avail(t1) and avail(t2):
                cap = TABLES[t1]['capacity'] + TABLES[t2]['capacity']
                return res([t1, t2], cap, f"{t1}・{t2}番連結（{cap}席）")

    # ── 6. 超大人数（〜18名）: VIP + メイン組み合わせ ──
    if adults <= 18:
        for tm in [1, 3, 5, 2, 4]:
            for tv in [11, 10]:
                if avail(tm) and avail(tv):
                    cap = TABLES[tm]['capacity'] + TABLES[tv]['capacity']
                    if cap >= adults:
                        return res([tm, tv], cap,
                                   f"{tm}・{tv}番（{cap}席）※要レイアウト確認")

    # ── 7. 席不足 ──
    return res([], 0, "空き席が見つかりません（手動で確認してください）")

# ============================================================
# テーブル状況サマリー（日次ビュー用）
# ============================================================
def get_table_status(reservations):
    """テーブル番号 → 予約リストのマッピングを返す"""
    status = {t_id: [] for t_id in TABLES}
    for res in reservations:
        tables = _to_list(res.get('assigned_tables', []))
        for t in tables:
            if t in status:
                status[t].append({
                    'id':              res['id'],
                    'name':            res['name'],
                    'time_slot':       res['time_slot'],
                    'total_people':    res['total_people'],
                    'duration_minutes':res.get('duration_minutes', 105),
                    'is_vip':          res.get('is_vip', 0),
                    'end_time':        _end_time(res['time_slot'], res.get('duration_minutes', 105)),
                    'status':          res.get('status'),
                    'is_visited':      res.get('status') == 'visited',
                })
    return status

def _end_time(start_str, duration_min):
    try:
        end = _pt(start_str) + timedelta(minutes=int(duration_min))
        return end.strftime('%H:%M')
    except Exception:
        return '?'

# ============================================================
# 席マップ表示用（フォームのリアルタイムUI用）
# ============================================================
def get_seat_map_status(time_slot, duration, existing_list, exclude_id=None):
    """
    指定時間帯の各テーブルの状態を返す。
    フォームの席マップUIから呼ばれる。
    """
    occupied, blocked = _get_occupied_and_blocked(
        time_slot, int(duration), existing_list, exclude_id
    )
    return {
        'occupied': sorted(list(occupied)),
        'blocked':  sorted(list(blocked)),
    }

# ============================================================
# 顧客カルテ（履歴の自動蓄積・呼び出し）& 来店頻度・売上集計
# ============================================================
# 顧客の同一判定は電話番号（数字のみ正規化）を優先キーとし、
# 電話番号が短い・未入力の場合は氏名にフォールバックする。

def _normalize_phone(phone):
    """電話番号からハイフン・空白等を除去し数字のみ返す"""
    return re.sub(r'\D', '', phone or '')

def customer_key(res):
    """予約データから顧客の同一判定キーを生成"""
    digits = _normalize_phone(res.get('phone'))
    if len(digits) >= 8:
        return 'P:' + digits
    return 'N:' + (res.get('name') or '').strip()

def get_customer_history(name, phone, all_reservations, exclude_id=None, limit=20):
    """
    氏名・電話番号をキーに過去の来店履歴（新しい順）を検索する。
    予約フォーム・予約詳細画面から呼ばれる「顧客カルテ」の中核。
    """
    key = customer_key({'name': name, 'phone': phone})
    matches = [
        r for r in all_reservations
        if customer_key(r) == key and r.get('id') != exclude_id
    ]
    matches.sort(key=lambda r: (r['date'], r['time_slot']), reverse=True)

    total_sales = sum(r.get('sales_amount') or 0 for r in matches)
    return {
        'found':           len(matches) > 0,
        'visit_count':     len(matches),
        'last_visit_date': matches[0]['date'] if matches else None,
        'total_sales':     total_sales,
        'visits': [
            {
                'id':            r['id'],
                'date':          r['date'],
                'time_slot':     r['time_slot'],
                'total_people':  r['total_people'],
                'menu_note':     r.get('menu_note') or '',
                'notes':         r.get('notes') or '',
                'sales_amount':  r.get('sales_amount'),
                'status':        r.get('status'),
                'visited_at':    r.get('visited_at'),
            }
            for r in matches[:limit]
        ],
    }

def aggregate_customer_ranking(all_reservations, since_date=None):
    """
    顧客（電話番号 or 氏名）単位で来店回数・売上を集計する。
    分析・集計ページ、顧客CSVエクスポートから呼ばれる。
    """
    groups = {}
    for r in all_reservations:
        if since_date and r['date'] < since_date:
            continue
        key = groups.setdefault(customer_key(r), {
            'key': customer_key(r), 'name': r['name'], 'phone': r['phone'],
            'visit_count': 0, 'total_sales': 0,
            'first_visit_date': r['date'], 'last_visit_date': r['date'],
            'last_visited_at': None,
        })
        key['visit_count'] += 1
        key['total_sales'] += r.get('sales_amount') or 0
        if r['date'] >= key['last_visit_date']:
            key['last_visit_date'] = r['date']
            key['name']  = r['name']    # 直近の表記を採用
            key['phone'] = r['phone']
        if r['date'] < key['first_visit_date']:
            key['first_visit_date'] = r['date']
        # 実際に来店受付（チェックイン）した日時のうち最新のものを記録
        if r.get('visited_at') and (not key['last_visited_at'] or r['visited_at'] > key['last_visited_at']):
            key['last_visited_at'] = r['visited_at']

    today = date.today()
    customers = list(groups.values())
    for c in customers:
        last_dt = datetime.strptime(c['last_visit_date'], '%Y-%m-%d').date()
        c['days_since_last_visit'] = (today - last_dt).days
        c['avg_sales'] = round(c['total_sales'] / c['visit_count']) if c['visit_count'] else 0
    return customers

# ============================================================
# マジレジ（POS）売上 ⇔ 予約の自動突合
# ============================================================
def _time_diff_minutes(time_a, time_b):
    """'HH:MM' 同士の差（分）。分子: time_a - time_b"""
    try:
        return int((_pt(time_a) - _pt(time_b)).total_seconds() / 60)
    except Exception:
        return None

def match_pos_sale_groups(pos_groups: list, reservations_by_date: dict) -> list:
    """
    伝票単位に集計されたPOS売上グループを、既存予約と突合する。
    優先順位: ① 電話番号一致 ② 氏名一致 ③ 卓番号＋会計時刻の近接（±利用時間内）
    戻り値: 各グループに 'matched_reservation_id' と 'match_confidence' を付加したリスト
    """
    results = []
    for g in pos_groups:
        candidates = [r for r in reservations_by_date.get(g['sale_date'], []) if r.get('status') != 'cancelled']
        match = None
        confidence = None

        # ① 電話番号一致（最優先・最も確実）
        gphone = _normalize_phone(g.get('phone'))
        if len(gphone) >= 8:
            for r in candidates:
                if _normalize_phone(r.get('phone')) == gphone:
                    match, confidence = r, 'phone'
                    break

        # ② 氏名一致
        if not match and g.get('customer_name'):
            gname = g['customer_name'].strip()
            for r in candidates:
                if r.get('name', '').strip() == gname:
                    match, confidence = r, 'name'
                    break

        # ③ 卓番号＋会計時刻の近接（POSに顧客情報が無い場合の主要ルート）
        if not match and g.get('table_no') and g.get('sale_time'):
            try:
                tno = int(re.sub(r'\D', '', str(g['table_no'])) or 0)
            except (TypeError, ValueError):
                tno = None
            if tno:
                best, best_score = None, None
                for r in candidates:
                    if tno not in _to_list(r.get('assigned_tables', [])):
                        continue
                    diff = _time_diff_minutes(g['sale_time'], r['time_slot'])
                    if diff is None:
                        continue
                    dur = int(r.get('duration_minutes', 105))
                    # 入店15分前 〜 (利用時間+90分) の会計を候補とする
                    if -15 <= diff <= dur + 90:
                        score = abs(diff - dur)  # 想定退店タイミングに近いほど良い
                        if best is None or score < best_score:
                            best, best_score = r, score
                if best:
                    match, confidence = best, 'table_time'

        results.append({
            **g,
            'matched_reservation_id': match['id'] if match else None,
            'matched_reservation_name': match['name'] if match else None,
            'match_confidence': confidence,
        })
    return results
