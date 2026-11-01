# duty.py — 今日の仕事・申し送りの画面（Flask Blueprint、/duty 以下）
# ログインは予約台帳と同じ（APP_PASSWORD）。担当者は名前を選ぶ・入れるだけ。
# 画面は1枚で、操作のたびに JSON で保存し、保存できた時だけ画面を「保存済」にする。
from datetime import date

from flask import Blueprint, render_template, request, jsonify

import duty_db as ddb

bp = Blueprint('duty', __name__, url_prefix='/duty')


def _date_arg(v):
    try:
        return date.fromisoformat(v).isoformat()
    except (TypeError, ValueError):
        return ddb.work_date()


def _payload():
    return request.get_json(silent=True) or request.form.to_dict()


def _respond(fn, wdate):
    """保存して最新の状態を返す。失敗した時は理由と最新の状態を返す（画面は保存前に戻す）"""
    try:
        fn()
    except ddb.Conflict as e:
        return jsonify({'ok': False, 'conflict': True, 'error': str(e), 'state': ddb.get_state(wdate)}), 409
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e), 'state': ddb.get_state(wdate)}), 400
    except Exception as e:   # データベースにつながらない等。完了と表示させない
        return jsonify({'ok': False, 'error': f'保存できませんでした（{type(e).__name__}）。もう一度押してください'}), 500
    return jsonify({'ok': True, 'state': ddb.get_state(wdate)})


@bp.route('/')
def index():
    wdate = _date_arg(request.args.get('date'))
    tab = request.args.get('tab', 'morning')
    return render_template('duty/index.html', wdate=wdate, tab=tab, state=ddb.get_state(wdate))


@bp.route('/history')
def history_page():
    return render_template('duty/history.html', reqs=ddb.history(60))


@bp.route('/api/state')
def api_state():
    return jsonify(ddb.get_state(_date_arg(request.args.get('date'))))


@bp.route('/api/rev')
def api_rev():
    return jsonify({'rev': ddb.get_rev()})


@bp.route('/api/staff', methods=['POST'])
def api_staff():
    d = _payload(); w = _date_arg(d.get('date'))
    return _respond(lambda: ddb.set_day_staff(w, d.get('section'), d.get('name')), w)


@bp.route('/api/check', methods=['POST'])
def api_check():
    d = _payload(); w = _date_arg(d.get('date'))
    return _respond(lambda: ddb.save_check(w, int(d.get('item_id')), bool(d.get('done')), d.get('minutes'),
                                           d.get('staff'), d.get('version')), w)


@bp.route('/api/inspect', methods=['POST'])
def api_inspect():
    d = _payload(); w = _date_arg(d.get('date'))
    return _respond(lambda: ddb.save_inspection(w, int(d.get('item_id')), d.get('state'), d.get('staff'),
                                                d.get('version')), w)


@bp.route('/api/request/add', methods=['POST'])
def api_request_add():
    d = _payload(); w = _date_arg(d.get('date'))
    return _respond(lambda: ddb.add_request(w, d.get('title'), d.get('staff')), w)


@bp.route('/api/request/<int:rid>/<action>', methods=['POST'])
def api_request_action(rid, action):
    d = _payload(); w = _date_arg(d.get('date'))
    actions = {
        'complete': lambda: ddb.complete_request(rid, d.get('version'), d.get('executor'), d.get('minutes'), w),
        'reopen':   lambda: ddb.reopen_request(rid, d.get('version'), d.get('staff'), d.get('memo'), w),
        'void':     lambda: ddb.void_request(rid, d.get('version'), d.get('staff'), d.get('memo'), w),
        'memo':     lambda: ddb.add_request_memo(rid, d.get('staff'), d.get('memo'), w),
    }
    if action not in actions:
        return jsonify({'ok': False, 'error': '操作が違います'}), 404
    return _respond(actions[action], w)


@bp.route('/api/note/add', methods=['POST'])
def api_note_add():
    d = _payload(); w = _date_arg(d.get('date'))
    return _respond(lambda: ddb.add_note(w, d.get('staff'), d.get('body')), w)


@bp.route('/api/note/<int:nid>/delete', methods=['POST'])
def api_note_delete(nid):
    d = _payload(); w = _date_arg(d.get('date'))
    return _respond(lambda: ddb.delete_note(nid, d.get('staff')), w)
