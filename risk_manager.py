from typing import Dict, List, Optional, Tuple
import numpy as np
import logging
from datetime import datetime, timedelta
import sqlite3
import pandas as pd

logger = logging.getLogger(__name__)

class RiskManager:
    def __init__(self, db_path: str = 'kishanx.db'):
        self.db_path = db_path
        self.max_position_size = 0.02  # 2% of portfolio per trade
        self.max_daily_loss = 0.05    # 5% of portfolio per day
        self.max_drawdown = 0.15      # 15% maximum drawdown
        self.stop_loss_pct = 0.02     # 2% stop loss
        self.take_profit_pct = 0.04   # 4% take profit

    def check_risk_limits(self, user_id: int, symbol: str, quantity: float) -> Tuple[bool, str]:
        """Check if trade meets risk management criteria"""
        try:
            # Get user's portfolio value
            portfolio_value = self.get_portfolio_value(user_id)
            if portfolio_value is None:
                return False, "Could not retrieve portfolio value"

            # Get current position size
            position_size = self.get_position_size(user_id, symbol)
            
            # Calculate new position value
            current_price = self.get_current_price(symbol)
            if current_price is None:
                return False, "Could not retrieve current price"
            
            new_position_value = current_price * quantity
            
            # Check position size limit
            if new_position_value > portfolio_value * self.max_position_size:
                return False, f"Position size exceeds {self.max_position_size * 100}% of portfolio"

            # Check daily loss limit
            daily_pnl = self.get_daily_pnl(user_id)
            if daily_pnl < -portfolio_value * self.max_daily_loss:
                return False, f"Daily loss limit of {self.max_daily_loss * 100}% reached"

            # Check drawdown limit
            drawdown = self.calculate_drawdown(user_id)
            if drawdown > self.max_drawdown:
                return False, f"Maximum drawdown of {self.max_drawdown * 100}% reached"

            return True, "Risk checks passed"
        except Exception as e:
            logger.error(f"Error checking risk limits: {str(e)}")
            return False, str(e)

    def calculate_position_size(self, user_id: int, symbol: str, risk_per_trade: float = 0.02) -> Optional[float]:
        """Calculate optimal position size based on risk parameters"""
        try:
            # Get portfolio value
            portfolio_value = self.get_portfolio_value(user_id)
            if portfolio_value is None:
                return None

            # Get current price and volatility
            current_price = self.get_current_price(symbol)
            volatility = self.calculate_volatility(symbol)
            
            if current_price is None or volatility is None:
                return None

            # Calculate position size based on risk
            risk_amount = portfolio_value * risk_per_trade
            position_size = risk_amount / (current_price * volatility)
            
            return position_size
        except Exception as e:
            logger.error(f"Error calculating position size: {str(e)}")
            return None

    def get_portfolio_value(self, user_id: int) -> Optional[float]:
        """Get user's current portfolio value"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Get cash balance
            cursor.execute('SELECT balance FROM users WHERE id = ?', (user_id,))
            cash_balance = cursor.fetchone()[0]
            
            # Get value of all positions
            cursor.execute('''
                SELECT symbol, quantity, current_price 
                FROM positions 
                WHERE user_id = ?
            ''', (user_id,))
            positions = cursor.fetchall()
            
            position_value = sum(quantity * price for _, quantity, price in positions)
            
            return cash_balance + position_value
        except Exception as e:
            logger.error(f"Error getting portfolio value: {str(e)}")
            return None
        finally:
            if conn:
                conn.close()

    def get_position_size(self, user_id: int, symbol: str) -> float:
        """Get current position size for symbol"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT quantity 
                FROM positions 
                WHERE user_id = ? AND symbol = ?
            ''', (user_id, symbol))
            
            result = cursor.fetchone()
            return result[0] if result else 0
        except Exception as e:
            logger.error(f"Error getting position size: {str(e)}")
            return 0
        finally:
            if conn:
                conn.close()

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for symbol"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT price 
                FROM market_data 
                WHERE symbol = ? 
                ORDER BY timestamp DESC 
                LIMIT 1
            ''', (symbol,))
            
            result = cursor.fetchone()
            return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting current price: {str(e)}")
            return None
        finally:
            if conn:
                conn.close()

    def get_daily_pnl(self, user_id: int) -> float:
        """Get user's daily profit/loss"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            today = datetime.now().date()
            cursor.execute('''
                SELECT SUM(profit_loss) 
                FROM trades 
                WHERE user_id = ? 
                AND DATE(entry_time) = ?
            ''', (user_id, today))
            
            result = cursor.fetchone()
            conn.close()
            
            return result[0] if result[0] is not None else 0
        except Exception as e:
            logger.error(f"Error getting daily P&L: {str(e)}")
            return 0

    def calculate_drawdown(self, user_id: int) -> float:
        """Calculate current drawdown"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Get portfolio value history
            cursor.execute('''
                SELECT portfolio_value, timestamp 
                FROM portfolio_history 
                WHERE user_id = ? 
                ORDER BY timestamp DESC
            ''', (user_id,))
            
            history = cursor.fetchall()
            conn.close()
            
            if not history:
                return 0
            
            # Calculate drawdown
            peak = max(value for value, _ in history)
            current = history[0][0]
            
            return (peak - current) / peak if peak > 0 else 0
        except Exception as e:
            logger.error(f"Error calculating drawdown: {str(e)}")
            return 0

    def calculate_volatility(self, symbol: str, window: int = 20) -> Optional[float]:
        """Calculate price volatility"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT price 
                FROM market_data 
                WHERE symbol = ? 
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (symbol, window))
            
            prices = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            if len(prices) < 2:
                return None
            
            returns = np.diff(np.log(prices))
            volatility = np.std(returns) * np.sqrt(252)  # Annualized volatility
            
            return volatility
        except Exception as e:
            logger.error(f"Error calculating volatility: {str(e)}")
            return None

    def update_risk_limits(self, user_id: int, limits: Dict) -> bool:
        """Update user's risk limits"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE risk_limits 
                SET max_position_size = ?,
                    max_daily_loss = ?,
                    max_drawdown = ?,
                    updated_at = ?
                WHERE user_id = ?
            ''', (
                limits.get('max_position_size', self.max_position_size),
                limits.get('max_daily_loss', self.max_daily_loss),
                limits.get('max_drawdown', self.max_drawdown),
                datetime.now().isoformat(),
                user_id
            ))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error updating risk limits: {str(e)}")
            return False

    def get_risk_metrics(self, trades: List[Dict]) -> Dict:
        """Calculate risk metrics for trading performance"""
        try:
            if not trades:
                return {
                    'total_trades': 0,
                    'winning_trades': 0,
                    'losing_trades': 0,
                    'win_rate': 0,
                    'average_win': 0,
                    'average_loss': 0,
                    'largest_win': 0,
                    'largest_loss': 0,
                    'profit_factor': 0,
                    'max_drawdown': 0
                }
                
            # Calculate basic metrics
            winning_trades = [t for t in trades if t.get('profit', 0) > 0]
            losing_trades = [t for t in trades if t.get('profit', 0) < 0]
            
            total_trades = len(trades)
            winning_trades_count = len(winning_trades)
            losing_trades_count = len(losing_trades)
            
            win_rate = winning_trades_count / total_trades if total_trades > 0 else 0
            
            # Calculate profit metrics
            total_profit = sum(t.get('profit', 0) for t in winning_trades)
            total_loss = abs(sum(t.get('profit', 0) for t in losing_trades))
            
            average_win = total_profit / winning_trades_count if winning_trades_count > 0 else 0
            average_loss = total_loss / losing_trades_count if losing_trades_count > 0 else 0
            
            largest_win = max((t.get('profit', 0) for t in winning_trades), default=0)
            largest_loss = min((t.get('profit', 0) for t in losing_trades), default=0)
            
            profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')
            
            # Calculate drawdown
            cumulative_returns = np.cumsum([t.get('profit', 0) for t in trades])
            max_drawdown = 0
            peak = cumulative_returns[0]
            
            for value in cumulative_returns:
                if value > peak:
                    peak = value
                drawdown = (peak - value) / peak if peak > 0 else 0
                max_drawdown = max(max_drawdown, drawdown)
                
            return {
                'total_trades': total_trades,
                'winning_trades': winning_trades_count,
                'losing_trades': losing_trades_count,
                'win_rate': win_rate,
                'average_win': average_win,
                'average_loss': average_loss,
                'largest_win': largest_win,
                'largest_loss': largest_loss,
                'profit_factor': profit_factor,
                'max_drawdown': max_drawdown
            }
            
        except Exception as e:
            logger.error(f'Error calculating risk metrics: {str(e)}')
            return {} 