"use strict";

const TOKEN = document.querySelector('meta[name="radnotes-token"]').content;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const editor = $("#editor");

const store = { get: (k) => { try { return localStorage.getItem("rn:" + k); } catch { return null; } },
                set: (k, v) => { try { localStorage.setItem("rn:" + k, v); } catch {} } };
const S = { notes: [], spaces: [], space: store.get("space") || "", id: null, version: null,
            dirty: false, timer: null, preview: false, query: "" };
// Every notes call goes to the space being viewed ("" is your own notes).
const inSpace = (path) => path + (path.includes("?") ? "&" : "?") + "space=" + encodeURIComponent(S.space);

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: { "X-RadNotes-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.detail || res.statusText); e.status = res.status; e.data = data; throw e; }
  return data;
}

function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// ---------------------------------------------------------------- list & status
async function refresh() {
  let data;
  try { data = await api("GET", inSpace(`/api/notes?q=${encodeURIComponent(S.query)}`)); }
  catch (e) { if (e.status === 404 && S.space) { S.space = ""; store.set("space", ""); return refresh(); } throw e; }
  const { notes, status, spaces } = data;
  S.notes = notes;
  S.spaces = spaces;
  renderSpaces();
  renderList();
  renderSync(status);
  // Pick up changes synced from another device, unless you're mid-edit.
  const mine = notes.find((n) => n.id === S.id);
  if (mine && mine.version !== S.version && !S.dirty) await open(S.id);
}

function renderSpaces() {
  const sel = $("#space");
  sel.hidden = !S.spaces.length;
  sel.innerHTML = `<option value="">My notes</option>` +
    S.spaces.map((sp) => `<option value="${esc(sp.id)}">${esc(sp.title)} (shared)</option>`).join("");
  sel.value = S.space;
}

async function switchSpace(space) {
  await leave();
  S.space = space;
  store.set("space", space);
  S.id = null;
  S.query = "";
  $("#search").value = "";
  await refresh();
  const last = store.get("last:" + space);
  const pick = S.notes.find((n) => n.id === last) || S.notes[0];
  if (pick) await open(pick.id);
  else await newNote();
}

function renderList() {
  $("#list").innerHTML = S.notes.map((n) =>
    `<li><a href="#" data-id="${esc(n.id)}" class="${n.id === S.id ? "active" : ""}">
       <b>${esc(n.title)}</b><small>${when(n.modified)}${n.preview ? " · " + esc(n.preview) : ""}</small></a></li>`).join("")
    || `<li><small>${S.query ? "Nothing found." : "No notes yet."}</small></li>`;
}

function renderSync(s) {
  const el = $("#sync");
  el.className = "secondary";
  if (s.syncing) el.textContent = "Syncing…";
  else if (!s.published) el.textContent = "On this device only";
  else if (s.error) { el.textContent = "Not synced"; el.className = "secondary error"; }
  else if (s.last_sync) el.textContent = `Synced ${agoText(s.last_sync)}`;
  else el.textContent = "Sync";
  el.title = s.error || (s.published ? "Sync now" : "Use Share to add your other devices");
}

function agoText(ts) {
  const s = Date.now() / 1000 - ts;
  return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : when(ts);
}

// ---------------------------------------------------------------- editing
async function open(id) {
  await leave();
  const n = await api("GET", inSpace(`/api/notes/${encodeURIComponent(id)}`));
  S.id = n.id;
  S.version = n.version;
  S.dirty = false;
  editor.value = n.text;
  store.set("last:" + S.space, n.id);
  $("#saved").textContent = "Saved";
  if (S.preview) await showPreview();
  renderList();
}

// Leaving a blank note deletes it, so "New note" never leaves clutter behind.
async function leave() {
  clearTimeout(S.timer);
  if (S.id && !editor.value.trim()) {
    try { await api("DELETE", inSpace(`/api/notes/${encodeURIComponent(S.id)}`)); } catch {}
    S.notes = S.notes.filter((n) => n.id !== S.id);
    S.id = null;
    renderList();
    return;
  }
  await save();
}

editor.addEventListener("input", () => {
  S.dirty = true;
  $("#saved").textContent = "Editing";
  clearTimeout(S.timer);
  S.timer = setTimeout(save, 500);
});

async function save() {
  if (!S.dirty || !S.id) return;
  const text = editor.value;
  S.dirty = false;
  try {
    const r = await api("PUT", inSpace(`/api/notes/${encodeURIComponent(S.id)}`), { text, base: S.version });
    S.version = r.version;
    $("#saved").textContent = "Saved";
    const n = S.notes.find((x) => x.id === S.id);
    if (n) { n.title = text.trim().split("\n")[0].replace(/^#+\s*/, "").slice(0, 120) || "Untitled"; n.modified = Date.now() / 1000; n.version = r.version; renderList(); }
  } catch (e) {
    if (e.status === 409) {
      // Edited on another device meanwhile: keep both versions.
      const copy = await api("POST", inSpace("/api/notes"), { text });
      S.version = null;
      await refresh();
      await open(copy.id);
      $("#saved").textContent = "This note changed on another device, so your edit was saved as a new note";
    } else { S.dirty = true; $("#saved").textContent = "Not saved: " + e.message; }
  }
}

async function newNote() {
  await leave();
  const n = await api("POST", inSpace("/api/notes"), { text: "" });
  S.query = "";
  $("#search").value = "";
  if (S.preview) togglePreview();
  await refresh();
  await open(n.id);
  editor.focus();
}

async function showPreview() {
  const { html } = await api("POST", "/api/render", { text: editor.value });
  $("#preview").innerHTML = html;
}

async function togglePreview() {
  S.preview = !S.preview;
  if (S.preview) await showPreview();
  $("#preview").hidden = !S.preview;
  editor.hidden = S.preview;
  $("#toggle").textContent = S.preview ? "Edit" : "Preview";
}

async function deleteNote() {
  if (!S.id || !confirm("Delete this note?")) return;
  clearTimeout(S.timer);
  await api("DELETE", inSpace(`/api/notes/${encodeURIComponent(S.id)}`));
  S.id = null;
  await refresh();
  if (S.notes.length) await open(S.notes[0].id);
  else { editor.value = ""; $("#saved").textContent = ""; }
}

// ---------------------------------------------------------------- wiring
$("#new").onclick = newNote;
$("#toggle").onclick = (e) => { e.preventDefault(); togglePreview(); };
$("#delete").onclick = (e) => { e.preventDefault(); deleteNote(); };
$("#sync").onclick = async (e) => {
  e.preventDefault();
  await save();
  $("#sync").textContent = "Syncing…";
  const r = await api("POST", inSpace("/api/sync"));
  renderSync(r.status);
  if (r.copies.length) alert("Some notes were edited on two devices. The other versions were saved as separate notes.");
  await refresh();
};
$("#list").addEventListener("click", (e) => {
  const a = e.target.closest("[data-id]");
  if (a) { e.preventDefault(); open(a.dataset.id); }
});
let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { S.query = e.target.value.trim(); refresh(); }, 200);
});
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
});
window.addEventListener("beforeunload", () => { if (S.dirty) save(); });

// ---------------------------------------------------------------- sharing
const dlg = $("#share-dlg");
let pairTimer = null;

function shareMsg(text, error = false) {
  $("#share-msg").className = error ? "error" : "";
  $("#share-msg small").textContent = text;
}

function renderSpacesList() {
  $("#spaces").innerHTML = S.spaces.map((sp) =>
    `<li><span><b>${esc(sp.title)}</b><br><code>${esc(sp.code || "")}</code></span>
       <a href="#" class="secondary" data-copy="${esc(sp.code || "")}">Copy code</a></li>`).join("");
}

async function busy(btn, fn) {
  btn.setAttribute("aria-busy", "true");
  btn.disabled = true;
  shareMsg("");
  try { return await fn(); }
  catch (e) { shareMsg(e.message, true); }
  finally { btn.removeAttribute("aria-busy"); btn.disabled = false; }
}

$("#share").onclick = async (e) => {
  e.preventDefault();
  shareMsg("");
  renderSpacesList();
  dlg.showModal();
};
dlg.addEventListener("click", (e) => {
  if (e.target.matches("[data-close]") || e.target === dlg) { dlg.close(); return; }
  const c = e.target.closest("[data-copy]");
  if (c) {
    e.preventDefault();
    navigator.clipboard.writeText(c.dataset.copy).then(() => { c.textContent = "Copied"; }, () => shareMsg(c.dataset.copy));
  }
});
dlg.addEventListener("close", () => { clearInterval(pairTimer); });

$("#pair-start").onclick = (e) => busy(e.currentTarget, async () => {
  const r = await api("POST", "/api/pair");
  $("#pair-box").innerHTML = `<div class="pair-code">${esc(r.code)}</div>
    <p><small id="pair-state">On the other device, enter this under Share, or run <code>radnotes pair ${esc(r.code)}</code>. Waiting…</small></p>`;
  clearInterval(pairTimer);
  pairTimer = setInterval(async () => {
    const st = await api("GET", "/api/pair").catch(() => ({}));
    if (st.state === "waiting") return;
    clearInterval(pairTimer);
    $("#pair-state").textContent = st.state === "paired" ? `Paired with ${st.paired}. Your notes now sync between the two.`
      : "That code isn't valid anymore. Close this and try again.";
    refresh();
  }, 1500);
});

$("#space-form").onsubmit = (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  busy($("button", form), async () => {
    const sp = await api("POST", "/api/spaces", { name: form.name.value });
    form.reset();
    await refresh();
    renderSpacesList();
    shareMsg(`Created “${sp.title}”. Copy its code and send it to your friends.`);
  });
};

$("#join-form").onsubmit = (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  busy($("button", form), async () => {
    shareMsg("Connecting… this can take a minute.");
    const r = await api("POST", "/api/join", { code: form.code.value });
    form.reset();
    if (r.kind === "space") {
      dlg.close();
      await switchSpace(r.space);
    } else {
      await switchSpace("");
      shareMsg("Paired. This device now has your notes.");
    }
  });
};

$("#space").addEventListener("change", (e) => switchSpace(e.target.value));

(async () => {
  await refresh();
  const last = store.get("last:" + S.space);
  const pick = S.notes.find((n) => n.id === last) || S.notes[0];
  if (pick) await open(pick.id);
  else await newNote();
  setInterval(() => refresh().catch(() => {}), 10000);
})();
