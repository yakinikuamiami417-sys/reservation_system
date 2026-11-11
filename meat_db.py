# meat_db.py — 正肉の仕入・在庫管理（テーブル定義と計算）
# 設計書: docs/正肉在庫_設計書.md
#
# 1塊＝1ロット（meat_lots）として 業者預け → 納品 → 商品化 → 使い切り を追いかける。
# 業者預け（held）は伝票前なので仕入にも棚卸にも含めない。
# 月次原価: 今期使用分 ＝ 前繰（前月の確定棚卸）＋ 今期仕入（伝票日付が当月）－ 期末棚卸
#           原価 ＝ 今期使用分 ÷ 出数
import json, os, re, unicodedata
from datetime import date, datetime, timedelta
from sqlalchemy import text

import price_db
from database import engine, _IS_PG, _now

LOT_STATUSES = {
    'held':      '業者預け',
    'received':  '納品済み',
    'processed': '商品化済み',
    'consumed':  '使い切り',
    'cancelled': '取消',
}
IN_HOUSE = ('received', 'processed')

# (部位, 分類, 主な仕入先, 下限kg, 並び順)  在庫アプリ(inventory_system)の商品マスターから移したもの
SEED_PARTS = [
    ('センレバハツ',       '内臓',   'イシマル',   6, 10),
    ('ホルモン',           '内臓',   'イシマル',   2, 50),
    ('ハラミ（和牛）',     '正肉',   'イシマル',   2, 60),
    ('タン（和牛）',       '内臓',   'イシマル',   2, 70),
    ('ミノ',               '内臓',   'イシマル',   2, 80),
    ('トリ',               '正肉',   'ナンチク',   3, 90),
    ('ウデモモ',           '正肉',   'フード',    10, 100),
    ('トモバラ',           '正肉',   'フード',     5, 120),
    ('クラシタ',           '正肉',   'フード',     5, 130),
    ('タン（アメリカ産）', '内臓',   'フード',     2, 150),
    # ハラミ（アメリカ産）は今は仕入れていないので外した（2026-10-29 社長）
    ('ヒレ',               '正肉',   'フード',     2, 160),
    ('サーロイン',         '正肉',   'フード',     2, 170),
    ('テール',             '内臓',   'その他',     1, 175),
    ('ブタ',               '正肉',   'フクショク', 5, 180),
]

# ラベル上の品名 → (麒麟屋の部位, 細かい部位名)。2026-10-26 決定：ウデ・モモ系→ウデモモ、バラ系→トモバラ
SEED_ALIASES = {
    'ウデモモ': [
        ('内モモ', 'ウチモモ'), ('ウチモモ', 'ウチモモ'), ('シンタマ', 'シンタマ'), ('芯玉', 'シンタマ'),
        ('友三角', 'トモサンカク'), ('トモサンカク', 'トモサンカク'), ('ランイチ', 'ランイチ'),
        ('ランプ', 'ランプ'), ('イチボ', 'イチボ'), ('ソトモモ', 'ソトモモ'), ('外モモ', 'ソトモモ'),
        ('シンシン', 'シンシン'), ('芯々', 'シンシン'), ('カメノコ', 'カメノコ'), ('マルシン', 'マルシン'),
        ('ウデ', 'ウデ'), ('ミスジ', 'ミスジ'), ('ウデサンカク', 'ウデサンカク'), ('トウガラシ', 'トウガラシ'),
    ],
    'トモバラ': [
        ('中バラ', '中バラ'), ('外バラ', '外バラ'), ('カッパ', 'カッパ'), ('カイノミ', 'カイノミ'),
        ('トモバラ', 'トモバラ'),
    ],
    'クラシタ': [('クラシタ', 'クラシタ'), ('肩ロース', 'クラシタ')],
    'サーロイン': [('サーロイン', 'サーロイン')],
    'ヒレ': [('ヒレ', 'ヒレ'), ('ヘレ', 'ヒレ'), ('テンダーロイン', 'ヒレ')],
}

# ウデとモモを分ける（2026-11-03 社長）。在庫・仕入・棚卸は「ウデ」「モモ」別々、原価・出数は「ウデモモ」にまとめる。
# 前からの「ウデモモ」の記録は、名前から推測して振り分けず「ウデモモ（部位未分類）」に残す（あとで1本ずつ分類できる）
UDEMOMO_GROUP = 'ウデモモ'
UDEMOMO_UNCLASSIFIED = 'ウデモモ（部位未分類）'
# 目利きの時のラベル品名の読み替え（初期値）だけをウデ・モモに分ける。既存の塊には使わない
UDE_SUB_PARTS = ('ウデ', 'ミスジ', 'ウデサンカク', 'トウガラシ')
# 分ける直前に写し取る表（データベースの中に bk20261103_〇〇 として残る）
SPLIT_BACKUP_TABLES = ('meat_parts', 'meat_part_aliases', 'meat_menu_map', 'meat_lots', 'meat_stock_count_lines',
                       'meat_period_closings', 'meat_lot_events')
SPLIT_BACKUP_PREFIX = 'bk20261103_'

SEED_MENU_MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'meat_seed_menu_map.json')


def normalize_label_name(s):
    """ラベル品名の表記ゆれをそろえる：全角統一（NFKC）、空白除去、「和牛」「牛」の接頭語を外す"""
    s = unicodedata.normalize('NFKC', str(s or '')).strip()
    s = re.sub(r'\s+', '', s)
    for prefix in ('黒毛和牛', '国産牛', '和牛', '牛'):
        if s.startswith(prefix) and len(s) > len(prefix):
            s = s[len(prefix):]
            break
    return s


def normalize_menu_name(s):
    """マジレジの商品名とメニュー名の突き合わせ用：全角統一・空白除去"""
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(s or '')).strip())


def init_meat_db():
    pk = 'id SERIAL PRIMARY KEY' if _IS_PG else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
    ddls = [
        f"""CREATE TABLE IF NOT EXISTS meat_suppliers (
            {pk},
            name        TEXT    NOT NULL UNIQUE,
            phone       TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            is_active   INTEGER NOT NULL DEFAULT 1
        )""",
        f"""CREATE TABLE IF NOT EXISTS meat_parts (
            {pk},
            name                 TEXT    NOT NULL UNIQUE,
            category             TEXT    NOT NULL DEFAULT '正肉',
            default_supplier_id  INTEGER,
            min_stock_kg         REAL    NOT NULL DEFAULT 0,
            sort_order           INTEGER NOT NULL DEFAULT 0,
            is_active            INTEGER NOT NULL DEFAULT 1
        )""",
        f"""CREATE TABLE IF NOT EXISTS meat_part_aliases (
            {pk},
            processor_name  TEXT    NOT NULL DEFAULT '',
            label_name      TEXT    NOT NULL,
            part_id         INTEGER NOT NULL,
            sub_part_name   TEXT,
            UNIQUE (processor_name, label_name)
        )""",
        f"""CREATE TABLE IF NOT EXISTS meat_menu_map (
            {pk},
            menu_name          TEXT    NOT NULL,
            part_id            INTEGER NOT NULL,
            grams_per_serving  REAL    NOT NULL,
            UNIQUE (menu_name, part_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS meat_purchase_slips (
            {pk},
            supplier_id   INTEGER NOT NULL,
            slip_no       TEXT,
            slip_date     TEXT    NOT NULL,
            total_amount  INTEGER NOT NULL DEFAULT 0,
            tax_amount    INTEGER,
            photo_id      INTEGER,
            created_by    TEXT,
            created_at    TEXT    NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS meat_lots (
            {pk},
            part_id              INTEGER NOT NULL,
            supplier_id          INTEGER NOT NULL,
            status               TEXT    NOT NULL DEFAULT 'held',
            individual_no        TEXT,
            best_before          TEXT,
            label_photo_id       INTEGER,
            grade                TEXT,
            promised_on          TEXT,
            label_kg             REAL,
            trade_kg             REAL,
            label_item_name      TEXT,
            sub_part_name        TEXT,
            processor_name       TEXT,
            processed_date       TEXT,
            branch_no            TEXT,
            serial_no            TEXT,
            slaughter_no         TEXT,
            origin               TEXT,
            brand                TEXT,
            promised_unit_price  INTEGER,
            promised_amount      INTEGER,
            ocr_raw              TEXT,
            received_on          TEXT,
            slip_id              INTEGER,
            storage              TEXT,
            current_kg           REAL,
            memo                 TEXT,
            created_by           TEXT,
            created_at           TEXT    NOT NULL,
            updated_at           TEXT    NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS meat_lot_events (
            {pk},
            lot_id       INTEGER NOT NULL,
            event_type   TEXT    NOT NULL,
            from_status  TEXT,
            to_status    TEXT,
            kg           REAL,
            staff_name   TEXT,
            memo         TEXT,
            logged_at    TEXT    NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS meat_processing (
            {pk},
            lot_id        INTEGER NOT NULL,
            processed_on  TEXT    NOT NULL,
            input_kg      REAL    NOT NULL,
            product_kg    REAL    NOT NULL,
            trim_kg       REAL,
            loss_kg       REAL,
            staff_name    TEXT,
            memo          TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS meat_cattle (
            individual_no    TEXT PRIMARY KEY,
            birth_date       TEXT,
            slaughter_date   TEXT,
            age_months       INTEGER,
            sex              TEXT,
            breed            TEXT,
            mother_no        TEXT,
            breeder_name     TEXT,
            breeder_place    TEXT,
            producer_name    TEXT,
            producer_place   TEXT,
            fetched_at       TEXT,
            raw_result       TEXT
        )""",
        # Renderの無料プランはディスクが更新のたびに消えるため、写真はDBに保存する（縮小済みJPEGのbase64）
        f"""CREATE TABLE IF NOT EXISTS meat_photos (
            {pk},
            kind        TEXT NOT NULL DEFAULT 'label',
            mime        TEXT NOT NULL DEFAULT 'image/jpeg',
            data_b64    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS meat_stock_counts (
            {pk},
            period      TEXT NOT NULL UNIQUE,
            counted_at  TEXT,
            status      TEXT NOT NULL DEFAULT 'draft',
            staff_name  TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS meat_stock_count_lines (
            {pk},
            count_id    INTEGER NOT NULL,
            part_id     INTEGER NOT NULL,
            lot_id      INTEGER,
            state       TEXT    NOT NULL DEFAULT 'raw',
            kg          REAL    NOT NULL,
            unit_price  INTEGER NOT NULL DEFAULT 0,
            amount      INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS meat_menu_sales (
            period     TEXT    NOT NULL,
            menu_name  TEXT    NOT NULL,
            qty        INTEGER NOT NULL DEFAULT 0,
            amount     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (period, menu_name)
        )""",
        """CREATE TABLE IF NOT EXISTS meat_period_closings (
            period           TEXT    NOT NULL,
            part_id          INTEGER NOT NULL,
            opening_amount   INTEGER NOT NULL DEFAULT 0,
            purchase_amount  INTEGER NOT NULL DEFAULT 0,
            closing_amount   INTEGER NOT NULL DEFAULT 0,
            usage_amount     INTEGER NOT NULL DEFAULT 0,
            opening_kg       REAL    NOT NULL DEFAULT 0,
            purchase_kg      REAL    NOT NULL DEFAULT 0,
            closing_kg       REAL    NOT NULL DEFAULT 0,
            usage_kg         REAL    NOT NULL DEFAULT 0,
            sales_qty        REAL    NOT NULL DEFAULT 0,  -- 【H】ハーフは0.5食として数えるため小数
            cost_per_sale    INTEGER,
            theoretical_kg   REAL    NOT NULL DEFAULT 0,
            loss_rate        REAL,
            confirmed_at     TEXT,
            confirmed_by     TEXT,
            PRIMARY KEY (period, part_id)
        )""",
        # 締めを解除した記録。解除前の数字（snapshot：JSON）も残す
        f"""CREATE TABLE IF NOT EXISTS meat_closing_reopens (
            {pk},
            period       TEXT NOT NULL,
            reason       TEXT NOT NULL,
            staff_name   TEXT,
            reopened_at  TEXT NOT NULL,
            snapshot     TEXT NOT NULL
        )""",
    ]
    with engine.begin() as con:
        for ddl in ddls:
            con.execute(text(ddl))
        con.execute(text('CREATE INDEX IF NOT EXISTS idx_meat_lots_status ON meat_lots(status)'))
        con.execute(text('CREATE INDEX IF NOT EXISTS idx_meat_lots_part ON meat_lots(part_id)'))
        con.execute(text('CREATE INDEX IF NOT EXISTS idx_meat_events_lot ON meat_lot_events(lot_id)'))
    _seed()
    _add_weekly_column()
    _add_cost_group_column()
    _split_udemomo()
    _add_count_only_parts()
    _add_offal_parts()
    price_db.init_price_db()
    _sync_price_items()


def _sync_price_items():
    """今の部位を価格表に載せる。部位未分類は前からの記録の置き場なので載せない"""
    with engine.begin() as con:
        suppliers = {r.id: r.name for r in con.execute(text('SELECT id, name FROM meat_suppliers'))}
    for p in list_parts():
        if p['name'] == UDEMOMO_UNCLASSIFIED:
            continue
        price = DEFAULT_UNIT_PRICES.get(p['name']) or DEFAULT_UNIT_PRICES.get(group_name(p))
        price_db.ensure_part_item(p['id'], p['name'], 'パック' if p['name'] in PACK_PARTS else 'kg',
                                  suppliers.get(p['default_supplier_id']), p['sort_order'], price)


# 2026-11-05 社長：棚卸の「一覧に無い分」で選べるように追加。月末だけ数える。下限は0（警告を出さない）
# (部位, 分類, 主な仕入先, 並び順)。仕入先・単価は在庫アプリの商品マスターより
COUNT_ONLY_PARTS = [
    ('ブタカタ',   '正肉',   'フクショク', 190),
    ('ウインナー', '加工肉', 'フード',     200),
    ('ハム',       '加工肉', 'マルキョウ', 210),
    ('ベーコン',   '加工肉', 'マルキョウ', 220),
]
# kgではなくパック数で数える部位（在庫アプリの単価が1パックあたりのため）
PACK_PARTS = ('ウインナー', 'ハム', 'ベーコン')


def _add_count_only_parts():
    """足りない部位を足す。既にあれば触らない（起動のたびに呼ばれるので何度動いても同じ結果になる）"""
    with engine.begin() as con:
        for name, category, supplier, order in COUNT_ONLY_PARTS:
            con.execute(text('INSERT INTO meat_suppliers (name) VALUES (:n) ON CONFLICT (name) DO NOTHING'), {'n': supplier})
            sid = con.execute(text('SELECT id FROM meat_suppliers WHERE name = :n'), {'n': supplier}).scalar()
            con.execute(text("""
                INSERT INTO meat_parts (name, category, default_supplier_id, min_stock_kg, sort_order, count_weekly)
                VALUES (:n, :c, :s, 0, :o, 0) ON CONFLICT (name) DO NOTHING
            """), {'n': name, 'c': category, 's': sid, 'o': order})


# 2026-11-11 社長：内臓の2社目「マルイチ」は内臓ごとに単価が違う（イシマルはセンレバハツ一括3,000円）。
# 部位は別々に登録し、原価は集計分類でまとめる（ウデ・モモと同じ仕組み）。すべて月末だけ数える・下限0。
# ハチノスは焼肉メニュー（ハチノス 塩/タレ）にも使うので「センレバハツ」側に入れる。
# モツ味噌煮込みは「煮込み用モツ」にまとめ、煮込み済みの残りは生の重さに直して数える（1食＝生0.1kg）
NIKOMI_GROUP = '煮込み用モツ'
NIKOMI_COOKED = 'モツ煮込み（仕込み済み・1食＝0.1kg）'
# (部位, 集計分類, 並び順, 単価円/kg)
OFFAL_PARTS = [
    ('ハツ',       'センレバハツ', 11, 2000),
    ('レバー',     'センレバハツ', 12, 2000),
    ('センマイ',   'センレバハツ', 13, 1900),
    ('ハチノス',   'センレバハツ', 14, 1000),
    ('大腸',       NIKOMI_GROUP,   41, 1200),
    ('直腸',       NIKOMI_GROUP,   42, 1200),
    ('アキレス',   NIKOMI_GROUP,   43, 1300),
    ('赤センマイ', NIKOMI_GROUP,   44, 2600),
    ('ギアラ',     NIKOMI_GROUP,   45, 1600),
    (NIKOMI_COOKED, NIKOMI_GROUP,  46, 1300),   # 前回の仕込みの生モツ平均 約1,291円/kg
]
OFFAL_SUPPLIER = 'マルイチ'
# 前回の仕込み（ハチノス1.0・赤センマイ0.3・大腸0.8・直腸0.4・アキレス0.9kg、約35食）を1食あたりに直したもの
NIKOMI_MENU = 'モツ味噌煮込み'
NIKOMI_MENU_GRAMS = {'センレバハツ': 29, NIKOMI_GROUP: 69}


def _add_offal_parts():
    """マルイチの内臓とモツ味噌煮込みのグラム表を足す。既にあれば触らない（何度動いても同じ結果）"""
    with engine.begin() as con:
        con.execute(text('INSERT INTO meat_suppliers (name) VALUES (:n) ON CONFLICT (name) DO NOTHING'),
                    {'n': OFFAL_SUPPLIER})
        sid = con.execute(text('SELECT id FROM meat_suppliers WHERE name = :n'), {'n': OFFAL_SUPPLIER}).scalar()
        for name, group, order, _ in OFFAL_PARTS:
            con.execute(text("""
                INSERT INTO meat_parts (name, category, default_supplier_id, min_stock_kg, sort_order, count_weekly,
                                        cost_group)
                VALUES (:n, '内臓', :s, 0, :o, 0, :g) ON CONFLICT (name) DO NOTHING
            """), {'n': name, 's': sid, 'o': order, 'g': group})
        has_menu = con.execute(text('SELECT COUNT(*) FROM meat_menu_map WHERE menu_name = :m'),
                               {'m': NIKOMI_MENU}).scalar()
    if not has_menu:
        set_menu_parts(NIKOMI_MENU, NIKOMI_MENU_GRAMS)


# 週次の棚卸で量る部位（金額の大きい部位）。それ以外は月末だけ量る（2026-10-29 決定）
MONTHLY_ONLY_PARTS = ('ホルモン', 'ミノ', 'センレバハツ', 'テール', 'トリ', 'ブタ')


def _add_weekly_column():
    """meat_parts に count_weekly（1＝毎週量る）を足す。足した時だけ初期値を入れる"""
    try:
        with engine.begin() as con:
            con.execute(text('ALTER TABLE meat_parts ADD COLUMN count_weekly INTEGER NOT NULL DEFAULT 1'))
    except Exception:
        return   # 既にある
    with engine.begin() as con:
        for name in MONTHLY_ONLY_PARTS:
            con.execute(text('UPDATE meat_parts SET count_weekly = 0 WHERE name = :n'), {'n': name})


def _add_cost_group_column():
    """meat_parts に cost_group（原価・出数をまとめる集計分類。空なら部位名そのもの）を足す"""
    try:
        with engine.begin() as con:
            con.execute(text('ALTER TABLE meat_parts ADD COLUMN cost_group TEXT'))
    except Exception:
        pass   # 既にある


def _split_udemomo():
    """「ウデモモ」を ウデ・モモ・ウデモモ（部位未分類） に分ける（1回だけ）。
    既存の部位の行は番号を変えず名前だけ「部位未分類」にするので、塊・棚卸・締めの記録は1件も動かない。
    直す前に関係する表を丸ごと写し取る（同じトランザクション内。失敗したら全部元に戻る）"""
    from sqlalchemy import inspect
    with engine.begin() as con:
        names = {r.name: r for r in con.execute(text('SELECT * FROM meat_parts'))}
        if 'ウデ' in names and 'モモ' in names:
            return
        old = names.get(UDEMOMO_GROUP)
        if old is None:
            return
        insp = inspect(con)
        for t in SPLIT_BACKUP_TABLES:
            if insp.has_table(t) and not insp.has_table(SPLIT_BACKUP_PREFIX + t):
                con.execute(text(f'CREATE TABLE {SPLIT_BACKUP_PREFIX}{t} AS SELECT * FROM {t}'))
        # 下限は部位ごとに決める（今は0kg）。前のウデモモの10kgを残すと、未分類に警告が出続ける
        con.execute(text('UPDATE meat_parts SET name = :n, cost_group = :g, sort_order = :o, min_stock_kg = 0 WHERE id = :id'),
                    {'n': UDEMOMO_UNCLASSIFIED, 'g': UDEMOMO_GROUP, 'o': old.sort_order + 2, 'id': old.id})
        new_ids = {}
        for i, name in enumerate(('ウデ', 'モモ')):
            con.execute(text("""
                INSERT INTO meat_parts (name, category, default_supplier_id, min_stock_kg, sort_order, is_active,
                                        count_weekly, cost_group)
                VALUES (:n, :c, :s, 0, :o, 1, :w, :g)
            """), {'n': name, 'c': old.category, 's': old.default_supplier_id, 'o': old.sort_order + i,
                   'w': getattr(old, 'count_weekly', 1), 'g': UDEMOMO_GROUP})
            new_ids[name] = con.execute(text('SELECT id FROM meat_parts WHERE name = :n'), {'n': name}).scalar()
        # 目利きの時の自動の部位（全業者共通の初期値だけ）。人が覚えさせた読み替えは触らない
        for a in con.execute(text("""SELECT id, sub_part_name FROM meat_part_aliases
                                     WHERE part_id = :p AND processor_name = ''"""), {'p': old.id}).all():
            target = 'ウデ' if a.sub_part_name in UDE_SUB_PARTS else 'モモ'
            con.execute(text('UPDATE meat_part_aliases SET part_id = :t WHERE id = :id'),
                        {'t': new_ids[target], 'id': a.id})


def _seed():
    """初回だけマスタを入れる（既にあるものは触らない）"""
    with engine.begin() as con:
        if con.execute(text('SELECT COUNT(*) FROM meat_parts')).scalar():
            return
        suppliers = []
        for _, _, sup, _, _ in SEED_PARTS:
            if sup not in suppliers:
                suppliers.append(sup)
        for i, sup in enumerate(suppliers):
            con.execute(text('INSERT INTO meat_suppliers (name, sort_order) VALUES (:n, :o)'),
                        {'n': sup, 'o': i * 10})
        sup_ids = {r.name: r.id for r in con.execute(text('SELECT id, name FROM meat_suppliers'))}
        for name, cat, sup, min_kg, order in SEED_PARTS:
            con.execute(text("""
                INSERT INTO meat_parts (name, category, default_supplier_id, min_stock_kg, sort_order)
                VALUES (:n, :c, :s, :m, :o)
            """), {'n': name, 'c': cat, 's': sup_ids[sup], 'm': min_kg, 'o': order})
        part_ids = {r.name: r.id for r in con.execute(text('SELECT id, name FROM meat_parts'))}
        for part, pairs in SEED_ALIASES.items():
            for label, sub in pairs:
                con.execute(text("""
                    INSERT INTO meat_part_aliases (processor_name, label_name, part_id, sub_part_name)
                    VALUES ('', :l, :p, :s)
                """), {'l': normalize_label_name(label), 'p': part_ids[part], 's': sub})
        if os.path.exists(SEED_MENU_MAP):
            with open(SEED_MENU_MAP, encoding='utf-8') as f:
                for row in json.load(f):
                    con.execute(text("""
                        INSERT INTO meat_menu_map (menu_name, part_id, grams_per_serving)
                        VALUES (:m, :p, :g)
                    """), {'m': row['menu_name'], 'p': part_ids[row['part']], 'g': row['grams']})


# ============================================================
# マスタ
# ============================================================
def list_parts(include_inactive=False):
    with engine.begin() as con:
        return [dict(r) for r in con.execute(text(
            'SELECT * FROM meat_parts' + ('' if include_inactive else ' WHERE is_active = 1') +
            ' ORDER BY sort_order')).mappings()]


def entry_parts():
    """目利きで選ぶ部位。「部位未分類」は前からの記録の置き場なので、新しくは選ばせない"""
    return [p for p in list_parts() if p['name'] != UDEMOMO_UNCLASSIFIED]


def group_name(part):
    return part.get('cost_group') or part['name']


def part_groups(parts=None):
    """部位 → 集計分類。{part_id: {'group': 'ウデモモ', 'rep': 代表の部位id, 'members': [id,...]}}
    代表＝その分類で一番古い部位（ウデモモなら前からの行）。原価の締め・グラム表はこの番号で持つ"""
    parts = parts if parts is not None else list_parts(include_inactive=True)
    by_group = {}
    for p in parts:
        by_group.setdefault(group_name(p), []).append(p)
    out = {}
    for g, ps in by_group.items():
        rep_id = min(p['id'] for p in ps)
        for p in ps:
            out[p['id']] = {'group': g, 'rep': rep_id, 'members': sorted(x['id'] for x in ps)}
    return out


def list_suppliers():
    with engine.begin() as con:
        return [dict(r) for r in con.execute(text(
            'SELECT * FROM meat_suppliers WHERE is_active = 1 ORDER BY sort_order')).mappings()]


def get_or_create_supplier(name):
    name = (name or '').strip()
    with engine.begin() as con:
        row = con.execute(text('SELECT id FROM meat_suppliers WHERE name = :n'), {'n': name}).first()
        if row:
            return row.id
        con.execute(text('INSERT INTO meat_suppliers (name, sort_order) VALUES (:n, 999)'), {'n': name})
        return con.execute(text('SELECT id FROM meat_suppliers WHERE name = :n'), {'n': name}).scalar()


def resolve_alias(label_name, processor_name=''):
    """ラベル品名から部位を推定する。業者別の読み替え → 全業者共通 の順に探す。
    見つからなければ (None, None)。"""
    key = normalize_label_name(label_name)
    if not key:
        return None, None
    with engine.begin() as con:
        for proc in ((processor_name or '').strip(), ''):
            row = con.execute(text("""
                SELECT part_id, sub_part_name FROM meat_part_aliases
                WHERE processor_name = :p AND label_name = :l
            """), {'p': proc, 'l': key}).first()
            if row:
                return row.part_id, row.sub_part_name
    return None, None


def learn_alias(label_name, processor_name, part_id):
    """初めて見る品名で人が部位を選んだら、次回から自動で入るよう読み替え表に追加する"""
    key = normalize_label_name(label_name)
    if not key or resolve_alias(label_name, processor_name)[0] == part_id:
        return
    proc = (processor_name or '').strip()
    with engine.begin() as con:
        con.execute(text('DELETE FROM meat_part_aliases WHERE processor_name = :p AND label_name = :l'),
                    {'p': proc, 'l': key})
        con.execute(text("""
            INSERT INTO meat_part_aliases (processor_name, label_name, part_id, sub_part_name)
            VALUES (:p, :l, :part, :s)
        """), {'p': proc, 'l': key, 'part': part_id, 's': key})


def last_unit_price(part_id):
    """部位の直近の単価（目利き画面の初期値用）。ウデ・モモに記録が無ければ「ウデモモ」の直近の単価"""
    members = part_groups().get(part_id, {}).get('members') or [part_id]
    keys = ','.join(f':m{i}' for i in range(len(members)))
    with engine.begin() as con:
        own = con.execute(text("""
            SELECT promised_unit_price FROM meat_lots
            WHERE part_id = :p AND promised_unit_price IS NOT NULL AND status != 'cancelled'
            ORDER BY id DESC LIMIT 1
        """), {'p': part_id}).scalar()
        if own is not None:
            return own
        return con.execute(text(f"""
            SELECT promised_unit_price FROM meat_lots
            WHERE part_id IN ({keys}) AND promised_unit_price IS NOT NULL AND status != 'cancelled'
            ORDER BY id DESC LIMIT 1
        """), {f'm{i}': m for i, m in enumerate(members)}).scalar()


# 目利き（業者預け）→ 納品 の2段階で入れるのは、フードの正肉6部位だけ（2026-11-11 社長）。
# それ以外（内臓・タン・ハラミ・トリ・ブタなど）は「仕入」画面で、その日に仕入＝納品として1回で入れる
MEKIKI_PARTS = ('トモバラ', 'クラシタ', 'ウデ', 'モモ', 'ヒレ', 'サーロイン')


def direct_parts():
    """仕入画面で選ぶ部位（目利きの6部位と部位未分類を除く）"""
    return [p for p in entry_parts() if p['name'] not in MEKIKI_PARTS]


def direct_default_prices():
    """仕入画面の単価の初期値 {part_id: 円}＝その部位の前回の仕入単価、無ければ価格表。
    同じ分類の他の部位の単価は使わない（ハチノス1,000円にセンレバハツ3,000円が入らないように）"""
    with engine.begin() as con:
        own = {r.part_id: r.price for r in con.execute(text("""
            SELECT l.part_id, l.promised_unit_price AS price FROM meat_lots l
            JOIN (SELECT part_id, MAX(id) AS id FROM meat_lots
                  WHERE promised_unit_price IS NOT NULL AND status != 'cancelled' GROUP BY part_id) x ON x.id = l.id
        """))}
    base = price_db.part_prices((date.fromisoformat(_now()[:10]) + timedelta(days=1)).isoformat())
    out = {}
    for p in direct_parts():
        price = own.get(p['id']) or base.get(p['id'])
        if price:
            out[p['id']] = price
    return out


def purchase_direct(supplier_id, slip_no, slip_date, lines, staff, memo=''):
    """その日に仕入＝納品：塊を作ってすぐ納品済みにする（1枚の伝票にまとめる）。
    lines: [{'part_id', 'kg', 'unit_price', 'best_before'}]。戻り値 (件数, 合計金額)"""
    lot_ids = []
    for ln in lines:
        lot_ids.append(create_held_lot({
            'part_id': ln['part_id'], 'supplier_id': supplier_id,
            'label_kg': ln['kg'], 'trade_kg': ln['kg'], 'unit_price': ln['unit_price'],
            'best_before': ln.get('best_before') or None, 'promised_on': slip_date, 'memo': memo or None,
        }, staff))
    total = sum(trade_amount(_num(ln['kg']), _num(ln['unit_price'])) or 0 for ln in lines)
    _, lines_total = receive_lots(supplier_id, slip_no, slip_date, total, lot_ids, staff)
    return len(lot_ids), lines_total


def base_part_prices():
    """価格表の今の値段 {part_id: 円}。部位に無ければ同じ集計分類の値段（部位未分類 → ウデ）"""
    tomorrow = date.fromisoformat(_now()[:10]) + timedelta(days=1)
    return _with_group_prices(price_db.part_prices(tomorrow.isoformat()))


def _with_group_prices(base):
    out = dict(base)
    for part_id, g in part_groups().items():
        if part_id not in out:
            found = next((base[m] for m in g['members'] if m in base), None)
            if found:
                out[part_id] = found
    return out


def recent_purchases(limit=5):
    """部位ごとの最近の仕入単価（新しい順）{part_id: [{'on','supplier','price'}]}。値上がりの流れを見る用"""
    with engine.begin() as con:
        rows = con.execute(text("""
            SELECT l.part_id, l.promised_unit_price AS price, COALESCE(sl.slip_date, l.promised_on) AS on_date,
                   s.name AS supplier
            FROM meat_lots l JOIN meat_suppliers s ON s.id = l.supplier_id
            LEFT JOIN meat_purchase_slips sl ON sl.id = l.slip_id
            WHERE l.promised_unit_price IS NOT NULL AND l.status != 'cancelled'
            ORDER BY on_date DESC, l.id DESC
        """)).all()
    out = {}
    for r in rows:
        bucket = out.setdefault(r.part_id, [])
        if len(bucket) < limit:
            bucket.append({'on': r.on_date, 'supplier': r.supplier, 'price': r.price})
    return out


def update_lot_price(lot_id, price, staff, memo=''):
    """塊の単価を直す（フードは単価があとで決まる・変わることがある）。金額も計算し直し、記録を残す。
    締めた期間の仕入は変えない（締めを解除してから直す）"""
    try:
        new_price = int(str(price).replace(',', '').strip())
    except ValueError:
        raise ValueError('単価は整数で入れてください')
    if new_price <= 0:
        raise ValueError('単価は1円以上で入れてください')
    with engine.begin() as con:
        lot = con.execute(text("""
            SELECT l.status, l.trade_kg, l.promised_unit_price, sl.slip_date FROM meat_lots l
            LEFT JOIN meat_purchase_slips sl ON sl.id = l.slip_id WHERE l.id = :id
        """), {'id': lot_id}).first()
        if not lot or lot.status == 'cancelled':
            raise ValueError('この塊の単価は直せません')
        if lot.slip_date:
            d = date.fromisoformat(lot.slip_date[:10])
            for period in (d.strftime('%Y-%m'), week_end(d).isoformat()):
                if _closed_rows(con, period):
                    raise ValueError(f'{period_label(period)} は締め済みです。先に原価の画面で「締めを解除」してください')
        note = f'単価 {lot.promised_unit_price or "-"}円 → {new_price}円'
        _set_status(con, lot_id, lot.status, 'price', staff, ' '.join(x for x in (note, (memo or '').strip()) if x), None,
                    {'promised_unit_price': new_price, 'promised_amount': trade_amount(lot.trade_kg, new_price)})


def set_menu_parts(menu_name, parts_grams):
    """メニューの部位とグラム数を入れ替える。parts_grams: {部位名: g}"""
    parts = list_parts()
    groups = part_groups(parts)
    ids = {p['name']: groups[p['id']]['rep'] for p in parts}
    ids.update({g['group']: g['rep'] for g in groups.values()})   # 「ウデモモ」で指定できる
    with engine.begin() as con:
        con.execute(text('DELETE FROM meat_menu_map WHERE menu_name = :m'), {'m': menu_name})
        for part, g in parts_grams.items():
            con.execute(text('INSERT INTO meat_menu_map (menu_name, part_id, grams_per_serving) VALUES (:m, :p, :g)'),
                        {'m': menu_name, 'p': ids[part], 'g': g})


def list_menu_map():
    """グラム表。部位は集計分類（ウデモモ）で返す：同じ販売数をウデとモモの両方に足さないため"""
    with engine.begin() as con:
        rows = [dict(r) for r in con.execute(text("""
            SELECT m.*, p.name AS part_name FROM meat_menu_map m
            JOIN meat_parts p ON p.id = m.part_id
            ORDER BY m.menu_name, p.sort_order
        """)).mappings()]
    groups = part_groups()
    for r in rows:
        g = groups.get(r['part_id'])
        if g:
            r['part_id'] = g['rep']
            r['part_name'] = g['group']
    return rows


# ============================================================
# 写真
# ============================================================
def save_photo(data_b64, mime='image/jpeg', kind='label'):
    with engine.begin() as con:
        res = con.execute(text("""
            INSERT INTO meat_photos (kind, mime, data_b64, created_at) VALUES (:k, :m, :d, :t)
            """ + (' RETURNING id' if _IS_PG else '')),
            {'k': kind, 'm': mime, 'd': data_b64, 't': _now()})
        return res.scalar() if _IS_PG else res.lastrowid


def get_photo(photo_id):
    with engine.begin() as con:
        row = con.execute(text('SELECT * FROM meat_photos WHERE id = :id'), {'id': photo_id}).mappings().first()
        return dict(row) if row else None


# ============================================================
# ロット（塊）
# ============================================================
def _num(v):
    if v in (None, ''):
        return None
    try:
        return float(str(v).replace(',', ''))
    except ValueError:
        return None


def trade_amount(trade_kg, unit_price):
    """仕入金額 ＝ 取引値 × 単価（業者への支払いは取引値で計算される：2026-10-26 確認）"""
    if trade_kg is None or unit_price is None:
        return None
    return int(round(trade_kg * unit_price))


def _event(con, lot_id, event_type, from_status, to_status, kg=None, staff='', memo=''):
    con.execute(text("""
        INSERT INTO meat_lot_events (lot_id, event_type, from_status, to_status, kg, staff_name, memo, logged_at)
        VALUES (:l, :e, :f, :t, :kg, :s, :m, :ts)
    """), {'l': lot_id, 'e': event_type, 'f': from_status, 't': to_status, 'kg': kg,
           's': staff, 'm': memo, 'ts': _now()})


def create_held_lot(d, staff):
    """① 目利き：業者預け（held）のロットを作る"""
    label_kg = _num(d.get('label_kg'))
    trade_kg = _num(d.get('trade_kg'))
    price = _num(d.get('unit_price'))
    price = int(price) if price is not None else None
    ts = _now()
    params = {
        'part_id': int(d['part_id']), 'supplier_id': int(d['supplier_id']),
        'individual_no': (d.get('individual_no') or '').strip() or None,
        'best_before': d.get('best_before') or None,
        'label_photo_id': d.get('label_photo_id') or None,
        'grade': d.get('grade') or None,
        'promised_on': d.get('promised_on') or ts[:10],
        'label_kg': label_kg, 'trade_kg': trade_kg,
        'label_item_name': d.get('label_item_name') or None,
        'sub_part_name': d.get('sub_part_name') or None,
        'processor_name': d.get('processor_name') or None,
        'processed_date': d.get('processed_date') or None,
        'branch_no': d.get('branch_no') or None,
        'serial_no': d.get('serial_no') or None,
        'slaughter_no': d.get('slaughter_no') or None,
        'origin': d.get('origin') or None,
        'brand': d.get('brand') or None,
        'price': price, 'amount': trade_amount(trade_kg, price),
        'ocr_raw': d.get('ocr_raw') or None,
        'storage': d.get('storage') or None,
        'memo': d.get('memo') or None,
        'staff': staff, 'ts': ts,
    }
    with engine.begin() as con:
        res = con.execute(text("""
            INSERT INTO meat_lots (part_id, supplier_id, status, individual_no, best_before, label_photo_id,
                grade, promised_on, label_kg, trade_kg, label_item_name, sub_part_name, processor_name,
                processed_date, branch_no, serial_no, slaughter_no, origin, brand,
                promised_unit_price, promised_amount, ocr_raw, storage, current_kg, memo,
                created_by, created_at, updated_at)
            VALUES (:part_id, :supplier_id, 'held', :individual_no, :best_before, :label_photo_id,
                :grade, :promised_on, :label_kg, :trade_kg, :label_item_name, :sub_part_name, :processor_name,
                :processed_date, :branch_no, :serial_no, :slaughter_no, :origin, :brand,
                :price, :amount, :ocr_raw, :storage, :label_kg, :memo,
                :staff, :ts, :ts)
        """ + (' RETURNING id' if _IS_PG else '')), params)
        lot_id = res.scalar() if _IS_PG else res.lastrowid
        _event(con, lot_id, 'promise', None, 'held', label_kg, staff)
    if params['label_item_name']:
        learn_alias(params['label_item_name'], params['processor_name'], params['part_id'])
    return lot_id


_LOT_SELECT = """
    SELECT l.*, p.name AS part_name, p.category AS part_category, p.sort_order AS part_sort,
           s.name AS supplier_name, sl.slip_no, sl.slip_date,
           ct.age_months AS cattle_age_months, ct.producer_name AS cattle_producer,
           ct.producer_place AS cattle_producer_place, ct.fetched_at AS cattle_fetched_at
    FROM meat_lots l
    JOIN meat_parts p ON p.id = l.part_id
    JOIN meat_suppliers s ON s.id = l.supplier_id
    LEFT JOIN meat_purchase_slips sl ON sl.id = l.slip_id
    LEFT JOIN meat_cattle ct ON ct.individual_no = l.individual_no
"""


def _decorate(lot, today=None):
    today = today or date.fromisoformat(_now()[:10])
    lot['status_label'] = LOT_STATUSES.get(lot['status'], lot['status'])
    lot['kg_now'] = lot['current_kg'] if lot['current_kg'] is not None else (lot['label_kg'] or 0)
    # 1kgあたりの実際の原価（取引値×単価 ÷ 実重量）。在庫金額の目安に使う
    if lot['promised_amount'] and lot['label_kg']:
        lot['cost_per_kg'] = lot['promised_amount'] / lot['label_kg']
    else:
        lot['cost_per_kg'] = lot['promised_unit_price'] or 0
    lot['value_now'] = int(round(lot['kg_now'] * lot['cost_per_kg']))
    lot['days_left'] = None
    if lot['best_before']:
        try:
            lot['days_left'] = (date.fromisoformat(lot['best_before']) - today).days
        except ValueError:
            pass
    lot['days_held'] = None
    if lot['status'] == 'held' and lot['promised_on']:
        try:
            lot['days_held'] = (today - date.fromisoformat(lot['promised_on'][:10])).days
        except ValueError:
            pass
    return lot


def get_lot(lot_id):
    with engine.begin() as con:
        row = con.execute(text(_LOT_SELECT + ' WHERE l.id = :id'), {'id': lot_id}).mappings().first()
        if not row:
            return None
        lot = _decorate(dict(row))
        lot['events'] = [dict(r) for r in con.execute(text(
            'SELECT * FROM meat_lot_events WHERE lot_id = :id ORDER BY id'), {'id': lot_id}).mappings()]
        lot['processing'] = [dict(r) for r in con.execute(text(
            'SELECT * FROM meat_processing WHERE lot_id = :id ORDER BY id'), {'id': lot_id}).mappings()]
        if lot['individual_no']:
            c = con.execute(text('SELECT * FROM meat_cattle WHERE individual_no = :n'),
                            {'n': lot['individual_no']}).mappings().first()
            lot['cattle'] = dict(c) if c else None
            lot['siblings'] = [dict(r) for r in con.execute(text(_LOT_SELECT + """
                WHERE l.individual_no = :n AND l.id != :id AND l.status != 'cancelled' ORDER BY l.id
            """), {'n': lot['individual_no'], 'id': lot_id}).mappings()]
        else:
            lot['cattle'], lot['siblings'] = None, []
    return lot


def list_lots(statuses):
    keys = ','.join(f':s{i}' for i in range(len(statuses)))
    params = {f's{i}': s for i, s in enumerate(statuses)}
    with engine.begin() as con:
        rows = [dict(r) for r in con.execute(text(
            _LOT_SELECT + f' WHERE l.status IN ({keys}) ORDER BY p.sort_order, l.best_before, l.id'),
            params).mappings()]
    today = date.fromisoformat(_now()[:10])
    return [_decorate(r, today) for r in rows]


def stock_summary(expiry_warn_days=3):
    """在庫一覧：店内（納品済み＋商品化済み）を部位ごとにまとめる。下限割れ・期限近いものを先頭に"""
    lots = list_lots(IN_HOUSE)
    parts = {p['id']: dict(p, lots=[], raw_kg=0.0, product_kg=0.0, total_kg=0.0, value=0,
                           carry_kg=0.0, month_kg=0.0)
             for p in list_parts()}
    # 前繰＝伝票の日付が今月より前の塊（月をまたいで残った塊は、前月末の棚卸で量った重さになっている）。
    # 2026-11-11 社長：前繰と今月の仕入を分けて見せる。使った分は月末の棚卸で初めて分かる
    month_start = date.fromisoformat(_now()[:10]).replace(day=1)
    carry_label = f'{(month_start - timedelta(days=1)).month}月末の残り'
    for lot in lots:
        p = parts.get(lot['part_id'])
        if p is None:
            continue
        bought = (lot.get('slip_date') or lot.get('promised_on') or '')[:10]
        lot['carryover'] = bool(bought) and bought < month_start.isoformat()
        lot['bought_md'] = f'{int(bought[5:7])}/{int(bought[8:10])}' if len(bought) == 10 else ''
        lot['carry_label'] = carry_label
        p['carry_kg' if lot['carryover'] else 'month_kg'] += lot['kg_now']
        p['lots'].append(lot)
        if lot['status'] == 'received':
            p['raw_kg'] += lot['kg_now']
        else:
            p['product_kg'] += lot['kg_now']
        p['total_kg'] += lot['kg_now']
        p['value'] += lot['value_now']
    rows = []
    groups = part_groups()
    shared = {}   # 2部位以上の集計分類（ウデモモ）は、合計も出す（下限は部位ごと）
    for p in parts.values():
        g = groups.get(p['id'], {})
        p['group'] = g.get('group', p['name'])
        if len(g.get('members', [])) > 1:
            sg = shared.setdefault(p['group'], {'name': p['group'], 'total_kg': 0.0, 'value': 0, 'min_stock_kg': 0.0,
                                                'members': [], 'sort_order': p['sort_order']})
            sg['total_kg'] += p['total_kg']
            sg['value'] += p['value']
            sg['min_stock_kg'] += p['min_stock_kg'] or 0
            sg['members'].append(p)
            sg['sort_order'] = min(sg['sort_order'], p['sort_order'])
        p['expiring'] = [l for l in p['lots'] if l['days_left'] is not None and l['days_left'] <= expiry_warn_days]
        p['lots'].sort(key=lambda l: not l['carryover'])   # 前繰を先に（その中は元の並び）
        rows.append(p)
    for p in rows:
        p['group_info'] = shared.get(p['group'])
        p['is_low'] = p['total_kg'] < (p['min_stock_kg'] or 0)
    for sg in shared.values():
        sg['is_low'] = any(m['is_low'] for m in sg['members'])
    rows.sort(key=lambda p: (not p['is_low'], not p['expiring'], p['sort_order']))
    held = list_lots(('held',))
    return {
        'parts': rows,
        'groups': sorted(shared.values(), key=lambda g: g['sort_order']),
        'held': held,
        'total_kg': sum(p['total_kg'] for p in rows),
        'total_value': sum(p['value'] for p in rows),
        'held_kg': sum(l['label_kg'] or 0 for l in held),
    }


def _set_status(con, lot_id, to_status, event_type, staff, memo='', kg=None, extra=None):
    cur = con.execute(text('SELECT status FROM meat_lots WHERE id = :id'), {'id': lot_id}).scalar()
    sets = {'status': to_status, 'updated_at': _now()}
    sets.update(extra or {})
    con.execute(text('UPDATE meat_lots SET ' + ', '.join(f'{k} = :{k}' for k in sets) + ' WHERE id = :id'),
                dict(sets, id=lot_id))
    _event(con, lot_id, event_type, cur, to_status, kg, staff, memo)


def receive_lots(supplier_id, slip_no, slip_date, total_amount, lot_ids, staff, photo_id=None):
    """② 納品：伝票を作り、選んだ預けロットを納品済みにする。
    戻り値 (slip_id, 明細合計)。明細合計と伝票合計のずれは画面で警告する。"""
    with engine.begin() as con:
        res = con.execute(text("""
            INSERT INTO meat_purchase_slips (supplier_id, slip_no, slip_date, total_amount, photo_id,
                                             created_by, created_at)
            VALUES (:s, :n, :d, :t, :p, :by, :ts)
        """ + (' RETURNING id' if _IS_PG else '')),
            {'s': supplier_id, 'n': slip_no, 'd': slip_date, 't': total_amount, 'p': photo_id,
             'by': staff, 'ts': _now()})
        slip_id = res.scalar() if _IS_PG else res.lastrowid
        lines_total = 0
        for lot_id in lot_ids:
            lot = con.execute(text('SELECT status, promised_amount, label_kg FROM meat_lots WHERE id = :id'),
                              {'id': lot_id}).first()
            if not lot or lot.status != 'held':
                continue
            lines_total += lot.promised_amount or 0
            _set_status(con, lot_id, 'received', 'receive', staff, f'伝票 {slip_no or ""}', lot.label_kg,
                        {'slip_id': slip_id, 'received_on': slip_date})
    return slip_id, lines_total


def unreceive_lot(lot_id, staff, memo=''):
    """納品の取り消し：誤って納品にした塊を「業者預け」に戻す。
    捌き前（納品済みのまま）で、確定済みの棚卸に入っていない塊だけ戻せる。"""
    with engine.begin() as con:
        lot = con.execute(text('SELECT status, slip_id, received_on FROM meat_lots WHERE id = :id'),
                          {'id': lot_id}).first()
        if not lot or lot.status != 'received':
            raise ValueError('業者預けに戻せるのは「納品済み（未開封）」の塊だけです')
        used = con.execute(text("""
            SELECT c.period FROM meat_stock_count_lines ln JOIN meat_stock_counts c ON c.id = ln.count_id
            WHERE ln.lot_id = :id AND c.status = 'confirmed' ORDER BY c.period
        """), {'id': lot_id}).scalars().all()
        if used:
            raise ValueError(f'{"・".join(used)} の確定済み棚卸に入っているため戻せません。'
                             '先にその棚卸を「入力中に戻す」にしてください')
        slip_no = con.execute(text('SELECT slip_no FROM meat_purchase_slips WHERE id = :id'),
                              {'id': lot.slip_id}).scalar() if lot.slip_id else None
        note = f'納品取消（伝票 {slip_no or "-"}・{lot.received_on or "-"}）'
        _set_status(con, lot_id, 'held', 'unreceive', staff, ' '.join(x for x in (note, memo) if x), None,
                    {'slip_id': None, 'received_on': None})


def lots_in_house_for_count(period):
    """棚卸の対象：その期間の終わりまでに納品された塊。
    さかのぼって入れる時（10月末の分を11月に入れる等）は、期間の後に使い切った塊も出す（その時は店にあったため）"""
    _, end = period_bounds(period)
    lots = [l for l in list_lots(IN_HOUSE) if not l.get('received_on') or l['received_on'][:10] < end]
    with engine.begin() as con:
        used_after = {r.lot_id: r.at for r in con.execute(text("""
            SELECT e.lot_id, MIN(e.logged_at) AS at FROM meat_lot_events e JOIN meat_lots l ON l.id = e.lot_id
            WHERE l.status = 'consumed' AND e.event_type = 'consume' AND e.logged_at >= :end
              AND l.received_on IS NOT NULL AND l.received_on < :end
            GROUP BY e.lot_id
        """), {'end': end})}
    if used_after:
        for l in list_lots(('consumed',)):
            if l['id'] in used_after:
                l['used_after'] = used_after[l['id']][:10]
                l['kg_now'] = l['label_kg'] or 0   # 今は0kg。期間末の重さは量った値を入れてもらう
                lots.append(l)
    return lots


def _changed_after(con, lot_id, end, period=None):
    """期間の後に、量った・捌いた・使い切った記録がある塊か（さかのぼりの棚卸で今の重さを上書きしないため）。
    同じ期間の棚卸を前に確定した時の記録は数えない（2026-11-11 不具合：10月の棚卸を確定し直した時、
    1回目の確定で残った「量った」記録のせいで「無い」にした塊が使い切りにならず、次の週に残った）"""
    own = (f'{period} 棚卸', f'{period} 棚卸で見つからず') if period else ('', '')
    return bool(con.execute(text("""
        SELECT COUNT(*) FROM meat_lot_events
        WHERE lot_id = :l AND logged_at >= :end AND event_type IN ('weigh', 'process', 'consume')
          AND COALESCE(memo, '') NOT IN (:m1, :m2)
    """), {'l': lot_id, 'end': end, 'm1': own[0], 'm2': own[1]}).scalar())


def reclassify_lot(lot_id, new_part_id, staff, memo=''):
    """部位未分類の塊を ウデ／モモ に分ける（同じ集計分類の中だけ）。
    数量・金額・日付は変えない。棚卸の行の部位も一緒に付け替えるので、ウデモモの合計は変わらない"""
    groups = part_groups()
    with engine.begin() as con:
        lot = con.execute(text('SELECT id, part_id, status FROM meat_lots WHERE id = :id'), {'id': lot_id}).first()
        if not lot:
            raise ValueError('塊が見つかりません')
        if lot.status == 'cancelled':
            raise ValueError('取り消した塊は分類できません')
        new_part_id = int(new_part_id)
        if new_part_id == lot.part_id:
            return
        g_old, g_new = groups.get(lot.part_id), groups.get(new_part_id)
        if not g_old or not g_new or g_old['group'] != g_new['group']:
            raise ValueError('同じ集計分類（例：ウデモモ）の中でしか分類し直せません')
        names = {r.id: r.name for r in con.execute(text('SELECT id, name FROM meat_parts'))}
        con.execute(text('UPDATE meat_lots SET part_id = :p, updated_at = :t WHERE id = :id'),
                    {'p': new_part_id, 't': _now(), 'id': lot_id})
        con.execute(text('UPDATE meat_stock_count_lines SET part_id = :p WHERE lot_id = :id'),
                    {'p': new_part_id, 'id': lot_id})
        _event(con, lot_id, 'reclassify', lot.status, lot.status, None, staff,
               ' '.join(x for x in (f'{names[lot.part_id]} → {names[new_part_id]}', memo) if x))


def process_lot(lot_id, input_kg, product_kg, trim_kg, loss_kg, staff, memo=''):
    """③ 捌き・商品化"""
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO meat_processing (lot_id, processed_on, input_kg, product_kg, trim_kg, loss_kg, staff_name, memo)
            VALUES (:l, :d, :i, :p, :t, :x, :s, :m)
        """), {'l': lot_id, 'd': _now()[:10], 'i': input_kg, 'p': product_kg, 't': trim_kg, 'x': loss_kg,
               's': staff, 'm': memo})
        yld = f'歩留まり {product_kg / input_kg:.0%}' if input_kg else ''
        _set_status(con, lot_id, 'processed', 'process', staff, ' '.join(x for x in (yld, memo) if x),
                    product_kg, {'current_kg': product_kg})


def weigh_lot(lot_id, kg, staff, memo=''):
    with engine.begin() as con:
        cur = con.execute(text('SELECT status FROM meat_lots WHERE id = :id'), {'id': lot_id}).scalar()
        _set_status(con, lot_id, cur, 'weigh', staff, memo, kg, {'current_kg': kg})


def consume_lot(lot_id, staff, memo=''):
    with engine.begin() as con:
        _set_status(con, lot_id, 'consumed', 'consume', staff, memo, 0, {'current_kg': 0})


def cancel_lot(lot_id, staff, memo=''):
    with engine.begin() as con:
        _set_status(con, lot_id, 'cancelled', 'cancel', staff, memo)


def get_cattle(individual_no):
    with engine.begin() as con:
        row = con.execute(text('SELECT * FROM meat_cattle WHERE individual_no = :n'),
                          {'n': individual_no}).mappings().first()
        return dict(row) if row else None


def save_cattle(individual_no, d, raw=None):
    """個体情報を保存する（自動照会の結果、または検索結果を人が転記したもの）。月齢は出生日とと畜日から計算"""
    from cattle_lookup import months_between
    age = None
    try:
        age = months_between(date.fromisoformat(d.get('birth_date') or ''),
                             date.fromisoformat(d.get('slaughter_date') or ''))
    except ValueError:
        pass
    params = {k: (d.get(k) or None) for k in
              ('birth_date', 'slaughter_date', 'sex', 'breed', 'mother_no',
               'breeder_name', 'breeder_place', 'producer_name', 'producer_place')}
    params.update(n=individual_no, age=age, ts=_now(), raw=raw)
    with engine.begin() as con:
        con.execute(text('DELETE FROM meat_cattle WHERE individual_no = :n'), {'n': individual_no})
        con.execute(text("""
            INSERT INTO meat_cattle (individual_no, birth_date, slaughter_date, age_months, sex, breed, mother_no,
                                     breeder_name, breeder_place, producer_name, producer_place, fetched_at,
                                     raw_result)
            VALUES (:n, :birth_date, :slaughter_date, :age, :sex, :breed, :mother_no,
                    :breeder_name, :breeder_place, :producer_name, :producer_place, :ts, :raw)
        """), params)
    return age


# ============================================================
# ④ 棚卸・月次原価
# ============================================================
# 期間は2種類：月次 '2026-10'、週次 '2026-11-02'（棚卸をする月曜の日付。火〜月の1週間）
# 2026-10-29 決定：毎週月曜（閉店後）に棚卸。火曜は定休日なので在庫は動かない
def is_week(period):
    return len(period) == 10


def week_end(d):
    """その日を含む週の月曜（＝棚卸の日）"""
    return d + timedelta(days=(0 - d.weekday()) % 7)


def period_bounds(period):
    """'2026-10' → ('2026-10-01', '2026-11-01')、'2026-11-02' → ('2026-10-27', '2026-11-03')"""
    if is_week(period):
        mon = date.fromisoformat(period)
        return (mon - timedelta(days=6)).isoformat(), (mon + timedelta(days=1)).isoformat()
    y, m = map(int, period.split('-'))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start.isoformat(), end.isoformat()


def prev_period(period):
    if is_week(period):
        return (date.fromisoformat(period) - timedelta(days=7)).isoformat()
    y, m = map(int, period.split('-'))
    return f'{y - (m == 1)}-{(m - 2) % 12 + 1:02d}'


def next_period(period):
    if is_week(period):
        return (date.fromisoformat(period) + timedelta(days=7)).isoformat()
    y, m = map(int, period.split('-'))
    return f'{y + (m == 12)}-{m % 12 + 1:02d}'


def recent_months(today, n=6):
    """棚卸を開ける月（今月から n か月前まで、新しい順）"""
    out, p = [], today.strftime('%Y-%m')
    for _ in range(n):
        out.append(p)
        p = prev_period(p)
    return out


def period_label(period):
    if is_week(period):
        s, e = period_bounds(period)
        f = lambda d: f'{d.month}/{d.day}'
        return f'{f(date.fromisoformat(s))}(火)〜{f(date.fromisoformat(period))}(月)の週'
    y, m = period.split('-')
    return f'{y}年{int(m)}月'


def _same_day_period(period):
    """同じ日の棚卸を、月次と週次で共有する（例：11/30 は月曜で月末。1回量れば両方に使う）"""
    if is_week(period):
        mon = date.fromisoformat(period)
        return mon.strftime('%Y-%m') if (mon + timedelta(days=1)).day == 1 else None
    _, end = period_bounds(period)
    last = date.fromisoformat(end) - timedelta(days=1)
    return last.isoformat() if last.weekday() == 0 else None


def valuation_prices(period):
    """部位ごとの棚卸評価単価（円/kg）＝ 当月までの最後の仕入単価（最終仕入原価法）"""
    _, end = period_bounds(period)
    with engine.begin() as con:
        rows = con.execute(text("""
            SELECT l.part_id, l.promised_unit_price, sl.slip_date, l.id
            FROM meat_lots l JOIN meat_purchase_slips sl ON sl.id = l.slip_id
            WHERE sl.slip_date < :end AND l.promised_unit_price IS NOT NULL
            ORDER BY sl.slip_date, l.id
        """), {'end': end}).all()
    # ウデ・モモは別々の単価（2026-11-03 社長：分類した時に棚卸の金額が動いてよい）。
    # その部位に仕入の記録がまだ無い時は、同じ集計分類（ウデモモ）の最後の仕入単価。
    # それも無い部位（使い始めの内臓・ハムなど）は、価格表のその期間の値段を使う
    parts = list_parts(include_inactive=True)
    groups = part_groups(parts)
    base = _with_group_prices(price_db.part_prices(end))
    own, group_last = {}, {}
    for r in rows:
        own[r.part_id] = r.promised_unit_price
        if r.part_id in groups:
            group_last[groups[r.part_id]['group']] = r.promised_unit_price
    prices = {}
    for p in parts:
        g = groups[p['id']]['group']
        # 同じ分類の最後の単価で代わりにするのはウデモモだけ。内臓は単価の差が大きい（ハチノス1,000円と
        # センレバハツ3,000円）ので、仕入の無い部位は価格表の値段を使う
        price = (own.get(p['id']) or (group_last.get(g) if g == UDEMOMO_GROUP else None) or base.get(p['id'])
                 or DEFAULT_UNIT_PRICES.get(p['name']) or DEFAULT_UNIT_PRICES.get(g))
        if price:
            prices[p['id']] = price
    return prices


# 価格表の初期値（一度載せた後の値段は価格表で変える）。在庫アプリ（inventory_system/seed_kirinya_items.py）の単価（円/kg）。
# テールは在庫アプリに無く社長に確認
DEFAULT_UNIT_PRICES = {
    'センレバハツ': 3000, 'ホルモン': 3000, 'ハラミ（和牛）': 5100, 'タン（和牛）': 5100, 'ミノ': 3700,
    'トリ': 1550, 'ウデモモ': 4200, 'トモバラ': 2100, 'クラシタ': 4200, 'タン（アメリカ産）': 5800,
    'ヒレ': 11000, 'サーロイン': 7500, 'ブタ': 1600, 'テール': 2000,   # テールは2026-10-29 社長回答
    'ブタカタ': 1000,                                  # 円/kg
    'ウインナー': 274, 'ハム': 298, 'ベーコン': 298,   # 円/パック（PACK_PARTS）
    **{name: price for name, _, _, price in OFFAL_PARTS},   # マルイチの内臓（2026-11-11 社長）
}


def get_count(period):
    with engine.begin() as con:
        head = con.execute(text('SELECT * FROM meat_stock_counts WHERE period = :p'), {'p': period}).mappings().first()
        if not head:
            return None
        head = dict(head)
        head['lines'] = [dict(r) for r in con.execute(text("""
            SELECT c.*, p.name AS part_name FROM meat_stock_count_lines c
            JOIN meat_parts p ON p.id = c.part_id
            WHERE c.count_id = :id ORDER BY p.sort_order, c.id
        """), {'id': head['id']}).mappings()]
    return head


def save_count(period, lines, staff, confirm=False, missing_lot_ids=()):
    """棚卸を保存する。lines: [{'part_id','lot_id','state','kg'}]。
    確定時は、計量したロットの現在重量を更新し、見つからなかったロットを使い切りにする。"""
    prices = valuation_prices(period)
    with engine.begin() as con:
        head = con.execute(text('SELECT id, status FROM meat_stock_counts WHERE period = :p'), {'p': period}).first()
        if head and head.status == 'confirmed':
            raise ValueError(f'{period} の棚卸は確定済みです')
        if head:
            count_id = head.id
            con.execute(text('DELETE FROM meat_stock_count_lines WHERE count_id = :id'), {'id': count_id})
        else:
            res = con.execute(text("""
                INSERT INTO meat_stock_counts (period, status, staff_name) VALUES (:p, 'draft', :s)
            """ + (' RETURNING id' if _IS_PG else '')), {'p': period, 's': staff})
            count_id = res.scalar() if _IS_PG else res.lastrowid
        for ln in lines:
            price = prices.get(ln['part_id'], 0) or 0
            con.execute(text("""
                INSERT INTO meat_stock_count_lines (count_id, part_id, lot_id, state, kg, unit_price, amount)
                VALUES (:c, :p, :l, :st, :kg, :u, :a)
            """), {'c': count_id, 'p': ln['part_id'], 'l': ln.get('lot_id'), 'st': ln.get('state', 'raw'),
                   'kg': ln['kg'], 'u': price, 'a': int(round(ln['kg'] * price))})
        con.execute(text('UPDATE meat_stock_counts SET counted_at = :t, staff_name = :s, status = :st WHERE id = :id'),
                    {'t': _now(), 's': staff, 'st': 'confirmed' if confirm else 'draft', 'id': count_id})
        if confirm:
            _, end = period_bounds(period)
            # さかのぼりの棚卸：期間の後に動きのあった塊は、今の重さ・状態を変えない（棚卸の行だけ残す）
            later = {lid for lid in {ln.get('lot_id') for ln in lines} | set(missing_lot_ids)
                     if lid and _changed_after(con, lid, end, period)}
            lines = [ln for ln in lines if ln.get('lot_id') not in later]
            missing_lot_ids = [lid for lid in missing_lot_ids if lid not in later]
            for ln in lines:
                if ln.get('lot_id'):
                    _set_status(con, ln['lot_id'],
                                con.execute(text('SELECT status FROM meat_lots WHERE id = :id'),
                                            {'id': ln['lot_id']}).scalar(),
                                'weigh', staff, f'{period} 棚卸', ln['kg'], {'current_kg': ln['kg']})
            for lot_id in missing_lot_ids:
                if con.execute(text('SELECT status FROM meat_lots WHERE id = :id'), {'id': lot_id}).scalar() == 'consumed':
                    continue
                _set_status(con, lot_id, 'consumed', 'consume', staff, f'{period} 棚卸で見つからず', 0,
                            {'current_kg': 0})
    return count_id


def unlock_count(period):
    with engine.begin() as con:
        if con.execute(text('SELECT COUNT(*) FROM meat_period_closings WHERE period = :p AND confirmed_at IS NOT NULL'),
                       {'p': period}).scalar():
            raise ValueError(f'{period} は締め済みのため、棚卸を戻せません。先に原価の画面で「締めを解除」してください')
        con.execute(text("UPDATE meat_stock_counts SET status = 'draft' WHERE period = :p"), {'p': period})


def _count_totals(period, confirmed_only=True):
    """棚卸の部位別合計 {part_id: (kg, amount)}。無ければ None"""
    head = get_count(period)
    if not head and _same_day_period(period):
        head = get_count(_same_day_period(period))
    if not head or (confirmed_only and head['status'] != 'confirmed'):
        return None
    totals = {}
    for ln in head['lines']:
        kg, amt = totals.get(ln['part_id'], (0.0, 0))
        totals[ln['part_id']] = (kg + ln['kg'], amt + ln['amount'])
    return totals


def purchases_in_period(period):
    """今期仕入：伝票日付が当月のロットの 金額（取引値×単価）と 実重量 を部位ごとに合計"""
    start, end = period_bounds(period)
    with engine.begin() as con:
        rows = con.execute(text("""
            SELECT l.part_id, COALESCE(SUM(l.promised_amount), 0) AS amount, COALESCE(SUM(l.label_kg), 0) AS kg
            FROM meat_lots l JOIN meat_purchase_slips sl ON sl.id = l.slip_id
            WHERE sl.slip_date >= :s AND sl.slip_date < :e AND l.status != 'cancelled'
            GROUP BY l.part_id
        """), {'s': start, 'e': end}).all()
    return {r.part_id: (float(r.kg), int(r.amount)) for r in rows}


def save_menu_sales(period, rows):
    """出数を保存（その月の分は入れ替え）。rows: [(menu_name, qty, amount)]"""
    with engine.begin() as con:
        con.execute(text('DELETE FROM meat_menu_sales WHERE period = :p'), {'p': period})
        merged = {}
        for name, qty, amount in rows:
            q, a = merged.get(name, (0, 0))
            merged[name] = (q + qty, a + amount)
        for name, (qty, amount) in merged.items():
            con.execute(text('INSERT INTO meat_menu_sales (period, menu_name, qty, amount) VALUES (:p, :n, :q, :a)'),
                        {'p': period, 'n': name, 'q': qty, 'a': amount})


# 【H】（ハーフ）のメニューは、通常サイズのメニューの何人前として数えるか
HALF_PREFIX = '【H】'
HALF_FACTOR = 0.5
# 【H】の名前が通常サイズと違うもの（マジレジの表記 → グラム表のメニュー名）
HALF_ALIASES = {'特選ヘッドバラ': '特選カルビ（ヘッドバラ）', '特上かいのみ': '特上カルビ（かいのみ）'}


def match_menu(pos_name, menu_names):
    """マジレジの商品名を、グラム表のメニュー名に突き合わせる。戻り値 (メニュー名, 何人前) か (None, 0)。
    完全一致 → 「特選カルビ（ヘッドバラ）タレ」のような味付き表記を前方一致で（最長一致）。
    「【H】上カルビ(タレ)」はハーフとして通常サイズの HALF_FACTOR 人前に数える。"""
    key = normalize_menu_name(pos_name)
    factor = 1
    if key.startswith(HALF_PREFIX):
        key = re.sub(r'\((タレ|塩)\)$', '', key[len(HALF_PREFIX):])
        key = normalize_menu_name(HALF_ALIASES.get(key, key))
        factor = HALF_FACTOR
    norm = {normalize_menu_name(m): m for m in menu_names}
    if key in norm:
        return norm[key], factor
    hits = [n for n in norm if n and key.startswith(n)]
    return (norm[max(hits, key=len)], factor) if hits else (None, 0)


def compute_closing(period):
    """月次原価を計算する（保存はしない）。締め済みならその値を返す。"""
    with engine.begin() as con:
        saved = [dict(r) for r in con.execute(text("""
            SELECT c.*, p.name AS part_name, p.sort_order FROM meat_period_closings c
            JOIN meat_parts p ON p.id = c.part_id WHERE c.period = :p ORDER BY p.sort_order
        """), {'p': period}).mappings()]
        sales = [dict(r) for r in con.execute(text(
            'SELECT menu_name, qty, amount FROM meat_menu_sales WHERE period = :p'), {'p': period}).mappings()]
    groups = part_groups()
    for r in saved:   # 締めは代表の部位の番号で持っている → 集計分類の名前で出す
        if r['part_id'] in groups:
            r['part_name'] = groups[r['part_id']]['group']
    menu_map = list_menu_map()
    menu_names = sorted({m['menu_name'] for m in menu_map})

    # 出数をメニュー名に突き合わせる
    qty_by_menu, unmatched = {}, []
    for s in sales:
        m, factor = match_menu(s['menu_name'], menu_names)
        if m:
            qty_by_menu[m] = qty_by_menu.get(m, 0) + s['qty'] * factor
        elif s['qty']:
            unmatched.append(s)

    # 部位ごとの出数と理論使用量（出数×1人前g）
    sales_qty, theo_g = {}, {}
    for mm in menu_map:
        q = qty_by_menu.get(mm['menu_name'], 0)
        sales_qty[mm['part_id']] = sales_qty.get(mm['part_id'], 0) + q
        theo_g[mm['part_id']] = theo_g.get(mm['part_id'], 0) + q * mm['grams_per_serving']

    status = {
        'count_confirmed': _count_totals(period) is not None,
        'opening_available': _count_totals(prev_period(period)) is not None,
        'has_sales': bool(sales),
        'closed': bool(saved) and all(r['confirmed_at'] for r in saved),
    }

    if status['closed']:
        rows = saved
    else:
        opening = _count_totals(prev_period(period)) or {}
        closing = _count_totals(period, confirmed_only=False) or {}
        purchases = purchases_in_period(period)
        rows = []
        all_parts = {p['id']: p for p in list_parts(include_inactive=True)}
        active = [p for p in list_parts() if groups.get(p['id'])]
        seen = []
        for p in active:
            if groups[p['id']]['rep'] not in seen:
                seen.append(groups[p['id']]['rep'])
        for pid in seen:
            g = groups[pid]
            members = [all_parts[m] for m in g['members']]
            if is_week(period) and not any(m.get('count_weekly', 1) for m in members):
                continue   # 月末だけ量る部位は、週次の原価には出さない
            # 前繰・仕入・棚卸は部位ごとの記録を、集計分類で足す（同じ記録を2回足さない）
            def total(src):
                return (sum(src.get(m['id'], (0.0, 0))[0] for m in members),
                        sum(src.get(m['id'], (0.0, 0))[1] for m in members))
            ok, oa = total(opening)
            pk, pa = total(purchases)
            ck, ca = total(closing)
            if not any((ok, oa, pk, pa, ck, ca, sales_qty.get(pid))):
                continue
            breakdown = [{'part_name': m['name'],
                          'opening_kg': opening.get(m['id'], (0.0, 0))[0], 'opening_amount': opening.get(m['id'], (0.0, 0))[1],
                          'purchase_kg': purchases.get(m['id'], (0.0, 0))[0], 'purchase_amount': purchases.get(m['id'], (0.0, 0))[1],
                          'closing_kg': closing.get(m['id'], (0.0, 0))[0], 'closing_amount': closing.get(m['id'], (0.0, 0))[1]}
                         for m in sorted(members, key=lambda x: x['sort_order'])] if len(members) > 1 else []
            usage_amount = oa + pa - ca
            usage_kg = ok + pk - ck
            qty = sales_qty.get(pid, 0)
            theoretical_kg = theo_g.get(pid, 0) / 1000
            rows.append({
                'period': period, 'part_id': pid, 'part_name': g['group'], 'breakdown': breakdown,
                'sort_order': min(m['sort_order'] for m in members),
                'opening_amount': oa, 'purchase_amount': pa, 'closing_amount': ca, 'usage_amount': usage_amount,
                'opening_kg': ok, 'purchase_kg': pk, 'closing_kg': ck, 'usage_kg': usage_kg,
                'sales_qty': qty,
                'cost_per_sale': int(round(usage_amount / qty)) if qty else None,
                'theoretical_kg': theoretical_kg,
                'loss_rate': (usage_kg - theoretical_kg) / usage_kg if usage_kg > 0 else None,
                'confirmed_at': None, 'confirmed_by': None,
            })

    # メニュー別の原価：部位の使用金額を「出数×1人前g」の比で配分（＝部位の1gあたり原価 × 使用g）
    yen_per_g = {r['part_id']: r['usage_amount'] / (r['theoretical_kg'] * 1000)
                 for r in rows if r['theoretical_kg']}
    menus = {}
    for mm in menu_map:
        m = menus.setdefault(mm['menu_name'], {'menu_name': mm['menu_name'], 'parts': [], 'cost': 0.0,
                                                'qty': qty_by_menu.get(mm['menu_name'], 0), 'complete': True})
        m['parts'].append(f"{mm['part_name']} {mm['grams_per_serving']:g}g")
        if mm['part_id'] in yen_per_g:
            m['cost'] += yen_per_g[mm['part_id']] * mm['grams_per_serving']
        else:
            m['complete'] = False
    menu_rows = sorted(menus.values(), key=lambda m: -m['qty'])

    return {
        'period': period, 'rows': rows, 'menus': menu_rows, 'unmatched': unmatched, 'status': status,
        'total': {k: sum(r[k] for r in rows) for k in
                  ('opening_amount', 'purchase_amount', 'closing_amount', 'usage_amount')},
    }


def _closed_rows(con, period):
    return [dict(r) for r in con.execute(text(
        'SELECT * FROM meat_period_closings WHERE period = :p AND confirmed_at IS NOT NULL ORDER BY part_id'),
        {'p': period}).mappings()]


def reopen_closing(period, staff, reason):
    """締めを解除する：解除前の数字と理由を記録してから、保存した数字を消す（原価はまた計算中に戻る）。
    この期間の棚卸は次の期間の前繰になるので、次の期間が締め済みの間は解除しない（新しい方から順に解除する）"""
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('解除の理由を入れてください')
    with engine.begin() as con:
        rows = _closed_rows(con, period)
        if not rows:
            raise ValueError(f'{period_label(period)} は締められていません')
        nxt = next_period(period)
        if _closed_rows(con, nxt):
            raise ValueError(f'次の期間（{period_label(nxt)}）が締め済みです。先にそちらを解除してください')
        con.execute(text("""
            INSERT INTO meat_closing_reopens (period, reason, staff_name, reopened_at, snapshot)
            VALUES (:p, :r, :s, :t, :snap)
        """), {'p': period, 'r': reason, 's': staff, 't': _now(),
               'snap': json.dumps(rows, ensure_ascii=False, default=str)})
        con.execute(text('DELETE FROM meat_period_closings WHERE period = :p'), {'p': period})


def list_closing_reopens(period):
    """締めを解除した記録（新しい順）"""
    with engine.begin() as con:
        return [dict(r) for r in con.execute(text("""
            SELECT id, reason, staff_name, reopened_at FROM meat_closing_reopens
            WHERE period = :p ORDER BY id DESC
        """), {'p': period}).mappings()]


def confirm_closing(period, staff):
    """月を締める：計算結果を保存してロックする。棚卸の確定が前提"""
    if _count_totals(period) is None:
        raise ValueError('先にこの期間の棚卸を確定してください')
    result = compute_closing(period)
    if result['status']['closed']:
        raise ValueError(f'{period} は締め済みです')
    cols = ('opening_amount', 'purchase_amount', 'closing_amount', 'usage_amount', 'opening_kg', 'purchase_kg',
            'closing_kg', 'usage_kg', 'sales_qty', 'cost_per_sale', 'theoretical_kg', 'loss_rate')
    with engine.begin() as con:
        con.execute(text('DELETE FROM meat_period_closings WHERE period = :p'), {'p': period})
        for r in result['rows']:
            con.execute(text(f"""
                INSERT INTO meat_period_closings (period, part_id, {', '.join(cols)}, confirmed_at, confirmed_by)
                VALUES (:period, :part_id, {', '.join(':' + c for c in cols)}, :ts, :by)
            """), dict({c: r[c] for c in cols}, period=period, part_id=r['part_id'], ts=_now(), by=staff))
    return result
