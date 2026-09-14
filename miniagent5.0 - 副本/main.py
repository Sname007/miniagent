import time
import threading
import urllib.request
import urllib.error

import webview
import uvicorn

from config import HOST, PORT
from server import app


def start_server():
    uvicorn.run(app, host=HOST, port=PORT, log_level="error")


def wait_for_server(url, timeout=10, interval=0.3):
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(interval)
    return False


if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()

    if not wait_for_server(f"http://{HOST}:{PORT}"):
        print("警告: 后端服务启动超时，请检查端口是否被占用")

    webview.create_window("Mini Agent", f"http://{HOST}:{PORT}",
                          width=1000, height=700, min_size=(800, 600))
    webview.start()
