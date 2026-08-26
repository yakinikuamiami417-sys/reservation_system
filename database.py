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
    d['special_tags']    = json.loads(d.get('special_tags',    '[]') or '[]')
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
            is_regular        INTEGER  NOT NULL DEFAULT 0,
            budget_per_person INTEGER,
            needs_type        TEXT,
            gender_male       INTEGER,
            gender_female     INTEGER,
            organizer_note    TEXT,
            notes             TEXT,
            special_tags      TEXT     NOT NULL DEFAULT '[]',
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

    # notes / menu_note / sales_amount / visited_at / is_deleted カラムのマイグレーション（既存DBへの追加列）
    new_columns = [
        ('notes',        'TEXT'),
        ('menu_note',    'TEXT'),
        ('sales_amount', 'INTEGER'),
        ('visited_at',   'TEXT'),   # 来店受付（チェックイン）した日時
        ('is_deleted',   'INTEGER NOT NULL DEFAULT 0'),  # 論理削除フラグ（ゴミ箱）
        ('deleted_at',   'TEXT'),   # ゴミ箱に移動した日時
        ('special_tags', "TEXT NOT NULL DEFAULT '[]'"),  # 特記事項タグ（妊婦・バースデー・アレルギー等）
        ('is_regular',   'INTEGER NOT NULL DEFAULT 0'),  # 常連フラグ（スタッフ手動設定）
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

    # ── マジレジ売上インポート用テーブル ──
    pos_id_col = 'id SERIAL PRIMARY KEY' if _IS_PG else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
    pos_ddl = f"""
        CREATE TABLE IF NOT EXISTS pos_sales (
            {pos_id_col},
            import_batch      TEXT     NOT NULL,
            sale_date         TEXT     NOT NULL,
            sale_time         TEXT,
            receipt_no        TEXT,
            table_no          TEXT,
            party_size        INTEGER,
            amount            INTEGER  NOT NULL DEFAULT 0,
            item_name         TEXT,
            customer_name     TEXT,
            phone             TEXT,
            payment_method    TEXT,
            receipt_key       TEXT     NOT NULL,
            matched_reservation_id INTEGER,
            match_status      TEXT     NOT NULL DEFAULT 'unmatched',
            raw_row           TEXT,
            created_at        TEXT     NOT NULL
        )
    """
    with engine.begin() as con:
        con.execute(text(pos_ddl))
        con.execute(text('CREATE INDEX IF NOT EXISTS idx_pos_date ON pos_sales(sale_date)'))
        con.execute(text('CREATE INDEX IF NOT EXISTS idx_pos_receipt_key ON pos_sales(receipt_key)'))
        con.execute(text('CREATE INDEX IF NOT EXISTS idx_pos_match_status ON pos_sales(match_status)'))

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
        'is_regular':        data.get('is_regular', 0),
        'budget_per_person': data.get('budget_per_person'),
        'needs_type':        data.get('needs_type'),
        'gender_male':       data.get('gender_male'),
        'gender_female':     data.get('gender_female'),
        'organizer_note':    data.get('organizer_note'),
        'notes':             data.get('notes'),
        'menu_note':         data.get('menu_note'),
        'sales_amount':      data.get('sales_amount'),
        'special_tags':      data['special_tags'] if isinstance(data.get('special_tags'), str) else json.dumps(data.get('special_tags') or [], ensure_ascii=False),
        'assigned_tables':   data['assigned_tables'] if isinstance(data['assigned_tables'], str) else json.dumps(data['assigned_tables']),
        'status':            data.get('status', 'confirmed'),
        'created_at':        now,
        'updated_at':        now,
    }
    sql = """
        INSERT INTO reservations
        (date, time_slot, name, phone, adults, children_info, total_people,
         duration_minutes, private_room, is_vip, is_group, is_regular,
         budget_per_person, needs_type, gender_male, gender_female, organizer_note,
         notes, menu_note, sales_amount, special_tags, assigned_tables, status, created_at, updated_at)
        VALUES (:date, :time_slot, :name, :phone, :adults, :children_info, :total_people,
         :duration_minutes, :private_room, :is_vip, :is_group, :is_regular,
         :budget_per_person, :needs_type, :gender_male, :gender_female, :organizer_note,
         :notes, :menu_note, :sales_amount, :special_tags, :assigned_tables, :status, :created_at, :updated_at)
    """
    if _IS_PG:
        sql += ' RETURNING id'
    with engine.begin() as con:
        result = con.execute(text(sql), params)
        return result.scalar() if _IS_PG else result.lastrowid


def get_reservations_by_date(target_date: str) -> list:
    with engine.connect() as con:
        rows = con.execute(
            text("SELECT * FROM reservations WHERE date=:date AND status!='cancelled' AND is_deleted=0 ORDER BY time_slot"),
            {"date": target_date}
        ).mappings().all()
    return [_row_to_dict(dict(r)) for r in rows]


def get_cancelled_reservations_by_date(target_date: str) -> list:
    """
    急なキャンセルの見落とし防止用：その日にキャンセルされた予約を
    キャンセルが新しい順（updated_at降順）で返す。
    """
    with engine.connect() as con:
        rows = con.execute(
            text("SELECT * FROM reservations WHERE date=:date AND status='cancelled' AND is_deleted=0 ORDER BY updated_at DESC"),
            {"date": target_date}
        ).mappings().all()
    return [_row_to_dict(dict(r)) for r in rows]


def get_all_reservations(include_cancelled: bool = False) -> list:
    """
    顧客カルテ検索・分析集計・全件エクスポート用に全予約を取得する。
    ゴミ箱（is_deleted=1）に入っているものは常に除外する。
    """
    sql = "SELECT * FROM reservations WHERE is_deleted=0"
    if not include_cancelled:
        sql += " AND status != 'cancelled'"
    sql += " ORDER BY date, time_slot"
    with engine.connect() as con:
        rows = con.execute(text(sql)).mappings().all()
    return [_row_to_dict(dict(r)) for r in rows]


def get_deleted_reservations() -> list:
    """ゴミ箱の中身（論理削除済みの予約）を削除が新しい順に返す。"""
    with engine.connect() as con:
        rows = con.execute(
            text("SELECT * FROM reservations WHERE is_deleted=1 ORDER BY deleted_at DESC")
        ).mappings().all()
    return [_row_to_dict(dict(r)) for r in rows]


def soft_delete_reservation(res_id: int):
    """予約をゴミ箱へ移動する（実データはDBに残るため復元可能）。"""
    with engine.begin() as con:
        con.execute(
            text("UPDATE reservations SET is_deleted=1, deleted_at=:now, updated_at=:now WHERE id=:id"),
            {"now": _now(), "id": res_id}
        )


def restore_reservation(res_id: int):
    """ゴミ箱から予約を復元する。"""
    with engine.begin() as con:
        con.execute(
            text("UPDATE reservations SET is_deleted=0, deleted_at=NULL, updated_at=:now WHERE id=:id"),
            {"now": _now(), "id": res_id}
        )


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
        'is_regular':        data.get('is_regular', 0),
        'budget_per_person': data.get('budget_per_person'),
        'needs_type':        data.get('needs_type'),
        'gender_male':       data.get('gender_male'),
        'gender_female':     data.get('gender_female'),
        'organizer_note':    data.get('organizer_note'),
        'notes':             data.get('notes'),
        'menu_note':         data.get('menu_note'),
        'sales_amount':      data.get('sales_amount'),
        'special_tags':      data['special_tags'] if isinstance(data.get('special_tags'), str) else json.dumps(data.get('special_tags') or [], ensure_ascii=False),
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
            is_vip=:is_vip, is_group=:is_group, is_regular=:is_regular,
            budget_per_person=:budget_per_person, needs_type=:needs_type,
            gender_male=:gender_male, gender_female=:gender_female,
            organizer_note=:organizer_note, notes=:notes,
            menu_note=:menu_note, sales_amount=:sales_amount, special_tags=:special_tags,
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


def apply_pos_sale_to_reservation(reservation_id: int, amount: int, items: list):
    """
    マジレジ売上を予約に反映する。売上金額はPOSを正として上書きし、
    メニューはスタッフが既に入力済みのメモを消さないよう追記する。
    """
    with engine.connect() as con:
        row = con.execute(
            text("SELECT menu_note FROM reservations WHERE id=:id"), {"id": reservation_id}
        ).mappings().fetchone()
    if row is None:
        return
    existing_note = (row['menu_note'] or '').strip()
    items_text = '、'.join(i for i in items if i)
    if items_text:
        merged_note = existing_note if items_text in existing_note else (
            f"{existing_note}\n[レジ] {items_text}" if existing_note else f"[レジ] {items_text}"
        )
    else:
        merged_note = existing_note or None

    with engine.begin() as con:
        con.execute(
            text("UPDATE reservations SET sales_amount=:amount, menu_note=:menu_note, updated_at=:now WHERE id=:id"),
            {"amount": amount, "menu_note": merged_note, "now": _now(), "id": reservation_id}
        )


# ============================================================
# マジレジ（POS）売上インポート
# ============================================================
def insert_pos_sales(rows: list) -> int:
    """
    パース済みのPOS売上行を一括登録する。rows は dict のリストで、
    import_batch/sale_date/sale_time/receipt_no/table_no/party_size/amount/
    item_name/customer_name/phone/payment_method/receipt_key/raw_row のキーを想定。
    戻り値: 登録件数
    """
    if not rows:
        return 0
    now = _now()
    params = []
    for r in rows:
        params.append({
            'import_batch':   r['import_batch'],
            'sale_date':      r['sale_date'],
            'sale_time':      r.get('sale_time'),
            'receipt_no':     r.get('receipt_no'),
            'table_no':       r.get('table_no'),
            'party_size':     r.get('party_size'),
            'amount':         r.get('amount') or 0,
            'item_name':      r.get('item_name'),
            'customer_name':  r.get('customer_name'),
            'phone':          r.get('phone'),
            'payment_method': r.get('payment_method'),
            'receipt_key':    r['receipt_key'],
            'match_status':   'unmatched',
            'raw_row':        json.dumps(r.get('raw_row') or {}, ensure_ascii=False),
            'created_at':     now,
        })
    sql = """
        INSERT INTO pos_sales
        (import_batch, sale_date, sale_time, receipt_no, table_no, party_size,
         amount, item_name, customer_name, phone, payment_method, receipt_key,
         match_status, raw_row, created_at)
        VALUES (:import_batch, :sale_date, :sale_time, :receipt_no, :table_no, :party_size,
         :amount, :item_name, :customer_name, :phone, :payment_method, :receipt_key,
         :match_status, :raw_row, :created_at)
    """
    with engine.begin() as con:
        con.execute(text(sql), params)
    return len(params)


def get_pos_sales(date_from: str = None, date_to: str = None, match_status: str = None) -> list:
    sql = "SELECT * FROM pos_sales WHERE 1=1"
    params = {}
    if date_from:
        sql += " AND sale_date >= :date_from"
        params['date_from'] = date_from
    if date_to:
        sql += " AND sale_date <= :date_to"
        params['date_to'] = date_to
    if match_status:
        sql += " AND match_status = :match_status"
        params['match_status'] = match_status
    sql += " ORDER BY sale_date DESC, sale_time DESC"
    with engine.connect() as con:
        rows = con.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


def set_pos_sale_group_match(receipt_key: str, reservation_id, match_status: str):
    """receipt_key を共有する行（同一伝票の明細）をまとめて紐付け更新する。"""
    with engine.begin() as con:
        con.execute(
            text("UPDATE pos_sales SET matched_reservation_id=:rid, match_status=:status WHERE receipt_key=:key"),
            {"rid": reservation_id, "status": match_status, "key": receipt_key}
        )
