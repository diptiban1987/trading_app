import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

class TradingSystem:
    def __init__(self):
        self.strategies = {
            'rsi': self.rsi_strategy,
            'macd': self.macd_strategy,
            'bollinger': self.bollinger_strategy,
            'moving_average': self.moving_average_strategy
        }
        self.timeframes = ['1m', '5m', '15m', '1h', '4h', '1d']
        self.cache = {}
        self.cache_duration = timedelta(minutes=5)

    def analyze_market(self, symbol: str) -> Dict:
        """Analyze market data and generate trading signals"""
        try:
            # Get historical data
            data = self.get_historical_data(symbol)
            if data is None or data.empty:
                logger.error(f"No data available for {symbol}")
                return {
                    'symbol': symbol,
                    'signal': 'NEUTRAL',
                    'confidence': 0.0,
                    'indicators': {},
                    'timestamp': datetime.now().isoformat()
                }

            # Calculate indicators
            indicators = self.calculate_indicators(data)
            if not indicators:
                logger.error(f"Failed to calculate indicators for {symbol}")
                return {
                    'symbol': symbol,
                    'signal': 'NEUTRAL',
                    'confidence': 0.0,
                    'indicators': {},
                    'timestamp': datetime.now().isoformat()
                }

            # Generate signals from each strategy
            signals = []
            confidences = []

            # RSI Strategy
            rsi_signal, rsi_conf = self.rsi_strategy(indicators['rsi'])
            signals.append(rsi_signal)
            confidences.append(rsi_conf)

            # MACD Strategy
            macd_signal, macd_conf = self.macd_strategy(
                indicators['macd'],
                indicators['macd_signal']
            )
            signals.append(macd_signal)
            confidences.append(macd_conf)

            # Bollinger Bands Strategy
            bb_signal, bb_conf = self.bollinger_strategy(
                data['Close'].iloc[-1],
                indicators['bb_upper'],
                indicators['bb_lower']
            )
            signals.append(bb_signal)
            confidences.append(bb_conf)

            # Moving Average Strategy
            ma_signal, ma_conf = self.moving_average_strategy(
                data['Close'].iloc[-1],
                indicators['sma_20'],
                indicators['sma_50']
            )
            signals.append(ma_signal)
            confidences.append(ma_conf)

            # Combine signals
            final_signal, confidence = self.combine_signals(signals, confidences)

            return {
                'symbol': symbol,
                'signal': final_signal,
                'confidence': confidence,
                'indicators': indicators,
                'timestamp': datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Error analyzing market for {symbol}: {str(e)}")
            return {
                'symbol': symbol,
                'signal': 'NEUTRAL',
                'confidence': 0.0,
                'indicators': {},
                'timestamp': datetime.now().isoformat()
            }

    def get_historical_data(self, symbol: str, period: str = '1d', interval: str = '1m') -> Optional[pd.DataFrame]:
        """Get historical price data"""
        try:
            # Check cache first
            cache_key = f"{symbol}_{period}_{interval}"
            if cache_key in self.cache:
                cache_time, cached_data = self.cache[cache_key]
                if datetime.now() - cache_time < self.cache_duration:
                    return cached_data

            # Fetch new data
            ticker = yf.Ticker(symbol)
            data = ticker.history(period=period, interval=interval)
            
            if data.empty:
                logger.error(f"No data returned for {symbol}")
                return None

            # Update cache
            self.cache[cache_key] = (datetime.now(), data)
            return data
        except Exception as e:
            logger.error(f"Error fetching historical data for {symbol}: {str(e)}")
            return None

    def calculate_indicators(self, data: pd.DataFrame) -> Optional[Dict]:
        """Calculate technical indicators"""
        try:
            if data.empty or len(data) < 50:  # Need enough data for calculations
                return None

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

            # Bollinger Bands
            sma_20 = data['Close'].rolling(window=20).mean()
            std_20 = data['Close'].rolling(window=20).std()
            bb_upper = sma_20 + (std_20 * 2)
            bb_lower = sma_20 - (std_20 * 2)

            # Moving Averages
            sma_50 = data['Close'].rolling(window=50).mean()

            return {
                'rsi': rsi.iloc[-1],
                'macd': macd.iloc[-1],
                'macd_signal': signal.iloc[-1],
                'bb_upper': bb_upper.iloc[-1],
                'bb_lower': bb_lower.iloc[-1],
                'sma_20': sma_20.iloc[-1],
                'sma_50': sma_50.iloc[-1]
            }
        except Exception as e:
            logger.error(f"Error calculating indicators: {str(e)}")
            return None

    def rsi_strategy(self, rsi: float) -> Tuple[str, float]:
        """RSI-based trading strategy"""
        try:
            if rsi > 70:
                return 'SELL', min((rsi - 70) / 30, 1.0)
            elif rsi < 30:
                return 'BUY', min((30 - rsi) / 30, 1.0)
            return 'NEUTRAL', 0.0
        except Exception as e:
            logger.error(f"Error in RSI strategy: {str(e)}")
            return 'NEUTRAL', 0.0

    def macd_strategy(self, macd: float, signal: float) -> Tuple[str, float]:
        """MACD-based trading strategy"""
        try:
            if macd > signal:
                return 'BUY', min(abs(macd - signal) / abs(signal), 1.0)
            elif macd < signal:
                return 'SELL', min(abs(macd - signal) / abs(signal), 1.0)
            return 'NEUTRAL', 0.0
        except Exception as e:
            logger.error(f"Error in MACD strategy: {str(e)}")
            return 'NEUTRAL', 0.0

    def bollinger_strategy(self, price: float, upper: float, lower: float) -> Tuple[str, float]:
        """Bollinger Bands-based trading strategy"""
        try:
            if price > upper:
                return 'SELL', min((price - upper) / upper, 1.0)
            elif price < lower:
                return 'BUY', min((lower - price) / lower, 1.0)
            return 'NEUTRAL', 0.0
        except Exception as e:
            logger.error(f"Error in Bollinger Bands strategy: {str(e)}")
            return 'NEUTRAL', 0.0

    def moving_average_strategy(self, price: float, sma_20: float, sma_50: float) -> Tuple[str, float]:
        """Moving Average-based trading strategy"""
        try:
            if price > sma_20 and sma_20 > sma_50:
                return 'BUY', min((price - sma_20) / sma_20, 1.0)
            elif price < sma_20 and sma_20 < sma_50:
                return 'SELL', min((sma_20 - price) / sma_20, 1.0)
            return 'NEUTRAL', 0.0
        except Exception as e:
            logger.error(f"Error in Moving Average strategy: {str(e)}")
            return 'NEUTRAL', 0.0

    def combine_signals(self, signals: List[str], confidences: List[float]) -> Tuple[str, float]:
        """Combine signals from different strategies"""
        try:
            if not signals or not confidences:
                return 'NEUTRAL', 0.0

            # Count signals
            buy_count = signals.count('BUY')
            sell_count = signals.count('SELL')
            neutral_count = signals.count('NEUTRAL')

            # Calculate weighted confidence
            total_confidence = sum(confidences)
            if total_confidence == 0:
                return 'NEUTRAL', 0.0

            # Determine final signal
            if buy_count > sell_count and buy_count > neutral_count:
                return 'BUY', total_confidence / len(signals)
            elif sell_count > buy_count and sell_count > neutral_count:
                return 'SELL', total_confidence / len(signals)
            return 'NEUTRAL', 0.0
        except Exception as e:
            logger.error(f"Error combining signals: {str(e)}")
            return 'NEUTRAL', 0.0

    def calculate_risk_metrics(self, trades: List[Dict]) -> Dict:
        """Calculate risk metrics for trading performance"""
        try:
            if not trades:
                return {
                    'win_rate': 0,
                    'profit_factor': 0,
                    'average_win': 0,
                    'average_loss': 0,
                    'max_drawdown': 0,
                    'sharpe_ratio': 0
                }
                
            # Calculate basic metrics
            winning_trades = [t for t in trades if t.get('profit', 0) > 0]
            losing_trades = [t for t in trades if t.get('profit', 0) < 0]
            
            win_rate = len(winning_trades) / len(trades) if trades else 0
            
            total_profit = sum(t.get('profit', 0) for t in winning_trades)
            total_loss = abs(sum(t.get('profit', 0) for t in losing_trades))
            
            profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')
            
            average_win = total_profit / len(winning_trades) if winning_trades else 0
            average_loss = total_loss / len(losing_trades) if losing_trades else 0
            
            # Calculate drawdown
            cumulative_returns = np.cumsum([t.get('profit', 0) for t in trades])
            max_drawdown = 0
            peak = cumulative_returns[0]
            
            for value in cumulative_returns:
                if value > peak:
                    peak = value
                drawdown = (peak - value) / peak if peak > 0 else 0
                max_drawdown = max(max_drawdown, drawdown)
                
            # Calculate Sharpe ratio (assuming risk-free rate of 0)
            returns = np.array([t.get('profit', 0) for t in trades])
            sharpe_ratio = np.mean(returns) / np.std(returns) if len(returns) > 1 and np.std(returns) > 0 else 0
            
            return {
                'win_rate': win_rate,
                'profit_factor': profit_factor,
                'average_win': average_win,
                'average_loss': average_loss,
                'max_drawdown': max_drawdown,
                'sharpe_ratio': sharpe_ratio
            }
            
        except Exception as e:
            logger.error(f"Error calculating risk metrics: {str(e)}")
            return {}

    def get_trading_signals(self, symbol: str) -> Dict:
        """Get trading signals for a symbol"""
        try:
            # Get historical data
            data = self.get_historical_data(symbol)
            if data is None or data.empty:
                logger.error(f"No data available for {symbol}")
                return None

            # Calculate indicators
            indicators = self.calculate_indicators(data)
            if not indicators:
                logger.error(f"Failed to calculate indicators for {symbol}")
                return None

            # Generate signals from each strategy
            signals = []
            confidences = []

            # RSI Strategy
            rsi_signal, rsi_conf = self.rsi_strategy(indicators['rsi'])
            signals.append(rsi_signal)
            confidences.append(rsi_conf)

            # MACD Strategy
            macd_signal, macd_conf = self.macd_strategy(
                indicators['macd'],
                indicators['macd_signal']
            )
            signals.append(macd_signal)
            confidences.append(macd_conf)

            # Bollinger Bands Strategy
            bb_signal, bb_conf = self.bollinger_strategy(
                data['Close'].iloc[-1],
                indicators['bb_upper'],
                indicators['bb_lower']
            )
            signals.append(bb_signal)
            confidences.append(bb_conf)

            # Moving Average Strategy
            ma_signal, ma_conf = self.moving_average_strategy(
                data['Close'].iloc[-1],
                indicators['sma_20'],
                indicators['sma_50']
            )
            signals.append(ma_signal)
            confidences.append(ma_conf)

            # Combine signals
            final_signal, confidence = self.combine_signals(signals, confidences)

            return {
                'symbol': symbol,
                'type': final_signal,
                'confidence': round(confidence * 100, 2),
                'timestamp': datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Error getting trading signals for {symbol}: {str(e)}")
            return None 