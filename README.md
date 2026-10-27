# canvas-dl

Download your Canvas LMS courses for offline study.

## Why

Many schools have turned off personal access tokens for students, so most Canvas download tools no longer work. canvas-dl signs in with a normal browser window, using the same SSO, Microsoft or 2FA login you already use, so it works either way. It then uses Canvas's official REST API to fetch your course content. The API is used read-only: canvas-dl only sends GET requests, so it never submits, posts or changes anything.

## Install

```sh
pipx install git+https://github.com/nyaadesu/canvas-dl
```

Requires Python 3.9+. No pipx? `pip install git+https://github.com/nyaadesu/canvas-dl` works too.

Signing in needs a Chromium-based browser. canvas-dl uses an installed Chrome, Edge, Chromium or Brave automatically. If you have none of those, run:

```sh
canvas-dl install-browser
```

To point canvas-dl at a specific browser, set the `CANVAS_DL_BROWSER` environment variable to that browser's executable.

## Quick start

1. Run `canvas-dl` and choose **Sign in**. Enter your school's Canvas address (for example `myschool`) and log in in the browser window that opens.
2. Choose **Download courses**, tick the courses you want, and pick a save folder.
3. Open the `index.html` file in each course folder in a browser to study offline.

The same thing from the command line:

```sh
canvas-dl login myschool.instructure.com
canvas-dl get 12345 -o ~/Canvas
```

### Interactive menu

Run `canvas-dl` with no arguments to get an arrow-key menu:

```
╭─ canvas-dl 0.1.1 ───────────────────────────────────╮
│ Signed in as Sam Student · myschool.instructure.com │
╰─────────────────────────────────────────────────────╯
? What would you like to do?
 » Download courses
   Update downloads (2 courses)
   Settings
   Sign out
   Quit
```

- **Download courses**: a checkbox list of your courses, grouped by term. For each course, choose "All modules" or pick specific modules. Then choose a save folder.
- **Update downloads**: re-syncs everything you downloaded before, fetching only new or changed files. "All modules" picks up modules added later.
- **Settings**: download folder, parallel downloads, and whether to show finished courses.
- **Sign out**: deletes the saved session.

Progress bars show while downloading.

## Commands

For scripts and cron jobs:

| Command | What it does |
| --- | --- |
| `canvas-dl login <canvas-url>` | Sign in. A bare name such as `myschool` expands to `myschool.instructure.com`. |
| `canvas-dl logout` | Sign out and delete the saved session. |
| `canvas-dl courses [--all]` | List course IDs. `--all` includes finished courses. |
| `canvas-dl modules <course id or URL>` | List the modules in a course. |
| `canvas-dl get <course id or URL> [-m MODULE ...] [-o DIR] [-j N]` | Download a course. `-m` takes a module name fragment or ID and can be repeated; a module URL also works. Default is all modules. `-o` sets the output folder, `-j` the number of parallel downloads. |
| `canvas-dl update [-j N]` | Re-sync everything downloaded before. Good for a nightly cron job. |
| `canvas-dl install-browser` | Download a browser for signing in, if you don't have Chrome, Edge or Chromium. |

Example crontab entry:

```
0 3 * * * canvas-dl update
```

How long a sign-in lasts is up to your school. Some keep you signed in for weeks, others only for a few hours. When it runs out, `canvas-dl update` exits with an error, and you sign in again.

## What you get

Each course is saved to `<download folder>/<Course name>/<Module name>/`, with items in module order and `01 - `, `02 - `, and so on as prefixes.

- **Files**: PDFs, slides, documents, Packet Tracer files, videos uploaded to Canvas, and similar.
- **Pages, assignment descriptions, quiz intros and discussion prompts** as clean, readable HTML pages that work offline. Course files and images uploaded to Canvas that they link to are saved in an `attachments/` folder, and the links point to those copies. Links between pages of the course open the local copies too. Images hosted on other websites still load from the web.
- **`links.md`** in each module, listing external links, external tools and quizzes. Quizzes must be taken in Canvas.
- **`index.html`** in each course: a table of contents of every module and item, linking to the local copies. Open it in a browser to study offline.

Re-running a download only fetches files that are new or that the teacher has replaced. If items move around in a module, files are renamed instead of downloaded again. A small `.canvas-dl.json` file in each course folder keeps track of this. Items that are locked or hidden by the teacher are listed at the end of the run instead of being silently dropped.

Example:

```
~/Canvas/
└── Intro to Networking/
    ├── index.html
    ├── Week 1 Introduction/
    │   ├── 01 - Syllabus.pdf
    │   ├── 02 - Lecture 1 slides.pptx
    │   ├── 03 - Welcome.html
    │   ├── links.md
    │   └── attachments/
    │       └── Reading list.pdf
    └── Week 2 Packet Tracer/
        ├── 01 - Lab 2 brief.html
        ├── 02 - Lab 2.pkt
        └── links.md
```

### Where settings and data are stored

Settings and your session live in the user config directory:

- Linux: `~/.config/canvas-dl`
- macOS: `~/Library/Application Support/canvas-dl`
- Windows: `%LOCALAPPDATA%\canvas-dl`

Set the `CANVAS_DL_HOME` environment variable to use a different location.

## Privacy & security

- The session is stored in `session.json`. It holds only your Canvas login cookies, not your school's single sign-on ones, and is readable only by you. Treat it like a password.
- `canvas-dl logout` deletes `session.json`.
- Sessions expire like normal Canvas logins. When one expires, sign in again.
- canvas-dl only sends GET requests to Canvas. It never submits, posts or changes anything.

## How is this different?

There are good Chrome extensions that also use your browser session, such as "Canvas Course Downloader & Exporter". canvas-dl is terminal-first. It:

- works on Linux, macOS and Windows, including from scripts and cron jobs (signing in needs a screen once; after that downloads run without one),
- is scriptable and cron-friendly, with incremental sync,
- produces a browsable offline copy with working links between pages and files.

Token-based tools such as canvas-stream need a personal access token, which many schools disable.

## Limitations

- Videos hosted in Panopto, Kaltura, YouTube, Teams and other external tools are not downloaded. They are listed in `links.md`.
- Quizzes cannot be taken offline.
- New Quizzes content is not downloaded.

## Responsible use

canvas-dl is for your own personal study. Course materials usually belong to your institution and teachers, so don't redistribute them, and respect your school's IT policies.

## FAQ

**My session expired. What now?**
Run `canvas-dl login <your-canvas-url>` again, or choose **Sign in** from the menu. Your downloaded files stay where they are.

**canvas-dl says "Couldn't start a browser". What do I do?**
Install a Chromium-based browser (Chrome, Edge, Chromium or Brave), or run `canvas-dl install-browser`. To use a specific browser, set `CANVAS_DL_BROWSER` to its executable.

**Does it work with my school?**
It works with any Canvas instance, including custom domains such as `canvas.myuni.ac.uk`. Pass the full address to `canvas-dl login`.

## License

MIT. See [LICENSE](LICENSE).

---

canvas-dl is not affiliated with Instructure. "Canvas" is a trademark of Instructure, Inc.
