# duty_db.py — 今日の仕事・申し送り（朝・夕方の仕事、朝の点検 → 夜の依頼）
# 元の紙：麒麟屋_掃除の気づき・引継ぎ表.xlsx（本人訂正版、2026-11-01）
#
# ・朝の仕事14項目／夕方の仕事5項目：日ごとに 完了・所要分・担当者 を記録（duty_checks）
# ・朝の点検8か所：未確認／問題なし／要掃除（duty_inspections）。要掃除 → 夜の依頼（duty_requests）
# ・夜の依頼は日付をまたいで残る。同じ場所の未完了の依頼は1件だけ（重複作成しない）
# ・同時操作で記録が消えないよう、更新は version を見て行う（古い画面からの上書きは断る）
# ・予約の表には一切触らない。テーブルはすべて duty_ で始まる
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

from database import engine, _IS_PG, _now

_JST = timezone(timedelta(hours=9))
DAY_STARTS_AT = 5   # 夜の作業が0時を過ぎても同じ日として扱う。朝5時に「今日」が切り替わる

MORNING_STAFF = ['碇 静子', '原 美郷']

SECTIONS = {'morning': '朝の仕事', 'evening': '夕方の仕事', 'detail': '朝の点検（細かい8か所）',
            'candidate': '追加候補（分担未定）'}

SEED_ITEMS = {
    'morning': [
        ('床の掃除', ''), ('掘りごたつの足元の掃除', ''), ('座布団を拭く', ''), ('灰皿の清掃', ''),
        ('トイレの掃除', '鏡・ダストボックスは夕方'), ('窓拭き', ''), ('玄関の扉拭き', ''),
        ('ゴミ箱回収', ''), ('ゴミ箱セット', ''), ('洗濯機を回す', ''), ('グリストラップの清掃', ''),
        ('ギョーザ包み', ''), ('爪楊枝と塩の確認', '補充は夕方'), ('トイレの備品の確認', ''),
    ],
    'evening': [
        ('爪楊枝の補充', ''), ('コチジャンの補充', ''), ('ニンニクの補充', ''),
        ('トイレの鏡を拭く', ''), ('トイレのダストボックスを清掃する', ''),
    ],
    'detail': [
        ('障子の桟・間仕切りの縁', ''), ('窓台・サッシの溝', ''), ('テーブルの脚・側面', ''),
        ('椅子の脚・背面', ''), ('手の届く棚の上', ''), ('メニュー立て・小物入れ周り', ''),
        ('壁際・家具の隙間', ''), ('下駄箱の中', ''),
    ],
    # 分担が決まるまで必須にしない。必要な日だけ「自由な仕事」として夜へ依頼できる
    'candidate': [
        ('客席のテーブル・椅子の汚れを拭く', ''), ('入口・玄関マットのごみを取り除く', ''),
        ('洗面台の汚れを落とす', ''),
    ],
}

INSPECT_STATES = {'unchecked': '未確認', 'ok': '問題なし', 'need': '要掃除'}


class Conflict(Exception):
    """ほかのタブレットが先に更新していた"""


def work_date(now=None):
    now = now or datetime.now(_JST).replace(tzinfo=None)
    return (now - timedelta(hours=DAY_STARTS_AT)).date().isoformat()


def init_duty_db():
    pk = 'id SERIAL PRIMARY KEY' if _IS_PG else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS duty_items (
            {pk},
            section     TEXT NOT NULL,
            name        TEXT NOT NULL,
            note        TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            UNIQUE (section, name)
        )""",
        """CREATE TABLE IF NOT EXISTS duty_day_staff (
            work_date   TEXT NOT NULL,
            section     TEXT NOT NULL,
            staff_name  TEXT,
            updated_at  TEXT,
            PRIMARY KEY (work_date, section)
        )""",
        """CREATE TABLE IF NOT EXISTS duty_checks (
            work_date   TEXT NOT NULL,
            item_id     INTEGER NOT NULL,
            done        INTEGER NOT NULL DEFAULT 0,
            minutes     INTEGER,
            staff_name  TEXT,
            version     INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT,
            PRIMARY KEY (work_date, item_id)
        )""",
        """CREATE TABLE IF NOT EXISTS duty_inspections (
            work_date   TEXT NOT NULL,
            item_id     INTEGER NOT NULL,
            state       TEXT NOT NULL DEFAULT 'unchecked',
            staff_name  TEXT,
            version     INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT,
            PRIMARY KEY (work_date, item_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS duty_requests (
            {pk},
            origin_date     TEXT NOT NULL,
            item_id         INTEGER,
            title           TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open',
            executor        TEXT,
            minutes         INTEGER,
            done_at         TEXT,
            done_work_date  TEXT,
            created_by      TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT,
            version         INTEGER NOT NULL DEFAULT 0
        )""",
        f"""CREATE TABLE IF NOT EXISTS duty_request_logs (
            {pk},
            request_id  INTEGER NOT NULL,
            work_date   TEXT,
            action      TEXT NOT NULL,
            staff_name  TEXT,
            memo        TEXT,
            logged_at   TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS duty_notes (
            {pk},
            work_date   TEXT NOT NULL,
            staff_name  TEXT,
            body        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            deleted_at  TEXT,
            deleted_by  TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS duty_rev (
            id   INTEGER PRIMARY KEY,
            rev  INTEGER NOT NULL DEFAULT 0
        )""",
    ]
    with engine.begin() as con:
        for s in stmts:
            con.execute(text(s))
        # 同じ場所の「未完了の依頼」は1件だけ（2台が同時に要掃除にしても重複しない）
        con.execute(text("""CREATE UNIQUE INDEX IF NOT EXISTS ux_duty_requests_open_item
                            ON duty_requests (item_id) WHERE status = 'open' AND item_id IS NOT NULL"""))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_duty_requests_status ON duty_requests (status)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_duty_request_logs_req ON duty_request_logs (request_id)"))
        if not con.execute(text('SELECT COUNT(*) FROM duty_rev')).scalar():
            con.execute(text('INSERT INTO duty_rev (id, rev) VALUES (1, 0)'))
        for section, rows in SEED_ITEMS.items():
            for i, (name, note) in enumerate(rows):
                exists = con.execute(text('SELECT id FROM duty_items WHERE section = :s AND name = :n'),
                                     {'s': section, 'n': name}).scalar()
                if not exists:
                    con.execute(text("""INSERT INTO duty_items (section, name, note, sort_order, active)
                                        VALUES (:s, :n, :note, :o, 1)"""),
                                {'s': section, 'n': name, 'note': note, 'o': (i + 1) * 10})


# ------------------------------------------------------------
# 共通
# ------------------------------------------------------------
def _bump(con):
    con.execute(text('UPDATE duty_rev SET rev = rev + 1 WHERE id = 1'))


def get_rev():
    with engine.begin() as con:
        return con.execute(text('SELECT rev FROM duty_rev WHERE id = 1')).scalar() or 0


def _log(con, request_id, action, staff, memo='', wdate=None):
    con.execute(text("""INSERT INTO duty_request_logs (request_id, work_date, action, staff_name, memo, logged_at)
                        VALUES (:r, :d, :a, :s, :m, :t)"""),
                {'r': request_id, 'd': wdate or work_date(), 'a': action, 's': staff or '', 'm': memo or '',
                 't': _now()})


def _insert_id(con, sql, params):
    res = con.execute(text(sql + (' RETURNING id' if _IS_PG else '')), params)
    return res.scalar() if _IS_PG else res.lastrowid


def _clean_minutes(v):
    if v is None or str(v).strip() == '':
        return None
    try:
        m = int(float(str(v).strip()))
    except ValueError:
        raise ValueError('所要時間は数字（分）で入れてください')
    if m < 0 or m > 600:
        raise ValueError('所要時間は0〜600分で入れてください')
    return m


def list_items(section=None):
    with engine.begin() as con:
        sql = 'SELECT * FROM duty_items WHERE active = 1'
        params = {}
        if section:
            sql += ' AND section = :s'
            params['s'] = section
        return [dict(r) for r in con.execute(text(sql + ' ORDER BY section, sort_order, id'), params).mappings()]


def _item(con, item_id, sections):
    row = con.execute(text('SELECT * FROM duty_items WHERE id = :id AND active = 1'), {'id': item_id}).mappings().first()
    if not row or row['section'] not in sections:
        raise ValueError('項目が見つかりません')
    return dict(row)


# ------------------------------------------------------------
# 担当者（朝・夕方）
# ------------------------------------------------------------
def set_day_staff(wdate, section, name):
    name = (name or '').strip()
    if section not in ('morning', 'evening'):
        raise ValueError('担当の区分が違います')
    if section == 'morning' and name and name not in MORNING_STAFF:
        raise ValueError('朝の確認者は ' + '・'.join(MORNING_STAFF) + ' から選んでください')
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO duty_day_staff (work_date, section, staff_name, updated_at) VALUES (:d, :s, :n, :t)
            ON CONFLICT (work_date, section) DO UPDATE SET staff_name = :n, updated_at = :t
        """), {'d': wdate, 's': section, 'n': name, 't': _now()})
        _bump(con)


# ------------------------------------------------------------
# 朝・夕方の仕事のチェック
# ------------------------------------------------------------
def save_check(wdate, item_id, done, minutes, staff, version):
    """完了・所要分を保存する。version は画面が知っている版。違えば Conflict（上書きしない）"""
    minutes = _clean_minutes(minutes)
    staff = (staff or '').strip()
    with engine.begin() as con:
        item = _item(con, item_id, ('morning', 'evening'))
        if done and not staff:
            raise ValueError('先に' + ('朝の確認者' if item['section'] == 'morning' else '夕方の担当者') + 'を選んでください')
        con.execute(text("""INSERT INTO duty_checks (work_date, item_id, done, version) VALUES (:d, :i, 0, 0)
                            ON CONFLICT (work_date, item_id) DO NOTHING"""), {'d': wdate, 'i': item_id})
        res = con.execute(text("""
            UPDATE duty_checks SET done = :done, minutes = :m, staff_name = :s, version = version + 1, updated_at = :t
            WHERE work_date = :d AND item_id = :i AND version = :v
        """), {'done': 1 if done else 0, 'm': minutes, 's': staff, 't': _now(), 'd': wdate, 'i': item_id,
               'v': int(version or 0)})
        if res.rowcount != 1:
            raise Conflict(f'「{item["name"]}」はほかのタブレットで先に更新されました。最新の表示に直しました')
        _bump(con)


# ------------------------------------------------------------
# 朝の点検（細かい8か所）→ 夜の依頼
# ------------------------------------------------------------
def save_inspection(wdate, item_id, state, staff, version):
    if state not in INSPECT_STATES:
        raise ValueError('判定が違います')
    staff = (staff or '').strip()
    if state != 'unchecked' and not staff:
        raise ValueError('先に朝の確認者を選んでください')
    with engine.begin() as con:
        item = _item(con, item_id, ('detail',))
        prev = con.execute(text('SELECT state FROM duty_inspections WHERE work_date = :d AND item_id = :i'),
                           {'d': wdate, 'i': item_id}).scalar()
        con.execute(text("""INSERT INTO duty_inspections (work_date, item_id, state, version) VALUES (:d, :i, 'unchecked', 0)
                            ON CONFLICT (work_date, item_id) DO NOTHING"""), {'d': wdate, 'i': item_id})
        res = con.execute(text("""
            UPDATE duty_inspections SET state = :st, staff_name = :s, version = version + 1, updated_at = :t
            WHERE work_date = :d AND item_id = :i AND version = :v
        """), {'st': state, 's': staff, 't': _now(), 'd': wdate, 'i': item_id, 'v': int(version or 0)})
        if res.rowcount != 1:
            raise Conflict(f'「{item["name"]}」はほかのタブレットで先に更新されました。最新の表示に直しました')

        open_req = con.execute(text("""SELECT * FROM duty_requests WHERE item_id = :i AND status = 'open'"""),
                               {'i': item_id}).mappings().first()
        if state == 'need':
            if open_req:
                # 重複して作らない。前からの依頼に「今日も要掃除」と書き足す（同じ日に何度も足さない）
                if open_req['origin_date'] != wdate:
                    already = con.execute(text("""SELECT COUNT(*) FROM duty_request_logs
                        WHERE request_id = :r AND action = 'reflag' AND work_date = :d"""),
                        {'r': open_req['id'], 'd': wdate}).scalar()
                    if not already:
                        _log(con, open_req['id'], 'reflag', staff, f'{_md(wdate)} 朝も要掃除', wdate)
                        _touch_request(con, open_req['id'])
            else:
                rid = _insert_id(con, """
                    INSERT INTO duty_requests (origin_date, item_id, title, status, created_by, created_at, updated_at, version)
                    VALUES (:d, :i, :title, 'open', :s, :t, :t, 0)
                """, {'d': wdate, 'i': item_id, 'title': item['name'], 's': staff, 't': _now()})
                _log(con, rid, 'create', staff, '朝の点検で要掃除', wdate)
        elif prev == 'need' and open_req and open_req['origin_date'] == wdate:
            # 今朝の押し間違いを直した時：今朝作った依頼で、まだ誰も手を付けていなければ取り下げる
            touched = con.execute(text("""SELECT COUNT(*) FROM duty_request_logs
                WHERE request_id = :r AND action NOT IN ('create')"""), {'r': open_req['id']}).scalar()
            if not touched:
                con.execute(text("""UPDATE duty_requests SET status = 'void', updated_at = :t, version = version + 1
                                    WHERE id = :r"""), {'t': _now(), 'r': open_req['id']})
                _log(con, open_req['id'], 'void', staff, f'朝の判定を「{INSPECT_STATES[state]}」に直したため取り下げ', wdate)
        _bump(con)


def _md(d):
    return f'{int(d[5:7])}/{int(d[8:10])}'


def _touch_request(con, rid):
    con.execute(text('UPDATE duty_requests SET updated_at = :t, version = version + 1 WHERE id = :r'),
                {'t': _now(), 'r': rid})


# ------------------------------------------------------------
# 夜の依頼
# ------------------------------------------------------------
def add_request(wdate, title, staff):
    title = (title or '').strip()
    if not title:
        raise ValueError('仕事の内容を入れてください')
    if len(title) > 200:
        raise ValueError('仕事の内容は200文字までにしてください（詳しい説明は申し送り文へ）')
    with engine.begin() as con:
        rid = _insert_id(con, """
            INSERT INTO duty_requests (origin_date, item_id, title, status, created_by, created_at, updated_at, version)
            VALUES (:d, NULL, :title, 'open', :s, :t, :t, 0)
        """, {'d': wdate, 'title': title, 's': (staff or '').strip(), 't': _now()})
        _log(con, rid, 'create', staff, '自由な仕事として追加', wdate)
        _bump(con)
    return rid


def _change_request(rid, version, from_status, sets, action, staff, memo, wdate):
    with engine.begin() as con:
        req = con.execute(text('SELECT * FROM duty_requests WHERE id = :r'), {'r': rid}).mappings().first()
        if not req:
            raise ValueError('依頼が見つかりません')
        if req['status'] != from_status or int(req['version']) != int(version or 0):
            raise Conflict(f'「{req["title"]}」はほかのタブレットで先に更新されました。最新の表示に直しました')
        sets = dict(sets, updated_at=_now())
        params = dict(sets, r=rid, v=int(version or 0), fs=from_status)
        res = con.execute(text('UPDATE duty_requests SET ' + ', '.join(f'{k} = :{k}' for k in sets) +
                               ', version = version + 1 WHERE id = :r AND version = :v AND status = :fs'), params)
        if res.rowcount != 1:
            raise Conflict(f'「{req["title"]}」はほかのタブレットで先に更新されました。最新の表示に直しました')
        _log(con, rid, action, staff, memo, wdate)
        _bump(con)


def complete_request(rid, version, executor, minutes, wdate):
    executor = (executor or '').strip()
    if not executor:
        raise ValueError('実行者の名前を入れてください')
    m = _clean_minutes(minutes)
    if m is None:
        raise ValueError('実際にかかった時間（分）を入れてください')
    _change_request(rid, version, 'open',
                    {'status': 'done', 'executor': executor, 'minutes': m, 'done_at': _now(), 'done_work_date': wdate},
                    'done', executor, f'{m}分', wdate)


def reopen_request(rid, version, staff, memo, wdate):
    """訂正：完了を取り消して残りに戻す（誰が・なぜ戻したかを履歴に残す）"""
    if not (staff or '').strip():
        raise ValueError('訂正する人の名前を入れてください')
    try:
        _change_request(rid, version, 'done',
                        {'status': 'open', 'executor': None, 'minutes': None, 'done_at': None, 'done_work_date': None},
                        'reopen', staff, memo or '完了を取り消し', wdate)
    except Exception as e:
        # 同じ場所の未完了がもう別にある時（ありえないが念のため）は、一意の索引で止まる
        if 'ux_duty_requests_open_item' in str(e) or 'UNIQUE' in str(e).upper():
            raise ValueError('同じ場所の未完了の依頼がすでにあるため戻せません')
        raise


def void_request(rid, version, staff, memo, wdate):
    """依頼そのものを取り消す（間違えて追加した時）。履歴は残る"""
    if not (staff or '').strip():
        raise ValueError('取り消す人の名前を入れてください')
    _change_request(rid, version, 'open', {'status': 'void'}, 'void', staff, memo or '依頼を取り消し', wdate)


def add_request_memo(rid, staff, memo, wdate):
    """できなかった理由・申し送りの追記（追記だけなので同時に書いても消えない）"""
    memo = (memo or '').strip()
    if not memo:
        raise ValueError('書く内容を入れてください')
    if not (staff or '').strip():
        raise ValueError('名前を入れてください')
    with engine.begin() as con:
        if not con.execute(text('SELECT id FROM duty_requests WHERE id = :r'), {'r': rid}).scalar():
            raise ValueError('依頼が見つかりません')
        _log(con, rid, 'memo', staff, memo, wdate)
        _touch_request(con, rid)
        _bump(con)


# ------------------------------------------------------------
# 申し送り文
# ------------------------------------------------------------
def add_note(wdate, staff, body):
    body = (body or '').strip()
    if not body:
        raise ValueError('申し送り文を入れてください')
    if not (staff or '').strip():
        raise ValueError('書いた人の名前を選んでください')
    with engine.begin() as con:
        nid = _insert_id(con, """INSERT INTO duty_notes (work_date, staff_name, body, created_at)
                                 VALUES (:d, :s, :b, :t)""", {'d': wdate, 's': staff.strip(), 'b': body, 't': _now()})
        _bump(con)
    return nid


def delete_note(nid, staff):
    if not (staff or '').strip():
        raise ValueError('取り消す人の名前を入れてください')
    with engine.begin() as con:
        res = con.execute(text("""UPDATE duty_notes SET deleted_at = :t, deleted_by = :s
                                  WHERE id = :id AND deleted_at IS NULL"""), {'t': _now(), 's': staff.strip(), 'id': nid})
        if res.rowcount != 1:
            raise Conflict('この申し送りはすでに取り消されています')
        _bump(con)


# ------------------------------------------------------------
# 画面に出すもの
# ------------------------------------------------------------
def _requests(con, where, params, order='origin_date, id'):
    reqs = [dict(r) for r in con.execute(text(
        'SELECT * FROM duty_requests WHERE ' + where + ' ORDER BY ' + order), params).mappings()]
    if reqs:
        ids = [r['id'] for r in reqs]
        keys = ','.join(f':i{n}' for n in range(len(ids)))
        logs = {}
        for lg in con.execute(text(f'SELECT * FROM duty_request_logs WHERE request_id IN ({keys}) ORDER BY id'),
                              {f'i{n}': v for n, v in enumerate(ids)}).mappings():
            logs.setdefault(lg['request_id'], []).append(dict(lg))
        for r in reqs:
            r['logs'] = logs.get(r['id'], [])
    return reqs


def get_state(wdate):
    today = work_date()
    with engine.begin() as con:
        items = [dict(r) for r in con.execute(text(
            'SELECT * FROM duty_items WHERE active = 1 ORDER BY sort_order, id')).mappings()]
        staff = {r['section']: r['staff_name'] for r in con.execute(text(
            'SELECT section, staff_name FROM duty_day_staff WHERE work_date = :d'), {'d': wdate}).mappings()}
        checks = {r['item_id']: dict(r) for r in con.execute(text(
            'SELECT * FROM duty_checks WHERE work_date = :d'), {'d': wdate}).mappings()}
        insp = {r['item_id']: dict(r) for r in con.execute(text(
            'SELECT * FROM duty_inspections WHERE work_date = :d'), {'d': wdate}).mappings()}
        notes = [dict(r) for r in con.execute(text(
            'SELECT * FROM duty_notes WHERE work_date = :d AND deleted_at IS NULL ORDER BY id'), {'d': wdate}).mappings()]
        # 未完了は日付に関係なくすべて（発生日の古い順）。完了はその日に完了したもの
        open_reqs = _requests(con, "status = 'open'", {})
        done_reqs = _requests(con, "status = 'done' AND done_work_date = :d", {'d': wdate})
        names = sorted({n for (n,) in con.execute(text("""
            SELECT staff_name FROM duty_day_staff WHERE section = 'evening' AND staff_name IS NOT NULL
            UNION SELECT executor FROM duty_requests WHERE executor IS NOT NULL
            UNION SELECT staff_name FROM duty_request_logs WHERE staff_name IS NOT NULL
        """)) if n and n.strip()})
        rev = con.execute(text('SELECT rev FROM duty_rev WHERE id = 1')).scalar() or 0
    by_sec = {s: [i for i in items if i['section'] == s] for s in SECTIONS}
    morning_done = sum(1 for i in by_sec['morning'] if checks.get(i['id'], {}).get('done'))
    evening_done = sum(1 for i in by_sec['evening'] if checks.get(i['id'], {}).get('done'))
    return {
        'date': wdate, 'today': today, 'is_today': wdate == today,
        'items': by_sec, 'staff': staff, 'morning_staff_choices': MORNING_STAFF,
        'checks': {str(k): v for k, v in checks.items()},
        'inspections': {str(k): v for k, v in insp.items()},
        'notes': notes, 'open_requests': open_reqs, 'done_requests': done_reqs,
        'names': names, 'rev': rev,
        'counts': {
            'morning_done': morning_done, 'morning_total': len(by_sec['morning']),
            'evening_done': evening_done, 'evening_total': len(by_sec['evening']),
            'open': len(open_reqs), 'open_old': sum(1 for r in open_reqs if r['origin_date'] < wdate),
            'notes': len(notes),
        },
    }


def summary():
    """予約台帳の入口に出す件数（今日）"""
    st = get_state(work_date())
    return dict(st['counts'], date=st['date'])


def history(days=30):
    """完了・取り消しの履歴（新しい順）"""
    since = (datetime.fromisoformat(work_date()) - timedelta(days=days)).date().isoformat()
    with engine.begin() as con:
        return _requests(con, "status IN ('done', 'void') AND COALESCE(done_work_date, origin_date) >= :s",
                         {'s': since}, order='COALESCE(done_at, updated_at) DESC, id DESC')
