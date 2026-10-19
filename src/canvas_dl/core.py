"""Shared steps used by both the menu and the commands."""

from collections import Counter
from pathlib import Path

from rich.markup import escape

from . import config
from .api import Canvas, CanvasError, SessionExpired
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
    known = tracked(cfg).get(cid)
    if known and known.get("folder"):
        return known["folder"]
    folder = safe(course_title(course))
    taken = {r.get("folder") for k, r in cfg["downloads"].items() if k != cid}
    if folder in taken:  # e.g. the same unit from last year
        folder = safe(f"{course_title(course)} ({course.get('course_code') or cid})")
    return folder


def absolute(out):
    """Keep '~/…' as typed (readable in settings); make anything else absolute."""
    out = str(out).strip()
    return out if out.startswith("~") else str(Path(out).resolve())


def tracked(cfg):
    """Downloads that belong to the school we're signed in to."""
    return {cid: r for cid, r in cfg["downloads"].items() if r.get("base", cfg["base"]) == cfg["base"]}


def sync_course(api, cfg, course, module_ids, out, console, workers=None):
    """Download a course (module_ids None = all modules) and remember it for updates.

    Modules downloaded before are always refreshed too, so the course folder and its
    index stay complete."""
    cid = str(course["id"])
    title = course_title(course)
    out = absolute(out)
    known = tracked(cfg).get(cid)
    if known and known.get("out") == out and module_ids:
        if known.get("modules") == "all":
            module_ids = None
        else:
            module_ids = list(dict.fromkeys([*known.get("modules", []), *map(str, module_ids)]))
    root = Path(out).expanduser().resolve() / course_folder(cfg, course)
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
        "base": cfg["base"],
        "name": title,
        "folder": root.name,
        "out": str(out),
        "modules": [str(m) for m in module_ids] if module_ids else "all",
    }
    config.save(cfg)
    return job


def update_all(api, cfg, console, workers=None):
    """Re-sync every tracked course; one failing course doesn't stop the rest."""
    for cid, rec in list(tracked(cfg).items()):
        name = rec.get("name", cid)
        try:
            course = api.course(cid)
            ids = None if rec.get("modules") == "all" else rec.get("modules")
            summary(console, sync_course(api, cfg, course, ids, rec.get("out") or cfg["out"], console, workers))
        except SessionExpired:
            raise
        except CanvasError as e:
            console.print(f"[yellow]Skipping {escape(name)}: {escape(str(e))}[/]")


def summary(console, job):
    def n(count, word):
        return f"{count} {word}{'' if count == 1 else 's'}"
    files = n(job.file_count, "file")
    if job.file_count:
        files += f" ({job.downloaded} new)" if job.downloaded else " (nothing new)"
    console.print(f"[green]✓[/] [bold]{escape(job.title)}[/]  "
                  f"{files}, {n(len(job.docs), 'page')}, {n(len(job.modules), 'module')}")
    console.print(f"  [dim]{escape(str(job.root))}[/]")
    console.print(f"  [dim]Open[/] {escape((job.root / 'index.html').as_uri())} [dim]to browse it offline[/]")
    if job.problems:
        console.print(f"  [yellow]{len(job.problems)} item(s) couldn't be downloaded:[/]")
        for p, n in Counter(job.problems).items():
            console.print(f"   [yellow]·[/] {escape(p)}" + (f" [dim](×{n})[/]" if n > 1 else ""))
