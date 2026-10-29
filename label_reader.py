# label_reader.py — 牛肉ラベル写真の読み取り（Claude API）
#
# スマホで撮ったラベル写真を Claude に読ませ、決まった形の JSON（Structured Outputs）で受け取る。
# 読み取った値は入力欄に入れるだけで、保存は必ず人が確認してから行う（自動では保存しない）。
#
# 実物ラベル6枚（一ノ瀬畜産・東和食品・JA全農みえミート）で分かったこと（2026-10-26）:
#   - 1枚の写真に複数のラベル、1枚のラベルに複数の塊（「牛セット」）がある → 塊の配列で返す
#   - 重量は「重量」（0.01kg単位）と「取引値」（0.1kg未満切り捨て）の2つ
#   - 3社ともGS1-128バーコードの下に (251)個体識別番号 (3102)正味重量 (11)加工日 (21)シリアル が印字されている
#     → 文字で読んだ値との照合に使う。賞味期限はバーコードに入っていないので文字から読む
#   - 日付は「26.10.25」のように年が2桁
import json, math, os, re
from datetime import date

MODEL = 'claude-opus-5-5'

FIELDS = ['label_item_name', 'label_kg', 'trade_kg', 'individual_no', 'number_type', 'best_before',
          'processed_date', 'branch_no', 'serial_no', 'slaughter_no', 'origin', 'breed', 'brand',
          'processor_name', 'barcode_texts']

_PIECE_SCHEMA = {
    'type': 'object',
    'properties': {
        'label_item_name': {'type': 'string', 'description': 'ラベル上の品名そのまま（例: シンタマ、内モモ、牛中バラ）'},
        'label_kg': {'type': 'string', 'description': '「重量」欄の正味重量kg（例: 11.14）。総重量・風袋込みではない'},
        'trade_kg': {'type': 'string', 'description': '「取引値」の重量kg（例: 11.1）。大きく印字された小数1桁の数字。無ければ空'},
        'individual_no': {'type': 'string', 'description': '個体識別番号または荷口番号の数字のみ'},
        'number_type': {'type': 'string', 'enum': ['個体識別番号', '荷口番号', '不明']},
        'best_before': {'type': 'string', 'description': '賞味期限 YYYY-MM-DD。加工日・包装日と取り違えない'},
        'processed_date': {'type': 'string', 'description': '加工年月日 YYYY-MM-DD'},
        'branch_no': {'type': 'string', 'description': '枝番（例: 0134右、左7304S）'},
        'serial_no': {'type': 'string', 'description': 'シリアルNO・ロットNO・セット番号'},
        'slaughter_no': {'type': 'string', 'description': 'と畜番号'},
        'origin': {'type': 'string', 'description': '産地（例: 佐賀県産）'},
        'breed': {'type': 'string', 'description': '品種（例: 黒毛和種）'},
        'brand': {'type': 'string', 'description': '銘柄（例: 佐賀牛、松阪牛）'},
        'processor_name': {'type': 'string', 'description': '加工元・加工者の会社名'},
        'barcode_texts': {'type': 'array', 'items': {'type': 'string'},
                          'description': 'バーコード下に印字された括弧付き数字列をそのまま（例: (01)94547913161002(3102)001114(11)260911(21)0000884021）'},
        'unreadable': {'type': 'array', 'items': {'type': 'string'},
                       'description': '写りが悪く確信を持って読めなかった項目名（上のキー名）'},
    },
    'required': FIELDS + ['unreadable'],
    'additionalProperties': False,
}

SCHEMA = {
    'type': 'object',
    'properties': {
        'pieces': {'type': 'array', 'items': _PIECE_SCHEMA},
        'notes': {'type': 'string', 'description': '写真全体についての注意（ラベルが切れている、反射で読めない等）'},
    },
    'required': ['pieces', 'notes'],
    'additionalProperties': False,
}

PROMPT = """これは焼肉店が仕入れる牛肉（枝肉から分割した部分肉）の箱に貼られたラベルの写真です。
写っているラベルを読み取り、肉の塊ごとに1件ずつ pieces に入れてください。

- 1枚の写真に複数の箱のラベルが写っていることがあります。読めるラベルはすべて読んでください。
- 1枚のラベルに複数の塊が並んでいることがあります（「牛セット」など、品名と重量の行が複数ある）。その場合は塊ごとに1件にし、共通の項目（個体識別番号・賞味期限など）は各件に入れてください。合計の行は1件にしないでください。
- 重量は2種類あります。「重量(kg)」欄の小数2桁の値が label_kg、大きく印字された小数1桁の値（「取引値」）が trade_kg です。
- 日付は「26.10.25」のように年が2桁です。2000年代として YYYY-MM-DD に直してください（例: 2026-10-25）。
- 賞味期限と加工年月日を取り違えないでください。
- 個体識別番号は通常10桁です。複数頭をまとめた包装では代わりに「荷口番号」が書かれることがあり、その場合は number_type を 荷口番号 にしてください。
- 番号や重量は推測で埋めないでください。読めない・写っていない項目は空文字にし、unreadable に項目名を入れてください。バーコード下の数字から読める場合はそちらを使ってかまいません。
- 写りが小さい・ぼやけているラベルも、読める項目だけ返してください。"""


class LabelReadError(Exception):
    pass


def is_configured():
    return bool(os.environ.get('ANTHROPIC_API_KEY'))


# ============================================================
# GS1-128（バーコード下の括弧付き数字）の読み解き
# ============================================================
_AI_RE = re.compile(r'\((\d{2,4})\)([^()]*)')


def parse_gs1(texts):
    """'(01)9454...(3102)001114(11)260911(21)0000884021' → {'01': ..., '3102': '001114', ...}"""
    out = {}
    for t in texts or []:
        for ai, val in _AI_RE.findall(re.sub(r'\s+', '', str(t))):
            out.setdefault(ai, val)
    return out


def gs1_values(ais):
    """GS1のAIから、照合に使う値を取り出す"""
    v = {}
    for ai in ('3100', '3101', '3102', '3103'):   # 正味重量kg、末尾の数字が小数点以下の桁数
        if ai in ais and ais[ai].isdigit():
            v['label_kg'] = int(ais[ai]) / 10 ** int(ai[-1])
            break
    if ais.get('11', '').isdigit() and len(ais['11']) == 6:   # 製造（加工）日 YYMMDD
        s = ais['11']
        v['processed_date'] = f'20{s[:2]}-{s[2:4]}-{s[4:6]}'
    if ais.get('15', '').isdigit() and len(ais['15']) == 6:   # 賞味期限 YYMMDD（入っている業者があれば）
        s = ais['15']
        v['best_before'] = f'20{s[:2]}-{s[2:4]}-{s[4:6]}'
    if ais.get('251', '').isdigit():                           # 元の個体の参照番号＝個体識別番号
        v['individual_no'] = ais['251']
    return v


def _to_float(s):
    try:
        return float(str(s).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def check_piece(p, today=None):
    """読み取った1件を検査して、黄色表示にする項目と理由を返す"""
    today = today or date.today()
    warn = {}
    for f in p.get('unreadable') or []:
        warn[f] = '写真から読み取れませんでした'
    no = p.get('individual_no') or ''
    if p.get('number_type') == '荷口番号':
        warn['individual_no'] = '荷口番号です。家畜改良センターでは照会できないので、業者に個体を確認してください'
    elif no and not re.fullmatch(r'\d{10}', no):
        warn['individual_no'] = '個体識別番号は10桁の数字です'
    kg = _to_float(p.get('label_kg'))
    tkg = _to_float(p.get('trade_kg'))
    if kg is not None and not (0.3 <= kg <= 40):
        warn['label_kg'] = '重量が普段の範囲（0.3〜40kg）を外れています'
    if kg is not None and tkg is not None and tkg != math.floor(kg * 10 + 1e-9) / 10:
        warn['trade_kg'] = f'取引値は通常、重量の0.1kg未満を切り捨てた値です（{kg} → {math.floor(kg * 10 + 1e-9) / 10}）'
    bb = p.get('best_before') or ''
    try:
        if bb and date.fromisoformat(bb) < today:
            warn['best_before'] = '賞味期限が今日より前です（加工日と取り違えていないか確認）'
    except ValueError:
        warn['best_before'] = '日付の形式が読み取れませんでした'
    # バーコード下の数字と、文字で読んだ値の食い違い
    g = gs1_values(parse_gs1(p.get('barcode_texts')))
    if g.get('individual_no') and no and g['individual_no'] != no:
        warn['individual_no'] = f'バーコードの番号（{g["individual_no"]}）と違います'
    if g.get('label_kg') is not None and kg is not None and abs(g['label_kg'] - kg) > 0.005:
        warn['label_kg'] = f'バーコードの重量（{g["label_kg"]}kg）と違います'
    if g.get('processed_date') and p.get('processed_date') and g['processed_date'] != p['processed_date']:
        warn['processed_date'] = f'バーコードの加工日（{g["processed_date"]}）と違います'
    return warn


def fill_from_gs1(p):
    """文字で読めなかった項目を、バーコード下の数字から補う"""
    g = gs1_values(parse_gs1(p.get('barcode_texts')))
    for k, v in g.items():
        if not p.get(k):
            p[k] = str(v)
            if k in (p.get('unreadable') or []):
                p['unreadable'].remove(k)
    return p


# ============================================================
# Claude API 呼び出し
# ============================================================
def read_label(image_b64, media_type='image/jpeg'):
    """ラベル写真を読み取る。戻り値: {'pieces': [...], 'notes': str}
    各 piece には検査結果 'warnings'（項目名→理由）を付ける。"""
    if not is_configured():
        raise LabelReadError('写真の読み取りは未設定です（ANTHROPIC_API_KEY が設定されていません）。手入力で登録できます。')
    import anthropic

    client = anthropic.Anthropic()
    try:
        # 安全分類で断られた場合に別モデルで自動でやり直す（server-side fallback）
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
            output_config={
                'effort': 'medium',
                'format': {'type': 'json_schema', 'schema': SCHEMA},
            },
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': image_b64}},
                    {'type': 'text', 'text': PROMPT},
                ],
            }],
        )
    except anthropic.AuthenticationError:
        raise LabelReadError('APIキーが正しくありません。設定を確認してください。')
    except anthropic.RateLimitError:
        raise LabelReadError('読み取りが混み合っています。少し待ってからもう一度お試しください。')
    except anthropic.BadRequestError as e:
        raise LabelReadError(f'写真を読み取れませんでした（{e.message}）')
    except anthropic.APIStatusError as e:
        raise LabelReadError(f'読み取りサービスでエラーが起きました（{e.status_code}）。もう一度お試しください。')
    except anthropic.APIConnectionError:
        raise LabelReadError('読み取りサービスに接続できませんでした。通信状態を確認してください。')

    if response.stop_reason == 'refusal':
        raise LabelReadError('この写真は読み取れませんでした。撮り直すか、手入力してください。')
    if response.stop_reason == 'max_tokens':
        raise LabelReadError('読み取り結果が長すぎて途中で切れました。ラベルを数枚ずつ撮ってください。')
    raw = next((b.text for b in response.content if b.type == 'text'), '')
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise LabelReadError('読み取り結果の形式が正しくありませんでした。もう一度お試しください。')

    for p in data.get('pieces', []):
        fill_from_gs1(p)
        p['warnings'] = check_piece(p)
    data['raw'] = raw
    return data
