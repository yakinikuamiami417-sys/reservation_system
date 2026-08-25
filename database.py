import os, json
from datetime import datetime
from sqlalchemy import create_engine, text

_URL = os.environ.get('DATABASE_URL', 'sqlite:///reservations.db')
if _URL.startswith('postgres://'):
    _URL = _URL.replace('postgres://', 'postgresql://', 1)

engine = create_engine(_URL, pool_pre_ping=True)
_IS_PG = _URL.startswith('postgresql')


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    d['children_info']   = json.loads(d.get('children_info',   '[]') or '[]')
    d['assigned_tables'] = json.loads(d.get('assigned_tables', '[]') or '[]')
    return d


def init_db():
    id_col = 'id SERIAL PRIMARY KEY' if _IS_PG else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
    ddl = f"""
        CREATE TABLE IF NOT EXISTS reservations (
            {id_col},
            date              TEXT     NOT NULL,
            time_slot         TEXT     NOT NULL,
            name              TEXT     NOT NULL,
            phone             TEXT     NOT NULL,
            adults            INTEGER  NOT NULL DEFAULT 1,
            children_info     TEXT     NOT NULL DEFAULT '[]',
            total_people      INTEGER  NOT NULL DEFAULT 1,
            duration_minutes  INTEGER  NOT NULL DEFAULT 105,
            private_room      INTEGER  NOT NULL DEFAULT 0,
            is_vip            INTEGER  NOT NULL DEFAULT 0,
            is_group          INTEGER  NOT NULL DEFAULT 0,
            budget_per_person INTEGER,
            needs_type        TEXT,
            gender_male       INTEGER,
            gender_female     INTEGER,
            organizer_note    TEXT,
            notes             TEXT,
            assigned_tables   TEXT     NOT NULL DEFAULT '[]',
            status            TEXT     NOT NULL DEFAULT 'confirmed',
            created_at        TEXT     NOT NULL,
            updated_at        TEXT     NOT NULL
        )
    """
    with engine.begin() as con:
        con.execute(text(ddl))
        con.execute(text('CREATE INDEX IF NOT EXISTS idx_res_date ON reservations(date)'))
        con.execute(text('CREATE INDEX IF NOT EXISTS idx_res_status ON reservations(status)'))

    # notes / menu_note / sales_amount / visited_at カラムのマイグレーション（既存DBへの追加列）
    new_columns = [
        ('notes',        'TEXT'),
        ('menu_note',    'TEXT'),
        ('sales_amount', 'INTEGER'),
        ('visited_at',   'TEXT'),   # 来店受付（チェックイン）した日時
    ]
    if _IS_PG:
        with engine.begin() as con:
            for col, col_type in new_columns:
                con.execute(text(f'ALTER TABLE reservations ADD COLUMN IF NOT EXISTS {col} {col_type}'))
    else:
        for col, col_type in new_columns:
            try:
                with engine.begin() as con:
                    con.execute(text(f'ALTER TABLE reservations ADD COLUMN {col} {col_type}'))
            except Exception:
                pass

    print("[OK] DB初期化完了")


def save_reservation(data: dict) -> int:
    now = _now()
    params = {
        'date':              data['date'],
        'time_slot':         data['time_slot'],
        'name':              data['name'],
        'phone':             data['phone'],
        'adults':            data['adults'],
        'children_info':     data['children_info'] if isinstance(data['children_info'], str) else json.dumps(data['children_info'], ensure_ascii=False),
        'total_people':      data['total_people'],
        'duration_minutes':  data['duration_minutes'],
        'private_room':      data['private_room'],
        'is_vip':            data['is_vip'],
        'is_group':          data['is_group'],
        'budget_per_person': data.get('budget_per_person'),
        'needs_type':        data.get('needs_type'),
        'gender_male':       data.get('gender_male'),
        'gender_female':     data.get('gender_female'),
        'organizer_note':    data.get('organizer_note'),
        'notes':             data.get('notes'),
        'menu_note':         data.get('menu_note'),
        'sales_amount':      data.get('sales_amount'),
        'assigned_tables':   data['assigned_tables'] if isinstance(data['assigned_tables'], str) else json.dumps(data['assigned_tables']),
        'status':            data.get('status', 'confirmed'),
        'created_at':        now,
        'updated_at':        now,
    }
    sql = """
        INSERT INTO reservations
        (date, time_slot, name, phone, adults, children_info, total_people,
         duration_minutes, private_room, is_vip, is_group,
         budget_per_person, needs_type, gender_male, gender_female, organizer_note,
         notes, menu_note, sales_amount, assigned_tables, status, created_at, updated_at)
        VALUES (:date, :time_slot, :name, :phone, :adults, :children_info, :total_people,
         :duration_minutes, :private_room, :is_vip, :is_group,
         :budget_per_person, :needs_type, :gender_male, :gender_female, :organizer_note,
         :notes, :menu_note, :sales_amount, :assigned_tables, :status, :created_at, :updated_at)
    """
    if _IS_PG:
        sql += ' RETURNING id'
    with engine.begin() as con:
        result = con.execute(text(sql), params)
        return result.scalar() if _IS_PG else result.lastrowid


def get_reservations_by_date(target_date: str) -> list:
    with engine.connect() as con:
        rows = con.execute(
            text("SELECT * FROM reservations WHERE date=:date AND status!='cancelled' ORDER BY time_slot"),
            {"date": target_date}
        ).mappings().all()
    return [_row_to_dict(dict(r)) for r in rows]


def get_all_reservations(include_cancelled: bool = False) -> list:
    """
    顧客カルテ検索・分析集計・全件エクスポート用に全予約を取得する。
    """
    sql = "SELECT * FROM reservations"
    if not include_cancelled:
        sql += " WHERE status != 'cancelled'"
    sql += " ORDER BY date, time_slot"
    with engine.connect() as con:
        rows = con.execute(text(sql)).mappings().all()
    return [_row_to_dict(dict(r)) for r in rows]


def get_reservation(res_id: int):
    with engine.connect() as con:
        row = con.execute(
            text("SELECT * FROM reservations WHERE id=:id"),
            {"id": res_id}
        ).mappings().fetchone()
    return _row_to_dict(dict(row)) if row else None


def update_reservation(res_id: int, data: dict):
    params = {
        'date':              data['date'],
        'time_slot':         data['time_slot'],
        'name':              data['name'],
        'phone':             data['phone'],
        'adults':            data['adults'],
        'children_info':     data['children_info'] if isinstance(data['children_info'], str) else json.dumps(data['children_info'], ensure_ascii=False),
        'total_people':      data['total_people'],
        'duration_minutes':  data['duration_minutes'],
        'private_room':      data['private_room'],
        'is_vip':            data['is_vip'],
        'is_group':          data['is_group'],
        'budget_per_person': data.get('budget_per_person'),
        'needs_type':        data.get('needs_type'),
        'gender_male':       data.get('gender_male'),
        'gender_female':     data.get('gender_female'),
        'organizer_note':    data.get('organizer_note'),
        'notes':             data.get('notes'),
        'menu_note':         data.get('menu_note'),
        'sales_amount':      data.get('sales_amount'),
        'assigned_tables':   data['assigned_tables'] if isinstance(data['assigned_tables'], str) else json.dumps(data['assigned_tables']),
        'status':            data.get('status', 'confirmed'),
        'updated_at':        _now(),
        'id':                res_id,
    }
    with engine.begin() as con:
        con.execute(text('''
            UPDATE reservations SET
            date=:date, time_slot=:time_slot, name=:name, phone=:phone,
            adults=:adults, children_info=:children_info, total_people=:total_people,
            duration_minutes=:duration_minutes, private_room=:private_room,
            is_vip=:is_vip, is_group=:is_group,
            budget_per_person=:budget_per_person, needs_type=:needs_type,
            gender_male=:gender_male, gender_female=:gender_female,
            organizer_note=:organizer_note, notes=:notes,
            menu_note=:menu_note, sales_amount=:sales_amount,
            assigned_tables=:assigned_tables, status=:status, updated_at=:updated_at
            WHERE id=:id
        '''), params)


def cancel_reservation(res_id: int):
    with engine.begin() as con:
        con.execute(
            text("UPDATE reservations SET status='cancelled', updated_at=:now WHERE id=:id"),
            {"now": _now(), "id": res_id}
        )


def set_reservation_status(res_id: int, status: str):
    """
    ステータスのみ更新（来店受付／来店取消のトグルなど）。
    'visited' に切り替えた瞬間の日時を visited_at に記録し、
    解除した場合は visited_at をクリアする（現在の状態を正しく反映するため）。
    """
    now = _now()
    visited_at = now if status == 'visited' else None
    with engine.begin() as con:
        con.execute(
            text("UPDATE reservations SET status=:status, visited_at=:visited_at, updated_at=:now WHERE id=:id"),
            {"status": status, "visited_at": visited_at, "now": now, "id": res_id}
        )
