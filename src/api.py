"""Core API for bridging the GUI and automation routines."""

import os
import re
import sys
import time
import json
import math
import random
import platform
import subprocess
import threading
import webbrowser

# `webview` (pywebview) is imported lazily inside `open_history_window` — the
# only method that needs it — so AutoRewarder_CLI.py can import
# AutoRewarderAPI and run headless without dragging in pywebview or its
# display-layer requirements.

from .config import (
    GUI_DIR,
    REPO,
    CURRENT_VERSION,
    JSON_FILE_PATH,
    BASE_DIR,
    edge_profile_path,
    history_path,
    status_path,
)
from .utils import check_for_updates
from .accounts import (
    AccountManager,
    AccountMetaManager,
    GlobalSettingsManager,
    detect_store_version,
)
from .emulator import DriverManager, HumanBehavior, edge_policy
from .search import HistoryManager, SearchEngine
from .dailytasks import DailySet

# Default wall-clock fire time (24h "HH:MM") if an account schedule does
# not yet have a `run_time` value. Each account stores its own time in
# meta.json; this constant is only the fallback default for fresh accounts.
AUTOSTART_TIME = "09:00"

# Naming prefix for the OS-level scheduled task / systemd unit. Each
# account gets its own task: AutoRewarder.<account_id> on Windows,
# autorewarder-<account_id>.{service,timer} on Linux. The unsuffixed
# names (without account_id) are reserved as legacy markers from the
# previous single-task design and only cleaned up, never created.
_AUTOSTART_TASK_NAME = "AutoRewarder"
_SYSTEMD_UNIT_NAME = "autorewarder"

# HH:MM validator — accepts 00:00..23:59.
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _normalize_run_time(value):
    """Return a valid HH:MM string, falling back to AUTOSTART_TIME."""
    if isinstance(value, str) and _TIME_RE.match(value.strip()):
        return value.strip()
    return AUTOSTART_TIME


class AutoRewarderAPI:
    """
    Core API class for AutoRewarder.

    Bridges the pywebview GUI and the Selenium automation. Multi-account aware:
    the driver, history, daily-set, and meta managers are rebuilt whenever the
    currently-selected account changes.
    """

    def __init__(self):
        self._webview_window = None
        self._driver_loader_thread_started = False
        self._update_check_started = False
        self._driver = None
        self.is_driver_loading = False
        self._run_lock = threading.Lock()
        # Set when the user clicks Stop. Long loops in search_engine and
        # daily_set poll this between iterations and bail out cleanly.
        self._stop_event = threading.Event()

        # Global (app-wide) settings. Per-account data is handled below.
        self.global_settings = GlobalSettingsManager()
        self.hide_browser = bool(
            self.global_settings.get_settings().get("hide_browser", False)
        )

        # Account layer: migration runs here. `account_manager` is the source of
        # truth for the dropdown.
        self.account_manager = AccountManager(
            self.global_settings, logger=self._safe_log
        )
        self.account_manager.migrate_legacy()

        # Per-account managers: rebuilt each time the active account changes.
        self.driver_manager = None
        self.history = None
        self.daily_set = None
        self.account_meta = None
        self.search_engine = None

        self._rebuild_account_context()

        # One-shot migration: lift any pre-existing global schedule (v1 feature)
        # into the per-account meta.json it referenced.
        self._migrate_legacy_global_schedule()

        # One-shot migration: lift any pre-existing fire-on-login autostart
        # (HKCU Run / .desktop) into the new daily scheduled task / systemd
        # timer. Otherwise a previously-enabled autostart would keep opening
        # a visible GUI at every login until the user manually toggles.
        self._migrate_legacy_autostart()

        # Scheduled runs are driven by the OS autostart entry which launches
        # `AutoRewarder.py --headless` → `AutoRewarder_CLI.main()`. No in-app
        # daemon thread.

    # ------------------------------------------------------------------
    # Context lifecycle
    # ------------------------------------------------------------------

    def _rebuild_account_context(self):
        """(Re)build the per-account managers based on the currently-selected account."""
        current_id = self.account_manager.current_id()

        if current_id:
            profile = edge_profile_path(current_id)
            self.account_meta = AccountMetaManager(current_id)
            proxy = self.account_meta.get_proxy()
            self.history = HistoryManager(history_path(current_id), logger=self.log)
            self.daily_set = DailySet(status_path(current_id), logger=self.log)
            self.driver_manager = DriverManager(
                profile_path=profile,
                hide_browser=self.hide_browser,
                proxy_config=proxy,
            )
            self.search_engine = SearchEngine(logger=self.log, history=self.history)
        else:
            self.account_meta = None
            self.history = None
            self.daily_set = None
            self.driver_manager = DriverManager(
                profile_path=None, hide_browser=self.hide_browser, proxy_config=None
            )
            self.search_engine = SearchEngine(logger=self.log, history=None)

    # ------------------------------------------------------------------
    # Webview plumbing
    # ------------------------------------------------------------------

    def set_window(self, window):
        """
        Attach the webview window and start background tasks.

        Args:
            window: The webview window to attach.
        """
        self._webview_window = window
        self.start_update_check()

        if not self._driver_loader_thread_started:
            self._driver_loader_thread_started = True
            threading.Thread(target=self.load_driver_in_background, daemon=True).start()

    def _safe_log(self, message):
        """
        Log wrapper usable before the webview window is attached.

        Args:
            message (str): The message to log.
        """
        if self._webview_window:
            self.log(message)
        else:
            print(message)

    def open_history_window(self):
        """Open the history viewer window."""
        # Local import: pywebview is a GUI-only dependency, kept out of the
        # headless CLI import chain (see comment at top of this module).
        import webview

        webview.create_window(
            title="查询历史",
            url=os.path.join(GUI_DIR, "history.html"),
            js_api=self,
            width=700,
            height=500,
            resizable=True,
            background_color="#0d1117",
            text_select=True,
        )

    def start_update_check(self):
        """Start a one-time background update check."""
        if self._update_check_started:
            return
        self._update_check_started = True
        threading.Thread(target=self.run_update_check, daemon=True).start()

    def run_update_check(self):
        """Check for updates and notify the UI when a newer version exists."""
        try:
            needs_update, latest_version = check_for_updates(logger=self.log)
        except Exception as e:
            self.log(f"[ERROR] 检查更新时出错：{e}")
            return

        if not needs_update or not latest_version:
            return
        if not self._webview_window:
            return

        url = f"https://github.com/{REPO}/releases/latest"
        msg = (
            f"新版本可用：{latest_version}（当前 {CURRENT_VERSION}）。\n"
            f"链接已添加到日志区域。"
            f"请下载最新版本以获得更好的性能并避免 Microsoft 更新可能带来的问题。"
        )

        # Structured call into JS: the text, the link label and the URL are
        # each passed as plain arguments (via json.dumps), and update_log_link
        # builds the <a> element with createElement/textContent — no HTML
        # parsing, so nothing user-controllable can inject markup.
        try:
            self._webview_window.evaluate_js(
                "update_log_link("
                f"{json.dumps(f'新版本 {latest_version} 可用。')}, "
                f"{json.dumps('点击此处下载')}, "
                f"{json.dumps(url)})"
            )
        except Exception as e:
            self.log(f"[ERROR] 显示更新链接时出错：{e}")

        try:
            self._webview_window.evaluate_js(f"alert({json.dumps(msg)})")
        except Exception as e:
            self.log(f"[ERROR] 显示更新提示时出错：{e}")

    def open_link(self, url):
        """Open a URL in the system default browser."""
        webbrowser.open(url)

    def load_driver_in_background(self):
        """Warmup the WebDriver download, only if an account is selected."""
        if self.account_manager.current_id() is None:
            # Nothing to warm up; empty state.
            if self._webview_window:
                self._webview_window.evaluate_js("stop_loader()")
            return

        self.is_driver_loading = True
        try:
            warmup_driver = None
            warmup_driver = self.driver_manager.setup_driver(headless=True)
            warmup_driver.quit()
        except Exception as e:
            self.log(f"[ERROR] 加载 WebDriver 时出错：{e}")
        finally:
            try:
                if self.driver_manager is not None:
                    self.driver_manager.stop_proxy()
            except Exception:
                pass
            self.is_driver_loading = False
            if self._webview_window:
                self._webview_window.evaluate_js("stop_loader()")

    def check_driver_status(self):
        """Return True while the driver warmup thread is active."""
        return self.is_driver_loading

    # ------------------------------------------------------------------
    # Exposed to JS: global settings
    # ------------------------------------------------------------------

    def get_settings(self):
        """Return global settings (hide_browser, current_account_id, schema_version)."""
        return self.global_settings.get_settings()

    def set_hide_browser(self, is_hide):
        """
        Persist and apply the hide-browser setting.

        Args:
            is_hide (bool): True to hide the browser, False to show it.

        Returns:
            bool: True if the setting was successfully updated, False otherwise.
        """
        self.hide_browser = bool(is_hide)
        if self.driver_manager is not None:
            self.driver_manager.hide_browser = bool(is_hide)
        self.global_settings.set_hide_browser(is_hide)
        self.log(f"浏览器隐藏模式：{'开启' if is_hide else '关闭'}")

    def get_close_to_tray(self):
        """Return whether the window X-close should minimize to tray."""
        return bool(self.global_settings.get_settings().get("close_to_tray", True))

    def set_close_to_tray(self, value):
        """
        Persist the close-to-tray setting. Reads at app startup, so a
        change only takes effect on the next launch.

        Args:
            value (bool): True to hide the window on X (close-to-tray),
                False to quit the app entirely on X.
        """
        self.global_settings.set_close_to_tray(value)
        state = "开启（X → 托盘）" if value else "关闭（X → 退出）"
        self.log(f"关闭到托盘：{state}。重启后生效。")

    def get_queries_counts(self):
        """
        Return the saved PC and Mobile query counts from global settings.

        Returns:
            dict: {"queries_pc": int, "queries_mobile": int}
        """
        return {
            "queries_pc": self.global_settings.get_queries_pc(),
            "queries_mobile": self.global_settings.get_queries_mobile(),
        }

    def set_queries_counts(self, queries_pc, queries_mobile):
        """
        Save PC and Mobile query counts to global settings.

        Args:
            queries_pc (int): Number of PC searches.
            queries_mobile (int): Number of mobile searches.

        Returns:
            bool: True if successfully saved, False otherwise.
        """
        try:
            before = (
                self.global_settings.get_queries_pc(),
                self.global_settings.get_queries_mobile(),
            )

            self.global_settings.set_queries_pc(queries_pc)
            self.global_settings.set_queries_mobile(queries_mobile)

            after = (
                self.global_settings.get_queries_pc(),
                self.global_settings.get_queries_mobile(),
            )

            if after != before:
                self.log(f"搜索次数已保存：PC={after[0]}，移动={after[1]}")
            return True
        except Exception as e:
            self.log(f"[WARNING] 保存搜索次数失败：{e}")
            return False

    # ------------------------------------------------------------------
    # Exposed to JS: per-account schedule + startup
    # ------------------------------------------------------------------

    def is_running(self):
        """True when the bot is mid-run. Used by the headless runner to avoid overlap."""
        return self._run_lock.locked()

    def stop(self):
        """
        User-initiated graceful stop.

        Sets the stop flag so cooperating loops (searches, daily set) bail at
        the next checkpoint, and force-quits the active driver to break any
        in-progress Selenium call. The current run thread will exit through
        its normal `finally` cleanup, which re-enables the Start button.
        """
        if not self._run_lock.locked():
            return False

        self.log("已请求停止。正在关闭浏览器…")
        self._stop_event.set()

        try:
            if self._driver is not None:
                self._driver.quit()
        except Exception:
            pass
        return True

    def get_schedule(self, account_id):
        """Return a specific account's schedule (defaults merged in)."""
        if not account_id or not self.account_manager.exists(account_id):
            return None
        return AccountMetaManager(account_id).get_schedule()

    def get_all_schedules(self):
        """Return [{id, label, first_setup_done, schedule}] for the settings modal."""
        result = []
        for acc in self.account_manager.list():
            result.append(
                {
                    "id": acc["id"],
                    "label": acc["label"],
                    "first_setup_done": acc["first_setup_done"],
                    "schedule": AccountMetaManager(acc["id"]).get_schedule(),
                    "proxy": AccountMetaManager(acc["id"]).get_proxy(),
                }
            )
        return result

    def get_account_proxy(self, account_id):
        """Return a specific account's proxy config."""
        if not account_id or not self.account_manager.exists(account_id):
            return None
        return AccountMetaManager(account_id).get_proxy()

    def set_account_proxy(self, account_id, payload):
        """
        Persist a specific account's proxy config.

        Returns:
            bool: True if saved, False if the account/payload is invalid.
        """
        if self._run_lock.locked():
            self.log("[WARNING] 机器人运行时无法更改代理设置。")
            return False
        if not account_id or not self.account_manager.exists(account_id):
            return False
        if not isinstance(payload, dict):
            return False

        try:
            meta = AccountMetaManager(account_id)
            meta.set_proxy(payload)
        except ValueError as e:
            self.log(f"[ERROR] Invalid proxy for account '{account_id}': {e}")
            return False

        if account_id == self.account_manager.current_id():
            if self.driver_manager is not None:
                self.driver_manager.proxy_config = meta.get_proxy()

        label = self.account_manager.get(account_id)
        label = label["label"] if label else account_id
        saved = meta.get_proxy()
        if saved.get("enabled"):
            self.log(
                f"代理已保存到 '{label}'：{saved['scheme']}://{saved['host']}:{saved['port']}"
            )
        else:
            self.log(f"代理已为 '{label}' 禁用。")
        return True

    def detect_store_version(self):
        """
        Detect and persist the current account's Rewards store version.

        Opens a **headless** Edge driver, navigates to `/earn` on
        `rewards.bing.com`, and classifies the account as **old** (404)
        or **new** (page loads normally).  The result is stored in the
        account's `meta.json` and returned so the GUI can display it.

        All automation code is written for the **old** layout; this method
        is purely informational for now so users know which layout their
        account has.  Future code can branch on the stored value.

        Returns:
            dict: `{"store_version": 1|2, "error": None|str}`
        """
        current_id = self.account_manager.current_id()
        if not current_id:
            return {"store_version": None, "error": "未选择账户"}
        if self.account_meta is None:
            return {"store_version": None, "error": "账户元数据不可用"}

        from .emulator import DriverManager

        proxy = self.account_meta.get_proxy()
        profile = edge_profile_path(current_id)
        mgr = DriverManager(
            profile_path=profile,
            hide_browser=True,
            proxy_config=proxy,
        )

        driver = None
        try:
            driver = mgr.setup_driver(headless=True, disable_identity=False)
            self.log("正在检测 Rewards 商店版本...")

            version = detect_store_version(driver)
            self.account_meta.set_store_version(version)

            label = self.account_manager.get(current_id)
            label = label["label"] if label else current_id
            display = "新版" if version == 2 else "旧版"
            self.log(f"账号 '{label}' 的 Rewards 商店版本：{display}")
            return {"store_version": version, "error": None}

        except Exception as e:
            self.log(f"[ERROR] 检测商店版本失败：{e}")
            return {"store_version": None, "error": str(e)}

        finally:
            try:
                if driver is not None:
                    driver.quit()
            except Exception:
                pass
            try:
                mgr.stop_proxy()
            except Exception:
                pass

    def set_schedule(self, account_id, payload):
        """
        Persist the schedule for a specific account.
        `payload` accepts: enabled, advancedScheduling, runDuration (1..24),
        queriesPerHour (1..99), queries_pc (0..130), queries_mobile (0..99),
        run_time (HH:MM 24h). Unknown keys are ignored.

        After persisting, if the global "Start with Windows/Linux" toggle
        is on, the account's OS-level scheduled task is (re)created or
        removed to match the new state.

        Args:
            account_id (str): The ID of the account to set the schedule for.
            payload (dict): The schedule settings to persist.

        Returns:
            bool: True if the schedule was successfully updated, False otherwise.
        """
        if not account_id or not self.account_manager.exists(account_id):
            return False
        if not isinstance(payload, dict):
            return False

        meta = AccountMetaManager(account_id)
        current = meta.get_schedule()

        def _pick(key, default):
            return payload[key] if key in payload else default

        new = {
            "enabled": bool(_pick("enabled", current["enabled"])),
            "advancedScheduling": bool(
                _pick("advancedScheduling", current["advancedScheduling"])
            ),
            "runDuration": max(
                1, min(24, int(_pick("runDuration", current["runDuration"])))
            ),
            "queriesPerHour": max(
                1, min(99, int(_pick("queriesPerHour", current["queriesPerHour"])))
            ),
            "queries_pc": max(
                0, min(130, int(_pick("queries_pc", current["queries_pc"])))
            ),
            "queries_mobile": max(
                0, min(99, int(_pick("queries_mobile", current["queries_mobile"])))
            ),
            "run_time": _normalize_run_time(_pick("run_time", current.get("run_time"))),
            # Reset the daily-dedup marker so the edited schedule can still fire today.
            "last_triggered_date": None,
        }
        meta.set_schedule(new)

        label = self.account_manager.get(account_id)
        label = label["label"] if label else account_id
        if new["enabled"]:
            mode = "高级" if new["advancedScheduling"] else "简单"
            self.log(
                f"计划 '{label}'（{mode}）@ {new['run_time']}："
                f"PC={new['queries_pc']}，移动={new['queries_mobile']}，"
                f"{new['runDuration']}小时 @ {new['queriesPerHour']}/小时"
            )
        else:
            self.log(f"计划 '{label}' 已禁用。")

        # Re-sync the OS-level scheduled task. _sync_account_autostart
        # itself respects the global Start-with-Windows toggle and the
        # new schedule.enabled value, so it correctly creates / updates
        # / removes the task in any state.
        try:
            self._sync_account_autostart(account_id)
        except Exception as e:
            self.log(f"[WARNING] Failed to sync autostart for '{label}': {e}")

        return True

    def _migrate_legacy_global_schedule(self):
        """
        Clean up any pre-existing global `schedule` key left over from an
        earlier version of this branch. The schedule now lives in each
        account's meta.json, so we just drop the global one. Anything
        valuable was already migrated during a previous upgrade cycle.
        """
        settings = self.global_settings.get_settings()
        if "schedule" in settings:
            settings.pop("schedule", None)
            self.global_settings.save_settings(settings)

    def _detect_legacy_autostart(self):
        """
        Return True if any pre-per-account autostart artifact exists on
        this system. Used both by the migration path and by every
        startup so stale legacy entries get cleaned up even when the
        user has already moved to the per-account model.

        Recognised sources:
          * Fire-on-login: HKCU Run (Windows) / .desktop (Linux)
          * Single-task daily scheduler: schtasks `AutoRewarder` /
            systemd `autorewarder.timer` (v3.3 single-task design)
        """
        system = platform.system()
        if system == "Windows":
            try:
                import winreg

                run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ
                ) as key:
                    winreg.QueryValueEx(key, _AUTOSTART_TASK_NAME)
                    return True
            except Exception:
                pass
            try:
                result = subprocess.run(
                    ["schtasks", "/Query", "/TN", _AUTOSTART_TASK_NAME],
                    capture_output=True,
                    creationflags=0x08000000,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass
        elif system == "Linux":
            try:
                if os.path.exists(self._legacy_linux_autostart_path()):
                    return True
            except Exception:
                pass
            try:
                timer_path = os.path.join(
                    self._systemd_user_dir(),
                    f"{_SYSTEMD_UNIT_NAME}.timer",
                )
                if os.path.exists(timer_path):
                    return True
            except Exception:
                pass
        return False

    # Bumped whenever the format of registered scheduled tasks changes in
    # a way that requires re-creating existing tasks on disk:
    #   * v1 switched dev-mode commands from python.exe to pythonw.exe
    #     to avoid the console-window flash.
    #   * v2 switched Windows registration from `schtasks /Create /SC DAILY
    #     /ST HH:MM` (flag form) to `schtasks /Create /XML <file>` so the
    #     resulting task carries StartWhenAvailable=true — without it,
    #     Windows silently skips daily triggers that fired while the
    #     machine was off, unlike systemd's Persistent=true on Linux.
    # Stored in global_settings as `autostart_schema_version`. Users on
    # autoStartUp=True with a lower version get all their tasks re-
    # registered on next launch.
    _AUTOSTART_SCHEMA_VERSION = 2

    def _migrate_legacy_autostart(self):
        """
        Two cleanup paths on every app launch:

        1. Legacy artifact exists → idempotent cleanup. Never auto-
           enables autostart, even if the user previously had it on
           under the old model: their explicit intent is whatever
           `autoStartUp` says today. If they want per-account
           autostart back, they re-toggle Start-with-Windows in
           Settings.
        2. User is on per-account model (autoStartUp=True) AND
           `autostart_schema_version` is below current → re-register
           every task so format changes (e.g. python.exe → pythonw.exe)
           take effect without the user having to toggle anything.

        Failures are logged but swallowed — a stale legacy entry is
        not worth crashing app startup.
        """
        legacy = self._detect_legacy_autostart()
        try:
            settings = self.global_settings.get_settings()
            autostartup = bool(settings.get("autoStartUp", False))
            schema_v = int(settings.get("autostart_schema_version", 0))
        except Exception:
            return

        needs_resync = autostartup and schema_v < self._AUTOSTART_SCHEMA_VERSION

        if not legacy and not needs_resync:
            return

        if legacy:
            self._safe_log("正在清理过时的旧版自动启动条目...")
            try:
                self._cleanup_legacy_autostart()
            except Exception as e:
                self._safe_log(f"[WARNING] Legacy cleanup failed: {e}")

        if needs_resync:
            self._safe_log(
                f"正在刷新各账户计划任务 "
                f"（架构 v{self._AUTOSTART_SCHEMA_VERSION}）..."
            )
            try:
                self._sync_all_autostart()
            except Exception as e:
                self._safe_log(f"[WARNING] Autostart refresh failed: {e}")

        # Mark current schema applied so we don't re-run unnecessarily.
        try:
            settings = self.global_settings.get_settings()
            settings["autostart_schema_version"] = self._AUTOSTART_SCHEMA_VERSION
            self.global_settings.save_settings(settings)
        except Exception:
            pass

    # ---- Autostart (OS-level) — ported from v3.1 main -----------------

    def _autostart_command(self, account_id):
        """
        Command string registered for an account's daily scheduled run.

        Args:
            account_id (str): the account this scheduled task targets.
                Passed to the CLI as `--account <id>` so the headless run
                only processes that account, regardless of which other
                accounts are enabled.

        Returns:
            str: The command to execute for autostart, which varies based on
                whether the app is frozen (packaged) or running in development mode.
        """
        # Frozen build: call the bundled exe with --headless --account <id>.
        # PyInstaller's `console=False` in AutoRewarder.spec means the exe
        # itself has no console, so this fires silently.
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}" --headless --account {account_id}'

        # Dev mode: prefer pythonw.exe on Windows. python.exe is the console
        # variant, so when Task Scheduler fires it Windows allocates a
        # console window — visible flash at every trigger. pythonw.exe is
        # the same interpreter without that console. Falls back to whatever
        # sys.executable is if pythonw isn't there (custom layout).
        python_exe = sys.executable
        if platform.system() == "Windows":
            candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if os.path.exists(candidate):
                python_exe = candidate
        entry = os.path.join(BASE_DIR, "AutoRewarder.py")
        return f'"{python_exe}" "{entry}" --headless --account {account_id}'

    # ---- Per-account OS-task naming -----------------------------------

    def _windows_task_name(self, account_id):
        """schtasks task name for a specific account."""
        return f"{_AUTOSTART_TASK_NAME}.{account_id}"

    def _systemd_unit_base(self, account_id):
        """Base name for the systemd service + timer of a specific account."""
        return f"{_SYSTEMD_UNIT_NAME}-{account_id}"

    # ------------------------------------------------------------------
    # Autostart — daily scheduled task (Windows Task Scheduler / systemd
    # user timer). Replaces the previous "fire on login" model so a daily
    # run still happens even when the machine stays logged in for days.
    # ------------------------------------------------------------------

    def _systemd_user_dir(self):
        return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")

    def _legacy_linux_autostart_path(self):
        """Old .desktop autostart path — kept only for migration cleanup."""
        return os.path.join(
            os.path.expanduser("~"), ".config", "autostart", "AutoRewarder.desktop"
        )

    def _cleanup_legacy_autostart(self):
        """
        Remove pre-per-account autostart entries:
          * HKCU Run / .desktop (the original fire-on-login mechanism)
          * Single-task `AutoRewarder` schtasks / `autorewarder.timer`
            systemd unit (the v3.3 single-task daily scheduler)

        Idempotent — safe to call on every startup. Outcomes are logged
        so that a silent failure can be diagnosed instead of leaving a
        stale task to fire alongside the new per-account ones.
        """
        system = platform.system()
        if system == "Windows":
            # HKCU Run value.
            try:
                import winreg

                run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE
                ) as key:
                    try:
                        winreg.DeleteValue(key, _AUTOSTART_TASK_NAME)
                        self.log("已移除旧版 HKCU 开机自启动项")
                    except FileNotFoundError:
                        pass
            except Exception:
                pass
            # Single-task daily scheduler from the v3.3 design.
            # Only attempt delete if it actually exists, so failures are
            # always meaningful (don't log "delete failed" for tasks
            # that were never there).
            try:
                q = subprocess.run(
                    ["schtasks", "/Query", "/TN", _AUTOSTART_TASK_NAME],
                    capture_output=True,
                    text=True,
                    creationflags=0x08000000,
                )
                if q.returncode == 0:
                    d = subprocess.run(
                        [
                            "schtasks",
                            "/Delete",
                            "/TN",
                            _AUTOSTART_TASK_NAME,
                            "/F",
                        ],
                        capture_output=True,
                        text=True,
                        creationflags=0x08000000,
                    )
                    if d.returncode == 0:
                        self.log("已移除旧版单任务计划程序")
                    else:
                        msg = (d.stderr or d.stdout or "").strip()
                        self.log(
                            f"[WARNING] Could not delete legacy task "
                            f"'{_AUTOSTART_TASK_NAME}': {msg}"
                        )
            except FileNotFoundError:
                # schtasks not on PATH — nothing we can do.
                pass
            except Exception as e:
                self.log(f"[WARNING] Legacy schtasks cleanup error: {e}")
        elif system == "Linux":
            old_desktop = self._legacy_linux_autostart_path()
            if os.path.exists(old_desktop):
                try:
                    os.remove(old_desktop)
                    self.log("已移除旧版 .desktop 自启动项")
                except OSError as e:
                    self.log(f"[WARNING] Could not remove .desktop: {e}")
            # Single-task systemd timer from the v3.3 design.
            base = self._systemd_user_dir()
            old_service = os.path.join(base, f"{_SYSTEMD_UNIT_NAME}.service")
            old_timer = os.path.join(base, f"{_SYSTEMD_UNIT_NAME}.timer")
            if os.path.exists(old_timer) or os.path.exists(old_service):
                try:
                    subprocess.run(
                        [
                            "systemctl",
                            "--user",
                            "disable",
                            "--now",
                            f"{_SYSTEMD_UNIT_NAME}.timer",
                        ],
                        capture_output=True,
                    )
                except Exception:
                    pass
                removed = False
                for path in (old_service, old_timer):
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                            removed = True
                        except OSError as e:
                            self.log(f"[WARNING] Could not remove {path}: {e}")
                if removed:
                    self.log("已移除旧版单任务 systemd 定时器")
                try:
                    subprocess.run(
                        ["systemctl", "--user", "daemon-reload"],
                        capture_output=True,
                    )
                except Exception:
                    pass

    # ---- Per-account OS-task management -------------------------------

    def _autostart_exec_and_args(self, account_id):
        """
        Split the autostart command into (executable, arguments) for the
        Task Scheduler XML Action element, which expects them separately.
        Mirrors the same dev-vs-frozen logic as _autostart_command.
        """
        if getattr(sys, "frozen", False):
            return sys.executable, f"--headless --account {account_id}"

        python_exe = sys.executable
        candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(candidate):
            python_exe = candidate
        entry = os.path.join(BASE_DIR, "AutoRewarder.py")
        return python_exe, f'"{entry}" --headless --account {account_id}'

    def _build_windows_task_xml(self, account_id, run_time, label):
        """
        Build a Task Scheduler 1.2 XML for a daily run.

        Key setting: <StartWhenAvailable>true</StartWhenAvailable>. Without
        it, Windows silently skips a trigger that fired while the machine
        was off (unlike systemd's Persistent=true). With it, the task
        runs as soon as possible after the missed time at the next boot —
        matching the Linux behavior.

        Also: <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
        so laptop users on battery still get their run.
        """
        executable, arguments = self._autostart_exec_and_args(account_id)

        def esc(s):
            return (
                s.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&apos;")
            )

        description = f"AutoRewarder daily run ({label})"
        # Past anchor date — only the HH:MM portion of StartBoundary
        # matters for DaysInterval=1 recurrence.
        start_boundary = f"2025-01-01T{run_time}:00"

        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            "  <RegistrationInfo>\n"
            f"    <Description>{esc(description)}</Description>\n"
            "  </RegistrationInfo>\n"
            "  <Triggers>\n"
            "    <CalendarTrigger>\n"
            f"      <StartBoundary>{start_boundary}</StartBoundary>\n"
            "      <Enabled>true</Enabled>\n"
            "      <ScheduleByDay>\n"
            "        <DaysInterval>1</DaysInterval>\n"
            "      </ScheduleByDay>\n"
            "    </CalendarTrigger>\n"
            "  </Triggers>\n"
            "  <Principals>\n"
            '    <Principal id="Author">\n'
            "      <LogonType>InteractiveToken</LogonType>\n"
            "      <RunLevel>LeastPrivilege</RunLevel>\n"
            "    </Principal>\n"
            "  </Principals>\n"
            "  <Settings>\n"
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
            "    <AllowHardTerminate>true</AllowHardTerminate>\n"
            "    <StartWhenAvailable>true</StartWhenAvailable>\n"
            "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
            "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
            "    <Enabled>true</Enabled>\n"
            "    <Hidden>false</Hidden>\n"
            "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
            "    <WakeToRun>false</WakeToRun>\n"
            "    <ExecutionTimeLimit>PT72H</ExecutionTimeLimit>\n"
            "    <Priority>7</Priority>\n"
            "  </Settings>\n"
            '  <Actions Context="Author">\n'
            "    <Exec>\n"
            f"      <Command>{esc(executable)}</Command>\n"
            f"      <Arguments>{esc(arguments)}</Arguments>\n"
            "    </Exec>\n"
            "  </Actions>\n"
            "</Task>\n"
        )

    def _register_windows_task(self, account_id, run_time, label=None):
        """
        Register a daily scheduled task via XML import.

        Why XML instead of `schtasks /SC DAILY /ST HH:MM` flags: the
        flag form doesn't expose StartWhenAvailable, so a trigger that
        fires while the machine is off is silently skipped forever.
        The XML form lets us flip StartWhenAvailable=true so a missed
        trigger catches up at next boot — same behavior as systemd's
        Persistent=true on the Linux side.
        """
        import tempfile

        xml_body = self._build_windows_task_xml(
            account_id, run_time, label or account_id
        )

        # schtasks /XML reads the task definition from disk; UTF-16 is
        # the encoding Task Scheduler expects (the XML decl says so and
        # schtasks refuses UTF-8 without a BOM on some Windows builds).
        fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix="autorewarder-task-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(xml_body.encode("utf-16"))

            result = subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    self._windows_task_name(account_id),
                    "/XML",
                    xml_path,
                    "/F",
                ],
                capture_output=True,
                text=True,
                creationflags=0x08000000,
            )
            if result.returncode != 0:
                self.log(
                    f"[ERROR] schtasks create failed for {label or account_id}: "
                    f"{(result.stderr or result.stdout).strip()}"
                )
                return False
            self.log(
                f"计划任务已注册：'{label or account_id}' 于 {run_time}"
            )
            return True
        except FileNotFoundError:
            self.log("[ERROR] schtasks not found — Task Scheduler unavailable.")
            return False
        except Exception as e:
            self.log(f"[ERROR] Failed to register Windows task: {e}")
            return False
        finally:
            try:
                os.remove(xml_path)
            except OSError:
                pass

    def _remove_windows_task(self, account_id):
        """schtasks /Delete an account's daily task (idempotent)."""
        try:
            subprocess.run(
                [
                    "schtasks",
                    "/Delete",
                    "/TN",
                    self._windows_task_name(account_id),
                    "/F",
                ],
                capture_output=True,
                creationflags=0x08000000,
            )
            return True
        except Exception:
            return False

    def _register_systemd_unit(self, account_id, run_time, label=None):
        """Write + enable a per-account systemd .service + .timer."""
        try:
            base = self._systemd_user_dir()
            unit_base = self._systemd_unit_base(account_id)
            service_path = os.path.join(base, f"{unit_base}.service")
            timer_path = os.path.join(base, f"{unit_base}.timer")
            timer_unit = f"{unit_base}.timer"

            os.makedirs(base, exist_ok=True)
            cmd = self._autostart_command(account_id)
            desc_label = label or account_id
            service_file = (
                "[Unit]\n"
                f"Description=AutoRewarder daily run ({desc_label})\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                f"ExecStart={cmd}\n"
            )
            timer_file = (
                "[Unit]\n"
                f"Description=Run AutoRewarder daily ({desc_label})\n\n"
                "[Timer]\n"
                f"OnCalendar=*-*-* {run_time}:00\n"
                "Persistent=true\n"
                f"Unit={unit_base}.service\n\n"
                "[Install]\n"
                "WantedBy=timers.target\n"
            )
            with open(service_path, "w", encoding="utf-8") as fh:
                fh.write(service_file)
            with open(timer_path, "w", encoding="utf-8") as fh:
                fh.write(timer_file)

            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], capture_output=True
            )
            result = subprocess.run(
                ["systemctl", "--user", "enable", "--now", timer_unit],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.log(
                    f"[ERROR] systemctl enable failed for {desc_label}: "
                    f"{(result.stderr or result.stdout).strip()}"
                )
                return False
            self.log(f"计划定时器已注册：'{desc_label}' 于 {run_time}")
            return True
        except FileNotFoundError:
            self.log("[ERROR] systemctl not found — systemd unavailable.")
            return False
        except Exception as e:
            self.log(f"[ERROR] Failed to register systemd timer: {e}")
            return False

    def _remove_systemd_unit(self, account_id):
        """Disable + delete an account's systemd service + timer."""
        try:
            base = self._systemd_user_dir()
            unit_base = self._systemd_unit_base(account_id)
            service_path = os.path.join(base, f"{unit_base}.service")
            timer_path = os.path.join(base, f"{unit_base}.timer")
            timer_unit = f"{unit_base}.timer"

            subprocess.run(
                ["systemctl", "--user", "disable", "--now", timer_unit],
                capture_output=True,
            )
            for path in (service_path, timer_path):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], capture_output=True
            )
            return True
        except Exception:
            return False

    def _remove_account_autostart(self, account_id):
        """Remove an account's OS-level scheduled task (platform-aware)."""
        system = platform.system()
        if system == "Windows":
            return self._remove_windows_task(account_id)
        if system == "Linux":
            return self._remove_systemd_unit(account_id)
        return False

    def _sync_account_autostart(self, account_id):
        """
        Bring one account's OS-level scheduled task in sync with its
        meta.json schedule. Called whenever set_schedule mutates an
        account, or the global Start-with-Windows toggle is flipped on.

        Semantics:
          * Global toggle OFF → always remove (regardless of schedule.enabled)
          * Account doesn't exist anymore → remove
          * schedule.enabled = False → remove
          * Otherwise → register at schedule.run_time
        """
        if not self.is_autostart_enabled():
            self._remove_account_autostart(account_id)
            return False

        if not self.account_manager.exists(account_id):
            self._remove_account_autostart(account_id)
            return False

        meta = AccountMetaManager(account_id)
        sched = meta.get_schedule()
        if not sched.get("enabled"):
            self._remove_account_autostart(account_id)
            return True

        run_time = _normalize_run_time(sched.get("run_time"))
        acc = self.account_manager.get(account_id)
        label = acc["label"] if acc else account_id

        system = platform.system()
        if system == "Windows":
            return self._register_windows_task(account_id, run_time, label)
        if system == "Linux":
            return self._register_systemd_unit(account_id, run_time, label)
        self.log("自动启动仅在 Windows 和 Linux 上支持。")
        return False

    def _sync_all_autostart(self):
        """Iterate every account and re-sync its scheduled task."""
        for acc in self.account_manager.list():
            try:
                self._sync_account_autostart(acc["id"])
            except Exception as e:
                self.log(f"[WARNING] Sync failed for {acc.get('label')}: {e}")

    def _set_autostart_registry(self, enable):
        """
        Global autostart master toggle. Persists the user's intent in
        global settings (`autoStartUp`) and syncs every account's OS-level
        scheduled task to match.

          * enable=True  → autoStartUp=True; create a task for each
                           account whose schedule.enabled=True at its
                           own schedule.run_time.
          * enable=False → autoStartUp=False; remove every per-account
                           task that we might have registered.

        Legacy entries (HKCU Run, .desktop, single-task daily scheduler)
        are always cleaned up on either path.
        """
        system_name = platform.system()
        if system_name not in ("Windows", "Linux"):
            self.log("自动启动仅在 Windows 和 Linux 上支持。")
            return False

        # Persist user intent FIRST so _sync_account_autostart reads the
        # new value when it queries is_autostart_enabled().
        settings = self.global_settings.get_settings()
        settings["autoStartUp"] = bool(enable)
        self.global_settings.save_settings(settings)

        # Always clean up legacy single-task / fire-on-login entries.
        self._cleanup_legacy_autostart()

        if not enable:
            # Remove every per-account task that might have been registered.
            for acc in self.account_manager.list():
                self._remove_account_autostart(acc["id"])
            self.log("自动启动已禁用（所有账户任务已移除）")
            return True

        # Enable path: register tasks for every account with schedule.enabled.
        self._sync_all_autostart()
        self.log("自动启动已启用（各账户计划任务已注册）")
        return True

    def is_autostart_enabled(self):
        """Return True if the global 'Start with Windows/Linux' toggle is on.

        Per-account OS tasks are derived from this AND each account's
        schedule.enabled — the toggle here is the master switch.
        """
        try:
            return bool(self.global_settings.get_settings().get("autoStartUp", False))
        except Exception:
            return False

    def get_launch_on_startup(self):
        """Return OS support flag + current autostart state for the Settings UI."""
        system_name = platform.system()
        return {
            "supported": system_name in ("Windows", "Linux"),
            "enabled": self.is_autostart_enabled(),
        }

    def set_launch_on_startup(self, enabled):
        """
        Register or unregister the OS autostart entry. Called from JS.

        Args:
            enabled (bool): True to enable autostart, False to disable.

        Returns:
            bool: True if the operation succeeded, False otherwise.
        """
        ok = self._set_autostart_registry(bool(enabled))
        if ok:
            # Mirror the state into global settings.json for the UI.
            settings = self.global_settings.get_settings()
            settings["autoStartUp"] = bool(enabled)
            self.global_settings.save_settings(settings)
        return ok

    # ------------------------------------------------------------------
    # Exposed to JS: accounts
    # ------------------------------------------------------------------

    def list_accounts(self):
        """Return accounts for UI display."""
        return self.account_manager.list()

    def get_current_account(self):
        """Return the currently selected account, or None."""
        return self.account_manager.get_current()

    def create_account(self, label, proxy_payload=None):
        """
        Create a new account, select it, and run First Setup against it.
        On setup failure (user closes browser without logging in), rolls back
        and restores the previously-selected account.

        Args:
            label (str): The user-friendly label for the new account.
            proxy_payload (dict | None): Optional proxy config to save before
                First Setup so setup also uses the account proxy.

        Returns:
            dict: {ok (bool), id (str), label (str)} on success, or {ok: False, error: str} on failure.
        """
        if self._run_lock.locked():
            self.log("[WARNING] 机器人运行时无法添加账户。")
            return {"ok": False, "error": "bot_running"}

        previous_id = self.account_manager.current_id()
        new_account = self.account_manager.create(label)
        new_id = new_account["id"]

        self.account_manager.select(new_id)
        self._rebuild_account_context()

        if proxy_payload is not None and self.account_meta is not None:
            try:
                self.account_meta.set_proxy(proxy_payload)
                if self.driver_manager is not None:
                    self.driver_manager.proxy_config = self.account_meta.get_proxy()
            except ValueError as e:
                self.log(f"[ERROR] Invalid proxy for new account: {e}")
                self.account_manager.delete(new_id)
                self.account_manager.select(previous_id)
                self._rebuild_account_context()
                self._broadcast_account_ui()
                return {"ok": False, "error": "invalid_proxy", "id": new_id}

        self._broadcast_account_ui()

        success = self._run_first_setup_for_current()

        if not success:
            # Rollback: drop the new account and restore previous.
            self.account_manager.delete(new_id)
            self.account_manager.select(previous_id)
            self._rebuild_account_context()
            self._broadcast_account_ui()
            return {"ok": False, "error": "setup_failed", "id": new_id}

        return {"ok": True, "id": new_id, "label": new_account["label"]}

    def switch_account(self, account_id):
        """
        Switch to the specified account if possible.

        Args:
            account_id (str): The ID of the account to switch to.

        Returns:
            bool: True if switching succeeded, False otherwise.
        """
        if self._run_lock.locked():
            self.log("[WARNING] 机器人运行时无法切换账户。")
            return False
        if not self.account_manager.exists(account_id):
            self.log(f"[ERROR] 未知账户：{account_id}")
            return False

        self.account_manager.select(account_id)
        self._rebuild_account_context()
        current = self.account_manager.get_current()
        if current:
            self.log(f"已切换到账户 '{current['label']}'。")
        self._broadcast_account_ui()
        return True

    def rename_account(self, account_id, new_label):
        """
        Rename an account label.

        Args:
            account_id (str): The ID of the account to rename.
            new_label (str): The new label for the account.

        Returns:
            bool: True if renaming succeeded, False otherwise.
        """
        try:
            self.account_manager.rename(account_id, new_label)
        except ValueError as e:
            self.log(f"[ERROR] {e}")
            return False
        self._broadcast_account_ui()
        return True

    def delete_account(self, account_id):
        """
        Delete an account and refresh the UI.

        Args:
            account_id (str): The ID of the account to delete.

        Returns:
            bool: True if deletion succeeded, False if the account is active or on error.
        """
        if self._run_lock.locked() and account_id == self.account_manager.current_id():
            self.log(
                "[WARNING] 机器人运行时无法删除当前账户。"
            )
            return False

        # Tear down the account's OS-level scheduled task BEFORE deletion
        # so the task name (which embeds the account_id) is still
        # resolvable. Idempotent — no-op if no task was registered.
        try:
            self._remove_account_autostart(account_id)
        except Exception as e:
            self.log(f"[WARNING] Failed to remove scheduled task: {e}")

        try:
            self.account_manager.delete(account_id)
        except ValueError as e:
            self.log(f"[ERROR] {e}")
            return False

        self._rebuild_account_context()
        self._broadcast_account_ui()
        return True

    def rerun_setup(self, account_id):
        """
        Re-run First Setup for an existing account (e.g. profile got corrupted).
        Temporarily switches to it if not current, then restores previous.

        Args:
            account_id (str): The ID of the account to run setup for.

        Returns:
            bool: True if setup succeeded, False on failure or if the bot is running.
        """
        if self._run_lock.locked():
            self.log("[WARNING] 机器人运行时无法重新运行设置。")
            return False
        if not self.account_manager.exists(account_id):
            return False

        previous_id = self.account_manager.current_id()
        if account_id != previous_id:
            self.account_manager.select(account_id)
            self._rebuild_account_context()
            self._broadcast_account_ui()

        ok = self._run_first_setup_for_current()

        if account_id != previous_id:
            self.account_manager.select(previous_id)
            self._rebuild_account_context()
            self._broadcast_account_ui()

        return ok

    # ------------------------------------------------------------------
    # First setup flow (scoped to the currently-active account)
    # ------------------------------------------------------------------

    def _run_first_setup_for_current(self):
        """
        Open Bing in a visible Edge window for the user to log in manually.
        Returns True on success (browser closed after login attempt), False on error.

        On Windows, temporarily disables the browser-level Microsoft sign-in
        policy (BrowserSignin=0) so Edge does not silently authenticate using
        the Windows account identity. The previous policy value is restored
        when setup ends, regardless of outcome.
        """
        if self.driver_manager is None or self.account_meta is None:
            self.log("[ERROR] 未选择用于设置的账户。")
            return False

        current = self.account_manager.get_current()
        label = current["label"] if current else "account"
        self.log(
            f"正在为 '{label}' 开始首次设置... 请登录您的 Microsoft 账户。"
        )

        # Capture current policy state so we can restore it afterwards.
        previous_policy = edge_policy.get_current_values()
        previous_user_data_dir = edge_policy.get_user_data_dir()
        policy_applied = False
        user_data_dir_applied = False
        if edge_policy.is_supported():
            policy_applied = edge_policy.set_identity_isolation_enabled(True)
            user_data_dir_applied = edge_policy.set_user_data_dir(
                self.driver_manager.profile_path
            )
            if policy_applied:
                self.log("Edge：本次设置已临时禁用系统登录。")
            if user_data_dir_applied:
                self.log("Edge：本次设置已强制指定账户配置文件目录。")

        setup_succeeded = False
        setup_driver = None

        try:
            setup_driver = self.driver_manager.setup_driver(
                headless=False, disable_identity=True
            )
        except Exception as e:
            self.log(f"[ERROR] 无法启动浏览器：{e}")
            if policy_applied:
                edge_policy.restore_values(previous_policy)
            if user_data_dir_applied:
                edge_policy.restore_user_data_dir(previous_user_data_dir)
            return False

        try:
            # Windows WAM can silently push an MSA identity even on a fresh
            # profile. Before showing anything to the user, wipe every bit of
            # state that could carry an identity forward (cookies, cache,
            # storage) via the DevTools protocol.
            self.log("正在清除缓存的 Microsoft 身份信息...")
            try:
                setup_driver.get("about:blank")
                time.sleep(0.5)
                setup_driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
                setup_driver.execute_cdp_cmd("Network.clearBrowserCache", {})
            except Exception:
                pass

            # Explicit logout at the Microsoft endpoint, then re-clear cookies
            # in case the logout page dropped new ones.
            try:
                setup_driver.get(
                    "https://login.live.com/logout.srf?wa=wsignout1.0&ct=0&rver=7.0"
                )
                time.sleep(3)
                try:
                    setup_driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
                except Exception:
                    pass
            except Exception:
                pass

            # Force the Microsoft sign-in form with prompt=login. This is an
            # OAuth2 parameter that forces re-authentication no matter what
            # cached/WAM session exists. The wreply sends the user back to
            # Bing after a successful sign-in.
            self.log("正在打开 Microsoft 登录页面...")
            try:
                setup_driver.get(
                    "https://login.live.com/login.srf?"
                    "wa=wsignin1.0&"
                    "rpsnv=13&"
                    "ct=0&"
                    "rver=7.0&"
                    "wp=MBI_SSL&"
                    "wreply=https%3a%2f%2fwww.bing.com%2f&"
                    "lc=1033&"
                    "id=264960&"
                    "mkt=en-us&"
                    "prompt=login"
                )
            except Exception:
                # Fallback if the forced-prompt URL fails.
                setup_driver.get("https://login.live.com/")

            self.log("""请使用与此配置文件对应的 Microsoft 账户登录。
- 请手动输入邮箱和密码，不要选择建议的账户。
- 如果 Microsoft 仍然自动连接了其他账户，请点击头像
  （Bing 右上角）并选择"使用其他账户登录"。
- 完成后关闭浏览器。""")

            while len(setup_driver.window_handles) > 0:
                time.sleep(1)

            setup_succeeded = True

        except Exception as e:
            error_msg = str(e).lower()
            if (
                "target window already closed" in error_msg
                or "disconnected" in error_msg
                or "not reachable" in error_msg
            ):
                setup_succeeded = True
            else:
                self.log(f"[ERROR] 设置过程中出错：{e}")
                if self.history is not None:
                    self.history.add_to_history(
                        "首次设置失败", "[ERROR] " + str(e)[:50]
                    )

        finally:
            try:
                setup_driver.quit()
            except Exception:
                pass
            try:
                self.driver_manager.stop_proxy()
            except Exception:
                pass

            # Always restore the Edge policy to its previous state.
            if policy_applied:
                edge_policy.restore_values(previous_policy)
            if user_data_dir_applied:
                edge_policy.restore_user_data_dir(previous_user_data_dir)

            if setup_succeeded:
                self.log(
                    f"'{label}' 的首次设置已完成！您现在可以启动机器人了。"
                )
                self.account_meta.mark_up_as_done()
                if self.history is not None:
                    self.history.add_to_history("首次设置完成", "成功")

        return setup_succeeded

    # ------------------------------------------------------------------
    # History (scoped to current account)
    # ------------------------------------------------------------------

    def get_history(self):
        """Return the current account query history."""
        if self.history is None:
            return []
        return self.history.get_history()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log(self, message):
        """
        Log to the GUI when attached; otherwise to stdout.

        Args:
            message (str): The message to log.
        """
        if self._webview_window:
            try:
                safe_message = json.dumps(message)
                self._webview_window.evaluate_js(f"update_log({safe_message})")
            except Exception as e:
                print(f"Log error: {e}")
        else:
            print(message)

    def _broadcast_account_ui(self):
        """Ask the GUI to refresh the account dropdown and setup state."""
        if self._webview_window:
            try:
                self._webview_window.evaluate_js("refresh_account_ui()")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def _sleep_with_stop(self, seconds):
        """
        Sleep up to `seconds`, but return early if Stop was requested.

        Args:
            seconds (float): The number of seconds to sleep.

        Returns:
            bool: True if stop was requested during the wait, else False.
        """
        try:
            return self._stop_event.wait(timeout=float(seconds))
        except Exception:
            time.sleep(seconds)
            return self._stop_event.is_set()

    def _run_advanced_schedule(
        self, pc_count, mobile_count, duration_hours, queries_per_hour
    ):
        """
        Drip-feed queries across a duration using the GUI run pipeline.

        Args:
            pc_count (int): total PC queries to run
            mobile_count (int): total Mobile queries to run
            duration_hours (float|int): how many hours to spread the queries across
            queries_per_hour (int): target queries per hour (overrides duration_hours if > 0)
        """
        try:
            pc = max(0, int(pc_count or 0))
        except (TypeError, ValueError):
            pc = 0
        try:
            mobile = max(0, int(mobile_count or 0))
        except (TypeError, ValueError):
            mobile = 0

        try:
            duration_hours = float(duration_hours)
        except (TypeError, ValueError):
            duration_hours = 3.0

        duration_hours = max(1.0, duration_hours)

        try:
            qph = int(queries_per_hour or 0)
        except (TypeError, ValueError):
            qph = 0

        if qph < 0:
            qph = 0

        total = pc + mobile
        self.log(
            f"高级调度：PC={pc}，移动={mobile}，{duration_hours}小时（qph={qph}）"
        )

        if total <= 0:
            self.log("[WARNING] 无需执行任何操作（PC 和移动查询数均为 0）。")
            return

        if qph > 0:
            raw_batch = qph // 6  # ~10-minute batches
        else:
            raw_batch = total // max(1, int(duration_hours * 2))
        per_batch = max(1, min(10, raw_batch))

        num_batches = math.ceil(total / per_batch)
        total_seconds = duration_hours * 3600
        interval = total_seconds / max(num_batches, 1)

        self.log(
            f"计划 {num_batches} 批，每批 ~{per_batch} 次查询，间隔 ~{interval:.1f}秒"
        )

        pc_left = pc
        mobile_left = mobile

        for i in range(num_batches):
            if self._stop_event.is_set():
                break

            if pc_left > 0:
                batch_pc = min(per_batch, pc_left)
                batch_mobile = 0
            else:
                batch_pc = 0
                batch_mobile = min(per_batch, mobile_left)

            if batch_pc == 0 and batch_mobile == 0:
                break

            self.log(
                f"批次 {i+1}/{num_batches}：PC={batch_pc}，移动={batch_mobile} "
                f"（PC 剩余 {pc_left}，移动剩余 {mobile_left}）"
            )

            if batch_pc > 0 and not self._stop_event.is_set():
                self._run_phase(mobile=False, count=batch_pc, do_daily_set=True)

            if batch_mobile > 0 and not self._stop_event.is_set():
                self._run_phase(mobile=True, count=batch_mobile, do_daily_set=False)

            pc_left -= batch_pc
            mobile_left -= batch_mobile

            if pc_left <= 0 and mobile_left <= 0:
                break
            if self._stop_event.is_set():
                break

            sleep_time = max(5.0, interval * random.uniform(0.75, 1.25))
            self.log(f"休眠 {sleep_time:.1f}秒，直到下一批次")
            if self._sleep_with_stop(sleep_time):
                break

        if not self._stop_event.is_set() and pc_left <= 0 and mobile_left <= 0:
            self.log("高级调度完成！")

    def main(self, pc_count, mobile_count=0, daily_only=False):
        """
        Run the bot against the currently-selected account.

        Default mode (daily_only=False): runs sequentially
          1. PC phase     — desktop UA, `pc_count` Bing searches, then Daily Set.
          2. Mobile phase — iPhone UA, `mobile_count` Bing searches only.
        Either count may be 0 to skip that phase.

        Daily-only mode (daily_only=True): skips searches entirely and only
        opens a desktop driver to run the Daily Set + More Activities. Both
        count arguments are ignored. Useful when the user just wants to
        collect today's daily-task points without churning searches.

        Args:
            pc_count (int): how many searches to do in the PC phase (ignored if daily_only)
            mobile_count (int): how many searches to do in the Mobile phase (ignored if daily_only)
            daily_only (bool): whether to skip searches and just run the Daily Set
        """
        if self.account_manager.current_id() is None:
            self.log("[ERROR] 未选择账户。请通过下拉菜单添加一个。")
            if self._webview_window:
                self._webview_window.evaluate_js("enable_start_button()")
            return

        if self.account_meta is None or not self.account_meta.is_first_setup_done():
            self.log("[ERROR] 此账户尚未完成首次设置。")
            if self._webview_window:
                self._webview_window.evaluate_js("enable_start_button()")
            return

        daily_only = bool(daily_only)

        try:
            pc_count = max(0, int(pc_count or 0))
            mobile_count = max(0, int(mobile_count or 0))
        except (TypeError, ValueError):
            pc_count, mobile_count = 0, 0

        if not daily_only and pc_count == 0 and mobile_count == 0:
            self.log("[WARNING] 无需执行任何操作（PC 和移动查询数均为 0）。")
            if self._webview_window:
                self._webview_window.evaluate_js("enable_start_button()")
            return

        schedule = {}
        if not daily_only and self.account_meta is not None:
            try:
                schedule = self.account_meta.get_schedule() or {}
            except Exception:
                schedule = {}

        schedule_enabled = isinstance(schedule, dict) and bool(schedule.get("enabled"))
        use_advanced = (
            not daily_only
            and schedule_enabled
            and bool(schedule.get("advancedScheduling"))
        )

        if (
            not daily_only
            and isinstance(schedule, dict)
            and bool(schedule.get("advancedScheduling"))
            and not schedule_enabled
        ):
            self.log(
                "[WARNING] 高级调度已启用，但计划开关未开启。将按正常速度运行。"
            )

        if not self._run_lock.acquire(blocking=False):
            self.log("[WARNING] 已有运行正在进行中。")
            return

        # Reset stop flag before each run so a previous Stop doesn't carry over.
        self._stop_event.clear()

        try:
            if daily_only:
                self.log("正在启动 AutoRewarder（仅每日任务）...")
            else:
                self.log("正在启动 AutoRewarder（Edge 完整版）...")
            if self._webview_window:
                try:
                    self._webview_window.evaluate_js(
                        "update_status_indicator && update_status_indicator('executing')"
                    )
                except Exception:
                    pass

            if daily_only:
                self._run_daily_only()
            else:
                if use_advanced:
                    duration = schedule.get("runDuration", 3)
                    qph = schedule.get("queriesPerHour", 10)
                    self.log("高级调度已启用。使用计划节奏。")
                    self._run_advanced_schedule(pc_count, mobile_count, duration, qph)
                else:
                    if pc_count > 0 and not self._stop_event.is_set():
                        self._run_phase(mobile=False, count=pc_count, do_daily_set=True)

                    if mobile_count > 0 and not self._stop_event.is_set():
                        self._run_phase(
                            mobile=True, count=mobile_count, do_daily_set=False
                        )

            if self._stop_event.is_set():
                self.log("已停止。")
            else:
                self.log("完成！")

                if self.account_meta is not None:
                    try:
                        from datetime import date

                        current_schedule = self.account_meta.get_schedule()
                        if isinstance(current_schedule, dict):
                            current_schedule["last_triggered_date"] = (
                                date.today().isoformat()
                            )
                            self.account_meta.set_schedule(current_schedule)
                    except Exception as e:
                        self.log(f"[WARNING] 更新去重日期失败：{e}")
        finally:
            try:
                if self._webview_window:
                    self._webview_window.evaluate_js("enable_start_button()")
            except Exception:
                pass
            self._run_lock.release()

    def _run_daily_only(self):
        """
        Open a PC driver, run only the Daily Set + More Activities, scrape
        the points balance, then quit. No Bing searches are performed.

        Unlike the normal flow, this path is user-initiated (explicit toggle)
        so it ignores `should_perform_daily_set()` — if the saved status says
        "done today" but the user clicked Start anyway, they want it to run.
        The card-level detection inside perform_daily_set will skip cards
        that are genuinely complete, so re-running on a real already-done day
        just confirms state without wasting clicks.
        """
        if self.daily_set is None:
            self.log("[ERROR] 此账户的每日任务不可用。")
            return

        if not self.daily_set.should_perform_daily_set():
            self.log(
                "注意：status.json 中今天已标记为完成，"
                "但因为是您手动点击运行，将继续执行。"
            )

        self.log("=== 仅每日任务 — 不执行搜索 ===")

        self._driver = self.driver_manager.setup_driver(mobile=False)
        try:
            human = HumanBehavior(self._driver, show_cursor=True, mobile=False)
            success = self.daily_set.perform_daily_set(
                self._driver, human, stop_event=self._stop_event
            )
            if self._stop_event.is_set():
                self.log("每日任务已被停止。")
                return
            if success:
                self.daily_set.mark_as_completed()
                self.log("每日任务已完成并标记为今日完成。")
            else:
                self.log("每日任务失败。未标记为今日完成。")

        finally:
            try:
                self._driver.quit()
            except Exception as e:
                self.log(f"[WARNING] 关闭驱动时出错：{e}")
            try:
                self.driver_manager.stop_proxy()
            except Exception:
                pass
            self._driver = None
            time.sleep(0.5)

    def _run_phase(self, mobile, count, do_daily_set):
        """
        Open a driver for a single phase (PC or Mobile), do `count` searches,
        optionally run the Daily Set, then quit.

        Args:
            mobile (bool): whether this is the Mobile phase (True) or PC phase (False)
            count (int): how many searches to perform in this phase
            do_daily_set (bool): whether to run the Daily Set after searches (PC phase only)
        """
        label = "移动端" if mobile else "PC"
        self.log(f"=== {label} 阶段 — {count} 次查询 ===")

        queries_to_search = self.search_engine.load_queries_from_json(
            JSON_FILE_PATH, num_needed=count
        )
        if not queries_to_search:
            self.log(f"[WARNING] {label}：没有可用查询。跳过此阶段。")
            if self.history is not None:
                self.history.add_to_history(
                    "N/A", f"[ERROR] {label}：没有可用查询"
                )
            return

        self._driver = self.driver_manager.setup_driver(mobile=mobile)
        try:
            self.search_engine.perform_searches(
                self._driver,
                queries_to_search,
                mobile=mobile,
                stop_event=self._stop_event,
            )

            if (
                do_daily_set
                and not self._stop_event.is_set()
                and self.daily_set.should_perform_daily_set()
            ):
                self.log("今天尚未完成每日任务。正在开始每日任务...")
                human = HumanBehavior(self._driver, show_cursor=True, mobile=mobile)
                success = self.daily_set.perform_daily_set(
                    self._driver, human, stop_event=self._stop_event
                )
                if not self._stop_event.is_set():
                    if success:
                        self.daily_set.mark_as_completed()
                        self.log(
                            "每日任务已完成并标记为今日完成。"
                        )
                    else:
                        self.log("每日任务失败。未标记为今日完成。")

        finally:
            try:
                self._driver.quit()
            except Exception as e:
                self.log(f"[WARNING] 关闭驱动时出错：{e}")
            try:
                self.driver_manager.stop_proxy()
            except Exception:
                pass
            self._driver = None
            time.sleep(0.5)
