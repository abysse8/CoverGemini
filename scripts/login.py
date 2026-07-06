#!/usr/bin/env python3
"""Open a VISIBLE browser so Julien can log in to a job platform once.

The session is saved into that platform's persistent CoverAI profile, so afterwards
Helene's headless tools (scan_form / resolve_apply_target) can see past the login wall
and read the real application form.

Usage (run from the CoverGemini repo root):

    python3 scripts/login.py apec
    python3 scripts/login.py linkedin
    python3 scripts/login.py apec linkedin        # both, one window each

Then: log in in the window that opens, and CLOSE the window when the site shows you
as signed in. Repeat per platform. That's the whole job.
"""

from __future__ import annotations

import sys
from pathlib import Path

# allow running as `python3 scripts/login.py` from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coverai.platforms import prepare_login_session  # noqa: E402
from coverai.storage import CoverAiStore  # noqa: E402

KNOWN = ("apec", "linkedin", "welcome_to_the_jungle", "jobteaser")


def main(argv: list[str]) -> int:
    platforms = argv or ["apec", "linkedin"]
    store = CoverAiStore("coverai.db")
    base = Path(".").resolve()
    for pid in platforms:
        try:
            res = prepare_login_session(store, "julien", pid, base, launch=True)
        except KeyError:
            print(f"[skip] unknown platform '{pid}'. Known: {', '.join(KNOWN)}")
            continue
        if res.get("launched"):
            print(f"[open] {pid}: a browser window is opening at {res['login_url']}")
            print(f"       log in, then CLOSE the window. Profile: {res['profile_dir']}")
        else:
            print(f"[warn] {pid}: not launched -> {res.get('status')} {res.get('message','')}")
    print("\nWhen every window is closed after signing in, tell Helene to re-run the scan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
