from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .storage import DEFAULT_USER_ID, CoverAiStore, utc_now


def playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def absolute_profile_dir(base_dir: str | Path, profile_dir: str) -> Path:
    path = Path(profile_dir)
    if not path.is_absolute():
        path = Path(base_dir) / path
    return path.expanduser().resolve()


def prepare_login_session(
    store: CoverAiStore,
    user_id: str,
    platform_id: str,
    base_dir: str | Path,
    launch: bool = False,
) -> dict[str, Any]:
    account = store.get_user_platform_account(user_id, platform_id)
    profile_path = absolute_profile_dir(base_dir, str(account["profile_dir"]))
    profile_path.mkdir(parents=True, exist_ok=True)
    status = "login_pending" if launch else str(account.get("status") or "not_connected")
    updated = store.update_user_platform_account(
        user_id,
        platform_id,
        profile_dir=str(account["profile_dir"]),
        status=status,
        metadata_json=json.dumps({"profile_dir_absolute": str(profile_path)}, ensure_ascii=False),
    )
    result = {
        "user_id": user_id,
        "platform_id": platform_id,
        "platform_name": updated.get("name", ""),
        "login_url": updated.get("login_url", ""),
        "profile_dir": str(profile_path),
        "status": updated.get("status", ""),
        "launched": False,
        "playwright_available": playwright_available(),
    }
    if launch:
        if not result["playwright_available"]:
            result["status"] = "needs_playwright"
            result["message"] = "Install Playwright and browser binaries before launching a login browser."
            store.update_user_platform_account(user_id, platform_id, status="needs_playwright")
        else:
            subprocess.Popen(
                [sys.executable, "-m", "coverai.platforms", "open-login", str(profile_path), str(updated.get("login_url") or "")],
                cwd=str(Path(base_dir)),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            result["launched"] = True
    return result


def check_platform_session(store: CoverAiStore, user_id: str, platform_id: str, base_dir: str | Path) -> dict[str, Any]:
    account = store.get_user_platform_account(user_id, platform_id)
    profile_path = absolute_profile_dir(base_dir, str(account["profile_dir"]))
    profile_path.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    if not playwright_available():
        updated = store.update_user_platform_account(
            user_id,
            platform_id,
            status="needs_playwright",
            last_login_check_at=now,
            metadata_json=json.dumps({"error": "playwright is not installed"}, ensure_ascii=False),
        )
        return {"account": updated, "ready": False, "reason": "playwright_missing"}

    from playwright.sync_api import sync_playwright

    check_url = str(account.get("base_url") or account.get("login_url") or "")
    login_url = str(account.get("login_url") or "")
    metadata: dict[str, Any] = {"checked_url": check_url}
    status = "unknown"
    ready = False
    reason = "unknown"
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile_path), headless=True)
            page = context.new_page()
            page.goto(check_url or login_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(750)
            current_url = page.url
            title = page.title()
            context.close()
        lowered = f"{current_url} {title}".lower()
        metadata.update({"current_url": current_url, "title": title})
        if any(marker in lowered for marker in ("login", "signin", "sign_in", "connexion", "auth")):
            status = "login_required"
            reason = "login_page_detected"
        else:
            status = "ready"
            ready = True
            reason = "session_reachable"
    except Exception as error:
        status = "check_failed"
        reason = str(error)
        metadata["error"] = str(error)

    updated = store.update_user_platform_account(
        user_id,
        platform_id,
        status=status,
        last_login_check_at=now,
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    return {"account": updated, "ready": ready, "reason": reason}


# A plausible desktop-Chrome user agent. Bundled Chromium otherwise reports
# "HeadlessChrome"/"Chromium", which Google/LinkedIn flag as "browser may not be secure".
STEALTH_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

# navigator.webdriver === true is the single biggest automation tell. Strip it before
# any site script runs.
_STEALTH_INIT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def launch_stealth_context(playwright: Any, profile_dir: str, headless: bool):
    """Persistent context with the obvious automation tells removed.

    Not a defeat-all-detection kit -- just enough that a human can complete a normal
    login (APEC, LinkedIn via email/password) without the "this browser may not be
    secure" block. Google OAuth may still refuse; sign in with the site's own
    credentials rather than "Sign in with Google".
    """
    context = playwright.chromium.launch_persistent_context(
        profile_dir,
        headless=headless,
        user_agent=STEALTH_UA,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )
    context.add_init_script(_STEALTH_INIT)
    return context


def open_login(profile_dir: str, login_url: str) -> None:
    if not playwright_available():
        raise RuntimeError("playwright is not installed")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = launch_stealth_context(playwright, profile_dir, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
        print(f"Login browser open for {login_url}. Close the browser window when finished.", flush=True)
        try:
            while len(context.pages) > 0:
                page.wait_for_timeout(1000)
        finally:
            context.close()


def main() -> None:
    if len(sys.argv) >= 4 and sys.argv[1] == "open-login":
        open_login(sys.argv[2], sys.argv[3])
        return
    raise SystemExit("Usage: python3 -m coverai.platforms open-login <profile_dir> <login_url>")


if __name__ == "__main__":
    main()
