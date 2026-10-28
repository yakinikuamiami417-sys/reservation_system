# cattle_lookup.py — 個体識別番号から牛の情報（生産者・月齢など）を得る
#
# 家畜改良センター「牛の個体識別情報検索サービス」には公式のAPIが無いため、
# 人が検索するのと同じ手順（利用上の注意に同意 → 番号で検索）でサーバーが画面を取得する。
# 2026-10-28 社長が利用上の注意を確認し、仕入れた肉の個体確認のための自動照会に同意した。
#   - 照会は登録時の1頭1回だけ。結果は meat_cattle に保存し、同じ番号では二度と照会しない
#   - 取得できない時（サイトの作りが変わった等）は、手で調べて転記する画面に切り替える
# 画面の作りは 2026-10-28 に実際の検索結果（1680941492）で確認した（test_data/nlbc_result_sample.html）。
import html, re, ssl, unicodedata
import http.cookiejar
import urllib.parse
import urllib.request
from datetime import date

BASE = 'https://www.id.nlbc.go.jp'
SEARCH_URL = BASE + '/CattleSearch/search/agreement'
TIMEOUT = 20


class CattleLookupError(Exception):
    pass


def normalize_individual_no(s):
    """全角数字・ハイフン・空白を取り除いて10桁の数字にそろえる"""
    return re.sub(r'\D', '', unicodedata.normalize('NFKC', str(s or '')))


def is_valid_individual_no(s):
    return bool(re.fullmatch(r'\d{10}', normalize_individual_no(s)))


def months_between(birth, slaughter):
    """出生日〜と畜日の月齢（満の月数）"""
    months = (slaughter.year - birth.year) * 12 + (slaughter.month - birth.month)
    if slaughter.day < birth.day:
        months -= 1
    return months


# ============================================================
# 検索結果ページの読み取り
# ============================================================
def _rows(page):
    page = re.sub(r'<!--.*?-->', '', page, flags=re.S)
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', page, flags=re.S | re.I):
        cells = [re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', c))).strip()
                 for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, flags=re.S | re.I)]
        if any(cells):
            yield cells


def _iso(d):
    """'2024.04.06' → '2024-04-06'"""
    m = re.fullmatch(r'(\d{4})\.(\d{1,2})\.(\d{1,2})', d or '')
    return f'{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}' if m else None


MOVES = ('出生', '転入', '転出', '搬入', '取引', 'と畜', '死亡', '輸入', '搬出')


def parse_result(page, individual_no=None):
    """検索結果ページから個体情報と異動履歴を取り出す。見つからなければ None"""
    info, moves = None, []
    for cells in _rows(page):
        # 【個体情報】の値の行: 個体識別番号・出生の年月日・雌雄の別・母牛の個体識別番号・種別
        if info is None and len(cells) == 5 and re.fullmatch(r'\d{10}', cells[0]) and _iso(cells[1]):
            info = {'individual_no': cells[0], 'birth_date': _iso(cells[1]), 'sex': cells[2],
                    'mother_no': cells[3] if re.fullmatch(r'\d{10}', cells[3]) else None, 'breed': cells[4]}
        # 【異動情報】の行: 番号・異動内容・異動年月日・都道府県・市区町村・氏名または名称
        elif len(cells) == 6 and cells[0].isdigit() and cells[1] in MOVES:
            moves.append({'type': cells[1], 'date': _iso(cells[2]), 'pref': cells[3], 'city': cells[4],
                          'name': cells[5]})
    if not info or (individual_no and info['individual_no'] != individual_no):
        return None

    birth = next((m for m in moves if m['type'] == '出生'), None)
    slaughter = next((m for m in reversed(moves) if m['type'] == 'と畜'), None)
    # 生産者（肥育）＝ と畜の前に最後に「転入」した飼養者。転入が無ければ出生地の飼養者が育てた
    before = moves[:moves.index(slaughter)] if slaughter else moves
    raised = next((m for m in reversed(before) if m['type'] == '転入'), None) or birth
    place = lambda m: f"{m['pref']}{m['city']}" if m else None
    info.update({
        'breeder_name': birth['name'] if birth else None, 'breeder_place': place(birth),
        'producer_name': raised['name'] if raised else None, 'producer_place': place(raised),
        'slaughter_date': slaughter['date'] if slaughter else None,
        'slaughter_place': slaughter['name'] if slaughter else None,
        'moves': moves,
    })
    try:
        info['age_months'] = months_between(date.fromisoformat(info['birth_date']),
                                            date.fromisoformat(info['slaughter_date']))
    except (TypeError, ValueError):
        info['age_months'] = None
    return info


# ============================================================
# 照会（同意 → 検索）
# ============================================================
def _opener():
    # 検索サービスのサーバーは古い暗号方式のため、Python標準の設定では接続できない（2026-10-28 確認）
    ctx = ssl.create_default_context()
    ctx.set_ciphers('DEFAULT:@SECLEVEL=1')
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                     urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [('User-Agent', 'Mozilla/5.0 (Kirinya meat inventory)')]
    return op


def _hidden_fields(page):
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page))


def lookup(individual_no):
    """家畜改良センターで1頭を照会する。戻り値は parse_result の辞書（raw に結果ページ）"""
    no = normalize_individual_no(individual_no)
    if not re.fullmatch(r'\d{10}', no):
        raise CattleLookupError('個体識別番号は10桁の数字です')
    op = _opener()

    def fetch(path, data=None, referer=None):
        req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(data).encode() if data else None)
        if referer:
            req.add_header('Referer', referer)
        with op.open(req, timeout=TIMEOUT) as r:
            return r.geturl(), r.read().decode('utf-8', 'replace')

    try:
        url, page = fetch('/CattleSearch/search/agreement')
        form = _hidden_fields(page)
        form.update({'method:goSearch.x': '40', 'method:goSearch.y': '10'})   # 「同意する」
        url, page = fetch('/CattleSearch/search/agreement.action', form, url)
        if 'txtIDNO' not in page:
            raise CattleLookupError('検索サービスの画面が変わったため、自動で照会できませんでした')
        form = _hidden_fields(page)
        form.update({'txtIDNO': no, 'method:doSearch.x': '30', 'method:doSearch.y': '20'})   # 「検索」
        url, page = fetch('/CattleSearch/search/search.action', form, url)
    except CattleLookupError:
        raise
    except Exception as e:
        raise CattleLookupError(f'検索サービスに接続できませんでした（{type(e).__name__}）')

    info = parse_result(page, no)
    if not info:
        if '【個体情報】' in page:
            raise CattleLookupError('この番号の牛は見つかりませんでした。番号の読み間違いがないか確認してください')
        raise CattleLookupError('検索結果を読み取れませんでした（画面の作りが変わった可能性があります）')
    info['raw'] = page
    return info
