import os
from dotenv import load_dotenv

load_dotenv()

# ===========================
# Telegram API Credentials
# ===========================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

# ===========================
# Bot Token
# ===========================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# ===========================
# Channels
# ===========================

# Example:
# @CoinTelegraph
# or -1001234567890

SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")

# Example:
# -100xxxxxxxxxx

TARGET_CHAT = int(os.getenv("TARGET_CHAT"))

# ===========================
# Files
# ===========================

REPLACE_FILE = "replace.json"

# ===========================
# Logging
# ===========================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ===========================
# Retry
# ===========================

MAX_RETRIES = 5
RECONNECT_DELAY = 5

# ===========================
# Duplicate Protection
# ===========================

MESSAGE_CACHE_SIZE = 100