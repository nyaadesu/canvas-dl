"""Command-line entry point: `canvas-dl` opens the menu, subcommands are for scripts."""

import argparse
import re
import sys

from rich.console import Console
from rich.markup import escape

from . import __version__, auth, config, core
from .api import CanvasError, SessionExpired

HELP = """Download your Canvas courses for offline study.
Run with no arguments for the interactive menu."""

EXAMPLES = """examples:
  canvas-dl login myschool.instructure.com
  canvas-dl courses
  canvas-dl get 12345                     all modules of course 12345
  canvas-dl get 12345 -m "week 3" -m 678  modules by name or id
  canvas-dl get https://myschool.instructure.com/courses/12345/modules/678
  canvas-dl update                        re-sync everything downloaded before"""


def course_and_module(arg):
    m = re.search(r"courses/(\d+)(?:/modules/(\d+))?", arg)
    if m:
        return m.group(1), m.group(2)
    if arg.strip().isdigit():
        return arg.strip(), None
    raise SystemExit(f"canvas-dl: can't find a course id in {arg!r} (use the number or the course URL)")


def pick_module(mods, want, cid):
    """Match by id, then exact name, then a name fragment that fits exactly one module."""
    w = want.strip().lower()
    for test in (lambda m: str(m["id"]) == w, lambda m: m["name"].strip().lower() == w,
                 lambda m: w in m["name"].lower()):
        hits = [m for m in mods if test(m)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = "\n".join(f"  {m['id']:>8}  {m['name']}" for m in hits)
            raise SystemExit(f"canvas-dl: {want!r} matches several modules; use the id or more of the name:\n{names}")
    raise SystemExit(f"canvas-dl: no module matches {want!r}. List them with: canvas-dl modules {cid}")


def positive(text):
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def cmd_login(a, cfg, out):
    base = auth.normalise_url(a.url)
    out.print(f"Opening a browser at {escape(base)}. Sign in there; it closes by itself once you're in.")
    with out.status("Waiting for you to sign in… (Ctrl+C to cancel)"):
        me = auth.login(base)
    cfg.update(base=base, user=me.get("name"))
    config.save(cfg)
    out.print(f"[green]✓[/] Signed in as [bold]{escape(me.get('name') or '?')}[/]")


def cmd_logout(a, cfg, out):
    config.sign_out(cfg)
    out.print("Signed out.")


def cmd_courses(a, cfg, out):
    api = core.connect(cfg)
    for c in api.courses(include_old=a.all):
        term = (c.get("term") or {}).get("name") or ""
        print(f"{c['id']:>8}  {c['name']}" + (f"  [{term}]" if term and a.all else ""))


def cmd_modules(a, cfg, out):
    cid, _ = course_and_module(a.course)
    api = core.connect(cfg)
    for m in api.modules(cid):
        print(f"{m['id']:>8}  {m['name']}  ({m.get('items_count', '?')} items)")


def cmd_get(a, cfg, out):
    cid, mid = course_and_module(a.course)
    api = core.connect(cfg)
    course = api.course(cid)
    wanted = list(a.module or []) + ([mid] if mid else [])
    ids = None
    if wanted:
        mods = api.modules(cid)
        ids = [pick_module(mods, w, cid)["id"] for w in wanted]
    job = core.sync_course(api, cfg, course, list(dict.fromkeys(ids)) if ids else None,
                           a.out or cfg["out"], out, a.jobs)
    core.summary(out, job)


def cmd_update(a, cfg, out):
    if not core.tracked(cfg):
        out.print("Nothing downloaded yet. Use `canvas-dl get` or the menu first.")
        return
    core.update_all(core.connect(cfg), cfg, out, a.jobs)


def cmd_install_browser(a, cfg, out):
    import subprocess
    out.print("Installing Playwright's Chromium (used only for signing in)…")
    raise SystemExit(subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="canvas-dl", description=HELP, epilog=EXAMPLES,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-V", "--version", action="version", version=f"canvas-dl {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    s = sub.add_parser("login", help="sign in with a browser window")
    s.add_argument("url", help="your Canvas address, e.g. myschool.instructure.com")
    s.set_defaults(f=cmd_login)
    s = sub.add_parser("logout", help="forget the saved session")
    s.set_defaults(f=cmd_logout)
    s = sub.add_parser("courses", help="list your courses")
    s.add_argument("--all", action="store_true", help="include finished courses")
    s.set_defaults(f=cmd_courses)
    s = sub.add_parser("modules", help="list a course's modules")
    s.add_argument("course", help="course id or URL")
    s.set_defaults(f=cmd_modules)
    s = sub.add_parser("get", help="download a course")
    s.add_argument("course", help="course id or URL (a module URL downloads just that module)")
    s.add_argument("-m", "--module", action="append", metavar="NAME|ID",
                   help="only this module (name fragment or id); repeatable. Default: all")
    s.add_argument("-o", "--out", metavar="DIR", help="download folder (default from settings)")
    s.add_argument("-j", "--jobs", type=positive, metavar="N", help="parallel downloads")
    s.set_defaults(f=cmd_get)
    s = sub.add_parser("update", help="re-sync everything downloaded before")
    s.add_argument("-j", "--jobs", type=positive, metavar="N", help="parallel downloads")
    s.set_defaults(f=cmd_update)

    s = sub.add_parser("install-browser", help="download a browser for signing in, if you have no Chrome/Chromium/Edge")
    s.set_defaults(f=cmd_install_browser)

    a = ap.parse_args(argv)
    if not a.cmd:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            ap.print_help()
            return 2
        from .menu import run
        return run()

    out = Console(stderr=True, highlight=False)
    cfg = config.load()
    try:
        if a.cmd not in ("login", "logout", "install-browser") and not config.signed_in(cfg):
            raise SessionExpired("Not signed in.")
        a.f(a, cfg, out)
    except SessionExpired as e:
        hint = f"canvas-dl login {cfg['base'].split('://', 1)[-1]}" if cfg.get("base") else "canvas-dl login <canvas address>"
        out.print(f"[red]✗[/] {escape(str(e))} Run: {escape(hint)}")
        return 1
    except (auth.LoginError, CanvasError, ValueError) as e:
        out.print(f"[red]✗[/] {escape(str(e))}")
        return 1
    except KeyboardInterrupt:
        out.print("\nCancelled.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
