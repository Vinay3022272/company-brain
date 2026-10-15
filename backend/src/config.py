"""
config.py — Global configuration loader for Company Brain.
"""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Root directory of backend
BACKEND_DIR = Path(__file__).resolve().parent.parent

# Load .env file
load_dotenv(BACKEND_DIR / ".env")

# Paths
DATA_DIR = Path(os.getenv("DATA_DIR", BACKEND_DIR / "data"))
if not DATA_DIR.is_absolute():
    DATA_DIR = BACKEND_DIR / DATA_DIR
    
    
# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("company_brain")