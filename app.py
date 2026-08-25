# app.py — Flask メインアプリケーション
# キリン屋 予約管理システム
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, Response, session
import json, socket, time as _time, csv, io, os
from datetime import datetime, date, timedelta
from sqlalchemy import text

from database import (
    init_db, save_reservation, get_reservations_by_date,
    get_reservation, update_reservation, cancel_reservation, engine
)
from logic import assign_seats, check_time_conflict, get_table_status, get_seat_map_status, TABLES

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'kiriniya-dev-only-change-in-prod')

APP_PASSWORD = os.environ.get('APP_PASSWORD', '')

# ============================================================
# DB初期化（モジュール読み込み時に実行）
# gunicornは `if __name__ == '__main__':` を通らず app を直接読み込むため、
# ここで呼ばないと本番環境（Render等）でテーブルが作成されない
# ============================================================
try:
    init_db()
except Exception as e:
    print(f"[WARN] DB初期化に失敗しました: {e}")

@app.before_request
def require_auth():
    public = {'login', 'static', 'api_last_change'}
    if not APP_PASSWORD or request.endpoint in public or session.get('ok'):
        return
    return redirect(url_for('login', next=request.path))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('pw') == APP_PASSWORD:
            session['ok'] = True
            return redirect(request.args.get('next') or url_for('index'))
        return render_template('login.html', error=True)
    return render_template('login.html', error=False)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ============================================================
# リアルタイム同期サポート
# ============================================================
_CHANGE_FILE = 'last_change.txt'

def _notify_change(date_str: str):
    """予約変更をファイルに記録 → 他端末がポーリングで検知する"""
    try:
        with open(_CHANGE_FILE, 'w', encoding='utf-8') as f:
            f.write(f"{date_str}|{_time.time()}")
    except Exception:
        pass

def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

LOCAL_IP = _get_local_ip()
app.jinja_env.globals['local_ip'] = LOCAL_IP

# ============================================================
# ヘルパー
# ============================================================
WEEKDAYS_JP = ['月', '火', '水', '木', '金', '土', '日']

def jp_date(dt_obj) -> str:
    """date → '2026年5月26日（火）'"""
    if isinstance(dt_obj, str):
        dt_obj = datetime.strptime(dt_obj, '%Y-%m-%d').date()
    return f"{dt_obj.year}年{dt_obj.month}月{dt_obj.day}日（{WEEKDAYS_JP[dt_obj.weekday()]}）"

def get_time_slots():
    """11:00〜22:00 を15分刻みで返す"""
    slots = []
    cur = datetime.strptime('11:00', '%H:%M')
    end = datetime.strptime('22:00', '%H:%M')
    while cur <= end:
        slots.append(cur.strftime('%H:%M'))
        cur += timedelta(minutes=15)
    return slots

DURATION_OPTIONS = [
    (90,  '1時間30分'),
    (105, '1時間45分（標準）'),
    (120, '2時間'),
    (150, '2時間30分'),
    (180, '3時間'),
]

# Jinja2グローバル関数
app.jinja_env.globals['jp_date'] = jp_date

# ============================================================
# ルート: トップ（日次一覧）
# ============================================================
@app.route('/')
def index():
    target_date = request.args.get('date', date.today().isoformat())
    try:
        target_dt = datetime.strptime(target_date, '%Y-%m-%d').date()
    except ValueError:
        target_dt   = date.today()
        target_date = target_dt.isoformat()

    reservations = get_reservations_by_date(target_date)
    table_status = get_table_status(reservations)

    return render_template(
        'index.html',
        reservations = reservations,
        target_date  = target_date,
        target_dt    = target_dt,
        date_display = jp_date(target_dt),
        prev_date    = (target_dt - timedelta(days=1)).isoformat(),
        next_date    = (target_dt + timedelta(days=1)).isoformat(),
        table_status = table_status,
        tables       = TABLES,
        today        = date.today().isoformat(),
    )

# ============================================================
# ルート: 新規予約
# ============================================================
@app.route('/reservation/new', methods=['GET', 'POST'])
def new_reservation():
    if request.method == 'POST':
        res_dict, err = _parse_form(request)
        if err:
            flash(f'⚠️ 入力エラー: {err}', 'danger')
            return redirect(request.url)

        existing = get_reservations_by_date(res_dict['date'])

        # 入店時間競合チェック
        conflicts = check_time_conflict(res_dict, existing)
        if conflicts and not res_dict['is_vip']:
            names = '、'.join(r['name'] for r in conflicts)
            flash(f"⚠️ 入店時間警告: {names} 様と15分以内の入店になります（VIPフラグでスキップ可）", 'warning')

        # 席の確定（スタッフが席マップで選択した場合はそれを優先）
        confirmed = _parse_confirmed_tables(request.form.get('confirmed_tables', '[]'))
        if confirmed:
            res_dict['assigned_tables'] = json.dumps(confirmed)
            seat_note = '・'.join(str(t) + '番' for t in confirmed) + '（スタッフ確定）'
        else:
            # 未選択の場合はシステム提案を自動使用
            suggestion = assign_seats(res_dict, existing)
            res_dict['assigned_tables'] = json.dumps(suggestion['tables'])
            seat_note = suggestion['note'] + '（自動）'

        save_reservation(res_dict)
        flash(f"✅ 予約登録完了 ｜ {res_dict['name']} 様 ／ {seat_note}", 'success')
        return redirect(url_for('index', date=res_dict['date']))

    default_date = request.args.get('date', date.today().isoformat())
    return render_template(
        'form.html',
        mode             = 'new',
        reservation      = None,
        time_slots       = get_time_slots(),
        duration_options = DURATION_OPTIONS,
        default_date     = default_date,
        tables           = TABLES,
    )

# ============================================================
# ルート: 予約詳細
# ============================================================
@app.route('/reservation/<int:rid>')
def view_reservation(rid):
    res = get_reservation(rid)
    if not res:
        flash('予約が見つかりません', 'danger')
        return redirect(url_for('index'))
    return render_template('detail.html', reservation=res, tables=TABLES)

# ============================================================
# ルート: 予約編集
# ============================================================
@app.route('/reservation/<int:rid>/edit', methods=['GET', 'POST'])
def edit_reservation(rid):
    res = get_reservation(rid)
    if not res:
        flash('予約が見つかりません', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        updated, err = _parse_form(request, edit_id=rid)
        if err:
            flash(f'⚠️ 入力エラー: {err}', 'danger')
            return redirect(request.url)

        # 自分以外の予約で競合チェック
        existing = [r for r in get_reservations_by_date(updated['date']) if r['id'] != rid]
        conflicts = check_time_conflict(updated, existing)
        if conflicts and not updated['is_vip']:
            names = '、'.join(r['name'] for r in conflicts)
            flash(f"⚠️ 入店時間警告: {names} 様と15分以内の入店になります", 'warning')

        # 席の確定（スタッフ選択 or 自動提案）
        confirmed = _parse_confirmed_tables(request.form.get('confirmed_tables', '[]'))
        if confirmed:
            updated['assigned_tables'] = json.dumps(confirmed)
            seat_note = '・'.join(str(t) + '番' for t in confirmed) + '（スタッフ確定）'
        else:
            suggestion = assign_seats(updated, existing)
            updated['assigned_tables'] = json.dumps(suggestion['tables'])
            seat_note = suggestion['note'] + '（自動）'
        updated['status'] = res['status']

        update_reservation(rid, updated)
        flash(f"✅ 予約を更新しました ｜ {seat_note}", 'success')
        return redirect(url_for('view_reservation', rid=rid))

    return render_template(
        'form.html',
        mode             = 'edit',
        reservation      = res,
        time_slots       = get_time_slots(),
        duration_options = DURATION_OPTIONS,
        default_date     = res['date'],
        tables           = TABLES,
    )

# ============================================================
# ルート: キャンセル
# ============================================================
@app.route('/reservation/<int:rid>/cancel', methods=['POST'])
def cancel_route(rid):
    res = get_reservation(rid)
    if res:
        cancel_reservation(rid)
        flash(f"🗑️ {res['name']} 様の予約をキャンセルしました", 'info')
        return redirect(url_for('index', date=res['date']))
    return redirect(url_for('index'))

# ============================================================
# ルート: タイムテーブル
# ============================================================
OPEN_HOUR   = 11    # タイムテーブル表示開始
CLOSE_HOUR  = 23    # タイムテーブル表示終了
PX_PER_MIN  = 2     # 通常タイム 1分あたりのピクセル数
ROTATION_WARN_GAP_DANGER  = 15
ROTATION_WARN_GAP_CAUTION = 30

# インターバル圧縮（ランチ〜ディナーの準備時間を短縮表示）
LUNCH_END_HOUR      = 14    # ランチ閉店・インターバル開始
DINNER_START_HOUR   = 17    # ディナー開店・インターバル終了
BREAK_COMPRESSED_PX = 40    # インターバルの表示高さ（px）

# スケジュールマーカー定義（開店・OS・閉店ライン）
SCHEDULE_MARKERS = [
    {'label': 'ランチ開店', 'hour': 11, 'min': 30, 'color': '#2ec4b6', 'dashed': False, 'below': False},
    {'label': 'ランチOS',   'hour': 13, 'min': 30, 'color': '#ff9800', 'dashed': True,  'below': False},
    {'label': 'ディナーOS', 'hour': 22, 'min': 0,  'color': '#ff9800', 'dashed': True,  'below': False},
    {'label': 'ディナー閉店','hour': 23, 'min': 0,  'color': '#e63946', 'dashed': False, 'below': False},
]

@app.route('/timetable')
def timetable():
    target_date = request.args.get('date', date.today().isoformat())
    try:
        target_dt = datetime.strptime(target_date, '%Y-%m-%d').date()
    except ValueError:
        target_dt   = date.today()
        target_date = target_dt.isoformat()

    reservations = get_reservations_by_date(target_date)

    # ── インターバル圧縮 px変換 ──
    lunch_end_min    = (LUNCH_END_HOUR   - OPEN_HOUR) * 60   # 180
    dinner_start_min = (DINNER_START_HOUR - OPEN_HOUR) * 60  # 360
    break_duration   = dinner_start_min - lunch_end_min       # 180
    break_start_px   = lunch_end_min * PX_PER_MIN             # 360
    break_end_px     = break_start_px + BREAK_COMPRESSED_PX   # 400

    def min_to_px(m):
        if m <= lunch_end_min:
            return round(m * PX_PER_MIN)
        elif m <= dinner_start_min:
            t = (m - lunch_end_min) / break_duration
            return round(break_start_px + t * BREAK_COMPRESSED_PX)
        else:
            return round(break_end_px + (m - dinner_start_min) * PX_PER_MIN)

    total_minutes = (CLOSE_HOUR - OPEN_HOUR) * 60
    total_height  = min_to_px(total_minutes)

    # ── テーブルごとに回転情報を集計 ──
    table_data = {}
    for table_id in TABLES:
        res_list = [r for r in reservations if table_id in r.get('assigned_tables', [])]
        res_list.sort(key=lambda r: r['time_slot'])

        rotations = []
        for i, res in enumerate(res_list):
            sh, sm_h = map(int, res['time_slot'].split(':'))
            start_min = (sh - OPEN_HOUR) * 60 + sm_h
            end_min   = start_min + int(res['duration_minutes'])
            top_px    = min_to_px(start_min)
            height_px = max(min_to_px(end_min) - top_px, 36)

            # 直前回転との間隔
            gap_min = None
            if i > 0:
                prev = rotations[-1]
                gap_min = start_min - prev['_end_min']

            rotation_num = i + 1
            gap_danger  = gap_min is not None and 0 <= gap_min < ROTATION_WARN_GAP_DANGER
            gap_caution = gap_min is not None and ROTATION_WARN_GAP_DANGER <= gap_min < ROTATION_WARN_GAP_CAUTION
            rotations.append({
                **res,
                'rotation_num': rotation_num,
                'top_px':       top_px,
                'height_px':    height_px,
                'gap_min':      gap_min,
                'warn_3rd':     rotation_num >= 3,
                'warn_gap':     gap_danger or gap_caution,
                'gap_danger':   gap_danger,
                'gap_caution':  gap_caution,
                '_end_min':     end_min,
            })
        table_data[table_id] = rotations

    # ── 時刻ラベル（30分刻み、インターバル内省略）──
    time_labels = []
    for h in range(OPEN_HOUR, CLOSE_HOUR + 1):
        for mt in (0, 30):
            if h == CLOSE_HOUR and mt > 0:
                break
            from_open = (h - OPEN_HOUR) * 60 + mt
            if lunch_end_min < from_open < dinner_start_min:
                continue   # インターバル内はスキップ
            time_labels.append({
                'label':   f'{h:02d}:{mt:02d}',
                'top_px':  min_to_px(from_open),
                'is_hour': mt == 0,
            })

    # ── グリッド線（15分ごと、インターバル内省略）──
    grid_lines = []
    for slot in range(0, total_minutes, 15):
        if lunch_end_min < slot < dinner_start_min:
            continue
        grid_lines.append({
            'top_px':  min_to_px(slot),
            'is_hour': slot % 60 == 0,
        })

    # ── スケジュールマーカー（px位置を付加）──
    schedule_markers = []
    for sm_def in SCHEDULE_MARKERS:
        from_open = (sm_def['hour'] - OPEN_HOUR) * 60 + sm_def['min']
        schedule_markers.append({**sm_def, 'top_px': min_to_px(from_open)})

    # ── 警告リスト ──
    warnings = []
    for tid, rots in table_data.items():
        for r in rots:
            if r['warn_3rd']:
                warnings.append({
                    'type': '3rd',
                    'msg':  f"{tid}番テーブル ｜ {r['name']} 様 {r['time_slot']}〜 が3回転目です",
                })
            if r['gap_danger']:
                warnings.append({
                    'type': 'danger',
                    'msg':  f"{tid}番テーブル ｜ {r['name']} 様 {r['time_slot']}〜 の前の回転との間隔が {r['gap_min']} 分です（15分未満・要確認）",
                })
            elif r['gap_caution']:
                warnings.append({
                    'type': 'caution',
                    'msg':  f"{tid}番テーブル ｜ {r['name']} 様 {r['time_slot']}〜 の前の回転との間隔が {r['gap_min']} 分です（15〜30分・注意）",
                })

    return render_template(
        'timetable.html',
        target_date      = target_date,
        target_dt        = target_dt,
        date_display     = jp_date(target_dt),
        prev_date        = (target_dt - timedelta(days=1)).isoformat(),
        next_date        = (target_dt + timedelta(days=1)).isoformat(),
        today            = date.today().isoformat(),
        tables           = TABLES,
        table_data       = table_data,
        time_labels      = time_labels,
        grid_lines       = grid_lines,
        total_height     = total_height,
        total_minutes    = total_minutes,
        warnings         = warnings,
        px_per_min       = PX_PER_MIN,
        open_hour        = OPEN_HOUR,
        close_hour       = CLOSE_HOUR,
        reservations     = reservations,
        schedule_markers = schedule_markers,
        break_start_px   = break_start_px,
        break_end_px     = break_end_px,
        lunch_end_min    = lunch_end_min,
        dinner_start_min = dinner_start_min,
    )

# ============================================================
# ルート: 印刷専用（ランチ・ディナー・サマリー 各1ページ）
# ============================================================
@app.route('/timetable/print')
def timetable_print_view():
    target_date = request.args.get('date', date.today().isoformat())
    try:
        target_dt = datetime.strptime(target_date, '%Y-%m-%d').date()
    except ValueError:
        target_dt = date.today(); target_date = target_dt.isoformat()

    reservations = get_reservations_by_date(target_date)

    lunch_end_min    = (LUNCH_END_HOUR   - OPEN_HOUR) * 60
    dinner_start_min = (DINNER_START_HOUR - OPEN_HOUR) * 60
    break_duration   = dinner_start_min - lunch_end_min
    break_start_px   = lunch_end_min * PX_PER_MIN
    break_end_px     = break_start_px + BREAK_COMPRESSED_PX

    def mtp(m):
        if m <= lunch_end_min:
            return round(m * PX_PER_MIN)
        elif m <= dinner_start_min:
            return round(break_start_px + (m - lunch_end_min) / break_duration * BREAK_COMPRESSED_PX)
        else:
            return round(break_end_px + (m - dinner_start_min) * PX_PER_MIN)

    total_minutes = (CLOSE_HOUR - OPEN_HOUR) * 60
    total_h       = mtp(total_minutes)

    # テーブルデータ（メインと同じ計算）
    table_data = {}
    for table_id in TABLES:
        res_list = [r for r in reservations if table_id in r.get('assigned_tables', [])]
        res_list.sort(key=lambda r: r['time_slot'])
        rotations = []
        for i, res in enumerate(res_list):
            sh, sm_h  = map(int, res['time_slot'].split(':'))
            start_min = (sh - OPEN_HOUR) * 60 + sm_h
            end_min   = start_min + int(res['duration_minutes'])
            top_px    = mtp(start_min)
            height_px = max(mtp(end_min) - top_px, 36)
            gap_min   = (start_min - rotations[-1]['_end_min']) if i > 0 else None
            rn        = i + 1
            gd        = gap_min is not None and 0 <= gap_min < ROTATION_WARN_GAP_DANGER
            gc        = gap_min is not None and ROTATION_WARN_GAP_DANGER <= gap_min < ROTATION_WARN_GAP_CAUTION
            rotations.append({**res, 'rotation_num': rn, 'top_px': top_px,
                               'height_px': height_px, 'gap_min': gap_min,
                               'warn_3rd': rn >= 3, 'gap_danger': gd, 'gap_caution': gc,
                               '_end_min': end_min})
        table_data[table_id] = rotations

    # ── ランチ（11:00〜14:00） ──
    lunch_h = break_start_px
    lunch_labels = [
        {'label': f'{h:02d}:{mt:02d}', 'top_px': mtp((h-OPEN_HOUR)*60+mt), 'is_hour': mt==0}
        for h in range(OPEN_HOUR, LUNCH_END_HOUR+1)
        for mt in (0, 30)
        if not (h == LUNCH_END_HOUR and mt > 0)
    ]
    lunch_grid = [{'top_px': mtp(s), 'is_hour': s%60==0}
                  for s in range(0, lunch_end_min+1, 15)]
    lunch_markers = [
        {'label': 'ランチ開店', 'top_px': mtp(30),    'color': '#2ec4b6', 'dashed': False, 'above': False},
        {'label': 'ランチOS',   'top_px': mtp(150),   'color': '#ff9800', 'dashed': True,  'above': True},
        {'label': 'ランチ閉店', 'top_px': lunch_h,    'color': '#e63946', 'dashed': False, 'above': True},
    ]
    lunch_tdata = {t: [r for r in rots if r['top_px'] < lunch_h]
                   for t, rots in table_data.items()}

    # ── ディナー（17:00〜23:00） ──
    off       = break_end_px
    dinner_h  = total_h - off
    dinner_labels = [
        {'label': f'{h:02d}:{mt:02d}', 'top_px': mtp((h-OPEN_HOUR)*60+mt)-off, 'is_hour': mt==0}
        for h in range(DINNER_START_HOUR, CLOSE_HOUR+1)
        for mt in (0, 30)
        if not (h == CLOSE_HOUR and mt > 0)
    ]
    dinner_grid = [{'top_px': mtp(s)-off, 'is_hour': s%60==0}
                   for s in range(dinner_start_min, total_minutes+1, 15)]
    dinner_markers = [
        {'label': 'ディナー開店', 'top_px': 0,                              'color': '#2ec4b6', 'dashed': False, 'above': False},
        {'label': 'ディナーOS',   'top_px': mtp((22-OPEN_HOUR)*60)-off,    'color': '#ff9800', 'dashed': True,  'above': True},
        {'label': 'ディナー閉店', 'top_px': dinner_h,                       'color': '#e63946', 'dashed': False, 'above': True},
    ]
    dinner_tdata = {
        t: [dict(r, top_px=r['top_px']-off) for r in rots if r['top_px'] >= off]
        for t, rots in table_data.items()
    }

    # 警告
    warnings = []
    for tid, rots in table_data.items():
        for r in rots:
            if r['warn_3rd']:
                warnings.append({'type': '3rd',     'msg': f"{tid}番 {r['name']} 様 {r['time_slot']}〜 が3回転目"})
            if r['gap_danger']:
                warnings.append({'type': 'danger',  'msg': f"{tid}番 {r['name']} 様 間隔{r['gap_min']}分（15分未満）"})
            elif r['gap_caution']:
                warnings.append({'type': 'caution', 'msg': f"{tid}番 {r['name']} 様 間隔{r['gap_min']}分（15〜30分）"})

    col_w = 92  # A4横: 1063px - 44px軸 = 1019px ÷ 11列 ≈ 92px

    # ── 飛び込み客記入欄の行数（最大40組基準） ──
    MAX_GROUPS       = 40
    total_reserved   = len(reservations)
    walk_in_slots    = max(0, MAX_GROUPS - total_reserved)
    lunch_res_count  = sum(1 for r in reservations
                           if int(r['time_slot'].split(':')[0]) < LUNCH_END_HOUR)
    dinner_res_count = sum(1 for r in reservations
                           if int(r['time_slot'].split(':')[0]) >= DINNER_START_HOUR)
    lunch_walkin  = max(5, min(12, round(walk_in_slots * 0.4)))
    dinner_walkin = max(8, min(15, round(walk_in_slots * 0.6)))
    if lunch_walkin + dinner_walkin > 25:
        dinner_walkin = 25 - lunch_walkin

    return render_template('timetable_print.html',
        date_display     = jp_date(target_dt),
        tables           = TABLES,
        col_w            = col_w,
        lunch_h          = lunch_h,
        lunch_labels     = lunch_labels,
        lunch_grid       = lunch_grid,
        lunch_markers    = lunch_markers,
        lunch_tdata      = lunch_tdata,
        dinner_h         = dinner_h,
        dinner_labels    = dinner_labels,
        dinner_grid      = dinner_grid,
        dinner_markers   = dinner_markers,
        dinner_tdata     = dinner_tdata,
        table_data       = table_data,
        reservations     = reservations,
        warnings         = warnings,
        lunch_res_count  = lunch_res_count,
        dinner_res_count = dinner_res_count,
        lunch_walkin     = lunch_walkin,
        dinner_walkin    = dinner_walkin,
        max_groups       = MAX_GROUPS,
    )

# ============================================================
# API: リアルタイム競合チェック & 席提案
# ============================================================
@app.route('/api/check', methods=['POST'])
def api_check():
    """フォームからAJAXで呼ばれる。競合と席提案を返す。"""
    data = request.get_json() or {}
    target_date = data.get('date', '')
    time_slot   = data.get('time_slot', '')

    if not target_date or not time_slot:
        return jsonify({'conflicts': [], 'seat_suggestion': {'tables': [], 'note': '日付・時間を入力してください'}})

    existing = get_reservations_by_date(target_date)
    edit_id  = data.get('edit_id')
    if edit_id:
        existing = [r for r in existing if r['id'] != int(edit_id)]

    excl = int(edit_id) if edit_id else None
    conflicts  = check_time_conflict(data, existing)
    suggestion = assign_seats(data, existing)
    seat_map   = get_seat_map_status(
        time_slot,
        data.get('duration_minutes', 105),
        existing,
        excl
    )

    return jsonify({
        'conflicts':      [{'name': r['name'], 'time_slot': r['time_slot']} for r in conflicts],
        'seat_suggestion': suggestion,
        'seat_map':        seat_map,   # 使用中・ブロック中テーブルID一覧
    })

# ============================================================
# API: 予約1件取得（モーダル用）
# ============================================================
@app.route('/api/reservation/<int:rid>')
def api_get_reservation(rid):
    res = get_reservation(rid)
    if not res:
        return jsonify({'error': '見つかりません'}), 404
    # assigned_tables / children_info はすでにリスト型で返ってくる
    return jsonify(res)


# ============================================================
# API: クイック編集（時間・席・キャンセル）
# ============================================================
@app.route('/api/reservation/<int:rid>/quick-edit', methods=['POST'])
def api_quick_edit(rid):
    data = request.get_json() or {}
    res  = get_reservation(rid)
    if not res:
        return jsonify({'ok': False, 'error': '予約が見つかりません'}), 404

    # キャンセル
    if data.get('action') == 'cancel':
        cancel_reservation(rid)
        return jsonify({'ok': True, 'action': 'cancelled'})

    # 時間 / 席を更新
    updated = dict(res)
    if 'time_slot' in data:
        updated['time_slot'] = data['time_slot']
    if 'assigned_tables' in data:
        updated['assigned_tables'] = json.dumps(data['assigned_tables'])

    # children_info がリスト型なら JSON 文字列に戻す
    if isinstance(updated.get('children_info'), list):
        updated['children_info'] = json.dumps(updated['children_info'], ensure_ascii=False)

    update_reservation(rid, updated)
    return jsonify({'ok': True, 'action': 'updated'})


# ============================================================
# API: クイック予約追加（タイムテーブルから直接作成）
# ============================================================
@app.route('/api/reservation/quick-add', methods=['POST'])
def api_quick_add():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': '氏名は必須です'}), 400

    adults = max(1, int(data.get('adults', 2)))
    tables = [int(t) for t in (data.get('assigned_tables') or [])]

    res_dict = {
        'date':             data.get('date', date.today().isoformat()),
        'time_slot':        data.get('time_slot', '18:00'),
        'name':             name,
        'phone':            (data.get('phone') or '').strip() or '-',
        'adults':           adults,
        'children_info':    '[]',
        'total_people':     adults,
        'duration_minutes': int(data.get('duration_minutes', 105)),
        'private_room':     0,
        'is_vip':           0,
        'is_group':         1 if adults >= 8 else 0,
        'budget_per_person': None,
        'needs_type':       None,
        'gender_male':      None,
        'gender_female':    None,
        'organizer_note':   None,
        'notes':            (data.get('notes') or '').strip() or None,
        'assigned_tables':  json.dumps(tables),
        'status':           'confirmed',
    }

    rid = save_reservation(res_dict)
    return jsonify({'ok': True, 'id': rid})


# ============================================================
# バックアップ: CSV全件エクスポート
# ============================================================
@app.route('/backup/csv')
def backup_csv():
    """全予約データをCSVでダウンロード"""
    with engine.connect() as con:
        rows = con.execute(
            text("SELECT * FROM reservations ORDER BY date, time_slot")
        ).mappings().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'ID', '日付', '時間', '氏名', '電話番号',
        '大人', '子供', '合計人数', '利用時間(分)',
        '個室', 'VIP', '団体', '予算/人', 'ニーズ',
        '男性', '女性', '幹事メモ', '備考',
        'テーブル', 'ステータス', '登録日時', '更新日時',
    ])
    for r in rows:
        d = dict(r)
        writer.writerow([
            d['id'], d['date'], d['time_slot'], d['name'], d['phone'],
            d['adults'],
            len(json.loads(d['children_info'] or '[]')),
            d['total_people'], d['duration_minutes'],
            '○' if d['private_room'] else '',
            '○' if d['is_vip'] else '',
            '○' if d['is_group'] else '',
            d['budget_per_person'] or '',
            d['needs_type'] or '',
            d['gender_male'] or '', d['gender_female'] or '',
            d['organizer_note'] or '', d['notes'] or '',
            d['assigned_tables'], d['status'],
            d['created_at'], d['updated_at'],
        ])

    fname = f"kiriniya_backup_{date.today().isoformat()}.csv"
    return Response(
        '﻿' + output.getvalue(),
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )


# ============================================================
# API: 同期ポーリング用（最終更新タイムスタンプを返す）
# ============================================================
@app.route('/api/last-change')
def api_last_change():
    """予約テーブルの最終更新日時をDBから直接取得"""
    try:
        with engine.connect() as con:
            ts = con.execute(text("SELECT MAX(updated_at) FROM reservations")).scalar()
        return jsonify({'ts': ts or ''})
    except Exception:
        return jsonify({'ts': ''})


# ============================================================
# ページ: 接続情報（タブレット共有用 QR・URL）
# ============================================================
@app.route('/share')
def share_page():
    return render_template('share.html', local_ip=LOCAL_IP)


@app.route('/init-db')
def init_db_route():
    init_db()
    return '✅ DB初期化完了'


# ============================================================
# フォームパーサー
# ============================================================
def _parse_form(req, edit_id=None) -> tuple[dict, str | None]:
    """request.form → dict 変換。エラー時は (None, エラーメッセージ)"""
    f = req.form
    try:
        child_ages    = req.form.getlist('child_age[]')
        children_info = [{"age": int(a)} for a in child_ages if a.strip().isdigit()]
        adults        = int(f.get('adults', 1))
        total         = adults + len(children_info)

        d = {
            'date':              f['date'],
            'time_slot':         f['time_slot'],
            'name':              f['name'].strip(),
            'phone':             f['phone'].strip(),
            'adults':            adults,
            'children_info':     json.dumps(children_info, ensure_ascii=False),
            'total_people':      total,
            'duration_minutes':  int(f.get('duration_minutes', 105)),
            'private_room':      1 if 'private_room' in f else 0,
            'is_vip':            1 if 'is_vip' in f else 0,
            'is_group':          1 if total >= 8 else 0,
            'budget_per_person': int(f['budget_per_person']) if f.get('budget_per_person', '').strip() else None,
            'needs_type':        f.get('needs_type') or None,
            'gender_male':       int(f['gender_male'])   if f.get('gender_male',   '').strip().isdigit() else None,
            'gender_female':     int(f['gender_female'])  if f.get('gender_female', '').strip().isdigit() else None,
            'organizer_note':    f.get('organizer_note', '').strip() or None,
            'notes':             f.get('notes', '').strip() or None,
            'assigned_tables':   '[]',
            'status':            'confirmed',
        }
        if edit_id:
            d['id'] = edit_id

        if not d['name'] or not d['phone']:
            return None, '氏名・電話番号は必須です'
        return d, None
    except (KeyError, ValueError) as e:
        return None, str(e)

def _parse_confirmed_tables(raw: str) -> list[int]:
    """フォームの confirmed_tables フィールドをパース。"""
    try:
        data = json.loads(raw or '[]')
        return [int(t) for t in data if str(t).isdigit() or isinstance(t, int)]
    except Exception:
        return []

# ============================================================
# エントリポイント
# ============================================================
if __name__ == '__main__':
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    init_db()
    print()
    print("=" * 50)
    print("  Kirin-ya Yoyaku System  Start")
    print("=" * 50)
    print("  http://localhost:5000")
    print("  Ctrl+C de teishi")
    print()
    app.run(debug=True, port=5000, host='0.0.0.0', threaded=True)
