"""A small read-only Canvas REST API client that uses a saved browser session."""

import json
import re
from urllib.parse import urlparse

import requests

from . import __version__, config


class CanvasError(Exception):
    pass


class SessionExpired(CanvasError):
    pass


class Forbidden(CanvasError):
    pass


def parse_json(text):
    # Canvas prefixes JSON with this when the request is authenticated by cookie
    if text.startswith("while(1);"):
        text = text[len("while(1);"):]
    return json.loads(text)


class Canvas:
    def __init__(self, base, session_file=None):
        session_file = session_file or config.SESSION
        if not session_file.exists():
            raise SessionExpired("Not signed in.")
        self.base = base.rstrip("/")
        self.host = urlparse(self.base).hostname
        self.http = requests.Session()
        self.http.headers["User-Agent"] = f"canvas-dl/{__version__}"
        state = json.loads(session_file.read_text(encoding="utf-8"))
        for c in state.get("cookies", []):
            self.http.cookies.set(c["name"], c["value"], domain=c["domain"],
                                  path=c.get("path", "/"), secure=c.get("secure", False))

    def get(self, path, params=None):
        url = path if path.startswith("http") else f"{self.base}/api/v1{path}"
        try:
            r = self.http.get(url, params=params, timeout=60)
        except requests.RequestException as e:
            raise CanvasError(f"network error ({e.__class__.__name__})") from e
        if r.status_code == 401:
            # Canvas also uses 401 for "you can't see this", not only for logged out
            if "not authorized" in r.text:
                raise Forbidden("not accessible to students (401)")
            raise SessionExpired("Your Canvas session has expired.")
        if r.status_code == 403:
            raise Forbidden("not accessible to students (403)")
        if r.status_code == 404:
            raise CanvasError("not found (404)")
        if not r.ok:
            raise CanvasError(f"Canvas returned {r.status_code}")
        try:
            return r, parse_json(r.text)
        except ValueError:
            raise CanvasError("Canvas sent something that isn't JSON") from None

    def all(self, path, params=None):
        """GET every page of a paginated list."""
        params = dict(params or {}, per_page=100)
        out, url = [], path
        while url:
            r, data = self.get(url, params)
            out += data
            params = None  # the next link carries them
            m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("link", ""))
            url = m.group(1) if m and urlparse(m.group(1)).hostname == self.host else None
        return out

    def me(self):
        return self.get("/users/self")[1]

    def course(self, cid):
        return self.get(f"/courses/{cid}", {"include[]": "term"})[1]

    def courses(self, include_old=False):
        params = {"include[]": "term"}
        if include_old:
            params["state[]"] = ["available", "completed"]
        else:
            params["enrollment_state"] = "active"
        return [c for c in self.all("/courses", params) if c.get("name")]

    def modules(self, cid):
        return self.all(f"/courses/{cid}/modules", {"include[]": "items"})

    def download(self, url, dest, progress=lambda n: None):
        part = dest.with_name(dest.name + ".part")
        try:
            with self.http.get(url, stream=True, timeout=60) as r:
                if r.status_code in (401, 403):
                    raise Forbidden(f"not accessible ({r.status_code})")
                if not r.ok:
                    raise CanvasError(f"download failed ({r.status_code})")
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        progress(len(chunk))
            part.replace(dest)
        except requests.RequestException as e:
            raise CanvasError(f"network error ({e.__class__.__name__})") from e
        finally:
            if part.exists():
                part.unlink()
