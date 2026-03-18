import os, json, time, uuid, threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import requests as http_requests

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sage-secret-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///sage.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login_page'

TWELVE_DATA_KEY = os.environ.get('TWELVE_DATA_API_KEY', '')

# ── User Model ─────────────────────────────────────────────
class User(UserMixin, db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    username     = db.Column(db.String(80), unique=True, nullable=False)
    email        = db.Column(db.String(120), unique=True, nullable=False)
    password_hash= db.Column(db.String(256), nullable=False)
    plan         = db.Column(db.String(20), default='student')
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

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
    return redirect(url_for('login_page'))

@app.route('/pricing')
def pricing_page():
    return render_template('pricing.html')

@app.route('/sage-mode')
@login_required
def sage_page():
    return render_template('sage.html')

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

    # Try TwelveData
    if TWELVE_DATA_KEY:
        try:
            td_sym = TD_SYM_MAP.get(pair, pair)
            td_interval_map = {'15m':'15min','1h':'1h','4h':'4h','1d':'1day'}
            td_interval = td_interval_map.get(interval, '1h')
            outputsize = {'15m':96,'1h':72,'4h':60,'1d':50}.get(interval, 72)
            r = http_requests.get(
                f'https://api.twelvedata.com/time_series',
                params={'symbol':td_sym,'interval':td_interval,'outputsize':outputsize,'apikey':TWELVE_DATA_KEY},
                timeout=10
            )
            d = r.json()
            if 'values' in d:
                candles = [{'time':v['datetime'],'open':float(v['open']),'high':float(v['high']),
                            'low':float(v['low']),'close':float(v['close']),'volume':int(float(v.get('volume',0)))}
                           for v in reversed(d['values'])]
                _candle_cache[key] = {'data':candles,'ts':time.time()}
                return candles
        except Exception as e:
            print(f'[TwelveData] {pair} {interval}: {e}')

    # Fallback: yfinance
    try:
        import yfinance as yf
        sym = YF_MAP.get(pair, pair.replace('/','') + '=X')
        period_map = {'15m':'5d','1h':'1mo','4h':'3mo','1d':'6mo'}
        df = yf.Ticker(sym).history(interval=interval, period=period_map.get(interval,'1mo'))
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
    d = request.get_json() or {}
    symbol = d.get('symbol', 'EURUSD=X')
    interval = d.get('interval', '1h')
    pair_display = TD_SYM_MAP.get(symbol, symbol)
    candles = get_candles(pair_display or symbol, interval)
    return jsonify({'candles': candles, 'symbol': symbol, 'interval': interval})

# ── SAGE AI JOB QUEUE ──────────────────────────────────────
_sage_jobs = {}

@app.route('/api/sage-start', methods=['POST'])
@login_required
def api_sage_start():
    d = request.get_json() or {}
    job_id = str(uuid.uuid4())[:8]
    _sage_jobs[job_id] = {'status': 'pending', 'result': None}
    def run():
        try:
            _sage_jobs[job_id] = {'status': 'done', 'result': d.get('prompt', '')}
        except Exception as e:
            _sage_jobs[job_id] = {'status': 'error', 'error': str(e)}
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/sage-poll/<job_id>', methods=['GET'])
@login_required
def api_sage_poll(job_id):
    job = _sage_jobs.get(job_id, {'status': 'not_found'})
    return jsonify(job)

# ── SCANNER ────────────────────────────────────────────────
_scanner_jobs = {}

@app.route('/api/sage-scanner', methods=['POST'])
@login_required
def api_sage_scanner():
    d = request.get_json() or {}
    job_id = str(uuid.uuid4())[:8]
    _scanner_jobs[job_id] = {'status': 'running', 'results': []}

    def scan():
        pairs = d.get('pairs', ['EUR/USD','GBP/USD','USD/JPY','GBP/JPY','AUD/USD'])
        results = []
        for pair in pairs:
            try:
                candles = get_candles(pair, '1h')
                if not candles or len(candles) < 20:
                    continue
                closes = [c['close'] for c in candles]
                price  = closes[-1]
                chg    = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) > 1 else 0
                # Simple EMA
                def ema(data, p):
                    k = 2/(p+1); e = data[0]
                    for v in data[1:]: e = v*k + e*(1-k)
                    return e
                ema8  = ema(closes, 8)
                ema21 = ema(closes, 21)
                trend = 'BULLISH' if ema8 > ema21 else 'BEARISH'
                results.append({'pair':pair,'price':price,'change':chg,'trend':trend,
                                 'ema8':round(ema8,5),'ema21':round(ema21,5)})
            except Exception as e:
                print(f'[Scanner] {pair}: {e}')
        _scanner_jobs[job_id] = {'status':'done','results':results}

    threading.Thread(target=scan, daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/sage-scanner-poll/<job_id>', methods=['GET'])
@login_required
def api_sage_scanner_poll(job_id):
    job = _scanner_jobs.get(job_id, {'status':'not_found','results':[]})
    return jsonify(job)

# ── HEALTH ─────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status':'ok','service':'sage-of-six-paths','time':datetime.utcnow().isoformat()})

@app.route('/setup-admin')
def setup_admin():
    """One-time admin setup — promotes jonel.michelfx@gmail.com to unleashed plan"""
    secret = request.args.get('key','')
    if secret != 'sage6paths2024admin':
        return jsonify({'error':'Invalid key'}), 403
    user = User.query.filter_by(email='jonel.michelfx@gmail.com').first()
    if not user:
        return jsonify({'error':'Account not found. Please sign up first at /login'}), 404
    user.plan = 'unleashed'
    db.session.commit()
    return jsonify({
        'success': True,
        'message': f'Account {user.email} upgraded to Six Paths Unleashed. You are now admin.',
        'username': user.username,
        'plan': user.plan
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
