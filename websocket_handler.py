from flask_socketio import SocketIO, emit
from flask import session, request
from datetime import datetime, timedelta
import json
import logging
from typing import Dict, Optional
import yfinance as yf
import pandas as pd
import numpy as np
import time
import random

logger = logging.getLogger(__name__)

REAL_FOREX_SYMBOLS = {'AUDUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'USDCAD', 'USDCHF', 'NZDUSD', 'EURGBP', 'EURJPY', 'GBPJPY'}
REAL_INDIAN_SYMBOLS = {'NIFTY50', 'BANKNIFTY', 'NSEBANK', 'NSEIT', 'NSEINFRA', 'NSEPHARMA', 'NSEFMCG', 'NSEMETAL', 'NSEENERGY', 'NSEAUTO', 'NIFTYMIDCAP', 'NIFTYSMALLCAP', 'NIFTYNEXT50', 'NIFTY100', 'NIFTY500', 'NIFTYREALTY', 'NIFTYPVTBANK', 'NIFTYPSUBANK', 'NIFTYFIN', 'NIFTYMEDIA', 'RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 'HINDUNILVR', 'SBIN', 'BHARTIARTL', 'KOTAKBANK', 'BAJFINANCE'}

class WebSocketHandler:
    def __init__(self, socketio):
        self.socketio = socketio
        self.connected_users = {}
        self.subscribed_symbols = {}
        self.price_cache = {}
        self.last_update = {}
        self.last_request_time = {}
        self.min_request_interval = 2  # Reduced to 2 seconds for better responsiveness
        self.rate_limit_backoff = {}
        self.max_backoff = 30
        self.forex_symbols = {
            'AUDUSD': 'AUDUSD=X',
            'EURUSD': 'EURUSD=X',
            'GBPUSD': 'GBPUSD=X',
            'USDJPY': 'USDJPY=X',
            'USDCAD': 'USDCAD=X',
            'USDCHF': 'USDCHF=X',
            'NZDUSD': 'NZDUSD=X',
            'EURGBP': 'EURGBP=X',
            'EURJPY': 'EURJPY=X',
            'GBPJPY': 'GBPJPY=X'
        }
        self.setup_handlers()
        
    def setup_handlers(self):
        """Setup WebSocket event handlers"""
        @self.socketio.on('connect')
        def handle_connect():
            if 'user_id' in session:
                self.handle_connect(session['user_id'])
            
        @self.socketio.on('disconnect')
        def handle_disconnect():
            if 'user_id' in session:
                self.handle_disconnect(session['user_id'])
            
        @self.socketio.on('subscribe')
        def handle_subscribe(data):
            try:
                symbol = data.get('symbol')
                if symbol and 'user_id' in session:
                    self.subscribe_symbol(session['user_id'], symbol)
            except Exception as e:
                logger.error(f'Error handling subscription: {str(e)}')
                
        @self.socketio.on('unsubscribe')
        def handle_unsubscribe(data):
            try:
                symbol = data.get('symbol')
                if symbol and 'user_id' in session:
                    self.unsubscribe_symbol(session['user_id'], symbol)
            except Exception as e:
                logger.error(f'Error handling unsubscription: {str(e)}')
                
    def handle_connect(self, user_id):
        """Handle new WebSocket connection"""
        try:
            self.connected_users[user_id] = {
                'sid': request.sid,
                'subscriptions': set()
            }
            logger.info(f"User {user_id} connected via WebSocket")
            return True
        except Exception as e:
            logger.error(f"Error handling WebSocket connection: {str(e)}")
            return False

    def handle_disconnect(self, user_id):
        """Handle WebSocket disconnection"""
        try:
            if user_id in self.connected_users:
                # Clean up subscriptions
                for symbol in self.connected_users[user_id]['subscriptions']:
                    self.unsubscribe_symbol(user_id, symbol)
                del self.connected_users[user_id]
                logger.info(f"User {user_id} disconnected from WebSocket")
        except Exception as e:
            logger.error(f"Error handling WebSocket disconnection: {str(e)}")

    def subscribe_symbol(self, user_id, symbol):
        """Subscribe user to symbol updates"""
        try:
            if user_id not in self.connected_users:
                logger.warning(f"User {user_id} not connected")
                return False

            # Extract symbol string if it's a SQLite Row object
            if hasattr(symbol, 'symbol'):
                symbol = symbol.symbol
            elif isinstance(symbol, dict) and 'symbol' in symbol:
                symbol = symbol['symbol']

            # Add to user's subscriptions
            self.connected_users[user_id]['subscriptions'].add(symbol)

            # Send initial data
            data = self.get_latest_price_data(symbol)
            if data:
                self.socketio.emit('price_update', data, room=request.sid)
                logger.info(f"User {user_id} subscribed to {symbol} with initial data")
                
                # Start periodic updates
                self.start_periodic_updates(user_id, symbol)
                return True
            return False
        except Exception as e:
            logger.error(f"Error subscribing to symbol {symbol}: {str(e)}")
            return False

    def start_periodic_updates(self, user_id, symbol):
        """Start periodic updates for a symbol"""
        try:
            def update():
                if user_id in self.connected_users and symbol in self.connected_users[user_id]['subscriptions']:
                    data = self.get_latest_price_data(symbol)
                    if data:
                        self.socketio.emit('price_update', data, room=self.connected_users[user_id]['sid'])
                        logger.debug(f"Sent periodic update for {symbol} to user {user_id}")
            
            # Schedule updates every 10 seconds instead of 5
            self.socketio.start_background_task(update)
        except Exception as e:
            logger.error(f"Error starting periodic updates for {symbol}: {str(e)}")

    def unsubscribe_symbol(self, user_id, symbol):
        """Unsubscribe user from symbol updates"""
        try:
            if user_id in self.connected_users and symbol in self.connected_users[user_id]['subscriptions']:
                self.connected_users[user_id]['subscriptions'].remove(symbol)
                logger.info(f"User {user_id} unsubscribed from {symbol}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error unsubscribing from symbol {symbol}: {str(e)}")
            return False

    def get_latest_price_data(self, symbol):
        """Get latest price data for symbol"""
        # Route to real or simulated data
        if symbol not in REAL_FOREX_SYMBOLS and symbol not in REAL_INDIAN_SYMBOLS:
            return self.get_simulated_otc_data(symbol)
        try:
            # Extract symbol string if it's a SQLite Row object
            if hasattr(symbol, 'symbol'):
                symbol = symbol.symbol
            elif isinstance(symbol, dict) and 'symbol' in symbol:
                symbol = symbol['symbol']

            # Check cache first
            cache_key = f"{symbol}_price"
            if cache_key in self.price_cache:
                cache_time, cached_data = self.price_cache[cache_key]
                if datetime.now() - cache_time < timedelta(seconds=15):  # Reduced cache time to 15 seconds
                    logger.info(f"Using cached data for {symbol}")
                    return cached_data

            # Check rate limiting and backoff
            current_time = datetime.now()
            
            if symbol in self.rate_limit_backoff:
                backoff_until = self.rate_limit_backoff[symbol]
                if current_time < backoff_until:
                    logger.info(f"Symbol {symbol} is in backoff period until {backoff_until}")
                    if cache_key in self.price_cache:
                        return self.price_cache[cache_key][1]
                    return self.get_default_data(symbol)
            
            if symbol in self.last_request_time:
                time_since_last_request = (current_time - self.last_request_time[symbol]).total_seconds()
                if time_since_last_request < self.min_request_interval:
                    logger.info(f"Rate limiting {symbol}, using cached data")
                    if cache_key in self.price_cache:
                        return self.price_cache[cache_key][1]
                    return self.get_default_data(symbol)

            # Get Yahoo Finance symbol
            yahoo_symbol = self.forex_symbols.get(symbol, f"{symbol}=X")
            logger.info(f"Using Yahoo Finance symbol: {yahoo_symbol}")
            
            # Update last request time
            self.last_request_time[symbol] = current_time
            
            try:
                ticker = yf.Ticker(yahoo_symbol)
                data = ticker.history(period='1d', interval='1m')
                
                if data.empty:
                    # Try alternative format for forex pairs
                    alt_symbol = f"{symbol[:3]}/{symbol[3:]}"
                    logger.info(f"Trying alternative symbol format: {alt_symbol}")
                    ticker = yf.Ticker(alt_symbol)
                    data = ticker.history(period='1d', interval='1m')
                    
                    if data.empty:
                        logger.error(f"No data available for {symbol}")
                        return self.get_default_data(symbol)

                latest = data.iloc[-1]
                price_change = self.calculate_price_change(data)
                indicators = self.calculate_indicators(data)
                
                # Calculate OTC market specific metrics
                high_24h = float(data['High'].max())
                low_24h = float(data['Low'].min())
                spread = float(latest['High'] - latest['Low'])
                volatility = float(data['Close'].pct_change().std() * 100)
                
                # Calculate bid/ask spread (typical for OTC markets)
                bid = float(latest['Low'])
                ask = float(latest['High'])
                spread_pips = (ask - bid) * 10000  # Convert to pips

                result = {
                    'symbol': symbol,
                    'price': float(latest['Close']),
                    'change': float(price_change),
                    'high_24h': high_24h,
                    'low_24h': low_24h,
                    'spread': spread,
                    'spread_pips': spread_pips,
                    'volatility': volatility,
                    'bid': bid,
                    'ask': ask,
                    'rsi': float(indicators['rsi']),
                    'macd': float(indicators['macd']),
                    'timestamps': data.index.strftime('%Y-%m-%d %H:%M:%S').tolist(),
                    'prices': [float(x) for x in data['Close'].tolist()]
                }

                # Update cache
                self.price_cache[cache_key] = (datetime.now(), result)
                logger.info(f"Updated cache for {symbol} with new data")
                
                if symbol in self.rate_limit_backoff:
                    del self.rate_limit_backoff[symbol]
                
                return result
                
            except Exception as e:
                if "Too Many Requests" in str(e):
                    current_backoff = self.rate_limit_backoff.get(symbol, 5)
                    if isinstance(current_backoff, datetime):
                        current_backoff = (current_backoff - datetime.now()).total_seconds()
                    new_backoff = min(current_backoff * 2, self.max_backoff)
                    backoff_until = current_time + timedelta(seconds=int(new_backoff))
                    self.rate_limit_backoff[symbol] = backoff_until
                    logger.warning(f"Rate limited for {symbol}, backing off until {backoff_until}")
                else:
                    logger.error(f"Error fetching data for {symbol}: {str(e)}")
                
                return self.get_default_data(symbol)
                
        except Exception as e:
            logger.error(f"Error getting price data for {symbol}: {str(e)}")
            return self.get_default_data(symbol)

    def get_simulated_otc_data(self, symbol):
        """Simulate OTC data for demo/educational purposes."""
        # Simulate a mid price and spread
        base_price = random.uniform(1.0, 2.0) if not symbol.endswith('JPY') else random.uniform(100, 200)
        spread = 0.0002 if not symbol.endswith('JPY') else 0.02
        bid = base_price - spread / 2
        ask = base_price + spread / 2
        spread_pips = (ask - bid) * (10000 if not symbol.endswith('JPY') else 100)
        return {
            'symbol': symbol,
            'price': base_price,
            'change': random.uniform(-0.5, 0.5),
            'high_24h': base_price * 1.002,
            'low_24h': base_price * 0.998,
            'spread': spread,
            'spread_pips': spread_pips,
            'volatility': random.uniform(0.01, 0.1),
            'bid': bid,
            'ask': ask,
            'rsi': random.uniform(40, 60),
            'macd': random.uniform(-1, 1),
            'timestamps': [],
            'prices': []
        }

    def get_default_data(self, symbol):
        """Return default data structure for a symbol"""
        return {
            'symbol': symbol,
            'price': 0.0,
            'change': 0.0,
            'high_24h': 0.0,
            'low_24h': 0.0,
            'spread': 0.0,
            'spread_pips': 0.0,
            'volatility': 0.0,
            'bid': 0.0,
            'ask': 0.0,
            'rsi': 50.0,
            'macd': 0.0,
            'timestamps': [],
            'prices': []
        }

    def calculate_price_change(self, data):
        """Calculate price change percentage"""
        try:
            if len(data) < 2:
                return 0.0
            return ((data['Close'].iloc[-1] - data['Close'].iloc[0]) / data['Close'].iloc[0]) * 100
        except Exception as e:
            logger.error(f"Error calculating price change: {str(e)}")
            return 0.0

    def calculate_indicators(self, data):
        """Calculate technical indicators"""
        try:
            # RSI
            delta = data['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))

            # MACD
            exp1 = data['Close'].ewm(span=12, adjust=False).mean()
            exp2 = data['Close'].ewm(span=26, adjust=False).mean()
            macd = exp1 - exp2
            signal = macd.ewm(span=9, adjust=False).mean()

            return {
                'rsi': rsi.iloc[-1],
                'macd': macd.iloc[-1],
                'macd_signal': signal.iloc[-1]
            }
        except Exception as e:
            logger.error(f"Error calculating indicators: {str(e)}")
            return {
                'rsi': 50.0,
                'macd': 0.0,
                'macd_signal': 0.0
            }

    def broadcast_updates(self):
        """Send updates to all subscribed users"""
        try:
            for user_id, user_data in self.connected_users.items():
                for symbol in user_data['subscriptions']:
                    data = self.get_latest_price_data(symbol)
                    if data:
                        self.socketio.emit('price_update', data, room=user_data['sid'])
                        logger.debug(f"Broadcast update for {symbol} to user {user_id}")
        except Exception as e:
            logger.error(f"Error broadcasting updates: {str(e)}")

    def update_price(self, symbol: str, price_data: Dict):
        """Update price data and broadcast to subscribed clients"""
        try:
            # Update cache
            self.price_cache[symbol] = {
                'symbol': symbol,
                'price': price_data.get('price'),
                'change': price_data.get('change'),
                'volume': price_data.get('volume'),
                'timestamp': datetime.now().isoformat()
            }
            
            # Broadcast to all subscribed clients
            self.socketio.emit('price_update', self.price_cache[symbol])
            
        except Exception as e:
            logger.error(f'Error updating price: {str(e)}')
            
    def broadcast_trade(self, trade_data: Dict):
        """Broadcast trade information to all clients"""
        try:
            self.socketio.emit('trade_update', trade_data)
        except Exception as e:
            logger.error(f'Error broadcasting trade: {str(e)}')
            
    def broadcast_signal(self, signal_data: Dict):
        """Broadcast trading signal to all clients"""
        try:
            self.socketio.emit('signal_update', signal_data)
        except Exception as e:
            logger.error(f'Error broadcasting signal: {str(e)}')
            
    def broadcast_alert(self, alert_data: Dict):
        """Broadcast alert to all clients"""
        try:
            self.socketio.emit('alert', alert_data)
        except Exception as e:
            logger.error(f'Error broadcasting alert: {str(e)}')
            
    def get_cached_price(self, symbol: str) -> Optional[Dict]:
        """Get cached price data for a symbol"""
        return self.price_cache.get(symbol)
        
    def clear_cache(self, symbol: Optional[str] = None):
        """Clear price cache for a symbol or all symbols"""
        try:
            if symbol:
                self.price_cache.pop(symbol, None)
            else:
                self.price_cache.clear()
        except Exception as e:
            logger.error(f'Error clearing cache: {str(e)}') 