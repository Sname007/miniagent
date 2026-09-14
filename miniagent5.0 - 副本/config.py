import os

API_KEY = ""
BASE_URL = "https://api.deepseek.com"
MODEL_NAME = "deepseek-flash"
REASONING_EFFORT = "low"

MAX_TOKENS = 1000000
API_TIMEOUT = 600.0

HISTORY_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history_data")
HOST = "127.0.0.1"
PORT = 8999
