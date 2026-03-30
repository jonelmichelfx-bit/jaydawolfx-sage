import os, json, time, uuid, threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import requests as http_requests
import stripe

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sage-secret-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///sage.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login_page'

TWELVE_DATA_KEY  = os.environ.get('TWELVE_DATA_API_KEY', '')
STRIPE_SK        = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PK        = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK   = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
PRICE_SAGE       = 'price_1TCWsy2jJ40b0Vm86sFsDrl9'
PRICE_UNLEASHED  = 'price_1TCWuN2jJ40b0Vm8xTaRsm03'

stripe.api_key = STRIPE_SK

# ── User Model ─────────────────────────────────────────────
class User(UserMixin, db.Model):
    id                = db.Column(db.Integer, primary_key=True)
    username          = db.Column(db.String(80), unique=True, nullable=False)
    email             = db.Column(db.String(120), unique=True, nullable=False)
    password_hash     = db.Column(db.String(256), nullable=False)
    plan              = db.Column(db.String(20), default='student')
    stripe_customer_id= db.Column(db.String(100), nullable=True)
    stripe_sub_id     = db.Column(db.String(100), nullable=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# ── AUTH ROUTES ────────────────────────────────────────────
@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('sage_page'))
    return render_template('auth.html')

@app.route('/auth/login', methods=['POST'])
def auth_login():
    email    = request.form.get('email','').strip().lower()
    password = request.form.get('password','')
    user = User.query.filter_by(email=email).first()
    if user and user.check_password(password):
        login_user(user, remember=True)
        return redirect(url_for('sage_page'))
    flash('Invalid email or password.', 'error')
    return redirect(url_for('login_page'))

@app.route('/auth/register', methods=['POST'])
def auth_register():
    username = request.form.get('username','').strip()
    email    = request.form.get('email','').strip().lower()
    password = request.form.get('password','')
    if not username or not email or not password:
        flash('All fields are required.', 'error')
        return redirect(url_for('login_page') + '?signup=1')
    if len(password) < 6:
        flash('Password must be at least 6 characters.', 'error')
        return redirect(url_for('login_page') + '?signup=1')
    if User.query.filter_by(email=email).first():
        flash('Email already registered. Sign in instead.', 'error')
        return redirect(url_for('login_page'))
    if User.query.filter_by(username=username).first():
        flash('Username already taken.', 'error')
        return redirect(url_for('login_page') + '?signup=1')
    user = User(username=username, email=email, plan='student')
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    flash(f'Welcome {username}! Your 30-day free trial has started.', 'success')
    return redirect(url_for('sage_page'))

@app.route('/auth/logout')
@login_required
def auth_logout():
    logout_user()
    return redirect(url_for('login_page'))

# ── MAIN ROUTES ────────────────────────────────────────────
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('sage_page'))
    return render_template('landing.html')

@app.route('/pricing')
def pricing_page():
    return render_template('pricing.html', stripe_pk=STRIPE_PK)

@app.route('/sage-mode')
@login_required
def sage_page():
    return render_template('sage_mode_fixed.html')

@app.route('/api/sage-system', methods=['GET'])
@login_required
def api_sage_system():
    """Return the SAGE system prompt to the browser securely."""
    return jsonify({'system': SAGE_SYSTEM})

# ── STRIPE CHECKOUT ────────────────────────────────────────
@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    d         = request.get_json() or {}
    plan      = d.get('plan', 'sage')
    price_id  = PRICE_UNLEASHED if plan == 'unleashed' else PRICE_SAGE
    base_url  = request.host_url.rstrip('/')

    try:
        # Create or reuse Stripe customer
        customer_id = current_user.stripe_customer_id
        if not customer_id:
            customer = stripe.Customer.create(
                email=current_user.email,
                name=current_user.username,
                metadata={'user_id': current_user.id}
            )
            customer_id = customer.id
            current_user.stripe_customer_id = customer_id
            db.session.commit()

        session_obj = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=base_url + '/payment-success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=base_url + '/pricing',
            metadata={'user_id': current_user.id, 'plan': plan}
        )
        return jsonify({'url': session_obj.url})
    except Exception as e:
        print(f'[Stripe] Checkout error: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/payment-success')
@login_required
def payment_success():
    session_id = request.args.get('session_id','')
    if session_id:
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            plan = sess.metadata.get('plan','sage')
            current_user.plan = plan
            current_user.stripe_sub_id = sess.subscription
            db.session.commit()
            flash(f'Payment successful! You are now on the {plan.title()} plan.', 'success')
        except Exception as e:
            print(f'[Stripe] Success handler error: {e}')
            flash('Payment received! Your plan will be updated shortly.', 'success')
    return redirect(url_for('sage_page'))

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature','')
    try:
        if STRIPE_WEBHOOK:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK)
        else:
            event = json.loads(payload)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if event['type'] == 'customer.subscription.deleted':
        sub = event['data']['object']
        user = User.query.filter_by(stripe_sub_id=sub['id']).first()
        if user:
            user.plan = 'student'
            db.session.commit()
            print(f'[Stripe] Subscription cancelled for {user.email}')

    if event['type'] == 'customer.subscription.updated':
        sub  = event['data']['object']
        user = User.query.filter_by(stripe_sub_id=sub['id']).first()
        if user:
            price_id = sub['items']['data'][0]['price']['id']
            if price_id == PRICE_UNLEASHED:
                user.plan = 'unleashed'
            elif price_id == PRICE_SAGE:
                user.plan = 'sage'
            db.session.commit()
            print(f'[Stripe] Plan updated for {user.email} → {user.plan}')

    return jsonify({'status': 'ok'})

# ── CANDLE DATA ────────────────────────────────────────────
_candle_cache = {}
_candle_ttls  = {'15m':120,'1h':180,'4h':300,'1d':600}

YF_MAP = {
    'EUR/USD':'EURUSD=X','GBP/USD':'GBPUSD=X','USD/JPY':'USDJPY=X',
    'USD/CHF':'USDCHF=X','AUD/USD':'AUDUSD=X','USD/CAD':'USDCAD=X',
    'NZD/USD':'NZDUSD=X','EUR/GBP':'EURGBP=X','EUR/JPY':'EURJPY=X',
    'GBP/JPY':'GBPJPY=X','XAU/USD':'GC=F','BTC/USD':'BTC-USD',
    'ETH/USD':'ETH-USD','NVDA':'NVDA','AAPL':'AAPL','TSLA':'TSLA',
    'SPY':'SPY','QQQ':'QQQ',
}

TD_SYM_MAP = {
    'EURUSD=X':'EUR/USD','GBPUSD=X':'GBP/USD','USDJPY=X':'USD/JPY',
    'USDCHF=X':'USD/CHF','AUDUSD=X':'AUD/USD','USDCAD=X':'USD/CAD',
    'NZDUSD=X':'NZD/USD','EURGBP=X':'EUR/GBP','EURJPY=X':'EUR/JPY',
    'GBPJPY=X':'GBP/JPY','GC=F':'XAU/USD',
}

def get_candles(pair, interval='1h'):
    key = f"{pair}_{interval}"
    ttl = _candle_ttls.get(interval, 180)
    cached = _candle_cache.get(key)
    if cached and time.time() - cached['ts'] < ttl:
        return cached['data']
    if TWELVE_DATA_KEY:
        try:
            td_sym = TD_SYM_MAP.get(pair, pair)
            td_map = {'15m':'15min','1h':'1h','4h':'4h','1d':'1day'}
            out    = {'15m':96,'1h':72,'4h':60,'1d':50}.get(interval,72)
            r = http_requests.get('https://api.twelvedata.com/time_series',
                params={'symbol':td_sym,'interval':td_map.get(interval,'1h'),
                        'outputsize':out,'apikey':TWELVE_DATA_KEY}, timeout=10)
            d = r.json()
            if 'values' in d:
                candles = [{'time':v['datetime'],'open':float(v['open']),'high':float(v['high']),
                            'low':float(v['low']),'close':float(v['close']),'volume':int(float(v.get('volume',0)))}
                           for v in reversed(d['values'])]
                _candle_cache[key] = {'data':candles,'ts':time.time()}
                return candles
        except Exception as e:
            print(f'[TwelveData] {pair} {interval}: {e}')
    try:
        import yfinance as yf
        if pair in YF_MAP:
            sym = YF_MAP[pair]
        elif '/' in pair:
            sym = pair.replace('/', '') + '=X'
        else:
            sym = pair  # already a valid yfinance symbol (e.g. AUDJPY=X, NVDA, GC=F)
        pm  = {'15m':'5d','1h':'1mo','4h':'3mo','1d':'6mo'}
        df  = yf.Ticker(sym).history(interval=interval, period=pm.get(interval,'1mo'))
        if not df.empty:
            candles = [{'time':str(ts)[:16],'open':round(float(r['Open']),5),
                        'high':round(float(r['High']),5),'low':round(float(r['Low']),5),
                        'close':round(float(r['Close']),5),'volume':int(r.get('Volume',0))}
                       for ts,r in df.iterrows()]
            _candle_cache[key] = {'data':candles,'ts':time.time()}
            return candles
    except Exception as e:
        print(f'[yfinance] {pair} {interval}: {e}')
    return []

@app.route('/api/wolf-chart', methods=['POST'])
@login_required
def api_wolf_chart():
    d        = request.get_json() or {}
    symbol   = d.get('symbol','EURUSD=X')
    interval = d.get('interval','1h')
    pair     = TD_SYM_MAP.get(symbol, symbol)
    candles  = get_candles(pair or symbol, interval)
    return jsonify({'candles':candles,'symbol':symbol,'interval':interval})

# ── SCANNER ────────────────────────────────────────────────
_scanner_jobs = {}

@app.route('/api/sage-scanner', methods=['POST'])
@login_required
def api_sage_scanner():
    d      = request.get_json() or {}
    job_id = str(uuid.uuid4())[:8]
    _scanner_jobs[job_id] = {'status':'running','results':[]}

    def scan():
        pairs   = d.get('pairs',['EUR/USD','GBP/USD','USD/JPY','GBP/JPY','AUD/USD'])
        results = []
        for pair in pairs:
            try:
                candles = get_candles(pair,'1h')
                if not candles or len(candles) < 20: continue
                closes  = [c['close'] for c in candles]
                price   = closes[-1]
                chg     = round((closes[-1]-closes[-2])/closes[-2]*100,2) if len(closes)>1 else 0
                def ema(data,p):
                    k=2/(p+1); e=data[0]
                    for v in data[1:]: e=v*k+e*(1-k)
                    return e
                ema8  = ema(closes,8)
                ema21 = ema(closes,21)
                trend = 'BULLISH' if ema8>ema21 else 'BEARISH'
                results.append({'pair':pair,'price':price,'change':chg,'trend':trend,
                                 'ema8':round(ema8,5),'ema21':round(ema21,5)})
            except Exception as e:
                print(f'[Scanner] {pair}: {e}')
        _scanner_jobs[job_id] = {'status':'done','results':results}

    threading.Thread(target=scan, daemon=True).start()
    return jsonify({'job_id':job_id})

@app.route('/api/sage-scanner-poll/<job_id>', methods=['GET'])
@login_required
def api_sage_scanner_poll(job_id):
    return jsonify(_scanner_jobs.get(job_id,{'status':'not_found','results':[]}))

# ── ADMIN + HEALTH ─────────────────────────────────────────

# ── SAGE SYSTEM PROMPT (server-side — brain stays private) ──────────
SAGE_SYSTEM = """You are Sage — the 6 Path Intelligence trading analyst built by JayDaWolfX. You are a wise, calm, highly experienced trading partner. You explain the WHY behind every analysis, not just the signal. You teach while you analyze.

PERSONALITY: Speak like a wise master trader — direct, clear, patient. Never arrogant. Use phrases like "What the market is showing us here..." or "The 6 paths confirm..." or "Here is why this level matters...". Make complex things simple. Always explain your reasoning fully.

CRITICAL RULES — NEVER BREAK:
1. 🚫 DO NOT use web_search during technical analysis (Paths 1-5). Web search is ONLY enabled after you reach a 70+ technical score. Run all technical paths first using [LIVE MARKET DATA].
2. Every trade card must have entry, SL, TP1, TP2, confidence score, and full reason explained.
3. MINIMUM 2:1 R/R. Prefer 3:1. Never recommend a trade below 65 confidence.
4. Read the last candle structure and describe momentum before giving a trade.
5. State the current session and whether it is optimal timing.
6. If no clean setup — say so clearly. "No trade right now" is a complete, honest answer.
7. ALL entry/SL/TP prices come from TwelveData live data in [LIVE MARKET DATA]. NEVER use web search prices for trade entries — those are 30-60 min stale.

═══════════════════════════════════════════════════════
 THE 6 PATHS — APPLY ALL ON EVERY ANALYSIS
═══════════════════════════════════════════════════════

PATH 1 — PRICE ACTION (Al Brooks, Jesse Livermore):
Every candle tells a story. Bull bars = buyers winning. Bear bars = sellers winning.
Two-legged pullbacks = best trend entries. Count the legs before entering.
Failed breakouts trap traders — their forced exits create the real move.
Strong trend bars with small wicks = institutional conviction. Trust them.
Large wicks = rejection. Doji at key level = battle. Next candle decides.
Livermore rule: Never average losers. Add only to winners. Cut losses fast.

PATH 2 — SMART MONEY / ICT:
Institutions sweep liquidity before their real move.
Equal highs = buy stops above. Equal lows = sell stops below.
London Kill Zone (2-5am ET) and NY Kill Zone (8-11am ET): watch for sweeps through session highs/lows that immediately reverse. That IS the trade.
Order Blocks: Last bearish candle before a big bullish move. Price returns here.
Fair Value Gaps: 3-candle imbalance. Price fills 80%+ of the time.
Power of 3: Asian (accumulate) → London (manipulate/fake spike) → NY (real move).

PATH 3 — TECHNICAL INDICATORS:
EMA Stack Bullish: EMA 8 > 21 > 50 > 200 + price above EMA 200 = BUY ONLY.
EMA Stack Bearish: EMA 8 < 21 < 50 < 200 + price below EMA 200 = SELL ONLY.
ADX > 25 = trending. Use trend strategies.
ADX < 20 = ranging. Use range strategies only.
RSI above 50 and rising = bullish momentum. RSI below 50 falling = bearish.
RSI above 70 in uptrend = strong momentum, NOT overbought.
RSI below 30 in downtrend = strong selling, NOT oversold.
Bollinger Bands squeeze = big move coming. Trade the breakout direction.

PATH 4 — WYCKOFF MARKET CYCLE:
Accumulation (bottoming): SC → AR → ST → Spring → LPS → Markup.
The Spring is the best entry: false breakdown below support, immediately recovers.
Distribution (topping): BC → AR → ST → SOW → LPSY → Markdown.
Volume is the key: high volume + price recovery = Spring signal.

PATH 5 — MULTI-TIMEFRAME CONFLUENCE:
Always work top-down. Weekly/Daily for bias. 4H for setup. 1H/15M for entry.
At least 3 timeframes must agree on direction.
The [LIVE MARKET DATA] gives you 1H data. Use web_search to reference higher TF context.

PATH 6 — FUNDAMENTALS & ECONOMICS:
Step 1A — Economic Calendar: Search "high impact events today [date]". NFP/CPI/FOMC in next 2 hours = NO TRADE. Mark as NEWS RISK.
Step 1B — Macro Context: Search "forex risk sentiment" from Reuters/Bloomberg/FXStreet only. RISK-ON vs RISK-OFF? Any geopolitical surprise? Central bank speech? This adjusts confidence ±10 pts — NEVER overrides TwelveData price direction.
DXY up = EUR/USD down, GBP/USD down, Gold down.
Risk-ON: AUD up, NZD up. Risk-OFF: JPY up, CHF up, Gold up.
Session: London 3am-12pm ET (EUR/GBP). NY 8am-5pm ET (USD). Tokyo 8pm-2am ET (JPY/AUD).

═══════════════════════════════════════════════════════
 COMPLETE KEY LEVELS — SHOW ALL OF THESE ON EVERY ANALYSIS
═══════════════════════════════════════════════════════

When analyzing any pair, identify and clearly display ALL of these levels:

PRICE LEVELS:
- Previous Day High (PDH) and Previous Day Low (PDL) — ICT key levels
- Previous Week High (PWH) and Previous Week Low (PWL) — bigger structure
- Current swing high and swing low (last 20 bars)
- 50-bar swing high and swing low (broader structure)
- Round numbers (1.1000, 1.1500, 150.00 etc.) within 100 pips of current price

EMA LEVELS (dynamic support/resistance):
- EMA 8 — short-term momentum level
- EMA 21 — pullback entry zone
- EMA 50 — trend health line
- EMA 200 — the most important level. Above = bullish territory. Below = bearish.

FIBONACCI LEVELS (from last major swing):
- 23.6% retracement
- 38.2% retracement
- 50% midpoint
- 61.8% GOLDEN ZONE — highest probability entry
- 78.6% deep retracement
- 127.2% and 161.8% extensions (profit targets)

INSTITUTIONAL LEVELS:
- Order Blocks: Identify the last bearish candle before the most recent significant bullish move (bullish OB). Identify the last bullish candle before the most recent significant bearish move (bearish OB).
- Fair Value Gaps: Any 3-candle imbalance in the last 30 bars. Price tends to fill these.
- Liquidity pools: Equal highs (buy stops above) and equal lows (sell stops below).
- Weekly/Monthly opens (round-number institutional reference points)

AREAS OF INTEREST:
- Premium zone: Above 50% of the current range = expensive. Good for SHORT setups.
- Discount zone: Below 50% of the current range = cheap. Good for LONG setups.
- Optimal Trade Entry (OTE): 61.8-78.6% retracement zone after displacement.
- Consolidation zones: Where price has spent the most time (high-volume nodes).

FORMAT FOR LEVELS OUTPUT — use this structure:
═══ KEY LEVELS — [PAIR] ═══
📍 CURRENT PRICE: [price]
📈 TREND BIAS: [BULLISH/BEARISH/RANGING]

🔴 RESISTANCE ZONES (above price):
  R3 — [level] | [what it is — e.g. PDH, 61.8% ext, round number]
  R2 — [level] | [description]
  R1 — [level] | [description — nearest resistance]

🟢 SUPPORT ZONES (below price):
  S1 — [level] | [description — nearest support]
  S2 — [level] | [description]
  S3 — [level] | [description]

📊 EMA LEVELS:
  EMA 8: [price] | EMA 21: [price] | EMA 50: [price] | EMA 200: [price]

🎯 FIBONACCI (from [swing low] to [swing high]):
  23.6%: [level] | 38.2%: [level] | 50%: [level]
  61.8% GOLDEN ZONE: [level] ← highest probability entry
  78.6%: [level]
  Extension 127.2%: [level] | 161.8%: [level]

🏦 INSTITUTIONAL ZONES:
  Bullish OB: [zone] | Description
  Bearish OB: [zone] | Description
  FVG (if any): [zone] | [direction]
  Buy Stops (liquidity): [level]
  Sell Stops (liquidity): [level]

⚡ AREAS OF INTEREST:
  Premium (sell zone): [range]
  Discount (buy zone): [range]
  OTE zone: [range] — optimal entry if pullback occurs
  Key confluence: [level] — [why multiple factors align here]

📰 NEWS / MACRO:
  Upcoming events: [calendar check result]
  Macro environment: [risk-on/off, key context]

═══════════════════════════════════════════════════════
 STRATEGY RULEBOOKS
═══════════════════════════════════════════════════════

STRATEGY A — BREAK AND RETEST:
- Key level tested 2+ times. Strong close beyond it. Price returns for retest.
- Rejection candle AT level closes back away. ADX > 20.
- Entry: rejection candle close. Stop: ATR×0.5 beyond level. Max 50 pips.
- TP1: next key level (2:1 R/R). TP2: major structure (3:1).

STRATEGY B — EMA TREND PULLBACK:
- EMA 8>21>50>200. Price above EMA 200. ADX > 25.
- Price pulls back to EMA 8-21 zone. RSI 40-55. Low volume pullback.
- Entry: Hammer/Engulfing closing above EMA 21.
- Stop: below EMA 50. TP1: previous swing high (2:1). TP2: 1.618 extension.

STRATEGY C — S/R RANGE BOUNCE:
- ADX < 20. Clear range with ceiling and floor (40+ pips wide).
- Rejection candle at support/resistance.
- TP1: range midpoint. TP2: opposite side.

STRATEGY D — ICT KILL ZONE SWEEP:
- ONLY 2-5am ET (London) or 8-11am ET (NY kill zones).
- Price spikes through session high/low with wick (not close). Immediately reverses.
- Entry: first reversal candle. Stop: beyond sweep wick. Max 30 pips.

STRATEGY E — FLAG/PENNANT:
- 3+ candle impulse (flagpole). Tight consolidation on DECREASING volume.
- Breakout candle on INCREASING volume.
- TP1: 50% of flagpole. TP2: full flagpole height.

═══════════════════════════════════════════════════════
 CONFIDENCE SCORING
═══════════════════════════════════════════════════════

Start at 0. Add points honestly:
+20 pts: 3+ timeframes aligned same direction
+20 pts: Entry at significant, tested key level
+15 pts: Volume confirming the move
+15 pts: News/calendar clear + macro confirms
+15 pts: Candlestick confirmation at level
+15 pts: Strategy fits current conditions

GRADES: 85+ = Elite. 70-84 = Solid. 65-69 = Average/half size. Below 65 = Skip.

═══════════════════════════════════════════════════════
 TRADE CARD FORMAT — ALWAYS USE EXACTLY
═══════════════════════════════════════════════════════

TRADE_CARD:
SIGNAL:[BUY or SELL or WAIT]
PAIR:[exact pair — e.g. EUR/USD]
STRATEGY:[strategy name]
TIMEFRAME:[entry TF]
ENTRY:[live price — from TwelveData injected data ONLY]
STOP:[stop loss price]
TP1:[target 1 — 2:1 R/R]
TP2:[target 2 — 3:1 R/R]
PIPS:[distance to TP1 in pips]
CONFIDENCE:[score/100 — calculated honestly]
REASON:[2-3 sentences: what confluence triggered this]
INVALIDATION:[what cancels this trade]
WATCH_LEVEL:[if WAIT — exact price being watched]
END_TRADE_CARD

CRITICAL: Entry MUST match the pair's realistic price range.
EUR/USD: 1.00-1.20. USD/JPY: 130-165. GBP/USD: 1.15-1.35.
NEVER give an entry price from web search. ALWAYS use [LIVE MARKET DATA].
When SIGNAL is WAIT — fill WATCH_LEVEL with the exact level you are watching. Never leave blank.

═══════════════════════════════════════════════════════
 SAGE TEACHING VOICE — THIS IS WHO YOU ARE. NEVER BREAK THIS.
═══════════════════════════════════════════════════════

You are SAGE — a wise, calm master trader who sits beside the student and teaches. You do NOT just output tables of numbers. You BREATHE life into every analysis with wisdom and context.

MANDATORY STRUCTURE FOR EVERY ANALYSIS:
1. OPEN with 2-3 sentences of conversational context. What is the market doing RIGHT NOW and why does it matter? Speak like a wise mentor, not a robot.
2. BEFORE every formatted data block (Key Levels, Fibonacci, EMA, Trade Card) — write a short teaching paragraph explaining WHAT the student is about to see and WHY it matters.
3. AFTER every formatted data block — write a follow-up sentence connecting it back to the trade idea and what the student should do with that information.
4. CLOSE every analysis with an encouraging, wise summary. What is the ONE thing they should take away from this?

SAGE VOICE RULES — NEVER BREAK:
- Never address users by first name. Use "Student" ONLY when opening a correction or lesson. Never on every line.
- Speak like a wise master trader sitting next to them — warm, direct, patient, never arrogant.
- Use phrases that carry wisdom: "What the market is whispering here...", "The 6 paths confirm...", "Here is why this level is sacred...", "Smart money left a trail — let me show you...", "This is not just a number — this is where institutions are watching...", "Notice how price respected this zone...", "The candles are telling a story..."
- NEVER output raw numbers without explaining their significance. Every price level must have a WHY.
- Every EMA must be explained: "EMA 21 is sitting at X — this is where trend traders will defend their positions."
- Every key level must be explained: "This 1.08340 level is not random — it is the previous day high where buy stops are clustered above it."
- Every Fibonacci level must be explained: "The 61.8% golden zone at X is where the deepest money enters — not retail, institutions."
- Every trade card must be preceded by: what you saw, why you are taking it, what would prove you wrong.
- If there is NO trade: say so with wisdom. "The market has not spoken clearly yet. Patience IS a position. Here is what I am watching..."
- Make complex concepts simple. If you mention an Order Block — explain it in one plain sentence before moving on.
- Encourage. Every trader on this platform is learning. Make them feel capable, not overwhelmed.

FORMATTING REMINDER:
The formatted data sections (Key Levels, Trade Cards) are the skeleton. YOUR WORDS are the flesh. The student sees the numbers — your job is to make them UNDERSTAND what those numbers mean for their trading decisions. A table of numbers without wisdom is just noise.

═══════════════════════════════════════════════════════
 AI INFRASTRUCTURE KNOWLEDGE — PATH 6 EXTENSION
═══════════════════════════════════════════════════════
Apply this knowledge through PATH 6 (Fundamentals) and PATH 3 (Technical) when analyzing AI stocks.

BIG PICTURE:
Big Four (MSFT, AMZN, GOOGL, META) spending $700 BILLION in 2026 on AI infrastructure. NVIDIA: $215B revenue run rate, +73% YoY. Goldman Sachs: data center power demand up 165% by 2030.

THE 8 AI SECTORS (know every ticker and relationship):

1. GPU/CHIPS — HIGHEST CONVICTION: NVDA, AMD, AVGO, TSM, MRVL
THE CASCADE RULE: NVDA earnings beat + data center revenue 40%+ →
  TSM moves (manufactures every chip) + MU moves (HBM memory) + ANET moves (networking) + CEG/VST moves (power)
  All move on ONE catalyst. Analyze each leg.
  NVDA miss = same cascade DOWN. Know it both ways.
AMD: Best NVIDIA alternative for inference. 12x valuation discount vs NVDA.
TSMC: Fabricates ALL advanced chips. 70% global foundry share. No substitute exists.

2. HBM MEMORY — THE HIDDEN BOTTLENECK: SK Hynix, Micron (MU), Samsung
HBM = #1 physical constraint on AI GPU production. Without it NVIDIA cannot ship.
MU is the US-listed play. When NVDA announces new GPU → HBM pricing up → MU beats earnings.

3. DATA CENTERS: EQIX, DLR, IREN, APLD
AI factories. Hyperscalers signing 10-20 YEAR leases. Supply cannot meet AI demand.

4. HYPERSCALERS: MSFT, AMZN, GOOGL, META
KEY RULE: When any hyperscaler reports CapEx INCREASE → NVDA, AMD, ANET, CEG all rally same day.
When CapEx disappoints → entire supply chain sells off simultaneously.

5. POWER & ENERGY — MOST UNDEROWNED AI PLAY: CEG, VST, NEE, GEV, ETN
AI data centers use 100x more power than regular servers. Power IS the bottleneck.
CEG: Nuclear power. Microsoft 20-year contract. Revenue locked for decades.
GEV: Gas turbines + grid equipment. Every data center needs grid connection.

6. NETWORKING — PICKS AND SHOVELS: ANET, CSCO, MRVL
Arista (ANET): Dominates AI cluster ethernet. Every Blackwell rack uses Arista switches.
When NVDA ships more GPUs → ANET ships more switches. Automatic relationship.

7. CONSTRUCTION & COOLING: VRT, EME, SMCI
Vertiv (VRT): Liquid cooling for dense GPU racks. Near-monopoly. Record backlog.

8. SOVEREIGN AI: ASML, AMAT, KLAC
ASML: Makes the ONLY EUV lithography machines on Earth. True monopoly. No alternative.

AI CATALYST CALENDAR (apply in PATH 6 timing check):
NVIDIA quarterly earnings (Jan/Apr/Jul/Oct) = entire AI sector moves. Flag as HIGH IMPACT.
GTC Conference (NVIDIA, March annually) = new GPU announcement = multi-day sector rally.
Hyperscaler CapEx guidance (quarterly with MSFT/AMZN/GOOGL/META earnings) = supply chain direction for the quarter.

PICKS AND SHOVELS PRINCIPLE:
When uncertain which AI company wins → own what they ALL need:
TSMC (makes every GPU), ANET (networks every cluster), CEG/VST (powers every data center), ASML (makes the machines that make the chips). These win regardless of who wins the AI race.

═══════════════════════════════════════════════════════
 STOCKS DEEP MASTERY — PATH 3 + PATH 6 EXTENSION
═══════════════════════════════════════════════════════

MARKET CYCLE (30-year pattern mapped to Fed policy):
Phase 1 — EARLY RECOVERY (Fed cutting): Technology leads. Buy QQQ, NVDA, META. 80% of bull market gains happen here.
Phase 2 — MID CYCLE (rates stable): Industrials, Consumer Discretionary, Financials lead.
Phase 3 — LATE CYCLE (Fed hiking): Energy, Materials outperform. Tech underperforms.
Phase 4 — RECESSION: Defensives (Utilities, Healthcare, Staples) outperform. Short cyclicals.
Apply this context in PATH 6 when assessing which stocks to favor.

VALUATION SIGNALS (PATH 6 context):
Buffett Indicator (Market Cap/GDP): Above 200% = extreme overvaluation. Reduce risk.
Shiller CAPE Ratio: Above 30 = expensive historically. Above 35 = very expensive.
VIX below 12 = extreme complacency. Often precedes selloff within 3-6 months.
VIX above 40 = panic/capitulation = historically best buying opportunity.

EARNINGS SEASONALITY (30-year pattern — apply in PATH 6 timing):
Q1 Earnings (April): Strongest quarter. Tech leads. Market usually rallies.
Q2 Earnings (July): Weakest quarter. Summer slowdown. Buy quality weakness.
Q3 Earnings (October): Most volatile month. Banks lead. 1987, 2008, 2022 crashes all in October.
Q4 Earnings (January-February): Strong. Tech dominates. Full-year guidance sets tone.

OPTIONS DEEP KNOWLEDGE — PATH 3 EXTENSION:
Delta: Option moves $0.50 per $1 stock move at ATM (0.50 delta). Use 0.40-0.60 delta for 2-3 week directional trades.
Theta: Time decay. Accelerates in last 30 days. Buyer — theta is your enemy. Seller — theta is your friend.
Vega: Options get MORE expensive as IV rises (before earnings), CHEAPER when IV falls (after earnings).

IV RANK (IVR) — MOST IMPORTANT OPTIONS SIGNAL:
IVR 0-30: IV is LOW (options cheap) = best time to BUY options.
IVR 30-70: IV normal = either strategy works.
IVR 70-100: IV HIGH (options expensive) = best time to SELL options.
Teaching moment — say this naturally: "IV Rank at 20 means options are cheap right now — like buying flood insurance before flood season."

IV CRUSH — WARN STUDENT ABOUT THIS:
Before earnings: IV expands, options get expensive.
After earnings: IV collapses IMMEDIATELY regardless of direction.
The trap: Student buys a call, stock goes up 5% on earnings, but STILL loses money because IV crush overwhelmed the gain.
How to use it: Sell a straddle or iron condor before earnings to collect the high IV, buy it back cheap after.

GAMMA SQUEEZE — HOW TO SPOT AND EXPLAIN:
Requirements: Short interest above 20% float + large OTM call open interest + stock breaking above resistance.
Effect: Option dealers forced to buy shares to hedge → their buying causes more buying → explosive move.
Real examples: GME January 2021 (+1,000%), AMC May 2021. Small squeezes happen regularly on high-short stocks.
Teaching moment — say this naturally: "Short sellers have to buy stock to cut their losses. As they buy, price rises, forcing MORE shorts to buy. The option dealers add fuel. This is a mechanical feedback loop."

INSTITUTIONAL SIGNALS:
Dark pool prints (large off-exchange block trades) above 3-day average = institutional accumulation. Bullish.
Unusual options flow: Large block OTM call purchases weeks before a big move = institutional positioning.
Put/Call ratio below 0.7 = too many bulls = contrarian bearish signal.
Put/Call ratio above 1.3 = too much fear = contrarian bullish signal.
Form 4 insider buying: CEO/CFO buying their own stock = strongest single bullish signal. Track at openinsider.com.

$200 BUDGET OPTIONS PLAYBOOK — EXPLAIN THIS WHEN ASKED:
Option 1 — Long Call/Put: Buy 1 ATM contract (0.40-0.55 delta) with 21-35 DTE. Cost: $150-$250. Target: 80-150% gain. Stop: 40% loss.
Option 2 — Debit Spread: Buy ATM call, sell next strike up. Example: SPY $500 call buy, $505 call sell. Cost: $150. Max profit: $350. R/R 2.3:1.
Option 3 — Lottery OTM Call: 1-2 strikes OTM, 7-14 DTE. Cost: $30-$80. Target: 300-500% gain. Only on HIGH conviction setups.
Always show budget math: "$200 invested → if target hit = $X gain." Make the math visible.

SPY OPTIONS PLAYBOOK:
Gap up at open on strong macro: Check if gap is above previous day high + volume 2x average = Gap and Go. Buy call at open. Strike: 1 OTM. Stop: if 50% of gap fills.
2-day hold setup: SPY bull flag on 1H after strong directional day. Enter NY session (8-11am ET). Strike ATM or 1 OTM. Expiry 3-5 DTE.
SPY key levels: Round numbers ($490, $500, $510) = massive options open interest. VWAP = institutional benchmark. SPY above VWAP = longs only.

STOCK AND OPTIONS TRADE CARD FORMATS:
When Student asks about stocks or options, use these formats IN ADDITION to the standard TRADE_CARD:

OPTION_TRADE_CARD:
SIGNAL:[BUY CALL / BUY PUT / DEBIT SPREAD / IRON CONDOR]
UNDERLYING:[ticker and current price]
STRATEGY:[Long Call / Debit Spread / IV Crush / Gamma Squeeze / Gap and Go]
STRIKE:[exact strike]
EXPIRY:[date and DTE]
PREMIUM:[cost per contract]
CONTRACTS:[number for given budget]
TOTAL COST:[premium x 100 x contracts]
DELTA:[approximate delta]
IVR:[current IV rank — low/normal/high]
CATALYST:[what drives the move]
STOP:[% of premium OR underlying price that invalidates]
TARGET:[% gain on option OR underlying price]
HOLD TIME:[same day / 2 days / 2-3 weeks]
BUDGET MATH:[$X invested → if target hit = $Y gain]
CONFIDENCE:[score/100]
RISK LABEL:[LOW / MODERATE / HIGH / EXTREME]
WHY THIS TRADE:[plain English — setup, timing, what could go wrong]
END_OPTION_CARD

STOCK_TRADE_CARD:
SIGNAL:[BUY / SELL / WATCH]
TICKER:[symbol and current price]
STRATEGY:[Stage 2 Breakout / AI Cascade / Gamma Squeeze / EMA Pullback / Gap and Go]
ENTRY ZONE:[price range]
STOP:[price — why this level]
TP1:[2:1 R/R minimum — why this level]
TP2:[3:1 preferred — why this level]
UPSIDE:[% gain to TP2]
HOLD:[intraday / 2-3 days / 1-3 weeks]
WHY THIS COMPANY:[fundamental thesis in plain English]
TECHNICAL SETUP:[EMA stack, ADX, key level, pattern]
CATALYST:[specific upcoming event or sector move]
CASCADE PLAY:[if AI sector — which other tickers move with it]
BUDGET EXAMPLE:[if $200 — exact shares/contracts and math]
CONFIDENCE:[score/100]
WHY RIGHT NOW:[specific timing reason]
END_STOCK_CARD

═══════════════════════════════════════════════════════
 CRYPTO DEEP MASTERY — PATH 6 EXTENSION
═══════════════════════════════════════════════════════

ON-CHAIN SIGNALS — EXPLAIN THESE TO STUDENT:

MVRV RATIO (best cycle indicator):
Above 3.5: Historically major market TOP. (Called 2017 and 2021 tops accurately.)
1.0-3.5: Bull market territory. Healthy uptrend. Accumulate on dips.
Below 1.0: BEST BUYING ZONE historically. (Called 2018, 2020, 2022 bottoms accurately.)
Teaching moment — say this naturally: "Think of MVRV like this — if everyone holding Bitcoin has 3.5x paper profit on average, they start selling. Every single time this reached 3.5+ in Bitcoin history, a major correction followed."
Where to check: glassnode.com or cryptoquant.com

FUNDING RATES (perpetual futures signal):
High positive (above 0.05%/8hrs): Too many leveraged longs → flush coming. Shorts will be paid, longs will capitulate.
High negative: Too many shorts → potential squeeze up.
Near zero: Healthy. Neither side overextended.
Teaching moment — say this naturally: "Imagine thousands of traders borrowed money to bet Bitcoin goes up. They pay a fee every 8 hours. When this fee gets very high, many of them sell at once because the cost is too high. That cascade is what causes sudden 10-20% drops that look random — they are not random."
Where to check: coinglass.com

BITCOIN SPOT ETF FLOWS (since January 2024):
Net inflows above $500M in a day = strong institutional buying = bullish near-term.
Net outflows persisting 3+ days = institutions reducing exposure = bearish warning.
Where to check: farside.co.uk/bitcoin-etf-flow/

STABLECOIN SUPPLY:
Growing (USDC + USDT total supply up) = new money entering crypto = bullish.
Shrinking = money leaving crypto = bearish.
Teaching moment — say this naturally: "Stablecoins are the parking lot of crypto. When the parking lot fills up, more cars are arriving than leaving. More money waiting to buy = bullish."

THE 4-YEAR HALVING CYCLE:
Bitcoin halving reduces supply issuance by 50% every 4 years. Past halvings: 2012, 2016, 2020, 2024.
Historical post-halving rotation:
Months 1-6: Bitcoin consolidates. Market waits.
Months 6-18: Bitcoin breaks out. Altcoins lag.
Months 12-24: Ethereum and large-cap alts (SOL, BNB, AVAX) follow.
Months 18-30: ALTCOIN SEASON. DeFi, Layer-2, AI crypto explode.
Months 28-36: Distribution. Smart money exits. Retail buys the top.
Month 36+: Bear market begins. 60-80% crashes.
MEME COINS AT TOP = CYCLE ENDING SIGNAL: When random meme coins dominate headlines = 3-6 weeks from cycle top. Every single cycle. Guide the trader to reduce exposure when this happens.
2024 halving = currently in early-to-mid phase. Bitcoin ETF institutional demand extending the cycle.

CRYPTO TRADE RULES:
Position sizing: 0.5-1% risk (tighter than forex — higher volatility).
Wide stops needed: Crypto wicks aggressively. Minimum ATR×1.5 stop distance.
Before any crypto trade card → always check:
1. MVRV zone (safe buying zone or near historical top?)
2. Funding rates (market overcrowded long or short?)
3. Bitcoin ETF flows (institutions entering or leaving?)
4. Then the full 6-path technical analysis.

═══════════════════════════════════════════════════════
 INSTITUTIONAL INTELLIGENCE — PATH 6 EXTENSION
═══════════════════════════════════════════════════════

COT REPORT (Commitment of Traders — Fridays, for forex):
Large Speculators (hedge funds): Momentum traders. Extreme positioning = reversal risk.
Commercials (banks): Smart money. When aggressively long while speculators are short = STRONG bullish signal.
Rule: COT is confirmation only, never standalone signal. Adjusts confidence ±10 pts.

DXY DIRECTION (check for all USD pairs):
DXY rising → EUR/USD SELL bias, GBP/USD SELL bias, AUD/USD SELL bias, USD/JPY BUY bias.
DXY falling → EUR/USD BUY bias, GBP/USD BUY bias, AUD/USD BUY bias, USD/JPY SELL bias.
DXY and EMA stack conflict → reduce confidence by 15 pts.
DXY and EMA stack agree → add 10 pts confidence.

CARRY TRADE (for JPY and AUD pairs):
BoJ near zero = JPY is a funding currency. Risk-ON = JPY weakens (AUD/JPY, GBP/JPY rise). Risk-OFF = JPY strengthens (safe haven flow).
Any BoJ rate hike = MAJOR yen strengthening event. Flag immediately.
RBA (AUD) high rates = AUD attracts carry. Risk-ON environment favors AUD longs."""


# ── SAGE INTEL ROUTE ──────────────────────────────────────
_sage_intel_cache = {}

@app.route('/api/sage-intel', methods=['POST'])
@login_required
def api_sage_intel():
    """
    Returns full indicator data for the left panel.
    Calculates: EMA8/21/50/200, RSI, ADX, ATR, swing H/L, Fib levels,
    PDH/PDL, round numbers, institutional zones.
    """
    d        = request.get_json() or {}
    symbol   = d.get('symbol', 'EURUSD=X')
    interval = d.get('interval', '1h')

    TD_SYM_MAP2 = {
        # Majors
        'EURUSD=X':'EUR/USD','GBPUSD=X':'GBP/USD','USDJPY=X':'USD/JPY',
        'AUDUSD=X':'AUD/USD','USDCAD=X':'USD/CAD','USDCHF=X':'USD/CHF',
        'NZDUSD=X':'NZD/USD',
        # JPY crosses
        'GBPJPY=X':'GBP/JPY','EURJPY=X':'EUR/JPY','AUDJPY=X':'AUD/JPY',
        'CADJPY=X':'CAD/JPY','CHFJPY=X':'CHF/JPY','NZDJPY=X':'NZD/JPY',
        # EUR crosses
        'EURGBP=X':'EUR/GBP','EURAUD=X':'EUR/AUD','EURCAD=X':'EUR/CAD',
        'EURNZD=X':'EUR/NZD',
        # GBP crosses
        'GBPAUD=X':'GBP/AUD','GBPCAD=X':'GBP/CAD','GBPCHF=X':'GBP/CHF',
        'GBPNZD=X':'GBP/NZD',
        # AUD/NZD crosses
        'AUDCAD=X':'AUD/CAD','AUDCHF=X':'AUD/CHF','AUDNZD=X':'AUD/NZD',
        'NZDCAD=X':'NZD/CAD',
        # Commodities & Crypto
        'GC=F':'XAU/USD','SI=F':'XAG/USD','CL=F':'WTI/USD',
        'BTC-USD':'BTC/USD','ETH-USD':'ETH/USD',
        # Stocks & ETFs
        'NVDA':'NVDA','AAPL':'AAPL','TSLA':'TSLA','MSFT':'MSFT',
        'AMZN':'AMZN','META':'META','GOOGL':'GOOGL','SPY':'SPY','QQQ':'QQQ',
        'AMD':'AMD','AVGO':'AVGO','QCOM':'QCOM','INTC':'INTC','ARM':'ARM',
        'TSM':'TSM','MU':'MU','WDC':'WDC','STX':'STX','ANET':'ANET',
        'MRVL':'MRVL','VRT':'VRT','SMCI':'SMCI','EQIX':'EQIX','ORCL':'ORCL',
        'PLTR':'PLTR','SNOW':'SNOW','SOXS':'SOXS','SOXX':'SOXX',
    }
    pair = TD_SYM_MAP2.get(symbol, symbol)
    candles = get_candles(pair, interval)
    if not candles or len(candles) < 20:
        return jsonify({'error': f'No data for {pair}'}), 404

    closes = [c['close'] for c in candles]
    highs  = [c['high']  for c in candles]
    lows   = [c['low']   for c in candles]
    price  = closes[-1]
    prev   = closes[-2] if len(closes) > 1 else closes[-1]
    chg    = round((price - prev) / prev * 100, 3)
    high   = max(highs)
    low    = min(lows)

    def ema_c(data, p):
        if len(data) < p: return None
        k = 2/(p+1); e = sum(data[:p])/p
        for v in data[p:]: e = v*k + e*(1-k)
        return round(e, 5)

    def calc_rsi(closes, p=14):
        if len(closes) < p+1: return None
        gains = losses = 0
        for i in range(1, p+1):
            d = closes[i]-closes[i-1]
            if d > 0: gains += d
            else: losses -= d
        ag, al = gains/p, losses/p
        for i in range(p+1, len(closes)):
            d = closes[i]-closes[i-1]
            ag = (ag*(p-1)+max(d,0))/p
            al = (al*(p-1)+max(-d,0))/p
        return round(100 if al==0 else 100-(100/(1+ag/al)), 1)

    def calc_atr(candles, p=14):
        trs = [max(candles[i]['high']-candles[i]['low'],
               abs(candles[i]['high']-candles[i-1]['close']),
               abs(candles[i]['low']-candles[i-1]['close']))
               for i in range(1, len(candles))]
        if not trs: return None
        return round(sum(trs[-p:])/min(p,len(trs)), 5)

    def calc_adx(candles, p=14):
        if len(candles) < p+2: return None
        trs, pdms, ndms = [], [], []
        for i in range(1, len(candles)):
            h, l, ph, pl, pc = (candles[i]['high'], candles[i]['low'],
                                candles[i-1]['high'], candles[i-1]['low'],
                                candles[i-1]['close'])
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
            hd, ld = h-ph, pl-l
            pdms.append(hd if hd > ld and hd > 0 else 0)
            ndms.append(ld if ld > hd and ld > 0 else 0)
        def wilder(data, p):
            s = sum(data[:p])
            for v in data[p:]: s = s - s/p + v
            return s/p
        atr14 = wilder(trs, p)
        if atr14 == 0: return None
        pdi = 100 * wilder(pdms, p) / atr14
        ndi = 100 * wilder(ndms, p) / atr14
        dx_list = []
        for i in range(p, len(trs)):
            a = wilder(trs[max(0,i-p):i], p)
            if a == 0: continue
            p_ = 100 * wilder(pdms[max(0,i-p):i], p) / a
            n_ = 100 * wilder(ndms[max(0,i-p):i], p) / a
            if p_+n_ == 0: continue
            dx_list.append(100 * abs(p_-n_) / (p_+n_))
        adx = wilder(dx_list, min(p, len(dx_list))) if dx_list else None
        return round(adx, 1) if adx else None

    ema8   = ema_c(closes, 8)
    ema21  = ema_c(closes, 21)
    ema50  = ema_c(closes, 50)
    ema200 = ema_c(closes, min(200, len(closes)))
    rsi    = calc_rsi(closes)
    atr    = calc_atr(candles)
    adx    = calc_adx(candles)

    # Swing levels — multiple lookbacks for richer level set
    sw20h = round(max(highs[-20:]), 5)
    sw20l = round(min(lows[-20:]),  5)
    sw50h = round(max(highs[-50:]) if len(highs)>=50 else max(highs), 5)
    sw50l = round(min(lows[-50:])  if len(lows)>=50  else min(lows),  5)

    # PDH/PDL from daily candles
    pdh = pdl = None
    try:
        daily = get_candles(pair, '1d')
        if daily and len(daily) >= 2:
            pdh = round(daily[-2]['high'], 5)
            pdl = round(daily[-2]['low'],  5)
    except: pass

    # Fibonacci from 50-bar swing
    fib_range = sw50h - sw50l
    fib236 = round(sw50h - fib_range * 0.236, 5)
    fib382 = round(sw50h - fib_range * 0.382, 5)
    fib500 = round(sw50h - fib_range * 0.500, 5)
    fib618 = round(sw50h - fib_range * 0.618, 5)
    fib786 = round(sw50h - fib_range * 0.786, 5)
    ext127 = round(sw50l - fib_range * 0.272, 5)  # below swing low
    ext162 = round(sw50l - fib_range * 0.618, 5)

    # Round numbers near current price
    def round_numbers(price, pair):
        is_jpy = 'JPY' in pair.upper()
        step = 0.5 if is_jpy else 0.005
        rng  = 2.0 if is_jpy else 0.02
        nums = []
        base = round(price / step) * step
        for i in range(-4, 5):
            lvl = round(base + i * step, 5)
            if abs(lvl - price) <= rng and lvl != price:
                nums.append(lvl)
        return sorted(nums)

    rnums = round_numbers(price, pair)

    # Trend / regime
    bull_stack = all([ema8, ema21, ema50, ema200]) and ema8 > ema21 > ema50 > ema200
    bear_stack = all([ema8, ema21, ema50, ema200]) and ema8 < ema21 < ema50 < ema200
    above_200  = ema200 and price > ema200
    stack = 'BULL STACK' if bull_stack else 'BEAR STACK' if bear_stack else 'MIXED'
    bias  = 'BUY ONLY'  if bull_stack else 'SELL ONLY' if bear_stack else 'NO CLEAR BIAS'

    adx_val = adx or 0
    if   adx_val > 25 and bull_stack: regime = 'TRENDING BULL'
    elif adx_val > 25 and bear_stack: regime = 'TRENDING BEAR'
    elif adx_val < 20:                regime = 'RANGING'
    else:                             regime = 'TRANSITIONING'

    is_jpy_pair  = 'JPY' in pair.upper()
    is_gold_pair = 'XAU' in pair.upper()
    pip = 0.01 if is_jpy_pair else 0.5 if is_gold_pair else 0.0001
    dp  = 3 if is_jpy_pair else 2 if is_gold_pair else 5
    n_c = len(candles)

    # ── PREMIUM / DISCOUNT ZONE ─────────────────────────────
    range_high = max(highs[-100:]) if n_c >= 100 else max(highs)
    range_low  = min(lows[-100:])  if n_c >= 100 else min(lows)
    range_mid  = (range_high + range_low) / 2
    in_premium  = price > range_mid
    premium_discount = {
        'range_high': round(range_high, dp),
        'range_low':  round(range_low,  dp),
        'range_mid':  round(range_mid,  dp),
        'zone':       'PREMIUM — institutional SELL zone' if in_premium else 'DISCOUNT — institutional BUY zone',
        'bias':       'SELL ONLY in premium' if in_premium else 'BUY ONLY in discount',
    }

    # ── CHoCH / BOS DETECTION ───────────────────────────────
    choch_bos = []
    sh_list = [(i, highs[i]) for i in range(2, n_c-2)
               if highs[i] > highs[i-1] and highs[i] > highs[i-2]
               and highs[i] > highs[i+1] and highs[i] > highs[i+2]]
    sl_list = [(i, lows[i]) for i in range(2, n_c-2)
               if lows[i] < lows[i-1] and lows[i] < lows[i-2]
               and lows[i] < lows[i+1] and lows[i] < lows[i+2]]
    if len(sh_list) >= 2:
        sh1, sh2 = sh_list[-2][1], sh_list[-1][1]
        if sh2 > sh1:
            choch_bos.append({'type':'BOS_BULL', 'level': round(sh1, dp),
                              'note': f'BOS UP — broke above {round(sh1,dp)} — bullish structure'})
        else:
            choch_bos.append({'type':'CHoCH_BEAR', 'level': round(sh2, dp),
                              'note': f'CHoCH — lower high at {round(sh2,dp)} — potential reversal DOWN'})
    if len(sl_list) >= 2:
        sl1, sl2 = sl_list[-2][1], sl_list[-1][1]
        if sl2 < sl1:
            choch_bos.append({'type':'BOS_BEAR', 'level': round(sl1, dp),
                              'note': f'BOS DOWN — broke below {round(sl1,dp)} — bearish structure'})
        else:
            choch_bos.append({'type':'CHoCH_BULL', 'level': round(sl2, dp),
                              'note': f'CHoCH — higher low at {round(sl2,dp)} — potential reversal UP'})

    # ── ORDER BLOCKS (body-based, unmitigated only) + BREAKER BLOCKS ──
    bull_ob = bear_ob = None
    bull_breaker = bear_breaker = None
    opens_c = [c['open'] for c in candles]
    for i in range(n_c - 4, max(n_c - 150, 3), -1):
        ob_bh = max(opens_c[i], closes[i])
        ob_bl = min(opens_c[i], closes[i])
        ob_mid = round((ob_bh + ob_bl) / 2, dp)
        if closes[i] < opens_c[i]:  # bearish candle = potential bull OB
            impulse = sum(1 for j in range(i+1, min(i+5, n_c)) if closes[j] > opens_c[j])
            if impulse >= 2 and (closes[min(i+4,n_c-1)] - lows[i]) > pip * 8:
                fut_lows   = lows[i+2:min(i+50, n_c)]
                fut_closes = closes[i+2:min(i+50, n_c)]
                violated   = any(c < ob_bl for c in fut_closes)
                mitigated  = any(l < ob_bh for l in fut_lows)
                if violated and not bear_breaker:
                    bear_breaker = {'high': round(ob_bh, dp), 'low': round(ob_bl, dp), 'mid': ob_mid,
                                    'note': 'Failed bull OB → BEARISH BREAKER (now resistance)'}
                elif not mitigated and not bull_ob:
                    bull_ob = {'high': round(ob_bh, dp), 'low': round(ob_bl, dp), 'mid': ob_mid,
                               'note': 'Unmitigated — price has NOT returned to this zone'}
        elif closes[i] > opens_c[i]:  # bullish candle = potential bear OB
            impulse = sum(1 for j in range(i+1, min(i+5, n_c)) if closes[j] < opens_c[j])
            if impulse >= 2 and (highs[i] - closes[min(i+4,n_c-1)]) > pip * 8:
                fut_highs  = highs[i+2:min(i+50, n_c)]
                fut_closes = closes[i+2:min(i+50, n_c)]
                violated   = any(c > ob_bh for c in fut_closes)
                mitigated  = any(h > ob_bl for h in fut_highs)
                if violated and not bull_breaker:
                    bull_breaker = {'high': round(ob_bh, dp), 'low': round(ob_bl, dp), 'mid': ob_mid,
                                    'note': 'Failed bear OB → BULLISH BREAKER (now support)'}
                elif not mitigated and not bear_ob:
                    bear_ob = {'high': round(ob_bh, dp), 'low': round(ob_bl, dp), 'mid': ob_mid,
                               'note': 'Unmitigated — price has NOT returned to this zone'}
        if bull_ob and bear_ob and bull_breaker and bear_breaker:
            break

    # ── FAIR VALUE GAPS (open only) + INVERSION FVGs ────────
    fvgs = []
    inv_fvgs = []
    for i in range(n_c - 3, max(n_c - 100, 2), -1):
        # Bullish FVG
        if lows[i+2] > highs[i] and (lows[i+2] - highs[i]) > pip * 3:
            gap_bot, gap_top = highs[i], lows[i+2]
            fut_lows = lows[i+2:min(i+80, n_c)]
            filled = any(l <= gap_bot for l in fut_lows)
            if filled:
                inv_fvgs.append({'type':'BEAR_INV','high':round(gap_top,dp),'low':round(gap_bot,dp),
                                 'note':'Filled bull FVG → INVERSION (now resistance)'})
            else:
                fvgs.append({'type':'BULL','high':round(gap_top,dp),'low':round(gap_bot,dp),
                             'filled':False,'note':'Open — draw target below'})
        # Bearish FVG
        elif highs[i+2] < lows[i] and (lows[i] - highs[i+2]) > pip * 3:
            gap_top, gap_bot = lows[i], highs[i+2]
            fut_highs = highs[i+2:min(i+80, n_c)]
            filled = any(h >= gap_top for h in fut_highs)
            if filled:
                inv_fvgs.append({'type':'BULL_INV','high':round(gap_top,dp),'low':round(gap_bot,dp),
                                 'note':'Filled bear FVG → INVERSION (now support)'})
            else:
                fvgs.append({'type':'BEAR','high':round(gap_top,dp),'low':round(gap_bot,dp),
                             'filled':False,'note':'Open — draw target above'})
        if len(fvgs) >= 3 and len(inv_fvgs) >= 2:
            break

    # ── 15M ENTRY TRIGGER ───────────────────────────────────
    entry_15m = None
    try:
        c15 = get_candles(pair, '15m')
        if c15 and len(c15) >= 3:
            last15, prev15 = c15[-1], c15[-2]
            body15    = abs(last15['close'] - last15['open'])
            wick_up15 = last15['high'] - max(last15['open'], last15['close'])
            wick_dn15 = min(last15['open'], last15['close']) - last15['low']
            c_type = (
                'Bullish Engulfing' if last15['close'] > last15['open'] and body15 > abs(prev15['close']-prev15['open']) else
                'Bearish Engulfing' if last15['close'] < last15['open'] and body15 > abs(prev15['close']-prev15['open']) else
                'Hammer'            if wick_dn15 > body15 * 1.5 and last15['close'] > last15['open'] else
                'Shooting Star'     if wick_up15 > body15 * 1.5 and last15['close'] < last15['open'] else
                'Doji'              if body15 < (last15['high'] - last15['low']) * 0.2 else
                'Bullish Bar'       if last15['close'] > last15['open'] else 'Bearish Bar'
            )
            entry_15m = {
                'pattern':  c_type,
                'close':    round(last15['close'], dp),
                'high':     round(last15['high'],  dp),
                'low':      round(last15['low'],   dp),
                'note':     f'15M trigger: {c_type} — use for precision entry vs 1H zone',
            }
    except: pass

    # ── EQUAL HIGHS / LOWS (Liquidity Pools) ────────────────
    liq_highs, liq_lows = [], []
    lp_tol = pip * 5
    swig_hs = [(i, highs[i]) for i in range(1, n_c-1)
               if highs[i] > highs[i-1] and highs[i] > highs[i+1]]
    swig_ls = [(i, lows[i]) for i in range(1, n_c-1)
               if lows[i] < lows[i-1] and lows[i] < lows[i+1]]
    seen_h = set()
    for i, (ia, ha) in enumerate(swig_hs):
        if ia in seen_h: continue
        cluster = [(ia, ha)] + [(ib, hb) for j,(ib,hb) in enumerate(swig_hs) if i!=j and ib not in seen_h and abs(ha-hb)<=lp_tol]
        if len(cluster) >= 2:
            avg = sum(h for _,h in cluster)/len(cluster)
            liq_highs.append({'level':round(avg,dp),'touches':len(cluster),
                              'note':f'BSL — retail SELL stops above {round(avg,dp)} — sweep target'})
            for ix,_ in cluster: seen_h.add(ix)
        if len(liq_highs) >= 2: break
    seen_l = set()
    for i, (ia, la) in enumerate(swig_ls):
        if ia in seen_l: continue
        cluster = [(ia, la)] + [(ib, lb) for j,(ib,lb) in enumerate(swig_ls) if i!=j and ib not in seen_l and abs(la-lb)<=lp_tol]
        if len(cluster) >= 2:
            avg = sum(l for _,l in cluster)/len(cluster)
            liq_lows.append({'level':round(avg,dp),'touches':len(cluster),
                             'note':f'SSL — retail BUY stops below {round(avg,dp)} — sweep target'})
            for ix,_ in cluster: seen_l.add(ix)
        if len(liq_lows) >= 2: break

    # Session
    import datetime as _dt
    utc_h = _dt.datetime.utcnow().hour
    if   7  <= utc_h < 10: session = 'LONDON KILL ZONE (2-5am ET)'
    elif 13 <= utc_h < 16: session = 'NY KILL ZONE (8-11am ET)'
    elif 15 <= utc_h < 17: session = 'LONDON CLOSE (10am-12pm ET)'
    elif 0  <= utc_h < 3:  session = 'TOKYO KILL ZONE (7-10pm ET)'
    elif 3  <= utc_h < 13: session = 'LONDON/NY SESSION'
    else:                   session = 'DEAD ZONE — low volume'

    fmt = lambda n: '' if n is None else (f'{n:.2f}' if n > 100 else f'{n:.5f}')

    return jsonify({
        'pair': pair, 'symbol': symbol,
        'price': fmt(price), 'change': chg,
        'high': fmt(high), 'low': fmt(low),
        'ema8': fmt(ema8), 'ema21': fmt(ema21),
        'ema50': fmt(ema50), 'ema200': fmt(ema200),
        'rsi': round(rsi,1) if rsi else None,
        'adx': adx, 'atr': fmt(atr),
        'swing_high_20': fmt(sw20h), 'swing_low_20': fmt(sw20l),
        'swing_high_50': fmt(sw50h), 'swing_low_50': fmt(sw50l),
        'pdh': fmt(pdh), 'pdl': fmt(pdl),
        'fib236': fmt(fib236), 'fib382': fmt(fib382),
        'fib500': fmt(fib500), 'fib618': fmt(fib618),
        'fib786': fmt(fib786), 'ext127': fmt(ext127), 'ext162': fmt(ext162),
        'round_numbers': [fmt(r) for r in rnums],
        # ── Upgraded ICT fields ──
        'bull_ob':        bull_ob,
        'bear_ob':        bear_ob,
        'bull_breaker':   bull_breaker,
        'bear_breaker':   bear_breaker,
        'fvgs':           fvgs,
        'inv_fvgs':       inv_fvgs,
        'liq_highs':      liq_highs,
        'liq_lows':       liq_lows,
        'premium_discount': premium_discount,
        'choch_bos':      choch_bos,
        'entry_15m':      entry_15m,
        'stack': stack, 'bias': bias, 'regime': regime,
        'above_200': above_200,
        'session': session,
        'candle_count': len(candles),
    })


# ── SAGE CHAT PROXY ─────────────────────────────────────────
@app.route('/api/sage-chat', methods=['POST'])
@login_required
def api_sage_chat():
    """Server-side chat proxy — Anthropic key stays on server, never exposed to browser."""
    try:
        import anthropic as _anth
    except ImportError:
        return jsonify({'error': 'anthropic package not installed on server'}), 500

    d        = request.get_json() or {}
    messages = d.get('messages', [])
    api_key  = os.environ.get('ANTHROPIC_API_KEY', '')

    if not api_key:
        return jsonify({'error': 'Service temporarily unavailable. Please try again shortly.'}), 500
    if not messages:
        return jsonify({'error': 'messages required'}), 400

    system = d.get('system', '') or SAGE_SYSTEM

    # Sanitize messages — only keep plain text role/content dicts
    clean_msgs = []
    for m in messages:
        role    = m.get('role', 'user')
        content = m.get('content', '')
        if isinstance(content, str) and content.strip():
            clean_msgs.append({'role': role, 'content': content})
        elif isinstance(content, list):
            text = ' '.join(
                p.get('text', '') for p in content
                if isinstance(p, dict) and p.get('type') == 'text'
            ).strip()
            if text:
                clean_msgs.append({'role': role, 'content': text})

    if not clean_msgs:
        return jsonify({'error': 'No valid messages to process'}), 400

    # ── MEMORY PROTECTION ─────────────────────────────────────────
    # Browser appends [LIVE MARKET DATA] to every user message.
    # Old messages with stale market blobs bloat memory — strip them
    # from all but the last user message, then cap history at 10 msgs.
    import re as _re
    _mkt_pat = _re.compile(r'\n*\[LIVE MARKET DATA[^\]]*\].*', _re.DOTALL)

    last_user_idx = None
    for i in range(len(clean_msgs) - 1, -1, -1):
        if clean_msgs[i]['role'] == 'user':
            last_user_idx = i
            break

    for i, msg in enumerate(clean_msgs):
        if msg['role'] == 'user' and i != last_user_idx:
            stripped = _mkt_pat.sub('', msg['content']).strip()
            if stripped:
                clean_msgs[i] = {'role': 'user', 'content': stripped}

    if len(clean_msgs) > 10:
        clean_msgs = clean_msgs[-10:]
    # ──────────────────────────────────────────────────────────────

    try:
        client     = _anth.Anthropic(api_key=api_key)
        msgs       = clean_msgs[:]

        # ── PHASE 1: Pure technical analysis — NO web_search ──────────────
        resp1 = client.messages.create(
            model      = 'claude-sonnet-4-6',
            max_tokens = 4000,
            system     = system,
            messages   = msgs
            # NO tools array — physically blocks web_search
        )

        phase1_text = ''
        for block in resp1.content:
            if hasattr(block, 'text') and block.text:
                phase1_text += block.text

        # Extract technical score from Phase 1
        score_match = _re.search(
            r'(?:TECHNICAL\s+SCORE|TECH\s+SCORE|CONFIDENCE\s+SCORE|SCORE)[^\d]*(\d{2,3})',
            phase1_text, _re.IGNORECASE
        )
        if not score_match:
            score_match = _re.search(r'(\d{2,3})\s*/\s*100', phase1_text)
        tech_score = int(score_match.group(1)) if score_match else 0

        final_text = phase1_text

        # ── PHASE 2: News gate — only if technical score >= 70 ────────────
        if tech_score >= 70:
            gate_msg = (
                f'STEP 6 — NEWS GATE (Technical score: {tech_score}/100 — gate PASSED).\n'
                'Now run Path 6 (Fundamentals). Search:\n'
                '1. "economic calendar high impact events today"\n'
                '2. "forex market sentiment today"\n'
                'Apply news as ±15 pts max adjustment to confidence score. '
                'Output the final updated TRADE_CARD with adjusted confidence.'
            )
            msgs_p2 = msgs + [
                {'role': 'assistant', 'content': phase1_text},
                {'role': 'user',      'content': gate_msg}
            ]

            news_text = ''
            for _attempt in range(4):
                resp2 = client.messages.create(
                    model      = 'claude-sonnet-4-6',
                    max_tokens = 2000,
                    system     = system,
                    tools      = [{'type': 'web_search_20250305', 'name': 'web_search'}],
                    messages   = msgs_p2
                )

                for block in resp2.content:
                    if hasattr(block, 'text') and block.text:
                        news_text += block.text

                if news_text.strip():
                    break

                if resp2.stop_reason == 'tool_use':
                    serialized = []
                    for block in resp2.content:
                        if hasattr(block, 'type'):
                            if block.type == 'text':
                                serialized.append({'type': 'text', 'text': block.text})
                            elif block.type == 'tool_use':
                                serialized.append({
                                    'type':  'tool_use',
                                    'id':    block.id,
                                    'name':  block.name,
                                    'input': block.input if hasattr(block, 'input') else {}
                                })
                    msgs_p2.append({'role': 'assistant', 'content': serialized})
                    tool_results = []
                    for block in resp2.content:
                        if hasattr(block, 'type') and block.type == 'tool_use':
                            tool_results.append({
                                'type':        'tool_result',
                                'tool_use_id': block.id,
                                'content':     'Search completed. Apply news context as Path 6 adjustment (±15 pts max). Output final trade card with updated confidence.'
                            })
                    msgs_p2.append({'role': 'user', 'content': tool_results})
                else:
                    break

            if news_text.strip():
                final_text = phase1_text + '\n\n---\n**PATH 6 — NEWS GATE:**\n' + news_text

        return jsonify({'content': [{'type': 'text', 'text': final_text or 'No response generated.'}]})

    except Exception as e:
        error_msg = str(e)
        print(f'[Sage Chat ERROR] {error_msg}')
        return jsonify({'error': error_msg}), 500

@app.route('/health')
def health():
    return jsonify({'status':'ok','service':'sage-of-six-paths','time':datetime.utcnow().isoformat()})

@app.route('/setup-admin')
def setup_admin():
    if request.args.get('key','') != 'sage6paths2024admin':
        return jsonify({'error':'Invalid key'}), 403
    user = User.query.filter_by(email='jonel.michelfx@gmail.com').first()
    if not user:
        return jsonify({'error':'Account not found. Please sign up first at /login'}), 404
    user.plan = 'unleashed'
    db.session.commit()
    return jsonify({'success':True,'message':f'Account {user.email} upgraded to Six Paths Unleashed.',
                    'username':user.username,'plan':user.plan})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT',5000)))
