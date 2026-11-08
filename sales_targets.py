# sales_targets.py — 日別の売上目標の画面（/sales-targets）
# 見るのは誰でも。入力・取り込み・客単価の変更は管理者だけ（正肉在庫と同じ管理者パスワード）
from datetime import date, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

import sales_target_db as stdb
from meat import is_admin, staff_name, admin_required, _today

bp = Blueprint('sales_targets', __name__, url_prefix='/sales-targets')
WEEKDAYS = '月火水木金土日'


def _month_arg(v):
    try:
        y, m = map(int, (v or '').split('-'))
        return date(y, m, 1)
    except ValueError:
        return _today().replace(day=1)


@bp.route('/')
def index():
    first = _month_arg(request.args.get('month'))
    month = first.strftime('%Y-%m')
    nxt = (first + timedelta(days=32)).replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    saved = stdb.month_targets(month)
    unit_price = stdb.get_unit_price()
    days = []
    d = first
    while d < nxt:
        row = saved.get(d.isoformat())
        goal = stdb.goal_for(row['amount'] if row else None, unit_price, 0, 0, 0)
        days.append({'date': d.isoformat(), 'label': f'{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）',
                     'weekday': d.weekday(), 'row': row, 'goal': goal.get('goal')})
        d += timedelta(days=1)
    set_days = [x['row']['amount'] for x in days if x['row']]
    return render_template('sales_targets.html', month=month, month_label=f'{first.year}年{first.month}月',
                           prev=prev.strftime('%Y-%m'), next=nxt.strftime('%Y-%m'), days=days,
                           unit_price=unit_price, sources=stdb.SOURCES, today=_today().isoformat(),
                           total=sum(set_days), set_count=len(set_days), can_edit=is_admin())


@bp.route('/save', methods=['POST'])
@admin_required
def save():
    month = request.form.get('month', '')
    values = {k[2:]: v for k, v in request.form.items() if k.startswith('d_')}
    try:
        n = stdb.save_targets(values, 'manual', staff_name())
        flash(f'売上目標を保存しました（目標のある日 {n}日）', 'success')
    except ValueError as e:
        flash(str(e) + '。保存していません', 'danger')
    return redirect(url_for('sales_targets.index', month=month))


@bp.route('/import', methods=['POST'])
@admin_required
def import_pasted():
    month = request.form.get('month', '')
    found, skipped = stdb.parse_pasted(request.form.get('pasted', ''), _month_arg(month).year)
    if not found:
        flash('日付と金額を読み取れませんでした。1行に「日付と金額」を1組ずつ貼り付けてください', 'danger')
        return redirect(url_for('sales_targets.index', month=month))
    try:
        n = stdb.save_targets(found, 'import', staff_name())
    except ValueError as e:
        flash(str(e) + '。取り込んでいません', 'danger')
        return redirect(url_for('sales_targets.index', month=month))
    msg = f'{n}日分の売上目標を取り込みました'
    if skipped:
        msg += f'。読み飛ばした行 {len(skipped)}行：' + '／'.join(skipped[:5]) + ('…' if len(skipped) > 5 else '')
    flash(msg, 'warning' if skipped else 'success')
    return redirect(url_for('sales_targets.index', month=min(found)[:7]))


@bp.route('/unit-price', methods=['POST'])
@admin_required
def unit_price():
    try:
        stdb.set_unit_price(request.form.get('unit_price'), staff_name())
        flash('目標客数の計算に使う客単価を変更しました', 'success')
    except ValueError as e:
        flash(str(e), 'danger')
    return redirect(url_for('sales_targets.index', month=request.form.get('month', '')))
