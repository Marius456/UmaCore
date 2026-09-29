"""
Configuration settings for the Umamusume Discord Bot
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Discord Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# Database Configuration
DATABASE_URL = os.getenv("DATABASE_URL")

# Scraping Configuration
SCRAPE_TIMEOUT = 90  # seconds
SCRAPE_RETRY_ATTEMPTS = 3
SCRAPE_RETRY_DELAY = 1  # seconds

# Uma.moe API Configuration
UMAMOE_API_KEY = os.getenv("UMAMOE_API_KEY", "")

# Playwright Configuration (for Cloudflare-bypassed scraping)
PLAYWRIGHT_COOKIE_DIR = os.getenv("PLAYWRIGHT_COOKIE_DIR", ".umamoe_cookies")

# Official Events Scraper Configuration
EVENTS_JSON_PATH = os.getenv("EVENTS_JSON_PATH", "data/events.json")

# Timezone Configuration
TIMEZONE = "Europe/Amsterdam"  # CEST
DAILY_REPORT_TIME = "16:00"

# Quota Rules
DAILY_QUOTA = 1_000_000

# Internal API server (web UI integration)
BOT_API_PORT = int(os.getenv("BOT_API_PORT", "7890"))

# Optional independent image Worker. Both URL and key are required to enable it.
HORSE_IMAGE_PROXY_URL = os.getenv("HORSE_IMAGE_PROXY_URL", "")
HORSE_IMAGE_PROXY_KEY = os.getenv("HORSE_IMAGE_PROXY_KEY", "")
HORSE_IMAGE_ALLOWED_HOSTS = os.getenv(
    "HORSE_IMAGE_ALLOWED_HOSTS",
    "assets.st-note.com,cdn.netkeiba.com,dir.netkeiba.com,i.daily.jp,"
    "jbpress.ismcdn.jp,jra-van.jp,jra.jp,meiba.jp,number.ismcdn.jp,"
    "pbs.twimg.com,stat.ameba.jp,static.wikia.nocookie.net,tospo-keiba.jp,"
    "uma-furi.com,upload.wikimedia.org,www.meiba.jp",
)

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = "bot.log"

# Discord Embed Colors
COLOR_ON_TRACK = 0x00FF00  # Green
COLOR_BEHIND = 0xFFA500     # Orange
COLOR_INFO = 0x3498db       # Blue
