from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable

from .explorer import run_offer_explorer
from .storage import DEFAULT_USER_ID, CoverAiStore, utc_now


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class OfferAutomationRunner:
    def __init__(
        self,
        store: CoverAiStore,
        config_path: str | Path,
        openai_client_getter: Callable[[], Any],
        model_getter: Callable[[], str],
        sms_client_factory: Callable[[], Any],
        logger: Callable[[str], None] | None = None,
        user_id: str = DEFAULT_USER_ID,
        enabled: bool | None = None,
        interval_seconds: int | None = None,
        run_on_start: bool | None = None,
    ) -> None:
        self.store = store
        self.config_path = Path(config_path)
        self.openai_client_getter = openai_client_getter
        self.model_getter = model_getter
        self.sms_client_factory = sms_client_factory
        self.logger = logger or (lambda _message: None)
        self.user_id = user_id
        self.enabled = env_bool("COVERAI_AUTOMATION_ENABLED", True) if enabled is None else enabled
        self.interval_seconds = max(60, interval_seconds if interval_seconds is not None else env_int("COVERAI_AUTOMATION_INTERVAL_SECONDS", 900))
        self.run_on_start = env_bool("COVERAI_AUTOMATION_RUN_ON_START", False) if run_on_start is None else run_on_start
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._started_at = ""
        self._completed_at = ""
        self._last_trigger = ""
        self._last_result: dict[str, Any] | None = None
        self._last_error = ""

    def start(self) -> dict[str, Any]:
        if not self.enabled:
            return self.status()
        if self._thread and self._thread.is_alive():
            return self.status()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="coverai-offer-automation", daemon=True)
        self._thread.start()
        self.logger(f"CoverAI automation started: every {self.interval_seconds}s")
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        return self.status()

    def _loop(self) -> None:
        if self.run_on_start:
            self.run_once("startup")
        while not self._stop_event.wait(self.interval_seconds):
            self.run_once("scheduled")

    def run_async(self, trigger: str = "manual") -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"started": False, "reason": "already_running", "automation": self.status()}
        with self._state_lock:
            self._running = True
            self._started_at = utc_now()
            self._completed_at = ""
            self._last_trigger = trigger
            self._last_error = ""
        thread = threading.Thread(target=self._run_async_locked, args=(trigger,), name=f"coverai-run-{trigger}", daemon=True)
        thread.start()
        return {"started": True, "trigger": trigger, "automation": self.status()}

    def _run_async_locked(self, trigger: str) -> None:
        try:
            self._execute_locked(trigger)
        finally:
            self._run_lock.release()

    def run_once(self, trigger: str = "manual") -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"ok": False, "skipped": "already_running", "automation": self.status()}
        try:
            return self._execute_locked(trigger)
        finally:
            self._run_lock.release()

    def _execute_locked(self, trigger: str) -> dict[str, Any]:
        started_at = utc_now()
        with self._state_lock:
            self._running = True
            self._started_at = started_at
            self._completed_at = ""
            self._last_trigger = trigger
            self._last_error = ""
        try:
            self.logger(f"CoverAI automation run started ({trigger})")
            result = run_offer_explorer(
                self.store,
                self.config_path,
                openai_client=self.openai_client_getter(),
                model=self.model_getter(),
                sms_client=self.sms_client_factory(),
                user_id=self.user_id,
            )
            completed_at = utc_now()
            error = str(result.get("error") or "")
            with self._state_lock:
                self._running = False
                self._completed_at = completed_at
                self._last_result = result
                self._last_error = error
            run = result.get("run") if isinstance(result.get("run"), dict) else {}
            self.logger(
                "CoverAI automation run finished "
                f"({trigger}): status={run.get('status')} new={run.get('offers_new')} reported={run.get('offers_reported')}"
            )
            return {"ok": not error, "trigger": trigger, "result": result, "automation": self.status()}
        except Exception as error:
            completed_at = utc_now()
            with self._state_lock:
                self._running = False
                self._completed_at = completed_at
                self._last_error = str(error)
            self.logger(f"CoverAI automation run failed ({trigger}): {error}")
            return {"ok": False, "trigger": trigger, "error": str(error), "automation": self.status()}

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            latest_run = None
            if self._last_result and isinstance(self._last_result.get("run"), dict):
                latest_run = self._last_result.get("run")
            return {
                "enabled": self.enabled,
                "interval_seconds": self.interval_seconds,
                "running": self._running,
                "started_at": self._started_at,
                "completed_at": self._completed_at,
                "last_trigger": self._last_trigger,
                "last_error": self._last_error,
                "last_run": latest_run,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
            }
