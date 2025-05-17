"""
Configuration settings for the trading application.
"""

# API Keys
ALPHA_VANTAGE_API_KEY = "35BDZ47V6D5T4B8G"  # Replace with your premium API key

# API Settings
API_TIMEOUT = 10  # Timeout in seconds for API requests
CACHE_DURATION = 30  # Cache duration in seconds for forex rates

# Premium API Settings
PREMIUM_API_ENABLED = False  # Set to True when using a premium API key
PREMIUM_API_CALLS_PER_MINUTE = 60  # Adjust based on your premium plan 