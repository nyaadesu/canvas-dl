"""The interactive menu shown when canvas-dl runs with no arguments."""

from itertools import groupby
from pathlib import Path

import questionary as q
from questionary import Choice, Separator, Style
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from . import __version__, auth, config, core
from .api import CanvasError, SessionExpired
from .download import course_title

STYLE = Style([
    ("qmark", "fg:#e0533d bold"),
    ("question", "bold"),
    ("pointer", "fg:#e0533d bold"),
    ("highlighted", "fg:#e0533d bold"),
    ("selected", "fg:#4fae6b"),
    ("answer", "fg:#e0533d bold"),
    ("instruction", "fg:#888888"),
    ("separator", "fg:#777777"),
    ("disabled", "fg:#777777 italic"),
])

BACK = "__back__"


def ask(question):
    """Run a prompt; Ctrl+C or Esc means 'go back' (None)."""
    try:
        return question.unsafe_ask()
    except KeyboardInterrupt:
        return None


class App:
    def __init__(self):
        self.console = Console(highlight=False)
        self.cfg = config.load()
        self.api = None

    # ---- main loop ----

    def run(self):
        while True:
            self.header()
            signed_in = config.signed_in(self.cfg)
            choices = []
            if signed_in:
                n = len(self.cfg["downloads"])
                choices.append(Choice("Download courses", "download"))
                choices.append(Choice(f"Update downloads ({n} course{'s' if n != 1 else ''})", "update")
                               if n else Choice("Update downloads", "update", disabled="nothing downloaded yet"))
            else:
                choices.append(Choice("Sign in", "signin"))
            choices.append(Choice("Settings", "settings"))
            if signed_in:
                choices.append(Choice("Sign out", "signout"))
            choices.append(Choice("Quit", "quit"))
            action = ask(q.select("What would you like to do?", choices, style=STYLE, qmark="?",
                                  instruction=" "))
            if action in (None, "quit"):
                return 0
            try:
                getattr(self, f"do_{action}")()
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Cancelled.[/]")
            except (auth.LoginError, CanvasError) as e:
                self.console.print(f"[red]✗[/] {escape(str(e))}")
            if action != "settings":
                self.pause()

    def header(self):
        self.console.clear()
        if config.signed_in(self.cfg):
            host = self.cfg["base"].split("://", 1)[-1]
            who = escape(self.cfg.get("user") or "you")
            line = f"Signed in as [bold]{who}[/] · [dim]{escape(host)}[/]"
        else:
            line = "[dim]Not signed in[/]"
        self.console.print(Panel(line, title=f"[bold]canvas-dl[/] [dim]{__version__}[/]", title_align="left",
                                 border_style="#e0533d", expand=False, padding=(0, 1)))

    def pause(self):
        ask(q.press_any_key_to_continue("Press any key to go back to the menu", style=STYLE))

    def connect(self):
        if self.api:
            return self.api
        try:
            with self.console.status("Connecting to Canvas…"):
                self.api = core.connect(self.cfg)
        except SessionExpired:
            self.console.print("[yellow]Your Canvas session has expired. Sign in again to continue.[/]")
            if not self.do_signin(self.cfg.get("base")):
                raise KeyboardInterrupt from None
            with self.console.status("Connecting to Canvas…"):
                self.api = core.connect(self.cfg)
        return self.api

    # ---- actions ----

    def do_signin(self, base=None):
        def valid(text):
            try:
                auth.normalise_url(text)
                return True
            except ValueError as e:
                return str(e)
        text = ask(q.text("Your Canvas address:", default=(base or "").split("://", 1)[-1],
                          instruction="(e.g. myschool.instructure.com)", validate=valid, style=STYLE))
        if not text:
            return False
        base = auth.normalise_url(text)
        self.console.print("A browser window is opening. Sign in there as usual; "
                           "it closes by itself once you're in.")
        with self.console.status("Waiting for you to sign in… [dim](Ctrl+C to cancel)[/]"):
            me = auth.login(base)
        self.cfg.update(base=base, user=me.get("name"))
        config.save(self.cfg)
        self.api = None
        self.console.print(f"[green]✓[/] Signed in as [bold]{escape(me.get('name') or '?')}[/]")
        return True

    def do_download(self):
        api = self.connect()
        with self.console.status("Fetching your courses…"):
            courses = api.courses(include_old=self.cfg["show_old"])
        if not courses:
            self.console.print("No courses found." + ("" if self.cfg["show_old"] else
                               " Finished courses are hidden. Turn them on in Settings."))
            return
        choices = []
        term = lambda c: (c.get("term") or {}).get("name") or "Other"
        for name, group in groupby(courses, key=term):
            choices.append(Separator(f"── {name} "))
            for c in group:
                done = str(c["id"]) in self.cfg["downloads"]
                title = [("", c["name"])] + ([("class:instruction", "  ✓ downloaded")] if done else [])
                choices.append(Choice(title, c))
        picked = ask(q.checkbox("Which courses?", choices, style=STYLE,
                                instruction="(space to select, enter when done)",
                                validate=lambda s: bool(s) or "Select at least one course with the space bar"))
        if not picked:
            return

        plan = []
        for c in picked:
            mode = ask(q.select(f"{course_title(c)}: which modules?", [
                Choice("All modules (and any added later)", "all"),
                Choice("Let me choose", "choose")], style=STYLE))
            if mode is None:
                return
            ids = None
            if mode == "choose":
                with self.console.status("Fetching modules…"):
                    mods = api.modules(c["id"])
                if not mods:
                    self.console.print(f"[yellow]{escape(course_title(c))} has no modules; skipping.[/]")
                    continue
                ids = ask(q.checkbox("Modules:", [
                    Choice(f"{m['name']}  ({m.get('items_count', len(m.get('items') or []))} items)", m["id"])
                    for m in mods], style=STYLE, instruction="(space to select, enter when done)",
                    validate=lambda s: bool(s) or "Select at least one module"))
                if not ids:
                    return
            plan.append((c, ids))
        if not plan:
            return

        out = ask(q.path("Save to:", default=self.cfg["out"], only_directories=True, style=STYLE))
        if not out:
            return
        if out != self.cfg["out"]:
            self.cfg["out"] = out
            config.save(self.cfg)
        self.console.print()
        for c, ids in plan:
            core.summary(self.console, core.sync_course(api, self.cfg, c, ids, out, self.console))

    def do_update(self):
        api = self.connect()
        for cid, rec in list(self.cfg["downloads"].items()):
            try:
                course = api.course(cid)
            except CanvasError as e:
                if isinstance(e, SessionExpired):
                    raise
                self.console.print(f"[yellow]Skipping {escape(rec.get('name', cid))}: {escape(str(e))}[/]")
                continue
            ids = None if rec.get("modules") == "all" else rec.get("modules")
            core.summary(self.console, core.sync_course(api, self.cfg, course, ids,
                                                        rec.get("out") or self.cfg["out"], self.console))

    def do_settings(self):
        last = None
        while True:
            self.header()
            on = "on" if self.cfg["show_old"] else "off"
            choice = ask(q.select("Settings", [
                Choice(f"Download folder       {self.cfg['out']}", "out"),
                Choice(f"Parallel downloads    {self.cfg['workers']}", "workers"),
                Choice(f"Show finished courses {on}", "old"),
                Choice("Back", BACK)], default=last, style=STYLE, instruction=" "))
            last = choice
            if choice in (None, BACK):
                return
            if choice == "out":
                v = ask(q.path("Download folder:", default=self.cfg["out"], only_directories=True, style=STYLE))
                if v:
                    self.cfg["out"] = v
            elif choice == "workers":
                v = ask(q.select("Parallel downloads:", [str(n) for n in (1, 2, 4, 6, 8)],
                                 default=str(self.cfg["workers"]) if self.cfg["workers"] in (1, 2, 4, 6, 8) else None,
                                 style=STYLE))
                if v:
                    self.cfg["workers"] = int(v)
            else:
                self.cfg["show_old"] = not self.cfg["show_old"]
            config.save(self.cfg)

    def do_signout(self):
        if ask(q.confirm("Sign out? Your downloads stay where they are.", default=True, style=STYLE)):
            config.sign_out(self.cfg)
            self.api = None
            self.console.print("[green]✓[/] Signed out.")


def run():
    try:
        return App().run()
    except KeyboardInterrupt:
        return 0
