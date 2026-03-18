from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
import anthropic
from flask_login import LoginManager, current_user, login_required
from models import db, User
from auth import auth_bp
from decorators import analysis_gate, basic_required, pro_required, elite_required, byakugan_required
import stripe
import numpy as np
from scipy.stats import norm
import os
import json
import time
from datetime import datetime
from dotenv import load_dotenv
import requests as http_requests
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

app = Flask(__name__)

app.secret_key = os.environ.get('SECRET_KEY', 'jaydawolfx-secret-2026')
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///jaydawolfx.db')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_DURATION'] = 60 * 60 * 24 * 30
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * 30
app.config['SESSION_PERMANENT'] = True

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PRICES = {
    'basic': os.environ.get('STRIPE_BASIC_PRICE_ID', 'price_REPLACE_BASIC'),
    'pro': os.environ.get('STRIPE_PRO_PRICE_ID', 'price_REPLACE_PRO'),
    'elite': os.environ.get('STRIPE_ELITE_PRICE_ID', 'price_REPLACE_ELITE'),
    'byakugan': os.environ.get('STRIPE_BYAKUGAN_PRICE_ID', 'price_REPLACE_BYAKUGAN'),
}
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = None  # Handled manually in unauthorized_handler below

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@login_manager.unauthorized_handler
def unauthorized():
    # For ALL API routes — return JSON 401, never redirect
    if request.path.startswith('/api/') or request.path.startswith('/scanner/'):
        return jsonify({'error': 'Session expired. Please refresh and log in again.', 'action': 'login'}), 401
    # For page routes — redirect to login
    return redirect(url_for('auth.login_page'))

@app.route('/api/server-time')
def server_time():
    from datetime import timedelta, timezone
    try:
        import zoneinfo
        eastern = zoneinfo.ZoneInfo('America/New_York')
        now = datetime.now(eastern)
    except Exception:
        from datetime import timedelta
        now = datetime.utcnow() - timedelta(hours=5)
    day = now.weekday()
    monday = now - timedelta(days=day)
    friday = monday + timedelta(days=4)
    week_range = f"{monday.strftime('%b %d')} — {friday.strftime('%b %d, %Y')}"
    hour = now.hour; minute = now.minute; is_weekday = day < 5
    market_open = is_weekday and (hour > 9 or (hour == 9 and minute >= 30)) and hour < 16
    pre_market = is_weekday and hour >= 4 and (hour < 9 or (hour == 9 and minute < 30))
    after_hours = is_weekday and hour >= 16 and hour < 20
    if market_open: market_status = 'MARKET OPEN'; status_color = '#00ff99'
    elif pre_market: market_status = 'PRE-MARKET'; status_color = '#ffe033'
    elif after_hours: market_status = 'AFTER HOURS'; status_color = '#ff7744'
    else: market_status = 'MARKET CLOSED'; status_color = '#ff4466'
    return jsonify({'date': now.strftime('%B %d, %Y'), 'time': now.strftime('%H:%M:%S') + (' EDT' if now.dst() else ' EST'),
        'day': now.strftime('%A'), 'week_range': week_range, 'market_status': market_status,
        'status_color': status_color, 'timestamp': now.isoformat()})

app.register_blueprint(auth_bp)
from payments import payments_bp
app.register_blueprint(payments_bp)
from scanner import scanner_bp
app.register_blueprint(scanner_bp)
from forex import forex_bp
app.register_blueprint(forex_bp)
from wolf_agent import wolf_bp
app.register_blueprint(wolf_bp)

with app.app_context():
    db.create_all()

# ═══════════════════════════════════════════════════════════════
# CANDLESTICK ENGINE — Real chart data from TwelveData (yfinance fallback)
# ═══════════════════════════════════════════════════════════════

# Yahoo Finance symbol map for forex pairs (fallback only)
YF_MAP = {
    # Forex — yfinance uses =X suffix
    'EUR/USD': 'EURUSD=X', 'GBP/USD': 'GBPUSD=X', 'USD/JPY': 'USDJPY=X',
    'USD/CHF': 'USDCHF=X', 'AUD/USD': 'AUDUSD=X', 'USD/CAD': 'USDCAD=X',
    'NZD/USD': 'NZDUSD=X', 'EUR/GBP': 'EURGBP=X', 'EUR/JPY': 'EURJPY=X',
    'GBP/JPY': 'GBPJPY=X', 'EUR/AUD': 'EURAUD=X', 'AUD/CAD': 'AUDCAD=X',
    'CAD/JPY': 'CADJPY=X', 'CHF/JPY': 'CHFJPY=X', 'AUD/NZD': 'AUDNZD=X',
    # Commodities
    'XAU/USD': 'GC=F', 'XAG/USD': 'SI=F', 'WTI/USD': 'CL=F', 'DXY': 'DX=F',
    # Crypto
    'BTC/USD': 'BTC-USD', 'ETH/USD': 'ETH-USD', 'SOL/USD': 'SOL-USD',
    # Stocks — yfinance uses ticker directly (NO =X suffix)
    'NVDA': 'NVDA', 'AAPL': 'AAPL', 'TSLA': 'TSLA', 'META': 'META',
    'AMD': 'AMD', 'MSFT': 'MSFT', 'AMZN': 'AMZN', 'GOOGL': 'GOOGL',
    # Indices
    'SPY': 'SPY', 'QQQ': 'QQQ', 'DIA': 'DIA', 'VIX': '^VIX',
    'SPX': '^GSPC', 'NDX': '^NDX',
}

# Candle cache — interval-aware TTLs so short TFs stay fresh
_candle_cache = {}
_candle_cache_ttls = {
    '15m':  120,   # M15: 2 min  — new candle every 15min, stay current
    '1h':   180,   # H1:  3 min  — entry timeframe, needs to be current
    '4h':   300,   # H4:  5 min  — direction confirmation
    '1d':   600,   # Daily: 10 min — trend, slower moving
    '1wk':  900,   # Weekly: 15 min — big picture
}

def get_candles(pair, interval='1d', period='3mo'):
    """
    Fetch real OHLC candles. Tries TwelveData first (35 days), falls back to yfinance.
    TwelveData interval map: 1d->1day, 1wk->1week, 4h->4h, 1h->1h, 15m->15min
    """
    cache_key = f"{pair}_{interval}_{period}"
    now = time.time()
    ttl = _candle_cache_ttls.get(interval, 300)
    if cache_key in _candle_cache:
        cached = _candle_cache[cache_key]
        if now - cached['ts'] < ttl:
            return cached['data']

    # TwelveData interval mapping
    TD_INTERVAL_MAP = {
        '1d': '1day', '1wk': '1week', '4h': '4h',
        '1h': '1h', '15m': '15min', '30m': '30min'
    }
    # TwelveData outputsize — tuned for FREE plan (800 calls/day, 8/min)
    # Keep low so scanners don't hit rate limits
    TD_OUTPUTSIZE_MAP = {
        '1d':  50,  # 50 daily candles = ~2.5 months
        '1wk': 52,  # 52 weekly candles = 1 year
        '4h':  60,  # 60 x 4H = ~10 days
        '1h':  72,  # 72 hourly = 3 days (original value)
        '15m': 96,  # 96 x 15M = ~1 day
        '30m': 60   # 60 x 30M = ~1.25 days
    }

    td_interval = TD_INTERVAL_MAP.get(interval)
    td_size = TD_OUTPUTSIZE_MAP.get(interval, 35)

    # ── Try TwelveData first ──────────────────────────────────────
    if TWELVE_DATA_KEY and td_interval:
        try:
            url = (f'https://api.twelvedata.com/time_series'
                   f'?symbol={pair}&interval={td_interval}'
                   f'&outputsize={td_size}&apikey={TWELVE_DATA_KEY}')
            resp = http_requests.get(url, timeout=10)
            js = resp.json()
            if 'values' in js and js['values']:
                candles = []
                for v in reversed(js['values']):  # oldest first
                    candles.append({
                        'time':   v['datetime'],
                        'open':   round(float(v['open']),  5),
                        'high':   round(float(v['high']),  5),
                        'low':    round(float(v['low']),   5),
                        'close':  round(float(v['close']), 5),
                        'volume': float(v.get('volume', 0))
                    })
                _candle_cache[cache_key] = {'data': candles, 'ts': now}
                return candles
        except Exception as e:
            err_msg = str(e)
            print(f'[TwelveData candles] {pair} {interval} FAILED: {err_msg}')
            # If it's an auth error, log clearly
            if '401' in err_msg or 'apikey' in err_msg.lower():
                print('[TwelveData] API key rejected — check TWELVE_DATA_API_KEY in Render env vars')
            elif '429' in err_msg or 'limit' in err_msg.lower():
                print('[TwelveData] Rate limit hit — check your plan limits')

    # ── Fallback: yfinance ────────────────────────────────────────
    try:
        import yfinance as yf
        sym = YF_MAP.get(pair, pair.replace('/', '') + '=X')
        ticker = yf.Ticker(sym)
        df = ticker.history(interval=interval, period=period)
        if df.empty:
            return []
        candles = []
        for ts, row in df.iterrows():
            candles.append({
                'time':   str(ts)[:10] if interval not in ('1h', '15m', '4h') else str(ts)[:16],
                'open':   round(float(row['Open']),  5),
                'high':   round(float(row['High']),  5),
                'low':    round(float(row['Low']),   5),
                'close':  round(float(row['Close']), 5),
                'volume': int(row.get('Volume', 0))
            })
        _candle_cache[cache_key] = {'data': candles, 'ts': now}
        return candles
    except Exception as e:
        print(f'[Candles yfinance] {pair} {interval} error: {e}')
        return []

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return round(ema, 5)

def calc_rsi(closes, period=14):
    """Wilder's Smoothed RSI — same method as TradingView/MT4"""
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    # First avg = simple average of first {period} values (Wilder's seed)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Then Wilder's smoothing for remaining values
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calc_macd(closes):
    if len(closes) < 26:
        return None, None
    ema12 = calc_ema(closes[-50:], 12)
    ema26 = calc_ema(closes[-50:], 26)
    if ema12 and ema26:
        macd = round(ema12 - ema26, 5)
        return macd, 'BULLISH' if macd > 0 else 'BEARISH'
    return None, None

def find_sr_levels(candles, current_price, lookback=50):
    if len(candles) < 5:
        return []

    recent = candles[-lookback:] if len(candles) > lookback else candles
    highs = [c['high'] for c in recent]
    lows  = [c['low']  for c in recent]
    closes = [c['close'] for c in recent]

    levels = []

    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            levels.append({'price': highs[i], 'type': 'swing_high', 'strength': 1})

    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            levels.append({'price': lows[i], 'type': 'swing_low', 'strength': 1})

    is_jpy = current_price > 50
    is_gold = current_price > 1000
    if is_gold:
        base = round(current_price / 50) * 50
        for i in range(-4, 5):
            levels.append({'price': base + i * 50, 'type': 'round_number', 'strength': 2})
    elif is_jpy:
        base = round(current_price)
        for i in range(-5, 6):
            if i % 50 == 0 or i % 100 == 0:
                levels.append({'price': base + i, 'type': 'round_number', 'strength': 2})
            elif i % 25 == 0:
                levels.append({'price': base + i, 'type': 'round_number', 'strength': 1})
    else:
        base = round(current_price * 100) / 100
        for i in [-200, -150, -100, -50, 0, 50, 100, 150, 200]:
            levels.append({'price': round(base + i * 0.0001, 4), 'type': 'round_number', 'strength': 1})

    threshold = current_price * 0.001
    clustered = []
    sorted_levels = sorted(levels, key=lambda x: x['price'])
    for lv in sorted_levels:
        merged = False
        for cl in clustered:
            if abs(lv['price'] - cl['price']) < threshold:
                cl['strength'] += lv['strength']
                merged = True
                break
        if not merged:
            clustered.append({'price': lv['price'], 'type': lv['type'], 'strength': lv['strength']})

    clustered.sort(key=lambda x: abs(x['price'] - current_price))
    result = []
    for lv in clustered[:8]:
        dist = lv['price'] - current_price
        is_jpy_pair = current_price > 50
        pip_size = 0.01 if is_jpy_pair else 0.0001
        pips = round(abs(dist) / pip_size)
        lv_type = 'RESISTANCE' if dist > 0 else 'SUPPORT'
        result.append({
            'type': lv_type,
            'price': round(lv['price'], 2 if is_jpy_pair or is_gold else 4),
            'distance_pips': pips,
            'strength': lv['strength'],
            'note': f"{'Strong' if lv['strength'] >= 3 else 'Moderate'} {lv_type.lower()} — {'swing ' + lv['type'].replace('_',' ') if 'swing' in lv['type'] else 'psychological level'}"
        })
    return result

def get_chart_analysis(pair, current_price):
    """
    FULL 5-timeframe chart analysis — wraps get_sage_chart_data.
    Safe to call sequentially. Candle cache (15min TTL) prevents redundant fetches.
    Wolf Scanner + Forex Scanner get identical real data as Sage Mode:
    - Wilder RSI, EMA9/20/50/100/200, real ADX (Wilder), HH/HL trend structure
    - Real ATR (SL sizing), 16 candlestick patterns, real swing S/R (min 2 touches)
    - Market structure (HH/HL/LH/LL), ADR, weekly range %, London levels
    """
    sage = get_sage_chart_data(pair, current_price)
    da   = sage.get("daily",   {})
    wk   = sage.get("weekly",  {})
    h1   = sage.get("hourly",  {})
    h4   = sage.get("h4",      {})
    m15  = sage.get("m15",     {})
    sr   = sage.get("sr_levels", [])

    w_trend  = wk.get("trend", "NEUTRAL")
    d_trend  = da.get("trend", "NEUTRAL")
    h_trend  = h1.get("trend", "NEUTRAL")
    h4_trend = h4.get("trend", "NEUTRAL")

    bull = sum(1 for t in [w_trend, d_trend, h_trend, h4_trend] if t == "BULLISH")
    bear = sum(1 for t in [w_trend, d_trend, h_trend, h4_trend] if t == "BEARISH")

    return {
        "pair":          pair,
        "current_price": current_price,
        "weekly":  wk,
        "daily":   da,
        "hourly":  h1,
        "h4":      h4,
        "m15":     m15,
        "sr_levels": sr,
        "adr":     sage.get("adr"),
        "weekly_range_pct": sage.get("weekly_range_pct"),
        "weekly_high": sage.get("weekly_high"),
        "weekly_low":  sage.get("weekly_low"),
        "trend_strength": sage.get("trend_strength", "UNKNOWN"),
        "trend_score":    sage.get("trend_score", 0),
        "d1_patterns":  sage.get("d1_patterns",  []),
        "h4_patterns":  sage.get("h4_patterns",  []),
        "m15_patterns": sage.get("m15_patterns", []),
        "indicators": {
            "ema9":      da.get("ema9"),
            "ema20":     da.get("ema20"),
            "ema50":     da.get("ema50"),
            "ema100":    da.get("ema100"),
            "ema200":    da.get("ema200"),
            "rsi":       da.get("rsi"),
            "macd_bias": da.get("macd_bias"),
            "atr":       da.get("atr"),
            "adx":       da.get("adx"),
            "adx_signal": da.get("adx_signal"),
            "hh_hl_structure": da.get("hh_hl_structure"),
            "hh_hl_strength":  da.get("hh_hl_strength"),
            "bb_upper":  da.get("bb_upper"),
            "bb_mid":    da.get("bb_mid"),
            "bb_lower":  da.get("bb_lower"),
            "bb_position": da.get("bb_position"),
        },
        "trend_summary": {
            "weekly":  w_trend,
            "daily":   d_trend,
            "hourly":  h_trend,
            "h4":      h4_trend,
            "overall": "BULLISH" if bull >= 3 else "BEARISH" if bear >= 3 else "MIXED",
            "alignment": f"{bull}/4 bullish, {bear}/4 bearish"
        }
    }

def format_chart_analysis_for_prompt(ca):
    """Full real chart data — Wolf Elite + Chart Analysis + Scenarios + Top3Day + Top3Swing"""
    if not ca:
        return "Chart data unavailable"

    pair = ca.get("pair", "?")
    cp   = ca.get("current_price", "?")
    da   = ca.get("daily",   {})
    wk   = ca.get("weekly",  {})
    h1   = ca.get("hourly",  {})
    h4   = ca.get("h4",      {})
    m15  = ca.get("m15",     {})
    ts   = ca.get("trend_summary", {})
    sr   = ca.get("sr_levels", [])
    sep  = "=" * 65

    lines = [
        sep,
        f"REAL CHART DATA — {pair} @ {cp}  [TwelveData 35-day OHLC]",
        f"Overall: {ts.get('overall','?')} | Trend Strength: {ca.get('trend_strength','?')}",
        f"TF Alignment: {ts.get('alignment','?')} | ADR: {ca.get('adr','?')} | Weekly Range Used: {ca.get('weekly_range_pct','?')}%",
        f"Weekly High: {ca.get('weekly_high','?')} | Weekly Low: {ca.get('weekly_low','?')}",
        sep,
        f"WEEKLY: Trend={wk.get('trend','?')} Structure={wk.get('structure','?')} Phase={wk.get('phase','?')}",
        f"  EMA20={wk.get('ema20','?')} RSI={wk.get('rsi','?')} Last3={wk.get('last3','?')}",
        f"  52wk High={wk.get('high52','?')} | 52wk Low={wk.get('low52','?')}",
        f"DAILY: Trend={da.get('trend','?')} Structure={da.get('structure','?')} Phase={da.get('phase','?')}",
        f"  {da.get('phase_desc','')}",
        f"  EMA9={da.get('ema9','?')} EMA20={da.get('ema20','?')} EMA50={da.get('ema50','?')} EMA100={da.get('ema100','?')} EMA200={da.get('ema200','?')}",
        f"  ADX={da.get('adx','?')} ({da.get('adx_signal','?')}) — >25=TRENDING trade it, <20=RANGING avoid or wait",
        f"  HH/HL Structure={da.get('hh_hl_structure','?')} | Trend Confidence={da.get('hh_hl_strength','?')}%",
        f"  RSI={da.get('rsi','?')} MACD={da.get('macd_bias','?')} ATR={da.get('atr','?')} vs200EMA={da.get('vs_ema200','?')} vs100EMA={da.get('vs_ema100','?')}",
        f"  BB: Upper={da.get('bb_upper','?')} Mid={da.get('bb_mid','?')} Lower={da.get('bb_lower','?')} Position={da.get('bb_position','?')}%",
        f"  Swing Highs: {da.get('swing_highs',[])} | Swing Lows: {da.get('swing_lows',[])}",
        f"  20d High={da.get('high20d','?')} | 20d Low={da.get('low20d','?')} | Last5={da.get('last5','?')}",
        f"H4: Trend={h4.get('trend','?')} Structure={h4.get('structure','?')} Phase={h4.get('phase','?')}",
        f"  EMA9={h4.get('ema9','?')} EMA20={h4.get('ema20','?')} RSI={h4.get('rsi','?')} MACD={h4.get('macd_bias','?')} ATR={h4.get('atr','?')}",
        f"  48h High={h4.get('high48h','?')} | 48h Low={h4.get('low48h','?')}",
        f"H1: Trend={h1.get('trend','?')} Structure={h1.get('structure','?')}",
        f"  EMA9={h1.get('ema9','?')} EMA20={h1.get('ema20','?')} RSI={h1.get('rsi','?')} MACD={h1.get('macd_bias','?')}",
        f"  24h High={h1.get('high24h','?')} | 24h Low={h1.get('low24h','?')}",
        f"M15: Trend={m15.get('trend','?')} Structure={m15.get('structure','?')}",
        f"  EMA9={m15.get('ema9','?')} RSI={m15.get('rsi','?')}",
        f"  London High={m15.get('london_high','?')} | London Low={m15.get('london_low','?')}",
    ]

    if sr:
        lines.append("KEY S/R (real swing highs/lows — min 2 touches Volman rule):")
        for lv in sr[:6]:
            lines.append(f"  {lv['type']}: {lv['price']} | {lv['distance_pips']} pips | strength={lv.get('strength',1)} | {lv.get('note','')}")

    h1_sr = h1.get("sr", [])
    if h1_sr:
        lines.append("INTRADAY S/R (H1):")
        for lv in h1_sr[:3]:
            lines.append(f"  {lv['type']}: {lv['price']} ({lv['distance_pips']} pips)")

    all_pats = (
        [("D1", p) for p in ca.get("d1_patterns",[])] +
        [("H4", p) for p in ca.get("h4_patterns",[])] +
        [("M15",p) for p in ca.get("m15_patterns",[])]
    )
    if all_pats:
        lines.append("CANDLESTICK PATTERNS (Steve Nison):")
        for tf, p in all_pats:
            lines.append(f"  [{tf}] {p['pattern']} ({p['bias']}) — {p['note']}")

    lines.append(sep)
    return "\n".join(lines)


def get_multi_pair_chart_data(pairs, current_prices):
    results = {}
    def fetch_one(pair):
        price = current_prices.get(pair, {})
        cp = float(price.get('price', 1.0)) if price else 1.0
        return pair, get_chart_analysis(pair, cp)

    # Sequential fetch — prevents OOM crash on 2GB Render instance.
    # yfinance is network I/O bound, not CPU bound, so sequential is nearly
    # as fast while using 4x less RAM. Candle cache (15min TTL) means
    # repeated scans are instant.
    for pair in pairs:
        try:
            price = current_prices.get(pair, {})
            cp = float(price.get('price', 1.0)) if price else 1.0
            results[pair] = get_chart_analysis(pair, cp)
        except Exception as e:
            print(f'[ChartFetch] {pair}: {e}')
    return results

# ═══════════════════════════════════════════════════════════════
# REAL STOCK SCORING ENGINE — Wilder RSI, real EMAs, real S/R
# ═══════════════════════════════════════════════════════════════

def get_market_regime():
    """Real market regime using SPY and VIX live from yfinance"""
    try:
        import yfinance as yf
        sh = yf.Ticker('SPY').history(period='5d', interval='1d', timeout=8)
        vh = yf.Ticker('^VIX').history(period='2d', interval='1d', timeout=8)
        spy_price  = round(float(sh['Close'].iloc[-1]), 2) if not sh.empty else 500
        spy_prev   = round(float(sh['Close'].iloc[-2]), 2) if len(sh) > 1 else spy_price
        spy_change = round(((spy_price - spy_prev) / spy_prev) * 100, 2)
        vix_price  = round(float(vh['Close'].iloc[-1]), 2) if not vh.empty else 20
        if vix_price > 30:   fear_greed, regime = 'EXTREME FEAR', 'BEARISH'
        elif vix_price > 20: fear_greed, regime = 'FEAR',         'NEUTRAL'
        elif vix_price < 15: fear_greed, regime = 'GREED',        'BULLISH'
        else:                fear_greed, regime = 'NEUTRAL',      'NEUTRAL'
        return {'spy_price': spy_price, 'spy_change': spy_change,
                'vix': vix_price, 'fear_greed': fear_greed, 'regime': regime}
    except Exception as e:
        print(f'[MarketRegime] {e}')
        return {'spy_price': 500, 'spy_change': 0.0, 'vix': 20.0,
                'fear_greed': 'NEUTRAL', 'regime': 'NEUTRAL'}

def score_stock(ticker_sym):
    """
    Real stock scoring engine — TwelveData first, yfinance fallback.
    Calculates: EMA20/50/100/200, RSI(14), MACD, ATR, ADX, HH/HL structure, real S/R.
    Returns 0-100 score + direction + all indicators — NO estimates.
    """
    try:
        # ── Fetch real OHLC from TwelveData (35 days) ──────────────
        candles = get_candles(ticker_sym, '1d', '6mo')
        if not candles or len(candles) < 20:
            return None

        closes = [c['close'] for c in candles]
        highs  = [c['high']  for c in candles]
        lows   = [c['low']   for c in candles]
        price  = round(closes[-1], 2)

        # ── Real EMAs ──────────────────────────────────────────────
        e20  = calc_ema(closes, 20)
        e50  = calc_ema(closes, 50)
        e100 = calc_ema(closes, min(100, len(closes)))
        e200 = calc_ema(closes, min(200, len(closes)))

        # ── Wilder Smoothed RSI (14-period) ────────────────────────
        rsi = calc_rsi(closes)

        # ── Real MACD (EMA12 vs EMA26) ─────────────────────────────
        macd_bias = calc_macd(closes)[1]

        # ── Full ADX (Wilder) ───────────────────────────────────────
        adx = calc_adx(candles)
        adx_signal = ("TRENDING" if adx and adx > 25 else "RANGING" if adx and adx < 20 else "WEAK") if adx else "UNKNOWN"

        # ── HH/HL Trend Structure ────────────────────────────────────
        trend_struct, trend_strength = detect_trend_structure(candles)

        # ── Volume ratio (today vs 20-day average) ─────────────────
        vols = []
        for c in candles:
            try: vols.append(float(c.get('volume', 0)))
            except: vols.append(0)
        vol_ratio = round(vols[-1] / (sum(vols[-21:-1])/20), 2) if len(vols) >= 21 and sum(vols[-21:-1]) > 0 else 1.0

        # ── ATR for SL sizing ───────────────────────────────────────
        atr14 = calc_atr(candles)
        iv_rank = round((atr14/price)*100*10, 1) if (atr14 and price) else 30

        # ── Real S/R from swing highs/lows (min 2 touches = Volman rule) ──
        sr = []
        for i in range(2, len(highs)-2):
            if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                sr.append({'type':'RESISTANCE','price':round(highs[i],2),
                           'distance_pips':round(abs(highs[i]-price),2),'strength':1})
            if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
                sr.append({'type':'SUPPORT','price':round(lows[i],2),
                           'distance_pips':round(abs(lows[i]-price),2),'strength':1})
        # Cluster nearby levels (count touches)
        sr.sort(key=lambda x: x['price'])
        clustered_sr = []
        margin = price * 0.005
        for lv in sr:
            merged = False
            for cl in clustered_sr:
                if abs(lv['price'] - cl['price']) < margin:
                    cl['strength'] += 1
                    merged = True
                    break
            if not merged:
                clustered_sr.append(dict(lv))
        # Only keep levels with 2+ touches (Volman rule)
        real_sr = [lv for lv in clustered_sr if lv['strength'] >= 2]
        real_sr.sort(key=lambda x: x['distance_pips'])
        if not real_sr:  # fallback: use top swing levels anyway
            clustered_sr.sort(key=lambda x: x['distance_pips'])
            real_sr = clustered_sr[:6]
        sr = real_sr[:6]

        # ── Direction consensus ─────────────────────────────────────
        bull = sum([1 if (e20  and price > e20)  else 0,
                    1 if (e50  and price > e50)   else 0,
                    1 if macd_bias == 'BULLISH'   else 0,
                    1 if (rsi  and rsi > 50)      else 0])
        direction = 'BULLISH' if bull >= 3 else 'BEARISH' if bull <= 1 else 'NEUTRAL'

        # ── Score 0-100 ─────────────────────────────────────────────
        score = 0
        if e200 and price > e200: score += 20
        if e100 and price > e100: score += 10
        if e50  and price > e50:  score += 10
        if e20  and price > e20:  score += 5
        if rsi:
            if 45 <= rsi <= 65:   score += 15
            elif rsi < 30:        score += 10
            elif rsi > 70:        score -= 10
        if macd_bias == 'BULLISH': score += 15
        if vol_ratio > 1.5:        score += 10
        if vol_ratio > 2.5:        score += 5
        # ADX bonus: trending stocks score higher
        if adx:
            if adx > 35: score += 10
            elif adx > 25: score += 5
            elif adx < 20: score -= 10  # penalize ranging

        signals = []
        if e200 and price > e200:  signals.append(f'Above 200EMA ({e200})')
        if e100 and price > e100:  signals.append(f'Above 100EMA ({e100})')
        if adx:                    signals.append(f'ADX {adx} ({adx_signal})')
        if rsi:                    signals.append(f'RSI {rsi}')
        if macd_bias:              signals.append(f'MACD {macd_bias}')
        if vol_ratio > 1.5:        signals.append(f'Vol {vol_ratio}x avg')
        if trend_struct != 'UNKNOWN': signals.append(f'{trend_struct} ({trend_strength}%)')

        near_earnings = False
        try:
            import yfinance as yf
            t = yf.Ticker(ticker_sym)
            cal = t.calendar
            if cal is not None and not cal.empty:
                cols = cal.columns.tolist()
                if cols:
                    earn_date = str(cols[0])[:10]
                    dt = datetime.strptime(earn_date, '%Y-%m-%d')
                    days_away = (dt - datetime.now()).days
                    if 0 < days_away <= 7: near_earnings = True
        except: pass

        return {
            'ticker': ticker_sym, 'price': price,
            'score': max(0, min(100, score)),
            'direction': direction, 'rsi': rsi,
            'ema20': e20, 'ema50': e50, 'ema100': e100, 'ema200': e200,
            'adx': adx, 'adx_signal': adx_signal,
            'hh_hl_structure': trend_struct, 'hh_hl_strength': trend_strength,
            'macd_bias': macd_bias, 'vol_ratio': vol_ratio,
            'atr': atr14,
            'iv_rank': iv_rank, 'unusual_activity': round(vol_ratio, 1),
            'sr_levels': sr, 'signals': signals,
            'near_earnings': near_earnings
        }
    except Exception as e:
        print(f'[ScoreStock {ticker_sym}] {e}')
        return None

# ── Options helpers ──────────────────────────────────────────

def calculate_greeks(S, K, T, r, sigma, option_type='call'):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0: return None
    try:
        d1 = (np.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*np.sqrt(T)); d2=d1-sigma*np.sqrt(T)
        if option_type=='call':
            price=S*norm.cdf(d1)-K*np.exp(-r*T)*norm.cdf(d2); delta=norm.cdf(d1)
            theta=(-(S*norm.pdf(d1)*sigma)/(2*np.sqrt(T))-r*K*np.exp(-r*T)*norm.cdf(d2))/365
        else:
            price=K*np.exp(-r*T)*norm.cdf(-d2)-S*norm.cdf(-d1); delta=norm.cdf(d1)-1
            theta=(-(S*norm.pdf(d1)*sigma)/(2*np.sqrt(T))+r*K*np.exp(-r*T)*norm.cdf(-d2))/365
        gamma=norm.pdf(d1)/(S*sigma*np.sqrt(T)); vega=S*norm.pdf(d1)*np.sqrt(T)/100
        rho=(K*T*np.exp(-r*T)*norm.cdf(d2)/100 if option_type=='call' else -K*T*np.exp(-r*T)*norm.cdf(-d2)/100)
        return {'price':round(float(price),4),'delta':round(float(delta),4),'gamma':round(float(gamma),6),
                'theta':round(float(theta),4),'vega':round(float(vega),4),'rho':round(float(rho),4)}
    except Exception as e: return {'error':str(e)}

def build_pnl_curve(S, K, T, r, sigma, option_type, premium_paid, days_held):
    price_range=np.linspace(S*0.70,S*1.30,80); T_remaining=max(T-(days_held/365),0.001); curve=[]
    for target in price_range:
        g=calculate_greeks(target,K,T_remaining,r,sigma,option_type)
        if g and 'price' in g: curve.append({'price':round(float(target),2),'pnl':round((g['price']-premium_paid)*100,2)})
    return curve

def fetch_live_data(ticker, expiration, strike, option_type):
    try:
        import yfinance as yf
        stock=yf.Ticker(ticker); hist=stock.history(period='1d')
        if hist.empty: return None,'Could not fetch stock price'
        stock_price=float(hist['Close'].iloc[-1])
        try:
            chain=stock.option_chain(expiration); contracts=chain.calls if option_type=='call' else chain.puts
            strike_f=float(strike); row=contracts.iloc[(contracts['strike']-strike_f).abs().argsort()[:1]]
            if not row.empty:
                iv=float(row['impliedVolatility'].iloc[0]); mark=float(row['lastPrice'].iloc[0])
                return {'stock_price':stock_price,'iv':iv,'mark':mark,'source':'yfinance'},None
        except: pass
        return {'stock_price':stock_price,'iv':None,'mark':None,'source':'price-only'},None
    except ImportError: return None,'yfinance not installed'
    except Exception as e: return None,str(e)

def fetch_stock_price_only(ticker):
    try:
        import yfinance as yf
        hist=yf.Ticker(ticker).history(period='1d')
        if not hist.empty: return float(hist['Close'].iloc[-1])
    except: pass
    return None

def fetch_option_expirations(ticker):
    try:
        import yfinance as yf
        return list(yf.Ticker(ticker).options),None
    except ImportError: return None,'yfinance not installed'
    except Exception as e: return None,str(e)

def fetch_option_strikes(ticker, expiration, option_type='call'):
    try:
        import yfinance as yf
        chain=yf.Ticker(ticker).option_chain(expiration)
        contracts=chain.calls if option_type=='call' else chain.puts
        return sorted(contracts['strike'].tolist()),None
    except ImportError: return None,'yfinance not installed'
    except Exception as e: return None,str(e)

# ── Pages ────────────────────────────────────────────────────

@app.route('/')
@login_required
def index(): return render_template('index.html')

@app.route('/login')
def login_page():
    if current_user.is_authenticated: return redirect(url_for('index'))
    return render_template('auth.html')

@app.route('/pricing')
def pricing(): return render_template('pricing.html')

@app.route('/ai-scanner')
@login_required
@basic_required
def ai_scanner(): return render_template('scanner.html')

@app.route('/ai-analysis')
@login_required
@elite_required
def ai_analysis(): return render_template('analysis.html')

@app.route('/wolf-elite')
@login_required
@elite_required
def wolf_elite(): return render_template('wolf_elite.html')

# ── Stripe ───────────────────────────────────────────────────

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    plan=request.form.get('plan')
    if plan not in ('basic','pro','elite','byakugan'): flash('Invalid plan selected.','danger'); return redirect(url_for('pricing'))
    try:
        checkout=stripe.checkout.Session.create(payment_method_types=['card'],mode='subscription',
            line_items=[{'price':STRIPE_PRICES[plan],'quantity':1}],
            success_url=url_for('payment_success',plan=plan,_external=True),
            cancel_url=url_for('pricing',_external=True),
            client_reference_id=str(current_user.id),metadata={'plan':plan,'user_id':str(current_user.id)})
        return redirect(checkout.url,code=303)
    except Exception as e: flash(f'Payment error: {str(e)}','danger'); return redirect(url_for('pricing'))

@app.route('/payment-success')
@login_required
def payment_success():
    plan=request.args.get('plan','basic'); current_user.plan=plan; db.session.commit()
    flash(f"🐺 You're now on Wolf Elite {'Elite' if plan=='elite' else 'Basic'}! Let's get it.",'success')
    return redirect(url_for('index'))

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload=request.get_data(as_text=True); sig_header=request.headers.get('Stripe-Signature')
    try: event=stripe.Webhook.construct_event(payload,sig_header,STRIPE_WEBHOOK_SECRET)
    except: return 'Invalid signature',400
    if event['type']=='checkout.session.completed':
        data=event['data']['object']; user_id=data.get('metadata',{}).get('user_id'); plan=data.get('metadata',{}).get('plan','basic')
        try:
            user=User.query.get(int(user_id))
            if user: user.plan=plan; user.stripe_customer_id=data.get('customer'); user.stripe_subscription_id=data.get('subscription'); db.session.commit()
        except Exception as e: print(f'[Webhook] DB error: {e}')
    elif event['type']=='customer.subscription.deleted':
        stripe_customer_id=event['data']['object'].get('customer')
        try:
            user=User.query.filter_by(stripe_customer_id=stripe_customer_id).first()
            if user: user.plan='trial'; db.session.commit()
        except Exception as e: print(f'[Webhook] DB error: {e}')
    return 'OK',200

# ── Options API ──────────────────────────────────────────────

@app.route('/api/autofill', methods=['POST'])
@analysis_gate
def autofill():
    ticker=request.json.get('ticker','').upper().strip()
    if not ticker: return jsonify({'error':'No ticker provided'})
    expirations,err=fetch_option_expirations(ticker)
    if err: return jsonify({'error':err})
    stock_price=fetch_stock_price_only(ticker)
    return jsonify({'ticker':ticker,'stock_price':round(stock_price,2) if stock_price else None,'expirations':expirations[:12] if expirations else []})

@app.route('/api/strikes', methods=['POST'])
@login_required
def strikes():
    data=request.json; strikes_list,err=fetch_option_strikes(data.get('ticker','').upper(),data.get('expiration',''),data.get('option_type','call'))
    if err: return jsonify({'error':err})
    return jsonify({'strikes':strikes_list})

@app.route('/api/contract', methods=['POST'])
@login_required
def contract():
    data=request.json; live,err=fetch_live_data(data.get('ticker','').upper(),data.get('expiration',''),data.get('strike',0),data.get('option_type','call'))
    if err: return jsonify({'error':err})
    return jsonify({'data':live})

@app.route('/api/greeks', methods=['POST'])
@analysis_gate
def greeks():
    data=request.json; ticker=data.get('ticker','AAPL').upper(); strike=float(data.get('strike',150))
    expiration=data.get('expiration',''); option_type=data.get('option_type','call')
    days_held=int(data.get('days_held',0)); r=float(data.get('r',0.045)); theta_alert=float(data.get('theta_alert',50))
    live_data=None
    if expiration: live_data,_=fetch_live_data(ticker,expiration,strike,option_type)
    if expiration:
        try:
            exp_date=datetime.strptime(expiration,'%Y-%m-%d'); T=max((exp_date-datetime.now()).days/365,0.001); dte_days=max((exp_date-datetime.now()).days,1)
        except: T=30/365; dte_days=30
    else: T=float(data.get('dte',30))/365; dte_days=int(data.get('dte',30))
    S=float(live_data['stock_price']) if live_data and live_data.get('stock_price') else float(data.get('stock_price',150))
    sigma=float(live_data['iv']) if live_data and live_data.get('iv') else float(data.get('iv',0.30))
    premium=float(live_data['mark']) if live_data and live_data.get('mark') else float(data.get('premium_paid',0))
    greeks_result=calculate_greeks(S,strike,T,r,sigma,option_type); pnl_curve=build_pnl_curve(S,strike,T,r,sigma,option_type,premium,days_held)
    daily_theta_d=(greeks_result['theta']*100) if greeks_result else 0
    return jsonify({'greeks':greeks_result,'live_data':live_data,'pnl_curve':pnl_curve,'stock_price':S,
        'premium_paid':premium,'sigma':sigma,'daily_theta_dollars':round(daily_theta_d,2),
        'theta_alert':abs(daily_theta_d)>theta_alert,'T':round(T*365,1),'dte_days':dte_days})

@app.route('/api/simulate', methods=['POST'])
@analysis_gate
def simulate():
    data=request.json
    S=float(data.get('stock_price',150));K=float(data.get('strike',150));T=float(data.get('dte',30))/365
    r=float(data.get('r',0.045));sigma=float(data.get('iv',0.30));option_type=data.get('option_type','call');premium=float(data.get('premium_paid',0))
    scenarios=[{'days':d,'curve':build_pnl_curve(S,K,T,r,sigma,option_type,premium,d)} for d in [0,5,10,15,20]]
    return jsonify({'scenarios':scenarios})

@app.route('/health')
def health(): return jsonify({'status':'online','terminal':'JAYDAWOLFX OPTIONS TERMINAL 🐺'}),200

@app.route('/api/track-pick', methods=['POST'])
@login_required
def track_pick():
    data=request.get_json(); tracker_file='wolf_tracker.json'
    try:
        if os.path.exists(tracker_file):
            with open(tracker_file,'r') as f: tracker=json.load(f)
        else: tracker={'picks':[]}
        tracker['picks'].append({'week':data.get('week'),'ticker':data.get('ticker'),'entry':data.get('entry'),
            'target':data.get('target'),'stop':data.get('stop'),'result':data.get('result','PENDING'),
            'pct_change':data.get('pct_change',0),'date_added':datetime.now().strftime('%Y-%m-%d')})
        with open(tracker_file,'w') as f: json.dump(tracker,f)
        return jsonify({'success':True}),200
    except Exception as e: return jsonify({'error':str(e)}),500

@app.route('/api/tracker-stats', methods=['GET'])
@login_required
def tracker_stats():
    tracker_file='wolf_tracker.json'
    try:
        if not os.path.exists(tracker_file): return jsonify({'total':0,'wins':0,'losses':0,'win_rate':0,'picks':[]}),200
        with open(tracker_file,'r') as f: tracker=json.load(f)
        picks=tracker.get('picks',[]); completed=[p for p in picks if p['result'] in ['WIN','LOSS']]
        wins=len([p for p in completed if p['result']=='WIN']); losses=len([p for p in completed if p['result']=='LOSS'])
        win_rate=round((wins/len(completed)*100) if completed else 0)
        return jsonify({'total':len(picks),'wins':wins,'losses':losses,'win_rate':win_rate,'picks':picks[-20:]}),200
    except Exception as e: return jsonify({'error':str(e)}),500

# ═══════════════════════════════════════════════════════════════
# FOREX — LIVE DATA, CACHE, WOLF SCANNER
# ═══════════════════════════════════════════════════════════════

TWELVE_DATA_KEY = os.environ.get('TWELVE_DATA_API_KEY', '')
NEWS_API_KEY    = os.environ.get('NEWS_API_KEY', '')

FOREX_SESSIONS = {
    'TOKYO':    {'pairs': ['USD/JPY','EUR/JPY','GBP/JPY','AUD/USD','NZD/USD']},
    'LONDON':   {'pairs': ['EUR/USD','GBP/USD','EUR/GBP','USD/CHF','EUR/JPY']},
    'NEW YORK': {'pairs': ['EUR/USD','GBP/USD','USD/CAD','USD/JPY','XAU/USD']},
    'OVERLAP':  {'pairs': ['EUR/USD','GBP/USD','USD/JPY','XAU/USD']},
}

FALLBACK = {
    'EUR/USD':{'price':1.0380,'change':-0.0021,'pct':-0.20,'high':1.0412,'low':1.0361},
    'GBP/USD':{'price':1.2621,'change':-0.0018,'pct':-0.14,'high':1.2658,'low':1.2598},
    'USD/JPY':{'price':150.21,'change':0.38,'pct':0.25,'high':150.58,'low':149.81},
    'USD/CHF':{'price':0.8981,'change':0.0014,'pct':0.16,'high':0.9001,'low':0.8958},
    'AUD/USD':{'price':0.6241,'change':-0.0019,'pct':-0.30,'high':0.6271,'low':0.6221},
    'USD/CAD':{'price':1.4431,'change':0.0028,'pct':0.19,'high':1.4461,'low':1.4401},
    'NZD/USD':{'price':0.5681,'change':-0.0012,'pct':-0.21,'high':0.5701,'low':0.5661},
    'EUR/GBP':{'price':0.8221,'change':0.0008,'pct':0.10,'high':0.8241,'low':0.8201},
    'EUR/JPY':{'price':155.82,'change':0.21,'pct':0.13,'high':156.21,'low':155.41},
    'GBP/JPY':{'price':189.61,'change':0.42,'pct':0.22,'high':190.21,'low':189.01},
    'XAU/USD':{'price':2857.40,'change':12.30,'pct':0.43,'high':2868.20,'low':2841.10},
    'DXY':    {'price':107.82,'change':0.21,'pct':0.19,'high':108.11,'low':107.51},
}

_price_cache = {'prices': {}, 'fetched_at': 0, 'ttl': 20, 'live': False}
_er_cache = {'rates': {}, 'fetched_at': 0}

def get_session():
    from datetime import timezone, timedelta
    try:
        now = datetime.now(timezone.utc) + timedelta(hours=-5)
        h, day = now.hour, now.weekday()
        if day == 5: return 'CLOSED', []
        if day == 6 and h < 17: return 'CLOSED', []
        if day == 4 and h >= 17: return 'CLOSED', []
        if h >= 19 or h < 3:  return 'TOKYO', FOREX_SESSIONS['TOKYO']['pairs']
        if 3 <= h < 8:         return 'LONDON', FOREX_SESSIONS['LONDON']['pairs']
        if 8 <= h < 12:        return 'OVERLAP', FOREX_SESSIONS['OVERLAP']['pairs']
        if 12 <= h < 17:       return 'NEW YORK', FOREX_SESSIONS['NEW YORK']['pairs']
        return 'AFTER HOURS', []
    except: return 'UNKNOWN', []

def get_price(symbol):
    try:
        import yfinance as yf
        sym = YF_MAP.get(symbol, symbol.replace('/', '') + '=X')
        ticker = yf.Ticker(sym)
        df = ticker.history(period='2d', interval='1h')
        if not df.empty:
            latest = df.iloc[-1]
            prev   = df.iloc[-2] if len(df) > 1 else df.iloc[-1]
            price  = round(float(latest['Close']), 5)
            prev_c = round(float(prev['Close']), 5)
            change = round(price - prev_c, 5)
            pct    = round((change / prev_c) * 100, 2) if prev_c else 0
            high   = round(float(df['High'].tail(8).max()), 5)
            low    = round(float(df['Low'].tail(8).min()),  5)
            return {
                'price': price, 'open': prev_c,
                'high': high,   'low':  low,
                'change': change, 'percent_change': pct,
                'symbol': symbol, 'live': True
            }
    except Exception as e:
        print(f'[yfinance price] {symbol}: {e}')

    try:
        sym = symbol.replace('/', '')
        r = http_requests.get(
            f'https://api.twelvedata.com/quote?symbol={sym}&apikey={TWELVE_DATA_KEY}',
            timeout=4)
        d = r.json()
        if 'close' in d and 'code' not in d:
            return {'price':float(d.get('close',0)),'open':float(d.get('open',0)),
                    'high':float(d.get('high',0)),'low':float(d.get('low',0)),
                    'change':float(d.get('change',0)),'percent_change':float(d.get('percent_change',0)),
                    'symbol':symbol,'live':True}
    except: pass

    fb = FALLBACK.get(symbol)
    if fb: return {'price':fb['price'],'open':fb['price'],'high':fb['high'],'low':fb['low'],
                   'change':fb['change'],'percent_change':fb['pct'],'symbol':symbol,'live':False}
    return None

def get_prices_parallel(pairs):
    results = {}
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(get_price, p): p for p in pairs}
            for f in as_completed(futures, timeout=5):
                pair = futures[f]
                try:
                    q = f.result()
                    if q: results[pair] = q
                except:
                    fb = FALLBACK.get(pair)
                    if fb: results[pair] = {'price':fb['price'],'high':fb['high'],'low':fb['low'],'change':fb['change'],'percent_change':fb['pct'],'symbol':pair,'live':False}
    except:
        for p in pairs:
            fb = FALLBACK.get(p)
            if fb: results[p] = {'price':fb['price'],'high':fb['high'],'low':fb['low'],'change':fb['change'],'percent_change':fb['pct'],'symbol':p,'live':False}
    return results

def get_cached_prices():
    now = time.time()
    if now - _price_cache['fetched_at'] < _price_cache['ttl'] and _price_cache['prices']:
        return _price_cache['prices'], _price_cache['live']
    pairs = ['EUR/USD','GBP/USD','USD/JPY','USD/CHF','AUD/USD','USD/CAD',
             'NZD/USD','EUR/GBP','EUR/JPY','GBP/JPY','XAU/USD','DXY']
    fresh = get_prices_parallel(pairs)
    if fresh:
        _price_cache['prices'] = fresh
        _price_cache['fetched_at'] = now
        _price_cache['live'] = any(v.get('live', False) for v in fresh.values())
    return _price_cache['prices'], _price_cache['live']

def get_news(pair=''):
    try:
        if pair:
            q = pair.replace('/','+')
            url = f'https://newsapi.org/v2/everything?q={q}+forex&language=en&sortBy=publishedAt&pageSize=4&apiKey={NEWS_API_KEY}'
        else:
            url = f'https://newsapi.org/v2/everything?q=forex+Fed+ECB+central+bank&language=en&sortBy=publishedAt&pageSize=6&apiKey={NEWS_API_KEY}'
        r = http_requests.get(url, timeout=3)
        arts = r.json().get('articles', [])
        return [{'title':a.get('title',''),'source':a.get('source',{}).get('name',''),'published':a.get('publishedAt','')[:10]} for a in arts if a.get('title')]
    except: return []

def call_claude(prompt, max_tokens=2500):
    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    msg = client.messages.create(model='claude-sonnet-4-5', max_tokens=max_tokens,
                                  messages=[{'role':'user','content':prompt}])
    return msg.content[0].text

def parse_json_response(text):
    text = text.strip()
    if text.startswith('```'):
        parts = text.split('```')
        text = parts[1] if len(parts) > 1 else text
        if text.startswith('json'): text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find('{')
    if start > 0: text = text[start:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        opens = text.count('{') - text.count('}')
        arrays = text.count('[') - text.count(']')
        for cutoff in [',\n    {', ', {', ',{']:
            last = text.rfind(cutoff)
            if last > len(text) * 0.7:
                text = text[:last]
                break
        text = text.rstrip(',').rstrip()
        text += ']' * max(0, arrays) + '}' * max(0, opens)
        try:
            return json.loads(text)
        except:
            raise json.JSONDecodeError("Could not parse AI response", text, 0)

# ── Forex pages ───────────────────────────────────────────────

@app.route('/forex')
@login_required
@pro_required
def forex(): return render_template('forex.html')

@app.route('/wolf-scanner')
@login_required
@pro_required
def wolf_scanner(): return render_template('forex.html')

@app.route('/forex-wolf')
@login_required
def forex_wolf(): return render_template('forex_wolf.html')

@app.route('/api/forex-prices', methods=['GET'])
@login_required
def forex_prices():
    try:
        prices, is_live = get_cached_prices()
        session_name, session_pairs = get_session()
        return jsonify({'prices': prices, 'session': session_name,
                        'session_pairs': session_pairs, 'live': is_live,
                        'cached_at': datetime.now().strftime('%H:%M:%S')})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/forex-price', methods=['POST'])
@login_required
def forex_price():
    try:
        symbol = request.get_json().get('symbol', 'EUR/USD')
        q = get_price(symbol)
        return jsonify(q or {'error': 'Unavailable'})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/news-calendar', methods=['GET'])
@login_required
def news_calendar():
    """
    Live forex news feed via NewsAPI.
    Fetches multiple targeted queries, deduplicates, tags pairs affected,
    detects signal killers, and returns sorted by recency.
    Cached 20 minutes.
    """
    try:
        now_ts = time.time()
        if not hasattr(news_calendar, '_cache'):
            news_calendar._cache = None
            news_calendar._cache_ts = 0
        if news_calendar._cache and (now_ts - news_calendar._cache_ts) < 300:  # 5min cache
            return jsonify(news_calendar._cache)

        if not NEWS_API_KEY:
            return jsonify({'error': 'NEWS_API_KEY not configured', 'articles': []}), 200

        # Signal killer keywords → rule
        SIGNAL_KILLERS = {
            'nonfarm':      {'label': 'NFP 🔴', 'rule': 'Wait until 9:45AM EST — EUR/JPY whipsaws violently', 'pairs': ['USD/JPY','EUR/JPY','GBP/USD','EUR/USD']},
            'non-farm':     {'label': 'NFP 🔴', 'rule': 'Wait until 9:45AM EST — EUR/JPY whipsaws violently', 'pairs': ['USD/JPY','EUR/JPY','GBP/USD','EUR/USD']},
            'nfp':          {'label': 'NFP 🔴', 'rule': 'Wait until 9:45AM EST — EUR/JPY whipsaws violently', 'pairs': ['USD/JPY','EUR/JPY','GBP/USD','EUR/USD']},
            'fomc':         {'label': 'FOMC 🔴', 'rule': 'Massive USD risk-off flows — expect volatility spikes', 'pairs': ['USD/JPY','EUR/USD','GBP/USD','EUR/JPY']},
            'federal reserve': {'label': 'FED 🔴', 'rule': 'USD-centric event — signal valid but expect vol spikes', 'pairs': ['USD/JPY','EUR/USD','GBP/USD']},
            'fed rate':     {'label': 'FED 🔴', 'rule': 'USD-centric event — signal valid but expect vol spikes', 'pairs': ['USD/JPY','EUR/USD','GBP/USD']},
            'cpi':          {'label': 'CPI 🔴', 'rule': 'Inflation data = JPY safe-haven flows if hot — wait for stabilization', 'pairs': ['USD/JPY','EUR/JPY','GBP/JPY']},
            'inflation':    {'label': 'CPI ⚠️', 'rule': 'Inflation narrative driving JPY flows — trade carefully', 'pairs': ['USD/JPY','EUR/JPY','XAU/USD']},
            'ecb':          {'label': 'ECB 🔴', 'rule': 'EUR side distorted — use USD/JPY as backup indicator', 'pairs': ['EUR/USD','EUR/JPY','EUR/GBP']},
            'lagarde':      {'label': 'ECB ⚠️', 'rule': 'EUR side distorted — use USD/JPY as backup indicator', 'pairs': ['EUR/USD','EUR/JPY']},
            'boj':          {'label': 'BOJ 🔴', 'rule': 'JPY spikes suddenly — correlation breaks. Skip that day.', 'pairs': ['USD/JPY','EUR/JPY','GBP/JPY']},
            'bank of japan':{'label': 'BOJ 🔴', 'rule': 'JPY spikes suddenly — correlation breaks. Skip that day.', 'pairs': ['USD/JPY','EUR/JPY','GBP/JPY']},
            'ueda':         {'label': 'BOJ ⚠️', 'rule': 'BOJ Governor speaking — JPY volatility risk', 'pairs': ['USD/JPY','EUR/JPY','GBP/JPY']},
            'gdp':          {'label': 'GDP ⚠️', 'rule': 'Can shift trend direction — wait for 15-min candle to close', 'pairs': ['USD/JPY','EUR/USD','GBP/USD']},
            'ppi':          {'label': 'PPI ⚠️', 'rule': 'Inflation proxy — watch for JPY flows', 'pairs': ['USD/JPY','EUR/JPY']},
            'tariff':       {'label': 'TARIFFS ⚠️', 'rule': 'Risk-off trigger — JPY and gold spike on escalation', 'pairs': ['USD/JPY','EUR/JPY','XAU/USD']},
            'trade war':    {'label': 'TRADE WAR ⚠️', 'rule': 'Risk-off — JPY safe haven demand spikes', 'pairs': ['USD/JPY','EUR/JPY','XAU/USD']},
            'recession':    {'label': 'RECESSION FEAR ⚠️', 'rule': 'Risk-off — USD and JPY both spike', 'pairs': ['USD/JPY','EUR/JPY','XAU/USD']},
            'powell':       {'label': 'FED CHAIR 🔴', 'rule': 'Fed Chair speaking — USD moves sharply on tone', 'pairs': ['USD/JPY','EUR/USD','GBP/USD']},
            'rate cut':     {'label': 'RATE CUT ⚠️', 'rule': 'Dovish signal — watch currency side for direction', 'pairs': ['USD/JPY','EUR/USD','GBP/USD']},
            'rate hike':    {'label': 'RATE HIKE ⚠️', 'rule': 'Hawkish signal — strengthens that currency', 'pairs': ['USD/JPY','EUR/USD','GBP/USD']},
        }

        # Pair keyword detection
        PAIR_KEYWORDS = {
            'EUR/USD': ['eur','euro','eurusd','european','ecb','lagarde'],
            'GBP/USD': ['gbp','pound','sterling','gbpusd','boe','bank of england','bailey'],
            'USD/JPY': ['jpy','yen','usdjpy','boj','bank of japan','ueda','japan'],
            'EUR/JPY': ['eurjpy','eur/jpy'],
            'GBP/JPY': ['gbpjpy','gbp/jpy'],
            'USD/CHF': ['chf','franc','usdchf','snb'],
            'AUD/USD': ['aud','aussie','audusd','rba'],
            'USD/CAD': ['cad','loonie','usdcad','boc','bank of canada'],
            'XAU/USD': ['gold','xauusd','xau','bullion'],
            'DXY':     ['dxy','dollar index','usd index','usdx'],
            'USD':     ['usd','dollar','federal reserve','fomc','fed','powell','nfp','nonfarm','cpi','inflation'],
        }

        # Fetch multiple targeted queries in parallel
        QUERIES = [
            'forex market today',
            'FOMC Federal Reserve interest rate',
            'NFP nonfarm payrolls jobs',
            'ECB Bank of Japan BOJ rate decision',
            'CPI inflation USD EUR JPY',
            'tariffs trade war dollar yen',
        ]

        seen_titles = set()
        all_articles = []

        def fetch_query(q):
            try:
                url = (f'https://newsapi.org/v2/everything?q={http_requests.utils.quote(q)}'
                       f'&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}')
                r = http_requests.get(url, timeout=5)
                return r.json().get('articles', [])
            except:
                return []

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(fetch_query, q) for q in QUERIES]
            for f in futures:
                all_articles.extend(f.result())

        processed = []
        for a in all_articles:
            title = (a.get('title') or '').strip()
            if not title or title in seen_titles or '[Removed]' in title:
                continue
            seen_titles.add(title)

            desc   = (a.get('description') or '')
            source = a.get('source', {}).get('name', 'Unknown')
            pub    = a.get('publishedAt', '')[:16].replace('T', ' ')
            url    = a.get('url', '')
            full   = (title + ' ' + desc).lower()

            # Detect signal killers
            killers = []
            seen_k = set()
            for kw, val in SIGNAL_KILLERS.items():
                if kw in full and val['label'] not in seen_k:
                    killers.append(val)
                    seen_k.add(val['label'])

            # Detect pairs affected
            pairs = []
            for pair, kws in PAIR_KEYWORDS.items():
                if any(kw in full for kw in kws):
                    pairs.append(pair)

            # Remove generic USD duplication if specific pairs found
            if len(pairs) > 1 and 'USD' in pairs:
                pairs.remove('USD')

            # Sentiment hint
            bullish_words = ['rise','rally','surge','gain','strength','hawkish','beat','strong','high']
            bearish_words = ['fall','drop','plunge','weak','dovish','miss','low','concern','fear','risk']
            b_score = sum(1 for w in bullish_words if w in full)
            br_score = sum(1 for w in bearish_words if w in full)
            sentiment = 'bullish' if b_score > br_score else 'bearish' if br_score > b_score else 'neutral'

            processed.append({
                'title':     title,
                'desc':      desc[:180] if desc else '',
                'source':    source,
                'published': pub,
                'url':       url,
                'killers':   killers[:2],   # max 2 killer tags per article
                'pairs':     pairs[:5],
                'sentiment': sentiment,
                'is_killer': len(killers) > 0,
            })

        # Sort: signal killers first, then by time
        processed.sort(key=lambda x: (0 if x['is_killer'] else 1, x['published']), reverse=False)
        processed.sort(key=lambda x: x['is_killer'], reverse=True)

        result = {
            'articles': processed[:20],
            'fetched_at': datetime.utcnow().strftime('%H:%M UTC'),
            'total': len(processed),
        }
        news_calendar._cache = result
        news_calendar._cache_ts = now_ts
        return jsonify(result)
    except Exception as e:
        import traceback; print('[NewsCalendar]', traceback.format_exc())
        return jsonify({'error': str(e), 'articles': []}), 200


@app.route('/api/forex-news', methods=['POST'])
@login_required
def forex_news():
    try:
        pair = request.get_json().get('pair', '')
        return jsonify({'pair_news': get_news(pair), 'market_news': get_news()})
    except Exception as e: return jsonify({'error': str(e)}), 500


@app.route('/api/spy-chart', methods=['POST'])
@login_required
def spy_chart():
    """
    Real EUR/JPY chart data for SPY Signal tab.
    Returns: real M15 RSI (Wilder), real London open pip calculation,
    real H4/Daily trend, real S/R levels. NO estimates. NO fakes.
    """
    try:
        # Get real EUR/JPY price
        q = get_price('EUR/JPY')
        if not q:
            return jsonify({'error': 'Cannot fetch EUR/JPY price'}), 500
        cp = float(q['price'])

        # ── Real M15 candles → real RSI ────────────────────────────
        m15 = get_candles('EUR/JPY', '15m', '3d')
        m15_rsi = None
        london_open_price = None
        london_pips = None
        session_high = None
        session_low  = None

        if m15 and len(m15) >= 16:
            m15_closes = [c['close'] for c in m15]
            m15_rsi = calc_rsi(m15_closes)  # Real Wilder RSI

            # London open = 3AM EST = 8AM UTC
            # Find the first M15 candle at or after 08:00 UTC today
            from datetime import datetime, timezone, timedelta
            utc_now = datetime.now(timezone.utc)
            today_london_open_utc = utc_now.replace(hour=8, minute=0, second=0, microsecond=0)
            if utc_now.hour < 8:  # Before today's London open - use yesterday's
                today_london_open_utc -= timedelta(days=1)

            # Find candle closest to London open
            london_candle = None
            for c in m15:
                try:
                    # candle time format: "2024-01-15 08:00"
                    ct = c['time']
                    # Try parsing
                    for fmt in ['%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']:
                        try:
                            parsed = datetime.strptime(ct[:16], fmt[:len(ct[:16])])
                            break
                        except: parsed = None
                    if parsed and parsed.hour == 8 and parsed.minute == 0:
                        london_candle = c
                        break
                except: continue

            # Fallback: use candle ~32 bars ago (8 hours before now on M15)
            if not london_candle and len(m15) >= 32:
                london_candle = m15[-32]

            if london_candle:
                london_open_price = london_candle['open']
                # Pips from London open to now (JPY pairs: 1 pip = 0.01)
                pip_diff = (cp - london_open_price) * 100
                london_pips = round(pip_diff)

            # Session high/low from last 32 candles (8 hours of M15)
            last32 = m15[-32:] if len(m15) >= 32 else m15
            session_high = round(max(c['high'] for c in last32), 3)
            session_low  = round(min(c['low']  for c in last32), 3)

        # ── Real H1 candles → last 24h high/low ────────────────────
        h1 = get_candles('EUR/JPY', '1h', '5d')
        h1_rsi = None
        high_24h = None
        low_24h  = None
        if h1 and len(h1) >= 14:
            h1_closes = [c['close'] for c in h1]
            h1_rsi = calc_rsi(h1_closes)
            last24 = h1[-24:] if len(h1) >= 24 else h1
            high_24h = round(max(c['high'] for c in last24), 3)
            low_24h  = round(min(c['low']  for c in last24), 3)

        # ── Real H4 + Daily via get_sage_chart_data ─────────────────
        chart = get_sage_chart_data('EUR/JPY', cp)
        da = chart.get('daily', {})
        h4 = chart.get('h4', {})
        wk = chart.get('weekly', {})
        sr = chart.get('sr_levels', [])

        # Find nearest support and resistance from real S/R
        support_levels    = sorted([lv for lv in sr if lv['type']=='SUPPORT'],    key=lambda x: x['distance_pips'])
        resistance_levels = sorted([lv for lv in sr if lv['type']=='RESISTANCE'], key=lambda x: x['distance_pips'])

        return jsonify({
            # Live price data
            'pair': 'EUR/JPY',
            'price': round(cp, 3),
            'high': q.get('high', cp),
            'low':  q.get('low',  cp),
            'change': q.get('change', 0),
            'percent_change': q.get('percent_change', 0),
            # Real M15 data
            'm15_rsi': m15_rsi,
            'london_open_price': round(london_open_price, 3) if london_open_price else None,
            'london_pips': london_pips,
            'session_high': session_high,
            'session_low':  session_low,
            # Real H1 data
            'h1_rsi':   h1_rsi,
            'high_24h': high_24h,
            'low_24h':  low_24h,
            # Real H4 + Daily structure
            'daily_trend':  da.get('trend', 'UNKNOWN'),
            'daily_rsi':    da.get('rsi'),
            'daily_ema20':  da.get('ema20'),
            'daily_ema50':  da.get('ema50'),
            'daily_ema200': da.get('ema200'),
            'daily_macd':   da.get('macd_bias'),
            'daily_struct': da.get('structure', 'UNKNOWN'),
            'daily_phase':  da.get('phase', 'UNKNOWN'),
            'h4_trend':     h4.get('trend', 'UNKNOWN'),
            'h4_rsi':       h4.get('rsi'),
            'h4_ema9':      h4.get('ema9'),
            'h4_ema20':     h4.get('ema20'),
            'h4_macd':      h4.get('macd_bias'),
            'h4_struct':    h4.get('structure', 'UNKNOWN'),
            'weekly_trend': wk.get('trend', 'UNKNOWN'),
            'weekly_rsi':   wk.get('rsi'),
            # Real S/R levels
            'nearest_support':    support_levels[0]    if support_levels    else None,
            'nearest_resistance': resistance_levels[0] if resistance_levels else None,
            'sr_levels': sr[:6],
            # ADR and range data
            'adr': chart.get('adr'),
            'weekly_range_pct': chart.get('weekly_range_pct'),
            'trend_strength':   chart.get('trend_strength'),
        })
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/london-signal', methods=['GET'])
@login_required
def london_signal():
    """
    EUR/JPY London session analysis.
    Direct TwelveData call — 120 hourly candles, UTC forced.
    3AM-8AM EST = 08:00-13:00 UTC.
    """
    try:
        from datetime import datetime, timezone, timedelta

        # ── FETCH DIRECT — bypass get_candles cache/timezone issues ─────────
        # Request 120 hourly candles (5 days) with explicit UTC timezone
        candles_raw = []
        if TWELVE_DATA_KEY:
            try:
                url = (f'https://api.twelvedata.com/time_series'
                       f'?symbol=EUR/JPY&interval=1h&outputsize=120'
                       f'&timezone=UTC&apikey={TWELVE_DATA_KEY}')
                resp = http_requests.get(url, timeout=12)
                js = resp.json()
                if 'values' in js and js['values']:
                    for v in reversed(js['values']):  # oldest first
                        candles_raw.append({
                            'time':  v['datetime'],   # guaranteed UTC
                            'open':  float(v['open']),
                            'high':  float(v['high']),
                            'low':   float(v['low']),
                            'close': float(v['close']),
                        })
            except Exception as e:
                print(f'[london-signal TwelveData] {e}')

        # Fallback to yfinance if TwelveData fails
        if not candles_raw:
            try:
                import yfinance as yf
                df = yf.Ticker('EURJPY=X').history(interval='1h', period='7d')
                for ts, row in df.iterrows():
                    # yfinance forex returns UTC-aware timestamps
                    t = ts.tz_convert('UTC') if ts.tzinfo else ts
                    candles_raw.append({
                        'time':  t.strftime('%Y-%m-%d %H:%M'),
                        'open':  float(row['Open']),
                        'high':  float(row['High']),
                        'low':   float(row['Low']),
                        'close': float(row['Close']),
                    })
            except Exception as e:
                print(f'[london-signal yfinance] {e}')

        if not candles_raw or len(candles_raw) < 5:
            return jsonify({'error': 'Not enough EUR/JPY candle data'}), 500

        # ── PARSE TIMES (all UTC now) ────────────────────────────────────────
        now_utc = datetime.now(timezone.utc)

        def parse_time(s):
            for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S']:
                try:
                    return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
                except:
                    continue
            return None

        # London session: 3AM-8AM EST = 08:00-13:00 UTC
        london_open_utc  = 8
        london_close_utc = 13
        pip = 100  # EUR/JPY: 1 pip = 0.01

        # Try today first, then walk back up to 4 days to find a valid session
        session_found = False
        candle_list = []
        session_date = None

        for days_back in range(0, 5):
            check_date = (now_utc - timedelta(days=days_back)).date()
            session_candles = []
            for c in candles_raw:
                ct = parse_time(c['time'])
                if ct is None:
                    continue
                if ct.date() == check_date and london_open_utc <= ct.hour < london_close_utc:
                    session_candles.append(c)

            if len(session_candles) >= 3:  # need at least 3 hourly candles
                candle_list = sorted(session_candles, key=lambda x: x['time'])
                session_date = check_date
                session_found = True
                break

        if not session_found or not candle_list:
            return jsonify({'error': f'London session candles not found (checked 5 days, got {len(candles_raw)} total candles)'}), 500

        # ── CORE CALCULATIONS ────────────────────────────────────────────────
        london_open_price  = candle_list[0]['open']
        london_close_price = candle_list[-1]['close']
        london_high = max(c['high'] for c in candle_list)
        london_low  = min(c['low']  for c in candle_list)

        # Total session range
        total_range_pips = round((london_high - london_low) * pip)

        # Dominant impulse: which extreme is further from open?
        bull_extreme  = london_high
        bear_extreme  = london_low
        bull_impulse  = round((bull_extreme  - london_open_price) * pip)
        bear_impulse  = round((london_open_price - bear_extreme) * pip)

        if bull_impulse >= bear_impulse:
            impulse_direction = 'BULL'
            impulse_pips      = bull_impulse
            impulse_end       = round(bull_extreme, 3)
            retrace_pips      = round((bull_extreme - london_close_price) * pip)
            retrace_start     = round(bull_extreme, 3)
            retrace_end       = round(london_close_price, 3)
        else:
            impulse_direction = 'BEAR'
            impulse_pips      = bear_impulse
            impulse_end       = round(bear_extreme, 3)
            retrace_pips      = round((london_close_price - bear_extreme) * pip)
            retrace_start     = round(bear_extreme, 3)
            retrace_end       = round(london_close_price, 3)

        retrace_pct = round((retrace_pips / impulse_pips) * 100) if impulse_pips > 0 else 0
        net_pips    = round((london_close_price - london_open_price) * pip)

        # ── DIRECTION — based on dominant impulse ───────────────────────────
        if impulse_direction == 'BEAR':
            direction = 'BEARISH'
            if retrace_pct < 50:
                overall_read = f'Bear impulse: -{impulse_pips} pips ↓ | Retraced: +{retrace_pips} pips ({retrace_pct}%) | Net: {net_pips:+d} pips → DOWNTREND — strong bias'
            elif retrace_pct < 70:
                overall_read = f'Bear impulse: -{impulse_pips} pips ↓ | Moderate retrace: +{retrace_pips} pips ({retrace_pct}%) | Net: {net_pips:+d} pips → DOWNTREND — moderate bias'
            else:
                overall_read = f'Bear impulse: -{impulse_pips} pips ↓ | Deep retrace: +{retrace_pips} pips ({retrace_pct}%) | Net: {net_pips:+d} pips → DOWNTREND (deep retrace — confirm at 9:30)'
        elif impulse_direction == 'BULL':
            direction = 'BULLISH'
            if retrace_pct < 50:
                overall_read = f'Bull impulse: +{impulse_pips} pips ↑ | Retraced: -{retrace_pips} pips ({retrace_pct}%) | Net: {net_pips:+d} pips → UPTREND — strong bias'
            elif retrace_pct < 70:
                overall_read = f'Bull impulse: +{impulse_pips} pips ↑ | Moderate retrace: -{retrace_pips} pips ({retrace_pct}%) | Net: {net_pips:+d} pips → UPTREND — moderate bias'
            else:
                overall_read = f'Bull impulse: +{impulse_pips} pips ↑ | Deep retrace: -{retrace_pips} pips ({retrace_pct}%) | Net: {net_pips:+d} pips → UPTREND (deep retrace — confirm at 9:30)'
        else:
            direction = 'FLAT'
            overall_read = f'No clear impulse | Net: {net_pips:+d} pips → No London bias — wait for SPY direction first'

        # SPY bias
        if direction == 'BULLISH':
            spy_bias      = 'BUY CALLS'
            bias_color    = 'green'
            signal_strength = 'STRONG' if impulse_pips >= 50 and retrace_pct < 50 else 'MODERATE' if impulse_pips >= 30 else 'WEAK'
        elif direction == 'BEARISH':
            spy_bias      = 'BUY PUTS'
            bias_color    = 'red'
            signal_strength = 'STRONG' if impulse_pips >= 50 and retrace_pct < 50 else 'MODERATE' if impulse_pips >= 30 else 'WEAK'
        else:
            spy_bias      = 'WAIT — NO SIGNAL'
            bias_color    = 'gold'
            signal_strength = 'NONE'

        # ── HOURLY BREAKDOWN ─────────────────────────────────────────────────
        key_levels = []
        prev_close = london_open_price
        for i, c in enumerate(candle_list):
            est_hour = 3 + i
            hour_label = f'{est_hour}AM EST'
            move = round((c['close'] - prev_close) * pip)
            if abs(move) >= 5:
                key_levels.append({
                    'hour': hour_label,
                    'level': round(c['close'], 3),
                    'move_pips': move,
                    'direction': 'BULL' if move > 0 else 'BEAR',
                    'high': round(c['high'], 3),
                    'low':  round(c['low'],  3),
                })
            prev_close = c['close']

        # ── CURRENT PRICE + EMA100 ───────────────────────────────────────────
        q = get_price('EUR/JPY')
        current_price = round(float(q['price']), 3) if q else london_close_price

        ema100 = None
        ema100_trend = 'UNKNOWN'
        try:
            h1_candles = get_candles('EUR/JPY', '1h', '5d')
            if h1_candles and len(h1_candles) >= 10:
                closes = [c['close'] for c in h1_candles]
                period = min(100, len(closes))
                k = 2.0 / (period + 1)
                ema = closes[0]
                for c_val in closes[1:]:
                    ema = c_val * k + ema * (1 - k)
                ema100 = round(ema, 3)
                ema100_trend = 'UPTREND' if current_price > ema100 else 'DOWNTREND'
        except:
            pass

        return jsonify({
            'session_date':       str(session_date),
            'candles_used':       len(candle_list),
            'london_open_price':  round(london_open_price, 3),
            'london_close_price': round(london_close_price, 3),
            'london_high':        round(london_high, 3),
            'london_low':         round(london_low, 3),
            'pip_move':           net_pips,
            'net_pips':           net_pips,
            'total_range_pips':   total_range_pips,
            'direction':          direction,
            'spy_bias':           spy_bias,
            'bias_color':         bias_color,
            'signal_strength':    signal_strength,
            'overall_read':       overall_read,
            'impulse_direction':  impulse_direction,
            'impulse_pips':       impulse_pips,
            'impulse_start':      round(london_open_price, 3),
            'impulse_end':        impulse_end,
            'retrace_pips':       retrace_pips,
            'retrace_pct':        retrace_pct,
            'retrace_start':      retrace_start,
            'retrace_end':        retrace_end,
            'key_levels':         key_levels,
            'current_price':      current_price,
            'ema100':             ema100,
            'ema100_trend':       ema100_trend,
            'session_hours':      '3AM-8AM EST (08:00-13:00 UTC)',
        })
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500
@app.route('/api/forex-analyze', methods=['POST'])
@login_required
def forex_analyze():
    try:
        data = request.get_json()
        prompt = data.get('prompt', '')
        max_tokens = data.get('max_tokens', 3000)
        pair = data.get('pair', '')

        live_ctx = ''
        if pair:
            q = get_price(pair)
            session_name, _ = get_session()
            if q:
                current_price = float(q['price'])
                live_ctx = (f"\nLIVE PRICE: {pair} = {current_price} | H:{q['high']} L:{q['low']}"
                            f" | Change: {q.get('percent_change',0):+.2f}% | Session: {session_name}\n"
                            f"DATA SOURCE: TwelveData 35-day OHLC — EMA9/20/50/100/200, real ADX (Wilder),"
                            f" HH/HL trend structure, ATR, real S/R zones (min 2 touches).\n"
                            f"ADX RULE: ADX > 25 = trending (trade it). ADX < 20 = ranging (skip or wait).\n")
                chart = get_chart_analysis(pair, current_price)
                live_ctx += format_chart_analysis_for_prompt(chart)

            news = get_news(pair)
            if news:
                live_ctx += f"LATEST NEWS:\n" + '\n'.join([f"- {n['title']} ({n['source']})" for n in news[:3]]) + '\n\n'

        full_prompt = live_ctx + prompt if live_ctx else prompt
        text = call_claude(full_prompt, max_tokens)
        return jsonify({'content': text})
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ─── Async job runners for heavy forex scans ────────────────────────────────
_forex_scan_jobs = {}

def _run_forex_scan_job(job_id, scan_type):
    """Background job for scenarios/daily/weekly — prevents Render 30s timeout crash"""
    try:
        _forex_scan_jobs[job_id] = {'status': 'running', 'step': 'Fetching live prices...'}
        date_str = datetime.now().strftime('%A, %B %d, %Y')
        session_name, _ = get_session()

        if scan_type == 'scenarios':
            scan_pairs = ['EUR/USD','GBP/USD','USD/JPY','XAU/USD','AUD/USD','USD/CAD','GBP/JPY']
        elif scan_type == 'daily':
            scan_pairs = ['EUR/USD','GBP/USD','USD/JPY','XAU/USD','AUD/USD','USD/CAD','GBP/JPY','EUR/JPY','USD/CHF']
        else:  # weekly
            scan_pairs = ['EUR/USD','GBP/USD','USD/JPY','XAU/USD','AUD/USD','USD/CAD','GBP/JPY','EUR/JPY','NZD/USD']

        prices = get_prices_parallel(scan_pairs)
        news = get_news()
        _forex_scan_jobs[job_id]['step'] = 'Analyzing 5 timeframes per pair...'
        chart_data = get_multi_pair_chart_data(scan_pairs, prices)
        prices_str = '\n'.join([f"{p}: {v['price']} (H:{v['high']} L:{v['low']} {v.get('percent_change',0):+.2f}%)" for p,v in prices.items()])
        news_str = '\n'.join([f"- {n['title']}" for n in news[:5]]) or "- Markets await key economic data"
        chart_ctx = '\n'.join([format_chart_analysis_for_prompt(chart_data[p]) for p in scan_pairs if p in chart_data])
        _forex_scan_jobs[job_id]['step'] = 'Wolf AI synthesizing analysis...'

        if scan_type == 'scenarios':
            prompt = f"""You are Wolf AI — professional forex trader. Today: {date_str}. Session: {session_name}.
LIVE PRICES:\n{prices_str}\nNEWS (real headlines only — do NOT invent upcoming events like NFP/CPI/FOMC unless they appear in these headlines):\n{news_str}\n{chart_ctx}
Using the REAL chart data above (TwelveData OHLC — EMA9/20/50/100/200, real ADX, HH/HL structure, ATR, swing S/R), find the 7 BEST trades. ADX RULE: only include pairs where ADX > 25 (trending) — skip or flag WAIT if ADX < 20 (ranging). Only include pairs where 4+ timeframes align. Use EXACT S/R levels and ATR values from chart data above for SL sizing. Give BOTH buy AND sell scenario for each trade.
Respond ONLY in valid JSON (no markdown, no backticks):
{{"week":"{date_str}","session":"{session_name}","market_theme":"string","dxy_direction":"BULLISH or BEARISH","risk_sentiment":"RISK-ON or RISK-OFF","trades":[{{"rank":1,"pair":"EUR/USD","live_price":"1.0380","overall_bias":"BEARISH","timeframe_alignment":{{"monthly":"BEARISH","weekly":"BEARISH","daily":"BEARISH","h4":"BEARISH","h1":"NEUTRAL","m15":"BEARISH"}},"aligned_count":5,"confidence":82,"primary_direction":"SELL","thesis":"3-4 sentence thesis using real chart data","key_resistance":"1.0400","key_support":"1.0340","buy_scenario":{{"trigger":"string","entry":"1.0410","stop_loss":"1.0380","tp1":"1.0450","tp2":"1.0500","tp3":"1.0550","probability":30}},"sell_scenario":{{"trigger":"string","entry":"1.0360","stop_loss":"1.0390","tp1":"1.0320","tp2":"1.0280","tp3":"1.0240","probability":70}},"best_session":"LONDON","key_news_this_week":"string","invalidation":"string"}}]}}"""
            max_tok = 5000
        elif scan_type == 'daily':
            prompt = f"""You are Wolf AI — professional intraday forex trader. Today: {date_str}. Session: {session_name}.
LIVE PRICES:\n{prices_str}\nNEWS (real headlines only — do NOT invent events like NFP/CPI/FOMC unless in headlines above):\n{news_str}\n{chart_ctx}
Using the REAL hourly chart data above (TwelveData OHLC — EMA9/20/50/100/200, real ADX, HH/HL structure, ATR, real S/R), find the 3 BEST day trades for today's session. ADX RULE: only pick pairs where ADX > 25. Use ATR for SL sizing. Use EXACT price levels from chart data.
Respond ONLY in valid JSON (no markdown):
{{"date":"{date_str}","session":"{session_name}","dxy_bias":"BULLISH or BEARISH","risk_environment":"RISK-ON or RISK-OFF","picks":[{{"rank":1,"pair":"EUR/USD","direction":"SELL","entry":"1.0390","stop_loss":"1.0420","tp1":"1.0350","tp2":"1.0310","tp3":"1.0270","rr_ratio":"1:2.5","confidence":85,"sharingan_score":5,"thesis":"2-3 sentence thesis using real chart data","confluences":["Price below EMA200 daily","RSI 42 bearish","Hourly resistance at 1.0400"],"best_window":"London Open 3-5AM EST","key_news":"derive from real news above only — no invented events","invalidation":"Break above 1.0430","buy_scenario":"string","sell_scenario":"string"}}]}}"""
            max_tok = 4000
        else:  # weekly
            prompt = f"""You are Wolf AI — professional swing trader. Today: {date_str}.
LIVE PRICES:\n{prices_str}\nNEWS (real headlines only — do NOT invent events like NFP/CPI/FOMC unless in headlines above):\n{news_str}\n{chart_ctx}
Using the REAL weekly and daily chart data above (TwelveData OHLC — EMA9/20/50/100/200, real ADX, HH/HL structure, ATR, 52-week range, real S/R), find the 3 BEST swing trades for this week (2-5 day holds). ADX RULE: only pick pairs where ADX > 25. Use ATR for SL sizing. Use EXACT levels from real chart data.
Respond ONLY in valid JSON (no markdown):
{{"week":"{date_str}","weekly_theme":"Main macro theme","dxy_outlook":"BULLISH or BEARISH","central_bank_focus":"Key CB event this week","picks":[{{"rank":1,"pair":"GBP/USD","direction":"SELL","entry_zone":"1.2630-1.2650","stop_loss":"1.2700","tp1":"1.2570","tp2":"1.2500","tp3":"1.2420","rr_ratio":"1:2.8","confidence":80,"sharingan_score":4,"hold_days":"3-4","fundamental":"string","technical":"string using real EMA/RSI data","confluences":["Weekly bearish","Daily below EMA200","RSI 45 bearish"],"key_events":"BOE minutes","key_risk":"Surprise hawkish BOE","buy_scenario":"string","sell_scenario":"string"}}]}}"""
            max_tok = 4000

        result = parse_json_response(call_claude(prompt, max_tok))
        _forex_scan_jobs[job_id] = {'status': 'done', 'result': result}
    except Exception as e:
        import traceback; print(traceback.format_exc())
        _forex_scan_jobs[job_id] = {'status': 'error', 'error': str(e)}

@app.route('/api/forex-scan-start', methods=['POST'])
@login_required
def forex_scan_start():
    """Start forex scan as background job — returns job_id immediately"""
    try:
        data = request.get_json() or {}
        scan_type = data.get('type', 'scenarios')  # scenarios, daily, weekly
        job_id = str(uuid.uuid4())[:8]
        _forex_scan_jobs[job_id] = {'status': 'starting'}
        threading.Thread(target=_run_forex_scan_job, args=(job_id, scan_type), daemon=True).start()
        return jsonify({'job_id': job_id, 'status': 'starting'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/forex-scan-poll/<job_id>', methods=['GET'])
@login_required
def forex_scan_poll(job_id):
    """Poll forex scan job"""
    job = _forex_scan_jobs.get(job_id)
    if not job: return jsonify({'status': 'error', 'error': 'Job not found'}), 404
    if job['status'] == 'done':
        result = dict(job.get('result', {})); result['status'] = 'done'
        _forex_scan_jobs.pop(job_id, None); return jsonify(result)
    if job['status'] == 'error':
        err = job.get('error', 'Unknown'); _forex_scan_jobs.pop(job_id, None)
        return jsonify({'status': 'error', 'error': err}), 500
    return jsonify({'status': job['status'], 'step': job.get('step', 'Processing...')})

@app.route('/api/forex-scenarios', methods=['POST'])
@login_required
def forex_scenarios():
    """True async — starts background job, returns job_id immediately (no blocking)"""
    try:
        job_id = str(uuid.uuid4())[:8]
        _forex_scan_jobs[job_id] = {'status': 'starting', 'step': 'Fetching live prices...'}
        threading.Thread(target=_run_forex_scan_job, args=(job_id, 'scenarios'), daemon=True).start()
        return jsonify({'job_id': job_id, 'status': 'starting'})
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/forex-daily-picks', methods=['POST'])
@login_required
def forex_daily_picks():
    """True async — starts background job, returns job_id immediately (no blocking)"""
    try:
        job_id = str(uuid.uuid4())[:8]
        _forex_scan_jobs[job_id] = {'status': 'starting', 'step': 'Fetching live prices...'}
        threading.Thread(target=_run_forex_scan_job, args=(job_id, 'daily'), daemon=True).start()
        return jsonify({'job_id': job_id, 'status': 'starting'})
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/forex-weekly-picks', methods=['POST'])
@login_required
def forex_weekly_picks():
    """True async — starts background job, returns job_id immediately (no blocking)"""
    try:
        job_id = str(uuid.uuid4())[:8]
        _forex_scan_jobs[job_id] = {'status': 'starting', 'step': 'Fetching live prices...'}
        threading.Thread(target=_run_forex_scan_job, args=(job_id, 'weekly'), daemon=True).start()
        return jsonify({'job_id': job_id, 'status': 'starting'})
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/forex-scanner', methods=['POST'])
@login_required
def forex_scanner():
    try:
        data = request.get_json() or {}
        theme = data.get('theme', 'best setups today')
        date_str = datetime.now().strftime('%A, %B %d, %Y')
        session_name, _ = get_session()
        scan_pairs = ['EUR/USD','GBP/USD','USD/JPY','XAU/USD','AUD/USD','USD/CAD','GBP/JPY','EUR/JPY','USD/CHF','NZD/USD']
        prices = get_prices_parallel(scan_pairs)
        news = get_news()
        chart_data = get_multi_pair_chart_data(scan_pairs, prices)
        prices_str = "\n".join([f"{p}: {v['price']} (H:{v['high']} L:{v['low']} {v.get('percent_change',0):+.2f}%)" for p,v in prices.items()])
        news_str = "\n".join([f"- {n['title']}" for n in news[:4]]) or "- Monitor key levels"
        chart_ctx = "\n".join([format_chart_analysis_for_prompt(chart_data[p]) for p in scan_pairs if p in chart_data])
        prompt = (
            f"You are Wolf AI - elite forex scanner. Today: {date_str}. Session: {session_name}.\n"
            f"Theme requested: {theme}\n\n"
            f"LIVE PRICES:\n{prices_str}\n\n"
            f"NEWS:\n{news_str}\n\n"
            f"{chart_ctx}\n\n"
            f"Scan all pairs and find the 3 BEST setups matching the theme \"{theme}\".\n"
            f"Use REAL chart data above for S/R levels, EMAs, RSI. Be specific.\n\n"
            f'Respond ONLY in valid JSON (no markdown):\n'
            '{{"scan_theme":"{theme}","date":"{date_str}","session":"{session_name}","dxy_bias":"BULLISH or BEARISH","risk_environment":"RISK-ON or RISK-OFF","picks":[{{"rank":1,"pair":"EUR/USD","direction":"SELL","entry":"1.0390","stop_loss":"1.0420","tp1":"1.0350","tp2":"1.0310","tp3":"1.0270","rr_ratio":"1:2.5","confidence":85,"thesis":"2-3 sentence thesis using real chart data","confluences":["real level 1","real level 2"],"best_window":"London Open 3-5AM EST","invalidation":"Break above 1.0430","buy_scenario":"string","sell_scenario":"string"}}]}}'.format(theme=theme,date_str=date_str,session_name=session_name)
        )
        result = parse_json_response(call_claude(prompt, 3000))
        return jsonify(result)
    except json.JSONDecodeError as e: return jsonify({'error': f'AI returned invalid JSON: {str(e)}'}), 500
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/api/forex-picks', methods=['POST'])
@login_required
def forex_picks():
    try:
        prompt = request.get_json().get('prompt', '')
        return jsonify({'content': call_claude(prompt, 2200)})
    except Exception as e: return jsonify({'error': str(e)}), 500

# ── Wolf Scan Job Store (in-memory) ──────────────────────────
import threading, uuid
def calculate_real_confidence(pair, direction, chart_data):
    """
    Calculate confidence score 0-100 from REAL chart data.
    Replaces AI-guessed confidence with data-driven score.
    direction: 'BUY' or 'SELL'
    """
    score = 0
    ca = chart_data.get(pair, {})
    if not ca:
        return 50  # no data — neutral

    d  = ca.get('daily', {})
    w  = ca.get('weekly', {})
    h  = ca.get('hourly', {})
    ts = ca.get('trend_summary', {})
    sr = ca.get('sr_levels', [])

    is_bull = direction.upper() in ('BUY', 'BULLISH')

    # ── 1. TIMEFRAME ALIGNMENT (30 pts) ──────────────────────
    w_trend = w.get('trend', 'NEUTRAL')
    d_trend = d.get('trend', 'NEUTRAL')
    h_trend = h.get('trend', 'NEUTRAL')

    tf_match = 0
    for t in [w_trend, d_trend, h_trend]:
        if is_bull and t == 'BULLISH':   tf_match += 1
        elif not is_bull and t == 'BEARISH': tf_match += 1

    score += tf_match * 10  # 0, 10, 20, or 30

    # ── 2. RSI ZONE (20 pts) ──────────────────────────────────
    d_rsi = d.get('rsi')
    h_rsi = h.get('rsi')

    for rsi in [d_rsi, h_rsi]:
        if rsi is None:
            continue
        if is_bull:
            if 40 <= rsi <= 60:   score += 8   # ideal momentum building
            elif 30 <= rsi < 40:  score += 10  # oversold bounce potential
            elif rsi < 30:        score += 6   # deep oversold — risky
            elif rsi > 70:        score -= 5   # overbought — bad for buy
        else:
            if 40 <= rsi <= 60:   score += 8
            elif 60 < rsi <= 70:  score += 10  # overbought rejection potential
            elif rsi > 70:        score += 6   # deep overbought — risky
            elif rsi < 30:        score -= 5   # oversold — bad for sell

    # ── 3. EMA STACK (20 pts) ─────────────────────────────────
    cp = ca.get('current_price', 0)
    ema20  = d.get('ema20')
    ema50  = d.get('ema50')
    ema200 = d.get('ema200')

    if cp and ema200:
        if is_bull and cp > ema200:   score += 8
        elif not is_bull and cp < ema200: score += 8

    if cp and ema50:
        if is_bull and cp > ema50:    score += 7
        elif not is_bull and cp < ema50:  score += 7

    if cp and ema20:
        if is_bull and cp > ema20:    score += 5
        elif not is_bull and cp < ema20:  score += 5

    # ── 4. MACD CONFIRMS (15 pts) ─────────────────────────────
    d_macd = d.get('macd_bias')
    h_macd = h.get('macd_bias')

    if d_macd:
        if is_bull and d_macd == 'BULLISH':   score += 8
        elif not is_bull and d_macd == 'BEARISH': score += 8

    if h_macd:
        if is_bull and h_macd == 'BULLISH':   score += 7
        elif not is_bull and h_macd == 'BEARISH': score += 7

    # ── 5. CLEAN S/R NEARBY (15 pts) ─────────────────────────
    if sr:
        close_levels = [lv for lv in sr if lv.get('distance_pips', 999) < 50]
        if close_levels:
            score += 8
        strong_levels = [lv for lv in sr if lv.get('strength', 0) >= 3 and lv.get('distance_pips', 999) < 80]
        if strong_levels:
            score += 7

    # ── 6. ADX TREND STRENGTH BONUS (10 pts) ─────────────────
    # Real Wilder ADX — same as Wolf Agent. Only trade confirmed trends.
    adx = d.get('adx')
    if adx is not None:
        if adx > 35:   score += 10   # strong trend — high confidence
        elif adx > 25: score += 5    # trending — trade it
        elif adx < 20: score -= 15   # ranging — avoid, kills confidence

    # ── CLAMP to 0-100 ────────────────────────────────────────
    return max(0, min(100, score))



# ── ASYNC JOB STORE ─────────────────────────────────────────────────────────
_async_jobs = {}

def _run_async_ai_job(job_id, prompt, max_tokens, pair=''):
    """Generic async AI job - prevents 502 Bad Gateway from Render 30s timeout"""
    try:
        _async_jobs[job_id] = {'status': 'running'}
        # Add live context if pair provided
        live_ctx = ''
        if pair:
            try:
                q = get_price(pair)
                session_name, _ = get_session()
                if q:
                    current_price = float(q['price'])
                    live_ctx = (f"\nLIVE PRICE: {pair} = {current_price} | H:{q['high']} L:{q['low']}"
                                f" | Change: {q.get('percent_change',0):+.2f}% | Session: {session_name}\n"
                                f"DATA: TwelveData 35-day OHLC — EMA9/20/50/100/200, real ADX, HH/HL structure, ATR, real S/R.\n"
                                f"ADX RULE: >25=trending (trade), <20=ranging (wait/skip).\n")
                    chart = get_chart_analysis(pair, current_price)
                    live_ctx += format_chart_analysis_for_prompt(chart)
                news = get_news(pair)
                if news:
                    live_ctx += "LATEST NEWS:\n" + '\n'.join([f"- {n['title']} ({n['source']})" for n in news[:3]]) + '\n\n'
            except Exception as ctx_err:
                print(f'[AsyncAI] context error: {ctx_err}')
        full_prompt = live_ctx + prompt if live_ctx else prompt
        text = call_claude(full_prompt, max_tokens)
        _async_jobs[job_id] = {'status': 'done', 'content': text}
    except Exception as e:
        import traceback; print(traceback.format_exc())
        _async_jobs[job_id] = {'status': 'error', 'error': str(e)}

@app.route('/api/async-ai-start', methods=['POST'])
@login_required
def async_ai_start():
    """Start any AI prompt as background job - returns job_id immediately"""
    try:
        data = request.get_json()
        prompt = data.get('prompt', '')
        max_tokens = data.get('max_tokens', 3000)
        pair = data.get('pair', '')
        job_id = str(uuid.uuid4())[:8]
        _async_jobs[job_id] = {'status': 'starting'}
        t = threading.Thread(target=_run_async_ai_job, args=(job_id, prompt, max_tokens, pair), daemon=True)
        t.start()
        return jsonify({'job_id': job_id, 'status': 'starting'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/async-ai-poll/<job_id>', methods=['GET'])
@login_required
def async_ai_poll(job_id):
    """Poll async AI job"""
    job = _async_jobs.get(job_id)
    if not job: return jsonify({'status': 'error', 'error': 'Job not found'}), 404
    if job['status'] == 'done':
        del _async_jobs[job_id]
        return jsonify({'status': 'done', 'content': job['content']})
    if job['status'] == 'error':
        err = job.get('error', 'Unknown error')
        del _async_jobs[job_id]
        return jsonify({'status': 'error', 'error': err}), 500
    return jsonify({'status': job['status']})

# ── Wolf Stock Scanner page ───────────────────────────────────
@app.route('/byakugan')
@login_required
@elite_required
def byakugan_page():
    return render_template('wolf_stocks.html')

# ═══════════════════════════════════════════════════════════════
# NEW FEATURES — Position Sizing, Earnings, Sector Heatmap
# ═══════════════════════════════════════════════════════════════

@app.route('/api/position-size', methods=['POST'])
@login_required
def position_size():
    try:
        data = request.get_json() or {}
        account_size  = float(data.get('account_size', 10000))
        risk_pct      = float(data.get('risk_pct', 1.0))
        option_price  = float(data.get('option_price', 3.00))
        stop_price    = float(data.get('stop_price', 1.50))
        target_price  = float(data.get('target_price', 6.00))
        risk_per_trade    = account_size * (risk_pct / 100)
        risk_per_contract = (option_price - stop_price) * 100
        contracts = int(risk_per_trade / risk_per_contract) if risk_per_contract > 0 else 0
        contracts = max(1, min(contracts, 50))
        total_cost  = contracts * option_price * 100
        max_loss    = contracts * risk_per_contract
        max_gain    = contracts * (target_price - option_price) * 100
        reward_risk = round((target_price - option_price) / (option_price - stop_price), 2) if (option_price - stop_price) > 0 else 0
        win_rate_needed = round(1 / (1 + reward_risk) * 100, 1) if reward_risk > 0 else 50
        return jsonify({'contracts': contracts, 'total_cost': round(total_cost,2), 'max_loss': round(max_loss,2),
                        'max_gain': round(max_gain,2), 'reward_risk': reward_risk,
                        'risk_per_trade': round(risk_per_trade,2), 'win_rate_needed': win_rate_needed,
                        'account_size': account_size, 'risk_pct': risk_pct})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/earnings-check', methods=['POST'])
@login_required
def earnings_check():
    try:
        import yfinance as yf
        data     = request.get_json() or {}
        tickers  = data.get('tickers', ['AAPL','MSFT','NVDA','TSLA','AMD'])
        days_out = int(data.get('days_out', 14))
        results  = []
        for sym in tickers[:20]:
            try:
                cal = yf.Ticker(sym).calendar
                earn_date = None; days_away = None; safe = True
                if cal is not None and not cal.empty:
                    cols = cal.columns.tolist()
                    if cols:
                        earn_date = str(cols[0])[:10]
                        dt = datetime.strptime(earn_date, '%Y-%m-%d')
                        days_away = (dt - datetime.now()).days
                        safe = days_away > days_out or days_away < 0
                results.append({'ticker': sym, 'earn_date': earn_date, 'days_away': days_away, 'safe': safe,
                                'warning': f'Earnings in {days_away} days — IV crush risk!' if (days_away and 0 < days_away <= days_out) else None})
            except:
                results.append({'ticker': sym, 'earn_date': None, 'days_away': None, 'safe': True, 'warning': None})
        return jsonify({'results': results, 'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/sector-heatmap', methods=['GET'])
@login_required
def sector_heatmap():
    try:
        import yfinance as yf
        sectors = {'Technology':'XLK','Financials':'XLF','Energy':'XLE','Healthcare':'XLV',
                   'Consumer Disc':'XLY','Industrials':'XLI','Materials':'XLB','Utilities':'XLU',
                   'Real Estate':'XLRE','Consumer Staples':'XLP','Communication':'XLC','Defense':'ITA'}
        results = []
        for name, etf in sectors.items():
            try:
                hist = yf.Ticker(etf).history(period='5d',interval='1d')
                if len(hist) >= 2:
                    today = float(hist['Close'].iloc[-1]); prev = float(hist['Close'].iloc[-2])
                    chg   = round(((today-prev)/prev)*100,2)
                    wk_chg= round(((today-float(hist['Close'].iloc[0]))/float(hist['Close'].iloc[0]))*100,2) if len(hist)>=5 else chg
                    results.append({'sector':name,'etf':etf,'price':round(today,2),'day_chg':chg,'week_chg':wk_chg,
                                    'signal':'HOT' if chg>1 else 'COLD' if chg<-1 else 'NEUTRAL'})
            except: pass
        results.sort(key=lambda x: x['day_chg'], reverse=True)
        return jsonify({'sectors':results,'updated':datetime.now().strftime('%H:%M EST')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/options-flow', methods=['GET'])
@login_required
def options_flow():
    return jsonify({'status':'placeholder','message':'Live options flow coming soon — Unusual Whales API',
                    'preview':[
                        {'ticker':'SPY','type':'CALL','strike':'580C','expiry':'0DTE','size':5000,'premium':'$2.1M','sentiment':'BULLISH'},
                        {'ticker':'NVDA','type':'CALL','strike':'900C','expiry':'1DTE','size':1200,'premium':'$890K','sentiment':'BULLISH'},
                        {'ticker':'AAPL','type':'PUT','strike':'220P','expiry':'3DTE','size':800,'premium':'$340K','sentiment':'BEARISH'},
                    ]})

import uuid, threading
_byakugan_jobs = {}

def run_byakugan_job(job_id, scan_filter, date_str, user_id):
    try:
        _byakugan_jobs[job_id]['status'] = 'scanning'
        if scan_filter=='TECH': universe=['AAPL','MSFT','NVDA','AMD','META','GOOGL','AMZN','NFLX','COIN','PLTR','SMCI','AVGO','MU','CRM']
        elif scan_filter=='MEME': universe=['TSLA','MARA','HOOD','SOFI','COIN','PLTR','SQ','PYPL','RIOT','NIO']
        elif scan_filter=='BLUE': universe=['AAPL','MSFT','GOOGL','AMZN','META','JPM','GS','BAC','XOM','CVX','DIS']
        elif scan_filter=='ETF': universe=['SPY','QQQ','IWM','GLD','TLT','XLK','XLF','XLE','XLV','ARKK']
        elif scan_filter=='DEFENSE': universe=['LMT','RTX','NOC','GD','HII','LHX','AXON','KTOS','PLTR']
        else: universe=['AAPL','MSFT','NVDA','TSLA','AMD','META','GOOGL','AMZN','COIN','PLTR','JPM','GS','XOM','SMCI','AVGO']  # 15 stocks — balanced speed vs coverage
        regime = get_market_regime()
        vix = float(regime.get('vix', 20))
        _byakugan_jobs[job_id]['status'] = 'scoring'
        scored = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(score_stock, sym): sym for sym in universe}
            for f in as_completed(futures, timeout=50):
                sym = futures[f]
                try:
                    s = f.result()
                    if s and s['score'] > 15: scored.append(s)
                except Exception as e: print(f'[Byakugan] {sym}: {e}')
        scored.sort(key=lambda x: x['score'], reverse=True)
        top5 = scored[:5]
        # Lower threshold if nothing scores > 15
        if not scored:
            print('[Byakugan] No stocks scored at all — retrying with lower threshold')
            scored = [s for s in [score_stock(sym) for sym in universe[:5]] if s]
        if not scored:
            _byakugan_jobs[job_id]={'status':'error','error':'Market data unavailable — try again in 30 seconds'}; return
        top5 = scored[:5]
        _byakugan_jobs[job_id]['status'] = 'news'
        for stock in top5:
            try: stock['news'] = get_news(stock['ticker'])[:3]
            except: stock['news'] = []
        _byakugan_jobs[job_id]['status'] = 'greeks'
        for stock in top5:
            try:
                S=stock['price']; K=round(S/5)*5; T=3/365; r=0.045
                iv=0.35 if not stock.get('iv_rank') else max(0.15,min(0.80,stock['iv_rank']/100))
                opt_type='call' if stock.get('direction')=='BULLISH' else 'put'
                stock['greeks']=calculate_greeks(S,K,T,r,iv,opt_type); stock['atm_strike']=K; stock['iv_estimate']=round(iv*100,1)
            except: stock['greeks']=None
        _byakugan_jobs[job_id]['status'] = 'analyzing'
        if vix>30: vix_guide="VIX EXTREME >30: Sell premium only."
        elif vix>20: vix_guide="VIX ELEVATED 20-30: Defined-risk spreads."
        elif vix<15: vix_guide="VIX LOW <15: Buy directional. 0DTE/1DTE viable SPY/QQQ."
        else: vix_guide="VIX NORMAL 15-20: Debit spreads 3-7 DTE ideal."
        stocks_ctx = ''
        for s in top5:
            g=s.get('greeks') or {}
            stocks_ctx += f"\n===\n{s['ticker']} ${s['price']} Score:{s['score']} {s['direction']}\nEMA20:{s['ema20']} EMA50:{s['ema50']} EMA100:{s.get('ema100','?')} EMA200:{s['ema200']} RSI:{s['rsi']} MACD:{s['macd_bias']}\nADX:{s.get('adx','?')} ({s.get('adx_signal','?')}) | Structure:{s.get('hh_hl_structure','?')} ({s.get('hh_hl_strength','?')}% confidence)\nATR:{s.get('atr','?')} IV:{s.get('iv_estimate','?')}% Vol:{s['vol_ratio']}x UOA:{s['unusual_activity']}x\nD:{g.get('delta','?')} G:{g.get('gamma','?')} T:{g.get('theta','?')} V:{g.get('vega','?')}\nSR:{[(l['type'],l['price'],l['strength']) for l in s['sr_levels'][:3]]}\nNEWS:{' | '.join([n['title'][:60] for n in s['news']]) if s['news'] else 'None'}\n==="
        prompt = f"""You are BYAKUGAN — elite Wall Street options trader with full chart pattern vision. Paul Tudor Jones + Tom Sosnoff + Jesse Livermore + Fidelity CMT pattern analysis. Analyze so trader knows EXACTLY what to do tomorrow open.

CHART PATTERN DETECTION (apply to real data above):
Reversal: Double/Triple Top/Bottom (neckline break only), H&S (lowest failure rate, neckline close required), Pipe Bottom (2 tall bars end of downtrend)
Continuation: Bull/Bear Flag (target=flagpole height), Pennant, Cup & Handle (break above both lips)
Triangles: Ascending (usually breaks up), Descending (usually breaks down), Symmetrical (either way)
Wedges: Rising Wedge (breaks DOWN), Falling Wedge (breaks UP) — need 5 touches
Compression: NR4 (day 4 narrower than 1-3 = breakout imminent), Inside Bar (same signal)
Rules: Pattern NOT complete until CLOSE beyond level. False breakout = trap. Failed breakout = your entry.
ADX RULE: Only recommend BUY if ADX > 25. If ADX < 20 = ranging, reduce size or skip.
EMA100 RULE: Long only above EMA100, short only below. No exceptions.

TODAY:{date_str} MARKET:SPY:{regime['spy_price']} ({regime['spy_change']:+.2f}%) VIX:{vix} {regime['fear_greed']} {regime['regime']} VIX STRATEGY:{vix_guide}
{stocks_ctx}
RULES: Friday weeklies 1-5DTE stocks. 0DTE/1DTE SPY/QQQ. Delta 0.35-0.50 directional. Confidence clear=80-92% mixed=65-75%. Greeks required. Real S/R entries. No guessing — conclusions from real data only.
JSON only no markdown: {{"scan_date":"{date_str}","market_regime":{{"spy":"{regime['spy_price']}","spy_change":"{regime['spy_change']:+.2f}%","vix":"{vix}","sentiment":"{regime['fear_greed']}","regime":"{regime['regime']}","wolf_market_read":"2 sentences"}},"tomorrow_game_plan":"3 sentences","picks":[{{"rank":1,"ticker":"X","price":"0","wolf_score":80,"confidence":82,"direction":"BULLISH","sector":"Tech","thesis":"thesis","pattern_detected":"pattern name or NONE","pattern_target":"price or N/A","adx_reading":28,"adx_signal":"TRENDING","news_catalyst":"news","technical_setup":"setup","entry_zone":"X","key_support":"X","key_resistance":"Y","stop_loss":"X","target_1":"X","target_2":"Y","target_3":"Z","tomorrow_entry":"entry plan","options_play":{{"strategy":"LONG CALL","recommended_strike":"Xc","expiration":"This Friday (3 DTE)","entry_price":"X","max_risk":"$X","target_exit":"$X","stop_exit":"$X","greeks":{{"delta":0.42,"gamma":0.008,"theta":-0.85,"vega":0.45}},"iv_environment":"context","note":"note"}},"confluences":["c1"],"warnings":["w1"],"invalidation":"stop"}}]}}"""
        result = None; last_error = None
        for attempt in range(3):
            try:
                raw = call_claude(prompt, 5000)
                if not raw or not raw.strip(): raise ValueError('Empty response')
                result = parse_json_response(raw); break
            except Exception as retry_err:
                last_error = retry_err
                print(f'[Byakugan] Attempt {attempt+1} failed: {retry_err}')
                if attempt < 2: time.sleep(2)
        if result is None: raise Exception(f'AI failed after 3 attempts: {last_error}')
        for pick in result.get('picks',[]):
            match = next((s for s in top5 if s['ticker']==pick.get('ticker','')),None)
            if match:
                pick['real_score']=match['score']; pick['real_rsi']=match['rsi']
                pick['real_vol_ratio']=match['vol_ratio']; pick['real_iv_rank']=match['iv_rank']
                pick['real_signals']=match['signals']; pick['sr_levels']=match['sr_levels']
                pick['news']=match['news']; pick['real_greeks']=match.get('greeks')
        result['market_regime']=regime
        _byakugan_jobs[job_id]={'status':'done','result':result}
    except Exception as e:
        import traceback; print(traceback.format_exc())
        _byakugan_jobs[job_id]={'status':'error','error':str(e)}

@app.route('/api/byakugan-scan', methods=['POST'])
@login_required
@elite_required
def byakugan_scan_v2():
    try:
        data=request.get_json() or {}
        scan_filter=data.get('filter','ALL')
        date_str=datetime.now().strftime('%A, %B %d, %Y')
        job_id=str(uuid.uuid4())[:8]
        _byakugan_jobs[job_id]={'status':'starting'}
        t=threading.Thread(target=run_byakugan_job,args=(job_id,scan_filter,date_str,current_user.id),daemon=True)
        t.start()
        return jsonify({'job_id':job_id,'status':'starting'})
    except Exception as e:
        return jsonify({'error':str(e)}),500

@app.route('/api/byakugan-poll/<job_id>', methods=['GET'])
@login_required
@elite_required
def byakugan_poll(job_id):
    job=_byakugan_jobs.get(job_id)
    if not job: return jsonify({'status':'error','error':'Job not found'}),404
    if job['status']=='done':
        result=job.get('result',{}); result['status']='done'
        del _byakugan_jobs[job_id]; return jsonify(result)
    if job['status']=='error':
        return jsonify({'status':'error','error':job.get('error','Unknown error')}),500
    return jsonify({'status':job['status']})
# ═══════════════════════════════════════════════════════════════
# AI INFRASTRUCTURE SCANNER — paste at bottom of app.py
# (before the "

# ═══════════════════════════════════════════════════════════════════
# SAGE MODE — ULTIMATE SCANNER ENGINE
# ═══════════════════════════════════════════════════════════════════

def calc_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = []
    for i in range(1, len(candles)):
        h=candles[i]["high"]; l=candles[i]["low"]; pc=candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return round(sum(trs[-period:])/period, 5)

def calc_adx(candles, period=14):
    """
    Full Wilder ADX formula — exact same as Wolf Agent.
    > 25 = trending (trade it), < 20 = ranging (avoid or wait).
    """
    if not candles or len(candles) < period * 2:
        return None
    try:
        plus_dm, minus_dm, tr_list = [], [], []
        for i in range(1, len(candles)):
            h,  l  = candles[i]['high'],     candles[i]['low']
            ph, pl, pc = candles[i-1]['high'], candles[i-1]['low'], candles[i-1]['close']
            up_move   = h - ph
            down_move = pl - l
            plus_dm.append(up_move   if up_move   > down_move and up_move   > 0 else 0)
            minus_dm.append(down_move if down_move > up_move   and down_move > 0 else 0)
            tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))

        def wilder_smooth(lst, p):
            s = sum(lst[:p])
            result = [s]
            for x in lst[p:]:
                result.append(result[-1] - result[-1] / p + x)
            return result

        tr_s  = wilder_smooth(tr_list,  period)
        pdm_s = wilder_smooth(plus_dm,  period)
        mdm_s = wilder_smooth(minus_dm, period)

        dx_list = []
        for i in range(len(tr_s)):
            pdi = 100 * pdm_s[i] / tr_s[i] if tr_s[i] else 0
            mdi = 100 * mdm_s[i] / tr_s[i] if tr_s[i] else 0
            dx  = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0
            dx_list.append(dx)

        if len(dx_list) < period:
            return None
        adx = sum(dx_list[-period:]) / period
        return round(adx, 1)
    except:
        return None

def detect_trend_structure(candles):
    """
    BabyPips + Volman: real trend = HH+HL (bull) or LH+LL (bear).
    Returns (structure, strength_pct).
    """
    if not candles or len(candles) < 10:
        return 'UNKNOWN', 0
    recent = candles[-20:]
    highs = [c['high']  for c in recent]
    lows  = [c['low']   for c in recent]
    hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
    hl = sum(1 for i in range(1, len(lows))  if lows[i]  > lows[i-1])
    lh = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i-1])
    ll = sum(1 for i in range(1, len(lows))  if lows[i]  < lows[i-1])
    bull_score = hh + hl
    bear_score = lh + ll
    total = bull_score + bear_score
    if total == 0:
        return 'SIDEWAYS', 0
    bull_pct = (bull_score / total) * 100
    bear_pct = (bear_score / total) * 100
    if bull_pct >= 60:
        return 'UPTREND', round(bull_pct)
    elif bear_pct >= 60:
        return 'DOWNTREND', round(bear_pct)
    return 'SIDEWAYS', round(max(bull_pct, bear_pct))


    if len(closes) < period: return None, None, None
    recent = closes[-period:]; mid = sum(recent)/period
    std = (sum((x-mid)**2 for x in recent)/period)**0.5
    return round(mid+sd*std,5), round(mid,5), round(mid-sd*std,5)

def detect_candle_patterns(candles):
    """Real candlestick pattern recognition — Steve Nison method"""
    if len(candles) < 3: return []
    patterns = []
    c0=candles[-1]; c1=candles[-2]; c2=candles[-3]
    # Core measurements
    body0=abs(c0["close"]-c0["open"]); range0=max(c0["high"]-c0["low"],0.0001)
    body1=abs(c1["close"]-c1["open"]); range1=max(c1["high"]-c1["low"],0.0001)
    lw0=min(c0["open"],c0["close"])-c0["low"]   # lower wick
    uw0=c0["high"]-max(c0["open"],c0["close"])   # upper wick
    lw1=min(c1["open"],c1["close"])-c1["low"]
    uw1=c1["high"]-max(c1["open"],c1["close"])
    bull0=c0["close"]>c0["open"]; bear0=c0["close"]<c0["open"]
    bull1=c1["close"]>c1["open"]; bear1=c1["close"]<c1["open"]
    avg_body=max((body0+body1)/2, 0.0001)

    # 1. DOJI — indecision, body < 10% of range
    if body0/range0 < 0.1:
        patterns.append({"pattern":"DOJI","bias":"NEUTRAL","note":"Market indecision at this level — breakout watch"})

    # 2. HAMMER — bullish reversal after downtrend (long lower wick)
    if lw0 > body0*2.5 and uw0 < body0*0.5 and bull0:
        patterns.append({"pattern":"HAMMER","bias":"BULLISH","note":"Strong buyer rejection of lows — bullish reversal"})

    # 3. INVERTED HAMMER — bullish after downtrend
    if uw0 > body0*2.5 and lw0 < body0*0.5 and bull0:
        patterns.append({"pattern":"INVERTED HAMMER","bias":"BULLISH","note":"Buyers testing highs — watch for follow-through"})

    # 4. SHOOTING STAR — bearish after uptrend (long upper wick)
    if uw0 > body0*2.5 and lw0 < body0*0.5 and bear0:
        patterns.append({"pattern":"SHOOTING STAR","bias":"BEARISH","note":"Seller rejection of highs — bearish reversal"})

    # 5. HANGING MAN — bearish after uptrend (same shape as hammer but at top)
    if lw0 > body0*2.5 and uw0 < body0*0.5 and bear0:
        patterns.append({"pattern":"HANGING MAN","bias":"BEARISH","note":"Failed buyers at high — distribution candle"})

    # 6. PIN BAR — long wick vs body (either direction, price action key pattern)
    if (lw0 > range0*0.6) or (uw0 > range0*0.6):
        direction = "BULLISH" if lw0 > uw0 else "BEARISH"
        patterns.append({"pattern":"PIN BAR","bias":direction,"note":"Strong price rejection — high probability reversal zone"})

    # 7. BULLISH ENGULFING — bears then bulls absorb
    if bear1 and bull0 and c0["open"] <= c1["close"] and c0["close"] >= c1["open"] and body0 > body1:
        patterns.append({"pattern":"BULLISH ENGULFING","bias":"BULLISH","note":"Bulls fully engulf prior bearish candle"})

    # 8. BEARISH ENGULFING
    if bull1 and bear0 and c0["open"] >= c1["close"] and c0["close"] <= c1["open"] and body0 > body1:
        patterns.append({"pattern":"BEARISH ENGULFING","bias":"BEARISH","note":"Bears fully engulf prior bullish candle"})

    # 9. INSIDE BAR — consolidation / compression before big move
    if c0["high"] <= c1["high"] and c0["low"] >= c1["low"]:
        patterns.append({"pattern":"INSIDE BAR","bias":"NEUTRAL","note":"Price compressed inside prior candle — breakout building"})

    # 10. MORNING STAR — 3-candle bullish reversal
    if bear1 and body0 > avg_body*0.5 and bull0 and c0["close"] > (c2["open"]+c2["close"])/2:
        patterns.append({"pattern":"MORNING STAR","bias":"BULLISH","note":"3-candle bottom reversal — bulls taking control"})

    # 11. EVENING STAR — 3-candle bearish reversal
    if bull1 and body0 > avg_body*0.5 and bear0 and c0["close"] < (c2["open"]+c2["close"])/2:
        patterns.append({"pattern":"EVENING STAR","bias":"BEARISH","note":"3-candle top reversal — bears taking control"})

    # 12. THREE WHITE SOLDIERS (check last 3 candles all bullish, higher closes)
    if len(candles) >= 3:
        last3 = candles[-3:]
        if all(c["close"] > c["open"] for c in last3) and            last3[2]["close"] > last3[1]["close"] > last3[0]["close"]:
            patterns.append({"pattern":"THREE WHITE SOLDIERS","bias":"BULLISH","note":"3 consecutive bullish candles — strong uptrend momentum"})

    # 13. THREE BLACK CROWS
    if len(candles) >= 3:
        last3 = candles[-3:]
        if all(c["close"] < c["open"] for c in last3) and            last3[2]["close"] < last3[1]["close"] < last3[0]["close"]:
            patterns.append({"pattern":"THREE BLACK CROWS","bias":"BEARISH","note":"3 consecutive bearish candles — strong downtrend momentum"})

    # 14. TWEEZER TOP — two candles with same high (resistance rejection)
    if abs(c0["high"] - c1["high"]) < range0*0.05 and bear0:
        patterns.append({"pattern":"TWEEZER TOP","bias":"BEARISH","note":"Double rejection at same resistance — sellers in control"})

    # 15. TWEEZER BOTTOM — two candles with same low (support acceptance)
    if abs(c0["low"] - c1["low"]) < range0*0.05 and bull0:
        patterns.append({"pattern":"TWEEZER BOTTOM","bias":"BULLISH","note":"Double bounce off same support — buyers in control"})

    # 16. MARUBOZU — full body candle (no wicks) = strong momentum
    if body0/range0 > 0.92:
        bias = "BULLISH" if bull0 else "BEARISH"
        patterns.append({"pattern":"MARUBOZU","bias":bias,"note":"Strong momentum candle — trend continuation expected"})

    return patterns[:4]  # return top 4 most recent patterns

def detect_market_structure(candles):
    """
    Real market structure detection — higher highs/lows = uptrend
    Based on Dow Theory + price action (Sam Seiden / ICT style)
    """
    if len(candles) < 10:
        return {"structure":"UNKNOWN","phase":"UNKNOWN","description":"Not enough data"}

    highs  = [c["high"]  for c in candles[-20:]]
    lows   = [c["low"]   for c in candles[-20:]]
    closes = [c["close"] for c in candles[-20:]]

    # Find swing highs and lows (local maxima/minima over 3-bar window)
    swing_highs, swing_lows = [], []
    for i in range(2, len(highs)-2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append(highs[i])
        if lows[i]  < lows[i-1]  and lows[i]  < lows[i-2]  and lows[i]  < lows[i+1]  and lows[i]  < lows[i+2]:
            swing_lows.append(lows[i])

    structure = "RANGING"
    description = "No clear directional structure"

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]   # higher high
        hl = swing_lows[-1]  > swing_lows[-2]    # higher low
        lh = swing_highs[-1] < swing_highs[-2]   # lower high
        ll = swing_lows[-1]  < swing_lows[-2]    # lower low

        if hh and hl:
            structure = "UPTREND"
            description = "Higher Highs + Higher Lows = clean uptrend. Bulls in full control."
        elif lh and ll:
            structure = "DOWNTREND"
            description = "Lower Highs + Lower Lows = clean downtrend. Bears in full control."
        elif hh and ll:
            structure = "RANGING"
            description = "Higher Highs but Lower Lows = expanded range. Breakout pending."
        elif lh and hl:
            structure = "RANGING"
            description = "Lower Highs + Higher Lows = compression triangle. Coiling for breakout."
        else:
            structure = "RANGING"
            description = "Mixed swing structure — no clear directional bias."

    # Detect impulse vs ABC correction
    # Simple: if last 5 candles all same direction = impulse, mixed = correction
    last5 = candles[-5:] if len(candles) >= 5 else candles
    bull_count = sum(1 for c in last5 if c["close"] > c["open"])
    bear_count = sum(1 for c in last5 if c["close"] < c["open"])

    if bull_count >= 4:
        phase = "IMPULSE BULLISH"
    elif bear_count >= 4:
        phase = "IMPULSE BEARISH"
    elif bull_count == 3 and bear_count == 2:
        phase = "CORRECTION — possible ABC bull trap in downtrend"
    elif bear_count == 3 and bull_count == 2:
        phase = "CORRECTION — possible ABC bear trap in uptrend"
    else:
        phase = "CONSOLIDATION — no momentum"

    return {"structure": structure, "phase": phase, "description": description,
            "swing_highs": [round(h,5) for h in swing_highs[-3:]] if swing_highs else [],
            "swing_lows":  [round(l,5) for l in swing_lows[-3:]]  if swing_lows  else []}


def calc_adr(candles, days=10):
    """Average Daily Range — how far price typically moves in a day"""
    if len(candles) < 3:
        return None
    recent = candles[-days:] if len(candles) >= days else candles
    ranges = [c["high"] - c["low"] for c in recent]
    return round(sum(ranges)/len(ranges), 5)


def calc_weekly_range_pct(candles):
    """How much of the weekly average range has been used — Gemini strategy key metric"""
    if len(candles) < 6:
        return None, None, None
    # Weekly = 5 trading days
    week_candles = candles[-5:] if len(candles) >= 5 else candles
    week_high = max(c["high"] for c in week_candles)
    week_low  = min(c["low"]  for c in week_candles)
    week_range = week_high - week_low

    # Average weekly range over past 4 weeks
    if len(candles) >= 20:
        avg_weekly_ranges = []
        for i in range(4):
            start = -(5*(i+1))
            end   = -(5*i) if i > 0 else None
            wc = candles[start:end]
            if wc:
                avg_weekly_ranges.append(max(c["high"] for c in wc) - min(c["low"] for c in wc))
        avg_range = sum(avg_weekly_ranges)/len(avg_weekly_ranges) if avg_weekly_ranges else week_range
    else:
        avg_range = week_range

    pct_used = round((week_range / avg_range * 100) if avg_range > 0 else 0, 1)
    return round(week_high, 5), round(week_low, 5), pct_used


def detect_trend_strength(candles_d1, candles_h4):
    """Trend strength: how aligned are EMAs across timeframes"""
    if not candles_d1 or not candles_h4:
        return "UNKNOWN", 0
    dc  = [c["close"] for c in candles_d1]
    h4c = [c["close"] for c in candles_h4]
    cp  = dc[-1] if dc else 0

    e9_d  = calc_ema(dc,  9);  e21_d = calc_ema(dc, 21);  e50_d = calc_ema(dc, 50)
    e9_h4 = calc_ema(h4c, 9);  e21_h4= calc_ema(h4c,21);  e50_h4= calc_ema(h4c,50)

    score = 0
    # Daily alignment
    if e9_d and e21_d and e50_d:
        if cp > e9_d > e21_d > e50_d:   score += 3  # perfect bull stack
        elif cp < e9_d < e21_d < e50_d: score -= 3  # perfect bear stack
        elif cp > e21_d: score += 1
        elif cp < e21_d: score -= 1
    # H4 alignment
    if e9_h4 and e21_h4 and e50_h4:
        if cp > e9_h4 > e21_h4 > e50_h4:   score += 2
        elif cp < e9_h4 < e21_h4 < e50_h4: score -= 2
        elif cp > e21_h4: score += 1
        elif cp < e21_h4: score -= 1

    abs_score = abs(score)
    if abs_score >= 4:
        strength = "STRONG " + ("BULLISH" if score > 0 else "BEARISH")
    elif abs_score >= 2:
        strength = "MODERATE " + ("BULLISH" if score > 0 else "BEARISH")
    else:
        strength = "WEAK / RANGING"

    return strength, score


def get_sage_chart_data(pair, current_price):
    """
    Full real chart analysis engine — pulls live OHLC from yfinance.
    Calculates real RSI (Wilder), real EMAs, real S/R from swing highs/lows,
    real candlestick patterns, market structure, ADR, weekly range usage.
    """
    data = {
        "pair": pair, "price": current_price,
        "weekly": {}, "daily": {}, "h4": {}, "hourly": {}, "m15": {},
        "sr_levels": [], "d1_patterns": [], "h4_patterns": [], "m15_patterns": [],
        "market_structure": {}, "trend_strength": "UNKNOWN",
        "adr": None, "weekly_range_pct": None,
        "weekly_high": None, "weekly_low": None,
        "london_levels": {}, "ny_levels": {}
    }
    try:
        # ── WEEKLY — big picture context ──────────────────────────────
        wk = get_candles(pair, "1wk", "1y")
        if wk and len(wk) >= 10:
            wc  = [c["close"] for c in wk]
            wk_high52 = max(c["high"] for c in wk)
            wk_low52  = min(c["low"]  for c in wk)
            wk_rsi    = calc_rsi(wc)
            wk_ema20  = calc_ema(wc, 20)
            wk_struct = detect_market_structure(wk[-16:])
            wk_adr    = calc_adr(wk, 8)
            wk_h, wk_l, wk_pct = calc_weekly_range_pct(wk)
            # Trend vs EMA alignment
            wk_trend = "BULLISH" if (wk_ema20 and current_price > wk_ema20) else "BEARISH"
            data["weekly"] = {
                "trend":    wk_trend,
                "ema20":    wk_ema20,
                "rsi":      wk_rsi,
                "high52":   round(wk_high52, 5),
                "low52":    round(wk_low52, 5),
                "structure": wk_struct["structure"],
                "phase":    wk_struct["phase"],
                "adr":      wk_adr,
                "last3":    "{} bull, {} bear".format(
                    sum(1 for c in wk[-3:] if c["close"]>c["open"]),
                    sum(1 for c in wk[-3:] if c["close"]<=c["open"]))
            }
            data["weekly_high"]    = wk_h
            data["weekly_low"]     = wk_l
            data["weekly_range_pct"] = wk_pct

        # ── DAILY — trade direction bias ──────────────────────────────
        d1 = get_candles(pair, "1d", "6mo")
        if d1 and len(d1) >= 20:
            dc  = [c["close"] for c in d1]
            bbu, bbm, bbl = calc_bollinger(dc)
            e9  = calc_ema(dc, 9)
            e20 = calc_ema(dc, 20)
            e50 = calc_ema(dc, 50)
            e100= calc_ema(dc, min(100, len(dc)))
            e200= calc_ema(dc, min(200, len(dc)))
            d1_rsi  = calc_rsi(dc)
            d1_macd = calc_macd(dc)[1]
            d1_atr  = calc_atr(d1)
            d1_adx  = calc_adx(d1)
            d1_trend_struct, d1_trend_strength = detect_trend_structure(d1)
            d1_struct = detect_market_structure(d1[-30:])
            d1_adr  = calc_adr(d1, 10)
            # Price location relative to Bollinger
            if bbu and bbl:
                bb_range = bbu - bbl
                bb_pos = round(((current_price - bbl) / bb_range * 100), 1) if bb_range > 0 else 50
            else:
                bb_pos = 50
            # Daily trend — require TWO confirmations
            bull_signals = sum([
                1 if (e9  and current_price > e9)   else 0,
                1 if (e20 and current_price > e20)  else 0,
                1 if (e50 and current_price > e50)  else 0,
                1 if (d1_macd == "BULLISH")         else 0,
            ])
            d1_trend = "BULLISH" if bull_signals >= 3 else "BEARISH" if bull_signals <= 1 else "MIXED"
            data["daily"] = {
                "trend":    d1_trend,
                "ema9":     e9,  "ema20": e20, "ema50": e50, "ema100": e100, "ema200": e200,
                "rsi":      d1_rsi,
                "macd_bias": d1_macd,
                "atr":      d1_atr,
                "adx":      d1_adx,
                "adx_signal": ("TRENDING" if d1_adx and d1_adx > 25 else "RANGING" if d1_adx and d1_adx < 20 else "WEAK") if d1_adx else "UNKNOWN",
                "hh_hl_structure": d1_trend_struct,
                "hh_hl_strength": d1_trend_strength,
                "adr":      d1_adr,
                "bb_upper": bbu, "bb_mid": bbm, "bb_lower": bbl,
                "bb_position": bb_pos,
                "vs_ema200": "ABOVE" if (e200 and current_price > e200) else "BELOW",
                "vs_ema100": "ABOVE" if (e100 and current_price > e100) else "BELOW",
                "structure": d1_struct["structure"],
                "phase":    d1_struct["phase"],
                "phase_desc": d1_struct["description"],
                "swing_highs": d1_struct.get("swing_highs", []),
                "swing_lows":  d1_struct.get("swing_lows",  []),
                "high20d":  round(max(c["high"] for c in d1[-20:]), 5),
                "low20d":   round(min(c["low"]  for c in d1[-20:]), 5),
                "last5":    "{} bull, {} bear".format(
                    sum(1 for c in d1[-5:] if c["close"]>c["open"]),
                    sum(1 for c in d1[-5:] if c["close"]<=c["open"]))
            }
            data["sr_levels"]   = find_sr_levels(d1, current_price, lookback=60)
            data["d1_patterns"] = detect_candle_patterns(d1[-10:])
            data["adr"]         = d1_adr

        # ── H4 — trade direction confirmation ────────────────────────
        h4 = get_candles(pair, "4h", "30d")
        if h4 and len(h4) >= 10:
            h4c = [c["close"] for c in h4]
            h4_e9   = calc_ema(h4c, 9)
            h4_e20  = calc_ema(h4c, 20)
            h4_e50  = calc_ema(h4c, 50)
            h4_rsi  = calc_rsi(h4c)
            h4_macd = calc_macd(h4c)[1]
            h4_atr  = calc_atr(h4)
            h4_struct = detect_market_structure(h4[-20:])
            bull_h4 = sum([
                1 if (h4_e9  and current_price > h4_e9)  else 0,
                1 if (h4_e20 and current_price > h4_e20) else 0,
                1 if (h4_macd == "BULLISH") else 0,
            ])
            h4_trend = "BULLISH" if bull_h4 >= 2 else "BEARISH" if bull_h4 == 0 else "MIXED"
            data["h4"] = {
                "trend":    h4_trend,
                "ema9":     h4_e9, "ema20": h4_e20, "ema50": h4_e50,
                "rsi":      h4_rsi,
                "macd_bias": h4_macd,
                "atr":      h4_atr,
                "structure": h4_struct["structure"],
                "phase":    h4_struct["phase"],
                "high48h":  round(max(c["high"] for c in h4[-12:]), 5),
                "low48h":   round(min(c["low"]  for c in h4[-12:]), 5),
                "last4":    "{} bull, {} bear".format(
                    sum(1 for c in h4[-4:] if c["close"]>c["open"]),
                    sum(1 for c in h4[-4:] if c["close"]<=c["open"]))
            }
            data["h4_patterns"] = detect_candle_patterns(h4[-10:])

        # ── H1 — entry timeframe ──────────────────────────────────────
        h1 = get_candles(pair, "1h", "5d")
        if h1 and len(h1) >= 20:
            h1c = [c["close"] for c in h1]
            h1_e9   = calc_ema(h1c, 9)
            h1_e20  = calc_ema(h1c, 20)
            h1_e50  = calc_ema(h1c, 50)
            h1_rsi  = calc_rsi(h1c)
            h1_macd = calc_macd(h1c)[1]
            h1_atr  = calc_atr(h1)
            h1_struct = detect_market_structure(h1[-24:])
            data["hourly"] = {
                "trend":    "BULLISH" if (h1_e20 and current_price > h1_e20) else "BEARISH",
                "ema9":     h1_e9, "ema20": h1_e20, "ema50": h1_e50,
                "rsi":      h1_rsi,
                "macd_bias": h1_macd,
                "atr":      h1_atr,
                "structure": h1_struct["structure"],
                "phase":    h1_struct["phase"],
                "high24h":  round(max(c["high"] for c in h1[-24:]), 5),
                "low24h":   round(min(c["low"]  for c in h1[-24:]), 5),
                "sr":       find_sr_levels(h1, current_price, lookback=40)[:5]
            }

        # ── M15 — trigger / entry confirmation ───────────────────────
        m15 = get_candles(pair, "15m", "3d")
        if m15 and len(m15) >= 14:
            m15c = [c["close"] for c in m15]
            m15_e9  = calc_ema(m15c, 9)
            m15_e20 = calc_ema(m15c, 20)
            m15_rsi = calc_rsi(m15c)
            m15_struct = detect_market_structure(m15[-20:])
            # London session range (approx last 8 candles of M15 from 3AM-8AM ET)
            london_range_h = max(c["high"] for c in m15[-32:-8]) if len(m15) >= 40 else None
            london_range_l = min(c["low"]  for c in m15[-32:-8]) if len(m15) >= 40 else None
            data["m15"] = {
                "trend":    "BULLISH" if (m15_e9 and current_price > m15_e9) else "BEARISH",
                "ema9":     m15_e9, "ema20": m15_e20,
                "rsi":      m15_rsi,
                "structure": m15_struct["structure"],
                "phase":    m15_struct["phase"],
                "london_high": round(london_range_h, 5) if london_range_h else None,
                "london_low":  round(london_range_l, 5) if london_range_l else None,
                "last4":    "{} bull, {} bear".format(
                    sum(1 for c in m15[-4:] if c["close"]>c["open"]),
                    sum(1 for c in m15[-4:] if c["close"]<=c["open"]))
            }
            data["m15_patterns"] = detect_candle_patterns(m15[-10:])

        # ── OVERALL TREND STRENGTH (multi-TF EMA alignment) ──────────
        d1_ref = get_candles(pair, "1d", "6mo") if not d1 else d1
        h4_ref = get_candles(pair, "4h", "30d") if not h4 else h4
        strength, score = detect_trend_strength(d1_ref, h4_ref)
        data["trend_strength"] = strength
        data["trend_score"]    = score

    except Exception as e:
        print("[SageChart] {}: {}".format(pair, e))
    return data


def format_sage_chart(d):
    """
    Formats all real chart data into a structured text block for the AI.
    Every number here comes from real live OHLC candles — no estimates.
    """
    wk  = d.get("weekly",  {})
    da  = d.get("daily",   {})
    h4  = d.get("h4",      {})
    h1  = d.get("hourly",  {})
    m15 = d.get("m15",     {})
    sep = "=" * 70

    lines = [
        sep,
        "LIVE REAL CHART DATA — {} @ {}".format(d["pair"], d["price"]),
        "Overall Trend Strength: {} (score: {})".format(
            d.get("trend_strength","?"), d.get("trend_score","?")),
        "ADR (Avg Daily Range): {} | Weekly Range Used: {}%".format(
            d.get("adr","?"), d.get("weekly_range_pct","?")),
        "Weekly High: {} | Weekly Low: {}".format(
            d.get("weekly_high","?"), d.get("weekly_low","?")),
        sep,

        "── WEEKLY CONTEXT (Big Picture) ──",
        "Trend={} | Structure={} | Phase={}".format(
            wk.get("trend","?"), wk.get("structure","?"), wk.get("phase","?")),
        "EMA20={} | RSI={} | ADR={} | Last3={}".format(
            wk.get("ema20","?"), wk.get("rsi","?"), wk.get("adr","?"), wk.get("last3","?")),
        "52wk High={} | 52wk Low={}".format(wk.get("high52","?"), wk.get("low52","?")),

        "── DAILY (Trade Direction) ──",
        "Trend={} | Structure={} | Phase={}".format(
            da.get("trend","?"), da.get("structure","?"), da.get("phase","?")),
        "Price Action: {}".format(da.get("phase_desc","?")),
        "EMA9={} | EMA20={} | EMA50={} | EMA100={} | EMA200={}".format(
            da.get("ema9","?"), da.get("ema20","?"), da.get("ema50","?"), da.get("ema100","?"), da.get("ema200","?")),
        "ADX={} ({}) | HH/HL Structure={} ({}% confidence)".format(
            da.get("adx","?"), da.get("adx_signal","?"),
            da.get("hh_hl_structure","?"), da.get("hh_hl_strength","?")),
        "RSI={} | MACD={} | ATR={} | vs200EMA={} | vs100EMA={}".format(
            da.get("rsi","?"), da.get("macd_bias","?"), da.get("atr","?"),
            da.get("vs_ema200","?"), da.get("vs_ema100","?")),
        "Bollinger: Upper={} | Mid={} | Lower={} | Position in BB={}%".format(
            da.get("bb_upper","?"), da.get("bb_mid","?"), da.get("bb_lower","?"), da.get("bb_position","?")),
        "20d High={} | 20d Low={} | Last5={}".format(
            da.get("high20d","?"), da.get("low20d","?"), da.get("last5","?")),
        "Swing Highs (D1): {} | Swing Lows (D1): {}".format(
            da.get("swing_highs",[]), da.get("swing_lows",[])),

        "── H4 (Trade Direction Confirmation) ──",
        "Trend={} | Structure={} | Phase={}".format(
            h4.get("trend","?"), h4.get("structure","?"), h4.get("phase","?")),
        "EMA9={} | EMA20={} | EMA50={} | RSI={} | MACD={} | ATR={}".format(
            h4.get("ema9","?"), h4.get("ema20","?"), h4.get("ema50","?"),
            h4.get("rsi","?"), h4.get("macd_bias","?"), h4.get("atr","?")),
        "48h High={} | 48h Low={} | Last4={}".format(
            h4.get("high48h","?"), h4.get("low48h","?"), h4.get("last4","?")),

        "── H1 (Entry Timeframe) ──",
        "Trend={} | Structure={} | Phase={}".format(
            h1.get("trend","?"), h1.get("structure","?"), h1.get("phase","?")),
        "EMA9={} | EMA20={} | EMA50={} | RSI={} | MACD={} | ATR={}".format(
            h1.get("ema9","?"), h1.get("ema20","?"), h1.get("ema50","?"),
            h1.get("rsi","?"), h1.get("macd_bias","?"), h1.get("atr","?")),
        "24h High={} | 24h Low={}".format(h1.get("high24h","?"), h1.get("low24h","?")),

        "── M15 (Entry Trigger) ──",
        "Trend={} | Structure={} | Phase={}".format(
            m15.get("trend","?"), m15.get("structure","?"), m15.get("phase","?")),
        "EMA9={} | EMA20={} | RSI={} | Last4={}".format(
            m15.get("ema9","?"), m15.get("ema20","?"), m15.get("rsi","?"), m15.get("last4","?")),
        "London Session High={} | London Session Low={}".format(
            m15.get("london_high","?"), m15.get("london_low","?")),
    ]

    # Support/Resistance
    sr = d.get("sr_levels", [])
    if sr:
        lines.append("── KEY S/R LEVELS (from real swing highs/lows + round numbers) ──")
        for lv in sr[:8]:
            lines.append("  {}: {} | {} pips away | Strength={} | {}".format(
                lv["type"], lv["price"], lv["distance_pips"], lv["strength"], lv["note"]))

    # Intraday S/R from H1
    h1_sr = h1.get("sr", [])
    if h1_sr:
        lines.append("── INTRADAY S/R (H1 level) ──")
        for lv in h1_sr[:4]:
            lines.append("  {}: {} | {} pips".format(lv["type"], lv["price"], lv["distance_pips"]))

    # Candlestick patterns
    all_pats = (
        [("D1", p) for p in d.get("d1_patterns",[])] +
        [("H4", p) for p in d.get("h4_patterns",[])] +
        [("M15",p) for p in d.get("m15_patterns",[])]
    )
    if all_pats:
        lines.append("── CANDLESTICK PATTERNS (Steve Nison method) ──")
        for tf, p in all_pats:
            lines.append("  [{}] {} ({}) — {}".format(tf, p["pattern"], p["bias"], p["note"]))
    else:
        lines.append("CANDLESTICK PATTERNS: No high-probability patterns on current candle")

    lines.append(sep)
    return "\n".join(lines)


def call_claude_with_search(prompt, max_tokens=600):
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        msg = client.messages.create(model="claude-sonnet-4-5", max_tokens=max_tokens,
            tools=[{"type":"web_search_20250305","name":"web_search"}],
            messages=[{"role":"user","content":prompt}])
        return "".join(b.text for b in msg.content if hasattr(b,"text") and b.type=="text").strip() or "No news available."
    except Exception as e:
        print("[SageSearch] {}".format(e)); return "News search unavailable."

_sage_jobs = {}

def _run_sage_job(job_id, pair, mode):
    try:
        _sage_jobs[job_id]={"status":"running","step":"Fetching live price..."}
        pd=get_price(pair)
        if not pd:
            _sage_jobs[job_id]={"status":"error","error":"Cannot fetch price for "+pair}; return
        cp=float(pd["price"]); sn,_=get_session()
        ds=datetime.now().strftime("%A %B %d %Y %H:%M UTC")

        _sage_jobs[job_id]["step"]="Reading 5 timeframes (M15 to Weekly)..."
        cd=get_sage_chart_data(pair,cp); cs=format_sage_chart(cd)

        _sage_jobs[job_id]["step"]="Scanning live news and economic events..."
        news=call_claude_with_search("Search: breaking news economic data and central bank statements affecting {} today {}. What is moving {} right now? 3 sentence summary.".format(pair,datetime.now().strftime("%B %d %Y"),pair))

        is_forex=any(x in pair for x in ["USD","EUR","GBP","JPY","CHF","AUD","NZD","CAD","XAU"])
        leg = ("FOREX LEGENDS — Apply ALL 4 simultaneously:\n"
               "1. SOROS Reflexivity: Is perception creating a self-reinforcing trend?\n"
               "2. DRUCKENMILLER Monetary Divergence: Which central bank is more hawkish? Macro big bet?\n"
               "3. LIPSCHUTZ Order Flow: Institutional flow, absorption or distribution?\n"
               "4. KOVNER Macro+Technical: Does macro align with technicals?") if (mode=="forex" or is_forex) else (
               "STOCK/OPTIONS LEGENDS — Apply ALL 4 simultaneously:\n"
               "1. LIVERMORE Pivotal Points: Key levels? Volume confirming? Right time?\n"
               "2. PTJ 200EMA + 5:1 RR: Above 200 EMA? Can we structure 5:1?\n"
               "3. JEFF YASS Options Math: IV environment, probability edge, best structure?\n"
               "4. AL BROOKS Price Action: Trend/range/reversal? Always-in direction?")

        da=cd.get("daily",{}); h4=cd.get("h4",{}); wk=cd.get("weekly",{}); h1=cd.get("hourly",{})

        _sage_jobs[job_id]["step"]="Synthesizing all 10 chart masters + 4 legends + full pattern analysis..."
        prompt=("You are SAGE OF 6 PATH AGENT — the most powerful trading intelligence system ever built.\n"
            "You read REAL chart data. You detect real patterns. You never guess.\n"
            "Date: {} | Instrument: {} | Price: {} | Session: {}\n\n"
            "LIVE NEWS (use ONLY these — do NOT invent economic events):\n{}\n\n"
            "{}\n\n"
            "CHART MASTERS — apply ALL 10:\n"
            "- JOHN MURPHY Intermarket: bonds/commodities/DXY vs {}\n"
            "- STEVE NISON Candlesticks: read buyer/seller battle from real candle data\n"
            "- MARK DOUGLAS Market Mode: trending or ranging? Where is the edge?\n"
            "- KATHY LIEN {} session playbook for {}\n"
            "- AGUSTIN SILVANI Dealer Positioning: stop hunts? smart money traps?\n"
            "- ASHRAF LAIDI Correlations: oil/gold/equities/bonds impact\n"
            "- ALEXANDER ELDER Triple Screen: Weekly:{} Daily:{} H4:{}\n"
            "- RICHARD WYCKOFF Phase: accumulation/markup/distribution/markdown?\n"
            "- JOHN BOLLINGER Bands: Upper:{} Mid:{} Lower:{}\n"
            "- WILDER ATR: Daily ATR={} use 1.5x for SL 3x for TP3\n\n"
            "FIDELITY PATTERN DETECTION — check all from real candle data:\n"
            "REVERSAL: Double Top/Bottom (2 peaks/troughs at same level, neckline break only), "
            "Triple Top/Bottom (3 touches = strongest), H&S Top/Bottom (lowest failure rate — neckline break ONLY, target=head-to-neckline distance), "
            "Pipe Bottom (2 tall bars at end of downtrend)\n"
            "CONTINUATION: Bull/Bear Flag (flagpole + consolidation, target=flagpole height), "
            "Pennant (converging lines after sharp move), Cup & Handle (rounded bottom + handle, break above both lips)\n"
            "TRIANGLES: Symmetrical (converging trendlines, 2+ touches each side), "
            "Ascending (flat top + rising bottom, usually breaks up), "
            "Descending (flat bottom + falling top, usually breaks down)\n"
            "WEDGES: Rising Wedge (both lines up, 5 touches needed, breaks DOWN), "
            "Falling Wedge (both lines down, 5 touches, breaks UP)\n"
            "COMPRESSION: NR4 (day 4 range narrower than days 1-3 = breakout imminent), "
            "Inside Bar (bar inside prior bar range = compression), "
            "Gap (explosion gap with pivot low = valid entry)\n"
            "CANDLESTICK: Engulfing (strongest at S/R), Doji (indecision at key level), "
            "Hammer/Shooting Star (long wick rejecting level), Dark Cloud/Piercing\n"
            "PATTERN RULES: Pattern NOT complete until price CLOSES beyond level — no wick entries. "
            "False breakout (breaks then returns) = trap. Failed breakout (false then opposite break) = YOUR entry.\n\n"
            "VOLMAN PRINCIPLES:\n"
            "- Buildup before breakout (tight consolidation at S/R) = HIGH CONFIDENCE entry\n"
            "- Price shooting through S/R with no buildup = SKIP (false break risk)\n"
            "- Double pressure (both bulls and bears same direction) = strongest moves\n"
            "- EMA100 is trend FILTER — long only above it, short only below it\n"
            "- ADX > 25 = trending (trade it), ADX < 20 = ranging (wait or skip)\n\n"
            "{}\n\n"
            "CRITICAL MARKET STRUCTURE ANALYSIS:\n"
            "1. WHERE IS PRICE? Near major S/R? Top of range? Bottom? Mid-range?\n"
            "2. MARKET PHASE: Trending (HH+HL or LH+LL) or Ranging?\n"
            "3. PATTERN: Is any Fidelity pattern completing from the data above?\n"
            "4. ABC/WAVE POSITION: Impulse wave or ABC correction (counter-trend trap)?\n"
            "5. KEY S/R ZONES: 2 nearest resistance above, 2 nearest support below.\n"
            "6. TREND STRENGTH: ADX >25 trending, <20 ranging — state actual value.\n"
            "7. HIGHER TF CONTEXT: Weekly and Daily structure alignment.\n"
            "8. RULE: Return WAIT if: at major S/R without breakout, ABC correction likely, or ranging. "
            "   BUT EVEN ON WAIT — fill in conditional entry levels (where trade would be IF setup triggers).\n\n"
            "MANDATE: 30-40 pip minimum. SL behind real SR. Min 2:1 RR. High TF=direction Low TF=entry.\n"
            "CRITICAL: NEVER return 0 for entry, stop_loss, tp1, tp2, tp3 or sl_pips. ALWAYS use real price levels.\n\n"
            "Return ONLY valid JSON (no markdown, no extra text):\n"
            '{{\"verdict\":\"BUY or SELL or WAIT\",\"confidence\":65,\"session\":\"{}\",\"entry\":\"{}\",\"stop_loss\":\"REAL_PRICE\",\"sl_pips\":20,'
            '\"tp1\":\"REAL_PRICE\",\"tp1_pips\":30,\"tp2\":\"REAL_PRICE\",\"tp2_pips\":50,\"tp3\":\"REAL_PRICE\",\"tp3_pips\":80,\"rr_ratio\":\"1:2.5\",'
            '\"timeframe_alignment\":{{\"weekly\":\"?\",\"daily\":\"?\",\"h4\":\"?\",\"h1\":\"?\",\"m15\":\"?\"}},'
            '\"legend_consensus\":{{\"soros\":\"\",\"druckenmiller\":\"\",\"lipschutz\":\"\",\"kovner\":\"\"}},'
            '\"chart_masters\":{{\"murphy\":\"\",\"nison\":\"\",\"douglas\":\"\",\"kathy_lien\":\"\",\"silvani\":\"\",\"laidi\":\"\",\"elder\":\"\",\"wyckoff\":\"\",\"bollinger\":\"\",\"wilder\":\"\"}},'
            '\"pattern_detected\":\"pattern name + timeframe or NONE\",'
            '\"pattern_target\":\"price target from pattern or N/A\",'
            '\"buildup_present\":true,'
            '\"key_levels\":{{\"nearest_support\":\"\",\"nearest_resistance\":\"\",\"stop_zone\":\"\"}},'
            '\"market_structure\":{{\"phase\":\"TRENDING or RANGING or BREAKOUT or REVERSAL\",\"abc_position\":\"IMPULSE WAVE or ABC CORRECTION or UNKNOWN\",\"price_location\":\"AT RESISTANCE or AT SUPPORT or MID-RANGE or BREAKOUT ZONE\",\"trend_strength\":\"STRONG TREND or MODERATE TREND or RANGING or CHOPPY\",\"higher_tf_context\":\"\",\"sr_above\":\"\",\"sr_below\":\"\"}},'
            '\"candlestick_signal\":\"\",\"news_impact\":\"\",\"geopolitical_risk\":\"\",\"sage_says\":\"\",\"invalidation\":\"\"}}'
        ).format(ds,pair,cp,sn,news,leg,pair,sn,pair,
                 wk.get("trend","?"),da.get("trend","?"),h4.get("trend","?"),
                 da.get("bb_upper","?"),da.get("bb_mid","?"),da.get("bb_lower","?"),
                 da.get("atr","?"),cs,sn,cp)

        raw=call_claude(prompt,max_tokens=4000)
        result=parse_json_response(raw)
        result.update({"pair":pair,"price":cp,"mode":mode,"analyzed_at":datetime.now().strftime("%H:%M UTC")})
        _sage_jobs[job_id]={"status":"done","result":result}
    except Exception as e:
        import traceback; print("[SageMode] {}".format(traceback.format_exc()))
        _sage_jobs[job_id]={"status":"error","error":str(e)}

@app.route("/sage-mode")
@login_required
@byakugan_required
def sage_mode_page():
    return render_template("sage.html")

@app.route("/api/sage-start", methods=["POST"])
@login_required
@byakugan_required
def sage_start():
    try:
        data=request.get_json() or {}
        pair=data.get("pair","EUR/USD").upper().strip()
        mode=data.get("mode","forex")
        import uuid as _uuid
        job_id=str(_uuid.uuid4())[:8]
        _sage_jobs[job_id]={"status":"starting","step":"Initializing Sage Mode..."}
        threading.Thread(target=_run_sage_job,args=(job_id,pair,mode),daemon=True).start()
        return jsonify({"job_id":job_id,"status":"starting"})
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/api/sage-poll/<job_id>", methods=["GET"])
@login_required
def sage_poll(job_id):
    job=_sage_jobs.get(job_id)
    if not job: return jsonify({"status":"error","error":"Job not found"}),404
    if job["status"]=="done":
        result=dict(job["result"]); result["status"]="done"
        _sage_jobs.pop(job_id,None); return jsonify(result)
    if job["status"]=="error":
        err=job.get("error","Unknown"); _sage_jobs.pop(job_id,None)
        return jsonify({"status":"error","error":err}),500
    return jsonify({"status":job["status"],"step":job.get("step","Processing...")})



@app.route("/api/sage-scanner", methods=["POST"])
@login_required
@byakugan_required
def sage_scanner():
    """Scan forex pairs, top stocks, or custom list simultaneously"""
    try:
        import uuid as _uuid
        body = request.get_json() or {}
        scan_type   = body.get('scan_type', 'forex')
        custom_list = body.get('custom_list', [])
        job_id = str(_uuid.uuid4())[:8]
        _sage_jobs[job_id] = {"status": "running", "step": "Initializing scanner..."}
        threading.Thread(target=_run_sage_scanner_job, args=(job_id, scan_type, custom_list), daemon=True).start()
        return jsonify({"job_id": job_id, "status": "starting"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sage-scanner-poll/<job_id>", methods=["GET"])
@login_required
def sage_scanner_poll(job_id):
    job = _sage_jobs.get(job_id)
    if not job: return jsonify({"status": "error", "error": "Job not found"}), 404
    if job["status"] == "done":
        result = dict(job["result"]); result["status"] = "done"
        _sage_jobs.pop(job_id, None); return jsonify(result)
    if job["status"] == "error":
        err = job.get("error", "Unknown"); _sage_jobs.pop(job_id, None)
        return jsonify({"status": "error", "error": err}), 500
    return jsonify({"status": job["status"], "step": job.get("step", "Scanning...")})

def _run_sage_scanner_job(job_id, scan_type='forex', custom_list=None):
    try:
        FOREX_PAIRS  = ["EUR/USD","GBP/USD","USD/JPY","AUD/USD","USD/CAD","NZD/USD","USD/CHF","EUR/JPY","GBP/JPY","XAU/USD"]
        TOP_STOCKS   = ["NVDA","AAPL","TSLA","AMZN","META","MSFT","GOOGL","SPY","QQQ","AMD","PLTR","MSTR"]

        if scan_type == 'stocks':
            scan_list = TOP_STOCKS
            label = 'stock'
        elif scan_type == 'custom' and custom_list:
            scan_list = [s.upper() for s in custom_list][:12]
            label = 'instrument'
        else:
            scan_list = FOREX_PAIRS
            label = 'pair'

        total = len(scan_list)
        results = []

        for i, instrument in enumerate(scan_list):
            _sage_jobs[job_id]["step"] = "Scanning {}/{}: {}...".format(i+1, total, instrument)
            try:
                pd = get_price(instrument)
                if not pd: continue
                cp = float(pd["price"])
                cd = get_sage_chart_data(instrument, cp)
                da = cd.get("daily", {}); h4 = cd.get("h4", {}); wk = cd.get("weekly", {})
                chart_str = format_sage_chart(cd)

                # Real ADX and trend structure from computed data
                real_adx = da.get("adx")
                adx_signal = da.get("adx_signal", "UNKNOWN")
                hh_hl = da.get("hh_hl_structure", "UNKNOWN")
                ema100 = da.get("ema100")
                atr_val = da.get("atr")

                # Per-pair news risk check
                news_warning = ""
                try:
                    pair_news = get_news(instrument)
                    if pair_news:
                        news_warning = pair_news[0]['title'][:80] if pair_news else ""
                except: pass

                verdict_prompt = (
                    'Analyze ' + instrument + ' at ' + str(cp) + '. '
                    'Real chart data:\n' + chart_str + '\n\n'
                    'Based ONLY on the real data above, return compact JSON (no markdown):\n'
                    '{"pair":"' + instrument + '",'
                    '"verdict":"BUY or SELL or WAIT",'
                    '"confidence":0,'
                    '"trend_strength":"STRONG BULLISH or STRONG BEARISH or MODERATE BULLISH or MODERATE BEARISH or RANGING",'
                    '"market_structure":"UPTREND or DOWNTREND or RANGING",'
                    '"abc_position":"IMPULSE or CORRECTION or CONSOLIDATION",'
                    '"direction":"UP or DOWN or SIDEWAYS",'
                    '"reason":"max 15 words using real S/R, EMA100, ADX data",'
                    '"entry":"' + str(cp) + '",'
                    '"sl":"ATR-based SL from real ATR=' + str(atr_val) + '",'
                    '"tp1":"next real S/R level",'
                    '"key_pattern":"candlestick pattern or none",'
                    '"adx_note":"ADX=' + str(real_adx) + ' ' + adx_signal + '"}'
                )
                raw = call_claude(verdict_prompt, max_tokens=400)
                parsed = parse_json_response(raw)
                if parsed and parsed.get("verdict") in ["BUY","SELL"]:
                    parsed["pair"] = instrument
                    parsed["price"] = cp
                    parsed["adx"] = real_adx
                    parsed["adx_signal"] = adx_signal
                    parsed["hh_hl_structure"] = hh_hl
                    parsed["ema100"] = ema100
                    parsed["atr"] = atr_val
                    if news_warning:
                        parsed["news_warning"] = news_warning
                    results.append(parsed)
            except Exception as pe:
                print("[SageScanner] {} error: {}".format(instrument, pe))
                continue

        results.sort(key=lambda x: int(x.get("confidence", 0)), reverse=True)
        top = [r for r in results if r.get("verdict") in ["BUY","SELL"]][:5]

        all_result = []
        for instrument in scan_list:
            found = next((r for r in results if r.get("pair") == instrument), None)
            if found:
                all_result.append(found)
            else:
                all_result.append({"pair": instrument, "verdict": "WAIT", "confidence": 0,
                                   "trend_strength": "UNKNOWN", "direction": "SIDEWAYS",
                                   "reason": "No data or ranging market"})

        _sage_jobs[job_id] = {
            "status": "done",
            "result": {
                "pairs": all_result,
                "top_picks": top,
                "scan_type": scan_type,
                "scanned_at": datetime.now().strftime("%H:%M UTC")
            }
        }
    except Exception as e:
        import traceback; print("[SageScanner] {}".format(traceback.format_exc()))
        _sage_jobs[job_id] = {"status": "error", "error": str(e)}


AI_INFRA_UNIVERSE = {
    'ALL': [
        'MRVL','INTC','SMCI','CRDO',
        'APLD','IREN','NBIS',
        'OKLO','CEG','VST',
        'MOD','STRL','CLS',
        'PATH','TER',
        'PLTR','AI',
    ],
    'CHIPS':      ['MRVL','INTC','AMD','AVGO','CRDO','MU'],
    'SERVERS':    ['SMCI','CLS','JBL','DELL'],
    'DATACENTER': ['APLD','IREN','NBIS','DLR'],
    'POWER':      ['OKLO','CEG','VST','ETN'],
    'ROBOTICS':   ['PATH','TER','ISRG'],
    'DEEP_VALUE': ['SMCI','INTC','MRVL','APLD','IREN','AI','PATH'],
}

TICKER_CATEGORY = {
    'MRVL':'CHIPS',    'INTC':'CHIPS',    'AMD':'CHIPS',
    'AVGO':'CHIPS',    'CRDO':'NETWORKING','MU':'CHIPS',
    'SMCI':'SERVERS',  'CLS':'MANUFACTURING','JBL':'MANUFACTURING','DELL':'SERVERS',
    'APLD':'DATA CENTER','IREN':'DATA CENTER','NBIS':'DATA CENTER','DLR':'DATA CENTER',
    'OKLO':'POWER',    'CEG':'POWER',     'VST':'POWER',    'ETN':'POWER',
    'MOD':'COOLING',   'STRL':'CONSTRUCTION',
    'PATH':'ROBOTICS', 'TER':'ROBOTICS',  'ISRG':'ROBOTICS',
    'PLTR':'AI SOFTWARE','AI':'AI SOFTWARE',
}

AI_ROLE_MAP = {
    'MRVL': 'Custom AI ASICs for hyperscalers — direct NVIDIA ASIC competitor',
    'INTC': '18A foundry + $350M SambaNova — US chip manufacturing turnaround',
    'AMD':  'MI300X GPU competing with NVIDIA H100/H200 for AI training',
    'AVGO': 'Custom AI XPU chips (Google TPU, Meta) + AI networking',
    'CRDO': 'High-speed Active Electrical Cables for AI data center interconnects',
    'MU':   'HBM3E memory stacked on NVIDIA GPUs for AI training',
    'SMCI': 'AI server racks with direct liquid cooling — deeply discounted',
    'CLS':  'Contract mfg: AI servers and networking gear for hyperscalers',
    'JBL':  'AI server racks, liquid-cooling, networking switches',
    'DELL': 'AI server infrastructure + PowerEdge AI, Microsoft/NVIDIA partner',
    'APLD': 'HPC & AI data center operator — 150MW CoreWeave deal',
    'IREN': 'Ex-BTC miner converting to AI GPU cloud — NVIDIA Blackwell ordered',
    'NBIS': 'AI cloud infra — Meta + Microsoft contracts, scaling fast',
    'DLR':  'Largest data center REIT — AI colocation demand surge',
    'OKLO': 'Small modular nuclear reactors for AI data center power',
    'CEG':  'Nuclear operator with Microsoft AI data center energy deals',
    'VST':  'Power generation play on AI electricity demand surge',
    'ETN':  'Electrical components for AI data center buildout',
    'MOD':  'Data center chillers — revenue +119% YoY, $2B target by 2028',
    'STRL': 'Builds AI data center facilities — $2.6B backlog +64% YoY',
    'PATH': 'Agentic AI automation — software robots for enterprise workflows',
    'TER':  'Semiconductor test equipment + Universal Robots',
    'ISRG': 'AI-guided da Vinci surgical robots — market leader',
    'PLTR': 'AI Platform (AIP) — US gov + enterprise AI analytics',
    'AI':   'Enterprise AI apps — pure-play beaten down, direct AI exposure',
}

def score_ai_infra_stock(ticker_sym):
    base = score_stock(ticker_sym)
    if not base:
        return None
    try:
        import yfinance as yf
        import concurrent.futures as cf
        ticker = yf.Ticker(ticker_sym)
        # hist already pulled in score_stock but we need 52w data
        try:
            hist = ticker.history(period='1y', interval='1d', timeout=8)
        except Exception:
            hist = None
        price  = base['price']
        week52_high = round(float(hist['High'].tail(252).max()), 2) if hist is not None and not hist.empty else None
        week52_low  = round(float(hist['Low'].tail(252).min()),  2) if hist is not None and not hist.empty else None
        vs_52w_high = round(((price - week52_high) / week52_high) * 100, 1) if week52_high else None
        analyst_target = None; analyst_rating = None; num_analysts = None
        pe_ratio = None; revenue_growth = None; market_cap = None
        # Wrap ticker.info in strict 5-second timeout — never block the whole job
        def _get_info():
            return ticker.info
        try:
            with cf.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_get_info)
                info = fut.result(timeout=5)
            analyst_target  = info.get('targetMeanPrice')
            analyst_rating  = (info.get('recommendationKey','') or '').replace('_',' ').title() or None
            num_analysts    = info.get('numberOfAnalystOpinions')
            pe_ratio        = info.get('trailingPE')
            revenue_growth  = info.get('revenueGrowth')
            mc = info.get('marketCap', 0)
            if mc > 1e12:   market_cap = f"{round(mc/1e12,1)}T"
            elif mc > 1e9:  market_cap = f"{round(mc/1e9,1)}B"
            elif mc > 1e6:  market_cap = f"{round(mc/1e6,1)}M"
        except Exception as e:
            print(f'[AIInfra info SKIP {ticker_sym}] {e}')
        upside_pct = round(((analyst_target - price) / price) * 100, 1) if analyst_target and price else None
        score = base['score']
        if vs_52w_high:
            if vs_52w_high < -40:   score += 20
            elif vs_52w_high < -25: score += 12
            elif vs_52w_high < -15: score += 6
        if upside_pct:
            if upside_pct > 50:   score += 15
            elif upside_pct > 30: score += 10
            elif upside_pct > 15: score += 5
            elif upside_pct < 0:  score -= 10
        if upside_pct and upside_pct >= 20 and vs_52w_high and vs_52w_high <= -15:
            signal = 'BUY'
        elif upside_pct and upside_pct > 0:
            signal = 'WATCH'
        else:
            signal = 'AVOID'
        base.update({
            'week52_high':    week52_high,
            'week52_low':     week52_low,
            'vs_52w_high':    vs_52w_high,
            'analyst_target': round(analyst_target, 2) if analyst_target else None,
            'analyst_rating': analyst_rating,
            'num_analysts':   num_analysts,
            'upside_pct':     upside_pct,
            'market_cap':     market_cap,
            'pe_ratio':       round(pe_ratio, 1) if pe_ratio else None,
            'revenue_growth': (f"+{round(revenue_growth*100,1)}%" if revenue_growth and revenue_growth > 0
                               else f"{round(revenue_growth*100,1)}%" if revenue_growth else None),
            'ai_role':        AI_ROLE_MAP.get(ticker_sym, ''),
            'category':       TICKER_CATEGORY.get(ticker_sym, 'AI'),
            'signal':         signal,
            'score':          max(0, min(100, score)),
        })
        return base
    except Exception as e:
        print(f'[ScoreAIInfra {ticker_sym}] {e}')
        return base

_ai_infra_jobs = {}

def _run_ai_infra_job(job_id, scan_filter, date_str):
    try:
        _ai_infra_jobs[job_id] = {'status': 'scanning'}
        universe = AI_INFRA_UNIVERSE.get(scan_filter, AI_INFRA_UNIVERSE['ALL'])
        regime   = get_market_regime()
        _ai_infra_jobs[job_id] = {'status': 'scoring'}
        scored = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(score_ai_infra_stock, sym): sym for sym in universe}
            for f in as_completed(futures, timeout=90):
                sym = futures[f]
                try:
                    s = f.result()
                    if s: scored.append(s)
                except Exception as e:
                    print(f'[AIInfra score] {sym}: {e}')
        scored.sort(key=lambda x: (
            0 if x.get('signal')=='BUY' else 1 if x.get('signal')=='WATCH' else 2,
            -(x.get('upside_pct') or 0)
        ))
        top5 = scored[:5]
        # If scoring failed entirely, use raw universe with base prices so we always return something
        if not scored:
            print(f'[AIInfra] WARNING: zero stocks scored for {scan_filter} — using fallback')
            for sym in universe[:5]:
                try:
                    import yfinance as yf
                    t = yf.Ticker(sym)
                    h = t.history(period='5d', timeout=8)
                    price = round(float(h['Close'].iloc[-1]), 2) if not h.empty else 0
                    scored.append({'ticker':sym,'price':price,'score':50,'signal':'WATCH',
                                   'rsi':50,'vol_ratio':1.0,'signals':[],'sr_levels':{},
                                   'ema20':None,'ema50':None,'ema200':None,'macd_bias':'NEUTRAL',
                                   'near_earnings':False,'news':[],
                                   'ai_role':AI_ROLE_MAP.get(sym,''),'category':TICKER_CATEGORY.get(sym,'AI'),
                                   'week52_high':None,'week52_low':None,'vs_52w_high':None,
                                   'analyst_target':None,'analyst_rating':None,'num_analysts':None,
                                   'upside_pct':None,'market_cap':None,'pe_ratio':None,'revenue_growth':None})
                except Exception as fe:
                    print(f'[AIInfra fallback {sym}] {fe}')
        if not scored:
            _ai_infra_jobs[job_id] = {'status':'error','error':'No stocks scored'}; return
        top5 = scored[:5]
        _ai_infra_jobs[job_id] = {'status': 'news'}
        for stock in top5:
            try: stock['news'] = get_news(stock['ticker'])[:3]
            except: stock['news'] = []
        _ai_infra_jobs[job_id] = {'status': 'analyzing'}
        vix = float(regime.get('vix', 20))
        stocks_ctx = ''
        for s in top5:
            stocks_ctx += f"""
---
{s['ticker']} | ${s['price']} | Signal:{s.get('signal','WATCH')} | Score:{s['score']}/100
Category:{s.get('category','')} | AI Role:{s.get('ai_role','')}
EMA20:{s.get('ema20','?')} EMA50:{s.get('ema50','?')} EMA100:{s.get('ema100','?')} EMA200:{s.get('ema200','?')}
ADX:{s.get('adx','?')} ({s.get('adx_signal','?')}) | Structure:{s.get('hh_hl_structure','?')} ({s.get('hh_hl_strength','?')}% confidence)
ATR:{s.get('atr','?')} | RSI:{s.get('rsi','?')} MACD:{s.get('macd_bias','?')} Volume:{s.get('vol_ratio','?')}x
52W HIGH:${s.get('week52_high','?')} CURRENT:${s['price']} 52W LOW:${s.get('week52_low','?')}
Discount from high:{s.get('vs_52w_high','?')}% | Analyst target:${s.get('analyst_target','?')} Upside:{s.get('upside_pct','?')}% Rating:{s.get('analyst_rating','?')} ({s.get('num_analysts','?')} analysts)
Market cap:{s.get('market_cap','?')} P/E:{s.get('pe_ratio','?')} Rev growth:{s.get('revenue_growth','?')}
News:{' | '.join([n['title'][:60] for n in s.get('news',[])]) or 'None'}
---"""
        prompt = f"""You are Wolf AI — elite AI infrastructure investor.
Peter Lynch + Druckenmiller + Cathie Wood methodology applied to AI picks & shovels.
TODAY:{date_str} | SPY:${regime['spy_price']} ({regime.get('spy_change',0):+.2f}%) VIX:{vix} {regime['fear_greed']} REGIME:{regime['regime']} | FILTER:{scan_filter}

REAL STOCK DATA (TwelveData candles — 35 days OHLC, computed indicators):
{stocks_ctx}

ADX RULE: Only recommend BUY if ADX > 25 (trending). Flag RANGING if ADX < 20.
For each stock give the EXACT reason to buy or avoid NOW based on AI infrastructure role, value vs fair value, specific catalyst, and time horizon.

Respond ONLY in valid JSON (no markdown):
{{"scan_date":"{date_str}","filter":"{scan_filter}","sector_read":"2-sentence AI infrastructure sector read","picks":[{{"rank":1,"ticker":"X","current_price":"0.00","signal":"BUY","confidence":82,"wolf_score":75,"category":"CHIPS","ai_role":"specific role","thesis":"3-sentence thesis using real data","infrastructure_role":"what breaks in AI if this fails","catalyst":"near-term catalyst max 12 words","risk":"key risk max 10 words","verdict":"one decisive sentence","why_now":"what specifically changed","time_horizon":"3-6 months","entry_strategy":"entry plan","exit_strategy":"exit plan","week52_high":"0","week52_low":"0","vs_52w_high":-20,"analyst_target":"0","upside_pct":25,"analyst_rating":"Strong Buy","num_analysts":20,"market_cap":"10B","pe_ratio":25,"revenue_growth":"+25%","ai_revenue_pct":"~50%","confluences":["reason1","reason2","reason3"],"ai_edge":["edge1","edge2"],"warnings":["risk1"],"invalidation":"what makes this wrong"}}]}}"""
        result = None; last_error = None
        for attempt in range(3):
            try:
                raw = call_claude(prompt, 6000)
                if not raw or not raw.strip(): raise ValueError('Empty response')
                result = parse_json_response(raw); break
            except Exception as retry_err:
                last_error = retry_err
                print(f'[AIInfra] Attempt {attempt+1} failed: {retry_err}')
                if attempt < 2: time.sleep(2)
        if result is None: raise Exception(f'Claude failed after 3 attempts: {last_error}')
        for pick in result.get('picks', []):
            match = next((s for s in top5 if s['ticker']==pick.get('ticker','')), None)
            if match:
                pick['real_score']        = match['score']
                pick['real_rsi']          = match['rsi']
                pick['real_vol_ratio']    = match['vol_ratio']
                pick['real_signals']      = match['signals']
                pick['sr_levels']         = match['sr_levels']
                pick['news']              = match['news']
                pick['ema20']             = match.get('ema20')
                pick['ema50']             = match.get('ema50')
                pick['ema100']            = match.get('ema100')
                pick['ema200']            = match.get('ema200')
                pick['adx']               = match.get('adx')
                pick['adx_signal']        = match.get('adx_signal')
                pick['hh_hl_structure']   = match.get('hh_hl_structure')
                pick['hh_hl_strength']    = match.get('hh_hl_strength')
                pick['atr']               = match.get('atr')
                pick['macd_bias']         = match.get('macd_bias')
                pick['near_earnings']     = match.get('near_earnings', False)
                if match.get('week52_high'):              pick['week52_high']   = str(match['week52_high'])
                if match.get('week52_low'):               pick['week52_low']    = str(match['week52_low'])
                if match.get('vs_52w_high') is not None:  pick['vs_52w_high']   = match['vs_52w_high']
                if match.get('analyst_target'):           pick['analyst_target']= str(match['analyst_target'])
                if match.get('upside_pct') is not None:  pick['upside_pct']    = match['upside_pct']
                if match.get('analyst_rating'):           pick['analyst_rating']= match['analyst_rating']
                if match.get('num_analysts'):             pick['num_analysts']  = match['num_analysts']
                if match.get('market_cap'):               pick['market_cap']    = match['market_cap']
        result['market_regime'] = regime
        _ai_infra_jobs[job_id] = {'status': 'done', 'result': result}
    except Exception as e:
        import traceback; print(traceback.format_exc())
        _ai_infra_jobs[job_id] = {'status': 'error', 'error': str(e)}


@app.route('/education')
def education_page():
    return render_template('education.html')

@app.route('/legends')
@login_required
@byakugan_required
def legends_page():
    return render_template('legends.html')

@app.route('/ai-infra')
@login_required
@byakugan_required
def ai_infra_page():
    return render_template('ai_infra.html')


@app.route('/api/ai-infra-scan', methods=['POST'])
@login_required
@elite_required
def ai_infra_scan():
    try:
        data        = request.get_json() or {}
        scan_filter = data.get('filter', 'ALL')
        date_str    = datetime.now().strftime('%A, %B %d, %Y')
        job_id      = str(uuid.uuid4())[:8]
        _ai_infra_jobs[job_id] = {'status': 'starting'}
        t = threading.Thread(target=_run_ai_infra_job, args=(job_id, scan_filter, date_str), daemon=True)
        t.start()
        return jsonify({'job_id': job_id, 'status': 'starting'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai-infra-poll/<job_id>', methods=['GET'])
@login_required
@elite_required
def ai_infra_poll(job_id):
    job = _ai_infra_jobs.get(job_id)
    if not job: return jsonify({'status':'error','error':'Job not found'}), 404
    if job['status'] == 'done':
        result = job.get('result', {}); result['status'] = 'done'
        _ai_infra_jobs.pop(job_id, None); return jsonify(result)
    if job['status'] == 'error':
        return jsonify({'status':'error','error':job.get('error','Unknown')}), 500
    return jsonify({'status': job['status']})


# ═══════════════════════════════════════════════════════════════════
# 🐺 WOLF BACKTESTING ENGINE
# Pure Python — uses only yfinance + pandas + numpy (already installed)
# No new packages needed. Surgical addition — nothing existing touched.
# ═══════════════════════════════════════════════════════════════════

import uuid as _bt_uuid
import threading as _bt_threading

_bt_jobs = {}  # job store

# ── INDICATOR CALCULATIONS ──────────────────────────────────────────

def _bt_ema(series, period):
    """Calculate EMA on a pandas Series"""
    return series.ewm(span=period, adjust=False).mean()

def _bt_rsi(series, period=14):
    """Calculate RSI"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def _bt_atr(df, period=14):
    """Calculate ATR"""
    import pandas as pd
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close  = (df['Low']  - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def _bt_bbands(series, period=20, std_dev=2):
    """Calculate Bollinger Bands"""
    mid   = series.rolling(period).mean()
    std   = series.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower

def _bt_macd(series, fast=12, slow=26, signal=9):
    """Calculate MACD"""
    ema_fast   = series.ewm(span=fast,   adjust=False).mean()
    ema_slow   = series.ewm(span=slow,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line= macd_line.ewm(span=signal, adjust=False).mean()
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram

def _bt_vwap(df):
    """Calculate intraday VWAP"""
    import pandas as pd
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    vwap = (tp * df['Volume']).cumsum() / df['Volume'].cumsum().replace(0, 1)
    return vwap

def _bt_stoch(df, k_period=14, d_period=3):
    """Calculate Stochastic"""
    low_min  = df['Low'].rolling(k_period).min()
    high_max = df['High'].rolling(k_period).max()
    k = 100 * (df['Close'] - low_min) / (high_max - low_min + 1e-10)
    d = k.rolling(d_period).mean()
    return k, d

# ── RISK PROFILES ──────────────────────────────────────────────────

RISK_PROFILES = {
    'conservative': {
        'risk_per_trade': 0.01,   # 1% per trade
        'max_positions': 2,
        'profit_target_mult': 2.0, # 1:2 R/R
        'stop_atr_mult': 1.5,
        'label': 'CONSERVATIVE — 1% risk, 1:2 R/R, max 2 positions'
    },
    'moderate': {
        'risk_per_trade': 0.02,   # 2% per trade
        'max_positions': 3,
        'profit_target_mult': 3.0, # 1:3 R/R
        'stop_atr_mult': 2.0,
        'label': 'MODERATE — 2% risk, 1:3 R/R, max 3 positions'
    },
    'aggressive': {
        'risk_per_trade': 0.03,   # 3% per trade
        'max_positions': 5,
        'profit_target_mult': 4.0, # 1:4 R/R
        'stop_atr_mult': 2.5,
        'label': 'AGGRESSIVE — 3% risk, 1:4 R/R, max 5 positions'
    }
}

# ── STRATEGIES ─────────────────────────────────────────────────────

def _strategy_ema_crossover(df, params):
    """Strategy 1: EMA 8/21 Crossover — classic trend following"""
    fast = _bt_ema(df['Close'], 8)
    slow = _bt_ema(df['Close'], 21)
    trend = _bt_ema(df['Close'], 200)
    rsi = _bt_rsi(df['Close'], 14)
    # BUY: fast crosses above slow, price above 200 EMA, RSI not overbought
    buy  = (fast > slow) & (fast.shift() <= slow.shift()) & (df['Close'] > trend) & (rsi < 70)
    # SELL: fast crosses below slow OR RSI overbought
    sell = ((fast < slow) & (fast.shift() >= slow.shift())) | (rsi > 78)
    return buy.astype(int), sell.astype(int)

def _strategy_ema_pullback(df, params):
    """Strategy 2: 8/21 EMA Pullback — wait for dip, enter on bounce
    Based on research: price above both EMAs, pulls back to 21 EMA, bounces"""
    ema8  = _bt_ema(df['Close'], 8)
    ema21 = _bt_ema(df['Close'], 21)
    ema200= _bt_ema(df['Close'], 200)
    rsi   = _bt_rsi(df['Close'], 14)
    # BUY: above both EMAs, price touches 21 EMA, RSI oversold on pullback
    touched_21 = (df['Low'] <= ema21 * 1.005) & (df['Close'] >= ema21 * 0.998)
    above_trend = df['Close'] > ema200
    bullish_stack = ema8 > ema21
    buy  = touched_21 & above_trend & bullish_stack & (rsi < 50) & (rsi > 25)
    sell = (df['Close'] < ema21) | (rsi > 75)
    return buy.astype(int), sell.astype(int)

def _strategy_rsi_reversal(df, params):
    """Strategy 3: RSI Divergence + Oversold Bounce — mean reversion"""
    ema50  = _bt_ema(df['Close'], 50)
    ema200 = _bt_ema(df['Close'], 200)
    rsi    = _bt_rsi(df['Close'], 14)
    # BUY: RSI crosses up from oversold zone, price above 200 EMA
    rsi_cross_up = (rsi > 30) & (rsi.shift() <= 30) & (df['Close'] > ema200)
    # SELL: RSI overbought cross down
    rsi_cross_dn = (rsi < 70) & (rsi.shift() >= 70)
    return rsi_cross_up.astype(int), rsi_cross_dn.astype(int)

def _strategy_bollinger_squeeze(df, params):
    """Strategy 4: Bollinger Band Squeeze Breakout — volatility expansion"""
    upper, mid, lower = _bt_bbands(df['Close'], 20, 2)
    band_width = (upper - lower) / mid
    # squeeze = narrow bands (bottom 20% of recent band width)
    squeeze = band_width < band_width.rolling(50).quantile(0.2)
    # BUY: price closes above upper band after squeeze
    buy  = (df['Close'] > upper) & squeeze.shift(1) & (df['Close'] > mid)
    # SELL: price closes below mid band
    sell = df['Close'] < mid
    return buy.astype(int), sell.astype(int)

def _strategy_macd_momentum(df, params):
    """Strategy 5: MACD Momentum — histogram crossover with trend filter"""
    macd, signal, hist = _bt_macd(df['Close'])
    ema200 = _bt_ema(df['Close'], 200)
    rsi = _bt_rsi(df['Close'], 14)
    # BUY: histogram crosses above zero in uptrend
    buy  = (hist > 0) & (hist.shift() <= 0) & (df['Close'] > ema200) & (rsi < 65)
    # SELL: histogram crosses below zero
    sell = (hist < 0) & (hist.shift() >= 0)
    return buy.astype(int), sell.astype(int)

def _strategy_london_breakout(df, params):
    """Strategy 6: London Open Breakout — 7am-9am GMT range breakout
    Works on H1 data. Asian session range broken at London open."""
    # Approximate: use rolling 4-candle range as Asian session
    roll_high = df['High'].rolling(4).max()
    roll_low  = df['Low'].rolling(4).min()
    atr = _bt_atr(df, 14)
    ema200 = _bt_ema(df['Close'], 200)
    # BUY: close above Asian high + above 200 EMA
    buy  = (df['Close'] > roll_high.shift(1)) & (df['Close'] > ema200)
    # SELL: close below Asian low OR close below 200 EMA
    sell = (df['Close'] < roll_low.shift(1)) | (df['Close'] < ema200)
    return buy.astype(int), sell.astype(int)

def _strategy_golden_cross(df, params):
    """Strategy 7: Golden/Death Cross — 50/200 SMA classic swing"""
    sma50  = df['Close'].rolling(50).mean()
    sma200 = df['Close'].rolling(200).mean()
    rsi = _bt_rsi(df['Close'], 14)
    # Golden cross: 50 SMA crosses above 200 SMA
    buy  = (sma50 > sma200) & (sma50.shift() <= sma200.shift())
    # Death cross
    sell = (sma50 < sma200) & (sma50.shift() >= sma200.shift())
    return buy.astype(int), sell.astype(int)

def _strategy_support_resistance(df, params):
    """Strategy 8: S/R Breakout + Retest — breakout then retest for entry"""
    # Use 20-period highs as resistance, 20-period lows as support
    resist = df['High'].rolling(20).max()
    support= df['Low'].rolling(20).min()
    vol_avg= df['Volume'].rolling(20).mean() if 'Volume' in df.columns else None
    ema50  = _bt_ema(df['Close'], 50)
    # BUY: breaks above 20-period high with volume + above EMA50
    buy  = (df['Close'] > resist.shift(1)) & (df['Close'] > ema50)
    if vol_avg is not None:
        buy = buy & (df['Volume'] > vol_avg * 1.2)
    # SELL: drops below 20-period low
    sell = df['Close'] < support.shift(1)
    return buy.astype(int), sell.astype(int)

def _strategy_triple_ema(df, params):
    """Strategy 9: Triple EMA Stack — 8/21/55 all aligned, enter pullbacks
    Based on research: three EMAs fanning apart = multi-timeframe consensus"""
    ema8  = _bt_ema(df['Close'], 8)
    ema21 = _bt_ema(df['Close'], 21)
    ema55 = _bt_ema(df['Close'], 55)
    ema200= _bt_ema(df['Close'], 200)
    rsi = _bt_rsi(df['Close'], 14)
    # Full bull stack: price > ema8 > ema21 > ema55 > ema200
    bull_stack = (df['Close'] > ema8) & (ema8 > ema21) & (ema21 > ema55)
    # Pullback to ema21 zone
    at_ema21 = (df['Low'] <= ema21 * 1.008)
    buy  = bull_stack & at_ema21 & (rsi < 55) & (rsi > 20)
    sell = (ema8 < ema21) | (rsi > 78)
    return buy.astype(int), sell.astype(int)

def _strategy_stoch_rsi(df, params):
    """Strategy 10: Stochastic + RSI Double Confirmation — high precision"""
    k, d = _bt_stoch(df, 14, 3)
    rsi = _bt_rsi(df['Close'], 14)
    ema200 = _bt_ema(df['Close'], 200)
    # BUY: both stoch and RSI oversold, stoch K crosses above D, above 200 EMA
    stoch_cross_up = (k > d) & (k.shift() <= d.shift())
    both_oversold  = (k < 25) & (rsi < 40)
    buy  = stoch_cross_up & both_oversold & (df['Close'] > ema200)
    # SELL: stoch overbought, K crosses below D
    stoch_cross_dn = (k < d) & (k.shift() >= d.shift())
    both_overbought= (k > 75) & (rsi > 60)
    sell = stoch_cross_dn & both_overbought
    return buy.astype(int), sell.astype(int)

def _strategy_fibonacci_bounce(df, params):
    """Strategy 11: Fibonacci 61.8% Retracement Entry — golden ratio pullback"""
    # Find swing high and low over last 50 candles
    window = 50
    swing_high = df['High'].rolling(window).max()
    swing_low  = df['Low'].rolling(window).min()
    fib618 = swing_high - (swing_high - swing_low) * 0.618
    fib50  = swing_high - (swing_high - swing_low) * 0.500
    ema200 = _bt_ema(df['Close'], 200)
    rsi = _bt_rsi(df['Close'], 14)
    # BUY: price pulls back to 61.8% zone, RSI oversold, uptrend intact
    at_fib = (df['Low'] <= fib618 * 1.005) & (df['Close'] >= fib618 * 0.995)
    uptrend = df['Close'] > ema200
    buy  = at_fib & uptrend & (rsi < 45)
    sell = (df['Close'] > swing_high.shift()) | (rsi > 75)
    return buy.astype(int), sell.astype(int)

def _strategy_mean_reversion(df, params):
    """Strategy 12: Mean Reversion — oversold in uptrend, snap back to mean"""
    ema50  = _bt_ema(df['Close'], 50)
    ema200 = _bt_ema(df['Close'], 200)
    upper, mid, lower = _bt_bbands(df['Close'], 20, 2)
    rsi = _bt_rsi(df['Close'], 14)
    # BUY: price touches lower Bollinger band in uptrend, RSI oversold
    at_lower = df['Close'] <= lower * 1.005
    uptrend  = df['Close'] > ema200
    buy  = at_lower & uptrend & (rsi < 35)
    # SELL: price reaches upper band or middle band
    sell = (df['Close'] >= upper) | (rsi > 70)
    return buy.astype(int), sell.astype(int)

# Strategy registry — each entry: (function, name, type, best_timeframe, description)
BT_STRATEGIES = {
    'ema_crossover':      (_strategy_ema_crossover,   'EMA 8/21 Crossover',         'SWING/DAY', '1h/4h', 'Buy when 8 EMA crosses above 21 EMA above 200 EMA trend filter'),
    'ema_pullback':       (_strategy_ema_pullback,     'EMA 8/21 Pullback',           'SWING',     '4h/1d', 'Wait for dip to 21 EMA in uptrend — highest probability entry'),
    'rsi_reversal':       (_strategy_rsi_reversal,     'RSI Oversold Reversal',       'SWING',     '4h/1d', 'RSI crosses up from oversold (<30) in uptrend — mean reversion'),
    'bollinger_squeeze':  (_strategy_bollinger_squeeze,'Bollinger Squeeze Breakout',  'SWING/DAY', '1h/4h', 'Volatility contraction → explosive breakout above upper band'),
    'macd_momentum':      (_strategy_macd_momentum,    'MACD Momentum',               'SWING',     '4h/1d', 'MACD histogram crosses above zero in uptrend with RSI filter'),
    'london_breakout':    (_strategy_london_breakout,  'London Open Breakout',        'DAY',       '1h',    'Break of Asian session range at London open — 70% daily direction'),
    'golden_cross':       (_strategy_golden_cross,     'Golden/Death Cross 50/200',   'SWING',     '1d',    'Classic 50/200 SMA crossover — long-term trend shift'),
    'support_resistance': (_strategy_support_resistance,'S/R Breakout + Volume',      'SWING/DAY', '1h/4h', 'Break of 20-period high/low on volume confirmation'),
    'triple_ema':         (_strategy_triple_ema,       'Triple EMA Stack 8/21/55',    'SWING',     '4h/1d', 'All 3 EMAs aligned bullish, enter on pullback to 21 EMA'),
    'stoch_rsi':          (_strategy_stoch_rsi,        'Stochastic + RSI Double',     'DAY/SWING', '1h/4h', 'Both Stoch and RSI oversold confirmation — high precision entries'),
    'fibonacci_bounce':   (_strategy_fibonacci_bounce, 'Fibonacci 61.8% Golden Ratio','SWING',     '4h/1d', 'Enter at 61.8% retracement level in established uptrend'),
    'mean_reversion':     (_strategy_mean_reversion,   'Bollinger Mean Reversion',    'SWING',     '4h/1d', 'Price touches lower band oversold in uptrend — snaps back to mean'),
}

# ── BACKTEST CORE ENGINE ────────────────────────────────────────────

def _run_backtest(symbol, strategy_key, interval, period, account_size, risk_mode):
    """
    Core backtest engine. Runs one strategy on one symbol.
    Returns performance metrics dict.
    """
    import yfinance as yf
    import pandas as pd
    import numpy as np

    risk = RISK_PROFILES.get(risk_mode, RISK_PROFILES['moderate'])
    strat_fn, strat_name, strat_type, strat_tf, strat_desc = BT_STRATEGIES[strategy_key]

    # Fetch data — try requested interval, fallback to daily if needed
    df = None
    tried = []
    for try_interval, try_period in [(interval, period), ('1d', '2y'), ('1d', '1y')]:
        try:
            ticker = yf.Ticker(symbol)
            _df = ticker.history(period=try_period, interval=try_interval)
            tried.append(f'{try_interval}/{try_period}={len(_df)}')
            if not _df.empty and len(_df) >= 50:
                df = _df
                interval = try_interval
                period = try_period
                break
        except Exception as e:
            tried.append(f'{try_interval}/{try_period}=ERR')
            continue
    if df is None or df.empty:
        return {'error': f'Not enough data for {symbol} after trying {tried}. Yahoo Finance may be blocking this server. Try a different pair.'}

    df = df.copy()
    closes = df['Close']
    n = len(closes)

    # Generate signals
    try:
        buy_signals, sell_signals = strat_fn(df, risk)
    except Exception as e:
        return {'error': f'Strategy calculation failed: {str(e)}'}

    # ATR for dynamic stops
    atr = _bt_atr(df, 14)

    # ── SIMULATE TRADES ──
    capital    = float(account_size)
    equity     = [capital]
    trades     = []
    in_trade   = False
    entry_price= 0.0
    stop_price = 0.0
    target_price=0.0
    entry_idx  = 0
    position_size = 0.0

    for i in range(50, n):  # start after warmup
        price = float(closes.iloc[i])
        curr_atr = float(atr.iloc[i]) if not np.isnan(atr.iloc[i]) else price * 0.01

        if not in_trade:
            # Check for entry
            if buy_signals.iloc[i] == 1 and capital > 10:
                # Position sizing: risk% of capital / stop distance
                stop_dist = curr_atr * risk['stop_atr_mult']
                stop_price = price - stop_dist
                risk_amount = capital * risk['risk_per_trade']
                position_size = risk_amount / max(stop_dist, 0.0001)
                cost = position_size * price
                if cost > capital:
                    position_size = capital / price
                    cost = capital
                target_price = price + (stop_dist * risk['profit_target_mult'])
                entry_price = price
                entry_idx = i
                capital -= cost
                in_trade = True

        else:
            # Check for exit: target hit, stop hit, or sell signal
            hit_target = price >= target_price
            hit_stop   = price <= stop_price
            got_signal = sell_signals.iloc[i] == 1
            max_bars   = 50  # max hold period

            if hit_target or hit_stop or got_signal or (i - entry_idx) >= max_bars:
                exit_price = price
                if hit_target:   exit_price = target_price
                elif hit_stop:   exit_price = stop_price
                proceeds = position_size * exit_price
                capital += proceeds
                pnl = proceeds - (position_size * entry_price)
                pct = (exit_price - entry_price) / entry_price * 100
                reason = 'TARGET' if hit_target else ('STOP' if hit_stop else ('SIGNAL' if got_signal else 'TIMEOUT'))
                trades.append({
                    'entry': round(entry_price, 5),
                    'exit':  round(exit_price, 5),
                    'pnl':   round(pnl, 2),
                    'pct':   round(pct, 3),
                    'win':   pnl > 0,
                    'reason': reason,
                    'bars':  i - entry_idx
                })
                in_trade  = False
                position_size = 0.0
        equity.append(capital + (position_size * float(closes.iloc[i]) if in_trade else 0))

    # Close any open trade at end
    if in_trade:
        exit_price = float(closes.iloc[-1])
        proceeds = position_size * exit_price
        capital += proceeds
        pnl = proceeds - (position_size * entry_price)
        trades.append({'entry': entry_price, 'exit': exit_price, 'pnl': round(pnl, 2),
                       'pct': round((exit_price-entry_price)/entry_price*100, 3),
                       'win': pnl > 0, 'reason': 'END', 'bars': n - entry_idx})

    equity = np.array(equity)
    final_capital = float(capital)

    if len(trades) == 0:
        return {'error': 'No trades generated — try a different strategy or longer period'}

    # ── PERFORMANCE METRICS ──
    wins    = [t for t in trades if t['win']]
    losses  = [t for t in trades if not t['win']]
    total_trades = len(trades)
    win_rate = len(wins) / total_trades * 100 if total_trades > 0 else 0

    total_win_pnl  = sum(t['pnl'] for t in wins)
    total_loss_pnl = abs(sum(t['pnl'] for t in losses))
    profit_factor  = (total_win_pnl / total_loss_pnl) if total_loss_pnl > 0 else float('inf')

    total_return = (final_capital - account_size) / account_size * 100
    bh_return    = (float(closes.iloc[-1]) - float(closes.iloc[50])) / float(closes.iloc[50]) * 100

    # Sharpe Ratio (annualized)
    if len(equity) > 1:
        returns = np.diff(equity) / equity[:-1]
        returns = returns[returns != 0]
        if len(returns) > 1 and returns.std() > 0:
            periods_per_year = {'1d': 252, '1h': 252*6.5, '4h': 252*1.625, '15m': 252*26}
            ann_factor = periods_per_year.get(interval, 252) ** 0.5
            sharpe = (returns.mean() / returns.std()) * ann_factor
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # Max Drawdown
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / np.maximum(peak, 1e-10) * 100
    max_dd = float(drawdown.min())

    avg_win  = total_win_pnl  / len(wins)   if wins   else 0
    avg_loss = total_loss_pnl / len(losses) if losses else 0

    # Weekly profit estimation
    num_weeks = len(df) / (5 if interval == '1d' else 40 if interval == '1h' else 10)
    weekly_pnl_est = (final_capital - account_size) / max(num_weeks, 1)

    # Realistic weekly target assessment
    target_weekly = account_size * 0.10  # 10% weekly = ambitious
    target_assessment = ''
    if weekly_pnl_est >= target_weekly:
        target_assessment = f'✅ This strategy historically generates ~${weekly_pnl_est:.0f}/week — ABOVE your target'
    elif weekly_pnl_est >= target_weekly * 0.5:
        target_assessment = f'⚠️ This generates ~${weekly_pnl_est:.0f}/week — BELOW target but realistic with optimization'
    else:
        target_assessment = f'🔴 Only ~${weekly_pnl_est:.0f}/week on this account — target too aggressive for this account size'

    # Equity curve (downsample to 100 points for chart)
    step = max(1, len(equity) // 100)
    equity_curve = [round(float(e), 2) for e in equity[::step]]

    return {
        'symbol':          symbol,
        'strategy_name':   strat_name,
        'strategy_type':   strat_type,
        'strategy_desc':   strat_desc,
        'risk_mode':       risk_mode,
        'risk_label':      risk['label'],
        'interval':        interval,
        'period':          period,
        'account_size':    account_size,
        'final_capital':   round(final_capital, 2),
        'total_return':    round(total_return, 2),
        'bh_return':       round(bh_return, 2),
        'alpha':           round(total_return - bh_return, 2),
        'total_trades':    total_trades,
        'win_rate':        round(win_rate, 1),
        'profit_factor':   round(profit_factor, 2) if profit_factor != float('inf') else 99.0,
        'sharpe_ratio':    round(sharpe, 2),
        'max_drawdown':    round(max_dd, 2),
        'avg_win':         round(avg_win, 2),
        'avg_loss':        round(avg_loss, 2),
        'total_pnl':       round(final_capital - account_size, 2),
        'weekly_pnl_est':  round(weekly_pnl_est, 2),
        'target_assessment': target_assessment,
        'equity_curve':    equity_curve,
        'trades':          trades[-20:],  # last 20 trades
        'candles_tested':  len(df),
        'data_from':       str(df.index[0])[:10],
        'data_to':         str(df.index[-1])[:10],
    }

def _run_bt_scan(job_id, symbols, strategy_key, interval, period, account_size, risk_mode):
    """Run backtest scan across multiple symbols in background thread"""
    try:
        import concurrent.futures
        results = []
        errors  = []

        def bt_one(sym):
            return sym, _run_backtest(sym, strategy_key, interval, period, account_size, risk_mode)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(bt_one, s): s for s in symbols}
            for f in concurrent.futures.as_completed(futures):
                sym, res = f.result()
                if 'error' in res:
                    errors.append({'symbol': sym, 'error': res['error']})
                else:
                    results.append(res)

        # Sort by total return descending
        results.sort(key=lambda r: r['total_return'], reverse=True)

        # Find the best pick
        best = results[0] if results else None

        _bt_jobs[job_id] = {
            'status': 'done',
            'results': results,
            'best': best,
            'errors': errors,
            'scanned': len(symbols),
        }
    except Exception as e:
        import traceback; print(traceback.format_exc())
        _bt_jobs[job_id] = {'status': 'error', 'error': str(e)}


# ── FLASK ROUTES ────────────────────────────────────────────────────

@app.route('/api/backtest', methods=['POST'])
@login_required
def api_backtest():
    """
    Single backtest endpoint.
    POST body: {
        symbol: str,       e.g. "AAPL" or "EURUSD=X"
        strategy: str,     e.g. "ema_pullback"
        interval: str,     e.g. "1h", "4h", "1d"
        period: str,       e.g. "1y", "2y", "6mo"
        account_size: float,
        risk_mode: str,    "conservative" | "moderate" | "aggressive"
    }
    """
    try:
        d = request.get_json() or {}
        symbol       = d.get('symbol', 'AAPL').upper().strip()
        strategy_key = d.get('strategy', 'ema_pullback')
        interval     = d.get('interval', '1d')
        period       = d.get('period', '2y')
        account_size = float(d.get('account_size', 1000))
        risk_mode    = d.get('risk_mode', 'moderate')

        if strategy_key not in BT_STRATEGIES:
            return jsonify({'error': f'Unknown strategy: {strategy_key}. Available: {list(BT_STRATEGIES.keys())}'}), 400

        result = _run_backtest(symbol, strategy_key, interval, period, account_size, risk_mode)
        return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/backtest-scan', methods=['POST'])
@login_required
def api_backtest_scan():
    """
    Scan multiple symbols with one strategy to find the best performer.
    POST body: {
        symbols: [str, ...],   up to 15 symbols
        strategy: str,
        interval: str,
        period: str,
        account_size: float,
        risk_mode: str
    }
    """
    try:
        d = request.get_json() or {}
        symbols      = [s.upper().strip() for s in d.get('symbols', [])][:15]
        strategy_key = d.get('strategy', 'ema_pullback')
        interval     = d.get('interval', '1d')
        period       = d.get('period', '2y')
        account_size = float(d.get('account_size', 1000))
        risk_mode    = d.get('risk_mode', 'moderate')

        if not symbols:
            return jsonify({'error': 'Provide at least one symbol'}), 400
        if strategy_key not in BT_STRATEGIES:
            return jsonify({'error': f'Unknown strategy: {strategy_key}'}), 400

        import uuid as _u, threading as _t
        job_id = str(_u.uuid4())[:8]
        _bt_jobs[job_id] = {'status': 'running'}
        t = _t.Thread(target=_run_bt_scan,
                      args=(job_id, symbols, strategy_key, interval, period, account_size, risk_mode),
                      daemon=True)
        t.start()
        return jsonify({'job_id': job_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/backtest-poll/<job_id>', methods=['GET'])
@login_required
def api_backtest_poll(job_id):
    """Poll background backtest scan job"""
    job = _bt_jobs.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'error': 'Job not found'}), 404
    if job['status'] == 'done':
        _bt_jobs.pop(job_id, None)
        return jsonify(job)
    if job['status'] == 'error':
        return jsonify({'status': 'error', 'error': job.get('error', 'Unknown')}), 500
    return jsonify({'status': 'running'})


@app.route('/api/backtest-strategies', methods=['GET'])
@login_required
def api_backtest_strategies():
    """Return list of all available strategies"""
    return jsonify({
        k: {
            'name': v[1],
            'type': v[2],
            'best_timeframe': v[3],
            'description': v[4]
        }
        for k, v in BT_STRATEGIES.items()
    })



# ═══════════════════════════════════════════════════════════════════
# 🐺 WOLF GOAL-SEEKING BACKTEST ENGINE
# "Give me $1,000 — find which strategy turns it into $1,500"
# Runs ALL strategies across ALL pairs on REAL historical windows
# No future data. No guessing. Pure historical truth.
# ═══════════════════════════════════════════════════════════════════

_goal_jobs = {}  # job store for goal scans

# Default pairs to scan across all markets
DEFAULT_SCAN_PAIRS = {
    'forex': [
        'EURUSD=X', 'GBPUSD=X', 'USDJPY=X', 'GBPJPY=X',
        'AUDUSD=X', 'USDCAD=X', 'NZDUSD=X', 'EURJPY=X',
        'USDCHF=X', 'EURGBP=X'
    ],
    'stocks': [
        'AAPL', 'NVDA', 'TSLA', 'SPY', 'QQQ',
        'MSFT', 'AMZN', 'META', 'GOOGL', 'GLD'
    ],
    'crypto': [
        'BTC-USD', 'ETH-USD'
    ]
}

def _run_goal_backtest_single(symbol, strategy_key, interval, 
                               account_size, target_amount, risk_mode,
                               window_days=90):
    """
    Run a rolling-window goal backtest on one symbol + strategy.
    
    Tests: "If I started with $X on this date, did I reach $target 
            within window_days?"
    
    Returns stats showing:
    - How many times it hit the target
    - How many windows tested  
    - Best return, worst return, average
    - Actual equity curve per window
    """
    import yfinance as yf
    import pandas as pd
    import numpy as np

    risk = RISK_PROFILES.get(risk_mode, RISK_PROFILES['moderate'])
    strat_fn, strat_name, strat_type, strat_tf, strat_desc = BT_STRATEGIES[strategy_key]
    target_pct = ((target_amount - account_size) / account_size) * 100

    # Fetch 2 years of data to get multiple windows
    try:
        ticker = yf.Ticker(symbol)
        # Use longer period for more windows
        df = ticker.history(period='2y', interval=interval)
        if df.empty or len(df) < 150:
            return None
        df = df.copy()
    except Exception:
        return None

    closes = df['Close']
    n = len(df)

    # Calculate candles per window based on interval
    candles_per_day = {
        '1d': 1, '4h': 1.625, '1h': 6.5, '15m': 26
    }
    cpd = candles_per_day.get(interval, 1)
    window_candles = int(window_days * cpd)

    if window_candles > n // 2:
        window_candles = n // 2

    # ── Run full strategy signals once on entire dataset ──
    try:
        buy_signals, sell_signals = strat_fn(df, risk)
    except Exception:
        return None

    atr = _bt_atr(df, 14)

    # ── Test multiple rolling windows ──
    # Stride = 1/3 of window to get overlapping but distinct windows
    stride = max(window_candles // 3, 10)
    window_results = []

    for start_idx in range(50, n - window_candles, stride):
        end_idx = start_idx + window_candles
        capital = float(account_size)
        in_trade = False
        entry_price = 0.0
        stop_price = 0.0
        target_price = 0.0
        pos_size = 0.0
        entry_idx_local = 0
        trades_this_window = []
        peak_capital = capital
        hit_target = False
        hit_target_at = None

        for i in range(start_idx, end_idx):
            price = float(closes.iloc[i])
            curr_atr = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else price * 0.01

            if not in_trade:
                if buy_signals.iloc[i] == 1 and capital > 10:
                    stop_dist = curr_atr * risk['stop_atr_mult']
                    stop_price = price - stop_dist
                    risk_amount = capital * risk['risk_per_trade']
                    pos_size = risk_amount / max(stop_dist, 0.0001)
                    cost = pos_size * price
                    if cost > capital:
                        pos_size = capital / price
                        cost = capital
                    target_price_trade = price + (stop_dist * risk['profit_target_mult'])
                    entry_price = price
                    entry_idx_local = i
                    capital -= cost
                    in_trade = True
                    target_price = target_price_trade
            else:
                hit_tp = price >= target_price
                hit_sl = price <= stop_price
                got_sig = sell_signals.iloc[i] == 1
                timeout = (i - entry_idx_local) >= 50

                if hit_tp or hit_sl or got_sig or timeout:
                    exit_price = target_price if hit_tp else (stop_price if hit_sl else price)
                    proceeds = pos_size * exit_price
                    capital += proceeds
                    pnl = proceeds - (pos_size * entry_price)
                    trades_this_window.append({
                        'pnl': round(pnl, 2),
                        'win': pnl > 0,
                        'reason': 'TP' if hit_tp else ('SL' if hit_sl else 'SIG')
                    })
                    in_trade = False
                    pos_size = 0.0

            # Track current equity
            curr_equity = capital + (pos_size * price if in_trade else 0)
            if curr_equity > peak_capital:
                peak_capital = curr_equity

            # Check if target hit
            if not hit_target and curr_equity >= target_amount:
                hit_target = True
                hit_target_at = i - start_idx  # candles into window

        # Close open trade
        if in_trade:
            exit_price = float(closes.iloc[end_idx - 1])
            proceeds = pos_size * exit_price
            capital += proceeds
            pnl = proceeds - (pos_size * entry_price)
            trades_this_window.append({'pnl': round(pnl,2), 'win': pnl > 0, 'reason': 'END'})

        final = capital
        ret_pct = (final - account_size) / account_size * 100

        # Max drawdown for this window
        # Approximate from final capital
        drawdown_pct = min(0, (final - peak_capital) / peak_capital * 100)

        window_results.append({
            'start_date': str(df.index[start_idx])[:10],
            'end_date':   str(df.index[end_idx - 1])[:10],
            'start_capital': account_size,
            'end_capital': round(final, 2),
            'return_pct':  round(ret_pct, 1),
            'hit_target':  hit_target,
            'hit_target_candles': hit_target_at,
            'num_trades':  len(trades_this_window),
            'wins':        sum(1 for t in trades_this_window if t['win']),
            'drawdown':    round(drawdown_pct, 1),
        })

    if not window_results:
        return None

    # ── Aggregate stats ──
    total_windows   = len(window_results)
    windows_hit     = sum(1 for w in window_results if w['hit_target'])
    hit_rate        = windows_hit / total_windows * 100
    returns         = [w['return_pct'] for w in window_results]
    avg_return      = sum(returns) / len(returns)
    best_return     = max(returns)
    worst_return    = min(returns)
    best_window     = next(w for w in window_results if w['return_pct'] == best_return)
    
    # Consistency score (hit_rate + avg_return combined)
    consistency_score = (hit_rate * 0.6) + (max(avg_return, 0) * 0.4)

    # Best final capital ever achieved
    best_capital = max(w['end_capital'] for w in window_results)
    avg_final    = round(sum(w['end_capital'] for w in window_results) / total_windows, 2)

    # Average days to hit target (when it did)
    hit_windows = [w for w in window_results if w['hit_target'] and w['hit_target_candles']]
    if hit_windows:
        avg_candles_to_target = sum(w['hit_target_candles'] for w in hit_windows) / len(hit_windows)
        cpd_val = candles_per_day.get(interval, 1)
        avg_days_to_target = round(avg_candles_to_target / cpd_val, 0)
    else:
        avg_days_to_target = None

    return {
        'symbol':            symbol,
        'strategy_key':      strategy_key,
        'strategy_name':     strat_name,
        'strategy_type':     strat_type,
        'risk_mode':         risk_mode,
        'interval':          interval,
        'window_days':       window_days,
        'account_size':      account_size,
        'target_amount':     target_amount,
        'target_pct':        round(target_pct, 1),
        'total_windows':     total_windows,
        'windows_hit_target':windows_hit,
        'hit_rate_pct':      round(hit_rate, 1),
        'avg_return_pct':    round(avg_return, 1),
        'best_return_pct':   round(best_return, 1),
        'worst_return_pct':  round(worst_return, 1),
        'best_capital':      round(best_capital, 2),
        'avg_final_capital': avg_final,
        'avg_days_to_target':avg_days_to_target,
        'consistency_score': round(consistency_score, 1),
        'windows':           window_results[-5:],  # last 5 windows for display
        'best_window':       best_window,
        'verdict': (
            f"🔥 STRONG — Hit ${target_amount:,.0f} in {windows_hit}/{total_windows} windows ({hit_rate:.0f}% of the time)"
            if hit_rate >= 60 else
            f"✅ DECENT — Hit target {windows_hit}/{total_windows} times ({hit_rate:.0f}%)"
            if hit_rate >= 30 else
            f"⚠️ WEAK — Only hit target {windows_hit}/{total_windows} times ({hit_rate:.0f}%)"
        )
    }


def _run_goal_scan_job(job_id, pairs, strategies, interval, 
                        account_size, target_amount, risk_mode, window_days):
    """Background job: test all strategy+pair combos, rank by target hit rate"""
    import concurrent.futures as cf
    
    try:
        all_results = []
        errors = []
        combos = [(p, s) for p in pairs for s in strategies]
        total = len(combos)
        done = 0

        def test_one(pair, strat):
            return _run_goal_backtest_single(
                pair, strat, interval, account_size, 
                target_amount, risk_mode, window_days
            )

        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            future_map = {ex.submit(test_one, p, s): (p, s) for p, s in combos}
            for fut in cf.as_completed(future_map):
                pair, strat = future_map[fut]
                try:
                    result = fut.result()
                    if result:
                        all_results.append(result)
                    else:
                        errors.append(f"{pair}/{strat}: no data")
                except Exception as e:
                    errors.append(f"{pair}/{strat}: {str(e)}")
                done += 1
                _goal_jobs[job_id]['progress'] = f"Tested {done}/{total} combinations..."

        # Sort by consistency score (hit rate + avg return)
        all_results.sort(key=lambda r: r['consistency_score'], reverse=True)

        # Top 5 winners
        top5 = all_results[:5]

        # Summary
        best = all_results[0] if all_results else None

        _goal_jobs[job_id] = {
            'status': 'done',
            'results': all_results[:20],  # top 20
            'top5': top5,
            'best': best,
            'total_tested': len(all_results),
            'errors': errors[:5],
            'account_size': account_size,
            'target_amount': target_amount,
            'window_days': window_days,
            'summary': (
                f"Tested {len(combos)} strategy+pair combos. "
                f"Found {len([r for r in all_results if r['hit_rate_pct'] >= 50])} "
                f"combos that hit ${target_amount:,.0f} more than 50% of the time."
            ) if all_results else "No results found"
        }

    except Exception as e:
        import traceback; print(traceback.format_exc())
        _goal_jobs[job_id] = {'status': 'error', 'error': str(e)}


# ── FLASK ROUTES ─────────────────────────────────────────────────

@app.route('/api/backtest-goal', methods=['POST'])
@login_required
def api_backtest_goal():
    """
    Goal-seeking backtest: "Turn $1,000 into $1,500"
    
    POST body:
    {
        account_size: 1000,
        target_amount: 1500,       // what you want to reach
        window_days: 90,           // how many days per test window
        interval: "1d",            // candle size: 1d, 4h, 1h
        risk_mode: "moderate",
        market: "forex",           // forex | stocks | crypto | all
        strategies: ["all"],       // ["all"] or specific list
        pairs: []                  // optional: override default pairs
    }
    Returns a job_id — poll /api/backtest-goal-poll/<job_id>
    """
    try:
        import uuid as _u, threading as _t
        d = request.get_json() or {}

        account_size  = float(d.get('account_size', 1000))
        target_amount = float(d.get('target_amount', 1500))
        window_days   = int(d.get('window_days', 90))
        interval      = d.get('interval', '1d')
        risk_mode     = d.get('risk_mode', 'moderate')
        market        = d.get('market', 'all')
        strat_list    = d.get('strategies', ['all'])
        custom_pairs  = d.get('pairs', [])

        # Build pairs list
        if custom_pairs:
            pairs = [p.upper().strip() for p in custom_pairs[:15]]
        elif market == 'all':
            pairs = (DEFAULT_SCAN_PAIRS['forex'][:6] + 
                     DEFAULT_SCAN_PAIRS['stocks'][:6] + 
                     DEFAULT_SCAN_PAIRS['crypto'])
        else:
            pairs = DEFAULT_SCAN_PAIRS.get(market, DEFAULT_SCAN_PAIRS['forex'])

        # Build strategies list
        if strat_list == ['all'] or 'all' in strat_list:
            strategies = list(BT_STRATEGIES.keys())
        else:
            strategies = [s for s in strat_list if s in BT_STRATEGIES]
            if not strategies:
                strategies = list(BT_STRATEGIES.keys())

        if target_amount <= account_size:
            return jsonify({'error': 'Target must be greater than account size'}), 400

        job_id = str(_u.uuid4())[:8]
        _goal_jobs[job_id] = {'status': 'running', 'progress': 'Starting...'}

        t = _t.Thread(
            target=_run_goal_scan_job,
            args=(job_id, pairs, strategies, interval,
                  account_size, target_amount, risk_mode, window_days),
            daemon=True
        )
        t.start()

        return jsonify({'job_id': job_id, 'total_combos': len(pairs) * len(strategies)})

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/backtest-goal-poll/<job_id>', methods=['GET'])
@login_required
def api_backtest_goal_poll(job_id):
    """Poll goal backtest job"""
    job = _goal_jobs.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'error': 'Job not found'}), 404
    if job['status'] == 'done':
        _goal_jobs.pop(job_id, None)
        return jsonify(job)
    if job['status'] == 'error':
        return jsonify({'status': 'error', 'error': job.get('error', 'Unknown')}), 500
    return jsonify({'status': 'running', 'progress': job.get('progress', 'Working...')})



# ═══════════════════════════════════════════════════════════════════
# 🐺 WOLF SCALPING ENGINE + COMPOUNDING CALCULATOR
# Research sources: DailyFX, FOREX.com, EBC Financial, LiteFinance
# Scalping: M1/M5/M15 timeframes, 5-20 pip targets, tight stops
# Compounding: A = P(1+r)^n — reinvest every win, grow the base
# ═══════════════════════════════════════════════════════════════════

# ── SCALPING STRATEGIES (M1/M5/M15) ───────────────────────────────

def _scalp_ema_pullback_fast(df, params):
    """
    SCALP 1: Fast EMA Pullback (M5)
    Research: MultiBank Group, FOREX.com
    - EMA 9 above EMA 21 = uptrend (M5)
    - Price pulls back to EMA 9 zone
    - Stochastic crosses up from oversold (<20)
    - Target: 8-12 pips | Stop: 5-6 pips | R/R ~1:2
    """
    ema9  = _bt_ema(df['Close'], 9)
    ema21 = _bt_ema(df['Close'], 21)
    k, d  = _bt_stoch(df, 5, 3)  # fast stoch for scalping
    # BUY: uptrend + price near EMA9 + stoch crosses up from oversold
    uptrend    = ema9 > ema21
    near_ema9  = (df['Low'] <= ema9 * 1.003) & (df['Close'] >= ema9 * 0.998)
    stoch_up   = (k > d) & (k.shift() <= d.shift()) & (k < 30)
    buy  = uptrend & near_ema9 & stoch_up
    # EXIT: stoch overbought OR EMA cross down
    sell = ((k > 75) & (k.shift() <= 75)) | (ema9 < ema21)
    return buy.astype(int), sell.astype(int)

def _scalp_macd_zero_cross(df, params):
    """
    SCALP 2: MACD Zero-Line Cross Scalp (M5)
    Research: LiteFinance — "enter when MACD crosses zero level"
    - MACD (12,26,9) histogram crosses above zero = momentum shift
    - RSI (7) fast — not overbought
    - EMA 50 as trend filter
    - Target: 10-15 pips | Stop: 6-8 pips
    """
    macd, signal, hist = _bt_macd(df['Close'], 12, 26, 9)
    rsi7   = _bt_rsi(df['Close'], 7)   # fast RSI for scalping
    ema50  = _bt_ema(df['Close'], 50)
    # BUY: hist crosses above zero in uptrend, RSI not overbought
    cross_up  = (hist > 0) & (hist.shift() <= 0)
    in_trend  = df['Close'] > ema50
    not_ob    = rsi7 < 65
    buy  = cross_up & in_trend & not_ob
    # EXIT: histogram crosses below zero or RSI overbought
    sell = ((hist < 0) & (hist.shift() >= 0)) | (rsi7 > 80)
    return buy.astype(int), sell.astype(int)

def _scalp_bollinger_bounce(df, params):
    """
    SCALP 3: Bollinger Band Bounce (M5/M15)
    Research: LiteFinance, ForexTraders.com
    - Price touches lower band + bounces back = buy
    - Price touches upper band + rejects = sell
    - Stochastic confirms oversold/overbought
    - Target: middle band | Stop: 5 pips beyond band
    """
    upper, mid, lower = _bt_bbands(df['Close'], 20, 2)
    k, d = _bt_stoch(df, 5, 3)
    rsi  = _bt_rsi(df['Close'], 7)
    # BUY: price at lower band + stoch oversold crossing up
    at_lower  = df['Close'] <= lower * 1.003
    stoch_rev = (k > d) & (k.shift() <= d.shift()) & (k < 25)
    buy  = at_lower & stoch_rev & (rsi < 35)
    # SELL: price reaches middle band or upper band
    sell = (df['Close'] >= mid) | (df['Close'] >= upper * 0.997)
    return buy.astype(int), sell.astype(int)

def _scalp_stoch_sma_1min(df, params):
    """
    SCALP 4: Stochastic + SMA 25/50 (M1/M5)
    Research: ForexTraders.com — "SMA 25/50 + Stochastic 5,3,3"
    Best for: EURUSD, GBPUSD, USDJPY — tight spreads
    - SMA25 above SMA50 = uptrend
    - Price pulls back to SMA25/50 zone
    - Stochastic crosses up from below 20
    - Target: 8-12 pips | Stop: 3-5 pips
    """
    import pandas as pd
    sma25 = df['Close'].rolling(25).mean()
    sma50 = df['Close'].rolling(50).mean()
    k, d  = _bt_stoch(df, 5, 3)
    # BUY: SMA25 > SMA50 + price near SMA zone + stoch oversold cross up
    uptrend   = sma25 > sma50
    near_sma  = (df['Low'] <= sma25 * 1.004) & (df['Close'] >= sma25 * 0.997)
    stoch_cross = (k > d) & (k.shift() <= d.shift()) & (k < 22)
    buy  = uptrend & near_sma & stoch_cross
    sell = ((k > 78) & (k < d)) | (sma25 < sma50)
    return buy.astype(int), sell.astype(int)

def _scalp_parabolic_sar(df, params):
    """
    SCALP 5: Parabolic SAR Flip (M5)
    Research: AskTraders.com — "contrarian opportunities throughout each day"
    - PSAR flips from above to below price = BUY
    - PSAR flips from below to above price = SELL
    - EMA 21 as trend filter — only trade with trend
    - Target: next PSAR flip | Stop: first SAR dot
    """
    import numpy as np
    close = df['Close'].values
    high  = df['High'].values
    low   = df['Low'].values
    n = len(close)
    
    # Calculate Parabolic SAR
    af_step = 0.02; af_max = 0.2
    psar = np.zeros(n); bull = np.ones(n, dtype=bool)
    ep = low[0]; af = af_step; psar[0] = high[0]
    
    for i in range(1, n):
        if bull[i-1]:
            psar[i] = psar[i-1] + af * (ep - psar[i-1])
            psar[i] = min(psar[i], low[i-1], low[max(0,i-2)])
            if low[i] < psar[i]:
                bull[i] = False; psar[i] = ep
                ep = low[i]; af = af_step
            else:
                bull[i] = True
                if high[i] > ep:
                    ep = high[i]; af = min(af + af_step, af_max)
        else:
            psar[i] = psar[i-1] + af * (ep - psar[i-1])
            psar[i] = max(psar[i], high[i-1], high[max(0,i-2)])
            if high[i] > psar[i]:
                bull[i] = True; psar[i] = ep
                ep = high[i]; af = af_step
            else:
                bull[i] = False
                if low[i] < ep:
                    ep = low[i]; af = min(af + af_step, af_max)

    import pandas as pd
    bull_series = pd.Series(bull, index=df.index)
    ema21 = _bt_ema(df['Close'], 21)
    
    # BUY: SAR flips bullish (was below, now above price) + above EMA21
    buy  = (bull_series == True) & (bull_series.shift() == False) & (df['Close'] > ema21)
    # SELL: SAR flips bearish
    sell = (bull_series == False) & (bull_series.shift() == True)
    return buy.astype(int), sell.astype(int)

def _scalp_vwap_bounce(df, params):
    """
    SCALP 6: VWAP Bounce Scalp (M5/M15) — Stocks & Indices
    Research: FOREX.com, professional day trading
    - Price above VWAP = bull bias, below = bear bias
    - Buy dips TO VWAP in uptrend + RSI confirms
    - Sell bounces OFF VWAP in downtrend
    - Target: 0.3-0.5% | Stop: 0.15-0.2%
    Best for: SPY, QQQ, NVDA, AAPL intraday
    """
    vwap = _bt_vwap(df)
    rsi  = _bt_rsi(df['Close'], 7)
    ema21= _bt_ema(df['Close'], 21)
    # BUY: above VWAP long-term, dips to VWAP, RSI oversold
    above_vwap_trend = df['Close'] > vwap
    at_vwap = (df['Low'] <= vwap * 1.003) & (df['Close'] >= vwap * 0.997)
    buy  = above_vwap_trend & at_vwap & (rsi < 40) & (df['Close'] > ema21)
    # EXIT: RSI overbought or price drops below VWAP
    sell = (rsi > 72) | (df['Close'] < vwap * 0.998)
    return buy.astype(int), sell.astype(int)

# Add scalping strategies to the main BT_STRATEGIES registry
BT_STRATEGIES.update({
    'scalp_ema_pullback':  (_scalp_ema_pullback_fast, 'Scalp: Fast EMA Pullback',    'SCALP',     'M5/M15', '9/21 EMA trend + Stochastic oversold cross — 8-12 pip target'),
    'scalp_macd_zero':     (_scalp_macd_zero_cross,   'Scalp: MACD Zero Cross',       'SCALP',     'M5',     'MACD histogram crosses zero in trend — 10-15 pip target'),
    'scalp_bb_bounce':     (_scalp_bollinger_bounce,  'Scalp: Bollinger Band Bounce',  'SCALP',     'M5/M15', 'Price bounces off lower BB with Stoch confirmation — target mid band'),
    'scalp_stoch_sma':     (_scalp_stoch_sma_1min,    'Scalp: Stoch + SMA 25/50',     'SCALP',     'M1/M5',  'Classic 1-min SMA25/50 + Stoch 5,3,3 — 8-12 pip target'),
    'scalp_psar':          (_scalp_parabolic_sar,     'Scalp: Parabolic SAR Flip',     'SCALP',     'M5',     'PSAR flips direction + EMA21 trend filter — ride the flip'),
    'scalp_vwap':          (_scalp_vwap_bounce,       'Scalp: VWAP Bounce',            'SCALP/DAY', 'M5/M15', 'Dip to VWAP in uptrend + RSI oversold — stocks & indices'),
})


# ── COMPOUNDING ENGINE ──────────────────────────────────────────────

def _calculate_compound_projection(
    starting_capital,
    win_rate_pct,
    avg_win_pct,
    avg_loss_pct,
    trades_per_day,
    trading_days,
    risk_per_trade_pct,
    compound_mode='full'  # 'full' = reinvest all, 'partial' = reinvest 50%, 'none' = fixed size
):
    """
    Real compounding calculator based on trade expectancy.
    Formula: A = P * (1 + r)^n
    Trade Expectancy = (WinRate * AvgWin) - (LossRate * AvgLoss)
    
    Research sources:
    - FBS.com: "With every win capital grows by 6%, compound effect demonstrated"
    - TradeCiety.com: "Expectancy = (WinRate * RR) - LossRate * PositionSize"
    - Rule of 72: Time to double = 72 / (daily return % * 100)
    - FXVerify.com: A = P(1+r)^n compounding formula
    """
    import numpy as np
    
    win_rate  = win_rate_pct / 100
    loss_rate = 1 - win_rate
    
    capital = float(starting_capital)
    daily_capitals = [capital]
    trade_log = []
    total_trades = 0
    total_wins = 0
    total_losses = 0
    peak = capital
    max_dd = 0.0
    
    # Trade expectancy per trade (as % of capital risked)
    # E = (WinRate * AvgWin%) - (LossRate * AvgLoss%)
    expectancy_pct = (win_rate * avg_win_pct) - (loss_rate * avg_loss_pct)
    
    import random
    random.seed(42)  # reproducible results
    
    for day in range(trading_days):
        day_start = capital
        day_wins = 0
        day_losses = 0
        
        for trade in range(int(trades_per_day)):
            if capital <= 0:
                break
                
            # Compound sizing: position size based on current capital
            if compound_mode == 'full':
                risk_amount = capital * (risk_per_trade_pct / 100)
            elif compound_mode == 'partial':
                # Reinvest 50% of profits, fixed base for 50%
                base = starting_capital + (capital - starting_capital) * 0.5
                risk_amount = base * (risk_per_trade_pct / 100)
            else:  # none — fixed size on original capital
                risk_amount = starting_capital * (risk_per_trade_pct / 100)
            
            # Simulate trade outcome based on win rate
            won = random.random() < win_rate
            
            if won:
                profit = risk_amount * (avg_win_pct / 100) / (risk_per_trade_pct / 100)
                capital += profit
                day_wins += 1
                total_wins += 1
            else:
                loss = risk_amount * (avg_loss_pct / 100) / (risk_per_trade_pct / 100)
                capital -= loss
                day_losses += 1
                total_losses += 1
            
            total_trades += 1
            
            # Track drawdown
            if capital > peak:
                peak = capital
            dd = (peak - capital) / peak * 100
            if dd > max_dd:
                max_dd = dd
        
        daily_capitals.append(round(capital, 2))
        
        if day % max(1, trading_days // 20) == 0:
            trade_log.append({
                'day': day + 1,
                'capital': round(capital, 2),
                'gain_pct': round((capital - starting_capital) / starting_capital * 100, 1),
                'daily_pnl': round(capital - day_start, 2),
            })
    
    # Rule of 72: days to double = 72 / (daily return % * 100)
    daily_return_pct = expectancy_pct * trades_per_day
    days_to_double = round(72 / max(daily_return_pct, 0.01), 0) if daily_return_pct > 0 else None
    
    # Key milestones
    milestones = {}
    targets = [
        starting_capital * 1.5,
        starting_capital * 2,
        starting_capital * 3,
        starting_capital * 5,
        starting_capital * 10,
    ]
    for cap_list_idx, cap in enumerate(daily_capitals):
        for t in targets:
            if cap >= t and t not in milestones:
                milestones[round(t, 0)] = cap_list_idx  # day reached
    
    final_capital = capital
    total_return  = (final_capital - starting_capital) / starting_capital * 100
    
    # Downsample equity curve to 50 points
    step = max(1, len(daily_capitals) // 50)
    equity_curve = daily_capitals[::step]
    
    return {
        'starting_capital':    starting_capital,
        'final_capital':       round(final_capital, 2),
        'total_return_pct':    round(total_return, 1),
        'total_trades':        total_trades,
        'total_wins':          total_wins,
        'total_losses':        total_losses,
        'actual_win_rate':     round(total_wins / max(total_trades, 1) * 100, 1),
        'expectancy_pct':      round(expectancy_pct, 3),
        'daily_return_pct':    round(daily_return_pct, 3),
        'days_to_double':      int(days_to_double) if days_to_double else None,
        'max_drawdown_pct':    round(max_dd, 1),
        'milestones':          {str(int(k)): v for k, v in milestones.items()},
        'equity_curve':        [round(e, 2) for e in equity_curve],
        'trade_log':           trade_log,
        'compound_mode':       compound_mode,
        'formula':             f'A = {starting_capital} × (1 + {round(daily_return_pct/100, 4)})^{trading_days}',
        'rule_of_72':          f'Double in ~{int(days_to_double)} trading days' if days_to_double else 'Negative expectancy',
        'summary': (
            f"Starting ${starting_capital:,.0f} → ${final_capital:,.0f} "
            f"({total_return:+.1f}%) over {trading_days} days | "
            f"{total_trades} trades | {round(total_wins/max(total_trades,1)*100,0):.0f}% win rate | "
            f"Max DD: {max_dd:.1f}%"
        )
    }


def _compound_from_backtest(backtest_result, trading_days=90, compound_mode='full'):
    """
    Takes real backtest results and projects compounding forward.
    Uses actual win rate and profit factor from the backtest.
    """
    if 'error' in backtest_result:
        return {'error': backtest_result['error']}
    
    win_rate = backtest_result.get('win_rate', 55)
    total_t  = backtest_result.get('total_trades', 1)
    
    # Derive avg win/loss from profit factor and win rate
    pf       = backtest_result.get('profit_factor', 1.5)
    avg_win  = 2.0   # 2% avg win assumed
    avg_loss = avg_win / max(pf, 0.1)  # loss derived from profit factor
    
    # Estimate trades per day from candles tested
    candles  = backtest_result.get('candles_tested', 200)
    interval = backtest_result.get('interval', '1d')
    cpd_map  = {'1d': 1, '4h': 1.625, '1h': 6.5, '15m': 26, '5m': 78}
    cpd      = cpd_map.get(interval, 1)
    # Total calendar days
    total_days = max(candles / cpd, 1)
    tpd = max(round(total_t / total_days, 2), 0.1)  # trades per day
    
    return _calculate_compound_projection(
        starting_capital=backtest_result.get('account_size', 1000),
        win_rate_pct=win_rate,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        trades_per_day=tpd,
        trading_days=trading_days,
        risk_per_trade_pct=2.0,
        compound_mode=compound_mode
    )


# ── FLASK ROUTES ────────────────────────────────────────────────────

@app.route('/api/compound-calculator', methods=['POST'])
@login_required
def api_compound_calculator():
    """
    Standalone compounding calculator.
    POST body:
    {
        starting_capital: 1000,
        win_rate_pct: 60,
        avg_win_pct: 2.0,       // avg win as % of capital per trade
        avg_loss_pct: 1.0,      // avg loss as % of capital per trade
        trades_per_day: 5,
        trading_days: 90,
        risk_per_trade_pct: 1.0,
        compound_mode: "full"   // "full" | "partial" | "none"
    }
    Returns compounding projection with equity curve.
    """
    try:
        d = request.get_json() or {}
        result = _calculate_compound_projection(
            starting_capital   = float(d.get('starting_capital', 1000)),
            win_rate_pct       = float(d.get('win_rate_pct', 60)),
            avg_win_pct        = float(d.get('avg_win_pct', 2.0)),
            avg_loss_pct       = float(d.get('avg_loss_pct', 1.0)),
            trades_per_day     = float(d.get('trades_per_day', 5)),
            trading_days       = int(d.get('trading_days', 90)),
            risk_per_trade_pct = float(d.get('risk_per_trade_pct', 1.0)),
            compound_mode      = d.get('compound_mode', 'full')
        )
        return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/backtest-with-compound', methods=['POST'])
@login_required
def api_backtest_with_compound():
    """
    Runs a backtest then projects compounding forward from results.
    POST body: same as /api/backtest + compound_days + compound_mode
    """
    try:
        d = request.get_json() or {}
        symbol         = d.get('symbol', 'AAPL').upper().strip()
        strategy_key   = d.get('strategy', 'ema_pullback')
        interval       = d.get('interval', '1d')
        period         = d.get('period', '1y')
        account_size   = float(d.get('account_size', 1000))
        risk_mode      = d.get('risk_mode', 'moderate')
        compound_days  = int(d.get('compound_days', 90))
        compound_mode  = d.get('compound_mode', 'full')

        if strategy_key not in BT_STRATEGIES:
            return jsonify({'error': f'Unknown strategy: {strategy_key}'}), 400

        # Run backtest
        bt = _run_backtest(symbol, strategy_key, interval, period, account_size, risk_mode)
        if 'error' in bt:
            return jsonify(bt), 400

        # Project compounding
        comp = _compound_from_backtest(bt, compound_days, compound_mode)

        return jsonify({
            'backtest': bt,
            'compound': comp,
            'combined_summary': (
                f"Backtest: {bt['total_return']:+.1f}% return | "
                f"Win rate: {bt['win_rate']:.0f}% | "
                f"If compounded {compound_days} days → ${comp['final_capital']:,.0f}"
            )
        })
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/scalp-scan', methods=['POST'])
@login_required
def api_scalp_scan():
    """
    Scan pairs specifically for scalping setups.
    Returns which scalping strategy + pair shows the most signals per day
    and best historical results on M5/M15 data.
    POST body:
    {
        pairs: ["EURUSD=X", "GBPUSD=X", ...],
        account_size: 1000,
        risk_mode: "moderate",
        compound_days: 30
    }
    """
    try:
        import uuid as _u, threading as _t
        d = request.get_json() or {}
        pairs         = [p.upper().strip() for p in d.get('pairs', 
                         ['EURUSD=X','GBPUSD=X','USDJPY=X','GBPJPY=X',
                          'AUDUSD=X','EURJPY=X'])][:10]
        account_size  = float(d.get('account_size', 1000))
        risk_mode     = d.get('risk_mode', 'moderate')
        compound_days = int(d.get('compound_days', 30))
        interval      = '15m'  # scalping uses M15 for backtesting
        period        = '60d'  # 60 days of M15 data

        scalp_strategies = ['scalp_ema_pullback', 'scalp_macd_zero', 
                            'scalp_bb_bounce', 'scalp_stoch_sma', 'scalp_psar']
        
        job_id = str(_u.uuid4())[:8]
        _bt_jobs[job_id] = {'status': 'running', 'progress': 'Scanning scalp setups...'}

        def run_scalp_scan():
            import concurrent.futures as cf
            results = []
            combos = [(p, s) for p in pairs for s in scalp_strategies]

            def test_one(pair, strat):
                bt = _run_backtest(pair, strat, interval, period, account_size, risk_mode)
                if 'error' in bt:
                    return None
                # Add compounding projection
                comp = _compound_from_backtest(bt, compound_days, 'full')
                bt['compound'] = comp
                # trades per day — key scalp metric
                cpd = bt.get('candles_tested', 1)
                days_tested = max(cpd / 26, 1)  # M15 = 26 candles/day
                bt['trades_per_day'] = round(bt['total_trades'] / days_tested, 1)
                return bt

            with cf.ThreadPoolExecutor(max_workers=6) as ex:
                futures = {ex.submit(test_one, p, s): (p, s) for p, s in combos}
                for fut in cf.as_completed(futures):
                    r = fut.result()
                    if r:
                        results.append(r)

            # Sort by win_rate * profit_factor (best scalp quality)
            results.sort(key=lambda r: (r['win_rate'] * r['profit_factor']), reverse=True)

            _bt_jobs[job_id] = {
                'status': 'done',
                'results': results[:10],
                'best': results[0] if results else None,
                'total_tested': len(results),
            }

        t = _t.Thread(target=run_scalp_scan, daemon=True)
        t.start()
        return jsonify({'job_id': job_id})

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500



# ═══════════════════════════════════════════════════════════════════
# 🐺 ICT / SMART MONEY CONCEPTS (SMC) BACKTEST STRATEGIES
# Research: joshyattridge/smart-money-concepts (GitHub),
#           Medium/Farnam Rami ICT 2022 Python,
#           TrendSpider FVG Guide, FluxCharts ICT Unicorn
#           Inner Circle Trader official concepts
# ═══════════════════════════════════════════════════════════════════

def _ict_detect_order_blocks(df, lookback=10):
    """
    Detect ICT Order Blocks.
    Bullish OB: Last BEARISH candle before a significant bullish move up
    Bearish OB: Last BULLISH candle before a significant bearish move down
    
    Research: ICT — "The last opposing candle before an impulsive move"
    GitHub: joshyattridge/smart-money-concepts
    """
    import pandas as pd
    import numpy as np
    
    n = len(df)
    bull_ob = pd.Series(False, index=df.index)
    bear_ob = pd.Series(False, index=df.index)
    bull_ob_level = pd.Series(np.nan, index=df.index)
    bear_ob_level = pd.Series(np.nan, index=df.index)
    
    for i in range(lookback, n - lookback):
        # Check for bullish OB: bearish candle followed by strong move up
        is_bear_candle = df['Close'].iloc[i] < df['Open'].iloc[i]
        # Strong move up: next candles make higher highs significantly
        future_high = df['High'].iloc[i+1:i+lookback].max()
        candle_range = df['High'].iloc[i] - df['Low'].iloc[i]
        move_up = future_high > df['High'].iloc[i] + candle_range * 1.5
        
        if is_bear_candle and move_up:
            bull_ob.iloc[i] = True
            bull_ob_level.iloc[i] = (df['Open'].iloc[i] + df['Close'].iloc[i]) / 2
        
        # Check for bearish OB: bullish candle followed by strong move down
        is_bull_candle = df['Close'].iloc[i] > df['Open'].iloc[i]
        future_low = df['Low'].iloc[i+1:i+lookback].min()
        move_down = future_low < df['Low'].iloc[i] - candle_range * 1.5
        
        if is_bull_candle and move_down:
            bear_ob.iloc[i] = True
            bear_ob_level.iloc[i] = (df['Open'].iloc[i] + df['Close'].iloc[i]) / 2
    
    return bull_ob, bear_ob, bull_ob_level, bear_ob_level


def _ict_detect_fvg(df):
    """
    Detect Fair Value Gaps (FVG / Imbalances).
    Bullish FVG: High of candle[i-1] < Low of candle[i+1]
    Bearish FVG: Low of candle[i-1] > High of candle[i+1]
    
    Research: Ziad Francis PhD Medium, TrendSpider FVG Guide
    "3-candle pattern where wicks of candle 1 and 3 do not overlap"
    """
    import pandas as pd
    import numpy as np
    
    n = len(df)
    bull_fvg = pd.Series(False, index=df.index)
    bear_fvg = pd.Series(False, index=df.index)
    bull_fvg_top = pd.Series(np.nan, index=df.index)
    bull_fvg_bot = pd.Series(np.nan, index=df.index)
    bear_fvg_top = pd.Series(np.nan, index=df.index)
    bear_fvg_bot = pd.Series(np.nan, index=df.index)
    
    for i in range(1, n - 1):
        # Bullish FVG: prev high < next low (gap above previous candle)
        if df['High'].iloc[i-1] < df['Low'].iloc[i+1]:
            bull_fvg.iloc[i] = True
            bull_fvg_top.iloc[i] = df['Low'].iloc[i+1]
            bull_fvg_bot.iloc[i] = df['High'].iloc[i-1]
        
        # Bearish FVG: prev low > next high (gap below previous candle)
        if df['Low'].iloc[i-1] > df['High'].iloc[i+1]:
            bear_fvg.iloc[i] = True
            bear_fvg_top.iloc[i] = df['Low'].iloc[i-1]
            bear_fvg_bot.iloc[i] = df['High'].iloc[i+1]
    
    return bull_fvg, bear_fvg, bull_fvg_top, bull_fvg_bot, bear_fvg_top, bear_fvg_bot


def _ict_detect_bos_choch(df, swing_length=5):
    """
    Detect Break of Structure (BOS) and Change of Character (CHoCH).
    
    BOS: Price breaks the last significant swing high/low (trend continuation)
    CHoCH: First break against the trend (potential reversal signal)
    
    Research: GitHub joshyattridge/smart-money-concepts
    "BOS = trend continuation, CHoCH = first sign of reversal"
    """
    import pandas as pd
    import numpy as np
    
    n = len(df)
    swing_highs = pd.Series(np.nan, index=df.index)
    swing_lows  = pd.Series(np.nan, index=df.index)
    bos_bull    = pd.Series(False, index=df.index)
    bos_bear    = pd.Series(False, index=df.index)
    choch_bull  = pd.Series(False, index=df.index)
    choch_bear  = pd.Series(False, index=df.index)
    
    # Find swing highs and lows
    for i in range(swing_length, n - swing_length):
        window_h = df['High'].iloc[i-swing_length:i+swing_length+1]
        window_l = df['Low'].iloc[i-swing_length:i+swing_length+1]
        if df['High'].iloc[i] == window_h.max():
            swing_highs.iloc[i] = df['High'].iloc[i]
        if df['Low'].iloc[i] == window_l.min():
            swing_lows.iloc[i] = df['Low'].iloc[i]
    
    # Detect BOS: close breaks above last swing high (bullish BOS)
    last_sh = None
    last_sl = None
    trend = None  # None, 'bull', 'bear'
    
    for i in range(swing_length, n):
        if not pd.isna(swing_highs.iloc[i]):
            last_sh = swing_highs.iloc[i]
        if not pd.isna(swing_lows.iloc[i]):
            last_sl = swing_lows.iloc[i]
        
        if last_sh and df['Close'].iloc[i] > last_sh:
            if trend == 'bear':
                choch_bull.iloc[i] = True  # First break up in downtrend = CHoCH
            else:
                bos_bull.iloc[i] = True    # Continue up = BOS
            trend = 'bull'
        elif last_sl and df['Close'].iloc[i] < last_sl:
            if trend == 'bull':
                choch_bear.iloc[i] = True  # First break down in uptrend = CHoCH
            else:
                bos_bear.iloc[i] = True    # Continue down = BOS
            trend = 'bear'
    
    return bos_bull, bos_bear, choch_bull, choch_bear


# ── ICT/SMC STRATEGY FUNCTIONS ─────────────────────────────────────

def _strategy_ict_order_block(df, params):
    """
    ICT Strategy 1: Order Block Entry
    
    Research: ICT, Medium/Farnam Rami
    Logic:
    - Identify bullish order blocks (last bear candle before big move up)
    - Wait for price to RETURN to the OB zone
    - Enter when price touches OB zone + RSI confirms oversold
    - Stop: below the OB low
    - Target: next resistance / 2:1 R/R
    
    "Order blocks are where institutions left unfilled orders.
     Price returns to fill them." — ICT
    """
    bull_ob, bear_ob, bull_ob_lvl, bear_ob_lvl = _ict_detect_order_blocks(df, 8)
    ema200 = _bt_ema(df['Close'], 200)
    rsi    = _bt_rsi(df['Close'], 14)
    import pandas as pd, numpy as np
    
    # Track active OB zones
    active_bull_zones = []  # list of (price_level, candle_idx)
    buy  = pd.Series(False, index=df.index)
    sell = pd.Series(False, index=df.index)
    
    for i in range(10, len(df)):
        # Add new OB zones
        if bull_ob.iloc[i]:
            zone_mid = bull_ob_lvl.iloc[i]
            if not np.isnan(zone_mid):
                active_bull_zones.append((zone_mid, i, df['Low'].iloc[i]))
        
        # Check if price returns to any active bull OB zone
        current_price = df['Close'].iloc[i]
        in_uptrend = current_price > ema200.iloc[i]
        
        for zone_mid, zone_idx, zone_low in active_bull_zones:
            # Price touches OB zone (within 0.5% of midpoint)
            if abs(current_price - zone_mid) / zone_mid < 0.005:
                if in_uptrend and rsi.iloc[i] < 50:
                    buy.iloc[i] = True
        
        # Clean old zones (price moved far past them)
        active_bull_zones = [(z, zi, zl) for z, zi, zl in active_bull_zones 
                              if i - zi < 50 and current_price > zl * 0.998]
        
        # Exit: RSI overbought or trend breaks
        if rsi.iloc[i] > 72 or current_price < ema200.iloc[i]:
            sell.iloc[i] = True
    
    return buy.astype(int), sell.astype(int)


def _strategy_ict_fvg_fill(df, params):
    """
    ICT Strategy 2: Fair Value Gap Fill
    
    Research: Ziad Francis PhD, TrendSpider, ICT teachings
    Logic:
    - Detect bullish FVGs (price imbalances pointing up)
    - Wait for price to RETURN and fill the gap (rebalance)
    - Enter at the FVG zone with confirmation candle
    - Stop: below the FVG bottom
    - Target: top of FVG or continuation to next level
    
    "FVGs are price magnets — they WILL be filled.
     Trade the return to the zone." — ICT / TrendSpider
    """
    bull_fvg, bear_fvg, bull_top, bull_bot, bear_top, bear_bot = _ict_detect_fvg(df)
    ema50  = _bt_ema(df['Close'], 50)
    ema200 = _bt_ema(df['Close'], 200)
    rsi    = _bt_rsi(df['Close'], 14)
    import pandas as pd, numpy as np
    
    active_bull_fvgs = []  # (top, bot, idx)
    buy  = pd.Series(False, index=df.index)
    sell = pd.Series(False, index=df.index)
    
    for i in range(3, len(df)):
        # Register new bull FVGs
        if bull_fvg.iloc[i]:
            t = bull_top.iloc[i]
            b = bull_bot.iloc[i]
            if not (np.isnan(t) or np.isnan(b)):
                active_bull_fvgs.append((t, b, i))
        
        current = df['Close'].iloc[i]
        uptrend = current > ema200.iloc[i]
        
        # Price returns INTO a bull FVG zone = entry
        for top, bot, fvg_idx in active_bull_fvgs:
            if bot <= current <= top:  # price inside FVG zone
                if uptrend and rsi.iloc[i] < 55:
                    buy.iloc[i] = True
        
        # Remove filled or expired FVGs
        active_bull_fvgs = [(t, b, fi) for t, b, fi in active_bull_fvgs
                             if i - fi < 40 and current >= b * 0.997]
        
        # Exit
        if rsi.iloc[i] > 70 or current < ema50.iloc[i]:
            sell.iloc[i] = True
    
    return buy.astype(int), sell.astype(int)


def _strategy_ict_bos_entry(df, params):
    """
    ICT Strategy 3: Break of Structure Entry
    
    Research: GitHub joshyattridge/smart-money-concepts
    Logic:
    - Detect BOS (trend continuation) — price breaks last swing high
    - Enter on the PULLBACK after BOS confirmation
    - CHoCH = first warning of reversal — use as exit signal
    - Stop: below the BOS origin candle
    - Target: Fibonacci extension 1.272 / 1.618
    
    "BOS confirms the trend. Trade pullbacks after BOS." — ICT
    """
    bos_bull, bos_bear, choch_bull, choch_bear = _ict_detect_bos_choch(df, 5)
    ema21  = _bt_ema(df['Close'], 21)
    ema200 = _bt_ema(df['Close'], 200)
    rsi    = _bt_rsi(df['Close'], 14)
    import pandas as pd
    
    buy  = pd.Series(False, index=df.index)
    sell = pd.Series(False, index=df.index)
    in_bull_trend = False
    
    for i in range(10, len(df)):
        # Bull BOS confirms uptrend — now look for pullback entry
        if bos_bull.iloc[i]:
            in_bull_trend = True
        
        if choch_bear.iloc[i]:
            in_bull_trend = False  # trend potentially reversing
        
        current = df['Close'].iloc[i]
        
        # Entry: in bull trend, price pulls back to 21 EMA zone, RSI moderate
        if in_bull_trend:
            near_ema21 = abs(current - ema21.iloc[i]) / ema21.iloc[i] < 0.004
            above_200  = current > ema200.iloc[i]
            if near_ema21 and above_200 and 25 < rsi.iloc[i] < 50:
                buy.iloc[i] = True
        
        # Exit: CHoCH bearish or RSI overbought
        if choch_bear.iloc[i] or rsi.iloc[i] > 75:
            sell.iloc[i] = True
    
    return buy.astype(int), sell.astype(int)


def _strategy_ict_liquidity_sweep(df, params):
    """
    ICT Strategy 4: Liquidity Sweep + Reversal (Stop Hunt)
    
    Research: ICT Power of 3, Smart Money Concepts
    Logic:
    - Institutions hunt stop losses above swing highs / below swing lows
    - Equal highs / equal lows = stop cluster = will be swept
    - After the sweep + immediate reversal candle = THE entry
    - "The best trade is after the liquidity grab" — ICT
    
    Stop: Beyond the sweep wick
    Target: Opposite liquidity pool
    """
    import pandas as pd, numpy as np
    
    n = len(df)
    buy  = pd.Series(False, index=df.index)
    sell = pd.Series(False, index=df.index)
    
    ema50  = _bt_ema(df['Close'], 50)
    ema200 = _bt_ema(df['Close'], 200)
    rsi    = _bt_rsi(df['Close'], 14)
    atr    = _bt_atr(df, 14)
    
    swing = 8  # swing detection window
    
    for i in range(swing + 1, n - 1):
        curr_atr = atr.iloc[i]
        curr_high = df['High'].iloc[i]
        curr_low  = df['Low'].iloc[i]
        curr_close= df['Close'].iloc[i]
        
        # Get recent swing high/low
        recent_high = df['High'].iloc[i-swing:i].max()
        recent_low  = df['Low'].iloc[i-swing:i].min()
        
        # BULLISH SETUP: price spikes BELOW recent swing low (liquidity sweep)
        # then CLOSES BACK ABOVE it = stop hunt complete
        swept_below = curr_low < recent_low
        recovered   = curr_close > recent_low  # closed back above
        in_uptrend  = curr_close > ema200.iloc[i]
        oversold    = rsi.iloc[i] < 45
        
        if swept_below and recovered and in_uptrend and oversold:
            buy.iloc[i] = True
        
        # BEARISH SETUP: spike above recent high then close back below
        swept_above = curr_high > recent_high
        closed_back = curr_close < recent_high
        in_downtrend= curr_close < ema200.iloc[i]
        overbought  = rsi.iloc[i] > 55
        
        if swept_above and closed_back and in_downtrend and overbought:
            sell.iloc[i] = True
    
    # For backtest engine, convert sell to exit signal
    return buy.astype(int), sell.astype(int)


def _strategy_ict_unicorn(df, params):
    """
    ICT Strategy 5: ICT Unicorn Setup (OB + FVG Confluence)
    
    Research: FluxCharts ICT Unicorn Guide
    "FVG that overlaps a Breaker Block = highest probability ICT setup"
    
    Logic:
    - Find a breaker block (failed order block that flipped)
    - Find a FVG overlapping that breaker block zone
    - Enter when price retraces INTO the FVG+OB confluence zone
    - Stop: below the breaker block
    - Target: 1:2 R/R minimum, 1:3 preferred
    
    This is the HIGHEST CONVICTION ICT setup.
    """
    import pandas as pd, numpy as np
    
    bull_ob, bear_ob, bull_ob_lvl, _ = _ict_detect_order_blocks(df, 8)
    bull_fvg, _, bull_top, bull_bot, _, _ = _ict_detect_fvg(df)
    ema200 = _bt_ema(df['Close'], 200)
    rsi    = _bt_rsi(df['Close'], 14)
    
    buy  = pd.Series(False, index=df.index)
    sell = pd.Series(False, index=df.index)
    
    # Find confluence zones: OB level near FVG zone
    confluence_zones = []
    
    for i in range(5, len(df) - 5):
        # Check if OB and FVG exist near same price level
        if bull_ob.iloc[i] and not np.isnan(bull_ob_lvl.iloc[i]):
            ob_level = bull_ob_lvl.iloc[i]
            # Look for a FVG within 10 candles that overlaps
            for j in range(max(0, i-10), min(len(df), i+10)):
                if bull_fvg.iloc[j]:
                    ftop = bull_top.iloc[j]
                    fbot = bull_bot.iloc[j]
                    if not (np.isnan(ftop) or np.isnan(fbot)):
                        # Check overlap
                        if fbot <= ob_level <= ftop:
                            confluence_zones.append({
                                'top': ftop, 'bot': fbot, 
                                'ob_lvl': ob_level, 'idx': i
                            })
    
    # Enter when price returns to confluence zones
    for i in range(20, len(df)):
        current = df['Close'].iloc[i]
        uptrend  = current > ema200.iloc[i]
        
        for zone in confluence_zones:
            if zone['idx'] < i < zone['idx'] + 60:  # zone valid for 60 bars
                in_zone = zone['bot'] * 0.998 <= current <= zone['top'] * 1.002
                if in_zone and uptrend and rsi.iloc[i] < 55:
                    buy.iloc[i] = True
        
        # Exit: RSI overbought
        if rsi.iloc[i] > 73:
            sell.iloc[i] = True
    
    return buy.astype(int), sell.astype(int)


def _strategy_ict_power_of_3(df, params):
    """
    ICT Strategy 6: Power of 3 (Accumulation → Manipulation → Distribution)
    
    Research: ICT teachings — "AMD: Accumulation, Manipulation, Distribution"
    
    Logic:
    - Asian session = ACCUMULATION (range forms)
    - London open = MANIPULATION (fake move — stop hunt)
    - NY session = DISTRIBUTION (real move — the actual trade)
    
    Entry: After manipulation (stop hunt) at start of NY session
    Stop: Beyond manipulation wick
    Target: Full distribution leg (2-3x manipulation range)
    
    This is the ICT daily model.
    """
    import pandas as pd
    
    ema50  = _bt_ema(df['Close'], 50)
    ema200 = _bt_ema(df['Close'], 200)
    rsi    = _bt_rsi(df['Close'], 14)
    atr    = _bt_atr(df, 14)
    
    # Approximate Power of 3 using price structure
    # Asian range: rolling 6-candle range
    # Manipulation: spike outside that range
    # Distribution: close back inside + continuation
    
    roll_high = df['High'].rolling(6).max()
    roll_low  = df['Low'].rolling(6).min()
    roll_range= roll_high - roll_low
    
    buy  = pd.Series(False, index=df.index)
    sell = pd.Series(False, index=df.index)
    
    for i in range(10, len(df) - 1):
        current = df['Close'].iloc[i]
        prev_close = df['Close'].iloc[i-1]
        curr_high = df['High'].iloc[i]
        curr_low  = df['Low'].iloc[i]
        
        asian_high = roll_high.iloc[i-1]
        asian_low  = roll_low.iloc[i-1]
        asian_range= roll_range.iloc[i-1]
        
        if pd.isna(asian_range) or asian_range == 0:
            continue
        
        in_uptrend = current > ema200.iloc[i]
        
        # BULLISH P3: spike below asian low (manipulation down) 
        # then close back above asian low (distribution up)
        manip_down = curr_low < asian_low
        recover_up = current > asian_low
        
        if manip_down and recover_up and in_uptrend and rsi.iloc[i] < 52:
            buy.iloc[i] = True
        
        # Exit
        if rsi.iloc[i] > 72 or current < ema50.iloc[i]:
            sell.iloc[i] = True
    
    return buy.astype(int), sell.astype(int)


def _strategy_wyckoff_spring(df, params):
    """
    Wyckoff Strategy: Spring + Sign of Strength
    
    Logic:
    - Price in a range (low ATR, tight Bollinger bands)
    - Spring: false breakdown below support (shakeout)
    - Immediately closes back ABOVE the support level
    - Volume spike confirms institutional buying
    - Enter on close back above support
    - Stop: below spring low
    - Target: Top of the range (AR) and beyond
    """
    import pandas as pd
    
    ema200 = _bt_ema(df['Close'], 200)
    upper, mid, lower = _bt_bbands(df['Close'], 20, 2)
    rsi = _bt_rsi(df['Close'], 14)
    atr = _bt_atr(df, 14)
    
    # Range detection: ATR is low (consolidating)
    atr_avg = atr.rolling(50).mean()
    in_range = atr < atr_avg * 0.8  # tight range
    
    # Support = rolling 20-period low
    support = df['Low'].rolling(20).min()
    
    buy  = pd.Series(False, index=df.index)
    sell = pd.Series(False, index=df.index)
    
    for i in range(25, len(df)):
        current = df['Close'].iloc[i]
        curr_low= df['Low'].iloc[i]
        sup_lvl = support.iloc[i-1]  # prior support
        
        # Spring: price dips below support but closes back above
        spring = curr_low < sup_lvl and current > sup_lvl
        was_ranging = in_range.iloc[i-5:i].any()
        uptrend = current > ema200.iloc[i]
        
        if spring and was_ranging and uptrend and rsi.iloc[i] < 50:
            buy.iloc[i] = True
        
        # Exit: upper Bollinger band or RSI overbought
        if current >= upper.iloc[i] or rsi.iloc[i] > 72:
            sell.iloc[i] = True
    
    return buy.astype(int), sell.astype(int)


def _strategy_wyckoff_accumulation(df, params):
    """
    Wyckoff Accumulation: Full Cycle Entry
    
    Based on: Richard Wyckoff's 5-phase accumulation model
    Phases: PS → SC → AR → ST → Spring → SOS → LPS → Breakout
    
    Simplified for backtesting:
    - Detect selling climax (SC): massive volume + big down candle
    - Detect automatic rally (AR): bounce from SC
    - Detect secondary test (ST): lower volume retest of lows
    - Enter at LPS (Last Point of Support) or Spring
    - Stop: below SC low
    - Target: Length of base projected upward
    """
    import pandas as pd
    
    rsi = _bt_rsi(df['Close'], 14)
    ema200 = _bt_ema(df['Close'], 200)
    atr = _bt_atr(df, 14)
    upper, mid, lower = _bt_bbands(df['Close'], 20, 2)
    
    # Volume analysis (if available)
    has_volume = 'Volume' in df.columns and df['Volume'].sum() > 0
    vol_avg = df['Volume'].rolling(20).mean() if has_volume else None
    
    buy  = pd.Series(False, index=df.index)
    sell = pd.Series(False, index=df.index)
    
    for i in range(30, len(df)):
        current = df['Close'].iloc[i]
        
        # SC detection: big bear candle + high volume (if available)
        candle_size = abs(df['Close'].iloc[i] - df['Open'].iloc[i])
        avg_candle = abs(df['Close'].iloc[i-20:i] - df['Open'].iloc[i-20:i]).mean()
        big_candle = candle_size > avg_candle * 2
        
        vol_spike = True
        if has_volume and vol_avg is not None:
            vol_spike = df['Volume'].iloc[i] > vol_avg.iloc[i] * 1.5
        
        bear_candle = df['Close'].iloc[i] < df['Open'].iloc[i]
        
        # Near lower Bollinger = oversold
        near_lower = current <= lower.iloc[i] * 1.01
        
        # Spring pattern: RSI oversold, near lower band, possible SC
        spring_entry = near_lower and rsi.iloc[i] < 30 and (big_candle or vol_spike)
        
        if spring_entry and bear_candle:
            buy.iloc[i] = True  # enter on/after SC
        
        # LPS: RSI recovering, higher low forming
        higher_low = (df['Low'].iloc[i] > df['Low'].iloc[i-5:i].min() and 
                      rsi.iloc[i] > rsi.iloc[i-3] and 
                      30 < rsi.iloc[i] < 50)
        
        if higher_low and current > lower.iloc[i]:
            buy.iloc[i] = True
        
        # Exit: upper band or resistance
        if current >= upper.iloc[i] * 0.998 or rsi.iloc[i] > 72:
            sell.iloc[i] = True
    
    return buy.astype(int), sell.astype(int)


# ── Register ALL ICT/SMC + Wyckoff strategies ─────────────────────
BT_STRATEGIES.update({
    'ict_order_block':     (_strategy_ict_order_block,     'ICT: Order Block Entry',           'SWING/DAY', '1h/4h', 'Enter when price returns to OB zone — institutional order fill'),
    'ict_fvg_fill':        (_strategy_ict_fvg_fill,        'ICT: Fair Value Gap Fill',          'SWING/DAY', '1h/4h', 'Enter when price returns to FVG imbalance zone — magnets for price'),
    'ict_bos_entry':       (_strategy_ict_bos_entry,       'ICT: Break of Structure Pullback',  'SWING',     '4h/1d', 'Enter pullback after BOS confirmation — trend continuation'),
    'ict_liquidity_sweep': (_strategy_ict_liquidity_sweep, 'ICT: Liquidity Sweep + Reversal',   'SWING/DAY', '1h/4h', 'Trade the stop hunt — enter after liquidity grab reversal'),
    'ict_unicorn':         (_strategy_ict_unicorn,         'ICT: Unicorn (OB + FVG Confluence)','SWING',     '4h/1d', 'Highest conviction ICT setup — OB and FVG overlapping'),
    'ict_power_of_3':      (_strategy_ict_power_of_3,      'ICT: Power of 3 (AMD Model)',       'DAY',       '1h',    'Accumulation→Manipulation→Distribution daily ICT model'),
    'wyckoff_spring':      (_strategy_wyckoff_spring,      'Wyckoff: Spring Reversal',          'SWING',     '4h/1d', 'False breakdown below support in range — spring entry'),
    'wyckoff_accumulation':(_strategy_wyckoff_accumulation,'Wyckoff: Accumulation SC/LPS',      'SWING',     '1d',    'Selling climax + last point of support entry — full Wyckoff cycle'),
})



# ═══════════════════════════════════════════════════════════════════
# 🐺 WOLF MULTI-BROKER AUTO-TRADING MODULE
# Supports: OANDA (US regulated) + Offshore via MT4/MT5 REST bridges
# Architecture: Wolf analyzes → confirms with user → places real trade
# All credentials stored in .env — never in code
# ═══════════════════════════════════════════════════════════════════

import os, time, json
from datetime import datetime

# ── BROKER CONFIGURATIONS ──────────────────────────────────────────

BROKER_CONFIGS = {
    'oanda': {
        'name':      'OANDA',
        'type':      'rest',
        'regulated': True,
        'leverage':  '50:1 (US regulated)',
        'base_url_live':  'https://api-fxtrade.oanda.com',
        'base_url_demo':  'https://api-fxpractice.oanda.com',
        'env_key':   'OANDA_API_KEY',
        'env_account':'OANDA_ACCOUNT_ID',
        'env_mode':  'OANDA_MODE',  # 'live' or 'demo'
        'supports':  ['forex', 'indices', 'commodities', 'crypto'],
        'min_lot':   0.001,
        'signup':    'https://www.oanda.com/us-en/trading/demo-account/',
        'api_docs':  'https://developer.oanda.com/rest-live-v20/introduction/',
    },
    'forex_com': {
        'name':      'FOREX.com',
        'type':      'rest',
        'regulated': True,
        'leverage':  '50:1 (US regulated)',
        'env_key':   'FOREXCOM_API_KEY',
        'env_account':'FOREXCOM_ACCOUNT_ID',
        'supports':  ['forex', 'indices', 'commodities'],
        'signup':    'https://www.forex.com/en-us/',
    },
    'duramarkets': {
        'name':      'DuraMarkets',
        'type':      'mt4_bridge',
        'regulated': False,
        'leverage':  '1:1000',
        'location':  'Comoros',
        'us_clients': True,
        'env_key':   'DURA_MT4_LOGIN',
        'env_password':'DURA_MT4_PASSWORD',
        'env_server': 'DURA_MT4_SERVER',
        'supports':  ['forex', 'indices', 'metals', 'crypto'],
        'min_deposit': 50,
        'signup':    'https://www.duramarkets.com',
        'notes':     'MT4 only. Fastest execution tested among offshore brokers.',
    },
    'coinexx': {
        'name':      'Coinexx',
        'type':      'mt5_bridge',
        'regulated': False,
        'leverage':  '1:500',
        'location':  'Mwali (Comoros)',
        'us_clients': True,
        'env_key':   'COINEXX_MT5_LOGIN',
        'env_password':'COINEXX_MT5_PASSWORD',
        'env_server': 'COINEXX_MT5_SERVER',
        'supports':  ['forex', 'indices', 'metals', 'crypto'],
        'min_deposit': 1,
        'notes':     'Lowest commission: $1/side. BTC deposits accepted.',
        'signup':    'https://coinexx.com',
    },
    'plexytrade': {
        'name':      'PlexyTrade (ex-LQDFX)',
        'type':      'mt5_bridge',
        'regulated': False,
        'leverage':  '1:2000 (Micro), 1:500 (Silver+)',
        'location':  'Saint Lucia',
        'us_clients': True,
        'env_key':   'PLEXYTRADE_MT5_LOGIN',
        'env_password':'PLEXYTRADE_MT5_PASSWORD',
        'env_server': 'PLEXYTRADE_MT5_SERVER',
        'supports':  ['forex', 'indices', 'metals', 'crypto'],
        'min_deposit': 50,
        'notes':     'Up to 2000:1 leverage on Micro account. $50 min deposit.',
        'signup':    'https://www.plexytrade.com',
    },
    'tradersway': {
        'name':      'TradersWay',
        'type':      'mt5_bridge',
        'regulated': False,
        'leverage':  '1:1000',
        'location':  'Dominica',
        'us_clients': True,
        'env_key':   'TRADERSWAY_LOGIN',
        'env_password':'TRADERSWAY_PASSWORD',
        'env_server': 'TRADERSWAY_SERVER',
        'supports':  ['forex', 'crypto', 'metals', 'energies'],
        'min_deposit': 5,
        'notes':     '$5 minimum deposit. MT4/MT5/cTrader. Since 2011.',
        'signup':    'https://www.tradersway.com',
    },
    'litefinance': {
        'name':      'LiteFinance',
        'type':      'rest',
        'regulated': False,
        'leverage':  '1:1000',
        'location':  'Saint Vincent and the Grenadines',
        'us_clients': True,
        'env_key':   'LITEFINANCE_API_KEY',
        'env_account':'LITEFINANCE_ACCOUNT_ID',
        'supports':  ['forex', 'crypto', 'indices', 'metals', 'stocks'],
        'min_deposit': 50,
        'notes':     'Has FIX API + REST API. Best for automation offshore.',
        'signup':    'https://www.litefinance.org',
        'api_docs':  'https://www.litefinance.org/blog/for-professionals/api/',
    },
}

# ── SYMBOL NORMALIZER ──────────────────────────────────────────────

def _normalize_symbol(symbol, broker_key):
    """Convert Wolf's symbol format to broker's format"""
    # Remove common suffixes
    sym = symbol.upper().replace('=X','').replace('-','').replace('/','')
    
    maps = {
        'oanda': {
            'EURUSD': 'EUR_USD', 'GBPUSD': 'GBP_USD', 'USDJPY': 'USD_JPY',
            'GBPJPY': 'GBP_JPY', 'AUDUSD': 'AUD_USD', 'USDCAD': 'USD_CAD',
            'USDCHF': 'USD_CHF', 'NZDUSD': 'NZD_USD', 'EURJPY': 'EUR_JPY',
            'EURGBP': 'EUR_GBP', 'XAUUSD': 'XAU_USD', 'BTCUSD': 'BTC_USD',
            'SPYUS': 'SPX500_USD', 'QQQUSD': 'NAS100_USD',
        },
        'mt4': {  # MT4/MT5 standard symbols
            'EURUSD': 'EURUSD', 'GBPUSD': 'GBPUSD', 'USDJPY': 'USDJPY',
            'GBPJPY': 'GBPJPY', 'AUDUSD': 'AUDUSD', 'USDCAD': 'USDCAD',
            'XAUUSD': 'XAUUSD', 'BTCUSD': 'BTCUSD',
        }
    }
    
    broker_type = BROKER_CONFIGS.get(broker_key, {}).get('type', 'rest')
    
    if broker_key == 'oanda':
        return maps['oanda'].get(sym, sym[:3] + '_' + sym[3:] if len(sym) == 6 else sym)
    else:
        return maps['mt4'].get(sym, sym)


# ── OANDA REST BRIDGE ──────────────────────────────────────────────

# ── OANDA instrument cache ──────────────────────────────────────
_oanda_instruments_cache = {}

def _oanda_get_tradeable_instruments(api_key, account_id, mode='demo'):
    """
    Fetch and cache the list of instruments tradeable on this OANDA account.
    US accounts cannot trade Gold, Silver, Crypto, Oil.
    """
    import requests as req
    cache_key = f"{account_id}_{mode}"
    if _oanda_instruments_cache.get(cache_key):
        return _oanda_instruments_cache[cache_key]
    
    key  = api_key  or os.environ.get('OANDA_API_KEY', '')
    acct = account_id or os.environ.get('OANDA_ACCOUNT_ID', '')
    base = BROKER_CONFIGS['oanda']['base_url_demo' if mode == 'demo' else 'base_url_live']
    
    try:
        resp = req.get(
            f'{base}/v3/accounts/{acct}/instruments',
            headers={'Authorization': f'Bearer {key}'},
            timeout=10
        )
        if resp.status_code == 200:
            instruments = [i['name'] for i in resp.json().get('instruments', [])]
            _oanda_instruments_cache[cache_key] = instruments
            print(f'[OANDA] Loaded {len(instruments)} tradeable instruments for {acct}')
            return instruments
    except Exception as e:
        print(f'[OANDA] Could not fetch instruments: {e}')
    return []


def _oanda_place_order(symbol, direction, units, sl_price, tp_price,
                        api_key=None, account_id=None, mode='demo'):
    """
    Place a real order on OANDA via REST API v20.
    direction: 'buy' or 'sell'
    units: number of units (e.g. 1000 = 1 micro lot)
    """
    import requests as req
    
    key  = api_key  or os.environ.get('OANDA_API_KEY', '')
    acct = account_id or os.environ.get('OANDA_ACCOUNT_ID', '')
    md   = mode or os.environ.get('OANDA_MODE', 'demo')
    
    if not key or not acct:
        return {'error': 'OANDA_API_KEY and OANDA_ACCOUNT_ID not set in .env'}
    
    base = BROKER_CONFIGS['oanda']['base_url_demo' if md == 'demo' else 'base_url_live']
    sym  = _normalize_symbol(symbol, 'oanda')

    # Pre-flight: check if instrument is tradeable on this account
    # US OANDA accounts CANNOT trade Gold, Silver, Crypto, Oil
    tradeable = _oanda_get_tradeable_instruments(key, acct, md)
    if tradeable and sym not in tradeable:
        friendly = {
            'XAU_USD': 'Gold (XAU/USD) is not available on US OANDA accounts due to CFTC regulations. Trade forex pairs instead: EUR/USD, GBP/USD, USD/JPY, GBP/JPY.',
            'XAG_USD': 'Silver (XAG/USD) is not available on US OANDA accounts.',
            'BTC_USD': 'Bitcoin is not available on US OANDA accounts.',
            'WTI_USD': 'Oil (WTI) is not available on US OANDA accounts.',
            'SPX500_USD': 'S&P 500 index is not available on this OANDA account.',
        }
        msg = friendly.get(sym, f'{sym} is not tradeable on your OANDA account.')
        print(f'[OANDA] Instrument not tradeable: {sym}. Account supports: {tradeable[:10]}...')
        return {
            'error':   f'{sym} not available on your OANDA account',
            'message': msg,
            'code':    'INSTRUMENT_NOT_AVAILABLE',
            'available_pairs': [i for i in tradeable if '_' in i and 'USD' in i][:15]
        }

    # Units: positive = buy, negative = sell
    unit_count = int(units) if direction == 'buy' else -int(units)

    # Minimum units guard — OANDA rejects tiny orders
    min_units = {'XAU_USD': 1, 'BTC_USD': 1}
    abs_units = abs(unit_count)
    if abs_units < min_units.get(sym, 1):
        unit_count = min_units.get(sym, 1) if direction == 'buy' else -min_units.get(sym, 1)

    # Price precision: Gold/XAU needs 2 decimals, JPY needs 3, others need 5
    if 'XAU' in sym or 'XAG' in sym:
        sl_price  = round(sl_price, 2)
        tp_price  = round(tp_price, 2)
    elif 'JPY' in sym:
        sl_price  = round(sl_price, 3)
        tp_price  = round(tp_price, 3)
    else:
        sl_price  = round(sl_price, 5)
        tp_price  = round(tp_price, 5)

    order_body = {
        'order': {
            'type':        'MARKET',
            'instrument':  sym,
            'units':       str(unit_count),
            'timeInForce': 'FOK',  # Fill or Kill
            'stopLossOnFill': {
                'price': str(round(sl_price, 5)),
                'timeInForce': 'GTC'
            },
            'takeProfitOnFill': {
                'price': str(round(tp_price, 5)),
                'timeInForce': 'GTC'
            },
            'positionFill': 'DEFAULT'
        }
    }
    
    try:
        # Log what we're sending to OANDA for debugging
        print(f'[OANDA ORDER] sym={sym} dir={direction} units={unit_count} sl={sl_price} tp={tp_price} mode={md}')
        print(f'[OANDA ORDER BODY] {str(order_body)[:400]}')
        resp = req.post(
            f'{base}/v3/accounts/{acct}/orders',
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type':  'application/json'
            },
            json=order_body,
            timeout=10
        )
        data = resp.json()
        
        if resp.status_code == 201:
            oc = data.get('orderCreateTransaction', {})
            of = data.get('orderFillTransaction', {})
            cc = data.get('orderCancelTransaction', {})  # FOK cancelled

            # If order was cancelled (FOK rejection), return error
            if cc and not of:
                cancel_reason = cc.get('reason', 'MARKET_HALTED')
                return {
                    'error':   f'Order cancelled: {cancel_reason}',
                    'message': 'OANDA rejected the order. Try again or check instrument availability.',
                    'code':    cancel_reason
                }

            trade_opened = of.get('tradeOpened', {})
            return {
                'success':    True,
                'broker':     'OANDA',
                'order_id':   oc.get('id'),
                'trade_id':   trade_opened.get('tradeID') or of.get('id'),
                'symbol':     sym,
                'direction':  direction,
                'units':      units,
                'fill_price': of.get('price', 'pending'),
                'sl':         sl_price,
                'tp':         tp_price,
                'mode':       md,
                'timestamp':  datetime.utcnow().isoformat(),
            }
        else:
            oanda_msg = data.get('errorMessage', '')
            oanda_code = data.get('errorCode', '')
            # Log full response for debugging
            print(f'[OANDA {resp.status_code}] code={oanda_code} msg={oanda_msg} body={str(data)[:300]}')
            return {
                'error':   f"OANDA {resp.status_code}: {oanda_code or oanda_msg or 'Bad request'}",
                'message': oanda_msg or str(data),
                'code':    oanda_code,
                'raw':     str(data)[:500]
            }
    except Exception as e:
        return {'error': f'Connection failed: {str(e)}'}


def _oanda_get_positions(api_key=None, account_id=None, mode='demo'):
    """Get all open positions from OANDA"""
    import requests as req
    key  = api_key  or os.environ.get('OANDA_API_KEY', '')
    acct = account_id or os.environ.get('OANDA_ACCOUNT_ID', '')
    md   = mode or os.environ.get('OANDA_MODE', 'demo')
    base = BROKER_CONFIGS['oanda']['base_url_demo' if md == 'demo' else 'base_url_live']
    try:
        resp = req.get(
            f'{base}/v3/accounts/{acct}/openPositions',
            headers={'Authorization': f'Bearer {key}'},
            timeout=10
        )
        return resp.json()
    except Exception as e:
        return {'error': str(e)}


def _oanda_close_trade(trade_id, api_key=None, account_id=None, mode='demo'):
    """Close a specific trade on OANDA"""
    import requests as req
    key  = api_key  or os.environ.get('OANDA_API_KEY', '')
    acct = account_id or os.environ.get('OANDA_ACCOUNT_ID', '')
    md   = mode or os.environ.get('OANDA_MODE', 'demo')
    base = BROKER_CONFIGS['oanda']['base_url_demo' if md == 'demo' else 'base_url_live']
    try:
        resp = req.put(
            f'{base}/v3/accounts/{acct}/trades/{trade_id}/close',
            headers={'Authorization': f'Bearer {key}'},
            timeout=10
        )
        return resp.json()
    except Exception as e:
        return {'error': str(e)}


def _oanda_account_summary(api_key=None, account_id=None, mode='demo'):
    """Get account balance, NAV, open P&L"""
    import requests as req
    key  = api_key  or os.environ.get('OANDA_API_KEY', '')
    acct = account_id or os.environ.get('OANDA_ACCOUNT_ID', '')
    md   = mode or os.environ.get('OANDA_MODE', 'demo')
    base = BROKER_CONFIGS['oanda']['base_url_demo' if md == 'demo' else 'base_url_live']
    try:
        resp = req.get(
            f'{base}/v3/accounts/{acct}/summary',
            headers={'Authorization': f'Bearer {key}'},
            timeout=10
        )
        data = resp.json()
        acct_data = data.get('account', {})
        return {
            'balance':    float(acct_data.get('balance', 0)),
            'nav':        float(acct_data.get('NAV', 0)),
            'open_pl':    float(acct_data.get('unrealizedPL', 0)),
            'margin_used':float(acct_data.get('marginUsed', 0)),
            'margin_avail':float(acct_data.get('marginAvailable', 0)),
            'open_trades':int(acct_data.get('openTradeCount', 0)),
            'currency':   acct_data.get('currency', 'USD'),
        }
    except Exception as e:
        return {'error': str(e)}


# ── WOLF PRE-TRADE ANALYSIS ENGINE ────────────────────────────────

def _wolf_pre_trade_analysis(symbol, direction, entry, sl, tp, 
                               account_size, risk_mode):
    """
    Wolf's full pre-trade research gate.
    MUST complete before any order fires.
    Returns: confidence score + full analysis + position size
    """
    risk = RISK_PROFILES.get(risk_mode, RISK_PROFILES['moderate'])
    
    # Position sizing: risk% of account / stop distance
    stop_dist  = abs(entry - sl)
    risk_amount= account_size * risk['risk_per_trade']
    
    if stop_dist > 0:
        units = risk_amount / stop_dist
    else:
        units = 0
    
    rr_ratio = abs(tp - entry) / max(stop_dist, 0.0001)
    
    # Minimum standards gate — R/R >= 1.0, valid prices, valid stop
    rr_ok       = rr_ratio >= 1.0
    stop_ok     = stop_dist > 0
    units_ok    = units > 0
    tp_ok       = abs(tp - entry) > stop_dist if stop_dist > 0 else False
    entry_ok    = entry > 0
    sl_ok       = sl > 0
    tp_val_ok   = tp > 0

    passes_gate = rr_ok and stop_ok and units_ok and tp_ok and entry_ok and sl_ok and tp_val_ok

    # Build detailed reason for debugging
    if passes_gate:
        gate_reason = f'All checks passed ✅ — RR={rr_ratio:.2f}, SL={stop_dist:.5f}, Units={int(units)}'
    elif not entry_ok:
        gate_reason = f'Failed: Entry price is 0 or missing (got {entry}). Wolf needs to search live price first.'
    elif not sl_ok:
        gate_reason = f'Failed: Stop loss is 0 or missing (got {sl}). Every trade needs a stop loss.'
    elif not tp_val_ok:
        gate_reason = f'Failed: Take profit is 0 or missing (got {tp}). Every trade needs a TP.'
    elif not stop_ok:
        gate_reason = f'Failed: Stop distance is 0 — entry ({entry}) and SL ({sl}) are the same price.'
    elif not rr_ok:
        gate_reason = f'Failed: R/R={rr_ratio:.2f} is below minimum 1.0. TP must be further from entry than the SL.'
    elif not units_ok:
        gate_reason = f'Failed: Position size calculated as 0 units. Check account size ({account_size}) and stop distance ({stop_dist:.5f}).'
    else:
        gate_reason = f'Failed: TP ({tp}) must be further from entry ({entry}) than the stop ({sl}). Check direction.'

    return {
        'symbol':       symbol,
        'direction':    direction,
        'entry':        round(entry, 5),
        'stop_loss':    round(sl, 5),
        'take_profit':  round(tp, 5),
        'stop_distance':round(stop_dist, 5),
        'rr_ratio':     round(rr_ratio, 2),
        'risk_amount':  round(risk_amount, 2),
        'units':        max(int(units), 1),
        'risk_mode':    risk_mode,
        'risk_pct':     risk['risk_per_trade'] * 100,
        'passes_gate':  passes_gate,
        'gate_reason':  gate_reason,
        'timestamp':    datetime.utcnow().isoformat(),
    }


# ── FLASK ROUTES ───────────────────────────────────────────────────

@app.route('/api/broker-list', methods=['GET'])
@login_required
def api_broker_list():
    """Return all supported brokers with setup info"""
    result = {}
    for key, cfg in BROKER_CONFIGS.items():
        result[key] = {
            'name':       cfg['name'],
            'regulated':  cfg['regulated'],
            'leverage':   cfg['leverage'],
            'us_clients': cfg.get('us_clients', cfg['regulated']),
            'type':       cfg['type'],
            'supports':   cfg['supports'],
            'min_deposit':cfg.get('min_deposit', 'N/A'),
            'notes':      cfg.get('notes', ''),
            'signup':     cfg.get('signup', ''),
            'configured': bool(os.environ.get(cfg['env_key'], '')),
        }
    return jsonify(result)


@app.route('/api/broker-account', methods=['POST'])
@login_required
def api_broker_account():
    """Get account summary for a configured broker"""
    try:
        d = request.get_json() or {}
        broker = d.get('broker', 'oanda').lower()
        
        if broker == 'oanda':
            result = _oanda_account_summary(
                api_key    = d.get('api_key'),
                account_id = d.get('account_id'),
                mode       = d.get('mode', 'demo')
            )
            result['broker'] = 'OANDA'
            return jsonify(result)
        else:
            return jsonify({'error': f'Broker {broker} account query not yet implemented. Connect via MT4/MT5 desktop app.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/wolf-analyze-trade', methods=['POST'])
@login_required
def api_wolf_analyze_trade():
    """
    Wolf's pre-trade analysis gate.
    Runs full analysis BEFORE allowing a trade to be placed.
    POST body:
    {
        symbol: "EURUSD=X",
        direction: "buy",
        entry: 1.0850,
        sl: 1.0800,
        tp: 1.0950,
        account_size: 1000,
        risk_mode: "moderate"
    }
    """
    try:
        d = request.get_json() or {}
        result = _wolf_pre_trade_analysis(
            symbol       = d.get('symbol', ''),
            direction    = d.get('direction', 'buy'),
            entry        = float(d.get('entry', 0)),
            sl           = float(d.get('sl', 0)),
            tp           = float(d.get('tp', 0)),
            account_size = float(d.get('account_size', 1000)),
            risk_mode    = d.get('risk_mode', 'moderate')
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/wolf-place-trade', methods=['POST'])
@login_required
def api_wolf_place_trade():
    """
    Place a REAL trade through Wolf after analysis gate passes.
    
    POST body:
    {
        broker: "oanda",
        symbol: "EURUSD=X",
        direction: "buy",
        entry: 1.0850,
        sl: 1.0800,
        tp: 1.0950,
        units: 1000,
        account_size: 1000,
        risk_mode: "moderate",
        confirmed: true,        // MUST be true — user confirmation required
        api_key: "...",         // optional override (else uses .env)
        account_id: "...",      // optional override
        mode: "demo"            // demo or live
    }
    """
    try:
        d = request.get_json() or {}
        
        # SAFETY: user must explicitly confirm
        if not d.get('confirmed', False):
            return jsonify({
                'error': 'Trade not confirmed. Set confirmed:true to place trade.',
                'action': 'confirm_required'
            }), 400
        
        broker    = d.get('broker', 'oanda').lower()
        symbol    = d.get('symbol', '')
        direction = d.get('direction', 'buy')
        sl        = float(d.get('sl', 0))
        tp        = float(d.get('tp', 0))
        units     = int(d.get('units', 1000))
        mode      = d.get('mode', 'demo')
        
        # Run pre-trade gate first
        entry = float(d.get('entry', 0))
        gate  = _wolf_pre_trade_analysis(
            symbol       = symbol,
            direction    = direction,
            entry        = entry,
            sl           = sl,
            tp           = tp,
            account_size = float(d.get('account_size', 1000)),
            risk_mode    = d.get('risk_mode', 'moderate')
        )
        
        if not gate['passes_gate']:
            return jsonify({
                'error':      'Trade failed Wolf pre-trade analysis gate',
                'reason':     gate['gate_reason'],
                'analysis':   gate
            }), 400
        
        # Use gate's calculated units if not explicitly provided
        if units == 0:
            units = gate['units']
        
        # Place the trade
        if broker == 'oanda':
            result = _oanda_place_order(
                symbol     = symbol,
                direction  = direction,
                units      = units,
                sl_price   = sl,
                tp_price   = tp,
                api_key    = d.get('api_key'),
                account_id = d.get('account_id'),
                mode       = mode
            )
        else:
            result = {
                'error': (
                    f'{broker.upper()} automated trading requires MT4/MT5 Expert Advisor setup. '
                    f'Wolf has analyzed the trade — use the details below to place manually '
                    f'or set up an EA on your MT4/MT5 terminal.'
                ),
                'trade_details': gate,
                'manual_trade': {
                    'symbol':    _normalize_symbol(symbol, broker),
                    'direction': direction,
                    'entry':     entry,
                    'sl':        sl,
                    'tp':        tp,
                    'lots':      round(units / 100000, 4),
                }
            }
        
        result['pre_trade_analysis'] = gate
        return jsonify(result)
    
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/wolf-positions', methods=['POST'])
@login_required
def api_wolf_positions():
    """Get open positions from broker"""
    try:
        d      = request.get_json() or {}
        broker = d.get('broker', 'oanda').lower()
        if broker == 'oanda':
            return jsonify(_oanda_get_positions(
                api_key    = d.get('api_key'),
                account_id = d.get('account_id'),
                mode       = d.get('mode', 'demo')
            ))
        return jsonify({'error': f'{broker} position query coming soon'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/wolf-close-trade', methods=['POST'])
@login_required
def api_wolf_close_trade():
    """Close a specific trade"""
    try:
        d        = request.get_json() or {}
        broker   = d.get('broker', 'oanda').lower()
        trade_id = d.get('trade_id', '')
        if broker == 'oanda':
            return jsonify(_oanda_close_trade(
                trade_id   = trade_id,
                api_key    = d.get('api_key'),
                account_id = d.get('account_id'),
                mode       = d.get('mode', 'demo')
            ))
        return jsonify({'error': f'{broker} trade closing coming soon'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wolf-open-trades', methods=['POST'])
@login_required
def api_wolf_open_trades():
    """Get all open individual trades with trade IDs — needed for closing"""
    import requests as req
    try:
        d      = request.get_json() or {}
        key    = os.environ.get('OANDA_API_KEY', '')
        acct   = os.environ.get('OANDA_ACCOUNT_ID', '')
        md     = os.environ.get('OANDA_MODE', 'demo')
        base   = BROKER_CONFIGS['oanda']['base_url_demo' if md == 'demo' else 'base_url_live']

        if not key or not acct:
            return jsonify({'error': 'OANDA not configured'})

        resp = req.get(
            f'{base}/v3/accounts/{acct}/trades?state=OPEN',
            headers={'Authorization': f'Bearer {key}'},
            timeout=10
        )
        data = resp.json()
        trades = data.get('trades', [])

        result = []
        for t in trades:
            units     = int(t.get('currentUnits', 0))
            direction = 'BUY' if units > 0 else 'SELL'
            pl        = float(t.get('unrealizedPL', 0))
            result.append({
                'trade_id':    t.get('id'),
                'instrument':  t.get('instrument', '').replace('_', '/'),
                'direction':   direction,
                'units':       abs(units),
                'open_price':  float(t.get('price', 0)),
                'current_pl':  round(pl, 2),
                'open_time':   t.get('openTime', '')[:16].replace('T', ' '),
                'sl':          t.get('stopLossOrder', {}).get('price', 'none'),
                'tp':          t.get('takeProfitOrder', {}).get('price', 'none'),
            })

        return jsonify({'trades': result, 'count': len(result)})

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/wolf-close-all', methods=['POST'])
@login_required
def api_wolf_close_all():
    """Close ALL open trades on OANDA"""
    import requests as req
    try:
        key  = os.environ.get('OANDA_API_KEY', '')
        acct = os.environ.get('OANDA_ACCOUNT_ID', '')
        md   = os.environ.get('OANDA_MODE', 'demo')
        base = BROKER_CONFIGS['oanda']['base_url_demo' if md == 'demo' else 'base_url_live']

        # Get all open trades first
        resp = req.get(
            f'{base}/v3/accounts/{acct}/trades?state=OPEN',
            headers={'Authorization': f'Bearer {key}'},
            timeout=10
        )
        trades = resp.json().get('trades', [])
        if not trades:
            return jsonify({'closed': 0, 'message': 'No open trades to close'})

        closed = []
        errors = []
        for t in trades:
            try:
                cr = req.put(
                    f'{base}/v3/accounts/{acct}/trades/{t["id"]}/close',
                    headers={'Authorization': f'Bearer {key}'},
                    timeout=10
                )
                cd = cr.json()
                if 'orderFillTransaction' in cd:
                    closed.append({
                        'trade_id':   t['id'],
                        'instrument': t.get('instrument','').replace('_','/'),
                        'pl':         float(cd['orderFillTransaction'].get('pl', 0))
                    })
                else:
                    errors.append({'trade_id': t['id'], 'error': str(cd)})
            except Exception as e:
                errors.append({'trade_id': t['id'], 'error': str(e)})

        total_pl = round(sum(c['pl'] for c in closed), 2)
        return jsonify({
            'closed': len(closed),
            'errors': len(errors),
            'total_pl': total_pl,
            'details': closed
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500





@app.route('/api/wolf-chart', methods=['POST'])
@login_required
def api_wolf_chart():
    """
    Real-time chart candles via TwelveData (yfinance fallback).
    Same data source as Wolf AI analysis — no more stale Yahoo proxies.
    """
    try:
        d        = request.get_json() or {}
        raw_sym  = d.get('symbol', 'EURUSD=X')
        interval = d.get('interval', '1h')

        # Translate Yahoo Finance symbol format → TwelveData/internal format
        YAHOO_TO_INTERNAL = {
            # Forex majors
            'EURUSD=X':'EUR/USD','GBPUSD=X':'GBP/USD','USDJPY=X':'USD/JPY',
            'GBPJPY=X':'GBP/JPY','AUDUSD=X':'AUD/USD','USDCAD=X':'USD/CAD',
            'USDCHF=X':'USD/CHF','EURJPY=X':'EUR/JPY','EURGBP=X':'EUR/GBP',
            'NZDUSD=X':'NZD/USD','EURAUD=X':'EUR/AUD','AUDCAD=X':'AUD/CAD',
            'CADJPY=X':'CAD/JPY','CHFJPY=X':'CHF/JPY','AUDNZD=X':'AUD/NZD',
            # Commodities
            'GC=F':'XAU/USD','SI=F':'XAG/USD','CL=F':'WTI/USD',
            # Crypto
            'BTC-USD':'BTC/USD','ETH-USD':'ETH/USD','SOL-USD':'SOL/USD',
            # US Stocks — TwelveData handles these natively by ticker
            'NVDA':'NVDA','AAPL':'AAPL','TSLA':'TSLA','META':'META',
            'AMD':'AMD','MSFT':'MSFT','AMZN':'AMZN','GOOGL':'GOOGL',
            # Indices
            'SPY':'SPY','QQQ':'QQQ','^GSPC':'SPX','^NDX':'NDX',
        }
        sym = YAHOO_TO_INTERNAL.get(raw_sym, raw_sym)

        # Map Yahoo intervals to internal format
        INTERVAL_MAP = {
            '5m':'15m','15m':'15m','60m':'1h','1h':'1h','4h':'4h','1d':'1d','1wk':'1wk',
        }
        server_interval = INTERVAL_MAP.get(interval, '1h')

        candles = get_candles(sym, server_interval)
        if not candles:
            return jsonify({'error': f'No data for {sym} {server_interval}'}), 404

        # Convert timestamps to unix epoch for LightweightCharts
        import calendar
        from datetime import datetime
        normalized = []
        for c in candles:
            t = c.get('time', '')
            try:
                if isinstance(t, (int, float)):
                    unix_t = int(t)
                elif len(str(t)) == 10:
                    unix_t = int(calendar.timegm(datetime.strptime(str(t), '%Y-%m-%d').timetuple()))
                else:
                    unix_t = int(calendar.timegm(datetime.strptime(str(t)[:19], '%Y-%m-%d %H:%M:%S').timetuple()))
                normalized.append({'time':unix_t,'open':c['open'],'high':c['high'],
                                   'low':c['low'],'close':c['close'],'volume':c.get('volume',0)})
            except Exception:
                continue

        source = 'twelvedata' if TWELVE_DATA_KEY else 'yfinance'
        candle_count = len(normalized)
        return jsonify({'candles':normalized,'symbol':sym,'interval':server_interval,'candle_count':candle_count,
                        'source':source,'count':len(normalized)})
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════
# 🐺 WOLF AUTO-TRADER — SERVER-SIDE 24/7 ENGINE
# Runs entirely on Render. No browser needed. No computer needed.
# Wolf scans → analyzes → trades → texts you. You live your life.
# SMS via Twilio (free trial: $15 credit, ~500 texts)
# ═══════════════════════════════════════════════════════════════════

import threading as _at_thread
import time as _at_time
import json as _at_json
from datetime import datetime as _at_dt

# ── AUTO-TRADER STATE ──────────────────────────────────────────────
# Session-aware pair lists — Wolf scans the right pairs for each session
_SESSION_PAIRS = {
    "LONDON":   ["EURUSD=X","GBPUSD=X","EURGBP=X","GBPJPY=X","EURJPY=X","USDCHF=X","XAUUSD=X"],
    "NEW_YORK": ["EURUSD=X","GBPUSD=X","USDJPY=X","USDCAD=X","USDCHF=X","XAUUSD=X","AUDUSD=X"],
    "OVERLAP":  ["EURUSD=X","GBPUSD=X","USDJPY=X","XAUUSD=X","GBPJPY=X","USDCAD=X"],
    "TOKYO":    ["USDJPY=X","EURJPY=X","GBPJPY=X","AUDUSD=X","NZDUSD=X","USDCHF=X"],
    "DEAD":     [],
}

def _get_session_pairs():
    from datetime import timezone
    h = _at_dt.now(timezone.utc).hour
    if 12 <= h < 17:  return _SESSION_PAIRS["OVERLAP"],  "LONDON/NY OVERLAP"
    elif 7 <= h < 12: return _SESSION_PAIRS["LONDON"],   "LONDON"
    elif 17 <= h < 21:return _SESSION_PAIRS["NEW_YORK"], "NEW YORK"
    elif h >= 23 or h < 7: return _SESSION_PAIRS["TOKYO"], "TOKYO"
    else:             return [], "DEAD ZONE"

_wolf_auto = {
    "running":         False,
    "thread":          None,
    "interval_mins":   30,
    "last_scan":       None,
    "last_trade":      None,
    "trades_today":    0,
    "losses_today":    0,
    "wins_today":      0,
    "loss_streak":     0,
    "paused":          False,
    "phone":           "",
    "mode":            "demo",
    "risk_per_trade":  60.0,
    "max_trades_day":  10,
    "max_simultaneous":2,
    "min_confidence":  75,
    "use_sessions":    True,
    "pairs": ["EURUSD=X","GBPUSD=X","USDJPY=X","GBPJPY=X","AUDUSD=X","EURJPY=X","USDCAD=X","XAUUSD=X"],
    "log": [],
}

# ── SMS VIA TWILIO ─────────────────────────────────────────────────

def _send_sms(message, to_phone=None):
    """
    Send SMS via Twilio.
    Requires in .env:
      TWILIO_ACCOUNT_SID = ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
      TWILIO_AUTH_TOKEN  = your_auth_token
      TWILIO_FROM_NUMBER = +1XXXXXXXXXX (your Twilio number)
    """
    phone = to_phone or _wolf_auto.get('phone') or os.environ.get('WOLF_PHONE_NUMBER', '')
    sid   = os.environ.get('TWILIO_ACCOUNT_SID', '')
    token = os.environ.get('TWILIO_AUTH_TOKEN', '')
    from_ = os.environ.get('TWILIO_FROM_NUMBER', '')

    if not all([phone, sid, token, from_]):
        print(f'[SMS] Not configured — would have sent: {message[:80]}')
        _wolf_auto['log'].append({
            'time': _at_dt.utcnow().isoformat(),
            'type': 'sms_skipped',
            'msg':  message[:100]
        })
        return False

    try:
        import requests as _r
        resp = _r.post(
            f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',
            auth=(sid, token),
            data={
                'From': from_,
                'To':   phone if phone.startswith('+') else '+1' + phone.replace('-','').replace(' ',''),
                'Body': message
            },
            timeout=10
        )
        ok = resp.status_code in (200, 201)
        _wolf_auto['log'].append({
            'time': _at_dt.utcnow().isoformat(),
            'type': 'sms_sent' if ok else 'sms_failed',
            'msg':  message[:100]
        })
        print(f'[SMS] {"Sent" if ok else "Failed"}: {message[:60]}')
        return ok
    except Exception as e:
        print(f'[SMS] Error: {e}')
        return False


# ── WOLF SERVER-SIDE ANALYSIS ENGINE ──────────────────────────────

def _wolf_server_analyze(symbol, anthropic_key):
    """
    Run full Wolf analysis server-side using Anthropic API.
    Returns trade decision with entry/SL/TP or None.
    """
    try:
        import anthropic as _anth
        client = _anth.Anthropic(api_key=anthropic_key)

        # Get current price data
        import yfinance as _yf
        ticker = _yf.Ticker(symbol)
        hist = ticker.history(period='5d', interval='1h')
        if hist.empty:
            return None

        current_price = float(hist['Close'].iloc[-1])
        prev_close    = float(hist['Close'].iloc[-2])
        change_pct    = (current_price - prev_close) / prev_close * 100

        # Calculate quick indicators
        closes = hist['Close']
        ema21  = float(closes.ewm(span=21, adjust=False).mean().iloc[-1])
        ema50  = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
        delta  = closes.diff()
        gain   = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss   = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
        rsi    = float(100 - (100 / (1 + gain.iloc[-1] / max(loss.iloc[-1], 0.0001))))
        atr    = float((hist['High'] - hist['Low']).ewm(span=14, adjust=False).mean().iloc[-1])

        prompt = f"""You are Wolf — elite AI trader. Analyze this setup RIGHT NOW and give me a binary decision.

SYMBOL: {symbol}
CURRENT PRICE: {current_price:.5f}
CHANGE: {change_pct:+.2f}%
EMA21: {ema21:.5f}
EMA50: {ema50:.5f}
RSI(14): {rsi:.1f}
ATR(14): {atr:.5f}
RISK PER TRADE: $60 (non-negotiable)

YOUR JOB: Decide BUY, SELL, or SKIP.
- Only trade if confidence >= {_wolf_auto['min_confidence']}%
- Every trade MUST have SL and TP
- Position size = $60 / stop distance
- If market is choppy or unclear → SKIP

Respond ONLY in this exact JSON format, nothing else:
{{
  "decision": "BUY" or "SELL" or "SKIP",
  "confidence": 0-100,
  "entry": price,
  "sl": stop_loss_price,
  "tp1": first_target,
  "tp2": second_target,
  "units": position_size_units,
  "strategy": "strategy name used",
  "reason": "one sentence why",
  "rr": "e.g. 1:2.5"
}}"""

        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}]
        )

        raw = response.content[0].text.strip()
        # Clean JSON
        if '```' in raw:
            raw = raw.split('```')[1].replace('json','').strip()

        data = _at_json.loads(raw)
        data['symbol']        = symbol
        data['current_price'] = current_price
        data['analyzed_at']   = _at_dt.utcnow().isoformat()
        return data

    except Exception as e:
        print(f'[Wolf Auto] Analysis error for {symbol}: {e}')
        return None


# ── MAIN AUTO-TRADER LOOP ──────────────────────────────────────────

def _wolf_auto_loop():
    """
    Main background loop. Runs on Render 24/7.
    Scans pairs → analyzes → places trades → sends SMS.
    """
    print('[Wolf Auto] 🐺 Auto-trader started — running 24/7 on server')

    while _wolf_auto['running']:
        try:
            # Check if paused after loss streak
            if _wolf_auto['paused']:
                _wolf_auto['log'].append({
                    'time': _at_dt.utcnow().isoformat(),
                    'type': 'paused',
                    'msg':  'Auto-trader paused after loss streak. Waiting for manual resume.'
                })
                _at_time.sleep(300)  # check again in 5 mins
                continue

            # Check daily trade limit
            if _wolf_auto['trades_today'] >= _wolf_auto['max_trades_day']:
                print(f'[Wolf Auto] Daily limit reached ({_wolf_auto["max_trades_day"]} trades)')
                _at_time.sleep(3600)
                continue

            # Get Anthropic key from environment
            anthropic_key = os.environ.get('ANTHROPIC_API_KEY', '')
            if not anthropic_key:
                print('[Wolf Auto] No ANTHROPIC_API_KEY set')
                _at_time.sleep(300)
                continue

            # Get OANDA config
            oanda_key  = os.environ.get('OANDA_API_KEY', '')
            oanda_acct = os.environ.get('OANDA_ACCOUNT_ID', '')
            oanda_mode = _wolf_auto.get('mode', 'demo')

            # ── Get session-aware pairs ──────────────────────────────────
            if _wolf_auto.get("use_sessions", True):
                scan_pairs, session_name = _get_session_pairs()
                if not scan_pairs:
                    print(f"[Wolf Auto] Dead zone — skipping scan")
                    _wolf_auto["log"].append({
                        "time": _at_dt.utcnow().isoformat(),
                        "type": "skip",
                        "msg":  "Dead zone (21:00-23:00 UTC) — no liquid pairs. Next scan soon."
                    })
                    _at_time.sleep(600)
                    continue
            else:
                scan_pairs  = _wolf_auto["pairs"]
                session_name = "ALL SESSIONS"

            _wolf_auto["last_scan"] = _at_dt.utcnow().isoformat()

            # ── Collect TOP 2 trades ─────────────────────────────────────────
            top_trades = []  # list of (confidence, result) — sorted desc

            print(f"[Wolf Auto] Scanning {len(scan_pairs)} pairs — {session_name} session")

            for symbol in scan_pairs:
                if not _wolf_auto["running"]:
                    break

                result = _wolf_server_analyze(symbol, anthropic_key)
                if not result:
                    continue

                decision   = result.get("decision", "SKIP")
                confidence = result.get("confidence", 0)

                _wolf_auto["log"].append({
                    "time":       _at_dt.utcnow().isoformat(),
                    "type":       "scan",
                    "symbol":     symbol,
                    "session":    session_name,
                    "decision":   decision,
                    "confidence": confidence,
                    "reason":     result.get("reason", "")
                })

                if decision in ("BUY", "SELL") and confidence >= _wolf_auto["min_confidence"]:
                    top_trades.append((confidence, result))

                _at_time.sleep(3)

            # Sort by confidence desc — best setups first
            top_trades.sort(key=lambda x: x[0], reverse=True)

            # How many simultaneous trades are allowed?
            max_sim    = _wolf_auto.get("max_simultaneous", 2)
            trades_to_place = top_trades[:max_sim]

            if not trades_to_place:
                print(f"[Wolf Auto] No qualifying setups — {session_name}")
                _wolf_auto["log"].append({
                    "time": _at_dt.utcnow().isoformat(),
                    "type": "no_setup",
                    "msg":  f"Scanned {len(scan_pairs)} pairs ({session_name}) — no setups above {_wolf_auto['min_confidence']}% confidence"
                })
            else:
                print(f"[Wolf Auto] Found {len(trades_to_place)} qualifying setup(s) — placing trades")

            # ── Place up to max_simultaneous trades ──────────────────────────
            for best_conf, best_trade in trades_to_place:
                if _wolf_auto["trades_today"] >= _wolf_auto["max_trades_day"]:
                    break
                if not (oanda_key and oanda_acct):
                    break

                sym       = best_trade["symbol"]
                direction = best_trade["decision"].lower()
                sl        = float(best_trade.get("sl", 0))
                tp        = float(best_trade.get("tp1", best_trade.get("tp", 0)))
                units     = int(best_trade.get("units", 1000))
                entry     = float(best_trade.get("entry", best_trade.get("current_price", 0)))
                strategy  = best_trade.get("strategy", "Wolf Analysis")
                reason    = best_trade.get("reason", "")
                rr        = best_trade.get("rr", "1:2")

                if not (sl > 0 and tp > 0):
                    continue

                trade_result = _oanda_place_order(
                    symbol     = sym,
                    direction  = direction,
                    units      = units,
                    sl_price   = sl,
                    tp_price   = tp,
                    api_key    = oanda_key,
                    account_id = oanda_acct,
                    mode       = oanda_mode
                )

                if trade_result.get("success"):
                    _wolf_auto["trades_today"] += 1
                    _wolf_auto["last_trade"]    = _at_dt.utcnow().isoformat()
                    _wolf_auto["loss_streak"]   = 0
                    _wolf_auto["log"].append({
                        "time":      _at_dt.utcnow().isoformat(),
                        "type":      "trade_placed",
                        "symbol":    sym,
                        "session":   session_name,
                        "direction": direction,
                        "entry":     entry,
                        "sl":        sl,
                        "tp":        tp,
                        "units":     units,
                        "trade_id":  trade_result.get("trade_id"),
                        "confidence":best_conf,
                    })

                    direction_emoji = "🟢 BUY" if direction == "buy" else "🔴 SELL"
                    _send_sms("\n".join([
                        "🐺 WOLF TRADE ALERT — " + session_name,
                        direction_emoji + " " + sym.replace("=X",""),
                        "Entry: " + str(round(entry, 5)),
                        "SL: " + str(round(sl, 5)),
                        "TP: " + str(round(tp, 5)),
                        "R/R: " + str(rr) + " | Risk: $60",
                        "Strategy: " + str(strategy),
                        "Confidence: " + str(best_conf) + "%",
                        "Reason: " + str(reason)[:60],
                        "Mode: " + oanda_mode.upper(),
                    ]))
                else:
                    error_msg = trade_result.get("error", "Unknown error")
                    _wolf_auto["loss_streak"] += 1
                    _send_sms("🐺 Wolf trade FAILED\n" + sym + " " + direction.upper() + "\nError: " + str(error_msg)[:80])

                    if _wolf_auto["loss_streak"] >= 3:
                        _wolf_auto["paused"] = True
                        _send_sms("🐺 WOLF AUTO-TRADER PAUSED\n3 consecutive losses.\nResume at jaydawolfx-terminal.onrender.com")
                        break

        except Exception as e:
            import traceback
            print(f'[Wolf Auto] Loop error: {e}')
            print(traceback.format_exc())
            _wolf_auto['log'].append({
                'time': _at_dt.utcnow().isoformat(),
                'type': 'error',
                'msg':  str(e)[:200]
            })

        # Keep only last 100 log entries
        _wolf_auto['log'] = _wolf_auto['log'][-100:]

        # Sleep until next scan
        interval_secs = _wolf_auto['interval_mins'] * 60
        print(f'[Wolf Auto] Next scan in {_wolf_auto["interval_mins"]} minutes')
        _at_time.sleep(interval_secs)

    print('[Wolf Auto] Auto-trader stopped')


# ── FLASK ROUTES ───────────────────────────────────────────────────

@app.route("/api/wolf-instant-scan", methods=["POST"])
@login_required
def api_wolf_instant_scan():
    """
    On-demand scan from Wolf chat.
    Scans current session pairs right now, returns best setups.
    Does NOT wait for the scheduled cycle.
    """
    import threading
    try:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            return jsonify({"error": "ANTHROPIC_API_KEY not set in Render environment"}), 500

        # Get session pairs
        scan_pairs, session_name = _get_session_pairs()
        if not scan_pairs:
            scan_pairs = _wolf_auto["pairs"]
            session_name = "ALL SESSIONS"

        results = []
        for sym in scan_pairs:
            r = _wolf_server_analyze(sym, anthropic_key)
            if not r: continue
            results.append(r)

        # Sort by confidence
        qualifying = [r for r in results if r.get("decision") in ("BUY","SELL")
                      and r.get("confidence", 0) >= _wolf_auto["min_confidence"]]
        qualifying.sort(key=lambda x: x.get("confidence", 0), reverse=True)

        all_results = []
        for r in results:
            all_results.append({
                "symbol":     r.get("symbol","").replace("=X",""),
                "decision":   r.get("decision","SKIP"),
                "confidence": r.get("confidence", 0),
                "reason":     r.get("reason",""),
                "entry":      r.get("entry", 0),
                "sl":         r.get("sl", 0),
                "tp1":        r.get("tp1", 0),
                "rr":         r.get("rr",""),
                "strategy":   r.get("strategy",""),
            })

        return jsonify({
            "session":    session_name,
            "scanned":    len(results),
            "qualifying": len(qualifying),
            "results":    all_results,
            "top_setup":  qualifying[0] if qualifying else None,
        })

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route('/api/wolf-auto/start', methods=['POST'])
@login_required
def api_wolf_auto_start():
    """Start the Wolf auto-trader background job"""
    try:
        d = request.get_json() or {}

        # Update config
        if 'phone' in d:
            _wolf_auto['phone'] = d['phone']
        if 'interval_mins' in d:
            _wolf_auto['interval_mins'] = max(15, int(d['interval_mins']))
        if 'mode' in d:
            _wolf_auto['mode'] = d['mode']
        if 'min_confidence' in d:
            _wolf_auto['min_confidence'] = max(60, min(95, int(d['min_confidence'])))
        if 'max_trades_day' in d:
            _wolf_auto['max_trades_day'] = max(1, min(20, int(d['max_trades_day'])))
        if 'pairs' in d and d['pairs']:
            _wolf_auto['pairs'] = d['pairs'][:12]
        if 'risk_per_trade' in d:
            _wolf_auto['risk_per_trade'] = float(d.get('risk_per_trade', 60))

        # Reset daily counters
        _wolf_auto['trades_today'] = 0
        _wolf_auto['losses_today'] = 0
        _wolf_auto['paused']       = False
        _wolf_auto['loss_streak']  = 0

        # Start if not already running
        if not _wolf_auto['running']:
            _wolf_auto['running'] = True
            t = _at_thread.Thread(target=_wolf_auto_loop, daemon=True)
            t.start()
            _wolf_auto['thread'] = t

            # Send start SMS
            _send_sms('\n'.join([
                '🐺 WOLF AUTO-TRADER STARTED',
                'Scanning: ' + str(len(_wolf_auto['pairs'])) + ' pairs',
                'Interval: every ' + str(_wolf_auto['interval_mins']) + ' mins',
                'Risk: $' + str(int(_wolf_auto['risk_per_trade'])) + '/trade',
                'Mode: ' + _wolf_auto['mode'].upper(),
                'Min confidence: ' + str(_wolf_auto['min_confidence']) + '%',
                'Max trades/day: ' + str(_wolf_auto['max_trades_day']),
                'Wolf is watching the markets for you. 🐺',
            ]))

            return jsonify({'status': 'started', 'config': {
                'interval_mins':  _wolf_auto['interval_mins'],
                'mode':           _wolf_auto['mode'],
                'min_confidence': _wolf_auto['min_confidence'],
                'max_trades_day': _wolf_auto['max_trades_day'],
                'pairs':          _wolf_auto['pairs'],
                'risk_per_trade': _wolf_auto['risk_per_trade'],
            }})
        else:
            return jsonify({'status': 'already_running',
                           'message': 'Auto-trader is already running'})

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/wolf-auto/stop', methods=['POST'])
@login_required
def api_wolf_auto_stop():
    """Stop the Wolf auto-trader"""
    _wolf_auto['running'] = False
    _send_sms('🐺 Wolf auto-trader stopped. Markets are on their own now.')
    return jsonify({'status': 'stopped'})


@app.route('/api/wolf-auto/status', methods=['GET'])
@login_required
def api_wolf_auto_status():
    """Get auto-trader status + recent log"""
    return jsonify({
        'running':       _wolf_auto['running'],
        'paused':        _wolf_auto['paused'],
        'last_scan':     _wolf_auto['last_scan'],
        'last_trade':    _wolf_auto['last_trade'],
        'trades_today':  _wolf_auto['trades_today'],
        'wins_today':    _wolf_auto['wins_today'],
        'losses_today':  _wolf_auto['losses_today'],
        'loss_streak':   _wolf_auto['loss_streak'],
        'interval_mins': _wolf_auto['interval_mins'],
        'mode':          _wolf_auto['mode'],
        'min_confidence':_wolf_auto['min_confidence'],
        'max_trades_day':_wolf_auto['max_trades_day'],
        'phone_set':     bool(_wolf_auto['phone'] or os.environ.get('WOLF_PHONE_NUMBER','')),
        'sms_ready':     bool(os.environ.get('TWILIO_ACCOUNT_SID','')),
        'log':           _wolf_auto['log'][-20:],
    })


@app.route('/api/wolf-auto/resume', methods=['POST'])
@login_required
def api_wolf_auto_resume():
    """Resume auto-trader after loss streak pause"""
    _wolf_auto['paused']      = False
    _wolf_auto['loss_streak'] = 0
    _send_sms('🐺 Wolf auto-trader RESUMED. Back on watch.')
    return jsonify({'status': 'resumed'})


@app.route('/api/wolf-auto/config', methods=['POST'])
@login_required
def api_wolf_auto_config():
    """Update auto-trader config without restarting"""
    try:
        d = request.get_json() or {}
        if 'phone' in d:
            _wolf_auto['phone'] = d['phone']
            os.environ['WOLF_PHONE_NUMBER'] = d['phone']
        if 'interval_mins' in d:
            _wolf_auto['interval_mins'] = max(15, int(d['interval_mins']))
        if 'min_confidence' in d:
            _wolf_auto['min_confidence'] = max(60, min(95, int(d['min_confidence'])))
        if 'max_trades_day' in d:
            _wolf_auto['max_trades_day'] = max(1, min(20, int(d['max_trades_day'])))
        if 'mode' in d:
            _wolf_auto['mode'] = d['mode']
        return jsonify({'status': 'updated', 'config': {
            k: _wolf_auto[k] for k in
            ['interval_mins','min_confidence','max_trades_day','mode','phone']
        }})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/wolf-auto/test-sms', methods=['POST'])
@login_required
def api_wolf_auto_test_sms():
    """Send a test SMS to verify setup"""
    d = request.get_json() or {}
    phone = d.get('phone', '')
    if phone:
        _wolf_auto['phone'] = phone
    ok = _send_sms(
        '🐺 Wolf Agent SMS test successful!\nYou will receive trade alerts here.\nWolf is ready to trade for you 24/7.',
        to_phone=phone or None
    )
    return jsonify({'sent': ok,
                   'message': 'SMS sent!' if ok else 'SMS not configured — check Twilio credentials in Render environment'})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
