"""Your notes, and the spaces you share with friends.

* Personal notes live in ~/RadNotes: a git repository published *privately* on
  Radicle. Each device you pair is a co-editor and follows the others directly.
* Each shared space lives in ~/RadNotes/shared/<name>: its own repository,
  published *publicly* so friends anywhere can reach it through Radicle's
  public seeds. Everyone writes to their own copy, and every sync merges the
  copies of everyone who has written to it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
from pathlib import Path

from .radicle import BRANCH, RID_RE, RadError, git, normalize_did, repo_name
from .store import Notebook, NotFound, ensure_git_identity, slugify

ADDRESS_RE = re.compile(r"(z6Mk[1-9A-HJ-NP-Za-km-z]{20,})@([^\s@]+?)(?::(\d+))?")
PORT = 8776
SHARED = "shared"
SPACE_FILE = ".space.json"


def version_of(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def title_of(text: str) -> str:
    for line in text.splitlines():
        t = line.strip().lstrip("#").strip()
        if t:
            return t[:120]
    return "Untitled"


def preview_of(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    body = " ".join(lines[1:])
    return re.sub(r"[#*_`>\[\]]|^- |\s- ", " ", body).strip()[:140]


def device_name() -> str:
    return socket.gethostname().split(".")[0]


def peer_name(nid_or_did: str) -> str:
    return "peer-" + nid_or_did.removeprefix("did:key:").removeprefix("z6Mk")[:8].lower()


def space_code(rid: str) -> str:
    """Invite code for a shared space: the repository ID, minus the 'rad:' prefix."""
    return "rn-" + rid.removeprefix("rad:")


def parse_space_code(code: str) -> str | None:
    code = (code or "").strip()
    if code.startswith("rn-"):
        code = "rad:" + code[3:]
    m = RID_RE.search(code)
    return m.group(0) if m else None


class Notes:
    def __init__(self, home, radicle, kind: str = "personal"):
        self.home = Path(home).expanduser()
        self.radicle = radicle
        self.kind = kind
        self.id = "" if kind == "personal" else self.home.name
        self.lock = threading.RLock()        # held while files change
        self.sync_lock = threading.Lock()    # one sync at a time
        self.last_sync: float | None = None
        self.last_error: str | None = None
        self.syncing = False
        self.migrated_from: Path | None = None
        self._ensure_repo()
        self.repo = Notebook(self.home)

    # setup -----------------------------------------------------------------
    def _ensure_repo(self) -> None:
        personal = self.kind == "personal"
        if personal and self.home.exists() and not (self.home / ".git").exists() and self._has_old_notes():
            self._migrate_old_layout()
        self.home.mkdir(parents=True, exist_ok=True)
        if not (self.home / ".git").exists():
            git(self.home, "init", "-q")
            git(self.home, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
            self._identity()
            git(self.home, "commit", "-q", "--allow-empty", "-m", "Start notes")
        if personal:
            ignore = self.home / ".gitignore"
            lines = ignore.read_text().splitlines() if ignore.exists() else []
            if f"{SHARED}/" not in lines:  # shared spaces are separate repositories
                ignore.write_text("\n".join(lines + [f"{SHARED}/"]) + "\n")
        if self.migrated_from:
            self._import_markdown(self.migrated_from)
        for p in self.home.glob("*.md"):  # blank notes left by an unused "New note"
            try:
                if not p.read_text("utf-8").strip():
                    p.unlink()
            except OSError:
                pass

    def _has_old_notes(self) -> bool:
        return any(p.suffix == ".md" or (p / ".git").exists() for p in self.home.iterdir() if not p.name.startswith("."))

    def _identity(self) -> None:
        me = self.radicle.identity() if self.radicle.available() else None
        ensure_git_identity(self.home, me["alias"] if me else device_name())

    def _migrate_old_layout(self) -> None:
        """Earlier versions kept several notebooks here; keep them aside and import their notes."""
        old = self.home.with_name(self.home.name + "-old")
        n = 2
        while old.exists():
            old, n = self.home.with_name(f"{self.home.name}-old{n}"), n + 1
        self.home.rename(old)
        self.migrated_from = old

    def _import_markdown(self, folder: Path) -> int:
        count = 0
        for src in sorted(folder.rglob("*.md")):
            if any(part.startswith(".") for part in src.relative_to(folder).parts):
                continue
            self.create(src.read_text("utf-8", errors="replace"))
            count += 1
        if count:
            Notebook(self.home).commit(f"Import {count} notes from {folder.name}")
        return count

    @property
    def title(self) -> str:
        if self.kind == "personal":
            return "My notes"
        try:
            return json.loads((self.home / SPACE_FILE).read_text())["title"]
        except (OSError, ValueError, KeyError):
            return self.home.name

    @property
    def code(self) -> str | None:
        rid = self.radicle.rid(self.home) if self.kind == "space" and self.published() else None
        return space_code(rid) if rid else None

    # notes -------------------------------------------------------------------
    def _path(self, note_id: str) -> Path:
        if not note_id or "/" in note_id or "\\" in note_id or note_id.startswith(".") or len(note_id) > 200:
            raise ValueError("That isn't a note")
        return self.home / f"{note_id}.md"

    def list(self) -> list[dict]:
        out = []
        for p in self.home.glob("*.md"):
            try:
                text = p.read_text("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.append({"id": p.stem, "title": title_of(text), "preview": preview_of(text),
                        "modified": p.stat().st_mtime, "version": version_of(text)})
        out.sort(key=lambda n: -n["modified"])
        return out

    def read(self, note_id: str) -> dict:
        p = self._path(note_id)
        if not p.is_file():
            raise NotFound("That note doesn't exist anymore")
        text = p.read_text("utf-8")
        return {"id": note_id, "text": text, "version": version_of(text)}

    def write(self, note_id: str, text: str, base: str | None = None) -> dict:
        p = self._path(note_id)
        with self.lock:
            if base is not None and p.exists():
                current = p.read_text("utf-8")
                if version_of(current) not in (base, version_of(text)):
                    raise ChangedElsewhere(current)
            tmp = p.with_name(f".{p.name}.tmp")
            tmp.write_text(text, "utf-8")
            os.replace(tmp, p)
        return {"id": note_id, "version": version_of(text)}

    def create(self, text: str = "") -> str:
        note_id = time.strftime("%Y-%m-%d-%H%M%S") + "-" + secrets.token_hex(2)
        self.write(note_id, text)
        return note_id

    def delete(self, note_id: str) -> None:
        p = self._path(note_id)
        if not p.is_file():
            raise NotFound("That note doesn't exist anymore")
        p.unlink()

    def search(self, query: str) -> list[dict]:
        q = query.strip().lower()
        return [n for n in self.list() if q in self.read(n["id"])["text"].lower()] if q else self.list()

    def find(self, ref: str) -> str:
        """A note by its number in `radnotes ls`, its ID, or words in its title."""
        notes = self.list()
        if ref.isdigit() and 1 <= int(ref) <= len(notes):
            return notes[int(ref) - 1]["id"]
        for n in notes:
            if n["id"] == ref:
                return n["id"]
        hits = [n for n in notes if ref.lower() in n["title"].lower()]
        if len(hits) == 1:
            return hits[0]["id"]
        raise NotFound(f"No single note matches {ref!r}. See `radnotes ls`." if not hits else
                       f"{len(hits)} notes match {ref!r}; use the number from `radnotes ls`.")

    # sync -----------------------------------------------------------------------
    def published(self) -> bool:
        return self.repo.published()

    def status(self) -> dict:
        return {"device": device_name(), "published": self.published(), "syncing": self.syncing,
                "last_sync": self.last_sync, "error": self.last_error,
                "changed": len(self.repo.changed_files())}

    def sync(self) -> dict:
        """Save everything, pull in changes from other devices, send ours.

        Network steps run without holding the write lock, so typing never
        waits on a slow connection."""
        if not self.sync_lock.acquire(blocking=False):
            return {"ok": True, "copies": [], "message": "Already syncing"}
        self.syncing = True
        try:
            with self.lock:
                self.repo.commit(f"Notes from {device_name()}")
            if not self.published():
                self.last_error = None
                return {"ok": True, "copies": [], "message": "Saved on this device only. Run `radnotes pair` to add a device."}
            note = self.radicle.fetch(self.home)
            for nid in self.radicle.untracked_remotes(self.home):  # someone new wrote here
                name = peer_name(nid)
                if name not in self.repo.peers():
                    try:
                        self.radicle.add_peer(self.home, f"did:key:{nid}", name)
                    except RadError:
                        pass
            peers = self.repo.peers()
            for peer in peers:
                try:
                    self.radicle.fetch_peer(self.home, peer)
                except RadError:
                    pass
            copies = []
            with self.lock:
                for ref in [f"rad/{BRANCH}"] + [f"{p}/{BRANCH}" for p in peers]:
                    try:
                        git(self.home, "rev-parse", "--verify", "-q", ref)
                    except RadError:
                        continue
                    copies += self.repo.merge(ref, "another device")["copies"]
            self.radicle.push(self.home)
            self.last_error = note or None
            return {"ok": True, "copies": copies, "message": note or "Synced"}
        except RadError as exc:
            self.last_error = str(exc)
            return {"ok": False, "copies": [], "message": str(exc)}
        finally:
            self.syncing = False
            self.last_sync = time.time()
            self.sync_lock.release()

    def send(self) -> str:
        """Quick save from the terminal: record and send without waiting to fetch."""
        with self.lock:
            sha = self.repo.commit(f"Notes from {device_name()}")
        if sha and self.published():
            try:
                self.radicle.push(self.home)
                return "Saved and sent."
            except RadError as exc:
                return f"Saved. Couldn't send yet ({exc}); it will go with the next sync."
        return "Saved."

    # pairing devices ------------------------------------------------------------
    def _require_radicle(self) -> dict:
        r = self.radicle
        if not r.available():
            raise ValueError("Install Radicle first: curl -sSf https://radicle.xyz/install | sh")
        me = r.identity()
        if not me:
            raise ValueError("Create your Radicle identity first: rad auth")
        return me

    def prepare_hub(self) -> None:
        """Publish personal notes privately and let other devices connect to this one."""
        self._require_radicle()
        if not self.published():
            self.repo.commit(f"Notes from {device_name()}")
            self.radicle.init(self.home, "RadNotes", "Personal notes", private=True)
        self.radicle.ensure_listening(PORT)

    def allow(self, did: str) -> str:
        """Pair another device: it may fetch, write, and is followed directly."""
        did = normalize_did(did)
        if not self.published():
            raise ValueError("This device's notes aren't published yet")
        r = self.radicle
        r.allow(self.home, did)
        r.add_delegate(self.home, did)
        name = peer_name(did)
        if name not in self.repo.peers():
            r.add_peer(self.home, did, name)
        return name

    def join(self, rid: str, address: str | None) -> dict:
        """Replace this device's notes with the shared set, keeping local notes and spaces."""
        r = self.radicle
        me = self._require_radicle()
        if address:
            a = ADDRESS_RE.fullmatch(address.strip())
            if not a:
                raise ValueError("The address looks like z6Mk…@hostname:8776")
            address = f"{a.group(1)}@{a.group(2)}:{a.group(3) or PORT}"
            r.add_connect(address)
            r.connect(address)
        tmp = self.home.with_name(f".{self.home.name}-joining")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            cloned = r.clone(rid, tmp)
        except RadError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RadError(f"Couldn't fetch your notes from the other device: {exc}") from exc
        local = [p.read_text("utf-8") for p in self.home.glob("*.md")]
        spaces = self.home / SHARED
        if spaces.exists():
            shutil.move(str(spaces), str(cloned / SHARED))
        backup = None
        if local:
            backup = self.home.with_name(f"{self.home.name}-before-pairing-{int(time.time())}")
            self.home.rename(backup)
        else:
            shutil.rmtree(self.home, ignore_errors=True)
        shutil.move(str(cloned), str(self.home))
        shutil.rmtree(tmp, ignore_errors=True)
        self._identity()
        self._ensure_repo()
        self.repo = Notebook(self.home)
        for text in local:
            self.create(text)
        for did in r.identity_doc(self.home).get("delegates", []):
            if did != me["did"] and peer_name(did) not in self.repo.peers():
                try:
                    r.add_peer(self.home, did, peer_name(did))
                except RadError:
                    pass
        result = self.sync()
        return {"kept": len(local), "backup": str(backup) if backup else None, "sync": result}


class Library:
    """Personal notes plus every shared space."""

    def __init__(self, home, radicle):
        self.radicle = radicle
        self.personal = Notes(home, radicle)
        self.home = self.personal.home
        self.spaces_dir = self.home / SHARED
        self._cache: dict[str, Notes] = {}

    def spaces(self) -> list[Notes]:
        out = []
        if self.spaces_dir.is_dir():
            for d in sorted(self.spaces_dir.iterdir()):
                if d.is_dir() and not d.name.startswith(".") and (d / ".git").exists():
                    if d.name not in self._cache:
                        self._cache[d.name] = Notes(d, self.radicle, kind="space")
                    out.append(self._cache[d.name])
        return out

    def get(self, space: str | None) -> Notes:
        if not space:
            return self.personal
        for sp in self.spaces():
            if sp.id == space:
                return sp
        raise NotFound("That shared space isn't on this device")

    def find_space(self, ref: str) -> Notes:
        spaces, low = self.spaces(), ref.strip().lower()
        if low.isdigit() and 1 <= int(low) <= len(spaces):
            return spaces[int(low) - 1]
        hits = [sp for sp in spaces if low in (sp.id.lower(), sp.title.lower())] or \
               [sp for sp in spaces if low in sp.title.lower()]
        if len(hits) == 1:
            return hits[0]
        raise NotFound(f"No single shared space matches {ref!r}. See `radnotes spaces`.")

    def _slot(self, title: str) -> Path:
        base = slugify(title)
        path, n = self.spaces_dir / base, 2
        while path.exists():
            path, n = self.spaces_dir / f"{base}-{n}", n + 1
        return path

    def create_space(self, title: str) -> Notes:
        title = (title or "").strip()
        if not title:
            raise ValueError("Give the shared space a name")
        self.personal._require_radicle()
        self.spaces_dir.mkdir(exist_ok=True)
        sp = Notes(self._slot(title), self.radicle, kind="space")
        (sp.home / SPACE_FILE).write_text(json.dumps({"title": title}) + "\n")
        sp.repo.commit(f"Start {title}")
        rid = self.radicle.init(sp.home, repo_name(title), f"RadNotes shared space: {title}", private=False)
        self.radicle.seed_all(rid)
        sp.radicle.push(sp.home)
        self._cache[sp.id] = sp
        return sp

    def join_space(self, code: str) -> Notes:
        rid = parse_space_code(code)
        if not rid:
            raise ValueError("That isn't an invite code. It looks like rn-z… or veggiepi-123456.")
        for sp in self.spaces():
            if self.radicle.rid(sp.home) == rid:
                raise ValueError(f"You're already in {sp.title}")
        self.personal._require_radicle()
        self.spaces_dir.mkdir(exist_ok=True)
        tmp = self.spaces_dir / ".joining"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            self.radicle.seed_all(rid)
            cloned = self.radicle.clone(rid, tmp)
        except RadError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RadError(f"Couldn't find that shared space on the network yet ({exc}). "
                           "Check the code, or try again in a minute.") from exc
        try:
            title = json.loads((cloned / SPACE_FILE).read_text())["title"]
        except (OSError, ValueError, KeyError):
            title = cloned.name
        dest = self._slot(title)
        shutil.move(str(cloned), str(dest))
        shutil.rmtree(tmp, ignore_errors=True)
        sp = Notes(dest, self.radicle, kind="space")
        sp._identity()
        self._cache[sp.id] = sp
        sp.sync()
        return sp

    def sync_all(self) -> dict:
        results = [self.personal.sync()] + [sp.sync() for sp in self.spaces()]
        copies = [c for r in results for c in r["copies"]]
        failed = [r["message"] for r in results if not r["ok"]]
        return {"ok": not failed, "copies": copies, "message": failed[0] if failed else results[0]["message"]}


class ChangedElsewhere(Exception):
    def __init__(self, current: str):
        super().__init__("This note changed on another device")
        self.current = current


