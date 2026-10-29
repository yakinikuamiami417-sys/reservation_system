# import_ledger_once.py — 精肉仕入れ用台帳.xlsx の「今ある在庫」を試し用DBに一度だけ入れる（2026-10-28）
#
# 使い方: DATABASE_URL=sqlite:///meat_trial.db python tools/import_ledger_once.py
# 取り消し線の行は使用済みとして入れない。社長と決めた扱い：
#   - 重量（小数1桁）は取引値として、実重量にも同じ値を入れる（10/31の棚卸で実測に直る）
#   - 仕入日を伝票日付として「納品済み」にする。業者は台帳に無いので「台帳から移行」
#   - 11桁の番号（No.15）は空欄にしてメモに残す。9桁の 168041492（No.24）は 1680941492 とする
#   - ウデ（No.19）は単価4,200円、仕入日は同じ牛のトモバラの 8/12（仮）、10/18 捌き済み＝商品化済み
import os, sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
assert 'meat_trial' in os.environ.get('DATABASE_URL', ''), '試し用DB（meat_trial.db）以外には入れない'

import openpyxl
from sqlalchemy import text
import meat_db as mdb
import meat
from database import engine

LEDGER = r'C:/Users/kirin/OneDrive/デスクトップ/精肉仕入れ用台帳.xlsx'
SHEET_PART = {'ﾄﾓﾊﾞﾗ': 'トモバラ', 'ｸﾗｼﾀ': 'クラシタ', 'ｳﾃﾞ': 'ウデモモ', 'ﾓﾓ': 'ウデモモ', 'ﾋﾚ': 'ヒレ', 'ﾛｰｽ': 'サーロイン'}
SHEET_SUB = {'ｳﾃﾞ': 'ウデ', 'ﾓﾓ': 'モモ'}
STAFF = '台帳から移行'


def xl_date(v):
    return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat() if isinstance(v, (int, float)) else None


def reset_trial_data():
    """試しで入れた塊・伝票・棚卸・出数を消す。マスタと照会済みの個体情報は残す"""
    with engine.begin() as con:
        for t in ('meat_lot_events', 'meat_processing', 'meat_lots', 'meat_purchase_slips', 'meat_photos',
                  'meat_stock_count_lines', 'meat_stock_counts', 'meat_menu_sales', 'meat_period_closings'):
            con.execute(text(f'DELETE FROM {t}'))


def read_rows():
    wb = openpyxl.load_workbook(LEDGER, data_only=True)
    n = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows(min_row=3):
            v = [c.value for c in row[:8]]
            if not any(x is not None for x in v):
                continue
            n += 1
            struck = any(c.font.strike for c in row[:8] if c.value is not None)
            yield n, ws.title, v, struck


def main():
    mdb.init_meat_db()
    reset_trial_data()
    parts = {p['name']: p['id'] for p in mdb.list_parts()}
    sup = mdb.get_or_create_supplier('台帳から移行')
    by_date, nos, processed = {}, [], []
    for n, sheet, v, struck in read_rows():
        if struck:
            continue
        bought, no_raw, price, kg, bb, cut = xl_date(v[0]), v[2], v[4], v[5], xl_date(v[6]), xl_date(v[7])
        memo = [f'台帳No.{n}']
        no = str(no_raw or '')
        if no == '168041492':
            no = '1680941492'
            memo.append('台帳の番号168041492（9桁）を同じ日のモモの番号に修正')
        elif len(no) != 10:
            memo.append(f'台帳の番号 {no}（{len(no)}桁）は誤りのため空欄')
            no = ''
        if bought is None and sheet == 'ｳﾃﾞ':
            bought, price = '2026-08-12', price or 4200
            memo.append('仕入日は台帳空欄のため同じ牛のトモバラの8/12（仮）、単価4,200円は社長確認')
        lot_id = mdb.create_held_lot({
            'part_id': parts[SHEET_PART[sheet]], 'supplier_id': sup, 'promised_on': bought,
            'label_kg': kg, 'trade_kg': kg, 'unit_price': price, 'individual_no': no or None,
            'best_before': bb, 'sub_part_name': SHEET_SUB.get(sheet), 'memo': '／'.join(memo),
        }, STAFF)
        by_date.setdefault(bought, []).append(lot_id)
        nos.append(no)
        if cut:
            processed.append((lot_id, cut))
    for d, ids in sorted(by_date.items()):
        total = sum(mdb.get_lot(i)['promised_amount'] or 0 for i in ids)
        mdb.receive_lots(sup, '台帳から移行', d, total, ids, STAFF)
    with engine.begin() as con:
        for lot_id, cut in processed:
            mdb._set_status(con, lot_id, 'processed', 'process', STAFF, f'台帳：{cut} 捌き済み（捌きの記録は無し）')
    found, errors = meat.lookup_cattle(nos)
    s = mdb.stock_summary()
    print(f'入れた塊: {sum(len(v) for v in by_date.values())}本 / 店内 {s["total_kg"]:.1f}kg ¥{s["total_value"]:,}')
    for p in s['parts']:
        if p['lots']:
            print(f'  {p["name"]}: {len(p["lots"])}本 {p["total_kg"]:.1f}kg ¥{p["value"]:,}')
    print(f'個体照会: 新しく{found}頭', *errors, sep='\n  ')


if __name__ == '__main__':
    main()
