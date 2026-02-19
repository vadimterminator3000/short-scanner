"""
==============================================
  SHORT SCANNER v3.0
  Binance Futures | Only SHORT signals
  High Quality & Quantity
==============================================
"""

import asyncio
import time
import json
import sqlite3
import requests
import numpy as np
from datetime import datetime, timedelta
from contextlib import contextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==============================================================
#                     КОНФИГУРАЦИЯ
# ==============================================================

TELEGRAM_BOT_TOKEN = "7980547578:AAG0fFJmduZJviTUa-4an3HyHMlZc13KQRE"

SCAN_INTERVAL_SECONDS = 45
TF_15M = "15m"
TF_1H  = "1h"
TF_4H  = "4h"

MIN_VOLUME_USDT_24H  = 3_000_000   # минимальный суточный объём
MIN_PRICE_CHANGE_PCT = 3.0         # минимальное движение цены
MIN_SCORE           = 40           # минимальный скор для сигнала
MIN_RR              = 1.8          # минимальный R:R
MAX_ACTIVE_SIGNALS  = 30
COOLDOWN_MINUTES    = 45
SIGNAL_EXPIRY_HOURS = 8
TRACK_INTERVAL      = 25

RSI_PERIOD = 14
ATR_PERIOD = 14

DB_NAME = "short_scanner.db"

# ==============================================================
#                     БАЗА ДАННЫХ
# ==============================================================

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, score REAL, signal_class TEXT,
            entry_price REAL, ez_lo REAL, ez_hi REAL,
            tp1 REAL, tp2 REAL, tp3 REAL, sl REAL,
            rr REAL, change_pct REAL, rsi REAL, funding REAL,
            analysis TEXT, status TEXT DEFAULT 'active',
            result TEXT, pnl REAL,
            tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0,
            tp3_hit INTEGER DEFAULT 0, sl_hit INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            closed_at TEXT, close_price REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            joined_at TEXT DEFAULT (datetime('now','localtime')),
            active INTEGER DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS cooldowns (
            symbol TEXT PRIMARY KEY,
            expires_at TEXT
        )""")


def save_signal(sd):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO signals
            (symbol, score, signal_class, entry_price, ez_lo, ez_hi,
             tp1, tp2, tp3, sl, rr, change_pct, rsi, funding, analysis)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sd['symbol'], sd['score'], sd['cls'],
             sd['entry'], sd['ez_lo'], sd['ez_hi'],
             sd['tp1'], sd['tp2'], sd['tp3'], sd['sl'], sd['rr'],
             sd['change'], sd['rsi'], sd['funding'],
             json.dumps(sd['factors'])))
        return c.lastrowid


def get_active():
    with get_db() as conn:
        return [dict(r) for r in conn.cursor().execute(
            "SELECT * FROM signals WHERE status='active' ORDER BY created_at DESC").fetchall()]


def update_status(sid, status, result=None, pnl=None, cp=None,
                  t1=False, t2=False, t3=False, sl=False):
    with get_db() as conn:
        conn.cursor().execute("""UPDATE signals SET
            status=?, result=?, pnl=?, close_price=?,
            tp1_hit=?, tp2_hit=?, tp3_hit=?, sl_hit=?,
            closed_at=datetime('now','localtime') WHERE id=?""",
            (status, result, pnl, cp, int(t1), int(t2), int(t3), int(sl), sid))


def get_stats(days=7):
    with get_db() as conn:
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        r = conn.cursor().execute("""SELECT
            COUNT(*) total,
            SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) wins,
            SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) losses,
            AVG(CASE WHEN result='win' THEN pnl END) avg_win,
            AVG(CASE WHEN result='loss' THEN pnl END) avg_loss,
            SUM(tp1_hit) tp1, SUM(tp2_hit) tp2, SUM(tp3_hit) tp3,
            SUM(CASE WHEN signal_class='S' THEN 1 ELSE 0 END) s_cls,
            SUM(CASE WHEN signal_class='A' THEN 1 ELSE 0 END) a_cls,
            SUM(CASE WHEN signal_class='B' THEN 1 ELSE 0 END) b_cls
            FROM signals WHERE created_at>=? AND status!='active'""", (since,)).fetchone()
        return dict(r) if r else None


def add_sub(cid, uname=None):
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT OR REPLACE INTO subscribers (chat_id,username,active) VALUES (?,?,1)",
            (cid, uname))


def get_subs():
    with get_db() as conn:
        return [r['chat_id'] for r in conn.cursor().execute(
            "SELECT chat_id FROM subscribers WHERE active=1").fetchall()]


def set_cooldown(sym):
    with get_db() as conn:
        exp = (datetime.now() + timedelta(minutes=COOLDOWN_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
        conn.cursor().execute(
            "INSERT OR REPLACE INTO cooldowns VALUES (?,?)", (sym, exp))


def on_cooldown(sym):
    with get_db() as conn:
        r = conn.cursor().execute(
            "SELECT expires_at FROM cooldowns WHERE symbol=?", (sym,)).fetchone()
        if r:
            if datetime.now() < datetime.strptime(r['expires_at'], '%Y-%m-%d %H:%M:%S'):
                return True
            conn.cursor().execute("DELETE FROM cooldowns WHERE symbol=?", (sym,))
        return False


# ==============================================================
#                  BINANCE API
# ==============================================================

class Binance:
    BASE = "https://fapi.binance.com"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers['User-Agent'] = 'Mozilla/5.0'

    def get(self, ep, params=None):
        try:
            r = self.s.get(f"{self.BASE}{ep}", params=params, timeout=12)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(30)
            return None
        except:
            return None

    def tickers(self):
        return self.get("/fapi/v1/ticker/24hr") or []

    def klines(self, sym, tf, limit=100):
        d = self.get("/fapi/v1/klines", {'symbol': sym, 'interval': tf, 'limit': limit})
        if not d:
            return None
        return [{'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]),
                 'c': float(k[4]), 'v': float(k[5]), 'qv': float(k[7]),
                 'tbqv': float(k[10])} for k in d]

    def funding(self, sym):
        d = self.get("/fapi/v1/premiumIndex", {'symbol': sym})
        if not d:
            return None
        return {'rate': float(d.get('lastFundingRate', 0)),
                'mark': float(d.get('markPrice', 0))}

    def oi(self, sym):
        d = self.get("/futures/data/openInterestHist",
                     {'symbol': sym, 'period': '15m', 'limit': 8})
        if not d:
            return None
        return [float(x['sumOpenInterestValue']) for x in d]

    def ls(self, sym):
        d = self.get("/futures/data/globalLongShortAccountRatio",
                     {'symbol': sym, 'period': '15m', 'limit': 5})
        if not d:
            return None
        return [float(x['longShortRatio']) for x in d]

    def collect(self, sym):
        c15 = self.klines(sym, TF_15M, 60)
        if not c15 or len(c15) < 20:
            return None
        return {
            'symbol': sym,
            'c15': c15,
            'c1h': self.klines(sym, TF_1H, 50),
            'c4h': self.klines(sym, TF_4H, 30),
            'funding': self.funding(sym),
            'oi': self.oi(sym),
            'ls': self.ls(sym),
        }


# ==============================================================
#                  ИНДИКАТОРЫ
# ==============================================================

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    c = np.array(closes, dtype=float)
    d = np.diff(c)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag, al = np.mean(g[:period]), np.mean(l[:period])
    for i in range(period, len(g)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + l[i]) / period
    return round(100.0 - 100.0 / (1.0 + ag / al) if al != 0 else 100.0, 2)


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0
    tr = [max(highs[i] - lows[i],
              abs(highs[i] - closes[i-1]),
              abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
    if not tr:
        return 0
    a = np.mean(tr[:period])
    for i in range(period, len(tr)):
        a = (a * (period - 1) + tr[i]) / period
    return round(a, 8)


def ema(values, period):
    v = np.array(values, dtype=float)
    if len(v) < period:
        return v
    e = np.zeros_like(v)
    m = 2.0 / (period + 1)
    e[period - 1] = np.mean(v[:period])
    for i in range(period, len(v)):
        e[i] = (v[i] - e[i-1]) * m + e[i-1]
    return e


def detect_bearish_pattern(candles):
    """Определяет медвежьи паттерны свечей"""
    if len(candles) < 3:
        return 0, "нет"
    
    c = candles
    o1, h1, l1, c1 = c[-3]['o'], c[-3]['h'], c[-3]['l'], c[-3]['c']
    o2, h2, l2, c2 = c[-2]['o'], c[-2]['h'], c[-2]['l'], c[-2]['c']
    o3, h3, l3, c3 = c[-1]['o'], c[-1]['h'], c[-1]['l'], c[-1]['c']
    
    body3 = abs(c3 - o3)
    range3 = h3 - l3
    upper_wick = h3 - max(o3, c3)
    lower_wick = min(o3, c3) - l3

    patterns = []
    score = 0

    # Медвежье поглощение
    if c2 > o2 and o3 > c2 and c3 < o2:
        patterns.append("Bearish Engulfing")
        score += 3

    # Shooting Star / Pin Bar (длинный верхний хвост)
    if range3 > 0 and upper_wick > body3 * 2 and upper_wick > lower_wick * 3:
        patterns.append("Shooting Star")
        score += 2

    # Вечерняя звезда
    if c1 > o1 and body3 < (h2 - l2) * 0.3 and o3 < o1 and c3 < (o1 + c1) / 2:
        patterns.append("Evening Star")
        score += 3

    # Три медвежьих свечи подряд
    if c1 < o1 and c2 < o2 and c3 < o3 and c3 < c2 < c1:
        patterns.append("3 Black Crows")
        score += 2

    # Доджи после роста
    if c2 > o2 and body3 < range3 * 0.1 and range3 > 0:
        patterns.append("Doji Top")
        score += 1

    return score, ", ".join(patterns) if patterns else "нет"


def find_resistance(candles, lookback=20):
    """Находит ближайший уровень сопротивления"""
    highs = [c['h'] for c in candles[-lookback:]]
    price = candles[-1]['c']
    
    # Кластеризуем максимумы
    resistance_levels = []
    for i in range(len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            resistance_levels.append(highs[i])
    
    # Находим ближайший уровень выше текущей цены
    above = [r for r in resistance_levels if r > price * 1.002]
    if above:
        return min(above)
    return max(highs)


def rsi_divergence(candles, period=14):
    """Проверяет медвежью дивергенцию RSI"""
    if len(candles) < 30:
        return False, 0
    
    closes = [c['c'] for c in candles]
    highs = [c['h'] for c in candles]
    
    # RSI для последних свечей
    rsi_values = []
    for i in range(period, len(closes)):
        rsi_values.append(rsi(closes[:i+1], period))
    
    if len(rsi_values) < 10:
        return False, 0
    
    # Ищем последние два локальных максимума цены
    price_highs = []
    rsi_highs = []
    for i in range(2, len(rsi_values) - 1):
        if highs[period + i] > highs[period + i - 1] and highs[period + i] > highs[period + i + 1]:
            price_highs.append((i, highs[period + i]))
            rsi_highs.append((i, rsi_values[i]))
    
    if len(price_highs) >= 2:
        # Медвежья дивергенция: цена выше, RSI ниже
        p1, p2 = price_highs[-2], price_highs[-1]
        r1, r2 = rsi_highs[-2], rsi_highs[-1]
        if p2[1] > p1[1] and r2[1] < r1[1]:
            strength = round((p2[1] - p1[1]) / p1[1] * 100, 2)
            return True, strength
    
    return False, 0


# ==============================================================
#               АНАЛИЗАТОР SHORT СИГНАЛОВ
# ==============================================================

class Analyzer:
    
    def analyze(self, data, btc_change=0.0):
        c15 = data['c15']
        if not c15 or len(c15) < 20:
            return None
        
        closes = [x['c'] for x in c15]
        highs  = [x['h'] for x in c15]
        lows   = [x['l'] for x in c15]
        vols   = [x['qv'] for x in c15]
        
        price    = closes[-1]
        price_20 = closes[-20]
        change   = ((price - price_20) / price_20) * 100
        
        # Только SHORT — нужен рост цены
        if change < MIN_PRICE_CHANGE_PCT:
            return None
        
        factors = {}
        total_score = 0

        # --- ФАКТОР 1: Сила движения vs ATR ---
        a = atr(highs, lows, closes, ATR_PERIOD)
        atr_pct = (a / price * 100) if price > 0 and a > 0 else 1
        surge_ratio = change / atr_pct
        
        if surge_ratio >= 4:    s1 = 10
        elif surge_ratio >= 3:  s1 = 8
        elif surge_ratio >= 2:  s1 = 6
        elif surge_ratio >= 1.5: s1 = 4
        else:                   s1 = 2
        factors['surge'] = {'s': s1, 'info': f"{change:+.1f}% | ATR×{surge_ratio:.1f}"}
        total_score += s1 * 1.5

        # --- ФАКТОР 2: Объём ---
        cur_vol = vols[-1]
        avg_vol = np.mean(vols[-25:-1]) if len(vols) > 25 else np.mean(vols[:-1])
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1
        avg3 = np.mean(vols[-3:]) / avg_vol if avg_vol > 0 else 1
        best_vol = max(vol_ratio, avg3)
        
        if best_vol >= 5:    s2 = 10
        elif best_vol >= 3:  s2 = 8
        elif best_vol >= 2:  s2 = 6
        elif best_vol >= 1.5: s2 = 4
        else:                s2 = 1
        factors['volume'] = {'s': s2, 'info': f"×{best_vol:.1f} от среднего"}
        total_score += s2 * 1.5

        # --- ФАКТОР 3: RSI перекупленность ---
        rsi_15 = rsi(closes, RSI_PERIOD)
        rsi_1h = 50
        if data.get('c1h') and len(data['c1h']) > RSI_PERIOD:
            rsi_1h = rsi([x['c'] for x in data['c1h']], RSI_PERIOD)
        rsi_4h = 50
        if data.get('c4h') and len(data['c4h']) > RSI_PERIOD:
            rsi_4h = rsi([x['c'] for x in data['c4h']], RSI_PERIOD)

        # RSI на 15м перекуплен
        if rsi_15 >= 85:    s3 = 10
        elif rsi_15 >= 78:  s3 = 8
        elif rsi_15 >= 72:  s3 = 6
        elif rsi_15 >= 65:  s3 = 3
        else:               s3 = 0
        
        # Бонус если 1h тоже перекуплен
        if rsi_1h >= 65: s3 = min(s3 + 2, 10)
        # Бонус если 4h тоже перекуплен
        if rsi_4h >= 60: s3 = min(s3 + 2, 10)
        
        factors['rsi'] = {'s': s3, 'info': f"15m:{rsi_15:.0f} 1h:{rsi_1h:.0f} 4h:{rsi_4h:.0f}"}
        total_score += s3 * 1.5

        # --- ФАКТОР 4: Funding Rate (высокий = шорт-сигнал) ---
        fd = data.get('funding')
        funding_rate = 0
        s4 = 0
        if fd:
            funding_rate = fd['rate']
            rpct = funding_rate * 100
            if rpct >= 0.10:   s4 = 10
            elif rpct >= 0.07: s4 = 8
            elif rpct >= 0.05: s4 = 6
            elif rpct >= 0.03: s4 = 4
            elif rpct >= 0.01: s4 = 2
        factors['funding'] = {'s': s4, 'info': f"{funding_rate*100:+.4f}%"}
        total_score += s4 * 1.3

        # --- ФАКТОР 5: Open Interest (рост OI при росте цены = опасно) ---
        s5 = 0
        oi_data = data.get('oi')
        if oi_data and len(oi_data) >= 2:
            oi_change = ((oi_data[-1] - oi_data[0]) / oi_data[0] * 100) if oi_data[0] > 0 else 0
            if oi_change >= 15:    s5 = 10
            elif oi_change >= 10:  s5 = 8
            elif oi_change >= 5:   s5 = 6
            elif oi_change >= 2:   s5 = 4
            else:                  s5 = 1
            factors['oi'] = {'s': s5, 'info': f"OI {oi_change:+.1f}%"}
        else:
            factors['oi'] = {'s': 0, 'info': 'нет данных'}
        total_score += s5 * 1.2

        # --- ФАКТОР 6: L/S Ratio (много лонгов = шорт) ---
        s6 = 0
        ls_data = data.get('ls')
        ls_ratio = 0
        if ls_data and len(ls_data) > 0:
            ls_ratio = ls_data[-1]
            if ls_ratio >= 3.0:    s6 = 10
            elif ls_ratio >= 2.5:  s6 = 8
            elif ls_ratio >= 2.0:  s6 = 6
            elif ls_ratio >= 1.5:  s6 = 4
            elif ls_ratio >= 1.2:  s6 = 2
        factors['ls'] = {'s': s6, 'info': f"L/S={ls_ratio:.2f}"}
        total_score += s6 * 1.0

        # --- ФАКТОР 7: Медвежьи паттерны свечей ---
        pat_score, pat_name = detect_bearish_pattern(c15)
        s7 = min(pat_score * 2, 10)
        factors['pattern'] = {'s': s7, 'info': pat_name}
        total_score += s7 * 1.3

        # --- ФАКТОР 8: RSI дивергенция ---
        div, div_strength = rsi_divergence(c15)
        s8 = 8 if div else 0
        factors['divergence'] = {'s': s8, 'info': f"{'✓ сила:' + str(div_strength) + '%' if div else 'нет'}"}
        total_score += s8 * 1.4

        # --- ФАКТОР 9: Trend alignment (4h нисходящий тренд) ---
        s9 = 0
        if data.get('c4h') and len(data['c4h']) >= 20:
            c4 = [x['c'] for x in data['c4h']]
            e20 = ema(c4, 20)
            e50 = ema(c4, 50) if len(c4) >= 50 else e20
            # Цена выше EMA на 4h = перекупленность относительно тренда
            if c4[-1] > e20[-1] * 1.03:  s9 = 6
            elif c4[-1] > e20[-1] * 1.01: s9 = 3
            if len(e50) > 0 and e20[-1] < e50[-1]:  # 4h в нисходящем тренде
                s9 = min(s9 + 3, 10)
        factors['trend'] = {'s': s9, 'info': f"4h тренд скор:{s9}"}
        total_score += s9 * 1.0

        # --- ФАКТОР 10: BTC независимость ---
        solo = change - abs(btc_change)
        if solo >= 8:    s10 = 10
        elif solo >= 5:  s10 = 7
        elif solo >= 3:  s10 = 5
        elif solo >= 1:  s10 = 3
        else:            s10 = 1
        factors['btc'] = {'s': s10, 'info': f"соло {solo:+.1f}% (BTC {btc_change:+.1f}%)"}
        total_score += s10 * 0.8

        # Нормализуем скор до 100
        max_possible = (10*1.5 + 10*1.5 + 10*1.5 + 10*1.3 + 10*1.2 + 10*1.0 + 10*1.3 + 10*1.4 + 10*1.0 + 10*0.8)
        score_pct = round((total_score / max_possible) * 100, 1)

        if score_pct >= 75:    cls, em = 'S', '🏆'
        elif score_pct >= 60:  cls, em = 'A', '💎'
        elif score_pct >= 45:  cls, em = 'B', '🔥'
        elif score_pct >= 30:  cls, em = 'C', '⚡'
        else:                  cls, em = 'D', '📊'

        return {
            'symbol': data['symbol'],
            'price': price, 'change': round(change, 2),
            'high': max(highs[-20:]), 'low': min(lows[-20:]),
            'score': score_pct, 'cls': cls, 'em': em,
            'factors': factors, 'rsi': rsi_15,
            'funding': funding_rate,
            'closes': closes, 'highs': highs, 'lows': lows,
        }


# ==============================================================
#                   РАСЧЁТ ВХОДА
# ==============================================================

def calc_entry(an):
    price = an['price']
    hi    = an['high']
    lo    = an['low']
    diff  = hi - lo
    closes = an['closes']
    highs  = an['highs']
    lows   = an['lows']
    a = atr(highs, lows, closes, ATR_PERIOD)

    # Зона входа: у текущей цены или у хая
    ez_hi = max(price, hi * 0.999)
    ez_lo = ez_hi * 0.998
    mid   = (ez_lo + ez_hi) / 2

    # Тейки по фибоначчи
    tp1 = hi - diff * 0.382
    tp2 = hi - diff * 0.500
    tp3 = hi - diff * 0.618

    # Корректируем если тейки выше входа
    if tp1 >= mid: tp1 = mid * 0.985
    if tp2 >= tp1: tp2 = tp1 * 0.990
    if tp3 >= tp2: tp3 = tp2 * 0.990

    sl   = hi + a * 1.5
    risk = sl - mid
    rw   = mid - tp1

    rr = round(rw / risk, 2) if risk > 0 else 0

    def pct(a, b): return round(((b - a) / a) * 100, 2)

    return {
        'entry': round(mid, 8), 'ez_lo': round(ez_lo, 8), 'ez_hi': round(ez_hi, 8),
        'tp1': round(tp1, 8), 'tp2': round(tp2, 8), 'tp3': round(tp3, 8),
        'sl':  round(sl,  8), 'rr': rr,
        'tp1p': pct(mid, tp1), 'tp2p': pct(mid, tp2),
        'tp3p': pct(mid, tp3), 'slp':  pct(mid, sl),
    }


# ==============================================================
#               СКАНЕР
# ==============================================================

class Scanner:
    def __init__(self):
        self.bn  = Binance()
        self.ana = Analyzer()
        self.btc = 0.0

    def pre_screen(self):
        tickers = self.bn.tickers()
        if not tickers:
            return []
        cands = []
        for t in tickers:
            sym = t.get('symbol', '')
            if sym == 'BTCUSDT':
                try: self.btc = float(t.get('priceChangePercent', 0))
                except: pass
            if not sym.endswith('USDT'):
                continue
            try:
                ch  = float(t.get('priceChangePercent', 0))
                vol = float(t.get('quoteVolume', 0))
                lp  = float(t.get('lastPrice', 0))
                if vol < MIN_VOLUME_USDT_24H: continue
                if ch < MIN_PRICE_CHANGE_PCT * 0.7: continue  # только рост
                if lp <= 0: continue
                if on_cooldown(sym): continue
                cands.append({'symbol': sym, 'ch': ch, 'vol': vol})
            except:
                continue
        cands.sort(key=lambda x: x['ch'], reverse=True)
        return cands[:60]

    def deep(self, sym):
        try:
            data = self.bn.collect(sym)
            if not data:
                return None
            an = self.ana.analyze(data, self.btc)
            if not an:
                return None
            if an['score'] < MIN_SCORE:
                return None

            entry = calc_entry(an)
            if entry['rr'] < MIN_RR:
                return None

            # Проверяем активные сигналы
            active = get_active()
            if len(active) >= MAX_ACTIVE_SIGNALS:
                return None
            if any(s['symbol'] == sym for s in active):
                return None

            # BTC фильтр: не шортим если BTC сильно падает (паника)
            if self.btc < -5.0:
                return None

            return {
                'symbol': sym, 'score': an['score'], 'cls': an['cls'], 'em': an['em'],
                'change': an['change'], 'price': an['price'],
                'entry': entry['entry'], 'ez_lo': entry['ez_lo'], 'ez_hi': entry['ez_hi'],
                'tp1': entry['tp1'], 'tp2': entry['tp2'], 'tp3': entry['tp3'],
                'sl': entry['sl'], 'rr': entry['rr'],
                'tp1p': entry['tp1p'], 'tp2p': entry['tp2p'],
                'tp3p': entry['tp3p'], 'slp': entry['slp'],
                'rsi': an['rsi'], 'funding': an['funding'],
                'factors': an['factors'],
            }
        except:
            return None

    def scan(self):
        cands = self.pre_screen()
        if not cands:
            return []
        sigs = []
        for c in cands:
            sig = self.deep(c['symbol'])
            if sig:
                sigs.append(sig)
            time.sleep(0.3)
        sigs.sort(key=lambda x: x['score'], reverse=True)
        return sigs


# ==============================================================
#               ТРЕКЕР
# ==============================================================

class Tracker:
    def __init__(self, bn):
        self.bn = bn

    def check(self):
        results = []
        for sig in get_active():
            r = self._check_one(sig)
            if r:
                results.append(r)
        return results

    def _price(self, sym):
        f = self.bn.funding(sym)
        return f['mark'] if f else None

    def _check_one(self, sig):
        try:
            sym = sig['symbol']
            sid = sig['id']
            created = datetime.strptime(sig['created_at'], '%Y-%m-%d %H:%M:%S')
            cp = self._price(sym)
            if not cp:
                return None

            # Истёк
            if datetime.now() - created > timedelta(hours=SIGNAL_EXPIRY_HOURS):
                entry = sig['entry_price']
                pnl = round((entry - cp) / entry * 100, 2)
                update_status(sid, 'closed', 'expired', pnl, cp)
                return {'type': 'expired', 'sig': sig, 'pnl': pnl, 'cp': cp}

            entry = sig['entry_price']
            pnl = round((entry - cp) / entry * 100, 2)

            t1w = bool(sig['tp1_hit'])
            t2w = bool(sig['tp2_hit'])
            t3w = bool(sig['tp3_hit'])

            t1h = cp <= sig['tp1']
            t2h = cp <= sig['tp2']
            t3h = cp <= sig['tp3']
            slh = cp >= sig['sl']

            if slh:
                update_status(sid, 'closed', 'loss', pnl, cp,
                              t1=t1h or t1w, t2=t2h or t2w, t3=t3h or t3w, sl=True)
                return {'type': 'sl', 'sig': sig, 'pnl': pnl, 'cp': cp}

            if t3h and not t3w:
                update_status(sid, 'closed', 'win', pnl, cp, t1=True, t2=True, t3=True)
                return {'type': 'tp3', 'sig': sig, 'pnl': pnl, 'cp': cp}

            if t2h and not t2w:
                update_status(sid, 'active', t1=True, t2=True, t3=t3w)
                return {'type': 'tp2', 'sig': sig, 'pnl': pnl, 'cp': cp}

            if t1h and not t1w:
                update_status(sid, 'active', t1=True, t2=t2w, t3=t3w)
                return {'type': 'tp1', 'sig': sig, 'pnl': pnl, 'cp': cp}

            return None
        except:
            return None


# ==============================================================
#               ФОРМАТИРОВАНИЕ
# ==============================================================

FNAMES = {
    'surge': '📈 Импульс', 'volume': '📊 Объём', 'rsi': '🔴 RSI',
    'funding': '💰 Фандинг', 'oi': '📊 OI', 'ls': '👥 L/S',
    'pattern': '🕯 Паттерн', 'divergence': '📉 Дивергенция',
    'trend': '📐 Тренд', 'btc': '₿ BTC',
}


def fmt_signal(sig):
    sc = sig['score']
    cls = sig['cls']
    em = sig['em']

    lines = [
        f"{'━'*28}",
        f"{em} *SHORT SIGNAL {cls}* | Скор: *{sc}%*",
        f"{'━'*28}",
        f"",
        f"🪙 *{sig['symbol']}* PERP",
        f"📈 Рост: *+{sig['change']:.2f}%* → ищем откат",
        f"💰 Цена: `{sig['price']:.6g}`",
        f"",
        f"📊 *Факторы анализа:*",
    ]

    for k, v in sig['factors'].items():
        nm = FNAMES.get(k, k)
        bar = '█' * v['s'] + '░' * (10 - v['s'])
        lines.append(f"├ {nm}: `{bar}` ({v['s']}/10)")
        if v['info'] and v['info'] != 'нет данных':
            lines.append(f"│  _{v['info']}_")

    e = sig
    lines += [
        f"",
        f"🎯 *Торговый план SHORT:*",
        f"├ 📍 Вход: `{e['ez_lo']:.6g}` – `{e['ez_hi']:.6g}`",
        f"├ ✅ TP1: `{e['tp1']:.6g}` ({e['tp1p']:.1f}%) → 40% позиции",
        f"├ ✅ TP2: `{e['tp2']:.6g}` ({e['tp2p']:.1f}%) → 35% позиции",
        f"├ ✅ TP3: `{e['tp3']:.6g}` ({e['tp3p']:.1f}%) → 25% позиции",
        f"└ 🛑 SL:  `{e['sl']:.6g}`  ({e['slp']:.1f}%)",
        f"",
        f"⚖️ *R:R = 1:{e['rr']:.1f}*",
        f"📡 RSI: {sig['rsi']:.0f} | Фандинг: {sig['funding']*100:+.4f}%",
        f"⏰ {datetime.now().strftime('%H:%M %d.%m.%Y')}",
        f"",
        f"⚠️ _Не финансовый совет. Управляй риском!_",
    ]

    return "\n".join(lines)


def fmt_update(upd):
    sig = upd['sig']
    pnl = upd['pnl']
    tp  = upd['type']

    labels = {
        'tp1': ('✅', 'TP1 ДОСТИГНУТ'),
        'tp2': ('✅✅', 'TP2 ДОСТИГНУТ'),
        'tp3': ('🎉', 'TP3 — ПОЛНЫЙ ТЕЙК!'),
        'sl':  ('🛑', 'СТОП ЛОСС'),
        'expired': ('⏰', 'ИСТЁК'),
    }
    em, title = labels.get(tp, ('📊', 'UPDATE'))
    pe = '🟢' if pnl > 0 else '🔴'

    return "\n".join([
        f"{em} *{title}*",
        f"",
        f"🪙 {sig['symbol']} SHORT",
        f"{pe} PnL: *{pnl:+.2f}%*",
        f"💰 Цена закрытия: `{upd['cp']:.6g}`",
        f"📊 Класс: {sig['signal_class']}",
        f"",
        f"├ Entry: `{sig['entry_price']:.6g}`",
        f"├ TP1: {'✅' if sig.get('tp1_hit') else '⬜'}",
        f"├ TP2: {'✅' if sig.get('tp2_hit') else '⬜'}",
        f"├ TP3: {'✅' if sig.get('tp3_hit') else '⬜'}",
        f"└ SL:  {'🛑' if tp == 'sl' else '⬜'}",
    ])


def fmt_stats(st, days):
    if not st or not st.get('total'):
        return f"📊 Нет данных за {days} дней"
    t  = st['total'] or 0
    w  = st['wins']  or 0
    l  = st['losses'] or 0
    wr = round(w / (w + l) * 100) if (w + l) > 0 else 0
    return "\n".join([
        f"📊 *СТАТИСТИКА {days}Д*",
        f"Всего: {t} | ✅ {w} ({wr}%) | ❌ {l}",
        f"💰 Ср.win: {st['avg_win'] or 0:+.2f}% | 💸 Ср.loss: {st['avg_loss'] or 0:+.2f}%",
        f"🎯 TP1:{st['tp1'] or 0} TP2:{st['tp2'] or 0} TP3:{st['tp3'] or 0}",
        f"🏆S:{st['s_cls'] or 0} 💎A:{st['a_cls'] or 0} 🔥B:{st['b_cls'] or 0}",
    ])


# ==============================================================
#               TELEGRAM БОТЫ
# ==============================================================

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Сканировать", callback_data="scan"),
         InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("▶️ Авто-мониторинг", callback_data="go"),
         InlineKeyboardButton("⏹ Остановить", callback_data="stop")],
        [InlineKeyboardButton("📋 Активные", callback_data="active")],
    ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    add_sub(update.effective_chat.id, update.effective_user.username)
    await update.message.reply_text(
        "🤖 *SHORT SCANNER v3.0*\n"
        "_Binance Futures | 10 факторов | Только SHORT_\n\n"
        "📡 Сканирование каждые 45 сек\n"
        "🔍 10 факторов анализа\n"
        "🕯 Паттерны + RSI дивергенция\n"
        "📐 Мульти-таймфрейм (15m/1h/4h)\n\n"
        "⚡C(30+) 🔥B(45+) 💎A(60+) 🏆S(75+)",
        reply_markup=main_kb(), parse_mode='Markdown'
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = fmt_stats(get_stats(7), 7) + "\n\n" + fmt_stats(get_stats(30), 30)
    await update.message.reply_text(msg, parse_mode='Markdown')


async def cmd_active(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    act = get_active()
    if not act:
        await update.message.reply_text("📋 Нет активных сигналов")
        return
    lines = [f"📋 *Активные SHORT ({len(act)}):*"]
    for s in act:
        hrs = (datetime.now() - datetime.strptime(s['created_at'], '%Y-%m-%d %H:%M:%S')).total_seconds() / 3600
        lines.append(f"🔴 *{s['symbol']}* {s['signal_class']} {s['score']:.0f}% — {hrs:.1f}ч")
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def btn_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = q.message.chat_id

    if q.data == "scan":
        ctx.application.bot_data['manual_scan'] = True
        ctx.application.bot_data['scan_cid'] = cid
        await q.edit_message_text("🔍 Сканирую рынок... 2-3 мин", reply_markup=main_kb())

    elif q.data == "go":
        ctx.application.bot_data['monitoring'] = True
        add_sub(cid, q.from_user.username)
        await q.edit_message_text("▶️ Авто-мониторинг запущен!\nСигналы будут приходить автоматически.", reply_markup=main_kb())

    elif q.data == "stop":
        ctx.application.bot_data['monitoring'] = False
        await q.edit_message_text("⏹ Мониторинг остановлен", reply_markup=main_kb())

    elif q.data == "stats":
        msg = fmt_stats(get_stats(7), 7) + "\n\n" + fmt_stats(get_stats(30), 30)
        await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=main_kb())

    elif q.data == "active":
        act = get_active()
        if not act:
            await q.edit_message_text("📋 Нет активных сигналов", reply_markup=main_kb())
        else:
            lines = [f"📋 *Активные SHORT ({len(act)}):*"]
            for s in act:
                lines.append(f"🔴 *{s['symbol']}* {s['signal_class']} {s['score']:.0f}%")
            await q.edit_message_text("\n".join(lines), parse_mode='Markdown', reply_markup=main_kb())


# ==============================================================
#               ФОНОВЫЕ ЗАДАЧИ
# ==============================================================

async def scan_loop(app, scanner):
    await asyncio.sleep(5)
    while True:
        try:
            mon = app.bot_data.get('monitoring', False)
            man = app.bot_data.get('manual_scan', False)

            if mon or man:
                app.bot_data['manual_scan'] = False
                sigs = await asyncio.to_thread(scanner.scan)

                if sigs:
                    subs = get_subs()
                    for sig in sigs:
                        sid = save_signal(sig)
                        set_cooldown(sig['symbol'])
                        msg = fmt_signal(sig)
                        for cid in subs:
                            try:
                                await app.bot.send_message(cid, msg, parse_mode='Markdown')
                            except:
                                pass
                        await asyncio.sleep(1)
                elif man:
                    scid = app.bot_data.get('scan_cid')
                    if scid:
                        try:
                            await app.bot.send_message(scid, "😴 Нет сигналов. Рынок спокоен.")
                        except:
                            pass

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        except Exception as e:
            await asyncio.sleep(30)


async def track_loop(app, scanner):
    await asyncio.sleep(15)
    tracker = Tracker(scanner.bn)
    while True:
        try:
            results = await asyncio.to_thread(tracker.check)
            if results:
                subs = get_subs()
                for upd in results:
                    msg = fmt_update(upd)
                    for cid in subs:
                        try:
                            await app.bot.send_message(cid, msg, parse_mode='Markdown')
                        except:
                            pass
                    await asyncio.sleep(0.5)
            await asyncio.sleep(TRACK_INTERVAL)
        except:
            await asyncio.sleep(30)


async def post_init(app):
    scanner = Scanner()
    app.bot_data['monitoring'] = False
    app.bot_data['manual_scan'] = False
    asyncio.create_task(scan_loop(app, scanner))
    asyncio.create_task(track_loop(app, scanner))


# ==============================================================
#               ЗАПУСК
# ==============================================================

def main():
    print("=" * 40)
    print("  SHORT SCANNER v3.0")
    print("  Binance Futures | 10 factors")
    print("=" * 40)
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("active", cmd_active))
    app.add_handler(CallbackQueryHandler(btn_handler))
    app.run_polling()


if __name__ == '__main__':
    main()
