"""Radicle and git plumbing.

Every call to the `rad` CLI lives in this module, so if a Radicle release
renames a flag there is exactly one place to fix it. Two backends share one
interface:

* ``Radicle``      drives the real `rad` CLI (Radicle 1.x) and git's `rad` remote.
* ``MockRadicle``  simulates the network with bare git repos in a local folder,
                   so the whole app (including sharing between two identities)
                   can be tried and tested without Radicle installed.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
RID_RE = re.compile(rf"rad:z[{B58}]{{20,}}")
DID_RE = re.compile(rf"did:key:z6Mk[{B58}]{{20,}}")
NID_RE = re.compile(rf"z6Mk[{B58}]{{20,}}")
PEER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BRANCH = "main"


class RadError(RuntimeError):
    """A git or rad command failed. ``output`` holds the full stderr/stdout."""

    def __init__(self, message: str, cmd: list[str] | None = None, output: str = ""):
        super().__init__(message)
        self.cmd = cmd or []
        self.output = output


def run(cmd: list[str], cwd=None, timeout: int = 120) -> str:
    env = {**os.environ, "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, env=env, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RadError(f"`{cmd[0]}` is not installed or not on PATH", cmd) from exc
    except subprocess.TimeoutExpired as exc:
        raise RadError(f"`{' '.join(cmd[:3])}` timed out after {timeout}s", cmd) from exc
    out, err = ANSI_RE.sub("", proc.stdout or ""), ANSI_RE.sub("", proc.stderr or "")
    if proc.returncode != 0:
        detail = (err or out).strip()
        lines = [l.strip() for l in detail.splitlines() if l.strip()]
        raise RadError(lines[-1] if lines else f"exit status {proc.returncode}", cmd, detail)
    return out


def git(path, *args: str, timeout: int = 120) -> str:
    return run(["git", *args], cwd=path, timeout=timeout)


def normalize_did(value: str) -> str:
    """Accept a DID or a bare node ID and return a DID."""
    value = (value or "").strip()
    if DID_RE.fullmatch(value):
        return value
    if NID_RE.fullmatch(value):
        return f"did:key:{value}"
    raise ValueError("That doesn't look like a Radicle DID (did:key:z6Mk…) or node ID (z6Mk…)")


def repo_name(title: str) -> str:
    """Radicle repo names allow only letters, digits, '-', '_' and '.'."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", title.strip()).strip("-.")
    return name[:64] or "notebook"


def short_did(did: str) -> str:
    key = did.removeprefix("did:key:")
    return f"{key[:8]}…{key[-6:]}" if len(key) > 16 else key


def _normalize_doc(doc: dict) -> dict:
    """Reduce a Radicle identity document to the fields the app uses."""
    vis = doc.get("visibility", {"type": "public"})
    if isinstance(vis, str):
        vis = {"type": vis}
    payload = doc.get("payload", {}).get("xyz.radicle.project", {})
    delegates = [d if d.startswith("did:") else f"did:key:{d}" for d in doc.get("delegates", [])]
    allow = [d if d.startswith("did:") else f"did:key:{d}" for d in vis.get("allow", [])]
    return {
        "name": payload.get("name") or doc.get("name", ""),
        "description": payload.get("description") or doc.get("description", ""),
        "delegates": delegates,
        "threshold": doc.get("threshold", 1),
        "visibility": vis.get("type", "public"),
        "allow": allow,
    }


class Radicle:
    """Backend for the real Radicle network via the `rad` CLI."""

    mock = False

    def __init__(self, binary: str = "rad"):
        self.binary = binary

    def _rad(self, *args: str, cwd=None, timeout: int = 120) -> str:
        return run([self.binary, *args], cwd=cwd, timeout=timeout)

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    # identity & node -------------------------------------------------------
    def identity(self) -> dict | None:
        if not self.available():
            return None
        try:
            did = self._rad("self", "--did", timeout=15).strip()
        except RadError:
            return None  # no key yet: user must run `rad auth`
        try:
            alias = self._rad("self", "--alias", timeout=15).strip()
        except RadError:
            alias = ""
        m = DID_RE.search(did)
        return {"did": m.group(0) if m else did, "alias": alias or "anonymous"}

    def node_status(self) -> dict:
        if not self.available():
            return {"running": False, "detail": "Radicle CLI not found"}
        try:
            out = self._rad("node", "status", timeout=15)
        except RadError as exc:
            return {"running": False, "detail": exc.output or str(exc)}
        low = out.lower()
        return {"running": "not running" not in low and "stopped" not in low, "detail": out.strip()}

    def node_start(self) -> None:
        subprocess.Popen(
            [self.binary, "node", "start"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )

    # repositories ------------------------------------------------------------
    def init(self, path, name: str, description: str, private: bool) -> str:
        self._rad(
            "init", ".", "--name", repo_name(name), "--description", description or name,
            "--default-branch", BRANCH, "--private" if private else "--public",
            "--no-confirm", cwd=path, timeout=180,
        )
        rid = self.rid(path)
        if not rid:
            raise RadError("rad init finished but no repository ID was found")
        return rid

    def rid(self, path) -> str | None:
        try:
            out = self._rad("inspect", cwd=path, timeout=30)
        except RadError:
            return None
        m = RID_RE.search(out)
        return m.group(0) if m else None

    def identity_doc(self, path) -> dict:
        try:
            raw = self._rad("inspect", "--identity", cwd=path, timeout=30)
        except RadError:
            return {}
        try:
            return _normalize_doc(json.loads(raw[raw.find("{"):]))
        except (ValueError, AttributeError):
            return {}

    def _id_update(self, path, title: str, *flags: str) -> None:
        base = ["id", "update", "--title", title, "--description", title, *flags]
        try:
            self._rad(*base, "--no-confirm", cwd=path)
        except RadError as exc:
            text = (exc.output or str(exc)).lower()
            if "no-confirm" in text or "unexpected argument" in text:
                self._rad(*base, cwd=path)  # older releases don't know --no-confirm
            else:
                raise

    def allow(self, path, did: str) -> None:
        self._id_update(path, f"Allow {short_did(did)} to read", "--allow", did)

    def disallow(self, path, did: str) -> None:
        self._id_update(path, f"Remove read access for {short_did(did)}", "--disallow", did)

    def add_delegate(self, path, did: str) -> None:
        self._id_update(path, f"Add co-editor {short_did(did)}", "--delegate", did)

    def make_public(self, path) -> None:
        self._rad("publish", cwd=path)

    def clone(self, rid: str, parent) -> Path:
        parent = Path(parent)
        before = {p.name for p in parent.iterdir()}
        out = self._rad("clone", rid, cwd=parent, timeout=900)
        new = [p for p in parent.iterdir() if p.name not in before and (p / ".git").exists()]
        if not new:
            raise RadError("rad clone reported success but no folder appeared", output=out)
        return new[0]

    # sync ----------------------------------------------------------------------
    def can_push(self, path) -> bool:
        me, doc = self.identity(), self.identity_doc(path)
        return bool(me) and (not doc.get("delegates") or me["did"] in doc["delegates"])

    def push(self, path) -> str:
        # The node announces new refs to connected peers on its own after a push.
        git(path, "push", "-q", "rad", BRANCH, timeout=120)
        return "Sent"

    def fetch(self, path) -> str:
        note = ""
        try:
            self._rad("sync", "--fetch", cwd=path, timeout=60)
        except RadError as exc:
            note = f"Couldn't reach your other devices ({exc})"
        git(path, "fetch", "-q", "rad", timeout=120)
        return note

    # node address & config ---------------------------------------------------
    def nid(self) -> str:
        m = NID_RE.search(self._rad("self", "--nid", timeout=15))
        if not m:
            raise RadError("Couldn't read this device's node ID")
        return m.group(0)

    def _edit_config(self, change) -> None:
        path = Path(self._rad("self", "--config", timeout=15).strip())
        if not path.exists():
            self._rad("config", "init", timeout=15)
        data = json.loads(path.read_text()) if path.exists() else {}
        change(data.setdefault("node", {}))
        path.write_text(json.dumps(data, indent=2) + "\n")

    def add_connect(self, address: str) -> None:
        self._edit_config(lambda n: address in n.setdefault("connect", []) or n["connect"].append(address))

    def set_listen(self, address: str) -> None:
        self._edit_config(lambda n: n.__setitem__("listen", [address]))

    def ensure_listening(self, port: int) -> bool:
        """Make the node accept connections from your other devices. Returns True if changed."""
        path = Path(self._rad("self", "--config", timeout=15).strip())
        try:
            listen = json.loads(path.read_text()).get("node", {}).get("listen", [])
        except (OSError, ValueError):
            listen = []
        if any(a.endswith(f":{port}") for a in listen):
            return False
        self.set_listen(f"0.0.0.0:{port}")
        self.node_restart()
        return True

    def seed_all(self, rid: str) -> None:
        """Follow everyone who writes to this repository, not just its owners."""
        self._rad("seed", rid, "--scope", "all", "--no-fetch", timeout=60)

    def untracked_remotes(self, path) -> list[str]:
        """Node IDs that have written to this repository but aren't followed in this folder yet."""
        try:
            out = self._rad("remote", "list", "--untracked", cwd=path, timeout=30)
        except RadError:
            return []
        me = self.nid()
        return [n for n in dict.fromkeys(NID_RE.findall(out)) if n != me]

    def connect(self, address: str) -> None:
        try:
            self._rad("node", "connect", address, timeout=30)
        except RadError:
            pass

    def node_restart(self) -> None:
        try:
            self._rad("node", "stop", timeout=30)
        except RadError:
            pass
        time.sleep(1)
        self.node_start()

    def add_peer(self, path, did: str, name: str) -> None:
        self._rad("remote", "add", did, "--name", name, "--no-sync", cwd=path, timeout=60)

    def fetch_peer(self, path, name: str) -> None:
        git(path, "fetch", "-q", name, timeout=120)


class MockRadicle:
    """Offline stand-in: the 'network' is a folder of bare git repos.

    Point two workspaces at the same ``network`` folder to try sharing between
    two identities on one machine.
    """

    mock = True

    def __init__(self, home, network=None, alias: str | None = None):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.net = Path(network or os.environ.get("RADNOTES_MOCK_NETWORK") or self.home / ".mock-network")
        self.net.mkdir(parents=True, exist_ok=True)
        self._id_file = self.home / ".mock-identity.json"
        if not self._id_file.exists():
            rand = "".join(secrets.choice(B58) for _ in range(44))
            self._id_file.write_text(json.dumps({
                "did": f"did:key:z6Mk{rand}",
                "alias": alias or os.environ.get("USER") or "you",
            }))
        self.node_running = True

    # registry of published repos, shared by everyone on the mock network
    def _load(self) -> dict:
        try:
            return json.loads((self.net / "registry.json").read_text())
        except (OSError, ValueError):
            return {"repos": {}}

    def _save(self, state: dict) -> None:
        (self.net / "registry.json").write_text(json.dumps(state, indent=2))

    def _bare(self, rid: str) -> Path:
        return self.net / f"{rid.split(':', 1)[1]}.git"

    def _entry(self, path):
        rid = self.rid(path)
        if not rid:
            raise RadError("This notebook hasn't been published yet")
        state = self._load()
        if rid not in state["repos"]:
            raise RadError("Repository not found on the mock network")
        return state, rid, state["repos"][rid]

    def _require_delegate(self, entry) -> None:
        if self.identity()["did"] not in entry["delegates"]:
            raise RadError("Only co-editors can change this notebook's identity")

    def available(self) -> bool:
        return True

    def identity(self) -> dict:
        return json.loads(self._id_file.read_text())

    def node_status(self) -> dict:
        return {"running": self.node_running, "detail": "Offline demo node (no network traffic)"}

    def node_start(self) -> None:
        self.node_running = True

    def init(self, path, name: str, description: str, private: bool) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise RadError(f"invalid value '{name}' for '--name <NAME>': invalid repository name")
        rid = "rad:z" + "".join(secrets.choice(B58) for _ in range(28))
        bare = self._bare(rid)
        git(self.net, "init", "-q", "--bare", str(bare))
        git(bare, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
        git(path, "remote", "add", "rad", str(bare))
        git(path, "config", "radnotes.rid", rid)
        git(path, "push", "-q", "rad", f"{BRANCH}:{BRANCH}")
        state = self._load()
        state["repos"][rid] = {
            "name": name, "description": description,
            "visibility": "private" if private else "public",
            "allow": [], "delegates": [self.identity()["did"]], "threshold": 1,
        }
        self._save(state)
        return rid

    def rid(self, path) -> str | None:
        try:
            return git(path, "config", "--get", "radnotes.rid").strip() or None
        except RadError:
            return None

    def identity_doc(self, path) -> dict:
        try:
            return dict(self._entry(path)[2])
        except RadError:
            return {}

    def _mutate(self, path, fn) -> None:
        state, _, entry = self._entry(path)
        self._require_delegate(entry)
        fn(entry)
        self._save(state)

    def allow(self, path, did: str) -> None:
        self._mutate(path, lambda e: did in e["allow"] or e["allow"].append(did))

    def disallow(self, path, did: str) -> None:
        self._mutate(path, lambda e: e.__setitem__("allow", [d for d in e["allow"] if d != did]))

    def add_delegate(self, path, did: str) -> None:
        self._mutate(path, lambda e: did in e["delegates"] or e["delegates"].append(did))

    def make_public(self, path) -> None:
        self._mutate(path, lambda e: e.__setitem__("visibility", "public"))

    def clone(self, rid: str, parent) -> Path:
        entry = self._load()["repos"].get(rid)
        if not entry:
            raise RadError("No notebook with that ID is on the network")
        me = self.identity()["did"]
        if entry["visibility"] == "private" and me not in entry["delegates"] + entry["allow"]:
            raise RadError("This notebook is private and your DID isn't on its access list")
        base = re.sub(r"[^\w-]+", "-", entry["name"].lower()).strip("-") or "notebook"
        dest, n = Path(parent) / base, 2
        while dest.exists():
            dest, n = Path(parent) / f"{base}-{n}", n + 1
        git(parent, "clone", "-q", "-o", "rad", str(self._bare(rid)), dest.name)
        git(dest, "config", "radnotes.rid", rid)
        return dest

    def can_push(self, path) -> bool:
        try:
            return self.identity()["did"] in self._entry(path)[2]["delegates"]
        except RadError:
            return False

    def push(self, path) -> str:
        # Like Radicle: everyone pushes to their own namespace; owners also move the canonical branch.
        git(path, "push", "-q", "-f", "rad", f"{BRANCH}:refs/heads/peers/{self.nid()}")
        if self.can_push(path):
            git(path, "push", "-q", "rad", f"{BRANCH}:{BRANCH}")
        return "Sent to the offline demo network"

    def fetch(self, path) -> str:
        git(path, "fetch", "-q", "rad")
        return ""

    def nid(self) -> str:
        return self.identity()["did"].removeprefix("did:key:")

    def add_connect(self, address: str) -> None:
        pass

    def set_listen(self, address: str) -> None:
        pass

    def ensure_listening(self, port: int) -> bool:
        return False

    def seed_all(self, rid: str) -> None:
        pass

    def untracked_remotes(self, path) -> list[str]:
        _, rid, _ = self._entry(path)
        heads = git(self._bare(rid), "for-each-ref", "--format=%(refname)", "refs/heads/peers/")
        try:
            tracked = set(git(path, "config", "--get-regexp", r"remote\..*\.radnotes-nid").split()[1::2])
        except RadError:
            tracked = set()
        nids = [r.rsplit("/", 1)[1] for r in heads.split()]
        return [n for n in nids if n != self.nid() and n not in tracked]

    def connect(self, address: str) -> None:
        pass

    def node_restart(self) -> None:
        self.node_running = True

    def add_peer(self, path, did: str, name: str) -> None:
        _, rid, _ = self._entry(path)
        nid = did.removeprefix("did:key:")
        git(path, "remote", "add", "-t", "x", name, str(self._bare(rid)))
        git(path, "config", f"remote.{name}.fetch", f"+refs/heads/peers/{nid}:refs/remotes/{name}/{BRANCH}")
        git(path, "config", f"remote.{name}.radnotes-nid", nid)

    def fetch_peer(self, path, name: str) -> None:
        try:
            git(path, "fetch", "-q", name)
        except RadError:
            pass  # that device hasn't sent anything yet
