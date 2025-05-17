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
import pandas as pd
import numpy as np
import yfinance as yf
from flask_socketio import SocketIO, emit
from otc_data import OTCDataHandler
from trading_system import TradingSystem
from risk_manager import RiskManager
from auto_trader import AutoTrader
from websocket_handler import WebSocketHandler
from config import (
    ALPHA_VANTAGE_API_KEY,
    API_TIMEOUT,
    CACHE_DURATION,
    PREMIUM_API_ENABLED,
    PREMIUM_API_CALLS_PER_MINUTE
)
import logging
from typing import Dict
import json
import threading

app = Flask(__name__)
app.secret_key = "kishan_secret"
socketio = SocketIO(app)
DATABASE = os.path.join(app.root_path, 'kishanx.db')

# Initialize components
trading_system = TradingSystem()
risk_manager = RiskManager()
auto_trader = AutoTrader(trading_system, risk_manager)
websocket_handler = WebSocketHandler(socketio)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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
            last_login TEXT,
            balance REAL DEFAULT 10000.0,
            is_premium BOOLEAN DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS user_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(user_id, symbol)
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            quantity REAL NOT NULL,
            status TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            profit_loss REAL,
            stop_loss REAL,
            take_profit REAL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            average_price REAL NOT NULL,
            last_updated TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(user_id, symbol)
        );

        CREATE TABLE IF NOT EXISTS market_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            volume REAL,
            timestamp TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS risk_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            max_position_size REAL DEFAULT 0.02,
            max_daily_loss REAL DEFAULT 0.05,
            max_drawdown REAL DEFAULT 0.15,
            stop_loss_pct REAL DEFAULT 0.02,
            take_profit_pct REAL DEFAULT 0.04,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS portfolio_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            portfolio_value REAL NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        ''')
        db.commit()

# Initialize database on startup
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

# Initialize price cache
price_cache = {}

# Symbol mapping for Indian markets
symbol_map = {
    # Major Indices
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "NSEBANK": "^NSEBANK",
    "NSEIT": "^CNXIT",
    "NSEINFRA": "^CNXINFRA",
    "NSEPHARMA": "^CNXPHARMA",
    "NSEFMCG": "^CNXFMCG",
    "NSEMETAL": "^CNXMETAL",
    "NSEENERGY": "^CNXENERGY",
    "NSEAUTO": "^CNXAUTO",
    # Additional Indices
    "NIFTYMIDCAP": "^NSEI_MIDCAP",
    "NIFTYSMALLCAP": "^NSEI_SMALLCAP",
    "NIFTYNEXT50": "^NSEI_NEXT50",
    "NIFTY100": "^NSEI_100",
    "NIFTY500": "^NSEI_500",
    # Sector Indices
    "NIFTYREALTY": "^NSEI_REALTY",
    "NIFTYPVTBANK": "^NSEI_PVTBANK",
    "NIFTYPSUBANK": "^NSEI_PSUBANK",
    "NIFTYFIN": "^NSEI_FIN",
    "NIFTYMEDIA": "^NSEI_MEDIA",
    # Popular Stocks
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "INFY": "INFY.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "HINDUNILVR": "HINDUNILVR.NS",
    "SBIN": "SBIN.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "BAJFINANCE": "BAJFINANCE.NS"
}

broker_payouts = {
    "Quotex": 0.85,
    "Pocket Option": 0.80,
    "Binolla": 0.78,
    "IQ Option": 0.82,
    "Bullex": 0.75,
    "Exnova": 0.77
}

def get_cached_realtime_forex(pair, api_key=ALPHA_VANTAGE_API_KEY, cache_duration=CACHE_DURATION, return_source=False):
    """
    Get cached real-time forex rate with improved error handling for premium API.
    
    Args:
        pair (str): Currency pair (e.g., 'EURUSD')
        api_key (str): Alpha Vantage API key
        cache_duration (int): Cache duration in seconds
    
    Returns:
        float: Current exchange rate or None if unavailable
    """
    cache_key = f"forex_{pair}"
    now = time.time()
    
    # Check cache
    if cache_key in price_cache:
        price, timestamp = price_cache[cache_key]
        if now - timestamp < cache_duration:
            logger.info(f"Using cached price for {pair}: {price} (cached {int(now - timestamp)} seconds ago)")
            if return_source:
                return price, 'cached'
            return price
        else:
            logger.info(f"Cache expired for {pair}, fetching new data...")
    
    try:
        price = get_realtime_forex(pair, api_key)
        if price is not None:
            price_cache[cache_key] = (price, now)
            logger.info(f"Updated cache for {pair} with new price: {price}")
            if return_source:
                return price, 'cached'
            return price
    except Exception as e:
        logger.error(f"Error getting forex rate for {pair}: {str(e)}")
        return None

def get_realtime_forex(pair, api_key=ALPHA_VANTAGE_API_KEY):
    """
    Get real-time forex rate with support for premium API features.
    
    Args:
        pair (str): Currency pair (e.g., 'EURUSD')
        api_key (str): Alpha Vantage API key
    
    Returns:
        float: Current exchange rate or None if unavailable
    """
    if not api_key or api_key == "YOUR_PREMIUM_API_KEY":
        logger.warning("Using fallback data - No valid API key provided")
        return None
        
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    
    # Try multiple data sources in order of preference
    data_sources = [
        lambda: _get_alpha_vantage_rate(pair, api_key),
        lambda: _get_exchange_rate_api_rate(pair),
        lambda: _get_fixer_io_rate(pair)
    ]
    
    for source in data_sources:
        try:
            rate = source()
            if rate is not None:
                return rate
        except Exception as e:
            logger.error(f"Error with data source: {str(e)}")
            continue
    
    return None

def _get_alpha_vantage_rate(pair, api_key):
    """Get rate from Alpha Vantage with premium API support."""
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    
    url = f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency={from_symbol}&to_currency={to_symbol}&apikey={api_key}"
    
    try:
        logger.info(f"Fetching rate for {pair} from Alpha Vantage")
        response = requests.get(url, timeout=API_TIMEOUT)
        data = response.json()
        
        if "Error Message" in data:
            logger.error(f"Alpha Vantage API Error: {data['Error Message']}")
            return None
            
        if "Note" in data:
            if "premium" in data["Note"].lower():
                if not PREMIUM_API_ENABLED:
                    logger.warning("Premium API features required. Set PREMIUM_API_ENABLED=True in config.py")
                return None
            elif "API call frequency" in data["Note"]:
                logger.warning(f"API rate limit reached: {data['Note']}")
                return None
            
        if "Realtime Currency Exchange Rate" in data:
            rate = float(data["Realtime Currency Exchange Rate"]["5. Exchange Rate"])
            logger.info(f"Successfully fetched rate for {pair}: {rate}")
            return rate
            
    except requests.exceptions.Timeout:
        logger.error(f"Timeout while fetching rate for {pair}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error while fetching rate for {pair}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error while fetching rate for {pair}: {str(e)}")
        return None
    
    return None

def _get_exchange_rate_api_rate(pair):
    """Get rate from ExchangeRate-API with error handling."""
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    url = f"https://open.er-api.com/v6/latest/{from_symbol}"
    
    try:
        logger.info(f"Fetching rate for {pair} from ExchangeRate-API")
        response = requests.get(url, timeout=API_TIMEOUT)
        data = response.json()
        
        if data.get("result") == "error":
            logger.error(f"ExchangeRate-API Error: {data.get('error-type', 'Unknown error')}")
            return None
            
        if data.get("rates") and to_symbol in data["rates"]:
            rate = float(data["rates"][to_symbol])
            logger.info(f"Successfully fetched rate for {pair}: {rate}")
            return rate
            
    except Exception as e:
        logger.error(f"Error fetching from ExchangeRate-API for {pair}: {str(e)}")
        return None
    
    return None

def _get_fixer_io_rate(pair):
    """Get rate from Fixer.io with error handling."""
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    url = f"http://data.fixer.io/api/latest?access_key=YOUR_FIXER_API_KEY&base={from_symbol}&symbols={to_symbol}"
    
    try:
        logger.info(f"Fetching rate for {pair} from Fixer.io")
        response = requests.get(url, timeout=API_TIMEOUT)
        data = response.json()
        
        if not data.get("success", False):
            logger.error(f"Fixer.io API Error: {data.get('error', {}).get('info', 'Unknown error')}")
            return None
            
        if data.get("rates") and to_symbol in data["rates"]:
            rate = float(data["rates"][to_symbol])
            logger.info(f"Successfully fetched rate for {pair}: {rate}")
            return rate
            
    except Exception as e:
        logger.error(f"Error fetching from Fixer.io for {pair}: {str(e)}")
        return None
    
    return None

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
            # Get the next page from the request args, default to dashboard
            next_page = request.args.get('next', url_for('dashboard'))
            return redirect(next_page)
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
        current_rate = get_cached_realtime_forex(pair)
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
    current_rate = get_cached_realtime_forex(selected_pair)
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
        S = get_cached_realtime_forex(s["pair"])
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
    """API endpoint to get current price and option prices"""
    try:
        data_source = None
        # Handle OTC pairs
        if pair.endswith('_OTC'):
            current_rate, data_source = otc_handler.get_realtime_price(pair, return_source=True)
        else:
            current_rate, data_source = get_cached_realtime_forex(pair, return_source=True), 'Alpha Vantage/Other'
        print(f"API price request for {pair}: {current_rate}")  # Debug print
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
            "risk_free_rate": risk_free_rate,
            "data_source": data_source
        })
    except Exception as e:
        print(f"Error in api_price: {str(e)}")  # Debug print
        return jsonify({
            "error": str(e)
        }), 500

# --- Indian Market Data ---
indian_pairs = [
    # Major Indices
    "NIFTY50", "BANKNIFTY", "NSEBANK", "NSEIT", "NSEINFRA", "NSEPHARMA", "NSEFMCG", "NSEMETAL", "NSEENERGY", "NSEAUTO",
    # Additional Indices
    "NIFTYMIDCAP", "NIFTYSMALLCAP", "NIFTYNEXT50", "NIFTY100", "NIFTY500",
    # Sector Indices
    "NIFTYREALTY", "NIFTYPVTBANK", "NIFTYPSUBANK", "NIFTYFIN", "NIFTYMEDIA",
    # Popular Stocks
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "SBIN", "BHARTIARTL", "KOTAKBANK", "BAJFINANCE"
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
            current_rate = get_cached_realtime_forex(pair)
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
        current_rate = get_cached_realtime_forex(selected_pair)
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
    # Major OTC Pairs
    "AUDCAD_OTC", "AUDCHF_OTC", "AUDHKD_OTC", "AUDJPY_OTC", "AUDMXN_OTC", "AUDNZD_OTC", "AUDSGD_OTC", "AUDUSD_OTC", "AUDZAR_OTC",
    "EURCAD_OTC", "EURCHF_OTC", "EURGBP_OTC", "EURHKD_OTC", "EURJPY_OTC", "EURMXN_OTC", "EURNZD_OTC", "EURSGD_OTC", "EURUSD_OTC", "EURZAR_OTC",
    "GBPCAD_OTC", "GBPCHF_OTC", "GBPHKD_OTC", "GBPJPY_OTC", "GBPMXN_OTC", "GBPNZD_OTC", "GBPSGD_OTC", "GBPUSD_OTC", "GBPZAR_OTC",
    "NZDCHF_OTC", "NZDUSD_OTC",
    "USDCAD_OTC", "USDCHF_OTC", "USDHKD_OTC", "USDJPY_OTC", "USDMXN_OTC", "USDNZD_OTC", "USDSGD_OTC", "USDZAR_OTC",
    # Additional Exotic OTC Pairs
    "USDARS_OTC", "USDBRL_OTC", "USDPKR_OTC"
]
otc_brokers = ["Quotex", "Pocket Option", "Binolla", "IQ Option", "Bullex", "Exnova"]

# Initialize OTC data handler
otc_handler = OTCDataHandler(ALPHA_VANTAGE_API_KEY)

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

        # Get real-time price using OTC handler
        current_rate = otc_handler.get_realtime_price(pair)
        print(f"Current rate for {pair}: {current_rate}")  # Debug print
        
        if current_rate:
            S = current_rate
            K = S
            T = expiry
            r = risk_free_rate
            sigma = volatility
            call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
            put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")

            # Get historical data for technical analysis
            historical_data = otc_handler.get_historical_data(pair, interval='1min')
            if historical_data is not None:
                indicators = otc_handler.calculate_technical_indicators(historical_data)
                # Store indicators in session for signal generation
                session['otc_indicators'] = indicators

        try:
            start = datetime.strptime(start_str, "%H:%M")
            end = datetime.strptime(end_str, "%H:%M")
            if start >= end:
                return render_template("otc.html", 
                    error="Start time must be before end time.",
                    pairs=otc_pairs,
                    brokers=otc_brokers,
                    current_rate=current_rate,
                    selected_pair=selected_pair,
                    selected_broker=selected_broker,
                    payout=payout,
                    call_price=call_price,
                    put_price=put_price,
                    volatility=volatility,
                    expiry=expiry,
                    risk_free_rate=risk_free_rate
                )

            signals = []
            current = start
            while current < end:
                # Generate signal based on technical indicators if available
                if 'otc_indicators' in session:
                    indicators = session['otc_indicators']
                    # Use RSI for signal generation
                    if 'rsi' in indicators and not indicators['rsi'].empty:
                        last_rsi = indicators['rsi'].iloc[-1]
                        if last_rsi > 70:
                            direction = "PUT"
                        elif last_rsi < 30:
                            direction = "CALL"
                        else:
                            direction = random.choice(["CALL", "PUT"]) if signal_type == "BOTH" else signal_type
                    else:
                        direction = random.choice(["CALL", "PUT"]) if signal_type == "BOTH" else signal_type
                else:
                    direction = random.choice(["CALL", "PUT"]) if signal_type == "BOTH" else signal_type

                signals.append({
                    "time": current.strftime("%H:%M"),
                    "pair": pair,
                    "direction": direction
                })
                save_signal(session["user_id"], current.strftime("%H:%M"), pair, direction)
                current += timedelta(minutes=random.randint(1, 15))

            session["otc_signals"] = signals
            return render_template("otc.html",
                signals=signals,
                current_rate=current_rate,
                selected_pair=selected_pair,
                selected_broker=selected_broker,
                payout=payout,
                call_price=call_price,
                put_price=put_price,
                volatility=volatility,
                expiry=expiry,
                risk_free_rate=risk_free_rate,
                pairs=otc_pairs,
                brokers=otc_brokers
            )

        except ValueError:
            return render_template("otc.html",
                error="Invalid time format.",
                pairs=otc_pairs,
                brokers=otc_brokers,
                current_rate=current_rate,
                selected_pair=selected_pair,
                selected_broker=selected_broker,
                payout=payout,
                call_price=call_price,
                put_price=put_price,
                volatility=volatility,
                expiry=expiry,
                risk_free_rate=risk_free_rate
            )

    # For GET requests, show the rate for the default pair and broker
    current_rate = otc_handler.get_realtime_price(selected_pair)
    print(f"Initial rate for {selected_pair}: {current_rate}")  # Debug print
    
    if current_rate:
        S = current_rate
        K = S
        T = expiry
        r = risk_free_rate
        sigma = volatility
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")

    signals = session.get("otc_signals", [])
    return render_template("otc.html",
        pairs=otc_pairs,
        brokers=otc_brokers,
        current_rate=current_rate,
        selected_pair=selected_pair,
        selected_broker=selected_broker,
        payout=payout,
        call_price=call_price,
        put_price=put_price,
        volatility=volatility,
        expiry=expiry,
        risk_free_rate=risk_free_rate,
        signals=signals
    )

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
        S = get_cached_realtime_forex(s["pair"].replace('_OTC',''))
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
            S = get_cached_realtime_forex(s["pair"])
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

def calculate_technical_indicators(data):
    try:
        # Convert data to pandas DataFrame
        df = pd.DataFrame(data)
        
        # Calculate indicators using pandas
        df['SMA_20'] = df['close'].rolling(window=20).mean()
        df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
        
        # MACD
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # Bollinger Bands
        df['BB_middle'] = df['close'].rolling(window=20).mean()
        df['BB_std'] = df['close'].rolling(window=20).std()
        df['BB_upper'] = df['BB_middle'] + (df['BB_std'] * 2)
        df['BB_lower'] = df['BB_middle'] - (df['BB_std'] * 2)
        
        return {
            'sma': df['SMA_20'].tolist(),
            'ema': df['EMA_20'].tolist(),
            'macd': df['MACD'].tolist(),
            'macd_signal': df['Signal'].tolist(),
            'rsi': df['RSI'].tolist(),
            'bollinger_upper': df['BB_upper'].tolist(),
            'bollinger_lower': df['BB_lower'].tolist()
        }
    except Exception as e:
        print(f"Error calculating indicators: {e}")
        return None

def get_historical_data(symbol, period='1mo', interval='1d'):
    """Fetch historical market data and calculate technical indicators"""
    try:
        yahoo_symbol = symbol_map.get(symbol)
        if not yahoo_symbol:
            print(f"Invalid symbol: {symbol}")
            return {
                'historical': None,
                'realtime': None,
                'error': f"Invalid symbol: {symbol}"
            }
        
        print(f"Fetching data for {symbol} using Yahoo symbol {yahoo_symbol}")
        
        # Fetch data from Yahoo Finance
        ticker = yf.Ticker(yahoo_symbol)
        df = ticker.history(period=period, interval=interval)
        
        if df.empty:
            print(f"No data received from Yahoo Finance for {symbol}")
            return {
                'historical': None,
                'realtime': None,
                'error': f"No data available for {symbol}"
            }
        
        # Calculate technical indicators
        # Simple Moving Averages
        df['SMA20'] = df['Close'].rolling(window=20).mean()
        
        # Exponential Moving Averages
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        
        # MACD
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        
        # RSI
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # Bollinger Bands
        df['BB_middle'] = df['Close'].rolling(window=20).mean()
        df['BB_std'] = df['Close'].rolling(window=20).std()
        df['BB_upper'] = df['BB_middle'] + (df['BB_std'] * 2)
        df['BB_lower'] = df['BB_middle'] - (df['BB_std'] * 2)
        
        # Replace NaN values with None for JSON serialization
        df = df.replace({np.nan: None})
        
        # Prepare the response data
        dates = df.index.strftime('%Y-%m-%d').tolist()
        
        historical_data = {
            'dates': dates,
            'prices': {
                'open': [round(x, 2) if x is not None else None for x in df['Open'].tolist()],
                'high': [round(x, 2) if x is not None else None for x in df['High'].tolist()],
                'low': [round(x, 2) if x is not None else None for x in df['Low'].tolist()],
                'close': [round(x, 2) if x is not None else None for x in df['Close'].tolist()],
                'volume': [int(x) if x is not None else None for x in df['Volume'].tolist()]
            },
            'indicators': {
                'sma': [round(x, 2) if x is not None else None for x in df['SMA20'].tolist()],
                'ema': [round(x, 2) if x is not None else None for x in df['EMA20'].tolist()],
                'macd': [round(x, 2) if x is not None else None for x in df['MACD'].tolist()],
                'macd_signal': [round(x, 2) if x is not None else None for x in df['Signal'].tolist()],
                'rsi': [round(x, 2) if x is not None else None for x in df['RSI'].tolist()],
                'bollinger_upper': [round(x, 2) if x is not None else None for x in df['BB_upper'].tolist()],
                'bollinger_middle': [round(x, 2) if x is not None else None for x in df['BB_middle'].tolist()],
                'bollinger_lower': [round(x, 2) if x is not None else None for x in df['BB_lower'].tolist()]
            }
        }
        
        # Get real-time data for current values
        realtime_data = get_indian_market_data(symbol)
        
        return {
            'historical': historical_data,
            'realtime': realtime_data
        }
        
    except Exception as e:
        print(f"Error in get_historical_data for {symbol}: {str(e)}")
        return {
            'historical': None,
            'realtime': None,
            'error': str(e)
        }

@app.route("/market_data/<symbol>")
def market_data(symbol):
    """API endpoint to get market data for a symbol"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        timeframe = request.args.get('timeframe', '1mo')
        print(f"Fetching data for {symbol} with timeframe {timeframe}")  # Debug log
        
        data = get_historical_data(symbol, period=timeframe)
        print(f"Received data: {data}")  # Debug log
        
        if not data:
            return jsonify({'error': 'No data available'}), 404
            
        if data.get('error'):
            return jsonify({'error': data['error']}), 500
            
        if not data.get('historical') or not data.get('realtime'):
            return jsonify({'error': 'Incomplete data received'}), 500
            
        return jsonify(data)
        
    except Exception as e:
        print(f"Error in market_data endpoint: {str(e)}")  # Debug log
        return jsonify({'error': str(e)}), 500

def get_trading_signals(symbol: str) -> Dict:
    """Get trading signals for a symbol"""
    try:
        # Get market analysis from trading system
        analysis = trading_system.analyze_market(symbol)
        if not analysis:
            return {
                'type': 'NEUTRAL',
                'confidence': 0,
                'timestamp': datetime.now().isoformat()
            }
        
        return {
            'type': analysis['signal'],
            'confidence': round(analysis['confidence'] * 100, 2),
            'timestamp': analysis['timestamp']
        }
    except Exception as e:
        logger.error(f"Error getting trading signals for {symbol}: {str(e)}")
        return {
            'type': 'NEUTRAL',
            'confidence': 0,
            'timestamp': datetime.now().isoformat()
        }

@app.route('/market')
def market_dashboard():
    symbols = load_symbols()
    return render_template(
        'market_dashboard.html',
        subscribed_symbols=[{'symbol': s} for s in symbols],
        signals={}
    )

# Add WebSocket event handlers
@socketio.on('connect')
def handle_connect():
    if 'user_id' not in session:
        return False
    return websocket_handler.handle_connect(session['user_id'])

@socketio.on('disconnect')
def handle_disconnect():
    if 'user_id' in session:
        websocket_handler.handle_disconnect(session['user_id'])

@socketio.on('subscribe_symbol')
def handle_subscribe(data):
    if 'user_id' not in session:
        return False
    return websocket_handler.subscribe_symbol(session['user_id'], data['symbol'])

@socketio.on('unsubscribe_symbol')
def handle_unsubscribe(data):
    if 'user_id' not in session:
        return False
    return websocket_handler.unsubscribe_symbol(session['user_id'], data['symbol'])

@app.route("/api/trade", methods=["POST"])
def api_trade():
    """API endpoint for executing trades"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    data = request.get_json()
    symbol = data.get('symbol')
    trade_type = data.get('trade_type')
    quantity = data.get('quantity')
    
    if not all([symbol, trade_type, quantity]):
        return jsonify({'success': False, 'message': 'Missing required parameters'}), 400
    
    success, message = execute_trade(session['user_id'], symbol, trade_type, quantity)
    return jsonify({'success': success, 'message': message})

@app.route("/legal")
def legal():
    """Legal information page"""
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    return render_template("legal.html", 
                         user=get_user_by_id(session["user_id"]))

@app.route("/subscription")
def subscription():
    """Subscription plans page"""
    # Define subscription plans
    plans = [
        {
            "name": "Basic",
            "price": "₹999",
            "period": "month",
            "features": [
                "Basic Market Analysis",
                "Daily Trading Signals",
                "Email Notifications",
                "Basic Technical Indicators"
            ],
            "id": "basic"
        },
        {
            "name": "Pro",
            "price": "₹2,499",
            "period": "month",
            "features": [
                "Advanced Market Analysis",
                "Real-time Trading Signals",
                "Priority Email Support",
                "Advanced Technical Indicators",
                "Custom Alerts",
                "Market News Updates"
            ],
            "popular": True,
            "id": "pro"
        },
        {
            "name": "Premium",
            "price": "₹4,999",
            "period": "month",
            "features": [
                "All Pro Features",
                "1-on-1 Trading Support",
                "Custom Strategy Development",
                "Portfolio Analysis",
                "Risk Management Tools",
                "VIP Market Insights"
            ],
            "id": "premium"
        }
    ]
    
    # Get user if authenticated, otherwise pass None
    user = get_user_by_id(session["user_id"]) if "user_id" in session else None
    
    return render_template("subscription.html", 
                         user=user,
                         plans=plans)

@app.route("/subscribe/<plan_id>", methods=["POST"])
def subscribe(plan_id):
    """Handle subscription requests"""
    if "user_id" not in session:
        return jsonify({"error": "Please login to subscribe"}), 401
        
    user = get_user_by_id(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
        
    # Validate plan_id
    valid_plans = ["basic", "pro", "premium"]
    if plan_id not in valid_plans:
        return jsonify({"error": "Invalid subscription plan"}), 400
        
    try:
        # Here you would typically:
        # 1. Process payment
        # 2. Update user's subscription status in database
        # 3. Send confirmation email
        
        # For now, we'll just update the session
        session['subscription'] = {
            'plan': plan_id,
            'started_at': datetime.now().isoformat()
        }
        
        return jsonify({
            "success": True,
            "message": f"Successfully subscribed to {plan_id} plan",
            "redirect": url_for("dashboard")
        })
        
    except Exception as e:
        print(f"Error processing subscription: {str(e)}")
        return jsonify({"error": "Failed to process subscription. Please try again."}), 500

# Load symbols from file
def load_symbols():
    with open('symbols.json') as f:
        return json.load(f)

ALL_SYMBOLS = load_symbols()

# Background price updater
def background_price_updater(handler):
    while True:
        symbols = load_symbols()  # Reload in case file changes
        for symbol in symbols:
            try:
                data = handler.get_latest_price_data(symbol)
                handler.price_cache[f'{symbol}_price'] = (datetime.now(), data)
            except Exception as e:
                print(f'Error updating {symbol}: {e}')
        time.sleep(10)

# Start the background updater thread after websocket_handler is created
threading.Thread(target=background_price_updater, args=(websocket_handler,), daemon=True).start()

if __name__ == "__main__":
    socketio.run(app, debug=True)
