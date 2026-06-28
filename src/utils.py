"""Shared utility helpers for AutoRewarder."""

import time
import random
import requests

from .config import CURRENT_VERSION, REPO


def human_typing(element, text):
    """
    Simulate human-like typing by sending keys to a web element with random delays.

    Args:
        element: The web element to send keys to.
        text: The text to type into the element.
    """

    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.18))


def check_for_updates(logger=None):
    """
    Check GitHub API for the latest release and compare it to the current version.

    Args:
        logger (callable, optional): A function to log messages. Defaults to None.

    Returns:
        tuple: (is_update_available (bool), latest_version (str or None))
    """
    try:
        headers = {"User-Agent": "AutoRewarder-App"}

        response = requests.get(
            f"https://api.github.com/repos/{REPO}/releases/latest",
            headers=headers,
            timeout=5,
        )
        if response.status_code == 200:
            latest = response.json().get("tag_name")
            if latest:
                return latest != CURRENT_VERSION, latest
        elif response.status_code == 429:
            if logger:
                logger("[WARNING] GitHub API 速率限制已达上限 (429)。")
                logger("请稍后重试或手动检查更新。")
        elif response.status_code == 403:

            is_rate_limit = response.headers.get("X-Ratelimit-Remaining") == "0"

            if logger:
                if is_rate_limit:
                    logger(
                        "[WARNING] GitHub API 速率限制已超限 (403)。请稍后重试。"
                    )
                else:
                    logger(
                        "[WARNING] GitHub 访问被拒绝 (403)。请检查您的 VPN 或网络连接。"
                    )
        else:
            if logger:
                logger(
                    f"[WARNING] GitHub 更新检查失败。状态码：{response.status_code}"
                )

    except requests.exceptions.RequestException as e:
        if logger:
            logger(f"[WARNING] 检查更新时网络错误：{e}")
    except Exception as e:
        if logger:
            logger(f"[ERROR] 检查更新时意外错误：{e}")

    return False, None
