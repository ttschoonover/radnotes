import io
import socket
import threading

import pytest
from fastapi.testclient import TestClient

from radnotes.cli import main
from radnotes.notes import ChangedElsewhere, Library, Notes, parse_space_code
from radnotes.pairing import Invitation, is_device_code, pair_with
from radnotes.radicle import MockRadicle
from radnotes.server import create_app


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def net(tmp_path):
    def device(name):
        home = tmp_path / name
        return Library(home / "RadNotes", MockRadicle(home / "rad", network=tmp_path / "net", alias=name))
    return device


@pytest.fixture
def rn(tmp_path, monkeypatch, capsys):
    """Run the CLI as a named device on a shared offline network."""
    monkeypatch.setenv("RADNOTES_MOCK_NETWORK", str(tmp_path / "net"))

    def run(device, *argv, stdin=None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = main(["--mock", "--alias", device, "--home", str(tmp_path / device / "RadNotes"), *argv])
        out, err = capsys.readouterr()
        return code, out + err
    return run


# -------------------------------------------------------------------- notes
def test_notes_basics(tmp_path):
    n = Notes(tmp_path / "RadNotes", MockRadicle(tmp_path / "m"))
    a = n.create("# Groceries\n\noat milk\n")
    b = n.create("Thesis idea\nlocal state capitalism")
    assert n.find("thesis") == b and n.find(a) == a
    assert [x["id"] for x in n.search("oat")] == [a]
    v = n.read(a)["version"]
    n.write(a, "# Groceries\n\noat milk, bread\n", base=v)
    with pytest.raises(ChangedElsewhere):
        n.write(a, "stale edit", base=v)
    with pytest.raises(ValueError):
        n.read("../etc/passwd")
    assert n.sync()["ok"] and n.status()["changed"] == 0
    assert "shared/" in (tmp_path / "RadNotes" / ".gitignore").read_text()


def test_old_notebooks_are_imported(tmp_path):
    home = tmp_path / "RadNotes"
    (home / "class-notes" / ".git").mkdir(parents=True)
    (home / "class-notes" / "Econ.md").write_text("# Econ\nSolow\n")
    n = Notes(home, MockRadicle(tmp_path / "m"))
    assert [x["title"] for x in n.list()] == ["Econ"]


def test_codes_are_told_apart():
    assert is_device_code("veggiepi-482913") and is_device_code("my-mac.local-000001")
    assert not is_device_code("rn-z3gqcJUoA1n9HaHKufZs5FCSGazv5") and not is_device_code("veggiepi-48291")
    assert parse_space_code("rn-z3gqcJUoA1n9HaHKufZs5FCSGazv5") == "rad:z3gqcJUoA1n9HaHKufZs5FCSGazv5"
    assert parse_space_code("rad:z3gqcJUoA1n9HaHKufZs5FCSGazv5") == "rad:z3gqcJUoA1n9HaHKufZs5FCSGazv5"


# -------------------------------------------------------------------- devices
def test_pair_two_devices_with_a_short_code(net):
    pi, mac = net("pi"), net("mac")
    pi.personal.create("thought from home\n")
    mac.personal.create("written on the mac before pairing\n")

    port = free_port()
    inv = Invitation(pi.personal, host="127.0.0.1", port=port).start()
    assert inv.code.startswith("127.0.0.1-") and len(inv.code.rsplit("-", 1)[1]) == 6
    result = pair_with(mac.personal, inv.code, port=port)
    assert inv.wait() == "paired" and result["kept"] == 1

    titles = {n["title"] for n in mac.personal.list()}
    assert titles == {"thought from home", "written on the mac before pairing"}
    mac.personal.create("idea at school\n")
    mac.personal.send()
    pi.personal.sync()
    assert "idea at school" in {n["title"] for n in pi.personal.list()}


def test_wrong_codes_lock_the_invitation(net):
    pi, mac = net("pi"), net("mac")
    port = free_port()
    inv = Invitation(pi.personal, host="127.0.0.1", port=port).start()
    wrong = "127.0.0.1-" + ("000000" if inv.pin != "000000" else "111111")
    for _ in range(5):
        with pytest.raises(ValueError, match="doesn't match"):
            pair_with(mac.personal, wrong, port=port)
    assert inv.state == "locked"
    with pytest.raises(ValueError):
        pair_with(mac.personal, inv.code, port=port)  # even the right code no longer works
    inv.close()


def test_unreachable_device_is_explained(net):
    with pytest.raises(ValueError, match="Couldn't reach"):
        pair_with(net("mac").personal, "127.0.0.1-123456", port=free_port())


# -------------------------------------------------------------------- friends
def test_shared_space_with_friends(net):
    ada, bo, cy = net("ada"), net("bo"), net("cy")
    ada.personal.create("ada's private note\n")
    space = ada.create_space("Book club")
    assert space.code.startswith("rn-z")

    for friend in (bo, cy):
        joined = friend.join_space(space.code)
        assert joined.title == "Book club"

    bo_space, cy_space = bo.spaces()[0], cy.spaces()[0]
    bo_space.create("Next book: The Dispossessed\n")
    bo_space.send()
    cy_space.create("I'll bring snacks\n")
    cy_space.send()

    for lib in (ada, bo, cy):
        lib.sync_all()
    ada.sync_all()  # second round picks up anyone discovered in the first

    for lib in (ada, bo, cy):
        titles = {n["title"] for n in lib.spaces()[0].list()}
        assert {"Next book: The Dispossessed", "I'll bring snacks"} <= titles
        assert "ada's private note" not in titles
    assert all("Book club" not in n["title"] for n in bo.personal.list())  # personal stays separate
    with pytest.raises(ValueError, match="already in"):
        bo.join_space(space.code)


def test_join_with_a_bad_code(net):
    with pytest.raises(ValueError, match="invite code"):
        net("bo").join_space("hello")


# -------------------------------------------------------------------- terminal
def test_terminal_sharing(rn):
    code, out = rn("ada", "share", "Book", "club")
    assert code == 0
    invite = out.split("code:\n\n")[1].split()[0]
    code, out = rn("bo", "join", invite)
    assert code == 0 and "Book club" in out
    rn("bo", "new", "-s", "book", "chapter 3 thoughts")
    rn("ada", "sync")
    assert "chapter 3 thoughts" in rn("ada", "ls", "-s", "1")[1]
    assert invite in rn("ada", "spaces")[1]
    assert "Shared spaces: Book club" in rn("ada", "ls")[1]


def test_terminal_basics(rn, monkeypatch):
    rn("pi", "new", "first thought")
    assert "first thought" in rn("pi", "ls")[1]
    assert "first thought" in rn("pi", "show", "1")[1]
    monkeypatch.setenv("EDITOR", "no-such-editor")
    code, out = rn("pi", "edit", "1")
    assert code == 1 and "export EDITOR" in out
    assert rn("pi", "rm", "-y", "1")[0] == 0


# -------------------------------------------------------------------- web app
def test_web_app(net):
    lib = net("ada")
    c = TestClient(create_app(lib, token="t", autosync=False))
    assert c.get("/api/notes").status_code == 401
    c.headers["X-RadNotes-Token"] = "t"
    assert c.get("/api/notes", headers={"host": "evil.example"}).status_code == 403

    note = c.post("/api/notes", json={"text": "# Hi\n"}).json()
    assert c.put(f"/api/notes/{note['id']}", json={"text": "# Hi there\n", "base": note["version"]}).status_code == 200
    stale = c.put(f"/api/notes/{note['id']}", json={"text": "old", "base": note["version"]})
    assert stale.status_code == 409

    sp = c.post("/api/spaces", json={"name": "Trip"}).json()
    assert sp["code"].startswith("rn-")
    c.post(f"/api/notes?space={sp['id']}", json={"text": "Pack boots\n"})
    listing = c.get(f"/api/notes?space={sp['id']}").json()
    assert [n["title"] for n in listing["notes"]] == ["Pack boots"] and listing["title"] == "Trip"
    assert [n["title"] for n in c.get("/api/notes").json()["notes"]] == ["Hi there"]
    assert c.get("/api/notes?space=nope").status_code == 404
    assert c.post("/api/join", json={"code": "nonsense"}).status_code == 400
    assert c.post("/api/sync").json()["ok"]
    assert "<script" not in c.post("/api/render", json={"text": "<script>x</script>"}).json()["html"]


def test_same_note_edited_on_two_devices_keeps_both(net):
    pi, mac = net("pi"), net("mac")
    note = pi.personal.create("draft\n")
    port = free_port()
    inv = Invitation(pi.personal, host="127.0.0.1", port=port).start()
    pair_with(mac.personal, inv.code, port=port)
    inv.wait()

    mac.personal.write(note, "mac version\n")
    mac.personal.send()
    pi.personal.write(note, "pi version\n")
    result = pi.personal.sync()
    assert result["copies"]
    titles = {n["title"] for n in pi.personal.list()}
    assert {"pi version", "mac version"} <= titles
    mac.personal.sync()
    assert {"pi version", "mac version"} <= {n["title"] for n in mac.personal.list()}
