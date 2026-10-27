"""Sign in through a real browser window and save the session."""

import json
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from . import config
from .api import parse_json


class LoginError(Exception):
    pass


def normalise_url(text):
    """'myschool' -> https://myschool.instructure.com; keeps custom domains."""
    text = (text or "").strip().rstrip("/")
    if not text:
        raise ValueError("Enter your Canvas address")
    if "://" not in text:
        text = "https://" + text
    u = urlparse(text)
    host = u.hostname
    if not host:
        raise ValueError("That doesn't look like a web address")
    if "." not in host:
        host += ".instructure.com"
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme}://{host}{port}"


def _system_browsers():
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                 "microsoft-edge", "brave-browser", "brave"):
        path = shutil.which(name)
        if path:
            yield path
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser"):
            path = Path(f"/Applications/{app}.app/Contents/MacOS/{app}")
            if path.exists():
                yield str(path)


def _launch(p):
    args = []
    if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        args.append("--ozone-platform=wayland")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")  # Chromium refuses to run as root otherwise
    attempts = []
    if os.environ.get("CANVAS_DL_BROWSER"):
        attempts.append({"executable_path": os.environ["CANVAS_DL_BROWSER"]})
    attempts += [{}, {"channel": "chrome"}, {"channel": "msedge"}]
    attempts += [{"executable_path": b} for b in _system_browsers()]
    last = None
    for kw in attempts:
        try:
            # Playwright's 30s default is too short for a cold start on a busy machine
            return p.chromium.launch(headless=False, args=args, timeout=120_000, **kw)
        except Exception as e:
            last = e
    detail = str(last).splitlines()[0] if last else ""
    raise LoginError(
        "Couldn't start a browser. Install Chrome, Edge or Chromium, or run "
        "`canvas-dl install-browser`, or set CANVAS_DL_BROWSER to a Chromium-based browser."
        + (f"\n({detail})" if detail else ""))


def login(base):
    """Open a browser at the Canvas login page; return the user once signed in."""
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    config.ensure_dir()
    closed = LoginError("The browser was closed before sign-in finished.")
    deadline = time.monotonic() + 15 * 60
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(base + "/login", wait_until="domcontentloaded")
            except PlaywrightError:
                raise LoginError(f"Couldn't open {base}. Check the address.") from None
            while True:
                if page.is_closed() or not browser.is_connected():
                    raise closed
                if time.monotonic() > deadline:
                    raise LoginError("Gave up waiting for sign-in after 15 minutes.")
                try:
                    page.wait_for_timeout(1500)
                    r = ctx.request.get(base + "/api/v1/users/self")
                    me = parse_json(r.text()) if r.ok else None
                except PlaywrightError:
                    raise closed from None
                except ValueError:  # a 200 that isn't Canvas JSON, e.g. a proxy page
                    me = None
                if isinstance(me, dict) and me.get("id"):
                    break
            # keep only Canvas's own cookies, not the school's single sign-on ones
            host = urlparse(base).hostname
            state = ctx.storage_state()
            state["cookies"] = [c for c in state["cookies"]
                                if host == c["domain"].lstrip(".") or host.endswith("." + c["domain"].lstrip("."))]
            state["origins"] = []
            fd = os.open(config.SESSION, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
        finally:
            try:
                browser.close()
            except Exception:
                pass
    try:
        config.SESSION.chmod(0o600)
    except OSError:
        pass
    return me
