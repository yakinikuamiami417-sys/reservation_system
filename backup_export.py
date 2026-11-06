# backup_export.py — 予約全件・顧客集計・POS売上明細・正肉在庫・価格表をまとめたExcelを作る共通ロジック
# app.py の /backup/xlsx（手動ダウンロード）と daily_backup.py（毎日自動メール送信）の
# 両方から使われる。出力内容がズレないよう、ここに一本化している。
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import get_column_letter
from sqlalchemy import inspect, text

from database import engine, get_all_reservations, get_pos_sales
from logic import aggregate_customer_ranking

JST = timezone(timedelta(hours=9))

# 正肉在庫・価格表は表をそのまま1シートずつ入れる。新しい表が増えても自動で入る（名前が無ければ表名をシート名にする）
DATA_TABLE_PREFIXES = ('meat_', 'price_')
EXCLUDED_TABLES = ('meat_photos',)   # ラベル写真。容量が大きく、Excelのセルにも入らない
SHEET_LABELS = {
    'meat_lots': '正肉_塊', 'meat_lot_events': '正肉_塊の記録', 'meat_purchase_slips': '正肉_伝票',
    'meat_processing': '正肉_捌き', 'meat_stock_counts': '正肉_棚卸', 'meat_stock_count_lines': '正肉_棚卸の明細',
    'meat_period_closings': '正肉_締め', 'meat_closing_reopens': '正肉_締めの解除', 'meat_menu_sales': '正肉_出数',
    'meat_menu_map': '正肉_グラム表', 'meat_parts': '正肉_部位', 'meat_part_aliases': '正肉_品名の読み替え',
    'meat_suppliers': '正肉_業者', 'meat_cattle': '正肉_個体情報',
    'price_items': '価格表_品目', 'price_history': '価格表_履歴',
}


def _excel_value(v):
    """Excelに入れられる形にする（制御文字はExcelがファイルを開けなくなるので取り除く）"""
    if v is None or isinstance(v, (bool, int, float, Decimal, date, datetime)):
        return v
    return ILLEGAL_CHARACTERS_RE.sub('', str(v))


def _append_data_tables(wb):
    """meat_ / price_ の表を1シートずつ足す。先頭に目次（シート名・表名・件数）を置いて、抜けを確かめられるようにする。
    まだ表が無いDBでも止まらない（予約のバックアップを巻き添えにしない）"""
    tables = sorted(t for t in inspect(engine).get_table_names()
                    if t.startswith(DATA_TABLE_PREFIXES) and t not in EXCLUDED_TABLES)
    index = wb.create_sheet('正肉・価格表の目次')
    index.append(['シート', '表の名前', '件数'])
    sheets = [index]
    with engine.begin() as con:
        for table in tables:
            result = con.execute(text(f'SELECT * FROM {table} ORDER BY 1'))
            label = SHEET_LABELS.get(table, table)[:31]
            ws = wb.create_sheet(label)
            ws.append(list(result.keys()))
            count = 0
            for row in result:
                ws.append([_excel_value(v) for v in row])
                count += 1
            index.append([label, table, count])
            sheets.append(ws)
    return sheets


def today_jst() -> date:
    return datetime.now(JST).date()


def build_backup_workbook_bytes() -> bytes:
    all_res   = get_all_reservations(include_cancelled=True)
    customers = aggregate_customer_ranking([r for r in all_res if r['status'] != 'cancelled'])
    customers.sort(key=lambda c: -c['visit_count'])
    pos_rows  = get_pos_sales()

    wb = Workbook()

    ws1 = wb.active
    ws1.title = '予約全件'
    ws1.append([
        'ID', '日付', '時間', '氏名', '電話番号', '受付担当者',
        '大人', '子供', '合計人数', '利用時間(分)',
        '個室希望', 'VIP', '団体', '予算/人', 'ニーズ',
        '男性', '女性', '幹事メモ', '備考', 'メニュー', '売上',
        'テーブル', 'ステータス', '来店受付日時', '登録日時', '更新日時',
    ])
    PRIV_LEVEL_LABEL = {'required': '必ず個室', 'preferred': 'できれば個室'}
    for r in all_res:
        ws1.append([
            r['id'], r['date'], r['time_slot'], r['name'], r['phone'], r.get('staff_name') or '',
            r['adults'], len(r['children_info']), r['total_people'], r['duration_minutes'],
            (PRIV_LEVEL_LABEL.get(r.get('private_room_level'), '○') if r['private_room'] else ''),
            '○' if r['is_vip'] else '', '○' if r['is_group'] else '',
            r.get('budget_per_person') or '', r.get('needs_type') or '',
            r.get('gender_male') or '', r.get('gender_female') or '',
            r.get('organizer_note') or '', r.get('notes') or '',
            r.get('menu_note') or '', r.get('sales_amount') or '',
            '・'.join(str(t) for t in r['assigned_tables']), r['status'], r.get('visited_at') or '',
            r['created_at'], r['updated_at'],
        ])

    ws2 = wb.create_sheet('顧客集計')
    ws2.append(['氏名', '電話番号', '来店回数', '累計売上', '平均単価/回',
                '初回来店', '前回来店', '前回からの経過日数', '最終来店受付日時'])
    for c in customers:
        ws2.append([
            c['name'], c['phone'], c['visit_count'], c['total_sales'], c['avg_sales'],
            c['first_visit_date'], c['last_visit_date'], c['days_since_last_visit'],
            c.get('last_visited_at') or '',
        ])

    ws3 = wb.create_sheet('POS売上明細')
    ws3.append(['日付', '時刻', '伝票番号', '卓番号', '客数', '商品名', '金額',
                'お客様名', '電話番号', '突合ステータス', '紐付け予約ID', 'インポート回次'])
    for r in pos_rows:
        ws3.append([
            r['sale_date'], r['sale_time'] or '', r['receipt_no'] or '', r['table_no'] or '',
            r['party_size'] or '', r['item_name'] or '', r['amount'],
            r['customer_name'] or '', r['phone'] or '',
            r['match_status'], r['matched_reservation_id'] or '', r['import_batch'],
        ])

    for ws in [ws1, ws2, ws3] + _append_data_tables(wb):
        for col_cells in ws.columns:
            length = max((len(str(cell.value)) for cell in col_cells if cell.value is not None), default=8)
            ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(length + 2, 40)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
