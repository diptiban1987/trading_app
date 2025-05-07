import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, send_file, g, flash, jsonify, abort
from datetime import datetime, timedelta
import random
import csv
import io
import requests
from werkzeug.security import generate_password_hash, check_password_hash
import math
from scipy.stats import norm
import time
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors

app = Flask(__name__)
app.secret_key = "kishan_secret"
DATABASE = os.path.join(app.root_path, 'kishanx.db')

# --- Database helpers ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            time TEXT,
            pair TEXT,
            direction TEXT,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        ''')
        db.commit()

with app.app_context():
    init_db()

# --- User helpers ---
def get_user_by_username(username):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

def get_user_by_id(user_id):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

def create_user(username, password):
    db = get_db()
    hashed = generate_password_hash(password)
    db.execute('INSERT INTO users (username, password, registered_at) VALUES (?, ?, ?)',
               (username, hashed, datetime.now().isoformat()))
    db.commit()

def update_last_login(user_id):
    db = get_db()
    db.execute('UPDATE users SET last_login = ? WHERE id = ?', (datetime.now().isoformat(), user_id))
    db.commit()

def verify_user(username, password):
    user = get_user_by_username(username)
    if user and check_password_hash(user['password'], password):
        return user
    return None

# --- Signal helpers ---
def save_signal(user_id, time, pair, direction):
    db = get_db()
    db.execute('INSERT INTO signals (user_id, time, pair, direction, created_at) VALUES (?, ?, ?, ?, ?)',
               (user_id, time, pair, direction, datetime.now().isoformat()))
    db.commit()

def get_signals_for_user(user_id, limit=20):
    db = get_db()
    return db.execute('SELECT * FROM signals WHERE user_id = ? ORDER BY created_at DESC LIMIT ?', (user_id, limit)).fetchall()

def get_signal_stats(user_id):
    db = get_db()
    total = db.execute('SELECT COUNT(*) FROM signals WHERE user_id = ?', (user_id,)).fetchone()[0]
    by_pair = db.execute('SELECT pair, COUNT(*) as count FROM signals WHERE user_id = ? GROUP BY pair', (user_id,)).fetchall()
    by_direction = db.execute('SELECT direction, COUNT(*) as count FROM signals WHERE user_id = ? GROUP BY direction', (user_id,)).fetchall()
    return total, by_pair, by_direction

# --- App logic ---
pairs = ["EURAUD", "USDCHF", "USDBRL", "AUDUSD", "GBPCAD", "EURCAD", "NZDUSD", "USDPKR", "EURUSD", "USDCAD", "AUDCHF", "GBPUSD", "EURGBP"]
brokers = ["Quotex", "Pocket Option", "Binolla", "IQ Option", "Bullex", "Exnova"]
API_KEY = "35BDZ47V6D5T4B8G"

price_cache = {}

broker_payouts = {
    "Quotex": 0.85,
    "Pocket Option": 0.80,
    "Binolla": 0.78,
    "IQ Option": 0.82,
    "Bullex": 0.75,
    "Exnova": 0.77
}

def get_cached_realtime_forex(pair, api_key, cache_duration=60):
    now = time.time()
    if pair in price_cache:
        price, timestamp = price_cache[pair]
        if now - timestamp < cache_duration:
            return price
    price = get_realtime_forex(pair, api_key)
    price_cache[pair] = (price, now)
    return price

def get_realtime_forex(pair, api_key):
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    url = f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency={from_symbol}&to_currency={to_symbol}&apikey={api_key}"
    response = requests.get(url)
    data = response.json()
    try:
        rate = data["Realtime Currency Exchange Rate"]["5. Exchange Rate"]
        return float(rate)
    except Exception:
        # Fallback for demo/testing
        return round(random.uniform(1.0, 2.0), 5)

def black_scholes_call_put(S, K, T, r, sigma, option_type="call"):
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "call":
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    return price

DEMO_UNLOCK_PASSWORD = 'Indiandemo2021'
DEMO_TIMEOUT_MINUTES = 30

@app.before_request
def demo_lockout():
    allowed_routes = {'login', 'register', 'static', 'lock', 'unlock'}
    if request.endpoint in allowed_routes or request.endpoint is None:
        return
    if 'demo_start_time' not in session:
        session['demo_start_time'] = datetime.now().isoformat()
    start_time = datetime.fromisoformat(session['demo_start_time'])
    if (datetime.now() - start_time).total_seconds() > DEMO_TIMEOUT_MINUTES * 60:
        session['locked'] = True
        if request.endpoint not in {'lock', 'unlock'}:
            return redirect(url_for('lock'))
    else:
        session['locked'] = False

@app.route('/lock', methods=['GET'])
def lock():
    return render_template('lock.html')

@app.route('/unlock', methods=['POST'])
def unlock():
    password = request.form.get('password')
    if password == DEMO_UNLOCK_PASSWORD:
        session['demo_start_time'] = datetime.now().isoformat()
        session['locked'] = False
        return redirect(url_for('dashboard'))
    else:
        flash('Incorrect password. Please try again.', 'error')
        return render_template('lock.html')

@app.route('/get_demo_time')
def get_demo_time():
    demo_timeout = DEMO_TIMEOUT_MINUTES
    start_time = session.get('demo_start_time')
    if not start_time:
        # fallback: reset timer
        session['demo_start_time'] = datetime.now().isoformat()
        start_time = session['demo_start_time']
    start_time = datetime.fromisoformat(start_time)
    elapsed = (datetime.now() - start_time).total_seconds()
    remaining = max(0, int(demo_timeout * 60 - elapsed))
    minutes = remaining // 60
    seconds = remaining % 60
    time_left = f"{minutes:02d}:{seconds:02d}"
    return jsonify({'time_left': time_left})

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if get_user_by_username(username):
            flash("Username already exists.", "error")
            return render_template("register.html")
        create_user(username, password)
        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = verify_user(username, password)
        if user:
            session["user_id"] = user["id"]
            update_last_login(user["id"])
            return redirect(url_for("dashboard"))
        else:
            return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user = get_user_by_id(session["user_id"])
    if request.method == "POST":
        new_password = request.form["new_password"]
        if new_password:
            db = get_db()
            db.execute('UPDATE users SET password = ? WHERE id = ?', (generate_password_hash(new_password), user["id"]))
            db.commit()
            flash("Password updated successfully.", "success")
    return render_template("profile.html", user=user)

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user = get_user_by_id(session["user_id"])
    signals = get_signals_for_user(user["id"], limit=10)
    total, by_pair, by_direction = get_signal_stats(user["id"])
    pair_labels = [p['pair'] for p in by_pair]
    pair_counts = [p['count'] for p in by_pair]
    direction_labels = [d['direction'] for d in by_direction]
    direction_counts = [d['count'] for d in by_direction]
    return render_template(
        "dashboard.html",
        user=user,
        signals=signals,
        total=total,
        pair_labels=pair_labels,
        pair_counts=pair_counts,
        direction_labels=direction_labels,
        direction_counts=direction_counts
    )

@app.route("/", methods=["GET", "POST"])
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    current_rate = None
    selected_pair = pairs[0]
    selected_broker = brokers[0]
    payout = broker_payouts[selected_broker]
    call_price = None
    put_price = None
    volatility = 0.2
    expiry = 1/365
    risk_free_rate = 0.01
    if request.method == "POST":
        pair = request.form["pair"]
        broker = request.form["broker"]
        signal_type = request.form["signal_type"].upper()
        start_hour = request.form["start_hour"]
        start_minute = request.form["start_minute"]
        end_hour = request.form["end_hour"]
        end_minute = request.form["end_minute"]
        start_str = f"{start_hour}:{start_minute}"
        end_str = f"{end_hour}:{end_minute}"
        selected_pair = pair
        selected_broker = broker
        payout = broker_payouts.get(broker, 0.75)
        current_rate = get_cached_realtime_forex(pair, API_KEY)
        if current_rate:
            S = current_rate
            K = S
            T = expiry
            r = risk_free_rate
            sigma = volatility
            call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
            put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
        try:
            start = datetime.strptime(start_str, "%H:%M")
            end = datetime.strptime(end_str, "%H:%M")
            if start >= end:
                return render_template("index.html", error="Start time must be before end time.", pairs=pairs, brokers=brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

            signals = []
            current = start
            while current < end:
                direction = random.choice(["CALL", "PUT"]) if signal_type == "BOTH" else signal_type
                signals.append({
                    "time": current.strftime("%H:%M"),
                    "pair": pair,
                    "direction": direction
                })
                save_signal(session["user_id"], current.strftime("%H:%M"), pair, direction)
                current += timedelta(minutes=random.randint(1, 15))

            session["signals"] = signals
            return render_template("results.html", signals=signals, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)
        except ValueError:
            return render_template("index.html", error="Invalid time format.", pairs=pairs, brokers=brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

    # For GET requests, show the rate for the default pair and broker
    current_rate = get_cached_realtime_forex(selected_pair, API_KEY)
    if current_rate:
        S = current_rate
        K = S
        T = expiry
        r = risk_free_rate
        sigma = volatility
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
    return render_template("index.html", pairs=pairs, brokers=brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

@app.route("/download")
def download():
    if "signals" not in session:
        return redirect(url_for("index"))

    signals = session["signals"]
    from io import BytesIO
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 40, "KishanX Signals Report")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 60, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    # Table header
    table_data = [["Time", "Pair", "Direction", "Call Price", "Put Price"]]
    for s in signals:
        S = get_cached_realtime_forex(s["pair"], API_KEY)
        K = S
        T = 1/365
        r = 0.01
        sigma = 0.2
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call") if S else "N/A"
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put") if S else "N/A"
        table_data.append([s["time"], s["pair"], s["direction"], f"{call_price:.6f}" if S else "N/A", f"{put_price:.6f}" if S else "N/A"])
    # Draw table
    x = 40
    y = height - 100
    row_height = 18
    col_widths = [60, 70, 70, 100, 100]
    c.setFont("Helvetica-Bold", 11)
    for col, header in enumerate(table_data[0]):
        c.drawString(x + sum(col_widths[:col]), y, header)
    c.setFont("Helvetica", 10)
    y -= row_height
    for row in table_data[1:]:
        for col, cell in enumerate(row):
            c.drawString(x + sum(col_widths[:col]), y, str(cell))
        y -= row_height
        if y < 60:
            c.showPage()
            y = height - 60
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="kishan_signals.pdf")

@app.route("/api/price/<pair>")
def api_price(pair):
    current_rate = get_cached_realtime_forex(pair, API_KEY)
    volatility = 0.2
    expiry = 1/365
    risk_free_rate = 0.01
    call_price = put_price = None
    if current_rate:
        S = current_rate
        K = S
        T = expiry
        r = risk_free_rate
        sigma = volatility
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
    return jsonify({
        "rate": current_rate,
        "call_price": call_price,
        "put_price": put_price,
        "volatility": volatility,
        "expiry": expiry,
        "risk_free_rate": risk_free_rate
    })

# --- Indian Market Data ---
indian_pairs = [
    "BANKNIFTY", "NIFTY50", "NSEBANK", "NSEIT", "NSEINFRA", "NSEPHARMA", "NSEFMCG", "NSEMETAL", "NSEENERGY", "NSEAUTO"
]
indian_brokers = ["Zerodha", "Upstox", "Angel One", "Groww", "ICICI Direct", "HDFC Securities"]

@app.route("/indian", methods=["GET", "POST"])
def indian_market():
    if "user_id" not in session:
        return redirect(url_for("login"))

    current_rate = None
    selected_pair = indian_pairs[0]
    selected_broker = indian_brokers[0]
    payout = 0.75  # Indian brokers may not have payout, but keep for UI consistency
    call_price = None
    put_price = None
    volatility = 0.2
    expiry = 1/365
    risk_free_rate = 0.01
    if request.method == "POST":
        pair = request.form["pair"]
        broker = request.form["broker"]
        signal_type = request.form["signal_type"].upper()
        start_hour = request.form["start_hour"]
        start_minute = request.form["start_minute"]
        end_hour = request.form["end_hour"]
        end_minute = request.form["end_minute"]
        start_str = f"{start_hour}:{start_minute}"
        end_str = f"{end_hour}:{end_minute}"
        selected_pair = pair
        selected_broker = broker
        # For demo, use get_cached_realtime_forex with a fallback for Indian symbols
        try:
            current_rate = get_cached_realtime_forex(pair, API_KEY)
        except Exception:
            current_rate = round(random.uniform(10000, 50000), 2)
        if current_rate:
            S = current_rate
            K = S
            T = expiry
            r = risk_free_rate
            sigma = volatility
            call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
            put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
        try:
            start = datetime.strptime(start_str, "%H:%M")
            end = datetime.strptime(end_str, "%H:%M")
            if start >= end:
                return render_template("indian.html", error="Start time must be before end time.", pairs=indian_pairs, brokers=indian_brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

            signals = []
            current = start
            while current < end:
                direction = random.choice(["CALL", "PUT"]) if signal_type == "BOTH" else signal_type
                signals.append({
                    "time": current.strftime("%H:%M"),
                    "pair": pair,
                    "direction": direction
                })
                save_signal(session["user_id"], current.strftime("%H:%M"), pair, direction)
                current += timedelta(minutes=random.randint(1, 15))

            session["indian_signals"] = signals
            return render_template("indian.html", signals=signals, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate, pairs=indian_pairs, brokers=indian_brokers)
        except ValueError:
            return render_template("indian.html", error="Invalid time format.", pairs=indian_pairs, brokers=indian_brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

    # For GET requests, show the rate for the default pair and broker
    try:
        current_rate = get_cached_realtime_forex(selected_pair, API_KEY)
    except Exception:
        current_rate = round(random.uniform(10000, 50000), 2)
    if current_rate:
        S = current_rate
        K = S
        T = expiry
        r = risk_free_rate
        sigma = volatility
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
    signals = session.get("indian_signals", [])
    return render_template("indian.html", pairs=indian_pairs, brokers=indian_brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate, signals=signals)

# --- OTC Market Data ---
otc_pairs = [
    "EURUSD_OTC", "GBPUSD_OTC", "USDJPY_OTC", "AUDUSD_OTC", "USDCHF_OTC", "USDCAD_OTC", "EURJPY_OTC", "EURGBP_OTC"
]
otc_brokers = ["Quotex", "Pocket Option", "Binolla", "IQ Option", "Bullex", "Exnova"]

@app.route("/otc", methods=["GET", "POST"])
def otc_market():
    if "user_id" not in session:
        return redirect(url_for("login"))

    current_rate = None
    selected_pair = otc_pairs[0]
    selected_broker = otc_brokers[0]
    payout = broker_payouts[selected_broker]
    call_price = None
    put_price = None
    volatility = 0.2
    expiry = 1/365
    risk_free_rate = 0.01
    if request.method == "POST":
        pair = request.form["pair"]
        broker = request.form["broker"]
        signal_type = request.form["signal_type"].upper()
        start_hour = request.form["start_hour"]
        start_minute = request.form["start_minute"]
        end_hour = request.form["end_hour"]
        end_minute = request.form["end_minute"]
        start_str = f"{start_hour}:{start_minute}"
        end_str = f"{end_hour}:{end_minute}"
        selected_pair = pair
        selected_broker = broker
        payout = broker_payouts.get(broker, 0.75)
        current_rate = get_cached_realtime_forex(pair.replace('_OTC',''), API_KEY)
        if current_rate:
            S = current_rate
            K = S
            T = expiry
            r = risk_free_rate
            sigma = volatility
            call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
            put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
        try:
            start = datetime.strptime(start_str, "%H:%M")
            end = datetime.strptime(end_str, "%H:%M")
            if start >= end:
                return render_template("otc.html", error="Start time must be before end time.", pairs=otc_pairs, brokers=otc_brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

            signals = []
            current = start
            while current < end:
                direction = random.choice(["CALL", "PUT"]) if signal_type == "BOTH" else signal_type
                signals.append({
                    "time": current.strftime("%H:%M"),
                    "pair": pair,
                    "direction": direction
                })
                save_signal(session["user_id"], current.strftime("%H:%M"), pair, direction)
                current += timedelta(minutes=random.randint(1, 15))

            session["otc_signals"] = signals
            return render_template("otc.html", signals=signals, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate, pairs=otc_pairs, brokers=otc_brokers)
        except ValueError:
            return render_template("otc.html", error="Invalid time format.", pairs=otc_pairs, brokers=otc_brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

    # For GET requests, show the rate for the default pair and broker
    current_rate = get_cached_realtime_forex(selected_pair.replace('_OTC',''), API_KEY)
    if current_rate:
        S = current_rate
        K = S
        T = expiry
        r = risk_free_rate
        sigma = volatility
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
    signals = session.get("otc_signals", [])
    return render_template("otc.html", pairs=otc_pairs, brokers=otc_brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate, signals=signals)

@app.route("/download_otc")
def download_otc():
    if "otc_signals" not in session:
        return redirect(url_for("otc_market"))
    signals = session["otc_signals"]
    from io import BytesIO
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 40, "KishanX OTC Signals Report")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 60, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    table_data = [["Time", "Pair", "Direction", "Call Price", "Put Price"]]
    for s in signals:
        S = get_cached_realtime_forex(s["pair"].replace('_OTC',''), API_KEY)
        K = S
        T = 1/365
        r = 0.01
        sigma = 0.2
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call") if S else "N/A"
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put") if S else "N/A"
        table_data.append([s["time"], s["pair"], s["direction"], f"{call_price:.6f}" if S else "N/A", f"{put_price:.6f}" if S else "N/A"])
    x = 40
    y = height - 100
    row_height = 18
    col_widths = [60, 70, 70, 100, 100]
    c.setFont("Helvetica-Bold", 11)
    for col, header in enumerate(table_data[0]):
        c.drawString(x + sum(col_widths[:col]), y, header)
    c.setFont("Helvetica", 10)
    y -= row_height
    for row in table_data[1:]:
        for col, cell in enumerate(row):
            c.drawString(x + sum(col_widths[:col]), y, str(cell))
        y -= row_height
        if y < 60:
            c.showPage()
            y = height - 60
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="kishan_otc_signals.pdf")

@app.route("/download_indian")
def download_indian():
    if "indian_signals" not in session:
        return redirect(url_for("indian_market"))
    signals = session["indian_signals"]
    from io import BytesIO
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 40, "KishanX Indian Signals Report")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 60, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    table_data = [["Time", "Pair", "Direction", "Call Price", "Put Price"]]
    for s in signals:
        try:
            S = get_cached_realtime_forex(s["pair"], API_KEY)
        except Exception:
            S = round(random.uniform(10000, 50000), 2)
        K = S
        T = 1/365
        r = 0.01
        sigma = 0.2
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call") if S else "N/A"
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put") if S else "N/A"
        table_data.append([s["time"], s["pair"], s["direction"], f"{call_price:.6f}" if S else "N/A", f"{put_price:.6f}" if S else "N/A"])
    x = 40
    y = height - 100
    row_height = 18
    col_widths = [60, 70, 70, 100, 100]
    c.setFont("Helvetica-Bold", 11)
    for col, header in enumerate(table_data[0]):
        c.drawString(x + sum(col_widths[:col]), y, header)
    c.setFont("Helvetica", 10)
    y -= row_height
    for row in table_data[1:]:
        for col, cell in enumerate(row):
            c.drawString(x + sum(col_widths[:col]), y, str(cell))
        y -= row_height
        if y < 60:
            c.showPage()
            y = height - 60
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="kishan_indian_signals.pdf")

if __name__ == "__main__":
    app.run(debug=True)
