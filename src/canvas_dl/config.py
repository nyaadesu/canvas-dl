"""Settings and session storage."""

import json
import os
from pathlib import Path

from platformdirs import user_config_dir

CONF_DIR = Path(os.environ.get("CANVAS_DL_HOME") or user_config_dir("canvas-dl", appauthor=False))
SESSION = CONF_DIR / "session.json"
CONFIG = CONF_DIR / "config.json"

DEFAULTS = {
    "base": None,       # https://school.instructure.com
    "user": None,       # display name of the signed-in user
    "out": "~/Canvas",  # download folder
    "workers": 4,       # parallel downloads
    "show_old": False,  # list finished courses too
    "downloads": {},    # course id -> {name, folder, out, modules}
}


def ensure_dir():
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONF_DIR.chmod(0o700)
    except OSError:
        pass


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
    except OSError:
        saved = {}
    except ValueError:  # corrupt: keep a copy rather than overwrite it on the next save
        try:
            CONFIG.replace(CONFIG.with_name("config.json.bad"))
        except OSError:
            pass
        saved = {}
    if isinstance(saved, dict):
        for key, default in DEFAULTS.items():  # ignore values of the wrong type
            if key in saved and (default is None or isinstance(saved[key], type(default))):
                cfg[key] = saved[key]
    return cfg


def save(cfg):
    ensure_dir()
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG)


def signed_in(cfg):
    return bool(cfg.get("base")) and SESSION.exists()


def sign_out(cfg):
    try:
        SESSION.unlink()
    except FileNotFoundError:
        pass
    cfg["user"] = None
    save(cfg)
