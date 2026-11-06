# price_db.py — 基本の価格表（部門を問わない共通の仕組み）
#
# 品目（price_items）と、値段の履歴（price_history）を持つ。値段は上書きせず「いつから幾ら」を積み上げるので、
# 値上がりの流れが残り、過去の棚卸は当時の値段で計算できる。
# 今は「肉」だけが在庫・棚卸・原価とつながっている（part_id で meat_parts に結びつく）。
# ほかの部門は品目と値段だけを持つ。仕入・棚卸・原価を広げる時は、この表を土台にする。
from datetime import date
from sqlalchemy import text

from database import engine, _IS_PG, _now

MEAT_DEPARTMENT = '肉'
DEPARTMENTS = (MEAT_DEPARTMENT, 'ドリンク', '野菜・食材', '消耗品', 'その他')
SEED_EFFECTIVE_FROM = '2026-01-01'   # 初期値の適用日。使い始めの棚卸（2026-10）より前


def init_price_db():
    pk = 'id SERIAL PRIMARY KEY' if _IS_PG else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
    with engine.begin() as con:
        con.execute(text(f"""CREATE TABLE IF NOT EXISTS price_items (
            {pk},
            department     TEXT    NOT NULL,
            name           TEXT    NOT NULL,
            unit           TEXT    NOT NULL DEFAULT 'kg',
            supplier_name  TEXT,
            part_id        INTEGER UNIQUE,   -- 肉の部位（meat_parts）。ほかの部門は空
            sort_order     INTEGER NOT NULL DEFAULT 0,
            is_active      INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT    NOT NULL,
            UNIQUE (department, name)
        )"""))
        con.execute(text(f"""CREATE TABLE IF NOT EXISTS price_history (
            {pk},
            item_id         INTEGER NOT NULL,
            unit_price      INTEGER NOT NULL,
            effective_from  TEXT    NOT NULL,
            memo            TEXT,
            staff_name      TEXT,
            created_at      TEXT    NOT NULL,
            UNIQUE (item_id, effective_from)
        )"""))


def _check_price(price):
    try:
        value = float(str(price).replace(',', '').strip())
    except ValueError:
        raise ValueError('値段は数字で入れてください')
    if value <= 0 or value != int(value):
        raise ValueError('値段は1円以上の整数で入れてください')
    return int(value)


def _check_date(s):
    try:
        return date.fromisoformat(str(s).strip()).isoformat()
    except ValueError:
        raise ValueError('適用日を正しく入れてください')


def ensure_part_item(part_id, name, unit, supplier_name, sort_order, price):
    """肉の部位を価格表に載せる。既に載っていれば何もしない（人が変えた値段を初期値で戻さない）"""
    with engine.begin() as con:
        inserted = con.execute(text("""
            INSERT INTO price_items (department, name, unit, supplier_name, part_id, sort_order, created_at)
            VALUES (:d, :n, :u, :s, :p, :o, :t) ON CONFLICT DO NOTHING
        """), {'d': MEAT_DEPARTMENT, 'n': name, 'u': unit, 's': supplier_name, 'p': part_id, 'o': sort_order,
               't': _now()}).rowcount
        if inserted and price:
            item_id = con.execute(text('SELECT id FROM price_items WHERE part_id = :p'), {'p': part_id}).scalar()
            con.execute(text("""
                INSERT INTO price_history (item_id, unit_price, effective_from, memo, created_at)
                VALUES (:i, :u, :e, '初期値（在庫アプリの単価）', :t)
            """), {'i': item_id, 'u': price, 'e': SEED_EFFECTIVE_FROM, 't': _now()})


def add_item(department, name, unit, supplier_name, price, effective_from, staff):
    """品目を手で足す。肉は部位から自動で載るので、ここでは足さない（在庫・棚卸とつながらない行を作らないため）"""
    name, unit = (name or '').strip(), (unit or '').strip()
    if department not in DEPARTMENTS or department == MEAT_DEPARTMENT:
        raise ValueError('この部門には品目を足せません')
    if not name or not unit:
        raise ValueError('品名と単位を入れてください')
    price, effective_from = _check_price(price), _check_date(effective_from)
    with engine.begin() as con:
        order = con.execute(text('SELECT COALESCE(MAX(sort_order), 0) + 10 FROM price_items WHERE department = :d'),
                            {'d': department}).scalar()
        inserted = con.execute(text("""
            INSERT INTO price_items (department, name, unit, supplier_name, sort_order, created_at)
            VALUES (:d, :n, :u, :s, :o, :t) ON CONFLICT DO NOTHING
        """), {'d': department, 'n': name, 'u': unit, 's': (supplier_name or '').strip() or None, 'o': order,
               't': _now()}).rowcount
        if not inserted:
            raise ValueError(f'{name} は既に載っています')
        item_id = con.execute(text('SELECT id FROM price_items WHERE department = :d AND name = :n'),
                              {'d': department, 'n': name}).scalar()
    set_price(item_id, price, effective_from, staff)
    return item_id


def set_price(item_id, price, effective_from, staff, memo=''):
    """値段を決める。同じ適用日にもう一度入れたら、その日の値段を直す"""
    price, effective_from = _check_price(price), _check_date(effective_from)
    with engine.begin() as con:
        if not con.execute(text('SELECT id FROM price_items WHERE id = :i'), {'i': item_id}).scalar():
            raise ValueError('品目が見つかりません')
        con.execute(text("""
            INSERT INTO price_history (item_id, unit_price, effective_from, memo, staff_name, created_at)
            VALUES (:i, :u, :e, :m, :s, :t)
            ON CONFLICT (item_id, effective_from) DO UPDATE
            SET unit_price = :u, memo = :m, staff_name = :s, created_at = :t
        """), {'i': item_id, 'u': price, 'e': effective_from, 'm': (memo or '').strip() or None,
               's': staff, 't': _now()})


def list_items(department, today):
    """部門の品目一覧。current＝今の値段、previous＝その前の値段、upcoming＝これから変わる値段、history＝新しい順"""
    today = today.isoformat()
    with engine.begin() as con:
        items = [dict(r) for r in con.execute(text("""
            SELECT * FROM price_items WHERE department = :d AND is_active = 1 ORDER BY sort_order, id
        """), {'d': department}).mappings()]
        rows = [dict(r) for r in con.execute(text("""
            SELECT h.* FROM price_history h JOIN price_items i ON i.id = h.item_id
            WHERE i.department = :d ORDER BY h.effective_from DESC
        """), {'d': department}).mappings()]
    by_item = {}
    for r in rows:
        by_item.setdefault(r['item_id'], []).append(r)
    for it in items:
        it['history'] = by_item.get(it['id'], [])
        started = [h for h in it['history'] if h['effective_from'] <= today]
        future = [h for h in it['history'] if h['effective_from'] > today]
        it['current'] = started[0] if started else None
        it['previous'] = started[1] if len(started) > 1 else None
        it['upcoming'] = future[-1] if future else None   # 一番近い予定
    return items


def part_prices(before):
    """肉の部位ごとの値段 {part_id: 円}。before の日より前に始まった中で一番新しい値段"""
    with engine.begin() as con:
        rows = con.execute(text("""
            SELECT i.part_id, h.unit_price FROM price_history h JOIN price_items i ON i.id = h.item_id
            WHERE i.part_id IS NOT NULL AND h.effective_from < :b ORDER BY h.effective_from
        """), {'b': before}).all()
    return {r.part_id: r.unit_price for r in rows}
