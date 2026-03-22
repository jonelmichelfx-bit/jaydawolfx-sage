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
    try:
        return render_template('landing.html')
    except Exception:
        return redirect(url_for('login_page'))

@app.route('/pricing')
def pricing_page():
    return render_template('pricing.html', stripe_pk=STRIPE_PK)

@app.route('/sage-mode')
@login_required
def sage_page():
    return render_template('sage_mode.html')

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
        sym = YF_MAP.get(pair, pair.replace('/','') + '=X')
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
1. ALWAYS use web_search to check live price + economic calendar before ANY analysis. Never use memory for prices.
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
 COMMUNICATION & TEACHING RULES
═══════════════════════════════════════════════════════
- Never address users by first name. This platform serves many traders. Use "Student" sparingly — only when opening a lesson or correction, not on every sentence.
- Your natural voice is warm, wise, and conversational — like a master trader sitting next to them. Teach naturally. Don't lecture robotically.
- Use phrases like "What the market is showing us here..." or "The 6 paths confirm..." or "Here is why this level matters..." — let the wisdom come through in HOW you explain, not by prefixing every line with "Student".
- Every response should feel like a conversation with someone who genuinely wants them to understand and succeed. Encourage. Explain. Make it click.
- Explain the WHY behind every signal. Never just give a number — always give the reason behind it.

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
        'EURUSD=X':'EUR/USD','GBPUSD=X':'GBP/USD','USDJPY=X':'USD/JPY',
        'GBPJPY=X':'GBP/JPY','AUDUSD=X':'AUD/USD','USDCAD=X':'USD/CAD',
        'USDCHF=X':'USD/CHF','EURJPY=X':'EUR/JPY','EURGBP=X':'EUR/GBP',
        'NZDUSD=X':'NZD/USD','GC=F':'XAU/USD','BTC-USD':'BTC/USD',
        'NVDA':'NVDA','AAPL':'AAPL','TSLA':'TSLA','MSFT':'MSFT',
        'AMZN':'AMZN','META':'META','GOOGL':'GOOGL','SPY':'SPY','QQQ':'QQQ',
    }
    pair = TD_SYM_MAP2.get(symbol, symbol)
    candles = get_candles(pair, interval)
    if not candles or len(candles) < 20:
        return jsonify({'error': f'No data for {pair}'}), 404

    closes = [c['close'] for c in candles]
    highs  = [c['high']  for c in candles]
    lows   = [c['low']   for c in candles]
    price  = closes[-1]
    first  = closes[0]
    chg    = round((price - first) / first * 100, 3)
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

    # Approximate order blocks (last strong candle before big move)
    bull_ob = bear_ob = None
    for i in range(len(candles)-2, max(0, len(candles)-30), -1):
        c, n = candles[i], candles[i+1]
        body = abs(c['close'] - c['open'])
        next_body = abs(n['close'] - n['open'])
        if c['close'] < c['open'] and n['close'] > n['open'] and next_body > body * 1.5:
            bull_ob = {'high': round(c['high'],5), 'low': round(c['low'],5)}
            break
    for i in range(len(candles)-2, max(0, len(candles)-30), -1):
        c, n = candles[i], candles[i+1]
        body = abs(c['close'] - c['open'])
        next_body = abs(n['close'] - n['open'])
        if c['close'] > c['open'] and n['close'] < n['open'] and next_body > body * 1.5:
            bear_ob = {'high': round(c['high'],5), 'low': round(c['low'],5)}
            break

    # Fair Value Gaps (3-candle imbalance)
    fvgs = []
    for i in range(1, min(len(candles)-1, 30)):
        c1, c2, c3 = candles[-i-1], candles[-i], candles[-i+1] if i > 1 else candles[-1]
        # Bullish FVG: c1 high < c3 low
        if c1['high'] < c3['low']:
            fvgs.append({'type':'BULL','high':round(c3['low'],5),'low':round(c1['high'],5)})
        # Bearish FVG: c1 low > c3 high
        if c1['low'] > c3['high']:
            fvgs.append({'type':'BEAR','high':round(c1['low'],5),'low':round(c3['high'],5)})
        if len(fvgs) >= 2: break

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
        'bull_ob': bull_ob, 'bear_ob': bear_ob,
        'fvgs': fvgs,
        'stack': stack, 'bias': bias, 'regime': regime,
        'above_200': above_200,
        'session': session,
        'candle_count': len(candles),
    })


# ── SAGE CHAT PROXY ─────────────────────────────────────────
@app.route('/api/sage-chat', methods=['POST'])
@login_required
def api_sage_chat():
    """Server-side chat proxy — keeps Anthropic key off the browser."""
    try:
        import anthropic as _anth
    except ImportError:
        return jsonify({'error': 'anthropic package not installed'}), 500
    d        = request.get_json() or {}
    messages = d.get('messages', [])
    api_key  = os.environ.get('ANTHROPIC_API_KEY', d.get('key', ''))
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY not set on server'}), 500
    if not messages:
        return jsonify({'error': 'messages required'}), 400
    try:
        client = _anth.Anthropic(api_key=api_key)
        # Agentic loop for web_search tool_use
        msgs = messages[:]
        final_text = ''
        for _attempt in range(4):
            resp = client.messages.create(
                model='claude-sonnet-4-20250514',
                max_tokens=2000,
                system=SAGE_SYSTEM,
                tools=[{'type':'web_search_20250305','name':'web_search'}],
                messages=msgs
            )
            for block in resp.content:
                if hasattr(block,'text') and block.text:
                    final_text += block.text
            if final_text.strip(): break
            if resp.stop_reason == 'tool_use':
                msgs.append({'role':'assistant','content':resp.content})
                tool_results = []
                for block in resp.content:
                    if block.type == 'tool_use':
                        tool_results.append({
                            'type':'tool_result',
                            'tool_use_id':block.id,
                            'content':'Search completed. Now provide your full Sage analysis.'
                        })
                msgs.append({'role':'user','content':tool_results})
            else: break
        return jsonify({'content':[{'type':'text','text':final_text or 'No response.'}]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
