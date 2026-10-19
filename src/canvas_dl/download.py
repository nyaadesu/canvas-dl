"""Turn a course's modules into a folder you can study from offline."""

from __future__ import annotations

import html
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Union
from urllib.parse import quote

from rich.progress import (BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn,
                           TimeRemainingColumn, TransferSpeedColumn)

from .api import CanvasError

ATTR = re.compile(r"""(\b(?:href|src)\s*=\s*)(["'])(.*?)\2""", re.I | re.S)
FILE_REF = re.compile(r"/files/(\d+)")
EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def safe(name, limit=120):
    """Make a string usable as a file or folder name on any OS."""
    name = re.sub(r"\s*:\s+", " - ", str(name))  # "LO1: Basics" -> "LO1 - Basics"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].rstrip(" .") or "untitled"


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


def rel(target, start):
    return quote(os.path.relpath(target, start).replace(os.sep, "/"))


@dataclass
class FileJob:
    fid: str
    name: str
    url: str
    dest: Path
    size: int


@dataclass
class Entry:
    kind: str                            # header, file, page, assignment, quiz, discussion, link
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
        self.used: set[Path] = set()
        self.docs: list[Doc] = []
        self.modules: list[tuple[str, Path, list[Entry]]] = []
        self.problems: list[str] = []
        self.downloaded = 0
        self.up_to_date = 0

    # ---- planning: ask Canvas what's there, decide where it goes ----

    def plan(self, modules, status=lambda text: None):
        for m in modules:
            status(m["name"])
            folder = self.root / safe(m["name"])
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
                    entries.append(self._item(it, kind, title, folder, f"{n:02d} - "))
                except CanvasError as e:
                    self.problems.append(f"{title}: {e}")
                    entries.append(Entry("link", title, it.get("html_url"), "not available"))
            self.modules.append((m["name"], folder, entries))

    def _item(self, it, kind, title, folder, pre):
        api, cid, cid_id = self.api, self.cid, it.get("content_id")
        if kind == "File":
            job = self._file(cid_id, folder, pre, title)
            return Entry("file", title, job.dest if job else it.get("html_url"), "" if job else "not available")
        if kind == "Page":
            x = api.get(f"/courses/{cid}/pages/{it['page_url']}")[1]
            body = x.get("body") or x.get("lock_explanation") or ""
            return self._doc(folder, pre, title, "page", body, [], it.get("html_url"))
        if kind == "Assignment":
            x = api.get(f"/courses/{cid}/assignments/{cid_id}")[1]
            facts = [("Due", fmt_date(x.get("due_at")) or "No due date"),
                     ("Points", x.get("points_possible")),
                     ("Submission", ", ".join(s.replace("_", " ") for s in x.get("submission_types") or []))]
            body = x.get("description") or x.get("lock_explanation") or ""
            return self._doc(folder, pre, title, "assignment", body, facts, it.get("html_url"))
        if kind == "Quiz":
            x = api.get(f"/courses/{cid}/quizzes/{cid_id}")[1]
            facts = [("Due", fmt_date(x.get("due_at")) or "No due date"),
                     ("Questions", x.get("question_count")),
                     ("Time limit", f"{x['time_limit']} min" if x.get("time_limit") else "None")]
            return self._doc(folder, pre, title, "quiz", x.get("description") or "", facts, it.get("html_url"))
        if kind == "Discussion":
            x = api.get(f"/courses/{cid}/discussion_topics/{cid_id}")[1]
            facts = [("Posted", fmt_date(x.get("posted_at")))]
            return self._doc(folder, pre, title, "discussion", x.get("message") or "", facts, it.get("html_url"))
        if kind == "ExternalUrl":
            return Entry("link", title, it.get("external_url") or it.get("html_url"))
        if kind == "ExternalTool":
            return Entry("link", title, it.get("html_url"), "opens in Canvas")
        return Entry("link", title, it.get("html_url"), kind or "")

    def _unique(self, path):
        stem, suffix, n = path.stem, path.suffix, 2
        while path in self.used:
            path = path.with_name(f"{stem} ({n}){suffix}")
            n += 1
        self.used.add(path)
        return path

    def _file(self, fid, folder, prefix="", context=""):
        fid = str(fid)
        if fid in self.jobs:
            return self.jobs[fid]
        self.jobs[fid] = None
        try:
            f = self.api.get(f"/files/{fid}")[1]
        except CanvasError as e:
            self.problems.append(f"{context or 'File ' + fid}: {e}")
            return None
        name = f.get("display_name") or f.get("filename") or fid
        ext = EXT.search(f.get("filename") or "")
        if ext and not EXT.search(name):  # display names sometimes drop the extension
            name += ext.group(0)
        if not f.get("url"):
            self.problems.append(f"{name}: locked or not released yet")
            return None
        job = FileJob(fid, name, f["url"], self._unique(folder / safe(prefix + name)), f.get("size") or 0)
        self.jobs[fid] = job
        return job

    def _doc(self, folder, pre, title, kind, body, facts, url):
        path = self._unique(folder / (safe(pre + title, 115) + ".html"))
        for fid in dict.fromkeys(FILE_REF.findall(body)):
            self._file(fid, folder / "attachments", context=f"File linked from “{title}”")
        self.docs.append(Doc(path, title, kind, body, [(k, v) for k, v in facts if v not in (None, "")], url))
        return Entry(kind, title, path)

    # ---- writing pages, the index and links.md ----

    def write(self):
        for doc in self.docs:
            doc.path.parent.mkdir(parents=True, exist_ok=True)
            doc.path.write_text(self._render_doc(doc), encoding="utf-8")
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
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "index.html").write_text(self._render_index(), encoding="utf-8")

    def _rewrite(self, body, here):
        """Point links at local copies; make Canvas-relative links absolute."""
        def sub(m):
            raw = html.unescape(m.group(3))
            ref = FILE_REF.search(raw)
            job = self.jobs.get(ref.group(1)) if ref else None
            if job:
                new = rel(job.dest, here.parent)
            elif raw.startswith("//"):
                new = "https:" + raw
            elif raw.startswith("/"):
                new = self.api.base + raw
            else:
                return m.group(0)
            return f"{m.group(1)}{m.group(2)}{html.escape(new)}{m.group(2)}"
        return ATTR.sub(sub, body or "")

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
                if isinstance(en.target, Path):
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

    def fetch(self, console, workers=4):
        jobs = [j for j in self.jobs.values() if j]
        todo = [j for j in jobs if not (j.dest.exists() and (not j.size or j.dest.stat().st_size == j.size))]
        self.up_to_date = len(jobs) - len(todo)
        if not todo:
            return
        columns = (SpinnerColumn(), TextColumn("{task.description}", markup=False), BarColumn(),
                   DownloadColumn(), TransferSpeedColumn(), TimeRemainingColumn())
        with Progress(*columns, console=console) as prog:
            total = prog.add_task(f"{self.title}: {len(todo)} file(s)", total=sum(j.size for j in todo) or None)

            def work(job):
                name = job.name if len(job.name) <= 40 else job.name[:37] + "…"
                task = prog.add_task("  " + name, total=job.size or None)

                def step(n):
                    prog.advance(task, n)
                    prog.advance(total, n)
                try:
                    job.dest.parent.mkdir(parents=True, exist_ok=True)
                    self.api.download(job.url, job.dest, step)
                    return 1
                except (CanvasError, OSError) as e:
                    self.problems.append(f"{job.name}: {e}")
                    return 0
                finally:
                    prog.remove_task(task)

            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                self.downloaded = sum(pool.map(work, todo))

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
