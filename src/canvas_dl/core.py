"""Shared steps used by both the menu and the commands."""

from pathlib import Path

from rich.markup import escape

from . import config
from .api import Canvas
from .download import CourseDownload, course_title, safe


def connect(cfg):
    """A Canvas client for the saved session. Raises SessionExpired."""
    api = Canvas(cfg["base"])
    me = api.me()
    if cfg.get("user") != me.get("name"):
        cfg["user"] = me.get("name")
        config.save(cfg)
    return api


def course_folder(cfg, course):
    cid = str(course["id"])
    known = cfg["downloads"].get(cid)
    if known and known.get("folder"):
        return known["folder"]
    folder = safe(course_title(course))
    taken = {r.get("folder") for k, r in cfg["downloads"].items() if k != cid}
    if folder in taken:  # e.g. the same unit from last year
        folder = safe(f"{course_title(course)} ({course.get('course_code') or cid})")
    return folder


def sync_course(api, cfg, course, module_ids, out, console, workers=None):
    """Download a course (module_ids None = all modules) and remember it for updates."""
    cid = str(course["id"])
    title = course_title(course)
    root = Path(out).expanduser() / course_folder(cfg, course)
    with console.status(f"Reading {escape(title)}…") as status:
        mods = api.modules(cid)
        if module_ids:
            wanted = {str(m) for m in module_ids}
            mods = [m for m in mods if str(m["id"]) in wanted]
        job = CourseDownload(api, course, root)
        job.plan(mods, lambda name: status.update(f"Reading {escape(title)} · {escape(name)}"))
        job.write()
    job.fetch(console, workers or cfg.get("workers", 4))
    cfg["downloads"][cid] = {
        "name": title,
        "folder": root.name,
        "out": str(out),
        "modules": [str(m) for m in module_ids] if module_ids else "all",
    }
    config.save(cfg)
    return job


def summary(console, job):
    pages = len(job.docs)
    new = f"{job.downloaded} new" if job.downloaded else "nothing new"
    console.print(f"[green]✓[/] [bold]{escape(job.title)}[/]  "
                  f"{job.file_count} files ({new}), {pages} pages, {len(job.modules)} modules")
    console.print(f"  [dim]{escape(str(job.root))}[/]")
    console.print(f"  [dim]Open[/] {escape((job.root / 'index.html').as_uri())} [dim]to browse it offline[/]")
    if job.problems:
        console.print(f"  [yellow]{len(job.problems)} item(s) couldn't be downloaded:[/]")
        for p in job.problems:
            console.print(f"   [yellow]·[/] {escape(p)}")
