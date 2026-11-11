// 棚卸の目視チェック（目視・無い）と、バーコード（GS1-128）での確認【試し】
// ✓はこの端末だけに覚える（棚卸の保存とは別。保存されるのは「無い」と重さだけ）

// ------------------------------------------------------------
// GS1-128 の読み解き（画面が無くても使えるように、先に定義する）
//   麒麟屋に来るラベルの中身：(251)個体識別番号 (3102)正味重量 (11)加工日 (21)シリアル
// ------------------------------------------------------------
const GS = '\u001d';
const FIXED = {'00': 18, '01': 14, '02': 14, '11': 6, '13': 6, '15': 6, '17': 6};
const VAR3 = ['251', '240', '241', '250'];
const VAR2 = ['10', '21', '22', '30', '37'];

function parseGS1(raw) {
  const out = {};
  const s = String(raw || '').replace(/^\]C1/, '');
  if (s.includes('(')) {   // 括弧付きで返ってきた時
    for (const m of s.matchAll(/\((\d{2,4})\)([^(]*)/g)) out[m[1]] = m[2].split(GS).join('').trim();
    return out;
  }
  let i = 0;
  while (i < s.length) {
    if (s[i] === GS) { i++; continue; }
    const two = s.substr(i, 2), four = s.substr(i, 4);
    if (FIXED[two]) { out[two] = s.substr(i + 2, FIXED[two]); i += 2 + FIXED[two]; continue; }
    if (/^3[1-6]\d\d$/.test(four)) { out[four] = s.substr(i + 4, 6); i += 10; continue; }
    let ai = null;
    if (VAR3.includes(s.substr(i, 3))) ai = s.substr(i, 3);
    else if (VAR2.includes(two)) ai = two;
    if (!ai) break;
    let end = s.indexOf(GS, i + ai.length);
    if (end < 0) end = s.length;
    out[ai] = s.slice(i + ai.length, end);
    i = end;
  }
  return out;
}

function gs1Kg(ais) {
  for (const ai of ['3100', '3101', '3102', '3103']) {
    if (ais[ai]) return Number(ais[ai]) / 10 ** Number(ai[3]);
  }
  return null;
}

// 読んだバーコードから塊を選ぶ。lots = [{id, ino, kg, serial, ok}]
// 同じ牛の箱が何本もある時は、シリアル → 重さ の順で見分け、まだ✓の無い方を選ぶ
function matchLot(raw, lots, prev) {
  const ais = Object.assign({}, prev || {}, parseGS1(raw));
  let ino = ais['251'] || '';
  const digits = String(raw).replace(/\D/g, '');
  const pool = lots.filter(l => l.ino);
  if (!ino) { const hit = pool.find(l => digits.includes(l.ino)); if (hit) ino = hit.ino; }
  const kg = gs1Kg(ais), serial = ais['21'] || '';
  if (!ino) return {kind: 'partial', ais, kg};
  const cand = pool.filter(l => l.ino === ino);
  if (!cand.length) return {kind: 'unknown', ino, kg};
  const strip = v => String(v).replace(/^0+/, '');
  const bySerial = serial ? cand.filter(l => l.serial && strip(l.serial) === strip(serial)) : [];
  const byKg = kg != null ? cand.filter(l => l.kg !== '' && l.kg != null && Math.abs(Number(l.kg) - kg) < 0.006) : [];
  const pick = bySerial.length ? bySerial : byKg.length ? byKg : cand;
  const fresh = pick.filter(l => !l.ok);
  return {kind: 'hit', lot: fresh[0] || pick[0], already: !fresh.length,
          kgDiffers: kg != null && !bySerial.length && !byKg.length, ino, kg};
}

if (typeof module !== 'undefined') module.exports = {parseGS1, gs1Kg, matchLot};

// ------------------------------------------------------------
// 画面
// ------------------------------------------------------------
if (typeof document !== 'undefined') {
  const period = (document.getElementById('count-check-js') || {dataset: {}}).dataset.period || '';
  const OK_KEY = 'meat-count-ok-' + period;
  const okIds = new Set((() => { try { return JSON.parse(localStorage.getItem(OK_KEY) || '[]'); } catch (e) { return []; } })());
  const saveOk = () => { try { localStorage.setItem(OK_KEY, JSON.stringify([...okIds])); } catch (e) {} };
  const cards = [...document.querySelectorAll('.lot-card')];
  const cardOf = id => document.getElementById('lotcard-' + id);
  const missOf = c => c.querySelector('.missing-check');
  const $ = id => document.getElementById(id);

  function refresh() {
    let done = 0;
    for (const c of cards) {
      const ok = c.querySelector('.ok-check'), miss = missOf(c);
      if (!ok || !miss) continue;
      ok.checked = okIds.has(c.dataset.id) && !miss.checked;
      c.classList.toggle('is-ok', ok.checked);
      c.classList.toggle('is-missing', miss.checked);
      if (ok.checked || miss.checked) done++;
    }
    $('ok-done').textContent = done;
    if ($('scan-count')) $('scan-count').textContent = done;
    $('ok-bar').style.width = (cards.length ? done / cards.length * 100 : 0) + '%';
  }
  function setOk(id, on) {
    id = String(id);
    if (on) { okIds.add(id); const m = missOf(cardOf(id)); if (m) m.checked = false; } else okIds.delete(id);
    saveOk(); refresh();
  }

  document.addEventListener('change', e => {
    const t = e.target;
    if (t.classList.contains('ok-check')) setOk(t.dataset.id, t.checked);
    if (t.classList.contains('missing-check')) {
      if (t.checked) okIds.delete(t.closest('.lot-card').dataset.id);
      saveOk(); refresh();
    }
  });
  // 開けた塊は、重さを入れたら「目視」にする
  document.addEventListener('input', e => {
    const c = e.target.closest && e.target.closest('.lot-card');
    if (c && e.target.name === 'lot_' + c.dataset.id && e.target.value.trim() && c.dataset.unopened === '0') setOk(c.dataset.id, true);
  });

  window.allPresent = function () {
    let n = 0;
    for (const c of cards) {
      if (c.dataset.unopened !== '1' || missOf(c).checked || missOf(c).disabled) continue;
      if (!okIds.has(c.dataset.id)) { okIds.add(c.dataset.id); n++; }
    }
    saveOk(); refresh();
    const left = cards.filter(c => c.dataset.unopened === '0' && !okIds.has(c.dataset.id) && !missOf(c).checked).length;
    alert(`未開封 ${n} 本を「目視」にしました。` +
          (left ? `\n開封済み・使い切りの ${left} 本は、量って重さを入れてください。` : '') +
          '\n無かった箱だけ「無い」に✓を付けてください。');
  };
  refresh();

  // ---------------- バーコード ----------------
  const lotsNow = () => cards.map(c => ({id: c.dataset.id, ino: c.dataset.ino, kg: c.dataset.kg,
                                          serial: c.dataset.serial, name: c.dataset.name, ok: okIds.has(c.dataset.id)}));
  let partial = null;   // 段が分かれたラベル：前に読んだ段を少しの間覚えておく
  const f2 = v => Number(v || 0).toFixed(2);
  const inoHtml = ino => `${ino.slice(0, -4)}<b>${ino.slice(-4)}</b>`;

  function handleCode(raw) {
    const prev = partial && Date.now() - partial.at < 15000 ? partial.ais : null;
    const r = matchLot(raw, lotsNow(), prev);
    if (r.kind === 'partial') {
      partial = {ais: r.ais, at: Date.now()};
      return say('warn', `番号の入っていない段でした${r.kg != null ? `（${f2(r.kg)}kg）` : ''}。同じラベルの別の段も読んでください。`);
    }
    partial = null;
    if (r.kind === 'unknown') {
      return say('ng', `一覧に無い箱です：番号 ${inoHtml(r.ino)}${r.kg != null ? ` ${f2(r.kg)}kg` : ''}<br>` +
                       '納品の入れ忘れかもしれません。紙の「一覧に無い分」にメモしてください。', true);
    }
    const lot = r.lot;
    setOk(lot.id, true);
    const c = cardOf(lot.id);
    c.classList.add('flash'); setTimeout(() => c.classList.remove('flash'), 1500);
    const note = r.kgDiffers ? `<br><span style="color:#ffe08a">重さが登録（${f2(lot.kg)}kg）と違います：バーコード ${f2(r.kg)}kg</span>` : '';
    say(r.already ? 'warn' : 'ok', (r.already ? '読み済み：' : '✓ 目視：') +
        `#${lot.id} ${lot.name} ${r.kg != null ? f2(r.kg) + 'kg' : ''}` + note);
  }

  let audio;
  function beep(freq) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const o = audio.createOscillator(), g = audio.createGain();
      o.frequency.value = freq || 1200; g.gain.value = freq === 1 ? 0 : .15;
      o.connect(g); g.connect(audio.destination);
      o.start(); o.stop(audio.currentTime + .12);
    } catch (e) {}
  }
  function say(kind, html, buzz) {
    const m = $('scan-msg');
    m.className = kind; m.innerHTML = html;
    try { navigator.vibrate && navigator.vibrate(kind === 'ok' ? 80 : [60, 60, 60]); } catch (e) {}
    if (kind === 'ok') beep(1200); else if (buzz) beep(220);
  }

  window.manualFind = function () {
    const v = $('scan-manual').value.replace(/\D/g, '');
    if (v.length < 4) return say('warn', '下4桁以上を入れてください');
    const hits = cards.filter(c => c.dataset.ino.endsWith(v) && !okIds.has(c.dataset.id) && !missOf(c).checked);
    if (!hits.length) return say('ng', `「${v}」で終わる番号で、まだ✓の付いていない塊はありません。`, true);
    if (hits.length > 1) {
      return say('warn', `${hits.length}本あります（同じ牛）：` +
                 hits.map(c => `#${c.dataset.id} ${f2(c.dataset.kg)}kg`).join('、') + '<br>画面の一覧で✓してください。');
    }
    setOk(hits[0].dataset.id, true);
    $('scan-manual').value = '';
    say('ok', `✓ 目視：#${hits[0].dataset.id} ${hits[0].dataset.name}`);
  };

  let stream = null, loopOn = false, zx = null, lastRaw = '', lastAt = 0;
  function onRaw(raw) {
    if (raw === lastRaw && Date.now() - lastAt < 3000) return;   // 同じ段を続けて読まない
    lastRaw = raw; lastAt = Date.now();
    handleCode(raw);
  }

  window.openScanner = async function () {
    $('scanner').classList.add('on');
    beep(1);   // iPhoneで音を出せるよう、押した時に一度準備する（無音）
    const video = $('scan-video');
    try {
      stream = await navigator.mediaDevices.getUserMedia(
        {video: {facingMode: 'environment', width: {ideal: 1920}, height: {ideal: 1080}}, audio: false});
    } catch (e) {
      return say('ng', 'カメラが使えません。ブラウザの設定でカメラを許可してください。<br>（下4桁で探すこともできます）');
    }
    video.srcObject = stream;
    try { await video.play(); } catch (e) {}
    loopOn = true;
    let native = null;
    try {
      if ('BarcodeDetector' in window && (await BarcodeDetector.getSupportedFormats()).includes('code_128')) {
        native = new BarcodeDetector({formats: ['code_128']});
      }
    } catch (e) {}
    if (native) {   // Android の Chrome など
      const tick = async () => {
        if (!loopOn) return;
        try { for (const b of await native.detect(video)) onRaw(b.rawValue); } catch (e) {}
        setTimeout(tick, 150);
      };
      return tick();
    }
    if (!window.ZXing) return say('ng', '読み取りの部品が読み込めませんでした。下4桁で探してください。');
    const hints = new Map();
    hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [ZXing.BarcodeFormat.CODE_128]);
    hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
    zx = new ZXing.BrowserMultiFormatReader(hints, 200);
    zx.decodeFromVideoElementContinuously(video, res => { if (res && loopOn) onRaw(res.getText()); });
  };

  window.closeScanner = function () {
    loopOn = false;
    try { zx && zx.reset(); } catch (e) {}
    zx = null;
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    $('scanner').classList.remove('on');
  };
}
