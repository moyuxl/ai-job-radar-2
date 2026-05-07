"""
DrissionPage 启动选项：降低本机「主页被劫持为 hao123」等对自动化的影响，
并避免落在默认调试端口 9222（无监听时即报「浏览器连接失败」）。

说明：ChromiumOptions(read_file=False) 的内存默认 address 即为 127.0.0.1:9222。
若仅调用 auto_port(True) 在某些环境下未生效，仍会连 9222。此处改为显式挑选空闲端口并绑定独立用户目录。
"""
from __future__ import annotations

import logging
import os
import socket
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def chromium_options_for_automation(headless: bool = False):
    """生成用于爬取的 ChromiumOptions（独立端口 + 干净用户目录 + 可选浏览器路径）。"""
    from DrissionPage import ChromiumOptions

    co = ChromiumOptions(read_file=False)

    path = (os.getenv("DRISSION_BROWSER_PATH") or os.getenv("CHROME_PATH") or "").strip()
    if path:
        co.set_browser_path(path)
        logger.info("DrissionPage 使用环境变量指定的浏览器可执行文件: %s", path)

    if headless:
        co.headless(True)

    port = _pick_free_port()
    profile_root = Path(tempfile.gettempdir()) / "ai-job-radar2-dp-profiles"
    profile_root.mkdir(parents=True, exist_ok=True)
    user_data = profile_root / f"profile_{port}"
    user_data.mkdir(parents=True, exist_ok=True)

    co.set_local_port(port)
    co.set_user_data_path(str(user_data))

    # 弱化主页劫持（键名因内核版本而异，失败则忽略）
    for arg, value in (
        ("homepage", "about:blank"),
        ("browser.startup.homepage", "about:blank"),
        ("browser.startup.restore_on_startup", 5),
    ):
        try:
            co.set_pref(arg, value)
        except Exception:
            pass

    logger.info("DrissionPage 调试端口 %s，用户目录 %s", port, user_data)
    return co
