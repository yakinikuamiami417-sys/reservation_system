# migrate_meat_data.py — 正肉在庫のデータ（meat_ で始まる表）を、試し用DBから本番DBへ移す
#
# 使い方（このパソコンから。本番のURLは Render の「External Database URL」）:
#   1) 確認だけ（何も書き込まない）
#      python tools/migrate_meat_data.py --source sqlite:///meat_trial.db --target "postgresql://..."
#   2) 実際に移す
#      python tools/migrate_meat_data.py --source sqlite:///meat_trial.db --target "postgresql://..." --commit
#   --target を省くと、.env ファイルの MEAT_TARGET_DATABASE_URL= の行を使う
#   （本番DBのURLにはパスワードが入っているので、チャットや画面に出さずに済むようにするため。.env はGitに入らない）
#
# 移すのは meat_ で始まる表だけ。予約・顧客・POS売上の表には一切触れない。
# 本番の正肉の表に塊が既に入っている時は止まる（二重に移さないため）。
import argparse, os, sys

from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TABLES = [
    'meat_suppliers', 'meat_parts', 'meat_part_aliases', 'meat_menu_map', 'meat_photos',
    'meat_purchase_slips', 'meat_lots', 'meat_lot_events', 'meat_processing', 'meat_cattle',
    'meat_stock_counts', 'meat_stock_count_lines', 'meat_menu_sales', 'meat_period_closings',
]
# 中身が合っているかを確かめる合計（表, 式）
CHECKS = [
    ('meat_lots', 'COALESCE(SUM(label_kg), 0)'),
    ('meat_lots', 'COALESCE(SUM(current_kg), 0)'),
    ('meat_lots', 'COALESCE(SUM(promised_amount), 0)'),
    ('meat_purchase_slips', 'COALESCE(SUM(total_amount), 0)'),
    ('meat_stock_count_lines', 'COALESCE(SUM(amount), 0)'),
]


def _url(u):
    return u.replace('postgres://', 'postgresql://', 1) if u.startswith('postgres://') else u


def target_from_env_file():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if os.path.exists(path):
        with open(path, encoding='utf-8-sig') as f:
            for line in f:
                key, _, val = line.strip().partition('=')
                if key == 'MEAT_TARGET_DATABASE_URL' and val:
                    return val.strip().strip('"')
    return os.environ.get('MEAT_TARGET_DATABASE_URL')


def _show(url):
    """画面にはパスワードを出さない"""
    return url.split('@')[-1] if '@' in url else url


def prepare_target(target_url):
    """本番側に正肉の表を作る（アプリの初期化と同じ処理。既にあれば何もしない）"""
    os.environ['DATABASE_URL'] = target_url
    import meat_db
    meat_db.init_meat_db()


def counts(con, tables):
    return {t: con.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar() for t in tables}


def totals(con):
    return [round(float(con.execute(text(f'SELECT {expr} FROM {t}')).scalar()), 3) for t, expr in CHECKS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True)
    ap.add_argument('--target', help='省略時は .env の MEAT_TARGET_DATABASE_URL')
    ap.add_argument('--commit', action='store_true', help='実際に書き込む（付けなければ確認だけ）')
    a = ap.parse_args()
    target = a.target or target_from_env_file()
    if not target:
        sys.exit('移す先がありません。--target を付けるか、.env に MEAT_TARGET_DATABASE_URL= を書いてください')
    src_url, dst_url = _url(a.source), _url(target)
    if src_url == dst_url:
        sys.exit('移す元と移す先が同じです')

    src = create_engine(src_url)
    with src.connect() as con:
        missing = [t for t in TABLES if not inspect(src).has_table(t)]
        if missing:
            sys.exit(f'移す元に表がありません: {missing}')
        src_counts, src_totals = counts(con, TABLES), totals(con)
    print('移す元の件数:')
    for t, n in src_counts.items():
        print(f'  {t:24s} {n}')

    prepare_target(dst_url)
    dst = create_engine(dst_url)
    is_pg = dst_url.startswith('postgresql')
    with dst.connect() as con:
        if con.execute(text('SELECT COUNT(*) FROM meat_lots')).scalar():
            sys.exit('本番の正肉の表に、すでに塊が入っています。二重に移さないため止めました。')
        other = [t for t in inspect(dst).get_table_names() if not t.startswith('meat_')]
    print(f'移す先: {_show(dst_url)}（予約などの表 {len(other)} 個には触れません）')

    if not a.commit:
        print('\n確認だけ行いました。実際に移すには --commit を付けて実行してください。')
        return

    with src.connect() as s, dst.begin() as d:
        for t in TABLES:
            rows = [dict(r) for r in s.execute(text(f'SELECT * FROM {t}')).mappings()]
            d.execute(text(f'DELETE FROM {t}'))   # 本番側の初期マスタは、試し用で育てたマスタに置き換える
            if rows:
                cols = list(rows[0].keys())
                d.execute(text(f'INSERT INTO {t} ({", ".join(cols)}) VALUES ({", ".join(":" + c for c in cols)})'),
                          rows)
        if is_pg:
            # id を指定して入れたので、次に振られる番号を最大値の次に合わせる
            for t in TABLES:
                if 'id' in [c['name'] for c in inspect(dst).get_columns(t)]:
                    d.execute(text(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                                   f"COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)"))

    with dst.connect() as con:
        dst_counts, dst_totals = counts(con, TABLES), totals(con)
    ok = dst_counts == src_counts and dst_totals == src_totals
    print('\n移した後の確認:', '件数・重量・金額の合計がすべて一致しました' if ok else '一致しません！')
    if not ok:
        for t in TABLES:
            if src_counts[t] != dst_counts[t]:
                print(f'  {t}: 元 {src_counts[t]} / 先 {dst_counts[t]}')
        print(f'  合計: 元 {src_totals} / 先 {dst_totals}')
        sys.exit(1)


if __name__ == '__main__':
    main()
