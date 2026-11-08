# sales_target_db.py — 日別の売上目標（税抜）と、目標客数の計算
#
# 目標の出どころは今は手入力・一覧の取り込み。プロフィットシフトと自動でつなぐ時も、
# この表（sales_targets）に source='profitshift' として書き込めば、画面の側は変えなくてよい。
# 予約の表には触らない。テーブルはすべて sales_target で始まる。
import re
from datetime import date
from sqlalchemy import text

from database import engine, _now

DEFAULT_UNIT_PRICE = 5400   # 目標客数の計算に使う客単価（税抜）。仮の値なので画面で変えられる
OVERDUE_GRACE_MINUTES = 15  # 予定時刻をこれだけ過ぎても来店受付が無い予約を「未受付」として知らせる
SOURCES = {'manual': '手入力', 'import': '取り込み', 'profitshift': '自動連携'}


def init_sales_target_db():
    with engine.begin() as con:
        con.execute(text("""CREATE TABLE IF NOT EXISTS sales_targets (
            target_date  TEXT    PRIMARY KEY,
            amount       INTEGER NOT NULL,
            source       TEXT    NOT NULL DEFAULT 'manual',
            updated_by   TEXT,
            updated_at   TEXT    NOT NULL
        )"""))
        con.execute(text("""CREATE TABLE IF NOT EXISTS sales_target_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_by  TEXT,
            updated_at  TEXT NOT NULL
        )"""))


# ------------------------------------------------------------
# 客単価（目標客数の計算用）
# ------------------------------------------------------------
def get_unit_price():
    with engine.begin() as con:
        v = con.execute(text("SELECT value FROM sales_target_settings WHERE key = 'unit_price'")).scalar()
    try:
        return int(v) if v and int(v) > 0 else DEFAULT_UNIT_PRICE
    except ValueError:
        return DEFAULT_UNIT_PRICE


def set_unit_price(value, staff):
    price = _to_amount(value)
    if price is None:
        raise ValueError('客単価は1円以上の整数で入れてください')
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO sales_target_settings (key, value, updated_by, updated_at) VALUES ('unit_price', :v, :s, :t)
            ON CONFLICT (key) DO UPDATE SET value = :v, updated_by = :s, updated_at = :t
        """), {'v': str(price), 's': staff, 't': _now()})


# ------------------------------------------------------------
# 日別の売上目標
# ------------------------------------------------------------
def _to_amount(value):
    """'367,553円' → 367553。空・0以下・数字でない時は None"""
    s = re.sub(r'[,，円¥￥\s]', '', str(value or '')).translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    if not s:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return int(n) if n >= 1 and n == int(n) else None


def get_target(target_date):
    """その日の売上目標（円）。未設定なら None（0円とは扱わない）"""
    with engine.begin() as con:
        v = con.execute(text('SELECT amount FROM sales_targets WHERE target_date = :d'), {'d': target_date}).scalar()
    return v if v and v > 0 else None


def month_targets(month):
    """'2026-11' の目標 {日付: {'amount','source','updated_by','updated_at'}}"""
    with engine.begin() as con:
        rows = con.execute(text("""
            SELECT * FROM sales_targets WHERE target_date >= :s AND target_date < :e ORDER BY target_date
        """), {'s': month + '-01', 'e': month + '-32'}).mappings()
        return {r['target_date']: dict(r) for r in rows}


def save_targets(values, source, staff):
    """{日付: 金額} を保存する。金額が空なら、その日の目標を消す（未設定に戻す）。
    おかしな金額が1つでもあれば、何も保存しない"""
    if source not in SOURCES:
        raise ValueError('目標の出どころが正しくありません')
    clean, bad = {}, []
    for d, v in values.items():
        try:
            d = date.fromisoformat(d).isoformat()
        except ValueError:
            bad.append(str(d))
            continue
        if str(v or '').strip() == '':
            clean[d] = None
            continue
        amount = _to_amount(v)
        if amount is None:
            bad.append(f'{d}（{v}）')
        else:
            clean[d] = amount
    if bad:
        raise ValueError('金額を読み取れませんでした：' + '、'.join(bad))
    with engine.begin() as con:
        for d, amount in clean.items():
            if amount is None:
                con.execute(text('DELETE FROM sales_targets WHERE target_date = :d'), {'d': d})
                continue
            # 金額が同じ日は書き換えない（誰がいつ入れたかを残す）
            con.execute(text("""
                INSERT INTO sales_targets (target_date, amount, source, updated_by, updated_at) VALUES (:d, :a, :src, :s, :t)
                ON CONFLICT (target_date) DO UPDATE SET amount = :a, source = :src, updated_by = :s, updated_at = :t
                WHERE sales_targets.amount <> :a
            """), {'d': d, 'a': amount, 'src': source, 's': staff, 't': _now()})
    return sum(1 for a in clean.values() if a is not None)


_DATE_PATTERNS = (
    re.compile(r'(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*日?'),
    re.compile(r'(?<![\d,])(?P<m>\d{1,2})\s*[/月]\s*(?P<d>\d{1,2})\s*日?'),
)


def parse_pasted(text_value, default_year):
    """Excelやプロフィットシフトからコピーした「日付と金額」の一覧を読む。
    1行に日付1つと金額1つ（タブ・カンマ・空白区切り）。見出しや空行は飛ばす。
    戻り値 ({日付: 金額の文字列}, [読めなかった行])"""
    found, skipped = {}, []
    for line in str(text_value or '').splitlines():
        line = line.translate(str.maketrans('０１２３４５６７８９／', '0123456789/')).strip()
        if not line:
            continue
        for pat in _DATE_PATTERNS:
            m = pat.search(line)
            if m:
                break
        if not m:
            skipped.append(line)   # 見出しなど
            continue
        y = int(m.groupdict().get('y') or default_year)
        try:
            d = date(y, int(m.group('m')), int(m.group('d'))).isoformat()
        except ValueError:
            skipped.append(line)
            continue
        rest = line[:m.start()] + ' ' + line[m.end():]
        amounts = [a for a in re.findall(r'[\d,，]+(?:\.\d+)?', rest) if _to_amount(a)]
        if len(amounts) != 1:
            skipped.append(line)   # 金額が無い・2つ以上あって決められない
            continue
        found[d] = amounts[0]
    return found, skipped


# ------------------------------------------------------------
# 目標客数の計算
# ------------------------------------------------------------
def goal_for(amount, unit_price, expected, visited, upcoming, overdue=0):
    """目標客数と不足人数。amount が None（未設定）なら目標の数字は出さない。
    expected＝来店見込み（キャンセルを除く総人数）＝ visited ＋ upcoming（重ねて足さない）"""
    base = {'expected': expected, 'visited': visited, 'upcoming': upcoming, 'overdue': overdue,
            'unit_price': unit_price}
    if not amount:
        return dict(base, status='unset')
    goal = -(-amount // unit_price)   # 切り上げ
    short = goal - expected
    result = dict(base, status='achieved' if short <= 0 else 'short', amount=amount, goal=goal,
                  short=max(short, 0), surplus=max(-short, 0))
    if short <= 0:
        result.update(_next_step(goal, expected))
    return result


# 目標を達成した後の、ねぎらいの言葉と次の目安（目標の110%・120%…と10%刻みで、まだ届いていない段）
CHEERS = (
    (110, '目標達成！みなさん、ありがとうございます'),
    (120, '目標を大きく超えました！ありがとうございます'),
    (None, 'すばらしい勢いです！本当にありがとうございます'),
)


def _next_step(goal, expected):
    next_pct = max(110, (expected * 100 // goal) // 10 * 10 + 10)
    next_goal = -(-goal * next_pct // 100)
    cheer = next(text_ for limit, text_ in CHEERS if limit is None or next_pct <= limit)
    return {'cheer': cheer, 'next_pct': next_pct, 'next_goal': next_goal, 'to_next': next_goal - expected}
