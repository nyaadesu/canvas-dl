"""Turn a course's modules into a folder you can study from offline."""

from __future__ import annotations

import html
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Union
from urllib.parse import quote, unquote, urlparse

from rich.progress import (BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn,
                           TimeRemainingColumn, TransferSpeedColumn)

from .api import CanvasError

ATTR = re.compile(r"""(\b(?:href|src)\s*=\s*)(["'])(.*?)\2""", re.I | re.S)
EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")
MANIFEST = ".canvas-dl.json"
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
PLAN_ERRORS = (CanvasError, KeyError, TypeError, ValueError)  # odd or missing fields from Canvas


def _clip(text, limit):
    """Cut to at most `limit` UTF-8 bytes (filesystems count bytes, not characters)."""
    data = text.encode("utf-8")
    return text if len(data) <= limit else data[:limit].decode("utf-8", "ignore")


def safe(name, limit=150, keep_ext=True):
    """Make a string usable as a file or folder name on any OS, keeping its extension."""
    name = re.sub(r"\s*:\s+", " - ", str(name))  # "LO1: Basics" -> "LO1 - Basics"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    ext = EXT.search(name) if keep_ext else None
    ext = ext.group(0) if ext else ""
    stem = name[:len(name) - len(ext)] if ext else name
    name = (_clip(stem, limit - len(ext.encode())).rstrip(" .") or "untitled") + ext
    if name.split(".")[0].upper() in RESERVED:  # Windows can't create these
        name = "_" + name
    return name


def with_ext(name, filename):
    """Add the real file's extension to a display name that lacks it."""
    ext = EXT.search(filename or "")
    if ext and not name.lower().endswith(ext.group(0).lower()):
        name += ext.group(0)
    return name


def course_title(course):
    """'Networking Tech ( NETTECH-HNC-O261 )' -> 'Networking Tech'."""
    name = course.get("name") or str(course.get("id"))
    return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip() or name


def fmt_date(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    return f"{dt:%a} {dt.day} {dt:%b %Y, %H:%M}"


def timestamp(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def fmt_number(x):
    return int(x) if isinstance(x, float) and x.is_integer() else x


def rel(target, start):
    return quote(os.path.relpath(target, start).replace(os.sep, "/"))


class _Cancelled(Exception):
    pass


@dataclass
class FileJob:
    fid: str
    name: str
    url: str
    dest: Path
    size: int
    modified: Optional[float] = None


@dataclass
class Entry:
    kind: str                              # header, file, page, assignment, quiz, discussion, link
    title: str
    target: Union[Path, str, None] = None  # local Path, or a URL
    note: str = ""


@dataclass
class Doc:
    path: Path
    title: str
    kind: str
    body: str
    facts: list = field(default_factory=list)
    url: Optional[str] = None


class CourseDownload:
    def __init__(self, api, course, root):
        self.api, self.course, self.root = api, course, Path(root)
        self.cid = str(course["id"])
        self.title = course_title(course)
        self.jobs: dict[str, Optional[FileJob]] = {}
        self.local: dict[tuple, Path] = {}  # (kind, key) of Canvas URLs we saved -> local path
        self.used: set[str] = set()
        self.docs: list[Doc] = []
        self.modules: list[tuple[str, Path, list[Entry]]] = []
        self.problems: list[str] = []
        self.downloaded = 0
        self.up_to_date = 0
        try:
            self.manifest = json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))
            if not isinstance(self.manifest, dict):
                raise ValueError
        except (OSError, ValueError):
            self.manifest = {}
        self.old_files = self.manifest.get("files") or {}  # file id -> path, from last time
        self.owner = {os.path.normcase(p): fid for fid, p in self.old_files.items()}

    # ---- planning: ask Canvas what's there, decide where it goes ----

    def plan(self, modules, status=lambda text: None):
        for m in modules:
            status(m["name"])
            folder = self._unique(self.root / safe(m["name"], keep_ext=False))
            items = m.get("items")
            if items is None:  # Canvas leaves them out for very large modules
                items = self.api.all(f"/courses/{self.cid}/modules/{m['id']}/items")
            entries, n = [], 0
            for it in items:
                kind, title = it.get("type"), it.get("title") or "Untitled"
                if kind == "SubHeader":
                    entries.append(Entry("header", title))
                    continue
                n += 1
                try:
                    entry = self._item(it, kind, title, folder, f"{n:02d} - ")
                except PLAN_ERRORS as e:
                    why = f"Canvas left out '{e.args[0]}'" if isinstance(e, KeyError) else str(e)
                    self.problems.append(f"{title}: {why}")
                    entry = Entry("link", title, it.get("html_url"), "not available")
                if isinstance(entry.target, Path) and it.get("id"):
                    self.local[("item", str(it["id"]))] = entry.target
                entries.append(entry)
            self.modules.append((m["name"], folder, entries))

    def _item(self, it, kind, title, folder, pre):
        api, cid, content = self.api, self.cid, it.get("content_id")
        if kind == "File":
            job = self._file(content, folder, pre, title, name=title)
            return Entry("file", title, job.dest if job else it.get("html_url"), "" if job else "not available")
        if kind == "Page":
            x = api.get(f"/courses/{cid}/pages/{it['page_url']}")[1]
            body = x.get("body") or x.get("lock_explanation") or ""
            return self._doc(folder, pre, title, "page", body, [], it.get("html_url"), ("page", it["page_url"]))
        if kind == "Assignment":
            x = api.get(f"/courses/{cid}/assignments/{content}")[1]
            facts = [("Due", fmt_date(x.get("due_at")) or "No due date"),
                     ("Points", fmt_number(x.get("points_possible"))),
                     ("Submission", ", ".join(s.replace("_", " ") for s in x.get("submission_types") or []))]
            body = x.get("description") or x.get("lock_explanation") or ""
            return self._doc(folder, pre, title, "assignment", body, facts, it.get("html_url"),
                             ("assignments", str(content)))
        if kind == "Quiz":
            x = api.get(f"/courses/{cid}/quizzes/{content}")[1]
            facts = [("Due", fmt_date(x.get("due_at")) or "No due date"),
                     ("Questions", x.get("question_count")),
                     ("Time limit", f"{x['time_limit']} min" if x.get("time_limit") else "None")]
            return self._doc(folder, pre, title, "quiz", x.get("description") or "", facts, it.get("html_url"),
                             ("quizzes", str(content)))
        if kind == "Discussion":
            x = api.get(f"/courses/{cid}/discussion_topics/{content}")[1]
            facts = [("Posted", fmt_date(x.get("posted_at")))]
            return self._doc(folder, pre, title, "discussion", x.get("message") or "", facts, it.get("html_url"),
                             ("discussion_topics", str(content)))
        if kind == "ExternalUrl":
            return Entry("link", title, it.get("external_url") or it.get("html_url"))
        if kind == "ExternalTool":
            return Entry("link", title, it.get("html_url"), "opens in Canvas")
        return Entry("link", title, it.get("html_url"), kind or "")

    def _unique(self, path):
        stem, suffix, n = path.stem, path.suffix, 2
        while str(path).lower() in self.used:  # case-insensitive filesystems
            path = path.with_name(f"{stem} ({n}){suffix}")
            n += 1
        self.used.add(str(path).lower())
        return path

    def _file(self, fid, folder, prefix="", context="", name=None):
        fid = str(fid)
        if fid in self.jobs:
            return self.jobs[fid]
        self.jobs[fid] = None
        try:
            f = self.api.get(f"/files/{fid}")[1]
        except CanvasError as e:
            self.problems.append(f"{context or 'File ' + fid}: {e}")
            return None
        filename = f.get("filename") or ""
        name = with_ext(name or f.get("display_name") or filename or fid, filename)
        if not f.get("url"):
            self.problems.append(f"{name}: locked or not released yet")
            return None
        job = FileJob(fid, name, f["url"], self._unique(folder / safe(prefix + name)), f.get("size") or 0,
                      timestamp(f.get("modified_at") or f.get("updated_at")))
        self.jobs[fid] = job
        return job

    def _doc(self, folder, pre, title, kind, body, facts, url, key):
        path = self._unique(folder / (safe(pre + title, 145, keep_ext=False) + ".html"))
        self.local[key] = path
        for ref in self._refs(body):
            if ref[0] == "files":
                self._file(ref[1], folder / "attachments", context=f"File linked from “{title}”")
        self.docs.append(Doc(path, title, kind, body, [(k, v) for k, v in facts if v not in (None, "")], url))
        return Entry(kind, title, path)

    # ---- links inside pages ----

    def _ref(self, url):
        """('files', id), ('page', slug), ('item', id)… if url points into this Canvas, else None."""
        u = urlparse(html.unescape(url))
        if u.scheme not in ("", "http", "https") or (u.netloc and u.hostname != self.api.host):
            return None
        m = re.search(r"/files/(\d+)", u.path)
        if m:
            return ("files", m.group(1))
        m = re.search(rf"/courses/{self.cid}/(pages)/([^/]+)/?$", u.path)
        if m:
            return ("page", unquote(m.group(2)))
        m = re.search(rf"/courses/{self.cid}/(assignments|quizzes|discussion_topics)/(\d+)/?$", u.path)
        if m:
            return (m.group(1), m.group(2))
        m = re.search(rf"/courses/{self.cid}/modules/items/(\d+)/?$", u.path)
        if m:
            return ("item", m.group(1))
        return None

    def _refs(self, body):
        return [r for r in (self._ref(m.group(3)) for m in ATTR.finditer(body or "")) if r]

    def _rewrite(self, body, here):
        """Point links at local copies; make Canvas-relative links absolute."""
        def sub(m):
            raw = html.unescape(m.group(3))
            ref = self._ref(raw)
            target = None
            if ref and ref[0] == "files":
                job = self.jobs.get(ref[1])
                target = job.dest if job else None
            elif ref:
                target = self.local.get(ref)
            if target:
                new = rel(target, here.parent)
            elif raw.startswith("//"):
                new = "https:" + raw
            elif raw.startswith("/"):
                new = self.api.base + raw
            else:
                return m.group(0)
            return f"{m.group(1)}{m.group(2)}{html.escape(new)}{m.group(2)}"
        return ATTR.sub(sub, body or "")

    # ---- writing pages, the index and links.md ----

    def _in_this_run(self, path):
        folders = {f for _n, f, _e in self.modules}
        return any(f == path.parent or f in path.parents for f in folders)

    def write(self):
        new_docs = {str(d.path) for d in self.docs}
        for old in self.manifest.get("docs", []):  # pages renamed or removed since last time
            p = self.root / old
            if str(p) not in new_docs and self._in_this_run(p) and p.suffix == ".html" and p.is_file():
                p.unlink()
        for doc in self.docs:
            try:
                doc.path.parent.mkdir(parents=True, exist_ok=True)
                doc.path.write_text(self._render_doc(doc), encoding="utf-8")
            except OSError as e:
                self.problems.append(f"{doc.title}: couldn't save the page ({e.strerror or e})")
        for name, folder, entries in self.modules:
            links = [e for e in entries if e.kind in ("header", "link", "quiz")]
            if any(e.kind != "header" for e in links):
                folder.mkdir(parents=True, exist_ok=True)
                lines = [f"# {name}: links"]
                for e in links:
                    if e.kind == "header":
                        lines += ["", f"## {e.title}"]
                        continue
                    if lines[-1].startswith("#"):
                        lines.append("")
                    if e.kind == "quiz":
                        lines.append(f"- {e.title} (quiz: take it in Canvas; the intro is in the HTML file)")
                    else:
                        lines.append(f"- [{e.title}]({e.target})" + (f" ({e.note})" if e.note else ""))
                (folder / "links.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.write_index()

    def write_index(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "index.html").write_text(self._render_index(), encoding="utf-8")

    def _render_doc(self, doc):
        e = html.escape
        facts = "".join(f"<div><dt>{e(k)}</dt><dd>{e(str(v))}</dd></div>" for k, v in doc.facts)
        source = f' · <a href="{e(doc.url)}">open in Canvas</a>' if doc.url else ""
        return PAGE.format(
            title=e(doc.title), css=CSS, kind=doc.kind.title(),
            back=rel(self.root / "index.html", doc.path.parent), course=e(self.title),
            facts=f'<dl class="facts">{facts}</dl>' if facts else "",
            body=self._rewrite(doc.body, doc.path) or '<p class="muted">No description.</p>',
            footer=f"Saved {fmt_date(datetime.now().astimezone().isoformat())}{source}")

    def _render_index(self):
        e = html.escape
        toc, sections = [], []
        for i, (name, _folder, entries) in enumerate(self.modules, 1):
            toc.append(f'<li><a href="#m{i}">{e(name)}</a></li>')
            rows = []
            for en in entries:
                if en.kind == "header":
                    rows.append(f'<li class="sub">{e(en.title)}</li>')
                    continue
                if isinstance(en.target, Path) and not en.target.exists():
                    href, ext, tag = "#", "", "Missing"
                    en = Entry(en.kind, en.title, None, "not downloaded; see the end of the run")
                elif isinstance(en.target, Path):
                    href, ext = rel(en.target, self.root), ""
                    tag = en.target.suffix.lstrip(".").upper() if en.kind == "file" else en.kind.title()
                else:
                    href, ext, tag = en.target or "#", ' class="ext"', "Link" if en.kind == "link" else en.kind.title()
                note = f' <span class="muted">{e(en.note)}</span>' if en.note else ""
                rows.append(f'<li><a href="{e(href)}"{ext}>{e(en.title)}</a>'
                            f'<span class="tag">{e(tag)}</span>{note}</li>')
            sections.append(f'<section id="m{i}"><h2>{e(name)}</h2><ul class="items">'
                            + ("".join(rows) or '<li class="muted">Empty module</li>') + "</ul></section>")
        canvas = f"{self.api.base}/courses/{self.cid}"
        sub = " · ".join(x for x in (e(self.course.get("course_code") or ""),
                                      f"saved {fmt_date(datetime.now().astimezone().isoformat())}",
                                      f'<a href="{e(canvas)}">open in Canvas</a>') if x)
        return INDEX.format(title=e(self.title), css=CSS, sub=sub, toc="".join(toc),
                            sections="".join(sections))

    # ---- downloading ----

    def _current(self, job):
        """True if the local copy is this file (same size, not older than the upload)."""
        owner = self.owner.get(os.path.normcase(os.path.relpath(job.dest, self.root)))
        if owner and owner != job.fid:  # last time this name held a different file
            return False
        try:
            st = job.dest.stat()
        except OSError:
            return False
        if job.size and st.st_size != job.size:
            return False
        return not job.modified or st.st_mtime >= job.modified - 2

    def _reuse(self, job):
        """If last run saved this file under another name (items got renumbered), move it."""
        old = self.old_files.get(job.fid)
        if not old or job.dest.exists():
            return False
        src = self.root / old
        if src == job.dest or not src.is_file() or (job.size and src.stat().st_size != job.size):
            return False
        job.dest.parent.mkdir(parents=True, exist_ok=True)
        src.replace(job.dest)
        return self._current(job)

    def fetch(self, console, workers=4):
        jobs = [j for j in self.jobs.values() if j]
        try:
            todo = [j for j in jobs if not (self._current(j) or self._reuse(j))]
            self.up_to_date = len(jobs) - len(todo)
            if todo:
                self._download(todo, console, workers)
        finally:
            self._save_manifest(jobs)
            self.write_index()

    def _download(self, todo, console, workers):
        stop = threading.Event()
        columns = (SpinnerColumn(), TextColumn("{task.description}", markup=False), BarColumn(),
                   DownloadColumn(), TransferSpeedColumn(), TimeRemainingColumn())
        with Progress(*columns, console=console) as prog:
            total = prog.add_task(f"{self.title}: {len(todo)} file(s)", total=sum(j.size for j in todo) or None)

            def work(job):
                name = job.name if len(job.name) <= 40 else job.name[:37] + "…"
                task = prog.add_task("  " + name, total=job.size or None)

                def step(n):
                    if stop.is_set():
                        raise _Cancelled
                    prog.advance(task, n)
                    prog.advance(total, n)
                try:
                    job.dest.parent.mkdir(parents=True, exist_ok=True)
                    self.api.download(job.url, job.dest, step, job.size)
                    if job.modified:
                        os.utime(job.dest, (job.modified, job.modified))
                    return 1
                except _Cancelled:
                    return 0
                except (CanvasError, OSError) as e:
                    self.problems.append(f"{job.name}: {e}")
                    return 0
                finally:
                    prog.remove_task(task)

            pool = ThreadPoolExecutor(max_workers=max(1, workers or 1))
            try:
                for fut in as_completed([pool.submit(work, j) for j in todo]):
                    self.downloaded += fut.result()
            except BaseException:
                stop.set()  # make running downloads stop at their next chunk
                raise
            finally:
                pool.shutdown(wait=True, cancel_futures=True)

    def _save_manifest(self, jobs):
        files = {j.fid: os.path.relpath(j.dest, self.root) for j in jobs if j.dest.is_file()}
        taken = {os.path.normcase(p) for p in files.values()}
        kept = {fid: p for fid, p in self.old_files.items() if fid not in files and os.path.normcase(p) not in taken}
        docs = [os.path.relpath(d.path, self.root) for d in self.docs]
        docs += [p for p in self.manifest.get("docs", []) if not self._in_this_run(self.root / p) and p not in docs]
        data = {"version": 1, "course": self.cid, "files": {**kept, **files}, "docs": docs}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / MANIFEST).write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass

    @property
    def file_count(self):
        return sum(1 for j in self.jobs.values() if j)


CSS = """
:root{--bg:#fbfaf8;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;--accent:#c4312b;--tag:#efece6}
@media (prefers-color-scheme:dark){:root{--bg:#18181a;--fg:#ececee;--muted:#9a9aa2;--line:#2e2e33;--accent:#ff7a6e;--tag:#26262a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:48rem;margin:0 auto;padding:2rem 1rem 4rem}
a{color:var(--accent)}
h1{font-size:1.9rem;line-height:1.2;margin:.3rem 0 .4rem}
h2{font-size:1.2rem;margin:2.2rem 0 .6rem;padding-bottom:.3rem;border-bottom:1px solid var(--line)}
img,video,iframe{max-width:100%;height:auto}
table{border-collapse:collapse;max-width:100%;display:block;overflow-x:auto}
td,th{border:1px solid var(--line);padding:.35rem .6rem}
pre{overflow-x:auto;background:var(--tag);padding:.8rem;border-radius:6px}
.muted,.sub-line{color:var(--muted)}
.top{display:flex;justify-content:space-between;gap:1rem;font-size:.9rem}
.top a{text-decoration:none}
.badge,.tag{font-size:.75rem;background:var(--tag);color:var(--muted);border-radius:99px;padding:.05rem .55rem;white-space:nowrap}
.facts{display:flex;flex-wrap:wrap;gap:.4rem 1.6rem;margin:.6rem 0 1.4rem;padding:.7rem 1rem;border:1px solid var(--line);border-radius:8px}
.facts div{display:flex;gap:.4rem}.facts dt{color:var(--muted)}.facts dd{margin:0;font-weight:600}
footer{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);font-size:.85rem;color:var(--muted)}
.toc{columns:2 16rem;padding-left:1.2rem;margin:1.2rem 0}
.items{list-style:none;padding:0;margin:0}
.items li{display:flex;align-items:baseline;gap:.6rem;padding:.4rem 0;border-bottom:1px solid var(--line)}
.items li a{flex:1;text-decoration:none}.items li a:hover{text-decoration:underline}
.items li.sub{display:block;border:0;padding-top:1rem;font-weight:600;color:var(--muted);font-size:.85rem;text-transform:uppercase;letter-spacing:.04em}
a.ext::after{content:" ↗";color:var(--muted)}
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head>
<body><main>
<div class="top"><a href="{back}">← {course}</a><span class="badge">{kind}</span></div>
<h1>{title}</h1>
{facts}
<article>{body}</article>
<footer>{footer}</footer>
</main></body></html>
"""

INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head>
<body><main>
<h1>{title}</h1>
<p class="sub-line">{sub}</p>
<ol class="toc">{toc}</ol>
{sections}
<footer>Made with canvas-dl</footer>
</main></body></html>
"""
