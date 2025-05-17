import threading
import time
from datetime import datetime
import logging
from typing import Dict, List, Optional
import sqlite3
from trading_system import TradingSystem
from risk_manager import RiskManager

logger = logging.getLogger(__name__)

class AutoTrader:
    def __init__(self, trading_system: TradingSystem, risk_manager: RiskManager):
        self.trading_system = trading_system
        self.risk_manager = risk_manager
        self.active_trades = {}
        self.db_path = 'kishanx.db'
        self.running = False
        self.trading_thread = None
        
    def start(self):
        """Start the auto trading system"""
        if self.running:
            logger.warning('Auto trading system is already running')
            return
            
        self.running = True
        self.trading_thread = threading.Thread(target=self._trading_loop)
        self.trading_thread.daemon = True
        self.trading_thread.start()
        logger.info('Auto trading system started')
        
    def stop(self):
        """Stop the auto trading system"""
        if not self.running:
            logger.warning('Auto trading system is not running')
            return
            
        self.running = False
        if self.trading_thread:
            self.trading_thread.join()
        logger.info('Auto trading system stopped')
        
    def _trading_loop(self):
        """Main trading loop"""
        while self.running:
            try:
                # Process each active trade
                self._process_active_trades()
                
                # Check for new trading opportunities
                self._check_trading_opportunities()
                
                # Sleep to prevent excessive CPU usage
                time.sleep(1)
                
            except Exception as e:
                logger.error(f'Error in trading loop: {str(e)}')
                time.sleep(5)  # Sleep longer on error
                
    def _process_active_trades(self):
        """Process all active trades"""
        try:
            for trade_id, trade in list(self.active_trades.items()):
                # Get current price
                current_price = self.risk_manager.get_current_price(trade['symbol'])
                if not current_price:
                    continue
                    
                # Check if stop loss or take profit is hit
                if self.check_exit_conditions(trade_id, current_price):
                    self.close_trade(trade_id, current_price)
                    
        except Exception as e:
            logger.error(f'Error processing active trades: {str(e)}')
            
    def _check_trading_opportunities(self):
        """Check for new trading opportunities"""
        try:
            # Get market data for all symbols
            symbols = self.risk_manager.get_all_symbols()
            
            for symbol in symbols:
                # Skip if already trading this symbol
                if symbol in self.active_trades:
                    continue
                    
                # Get current price data
                price_data = self.risk_manager.get_current_price(symbol)
                if not price_data:
                    continue
                    
                # Generate trading signals
                signals = self.trading_system.generate_signals(price_data)
                
                # Check for strong signals
                if signals.get('overall') in ['STRONG_BUY', 'STRONG_SELL']:
                    self.execute_trade(signals, price_data)
                    
        except Exception as e:
            logger.error(f'Error checking trading opportunities: {str(e)}')
            
    def execute_trade(self, signals: Dict, price_data: Dict):
        """Execute a new trade"""
        try:
            # Prepare trade data
            trade_data = {
                'symbol': signals['symbol'],
                'direction': 'BUY' if signals['overall'] == 'STRONG_BUY' else 'SELL',
                'entry_price': price_data['price'],
                'stop_loss': self.calculate_exit_levels(trade_data['direction'], price_data['price'])[0],
                'take_profit': self.calculate_exit_levels(trade_data['direction'], price_data['price'])[1],
                'lot_size': self.risk_manager.calculate_position_size({
                    'entry_price': price_data['price'],
                    'stop_loss': self.calculate_exit_levels(trade_data['direction'], price_data['price'])[0]
                })
            }
            
            # Check risk parameters
            if not self.risk_manager.check_trade_risk(trade_data):
                logger.warning(f'Trade rejected by risk manager: {trade_data["symbol"]}')
                return
                
            # Execute trade
            self.active_trades[trade_data['symbol']] = trade_data
            
            # Broadcast trade information
            self.risk_manager.broadcast_trade({
                'action': 'OPEN',
                'trade': trade_data
            })
            
            logger.info(f'New trade executed: {trade_data["symbol"]} {trade_data["direction"]}')
            
        except Exception as e:
            logger.error(f'Error executing trade: {str(e)}')
            
    def calculate_exit_levels(self, trade_type: str, entry_price: float) -> tuple:
        """Calculate stop loss and take profit levels"""
        try:
            if trade_type == 'BUY':
                stop_loss = entry_price * (1 - self.risk_manager.stop_loss_pct)
                take_profit = entry_price * (1 + self.risk_manager.take_profit_pct)
            else:  # SELL
                stop_loss = entry_price * (1 + self.risk_manager.stop_loss_pct)
                take_profit = entry_price * (1 - self.risk_manager.take_profit_pct)

            return stop_loss, take_profit
        except Exception as e:
            logger.error(f"Error calculating exit levels: {str(e)}")
            return None, None

    def check_exit_conditions(self, trade_id: int, current_price: float) -> bool:
        """Check if trade should be closed based on exit conditions"""
        try:
            trade = self.active_trades.get(trade_id)
            if not trade:
                return False

            if trade['direction'] == 'BUY':
                if current_price <= trade['stop_loss'] or current_price >= trade['take_profit']:
                    return self.close_trade(trade_id, current_price)
            else:  # SELL
                if current_price >= trade['stop_loss'] or current_price <= trade['take_profit']:
                    return self.close_trade(trade_id, current_price)

            return False
        except Exception as e:
            logger.error(f"Error checking exit conditions: {str(e)}")
            return False

    def close_trade(self, trade_id: int, exit_price: float) -> bool:
        """Close a trade"""
        conn = None
        try:
            trade = self.active_trades.get(trade_id)
            if not trade:
                return False

            # Calculate profit/loss
            if trade['direction'] == 'BUY':
                profit_loss = (exit_price - trade['entry_price']) * trade['lot_size']
            else:  # SELL
                profit_loss = (trade['entry_price'] - exit_price) * trade['lot_size']

            # Update trade in database
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                UPDATE trades 
                SET status = ?,
                    exit_price = ?,
                    exit_time = ?,
                    profit_loss = ?
                WHERE id = ?
            ''', (
                'closed',
                exit_price,
                datetime.now().isoformat(),
                profit_loss,
                trade_id
            ))

            conn.commit()

            # Update position
            self.risk_manager.update_position(
                trade['user_id'],
                trade['symbol'],
                'sell' if trade['direction'] == 'BUY' else 'buy',
                trade['lot_size'],
                exit_price
            )

            # Remove from active trades
            del self.active_trades[trade_id]

            logger.info(f"Trade closed successfully: {trade_id}")
            return True
        except Exception as e:
            logger.error(f"Error closing trade: {str(e)}")
            return False
        finally:
            if conn:
                conn.close()

    def get_active_trades(self, user_id: int) -> List[Dict]:
        """Get user's active trades"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT * FROM trades 
                WHERE user_id = ? AND status = 'open'
                ORDER BY entry_time DESC
            ''', (user_id,))

            trades = cursor.fetchall()
            return [dict(trade) for trade in trades]
        except Exception as e:
            logger.error(f"Error getting active trades: {str(e)}")
            return []
        finally:
            if conn:
                conn.close()

    def get_trade_history(self, user_id: int, limit: int = 100) -> List[Dict]:
        """Get user's trade history"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT * FROM trades 
                WHERE user_id = ? 
                ORDER BY entry_time DESC 
                LIMIT ?
            ''', (user_id, limit))

            trades = cursor.fetchall()
            return [dict(trade) for trade in trades]
        except Exception as e:
            logger.error(f"Error getting trade history: {str(e)}")
            return []
        finally:
            if conn:
                conn.close() 