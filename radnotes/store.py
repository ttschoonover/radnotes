"""Notebooks are plain git repositories of markdown files.

Nothing here knows about Radicle except the name of the `rad` remote, so a
notebook is always readable with any editor and `git log`.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from .radicle import BRANCH, RadError, git

META_DIR = ".radnotes"
META_FILE = "notebook.json"
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#\n]+?)(#[^\[\]|\n]*)?(?:\|([^\[\]\n]+))?\]\]")
TAG_RE = re.compile(r"(?:(?<=\s)|^)#([A-Za-z][\w/-]*)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
SHA_RE = re.compile(r"[0-9a-f]{7,40}")

WELCOME = """# {title}

This notebook is a folder of markdown files tracked by git, and it can be
shared peer-to-peer over Radicle.

- Link notes by name: [[Ideas]]. Clicking a link to a note that doesn't exist yet creates it.
- Tag anything with #tags and find it again from search.
- Press **Ctrl/Cmd+S** to save a version you can return to later.
- Open **Share** to publish the notebook and add co-editors by their DID.

- [ ] Write your first note
"""


class NotFound(LookupError):
    pass


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", s)[:60].strip("-") or "notebook"


def link_key(rel: str) -> str:
    return rel[:-3] if rel.lower().endswith(".md") else rel


def prose_lines(text: str):
    """Yield (index, line) for lines outside fenced code blocks."""
    fenced = False
    for i, line in enumerate(text.split("\n")):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield i, line


def title_from(text: str, rel: str) -> str:
    for _, line in prose_lines(text):
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2)
    return Path(rel).stem


def extract_tags(text: str) -> set[str]:
    return {m.group(1) for _, line in prose_lines(text)
            if not HEADING_RE.match(line) for m in TAG_RE.finditer(line)}


def _snippet(line: str, q: str) -> str:
    i = line.lower().find(q)
    start = max(0, i - 50)
    return ("…" if start else "") + line[start:i + len(q) + 90].strip()


def ensure_git_identity(path, name: str | None) -> None:
    for key, fallback in (("user.name", name or "RadNotes user"),
                          ("user.email", f"{slugify(name or 'user')}@radnotes.invalid")):
        try:
            git(path, "config", key)
        except RadError:
            git(path, "config", key, fallback)


class Notebook:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.id = self.path.name

    # metadata ----------------------------------------------------------------
    def meta(self) -> dict:
        try:
            data = json.loads((self.path / META_DIR / META_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        return {"title": data.get("title") or self.id,
                "description": data.get("description", ""),
                "created": data.get("created")}

    def set_meta(self, **changes) -> dict:
        data = self.meta()
        data.update({k: v for k, v in changes.items() if v is not None})
        (self.path / META_DIR).mkdir(exist_ok=True)
        (self.path / META_DIR / META_FILE).write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        return data

    def published(self) -> bool:
        try:
            return bool(git(self.path, "config", "--get", "remote.rad.url").strip())
        except RadError:
            return False

    # files -------------------------------------------------------------------
    def resolve(self, rel: str) -> tuple[Path, str]:
        rel = (rel or "").replace("\\", "/").strip().strip("/")
        if not rel:
            raise ValueError("A note needs a name")
        if not rel.lower().endswith(".md"):
            rel += ".md"
        if any(part in ("", "..") or part.startswith(".") for part in rel.split("/")):
            raise ValueError("Note names can't start with a dot or contain '..'")
        full = (self.path / rel).resolve()
        if self.path not in full.parents:
            raise ValueError("That path is outside the notebook")
        return full, rel

    def _files(self):
        for root, dirs, files in os.walk(self.path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files, key=str.lower):
                if name.lower().endswith(".md") and not name.startswith("."):
                    full = Path(root) / name
                    yield full, full.relative_to(self.path).as_posix()

    def _texts(self):
        for full, rel in self._files():
            try:
                yield rel, full.read_text("utf-8")
            except (OSError, UnicodeDecodeError):
                continue

    def paths(self) -> list[str]:
        return [rel for _, rel in self._files()]

    def notes(self) -> list[dict]:
        out = []
        for rel, text in self._texts():
            st = (self.path / rel).stat()
            out.append({"path": rel, "title": title_from(text, rel), "modified": st.st_mtime})
        return out

    def read(self, rel: str) -> tuple[str, str]:
        full, rel = self.resolve(rel)
        if not full.is_file():
            raise NotFound(f"There's no note at {rel}")
        return rel, full.read_text("utf-8")

    def write(self, rel: str, content: str) -> str:
        full, rel = self.resolve(rel)
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_name(f".{full.name}.tmp")
        tmp.write_text(content, "utf-8")
        os.replace(tmp, full)
        return rel

    def create(self, rel: str, content: str | None = None) -> str:
        full, rel = self.resolve(rel)
        if full.exists():
            raise ValueError(f"{rel} already exists")
        return self.write(rel, f"# {Path(rel).stem}\n\n" if content is None else content)

    def delete(self, rel: str) -> None:
        full, rel = self.resolve(rel)
        if not full.is_file():
            raise NotFound(f"There's no note at {rel}")
        full.unlink()
        self._prune(full.parent)

    def _prune(self, folder: Path) -> None:
        while folder != self.path and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent

    def rename(self, old: str, new: str) -> dict:
        src, old = self.resolve(old)
        dst, new = self.resolve(new)
        if not src.is_file():
            raise NotFound(f"There's no note at {old}")
        if dst.exists():
            raise ValueError(f"{new} already exists")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        self._prune(src.parent)
        return {"path": new, "updated_links": self._rewrite_links(old, new)}

    def _rewrite_links(self, old: str, new: str) -> int:
        old_keys = {link_key(old).lower(), Path(old).stem.lower()}
        target = Path(new).stem if "/" not in link_key(old) else link_key(new)
        changed_files = 0
        for rel, text in list(self._texts()):
            hits = 0

            def swap(m):
                nonlocal hits
                if m.group(1).strip().lower() not in old_keys:
                    return m.group(0)
                hits += 1
                return f"[[{target}{m.group(2) or ''}{'|' + m.group(3) if m.group(3) else ''}]]"

            updated = WIKILINK_RE.sub(swap, text)
            if hits:
                self.write(rel, updated)
                changed_files += 1
        return changed_files

    # links, tags, search --------------------------------------------------------
    def resolve_link(self, target: str, paths: list[str] | None = None) -> str | None:
        t = target.strip().lower()
        t = t[:-3] if t.endswith(".md") else t
        paths = self.paths() if paths is None else paths
        for rel in paths:
            if link_key(rel).lower() == t:
                return rel
        for rel in paths:
            if Path(rel).stem.lower() == t:
                return rel
        return None

    def backlinks(self, rel: str) -> list[dict]:
        _, rel = self.resolve(rel)
        paths, out = self.paths(), []
        for other, text in self._texts():
            if other == rel:
                continue
            for i, line in prose_lines(text):
                if any(self.resolve_link(m.group(1), paths) == rel for m in WIKILINK_RE.finditer(line)):
                    out.append({"path": other, "title": title_from(text, other),
                                "line": i + 1, "context": line.strip()[:220]})
                    break
        return out

    def tags(self) -> list[dict]:
        counts = Counter(tag for _, text in self._texts() for tag in extract_tags(text))
        return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))]

    def search(self, query: str, limit: int = 60) -> list[dict]:
        q = query.strip().lower()
        if not q:
            return []
        results = []
        for rel, text in self._texts():
            title = title_from(text, rel)
            score = 2 if q in title.lower() or q in rel.lower() else 0
            line_no, snippet = None, ""
            for i, line in enumerate(text.split("\n")):
                if q in line.lower():
                    line_no, snippet, score = i + 1, _snippet(line, q), max(score, 1)
                    break
            if score:
                results.append({"path": rel, "title": title, "line": line_no, "snippet": snippet, "score": score})
        results.sort(key=lambda r: (-r["score"], r["path"].lower()))
        return results[:limit]

    # versions ------------------------------------------------------------------
    def _git(self, *args: str, **kw) -> str:
        return git(self.path, *args, **kw)

    def has_commits(self) -> bool:
        try:
            self._git("rev-parse", "--verify", "-q", "HEAD")
            return True
        except RadError:
            return False

    def changed_files(self) -> list[dict]:
        out = []
        for line in self._git("status", "--porcelain", "-uall").splitlines():
            path = line[3:].split(" -> ")[-1].strip('"')
            out.append({"status": line[:2].strip() or "?", "path": path})
        return out

    def commit(self, message: str | None = None) -> str | None:
        self._git("add", "-A")
        staged = [l for l in self._git("diff", "--cached", "--name-only").splitlines() if l]
        if not staged:
            return None
        if not (message or "").strip():
            names = [Path(f).stem for f in staged if f.endswith(".md")] or staged
            if len(names) == 1:
                message = f"Update {names[0]}"
            elif len(names) <= 3:
                message = f"Update {', '.join(names[:-1])} and {names[-1]}"
            else:
                message = f"Update {names[0]} and {len(names) - 1} other notes"
        self._git("commit", "-q", "-m", message.strip())
        return self._git("rev-parse", "HEAD").strip()

    def history(self, rel: str | None = None, limit: int = 60) -> list[dict]:
        if not self.has_commits():
            return []
        args = ["log", f"-n{limit}", "--format=%x1e%H%x1f%an%x1f%at%x1f%s"]
        if rel:
            _, rel = self.resolve(rel)
            args += ["--follow", "--name-only", "--", rel]
        entries = []
        for chunk in self._git(*args).split("\x1e"):
            lines = [l for l in chunk.strip("\n").split("\n") if l]
            if not lines:
                continue
            sha, author, ts, subject = lines[0].split("\x1f", 3)
            entries.append({"sha": sha, "short": sha[:8], "author": author, "time": int(ts),
                            "message": subject, "path": lines[-1] if rel and len(lines) > 1 else rel})
        return entries

    def _check_sha(self, sha: str) -> str:
        if not SHA_RE.fullmatch(sha or ""):
            raise ValueError("That isn't a version ID")
        return sha

    def at_version(self, sha: str, rel: str) -> str:
        _, rel = self.resolve(rel)
        try:
            return self._git("show", f"{self._check_sha(sha)}:{rel}")
        except RadError as exc:
            raise NotFound(f"{rel} didn't exist in that version") from exc

    def diff(self, sha: str, rel: str | None = None) -> str:
        args = ["show", "--format=", "--patch", "--no-color", "--find-renames", self._check_sha(sha)]
        if rel:
            args += ["--", self.resolve(rel)[1]]
        return self._git(*args)

    def working_diff(self) -> str:
        if not self.has_commits():
            return ""
        self._git("add", "--intent-to-add", "--", ".")
        return self._git("diff", "--no-color", "HEAD")

    def restore(self, sha: str, rel_at: str, rel_now: str) -> str:
        content = self.at_version(sha, rel_at)
        self.write(rel_now, content)
        return content

    # sharing --------------------------------------------------------------------
    def sync_state(self) -> dict:
        state = {"changed": len(self.changed_files()), "ahead": None, "behind": None}
        try:
            out = self._git("rev-list", "--left-right", "--count", f"{BRANCH}...rad/{BRANCH}")
            state["ahead"], state["behind"] = (int(x) for x in out.split())
        except (RadError, ValueError):
            pass
        return state

    def peers(self) -> list[str]:
        return [r for r in self._git("remote").split() if r != "rad"]

    def merge(self, ref: str, label: str) -> dict:
        """Merge ``ref`` into the notebook. Conflicting notes keep the local
        text, and the other side's text is saved beside them as a copy."""
        self.commit(f"Save work before getting changes from {label}")
        before = self._git("rev-parse", "HEAD").strip()
        try:
            self._git("merge", "--no-edit", "-m", f"Get changes from {label}", ref)
        except RadError:
            conflicted = [l for l in self._git("diff", "--name-only", "--diff-filter=U").splitlines() if l]
            try:
                self._git("merge", "--abort")
            except RadError:
                pass
            if not conflicted:
                raise
            try:
                self._git("merge", "--no-edit", "-X", "ours", "-m",
                          f"Get changes from {label} (kept local text where both edited)", ref)
            except RadError as exc:
                try:
                    self._git("merge", "--abort")
                except RadError:
                    pass
                raise RadError(f"Couldn't combine changes from {label} automatically; "
                               "resolve it with git in the notebook folder", output=exc.output) from exc
            safe = re.sub(r"[^\w -]+", "", label).strip() or "peer"
            copies = []
            for rel in conflicted:
                try:
                    theirs = self._git("show", f"{ref}:{rel}")
                except RadError:
                    continue
                p = Path(rel)
                copy, n = (p.parent / f"{p.stem} (from {safe}).md").as_posix(), 2
                while (self.path / copy).exists():
                    copy, n = (p.parent / f"{p.stem} (from {safe} {n}).md").as_posix(), n + 1
                self.write(copy, theirs)
                copies.append(copy)
            self.commit(f"Keep {safe}'s text for notes both sides edited")
            return {"status": "conflicts", "conflicts": conflicted, "copies": copies}
        after = self._git("rev-parse", "HEAD").strip()
        return {"status": "merged" if after != before else "up-to-date", "conflicts": [], "copies": []}


class Workspace:
    """A folder whose subfolders are notebooks."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def notebooks(self) -> list[Notebook]:
        return [Notebook(p) for p in sorted(self.root.iterdir())
                if p.is_dir() and not p.name.startswith(".") and (p / ".git").exists()]

    def get(self, nb_id: str) -> Notebook:
        if not re.fullmatch(r"[\w][\w.-]*", nb_id or ""):
            raise NotFound("There's no notebook with that name")
        path = self.root / nb_id
        if not (path / ".git").exists():
            raise NotFound(f"There's no notebook called {nb_id}")
        return Notebook(path)

    def create(self, title: str, description: str = "", author: str | None = None) -> Notebook:
        title = (title or "").strip()
        if not title:
            raise ValueError("Give the notebook a title")
        base = slugify(title)
        path, n = self.root / base, 2
        while path.exists():
            path, n = self.root / f"{base}-{n}", n + 1
        path.mkdir()
        git(path, "init", "-q")
        git(path, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
        ensure_git_identity(path, author)
        nb = Notebook(path)
        nb.set_meta(title=title, description=description.strip(), created=int(time.time()))
        nb.write("Welcome.md", WELCOME.format(title=title))
        nb.commit("Create notebook")
        return nb

    def adopt(self, path, author: str | None = None) -> Notebook:
        ensure_git_identity(path, author)
        return Notebook(path)
