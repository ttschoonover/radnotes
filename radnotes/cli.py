"""radnotes: open your notes, or work with them from the terminal."""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import REPO, __version__
from .notes import Library, Notes
from .pairing import Invitation, is_device_code, pair_with
from .radicle import MockRadicle, RadError, Radicle
from .store import NotFound

HELP = """\
radnotes                  open your notes in the browser
radnotes new [TEXT]       write a note (quick: radnotes new "call the bank")
radnotes ls               list notes, newest first
radnotes show N           print note N (numbers come from `radnotes ls`)
radnotes edit N           edit note N in your terminal editor
radnotes rm N             delete note N
radnotes sync             sync now (the app also syncs on its own)

Your devices
radnotes pair             show a code to add another of your devices
radnotes pair CODE        on the other device, enter that code

Friends
radnotes share NAME       make a shared space and get an invite code
radnotes join CODE        join a friend's shared space
radnotes spaces           list your shared spaces and their codes
  add -s NAME to new, ls, show, edit or rm to work in a shared space

radnotes status           this device and what it syncs with
radnotes update           install the latest version
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS, help="notes folder (default ~/RadNotes)")
    common.add_argument("--port", type=int, default=argparse.SUPPRESS, help="app port (default 7357)")
    common.add_argument("--no-browser", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--mock", action="store_true", default=argparse.SUPPRESS, help="offline demo network")
    common.add_argument("--alias", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--rad", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="radnotes", parents=[common], usage="%(prog)s [command]",
                                description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"radnotes {__version__}")
    sub = p.add_subparsers(dest="cmd", help=argparse.SUPPRESS)
    add = lambda name: sub.add_parser(name, parents=[common])  # noqa: E731

    def with_space(sp):
        sp.add_argument("-s", "--space", help="a shared space (name or number from `radnotes spaces`)")
        return sp

    add("open")
    with_space(add("new")).add_argument("text", nargs="*")
    with_space(add("ls"))
    with_space(add("show")).add_argument("note")
    with_space(add("edit")).add_argument("note")
    rm = with_space(add("rm"))
    rm.add_argument("note")
    rm.add_argument("-y", "--yes", action="store_true")
    add("sync")
    add("pair").add_argument("code", nargs="?")
    add("share").add_argument("name", nargs="+")
    add("join").add_argument("code")
    add("spaces")
    add("status")
    add("update")
    return p


def load(args) -> Library:
    home = Path(getattr(args, "home", None) or os.environ.get("RADNOTES_HOME", "~/RadNotes")).expanduser()
    if getattr(args, "mock", False) or os.environ.get("RADNOTES_MOCK") == "1":
        radicle = MockRadicle(home.with_name(home.name + ".mock"), alias=getattr(args, "alias", None),
                              network=os.environ.get("RADNOTES_MOCK_NETWORK") or home.parent / ".radnotes-mock-network")
    else:
        radicle = Radicle(getattr(args, "rad", None) or "rad")
    return Library(home, radicle)


def open_editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if shutil.which("nano") else "vi")
    try:
        subprocess.call([*shlex.split(editor), str(path)])
    except FileNotFoundError:
        raise ValueError(f"Couldn't start the editor {editor!r}. Set one, e.g. export EDITOR=nano") from None


def run_app(lib: Library, port: int, no_browser: bool) -> None:
    import uvicorn

    from .server import create_app

    if lib.personal.migrated_from:
        print(f"Moved your earlier notebooks to {lib.personal.migrated_from} and imported their notes.")
    url = f"http://127.0.0.1:{port}/"
    print(f"RadNotes is open at {url}  (Ctrl+C to stop)")
    if not no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(create_app(lib), host="127.0.0.1", port=port, log_level="warning")


def print_sync(r: dict) -> None:
    print(r["message"])
    if r["copies"]:
        print("Some notes were edited on two devices; the other versions were saved as separate notes.")


def update() -> int:
    sources = [f"https://github.com/{REPO}/releases/latest/download/radnotes.tar.gz",
               f"https://github.com/{REPO}/archive/refs/heads/main.zip"]
    for src in sources:
        print(f"Installing from {src}")
        code = subprocess.call([sys.executable, "-m", "pip", "install", "-q", "--upgrade", src])
        if code == 0:
            out = subprocess.run([sys.executable, "-c", "import radnotes; print(radnotes.__version__)"],
                                 capture_output=True, text=True).stdout.strip()
            print(f"RadNotes is now version {out or 'up to date'}. Restart it if it's open.")
            return 0
    print("Couldn't update. Check your internet connection and try again.", file=sys.stderr)
    return 1


def dispatch(lib: Library, a) -> int:
    c = a.cmd
    notes: Notes = lib.find_space(a.space) if getattr(a, "space", None) else lib.personal
    where = f" in {notes.title}" if notes.kind == "space" else ""

    if c == "new":
        text = " ".join(a.text)
        if text:
            notes.create(text + "\n")
        elif not sys.stdin.isatty():
            notes.create(sys.stdin.read())
        else:
            note_id = notes.create("")
            open_editor(notes.home / f"{note_id}.md")
            if not notes.read(note_id)["text"].strip():
                notes.delete(note_id)
                print("Empty, so nothing was saved.")
                return 0
        print(notes.send())
    elif c == "ls":
        items = notes.list()
        if not items:
            print(f'No notes{where} yet. Try: radnotes new{" -s " + repr(notes.title) if where else ""} "first thought"')
        width = len(str(len(items)))
        for i, n in enumerate(items, 1):
            print(f"{i:>{width}}  {time.strftime('%b %d', time.localtime(n['modified']))}  {n['title']}")
        if not where and lib.spaces():
            print(f"\nShared spaces: {', '.join(sp.title for sp in lib.spaces())}  (radnotes ls -s NAME)")
    elif c == "show":
        print(notes.read(notes.find(a.note))["text"], end="")
    elif c == "edit":
        note_id = notes.find(a.note)
        open_editor(notes.home / f"{note_id}.md")
        print(notes.send())
    elif c == "rm":
        note_id = notes.find(a.note)
        text = notes.read(note_id)["text"].strip()
        title = text.splitlines()[0][:60] if text else "Untitled"
        if a.yes or (sys.stdin.isatty() and input(f"Delete “{title}”? [y/N] ").lower() in ("y", "yes")):
            notes.delete(note_id)
            print(notes.send())
    elif c == "sync":
        r = lib.sync_all()
        print_sync(r)
        return 0 if r["ok"] else 1

    elif c == "pair" and not a.code:
        inv = Invitation(lib.personal).start()
        print(f"\nOn your other device, run:\n\n    radnotes pair {inv.code}\n")
        print("Or enter the code in the app under Share. Waiting (the code works for 10 minutes)…")
        try:
            state = inv.wait()
        finally:
            inv.close()
        if state == "paired":
            print(f"Paired with {inv.paired}. Your notes now sync between the two.")
            return 0
        print("No device paired. Run `radnotes pair` again for a new code." if state == "expired"
              else "Too many wrong tries, so the code was cancelled. Run `radnotes pair` again.")
        return 1
    elif c == "pair" or (c == "join" and is_device_code(a.code)):
        print("Pairing…")
        r = pair_with(lib.personal, a.code)
        print("Paired. This device now has your notes and keeps them in sync.")
        if r["kept"]:
            print(f"The {r['kept']} notes that were on this device were added too (old folder: {r['backup']}).")
    elif c == "share":
        sp = lib.create_space(" ".join(a.name))
        print(f"Created the shared space “{sp.title}”. Send your friends this code:\n\n    {sp.code}\n")
        print("They run `radnotes join CODE` (or paste it under Share in the app).")
        print("Anyone with the code can read and write this space, so share it only with them.")
    elif c == "join":
        print("Looking for the shared space on the network…")
        sp = lib.join_space(a.code)
        print(f"Joined “{sp.title}”. It shows up in the app, or use: radnotes ls -s {shlex.quote(sp.title)}")
    elif c == "spaces":
        spaces = lib.spaces()
        if not spaces:
            print('No shared spaces yet. Make one: radnotes share "Book club"')
        for i, sp in enumerate(spaces, 1):
            print(f"{i}  {sp.title}  ({len(sp.list())} notes)\n   invite code: {sp.code}")
    elif c == "status":
        p, me = lib.personal, lib.radicle.identity() if lib.radicle.available() else None
        s = p.status()
        print(f"Device     {s['device']}")
        print(f"Notes      {len(p.list())} in {p.home}")
        print(f"Radicle    {me['alias'] + ' ' + me['did'] if me else 'not set up (install Radicle, then run `rad auth`)'}")
        print(f"Devices    {len(p.repo.peers())} paired" if s["published"] else "Devices    none yet (radnotes pair)")
        for sp in lib.spaces():
            print(f"Shared     {sp.title}  {sp.code}")
    elif c == "update":
        return update()
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "update":
            return update()
        lib = load(args)
        if args.cmd in (None, "open"):
            run_app(lib, getattr(args, "port", int(os.environ.get("RADNOTES_PORT", 7357))), getattr(args, "no_browser", False))
            return 0
        return dispatch(lib, args)
    except (ValueError, NotFound, RadError) as exc:
        print(f"radnotes: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
