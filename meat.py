# meat.py — 正肉の仕入・在庫管理の画面（Flask Blueprint）
# 見るのはログインした全員（仕入単価・原価も社内公開：2026-10-26 確認）。
# 登録・修正・月の締めは管理者パスワード（MEAT_ADMIN_PASSWORD）を入れた時だけできる。
import csv, io, os, base64, time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, Response, abort

import meat_db as mdb
import label_reader
import cattle_lookup

bp = Blueprint('meat', __name__, url_prefix='/meat')

MEAT_ADMIN_PASSWORD = os.environ.get('MEAT_ADMIN_PASSWORD', '')
# 家畜改良センターへの自動照会（2026-10-28 社長同意）。止めたい時は NLBC_AUTO_LOOKUP=0
NLBC_AUTO_LOOKUP = os.environ.get('NLBC_AUTO_LOOKUP', '1') != '0'
_JST = timezone(timedelta(hours=9))


def _today():
    return datetime.now(_JST).date()


def _this_period():
    return _today().strftime('%Y-%m')


def _this_week():
    """今週の締めの日（月曜）。毎週月曜の閉店後に棚卸をする（2026-10-29 決定）"""
    return mdb.week_end(_today()).isoformat()


def _valid_period(period):
    try:
        mdb.period_bounds(period)
        return True
    except ValueError:
        return False


def is_admin():
    # パスワード未設定（手元のパソコンで試す時）は誰でも登録できる。本番では必ず設定する
    return not MEAT_ADMIN_PASSWORD or session.get('meat_admin')


def staff_name():
    return session.get('meat_staff') or ''


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not is_admin():
            if request.is_json or request.path.startswith('/meat/api/') or request.headers.get('X-Requested-With') == 'fetch':
                return jsonify({'error': '登録には管理者パスワードが必要です'}), 403
            flash('登録・修正には管理者パスワードが必要です', 'warning')
            return redirect(url_for('meat.unlock', next=request.referrer or url_for('meat.index')))
        return f(*a, **kw)
    return wrapper


@bp.app_context_processor
def _ctx():
    return {'meat_is_admin': is_admin(), 'meat_staff': staff_name(), 'meat_period': _this_period(),
            'meat_week': _this_week(), 'period_label': mdb.period_label}


def lookup_cattle(individual_nos):
    """まだ照会していない個体識別番号だけを家畜改良センターに照会して保存する。
    戻り値: (照会できた件数, 失敗メッセージのリスト)。失敗しても登録は止めない"""
    done, errors = 0, []
    if not NLBC_AUTO_LOOKUP:
        return done, errors
    for no in dict.fromkeys(n for n in individual_nos if n):
        if mdb.get_cattle(no) or not cattle_lookup.is_valid_individual_no(no):
            continue
        if done or errors:
            time.sleep(1)   # 続けて照会する時はサーバーに負担をかけないよう間を空ける
        try:
            info = cattle_lookup.lookup(no)
            mdb.save_cattle(no, info, raw=info.get('raw'))
            done += 1
        except cattle_lookup.CattleLookupError as e:
            errors.append(f'{no}：{e}')
    return done, errors


def _float(v):
    try:
        return float(str(v).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


# ============================================================
# 管理者の切り替え
# ============================================================
@bp.route('/unlock', methods=['GET', 'POST'])
def unlock():
    nxt = request.args.get('next') or url_for('meat.index')
    if request.method == 'POST':
        name = request.form.get('staff', '').strip()
        if not name:
            flash('入力者の名前を入れてください', 'warning')
        elif MEAT_ADMIN_PASSWORD and request.form.get('pw') != MEAT_ADMIN_PASSWORD:
            flash('パスワードが違います', 'danger')
        else:
            session['meat_admin'] = True
            session['meat_staff'] = name
            return redirect(nxt)
    return render_template('meat/unlock.html', next=nxt, need_pw=bool(MEAT_ADMIN_PASSWORD))


@bp.route('/lock')
def lock():
    session.pop('meat_admin', None)
    return redirect(url_for('meat.index'))


# ============================================================
# 在庫一覧（主画面）
# ============================================================
@bp.route('/')
def index():
    tab = request.args.get('tab', 'house')
    return render_template('meat/index.html', s=mdb.stock_summary(), tab=tab, today=_today(),
                           period=_this_period())


@bp.route('/lot/<int:lot_id>')
def lot_detail(lot_id):
    lot = mdb.get_lot(lot_id)
    if not lot:
        abort(404)
    groups = mdb.part_groups()
    parts = mdb.list_parts()
    g = groups.get(lot['part_id'], {})
    # 同じ集計分類の中で付け替えられる部位（ウデモモ：ウデ・モモ・部位未分類）
    siblings = [p for p in parts if p['id'] in g.get('members', []) and p['id'] != lot['part_id']] \
        if len(g.get('members', [])) > 1 else []
    return render_template('meat/lot.html', lot=lot, parts=parts, siblings=siblings,
                           group_name=g.get('group'), nlbc_url=cattle_lookup.SEARCH_URL)


@bp.route('/lot/<int:lot_id>/reclassify', methods=['POST'])
@admin_required
def lot_reclassify(lot_id):
    try:
        mdb.reclassify_lot(lot_id, request.form.get('part_id'), staff_name(), request.form.get('memo', '').strip())
        flash('部位を分類しました（数量・金額・日付はそのまま。ウデモモの合計は変わりません）', 'success')
    except (ValueError, TypeError) as e:
        flash(str(e) or '部位を選んでください', 'danger')
    return redirect(url_for('meat.lot_detail', lot_id=lot_id))


@bp.route('/photo/<int:photo_id>')
def photo(photo_id):
    p = mdb.get_photo(photo_id)
    if not p:
        abort(404)
    return Response(base64.b64decode(p['data_b64']), mimetype=p['mime'],
                    headers={'Cache-Control': 'private, max-age=86400'})


# ============================================================
# ① 目利き（業者預け）
# ============================================================
@bp.route('/new', methods=['GET', 'POST'])
@admin_required
def new_lots():
    parts = mdb.list_parts()
    suppliers = mdb.list_suppliers()
    if request.method == 'POST':
        f = request.form
        supplier_name = f.get('supplier_name', '').strip()
        if not supplier_name:
            flash('業者を選んでください', 'warning')
            return redirect(url_for('meat.new_lots'))
        supplier_id = mdb.get_or_create_supplier(supplier_name)
        count, nos = 0, []
        for i in range(int(f.get('piece_count', 0) or 0)):
            g = lambda k: f.get(f'p{i}_{k}', '').strip()
            if not g('part_id') or g('skip'):
                continue
            mdb.create_held_lot({
                'part_id': g('part_id'), 'supplier_id': supplier_id,
                'label_item_name': g('label_item_name'), 'sub_part_name': g('sub_part_name'),
                'label_kg': g('label_kg'), 'trade_kg': g('trade_kg'), 'unit_price': g('unit_price'),
                'individual_no': cattle_lookup.normalize_individual_no(g('individual_no')) or None,
                'best_before': g('best_before'), 'processed_date': g('processed_date'),
                'branch_no': g('branch_no'), 'serial_no': g('serial_no'), 'slaughter_no': g('slaughter_no'),
                'origin': g('origin'), 'brand': g('brand'), 'processor_name': g('processor_name'),
                'label_photo_id': f.get('photo_id') or None, 'ocr_raw': f.get('ocr_raw') or None,
                'promised_on': f.get('promised_on') or _today().isoformat(),
            }, staff_name())
            count += 1
            nos.append(cattle_lookup.normalize_individual_no(g('individual_no')))
        flash(f'業者預けに {count} 件登録しました', 'success')
        found, errors = lookup_cattle(nos)
        if found:
            flash(f'家畜改良センターで {found} 頭の個体情報（生産者・月齢）を取得しました', 'success')
        for e in errors:
            flash(f'個体情報を自動で取得できませんでした（{e}）。塊の画面から手で入れられます', 'warning')
        if f.get('next_action') == 'continue':
            return redirect(url_for('meat.new_lots', supplier=supplier_name))
        return redirect(url_for('meat.index', tab='held'))
    last_prices = {p['id']: mdb.last_unit_price(p['id']) for p in parts}
    return render_template('meat/new.html', parts=mdb.entry_parts(), suppliers=suppliers, last_prices=last_prices,
                           today=_today(), supplier=request.args.get('supplier', ''),
                           reader_ready=label_reader.is_configured())


@bp.route('/api/cattle/<no>')
@admin_required
def api_cattle(no):
    """目利きの画面で個体識別番号を10桁打った時点で呼ぶ。照会済みなら保存した値をすぐ返す"""
    no = cattle_lookup.normalize_individual_no(no)
    if not cattle_lookup.is_valid_individual_no(no):
        return jsonify({'error': '個体識別番号は10桁の数字です'}), 400
    c = mdb.get_cattle(no)
    if not c:
        if not NLBC_AUTO_LOOKUP:
            return jsonify({'error': '自動照会は止めてあります'})
        try:
            info = cattle_lookup.lookup(no)
        except cattle_lookup.CattleLookupError as e:
            return jsonify({'error': str(e)})
        mdb.save_cattle(no, info, raw=info.get('raw'))
        c = mdb.get_cattle(no)
    keys = ('individual_no', 'birth_date', 'slaughter_date', 'age_months', 'sex', 'breed',
            'producer_name', 'producer_place', 'breeder_name', 'breeder_place')
    return jsonify({k: c.get(k) for k in keys})


@bp.route('/api/read-label', methods=['POST'])
@admin_required
def api_read_label():
    """写真を保存して読み取り、部位の候補と単価の初期値を付けて返す"""
    body = request.get_json(silent=True) or {}
    data_url = body.get('image', '')
    if ',' in data_url:
        header, b64 = data_url.split(',', 1)
        mime = header.split(';')[0].replace('data:', '') or 'image/jpeg'
    else:
        b64, mime = data_url, 'image/jpeg'
    if not b64:
        return jsonify({'error': '写真がありません'}), 400
    photo_id = mdb.save_photo(b64, mime)
    try:
        result = label_reader.read_label(b64, mime)
    except label_reader.LabelReadError as e:
        return jsonify({'error': str(e), 'photo_id': photo_id})
    for p in result['pieces']:
        part_id, sub = mdb.resolve_alias(p.get('label_item_name'), p.get('processor_name'))
        p['part_id'] = part_id
        p['sub_part_name'] = sub or ''
        p['unit_price'] = mdb.last_unit_price(part_id) if part_id else None
        p['warnings'] = label_reader.check_piece(p, _today())
    return jsonify({'photo_id': photo_id, 'pieces': result['pieces'], 'notes': result.get('notes', ''),
                    'raw': result['raw']})


# ============================================================
# ② 納品・伝票
# ============================================================
@bp.route('/receive', methods=['GET', 'POST'])
@admin_required
def receive():
    if request.method == 'POST':
        f = request.form
        lot_ids = [int(x) for x in f.getlist('lot_id')]
        if not lot_ids:
            flash('納品する塊を選んでください', 'warning')
            return redirect(url_for('meat.receive'))
        supplier_id = int(f['supplier_id'])
        total = int(_float(f.get('total_amount')) or 0)
        _, lines_total = mdb.receive_lots(supplier_id, f.get('slip_no', '').strip(),
                                          f.get('slip_date') or _today().isoformat(), total, lot_ids,
                                          staff_name())
        if total and total != lines_total:
            flash(f'納品にしました。ただし伝票合計 {total:,}円 と、目利きの金額の合計 {lines_total:,}円 が'
                  f' {total - lines_total:+,}円 違います。単価・取引値を確認してください', 'warning')
        else:
            flash(f'{len(lot_ids)} 件を納品済みにしました（合計 {lines_total:,}円）', 'success')
        return redirect(url_for('meat.index'))
    held = mdb.list_lots(('held',))
    groups = {}
    for lot in held:
        groups.setdefault((lot['supplier_id'], lot['supplier_name']), []).append(lot)
    return render_template('meat/receive.html', groups=groups, today=_today())


# ============================================================
# ③ 捌き・計量・使い切り・取消・個体情報
# ============================================================
@bp.route('/lot/<int:lot_id>/process', methods=['POST'])
@admin_required
def lot_process(lot_id):
    f = request.form
    input_kg, product_kg = _float(f.get('input_kg')), _float(f.get('product_kg'))
    if not input_kg or product_kg is None:
        flash('捌く前の重量と、商品になった重量を入れてください', 'warning')
    else:
        mdb.process_lot(lot_id, input_kg, product_kg, _float(f.get('trim_kg')), _float(f.get('loss_kg')),
                        staff_name(), f.get('memo', '').strip())
        flash(f'商品化を記録しました（歩留まり {product_kg / input_kg:.0%}）', 'success')
    return redirect(url_for('meat.lot_detail', lot_id=lot_id))


@bp.route('/lot/<int:lot_id>/weigh', methods=['POST'])
@admin_required
def lot_weigh(lot_id):
    kg = _float(request.form.get('kg'))
    if kg is None:
        flash('重量を入れてください', 'warning')
    else:
        mdb.weigh_lot(lot_id, kg, staff_name(), request.form.get('memo', '').strip())
        flash('計量を記録しました', 'success')
    return redirect(url_for('meat.lot_detail', lot_id=lot_id))


@bp.route('/lot/<int:lot_id>/consume', methods=['POST'])
@admin_required
def lot_consume(lot_id):
    mdb.consume_lot(lot_id, staff_name())
    flash('使い切りにしました', 'success')
    return redirect(url_for('meat.index'))


@bp.route('/lot/<int:lot_id>/cancel', methods=['POST'])
@admin_required
def lot_cancel(lot_id):
    lot = mdb.get_lot(lot_id)
    if lot and lot['status'] == 'held':
        mdb.cancel_lot(lot_id, staff_name(), request.form.get('memo', '').strip())
        flash('買付を取り消しました', 'success')
    return redirect(url_for('meat.index', tab='held'))


@bp.route('/lot/<int:lot_id>/unreceive', methods=['POST'])
@admin_required
def lot_unreceive(lot_id):
    want_json = request.headers.get('X-Requested-With') == 'fetch'
    try:
        mdb.unreceive_lot(lot_id, staff_name(), request.form.get('memo', '').strip())
    except ValueError as e:
        if want_json:
            return jsonify({'error': str(e)}), 400
        flash(str(e), 'danger')
        return redirect(url_for('meat.lot_detail', lot_id=lot_id))
    if want_json:
        return jsonify({'ok': True})
    flash('納品を取り消して、目利き（業者預け）に戻しました', 'success')
    return redirect(url_for('meat.lot_detail', lot_id=lot_id))


@bp.route('/lot/<int:lot_id>/lookup', methods=['POST'])
@admin_required
def lot_lookup(lot_id):
    lot = mdb.get_lot(lot_id)
    if not lot or not lot['individual_no']:
        abort(404)
    try:
        info = cattle_lookup.lookup(lot['individual_no'])
        age = mdb.save_cattle(lot['individual_no'], info, raw=info.get('raw'))
        flash('家畜改良センターで個体情報を取得しました' + (f'（月齢 {age}か月）' if age is not None else ''), 'success')
    except cattle_lookup.CattleLookupError as e:
        flash(f'{e}。下の欄に手で入れることもできます', 'warning')
    return redirect(url_for('meat.lot_detail', lot_id=lot_id))


@bp.route('/lot/<int:lot_id>/cattle', methods=['POST'])
@admin_required
def lot_cattle(lot_id):
    lot = mdb.get_lot(lot_id)
    if not lot or not lot['individual_no']:
        abort(404)
    age = mdb.save_cattle(lot['individual_no'], request.form)
    flash('個体情報を保存しました' + (f'（月齢 {age}か月）' if age is not None else ''), 'success')
    return redirect(url_for('meat.lot_detail', lot_id=lot_id))


# ============================================================
# ④ 棚卸（毎週月曜の閉店後。月末にも1回）
# ============================================================
@bp.route('/count/<period>', methods=['GET', 'POST'])
def count(period):
    head = mdb.get_count(period)
    if request.method == 'POST':
        if not is_admin():
            return admin_required(lambda: None)()
        f = request.form
        if f.get('action') == 'unlock':
            try:
                mdb.unlock_count(period)
                flash('棚卸を入力中に戻しました', 'success')
            except ValueError as e:
                flash(str(e), 'danger')
            return redirect(url_for('meat.count', period=period))
        lines, missing = [], []
        for lot in mdb.lots_in_house_for_count(period):
            key = f'lot_{lot["id"]}'
            if f.get(key + '_missing'):
                missing.append(lot['id'])
                continue
            kg = _float(f.get(key))
            if kg is None:
                continue
            lines.append({'part_id': lot['part_id'], 'lot_id': lot['id'], 'kg': kg,
                          'state': 'product' if lot['status'] == 'processed' else 'raw'})
        for i in range(int(f.get('extra_count', 0) or 0)):
            pid, kg = f.get(f'extra_{i}_part'), _float(f.get(f'extra_{i}_kg'))
            if pid and kg:
                lines.append({'part_id': int(pid), 'lot_id': None, 'kg': kg, 'state': f.get(f'extra_{i}_state', 'raw')})
        confirm = f.get('action') == 'confirm'
        if confirm:
            # 開けた塊を量り忘れると、残りが0kgとして数えられて原価が高く出る。確定の前に止める
            counted_parts = {ln['part_id'] for ln in lines}
            forgot = [f'{l["part_name"]} #{l["id"]}' for l in mdb.list_lots(('processed',))
                      if l['id'] not in missing and l['part_id'] not in counted_parts
                      and f'lot_{l["id"]}' in f]
            blank = [f'{l["part_name"]} #{l["id"]}' for l in mdb.list_lots(('received',))
                     if l['id'] not in missing and f'lot_{l["id"]}' in f and not f.get(f'lot_{l["id"]}', '').strip()]
            if blank:
                mdb.save_count(period, lines, staff_name())
                flash('重さが空欄の未開封の箱があります：' + '、'.join(blank) +
                      '。空欄は0kgとして数えません。重さを入れるか、無ければ「無い」にしてください（一時保存しました）', 'warning')
                return redirect(url_for('meat.count', period=period))
            if forgot:
                mdb.save_count(period, lines, staff_name())
                flash('開けた塊の重さが入っていません：' + '、'.join(forgot) +
                      '。量って入れるか、使い切っていれば「無い」にしてください（一時保存しました）', 'warning')
                return redirect(url_for('meat.count', period=period))
        try:
            mdb.save_count(period, lines, staff_name(), confirm=confirm, missing_lot_ids=missing if confirm else ())
        except ValueError as e:
            flash(str(e), 'danger')
            return redirect(url_for('meat.count', period=period))
        flash('棚卸を確定しました' if confirm else '棚卸を一時保存しました', 'success')
        return redirect(url_for('meat.closing', period=period) if confirm else url_for('meat.count', period=period))
    if not _valid_period(period):
        abort(404)
    saved = {ln['lot_id']: ln['kg'] for ln in (head['lines'] if head else []) if ln['lot_id']}
    extras = [ln for ln in (head['lines'] if head else []) if not ln['lot_id']]
    parts = mdb.list_parts()
    week = mdb.is_week(period)
    weekly_ids = {p['id'] for p in parts if p.get('count_weekly', 1)}
    lots = [l for l in mdb.lots_in_house_for_count(period) if not week or l['part_id'] in weekly_ids]
    # 未開封の箱（納品済みのまま）は量らなくてよいので、ラベルの重さを最初から入れておく
    prefill = {l['id']: round(l['kg_now'], 2) for l in lots if l['status'] == 'received'}
    return render_template('meat/count.html', period=period, head=head, lots=lots, saved=saved, prefill=prefill,
                           extras=extras, parts=[p for p in parts if not week or p['id'] in weekly_ids],
                           monthly_only=[p['name'] for p in parts if p['id'] not in weekly_ids] if week else [],
                           is_week=week, prices=mdb.valuation_prices(period))


# ============================================================
# 月次原価
# ============================================================
_NAME_COLS = ('メニュー名', '商品名', '品名', 'メニュー', '商品', '印字メニュー名1')
_QTY_COLS = ('売上数量', '数量', '出数', '点数', '販売数', '個数', '売上数')
_AMOUNT_COLS = ('金額', '売上金額', '売上', '小計', '合計金額')


def _sales_rows(table):
    """表（行のリスト）から (メニュー名, 数量, 金額) を取り出す。
    マジレジの「メニュー別累計」は、題名2行のあとに 品番・メニュー名・売価・売上数量・金額 の見出しがある。
    見出しの無いCSV（Dropbox の 2025.09.csv）は 品番,メニュー名,売価,数量,金額 の並びとして読む。"""
    table = [['' if c is None else str(c).strip() for c in r] for r in table]
    ni = qi = ai = None
    for h, cols in enumerate(table[:20]):
        pick = lambda names: next((cols.index(n) for n in names if n in cols), None)
        ni, qi, ai = pick(_NAME_COLS), pick(_QTY_COLS), pick(_AMOUNT_COLS)
        if ni is not None and qi is not None:
            body = table[h + 1:]
            break
    else:
        first = next((r for r in table if any(r)), [])
        if len(first) >= 5 and first[0].isdigit() and _float(first[3]) is not None:
            ni, qi, ai, body = 1, 3, 4, table
        else:
            raise ValueError('「メニュー名」と「売上数量」の列が見つかりませんでした')
    out = []
    for r in body:
        if len(r) <= max(ni, qi) or not r[ni]:
            continue
        qty = _float(r[qi])
        if qty is None:
            continue
        amt = _float(r[ai]) if ai is not None and ai < len(r) else 0
        out.append((r[ni], int(qty), int(amt or 0)))
    return out


def parse_sales_file(raw_bytes, filename=''):
    """マジレジの出数ファイル（Excel の「出数データ」シート、または CSV）を読む"""
    if filename.lower().endswith(('.xlsx', '.xlsm')) or raw_bytes[:2] == b'PK':
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
        ws = wb['出数データ'] if '出数データ' in wb.sheetnames else wb.worksheets[0]
        return _sales_rows(list(ws.iter_rows(values_only=True)))
    for enc in ('utf-8-sig', 'cp932'):
        try:
            content = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('文字コードを読み取れませんでした')
    return _sales_rows(list(csv.reader(io.StringIO(content))))


@bp.route('/closing/<period>', methods=['GET', 'POST'])
def closing(period):
    if request.method == 'POST':
        if not is_admin():
            return admin_required(lambda: None)()
        action = request.form.get('action')
        if action == 'upload':
            file = request.files.get('sales_csv')
            try:
                rows = parse_sales_file(file.read(), file.filename or '') if file else []
            except ValueError as e:
                flash(str(e), 'danger')
                return redirect(url_for('meat.closing', period=period))
            mdb.save_menu_sales(period, rows)
            flash(f'出数を {len(rows)} 行取り込みました', 'success')
        elif action == 'manual':
            rows = []
            for k, v in request.form.items():
                if k.startswith('qty__') and _float(v):
                    rows.append((k[5:], int(_float(v)), 0))
            mdb.save_menu_sales(period, rows)
            flash('出数を保存しました', 'success')
        elif action == 'confirm':
            try:
                mdb.confirm_closing(period, staff_name())
                flash(f'{mdb.period_label(period)} を締めました', 'success')
            except ValueError as e:
                flash(str(e), 'danger')
        return redirect(url_for('meat.closing', period=period))
    if not _valid_period(period):
        abort(404)
    result = mdb.compute_closing(period)
    prev = mdb.compute_closing(mdb.prev_period(period))
    prev_cost = {r['part_id']: r['cost_per_sale'] for r in prev['rows']}
    prev_loss = {r['part_id']: r['loss_rate'] for r in prev['rows']}
    prev_menu = {m['menu_name']: m['cost'] for m in prev['menus'] if m['complete'] and m['cost']}
    return render_template('meat/closing.html', r=result, period=period, prev_period=mdb.prev_period(period),
                           next_period=mdb.next_period(period), is_week=mdb.is_week(period),
                           prev_cost=prev_cost, prev_loss=prev_loss, prev_menu=prev_menu)


@bp.route('/menus')
def menus():
    return render_template('meat/menus.html', rows=mdb.list_menu_map())
