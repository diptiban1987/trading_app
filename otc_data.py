from alpha_vantage.timeseries import TimeSeries
from alpha_vantage.foreignexchange import ForeignExchange
import pandas as pd
from datetime import datetime, timedelta
import time
from typing import Dict, Optional, Tuple
import os
import requests
import logging
import random
from config import (
    ALPHA_VANTAGE_API_KEY,
    API_TIMEOUT,
    CACHE_DURATION,
    PREMIUM_API_ENABLED,
    PREMIUM_API_CALLS_PER_MINUTE
)

# Configure logging
logger = logging.getLogger(__name__)

class OTCDataHandler:
    def __init__(self, api_key=ALPHA_VANTAGE_API_KEY):
        """
        Initialize OTC data handler with API key.
        
        Args:
            api_key (str): Alpha Vantage API key
        """
        if not api_key or api_key == "YOUR_PREMIUM_API_KEY":
            logger.error("Valid Alpha Vantage API key is required")
            raise ValueError("Valid Alpha Vantage API key is required")
            
        self.api_key = api_key
        self.ts = TimeSeries(key=api_key, output_format='pandas')
        self.fx = ForeignExchange(key=api_key, output_format='pandas')
        self.price_cache = {}
        self.last_api_call = 0
        self.api_call_count = 0
        self.fallback_apis = [
            self._get_fixer_rate,
            self._get_exchangerate_api_rate,
            self._get_mock_rate  # Last resort fallback
        ]
        self._validate_api_key()
        
    def _validate_api_key(self):
        """Validate the API key by making a test request."""
        try:
            logger.info("Validating Alpha Vantage API key")
            url = f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency=EUR&to_currency=USD&apikey={self.api_key}"
            response = requests.get(url, timeout=API_TIMEOUT)
            data = response.json()
            
            if "Error Message" in data:
                logger.error(f"Invalid API key: {data['Error Message']}")
                raise ValueError(f"Invalid API key: {data['Error Message']}")
                
            if "Note" in data:
                if "premium" in data["Note"].lower():
                    if not PREMIUM_API_ENABLED:
                        logger.warning("Premium API features required. Set PREMIUM_API_ENABLED=True in config.py")
                elif "API call frequency" in data["Note"]:
                    logger.warning(f"API rate limit reached: {data['Note']}")
                    
            logger.info("API key validation successful")
                    
        except requests.exceptions.Timeout:
            logger.error("Timeout while validating API key")
            raise ValueError("Timeout while validating API key")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error while validating API key: {str(e)}")
            raise ValueError(f"Request error while validating API key: {str(e)}")
        except Exception as e:
            logger.error(f"Failed to validate API key: {str(e)}")
            raise ValueError(f"Failed to validate API key: {str(e)}")
            
    def get_realtime_price(self, symbol, return_source=False):
        """
        Get real-time price for OTC symbol with fallback sources.
        If return_source is True, return (price, data_source)
        """
        # Check cache first with longer duration due to rate limits
        if symbol in self.price_cache:
            price, timestamp = self.price_cache[symbol]
            if time.time() - timestamp < CACHE_DURATION * 2:
                logger.info(f"Using cached price for {symbol}: {price}")
                if return_source:
                    return price, 'cached'
                return price
        try:
            if symbol.endswith('_OTC'):
                base_pair = symbol.replace('_OTC', '')
                from_currency = base_pair[:3]
                to_currency = base_pair[3:]
                # Try Alpha Vantage first
                price = self._get_alpha_vantage_rate(from_currency, to_currency)
                if price is not None:
                    self.price_cache[symbol] = (price, time.time())
                    if return_source:
                        return price, 'Alpha Vantage'
                    return price
                # If Alpha Vantage fails, try fallback sources
                for fallback in self.fallback_apis:
                    price = fallback(from_currency, to_currency)
                    if price is not None:
                        self.price_cache[symbol] = (price, time.time())
                        if return_source:
                            return price, fallback.__name__.replace('_get_', '').replace('_rate', '').replace('_', ' ').title()
                        return price
            if return_source:
                return None, 'None'
            return None
        except Exception as e:
            logger.error(f"Error getting OTC price for {symbol}: {str(e)}")
            if return_source:
                return None, 'Error'
            return None

    def _get_alpha_vantage_rate(self, from_currency, to_currency):
        """Get rate from Alpha Vantage with rate limit handling."""
        # Check if we're within rate limits
        current_time = time.time()
        if current_time - self.last_api_call < 60:  # Within the same minute
            if self.api_call_count >= 5:  # Free tier limit
                logger.warning("Alpha Vantage rate limit reached, using fallback")
                return None
        else:
            self.api_call_count = 0
            self.last_api_call = current_time
            
        try:
            url = f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency={from_currency}&to_currency={to_currency}&apikey={self.api_key}"
            response = requests.get(url, timeout=API_TIMEOUT)
            data = response.json()
            
            self.api_call_count += 1
            
            if "Realtime Currency Exchange Rate" in data:
                return float(data["Realtime Currency Exchange Rate"]["5. Exchange Rate"])
            elif "Note" in data and "API call frequency" in data["Note"]:
                logger.warning("Alpha Vantage rate limit reached")
                return None
                
        except Exception as e:
            logger.error(f"Alpha Vantage API error: {str(e)}")
            
        return None

    def _get_fixer_rate(self, from_currency, to_currency):
        """Get rate from Fixer.io API."""
        try:
            url = f"https://api.exchangerate.host/latest?base={from_currency}&symbols={to_currency}"
            response = requests.get(url, timeout=API_TIMEOUT)
            data = response.json()
            
            if data.get("success", False) and to_currency in data.get("rates", {}):
                return float(data["rates"][to_currency])
                
        except Exception as e:
            logger.error(f"Fixer API error: {str(e)}")
            
        return None

    def _get_exchangerate_api_rate(self, from_currency, to_currency):
        """Get rate from ExchangeRate-API."""
        try:
            url = f"https://open.er-api.com/v6/latest/{from_currency}"
            response = requests.get(url, timeout=API_TIMEOUT)
            data = response.json()
            
            if data.get("result") == "success" and to_currency in data.get("rates", {}):
                return float(data["rates"][to_currency])
                
        except Exception as e:
            logger.error(f"ExchangeRate-API error: {str(e)}")
            
        return None

    def _get_mock_rate(self, from_currency, to_currency):
        """
        Generate a realistic mock rate based on typical forex rates.
        This is used as a last resort when all other sources fail.
        """
        base_rates = {
            # Major Pairs
            "EURUSD": 1.0922,
            "GBPUSD": 1.2641,
            "USDJPY": 148.12,
            "AUDUSD": 0.6573,
            "USDCHF": 0.8637,
            "USDCAD": 1.3482,
            "EURJPY": 161.78,
            "EURGBP": 0.8641,
            
            # Additional Pairs
            "GBPJPY": 187.23,
            "AUDJPY": 97.45,
            "EURCHF": 0.9432,
            "GBPCHF": 1.0923,
            "AUDCHF": 0.5678,
            "EURCAD": 1.4732,
            "GBPCAD": 1.7045,
            "AUDCAD": 0.8856,
            "EURNZD": 1.8234,
            "GBPNZD": 2.1123,
            "USDNZD": 1.6712,
            "AUDNZD": 1.0987,
            
            # Exotic Pairs
            "EURHKD": 8.5432,
            "GBPHKD": 9.8765,
            "USDHKD": 7.8123,
            "AUDHKD": 5.1234,
            "EURSGD": 1.4567,
            "GBPSGD": 1.6876,
            "USDSGD": 1.3345,
            "AUDSGD": 0.8765,
            "EURMXN": 18.2345,
            "GBPMXN": 21.1234,
            "USDMXN": 16.7123,
            "AUDMXN": 10.9876,
            "EURZAR": 20.1234,
            "GBPZAR": 23.3456,
            "USDZAR": 18.4567,
            "AUDZAR": 12.1234
        }
        
        pair = f"{from_currency}{to_currency}"
        if pair in base_rates:
            # Add small random variation to base rate
            base_rate = base_rates[pair]
            variation = random.uniform(-0.0002, 0.0002)
            return round(base_rate + variation, 6)
            
        return None

    def get_historical_data(self, symbol: str, interval: str = '1min', 
                          output_size: str = 'compact') -> Optional[pd.DataFrame]:
        """Get historical data with fallback to generated data if API fails."""
        try:
            if symbol.endswith('_OTC'):
                base_pair = symbol.replace('_OTC', '')
                from_currency = base_pair[:3]
                to_currency = base_pair[3:]
                
                # Try to get real data first
                data = self._get_alpha_vantage_historical(from_currency, to_currency, interval)
                
                # If real data fails, generate mock historical data
                if data is None:
                    data = self._generate_mock_historical_data(base_pair)
                    
                return data
                
        except Exception as e:
            logger.error(f"Error fetching historical data: {str(e)}")
            return None

    def _generate_mock_historical_data(self, pair: str) -> pd.DataFrame:
        """Generate realistic mock historical data when API fails."""
        base_rates = {
            # Major Pairs
            "EURUSD": 1.0922,
            "GBPUSD": 1.2641,
            "USDJPY": 148.12,
            "AUDUSD": 0.6573,
            "USDCHF": 0.8637,
            "USDCAD": 1.3482,
            "EURJPY": 161.78,
            "EURGBP": 0.8641,
            
            # Additional Pairs
            "GBPJPY": 187.23,
            "AUDJPY": 97.45,
            "EURCHF": 0.9432,
            "GBPCHF": 1.0923,
            "AUDCHF": 0.5678,
            "EURCAD": 1.4732,
            "GBPCAD": 1.7045,
            "AUDCAD": 0.8856,
            "EURNZD": 1.8234,
            "GBPNZD": 2.1123,
            "USDNZD": 1.6712,
            "AUDNZD": 1.0987,
            
            # Exotic Pairs
            "EURHKD": 8.5432,
            "GBPHKD": 9.8765,
            "USDHKD": 7.8123,
            "AUDHKD": 5.1234,
            "EURSGD": 1.4567,
            "GBPSGD": 1.6876,
            "USDSGD": 1.3345,
            "AUDSGD": 0.8765,
            "EURMXN": 18.2345,
            "GBPMXN": 21.1234,
            "USDMXN": 16.7123,
            "AUDMXN": 10.9876,
            "EURZAR": 20.1234,
            "GBPZAR": 23.3456,
            "USDZAR": 18.4567,
            "AUDZAR": 12.1234
        }
        
        if pair not in base_rates:
            return None
            
        base_rate = base_rates[pair]
        now = datetime.now()
        dates = [now - timedelta(minutes=i) for i in range(60)]
        
        data = []
        current_rate = base_rate
        for date in dates:
            # Generate realistic price movement
            variation = random.uniform(-0.0002, 0.0002)
            current_rate += variation
            
            data.append({
                'timestamp': date,
                'open': current_rate,
                'high': current_rate + random.uniform(0, 0.0001),
                'low': current_rate - random.uniform(0, 0.0001),
                'close': current_rate,
                'volume': random.randint(1000, 5000)
            })
            
        return pd.DataFrame(data)

    def calculate_technical_indicators(self, data: pd.DataFrame) -> Dict:
        """
        Calculate technical indicators for the given data
        
        Args:
            data (pd.DataFrame): Price data
            
        Returns:
            Dict: Dictionary of technical indicators
        """
        try:
            # Ensure we have the required columns
            if '4. close' in data.columns:
                close = data['4. close']
            else:
                close = data['close']

            # Calculate indicators
            indicators = {
                'sma_20': close.rolling(window=20).mean(),
                'ema_20': close.ewm(span=20, adjust=False).mean(),
                'rsi': self._calculate_rsi(close),
                'macd': self._calculate_macd(close),
                'bollinger': self._calculate_bollinger_bands(close)
            }

            return indicators

        except Exception as e:
            print(f"Error calculating indicators: {str(e)}")
            return {}

    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """
        Calculate Relative Strength Index
        
        Args:
            prices (pd.Series): Price data
            period (int): RSI period (default: 14)
            
        Returns:
            pd.Series: RSI values
        """
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def _calculate_macd(self, prices: pd.Series) -> Dict[str, pd.Series]:
        """
        Calculate MACD (Moving Average Convergence Divergence)
        
        Args:
            prices (pd.Series): Price data
            
        Returns:
            Dict[str, pd.Series]: MACD and signal line
        """
        exp1 = prices.ewm(span=12, adjust=False).mean()
        exp2 = prices.ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        return {'macd': macd, 'signal': signal}

    def _calculate_bollinger_bands(self, prices: pd.Series, 
                                 period: int = 20, 
                                 std_dev: int = 2) -> Dict[str, pd.Series]:
        """
        Calculate Bollinger Bands
        
        Args:
            prices (pd.Series): Price data
            period (int): Moving average period (default: 20)
            std_dev (int): Number of standard deviations (default: 2)
            
        Returns:
            Dict[str, pd.Series]: Bollinger Bands (middle, upper, lower)
        """
        middle_band = prices.rolling(window=period).mean()
        std = prices.rolling(window=period).std()
        upper_band = middle_band + (std * std_dev)
        lower_band = middle_band - (std * std_dev)
        return {
            'middle': middle_band,
            'upper': upper_band,
            'lower': lower_band
        } 