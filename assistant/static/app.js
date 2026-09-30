/* News Assistant UI: vanilla JS, hash routing, SSE streaming. */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const el = (tag, attrs, children) => {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) n.setAttribute(k, v);
    }
    for (const c of [].concat(children || [])) if (c !== null && c !== undefined) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const md = (text) => DOMPurify.sanitize(marked.parse(text || "", { breaks: false, gfm: true }), { ADD_ATTR: ["target"] });
  const fmtDate = (s) => (s ? new Date(s).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");
  const money = (v) => "$" + Number(v || 0).toFixed(2);

  const state = { chats: [], models: null, status: null, feeds: null, currentChat: null, streaming: false };

  // ── API ────────────────────────────────────────────────────────────────
  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
    const r = await fetch(path, init);
    if (r.status === 401) { showLogin(); throw new Error("login required"); }
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) {} throw new Error(m); }
    return r.json();
  }

  async function sseStream(path, body, onEvent) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.status === 401) { showLogin(); throw new Error("login required"); }
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) {} throw new Error(m); }
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const frames = buf.split("\n\n"); buf = frames.pop();
      for (const f of frames) { const m = f.match(/^data:\s*(.+)$/ms); if (!m) continue; try { onEvent(JSON.parse(m[1])); } catch (e) {} }
    }
  }

  // ── Login ──────────────────────────────────────────────────────────────
  function showLogin() { $("#login").classList.remove("hidden"); $("#login-password").focus(); }
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: $("#login-password").value }) });
      if (!r.ok) { $("#login-error").textContent = "Wrong password"; return; }
      $("#login").classList.add("hidden"); $("#login-error").textContent = ""; boot();
    } catch (err) { $("#login-error").textContent = String(err); }
  });

  // ── Sidebar ────────────────────────────────────────────────────────────
  async function refreshChats() {
    state.chats = (await api("/api/chats")).chats;
    const ul = $("#chat-list"); ul.innerHTML = "";
    for (const c of state.chats) {
      const a = el("a", { href: "#/chat/" + c.id, class: state.currentChat && state.currentChat.id === c.id ? "active" : "" }, c.title);
      const del = el("button", { class: "del", title: "Delete", onclick: async (e) => { e.preventDefault(); if (!confirm("Delete this chat?")) return; await api("/api/chats/" + c.id, { method: "DELETE" }); if (location.hash === "#/chat/" + c.id) location.hash = "#/chat"; refreshChats(); } }, "×");
      ul.append(el("li", null, [a, del]));
    }
  }
  async function refreshStatus() {
    try {
      const s = state.status = await api("/api/status");
      const w = s.worker;
      $("#status-bar").innerHTML =
        `<div>${s.scoring_paused ? "⏸ scoring paused" : (w.running ? "⚙ " + esc(w.current || "working") : "✓ worker idle")}</div>` +
        `<div>${s.pending} pending · ${s.unread} unread</div>` +
        `<div>${money(s.usage_today_usd)} today · ${money(s.usage_30d_usd)} / 30d</div>` +
        (s.api_key_set ? "" : `<div class="error">No API key</div>`);
    } catch (e) { /* login flow handles 401 */ }
  }
  $("#new-chat-btn").addEventListener("click", () => { location.hash = "#/chat"; });

  // ── Router ─────────────────────────────────────────────────────────────
  function route() {
    const h = location.hash.replace(/^#\/?/, "") || "chat";
    const parts = h.split("/");
    document.querySelectorAll("#sidebar nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === parts[0]));
    const main = $("#main"); main.innerHTML = "";
    if (parts[0] === "chat") return parts[1] ? viewChat(Number(parts[1])) : viewChatNew();
    if (parts[0] === "reader") return viewReader();
    if (parts[0] === "briefs") {
      if (parts[1] === "new") return viewBriefForm(null);
      if (parts[1] === "edit") return viewBriefForm(Number(parts[2]));
      if (parts[1]) return viewRun(Number(parts[1]));
      return viewBriefs();
    }
    if (parts[0] === "settings") return viewSettings();
    location.hash = "#/chat";
  }
  window.addEventListener("hashchange", route);

  // ── Chat ───────────────────────────────────────────────────────────────
  const QUICK = [
    ["Catch me up", "Catch me up on what I've missed. Look at my unread entries, prioritize by relevance to my interests (read the high-value ones), and give me the substance in a scannable briefing with links. Then tell me which ones you'd mark as read and ask before doing it."],
    ["High-relevance this week", "What are the high-relevance unread items from the last 7 days? Read the top ones and summarize what actually matters."],
    ["Zvi this week", "Find Zvi's posts from the last 7 days, read them, and give me the key theses and the two or three strongest points from each."],
    ["Clear the noise", "Find unread entries scored 3 or lower from the last 30 days, list a sample so I can sanity check, and ask me whether to mark all of them as read."],
  ];

  function modelSelect(current, cls) {
    const s = el("select", { class: cls || "" });
    for (const m of state.models.models) s.append(el("option", { value: m.id, selected: m.id === current ? "" : null }, m.label.split(" (")[0]));
    return s;
  }
  function effortSelect(current, cls) {
    const s = el("select", { class: cls || "" });
    for (const e of state.models.efforts) s.append(el("option", { value: e, selected: e === current ? "" : null }, "effort: " + e));
    return s;
  }

  async function viewChatNew() {
    state.currentChat = null; refreshChats();
    const s = await api("/api/settings");
    const main = $("#main");
    const wrap = el("div", { id: "chat-view" });
    const head = el("div", { class: "chat-head" }, [el("h1", null, "New chat")]);
    const mSel = modelSelect(s.chat_model), eSel = effortSelect(s.chat_effort);
    head.append(mSel, eSel);
    const empty = el("div", { class: "empty" }, [el("h2", null, "Chat with your news"),
      el("p", null, "Ask about anything in your feeds. The assistant can search and read entries, mark them read, and update your interests.")]);
    const quick = el("div", { class: "quick" });
    const ta = el("textarea", { placeholder: "Ask about your news… (Enter to send, Shift+Enter for newline)", rows: 2 });
    const send = el("button", { class: "primary" }, "Send");
    const composer = el("div", { id: "composer" }, [ta, send]);
    const start = async (text) => {
      if (!text.trim()) return;
      send.disabled = true;
      const c = await api("/api/chats", { method: "POST", body: { model: mSel.value, effort: eSel.value } });
      sessionStorage.setItem("pendingMessage:" + c.id, text);
      location.hash = "#/chat/" + c.id;
    };
    for (const [label, text] of QUICK) quick.append(el("button", { onclick: () => start(text) }, label));
    send.addEventListener("click", () => start(ta.value));
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); start(ta.value); } });
    wrap.append(head, empty, quick, composer); main.append(wrap); ta.focus();
  }

  async function viewChat(id) {
    let data;
    try { data = await api("/api/chats/" + id); } catch (e) { $("#main").append(el("div", { class: "empty" }, "Chat not found.")); return; }
    state.currentChat = data.chat; refreshChats();
    const main = $("#main");
    const wrap = el("div", { id: "chat-view" });
    const title = el("h1", null, data.chat.title);
    const mSel = modelSelect(data.chat.model), eSel = effortSelect(data.chat.effort);
    const head = el("div", { class: "chat-head" }, [title, mSel, eSel]);
    if (data.chat.context_type === "brief") head.append(el("a", { href: "#/briefs/" + data.chat.context_id, class: "small" }, "view brief"));
    const messages = el("div", { id: "messages" });
    const ta = el("textarea", { placeholder: "Message… (Enter to send)", rows: 2 });
    const send = el("button", { class: "primary" }, "Send");
    const composer = el("div", { id: "composer" }, [ta, send]);
    wrap.append(head, messages, composer); main.append(wrap);

    for (const m of data.messages) renderStoredMessage(messages, m);
    messages.scrollTop = messages.scrollHeight;

    const doSend = async (text) => {
      text = (text || "").trim(); if (!text || state.streaming) return;
      ta.value = ""; send.disabled = true; state.streaming = true;
      messages.append(el("div", { class: "msg user" }, text));
      let current = null, buf = "", thinkEl = null, statusEl = null;
      const ensureMsg = () => { if (!current) { current = el("div", { class: "msg assistant" }); messages.append(current); } return current; };
      const scroll = () => { messages.scrollTop = messages.scrollHeight; };
      try {
        await sseStream("/api/chats/" + id + "/message", { text, model: mSel.value, effort: eSel.value }, (ev) => {
          if (statusEl && ev.type !== "status") { statusEl.remove(); statusEl = null; }
          if (ev.type === "text") { buf += ev.text; ensureMsg().innerHTML = md(buf); scroll(); }
          else if (ev.type === "thinking") {
            if (!thinkEl) { thinkEl = el("details", { class: "thinking" }, [el("summary", null, "Thinking"), el("div")]); messages.append(thinkEl); }
            thinkEl.lastChild.textContent += ev.text; scroll();
          }
          else if (ev.type === "status") { if (!statusEl) { statusEl = el("div", { class: "status-line" }); messages.append(statusEl); } statusEl.textContent = ev.text; scroll(); }
          else if (ev.type === "tool_call") {
            if (current && buf) { current = null; buf = ""; }
            thinkEl = null;
            messages.append(el("div", { class: "tool-line" }, [el("span", { class: "name" }, ev.name), el("span", { class: "muted" }, summarizeInput(ev.input))])); scroll();
          }
          else if (ev.type === "tool_result") { messages.append(el("div", { class: "tool-line" + (ev.is_error ? " error" : "") }, [el("span", { class: "muted" }, "→ " + (ev.is_error ? "error: " : "") + ev.text)])); scroll(); }
          else if (ev.type === "error") { messages.append(el("div", { class: "tool-line error" }, ev.message)); }
        });
      } catch (err) { messages.append(el("div", { class: "tool-line error" }, String(err))); }
      state.streaming = false; send.disabled = false; ta.focus();
      refreshChats(); refreshStatus();
      if (title.textContent === "New chat") { try { title.textContent = (await api("/api/chats/" + id)).chat.title; refreshChats(); } catch (e) {} }
    };
    send.addEventListener("click", () => doSend(ta.value));
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(ta.value); } });
    const pending = sessionStorage.getItem("pendingMessage:" + id);
    if (pending) { sessionStorage.removeItem("pendingMessage:" + id); doSend(pending); } else ta.focus();
  }

  function summarizeInput(input) {
    if (!input || typeof input !== "object") return "";
    const parts = [];
    for (const [k, v] of Object.entries(input)) {
      if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) continue;
      let s = Array.isArray(v) ? (v.length > 4 ? v.length + " items" : v.join(", ")) : String(v);
      if (s.length > 60) s = s.slice(0, 60) + "…";
      parts.push(k + "=" + s);
    }
    return parts.join(" ");
  }

  function renderStoredMessage(container, m) {
    if (m.role === "user") {
      const text = m.blocks.filter((b) => b.type === "text").map((b) => b.text).join("\n");
      if (text.trim()) container.append(el("div", { class: "msg user" }, text));
      for (const b of m.blocks) if (b.type === "tool_result") container.append(el("div", { class: "tool-line" + (b.is_error ? " error" : "") }, [el("span", { class: "muted" }, "→ " + b.text)]));
      return;
    }
    for (const b of m.blocks) {
      if (b.type === "text" && b.text.trim()) container.append(el("div", { class: "msg assistant", html: md(b.text) }));
      else if (b.type === "thinking" && b.text.trim()) container.append(el("details", { class: "thinking" }, [el("summary", null, "Thinking"), el("div", null, b.text)]));
      else if (b.type === "tool_use") container.append(el("div", { class: "tool-line" }, [el("span", { class: "name" }, b.name), el("span", { class: "muted" }, summarizeInput(b.input))]));
    }
  }

  // ── Reader ─────────────────────────────────────────────────────────────
  async function viewReader() {
    const main = $("#main");
    const page = el("div", { class: "page" }, [el("h1", null, "Reader")]);
    if (!state.feeds) state.feeds = await api("/api/feeds");
    const cat = el("select", null, [el("option", { value: "" }, "All categories")]);
    for (const c of state.feeds.categories) cat.append(el("option", { value: c.id }, c.name));
    const minScore = el("select", null, [["", "Any score"], ["7", "Score ≥ 7"], ["4", "Score ≥ 4"]].map(([v, l]) => el("option", { value: v }, l)));
    const since = el("select", null, [["", "Any time"], ["1", "Last day"], ["7", "Last week"], ["30", "Last month"], ["90", "Last 3 months"]].map(([v, l]) => el("option", { value: v, selected: v === "7" ? "" : null }, l)));
    const unread = el("input", { type: "checkbox", checked: "" });
    const q = el("input", { type: "search", placeholder: "Search…" });
    const list = el("div");
    const markAll = el("button", null, "Mark all shown as read");
    const load = async () => {
      const p = new URLSearchParams();
      if (cat.value) p.set("category_id", cat.value); if (minScore.value) p.set("min_score", minScore.value);
      if (since.value) p.set("since_days", since.value); if (q.value.trim()) p.set("query", q.value.trim());
      p.set("unread_only", unread.checked); p.set("limit", "100");
      const data = await api("/api/entries?" + p);
      list.innerHTML = "";
      if (!data.entries.length) list.append(el("div", { class: "empty" }, "Nothing here."));
      for (const e of data.entries) {
        const badge = e.score === null || e.score === undefined ? el("span", { class: "tag" }, "–")
          : el("span", { class: "badge " + (e.score >= 7 ? "high" : e.score >= 4 ? "mid" : "low"), title: e.reason || "" }, e.score);
        const btn = el("button", { class: "mini", title: e.read ? "Mark unread" : "Mark read" }, e.read ? "↺" : "✓");
        const row = el("div", { class: "entry" + (e.read ? " read" : "") }, [badge,
          el("div", null, [el("a", { class: "title", href: e.url, target: "_blank", rel: "noopener" }, (e.video ? "▶ " : "") + e.title),
            el("div", { class: "meta" }, `${e.feed} · ${e.date}`),
            e.summary ? el("div", { class: "summary" }, e.summary) : null]), btn]);
        btn.addEventListener("click", async () => { await api("/api/entries/mark", { method: "POST", body: { entry_ids: [e.id], read: !e.read } }); e.read = !e.read; row.classList.toggle("read", e.read); btn.textContent = e.read ? "↺" : "✓"; refreshStatus(); });
        row.dataset.id = e.id;
        list.append(row);
      }
      markAll.onclick = async () => { const ids = data.entries.filter((e) => !e.read).map((e) => e.id); if (!ids.length || !confirm(`Mark ${ids.length} entries as read?`)) return; await api("/api/entries/mark", { method: "POST", body: { entry_ids: ids, read: true } }); load(); refreshStatus(); };
    };
    for (const c of [cat, minScore, since, unread]) c.addEventListener("change", load);
    q.addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
    page.append(el("div", { class: "filters" }, [q, cat, minScore, since, el("label", null, [unread, " unread only"]), markAll]), list);
    main.append(page); load();
  }

  // ── Briefs ─────────────────────────────────────────────────────────────
  async function viewBriefs() {
    const main = $("#main");
    const data = await api("/api/briefs");
    const page = el("div", { class: "page" }, [el("div", { class: "row" }, [el("h1", { class: "grow" }, "Briefs"), el("a", { href: "#/briefs/new" }, el("button", { class: "primary" }, "New brief"))])]);
    if (!data.briefs.length) page.append(el("div", { class: "card muted" }, "No briefs yet. A brief is a scheduled summary over a set of feeds, for example a daily Zvi brief at 6:30. Each run is stored here and you can chat about it."));
    for (const b of data.briefs) {
      const running = data.running.includes(b.id);
      const runBtn = el("button", { disabled: running ? "" : null }, running ? "Running…" : "Run now");
      runBtn.addEventListener("click", async () => { runBtn.disabled = true; runBtn.textContent = "Running…"; await api("/api/briefs/" + b.id + "/run", { method: "POST", body: {} }); pollRuns(); });
      const card = el("div", { class: "card" }, [
        el("div", { class: "row" }, [el("strong", { class: "grow" }, b.name), el("span", { class: "tag" }, b.enabled ? "enabled" : "disabled"),
          el("span", { class: "small muted" }, "cron " + b.schedule), runBtn, el("a", { href: "#/briefs/edit/" + b.id }, el("button", null, "Edit"))]),
        el("div", { class: "small muted" }, `Last run: ${b.last_run_at ? fmtDate(b.last_run_at) : "never"}${b.send_email ? " · emails" : ""}`),
      ]);
      const runs = data.runs.filter((r) => r.brief_id === b.id).slice(0, 5);
      if (runs.length) card.append(el("ul", { class: "runs small" }, runs.map((r) => el("li", null, [
        el("a", { href: "#/briefs/" + r.id }, fmtDate(r.started_at)), ` · ${r.status} · ${r.n_entries} items`, r.error ? el("span", { class: "error" }, " · " + r.error) : null]))));
      page.append(card);
    }
    main.append(page);
    let timer = null;
    const pollRuns = () => { clearTimeout(timer); timer = setTimeout(async () => { if (location.hash.startsWith("#/briefs") && !location.hash.includes("/edit") && !location.hash.includes("/new")) { const d = await api("/api/briefs"); if (d.running.length) { route(); } else route(); } }, 5000); };
    if (data.running.length) pollRuns();
  }

  async function viewRun(runId) {
    const main = $("#main");
    const r = await api("/api/runs/" + runId);
    const chatBtn = el("button", { class: "primary" }, "Chat about this brief");
    chatBtn.addEventListener("click", async () => {
      const c = await api("/api/chats", { method: "POST", body: { title: "Brief: " + r.brief_name, context_type: "brief", context_id: String(r.id) } });
      location.hash = "#/chat/" + c.id;
    });
    const rerun = el("button", null, "Re-run (same window)");
    rerun.addEventListener("click", async () => { const hrs = Math.max(1, Math.round((new Date(r.period_end) - new Date(r.period_start)) / 36e5)); await api("/api/briefs/" + r.brief_id + "/run", { method: "POST", body: { window_hours: hrs } }); location.hash = "#/briefs"; });
    const page = el("div", { class: "page" }, [
      el("div", { class: "row" }, [el("h1", { class: "grow" }, r.brief_name), chatBtn, rerun]),
      el("div", { class: "small muted" }, `${fmtDate(r.period_start)} → ${fmtDate(r.period_end)} · ${r.status} · ${(r.entry_ids || []).length} items` + (r.usage ? ` · ${money(r.usage.cost_usd)}` : "")),
      el("div", { class: "card brief-content", html: md(r.content_md || (r.error ? "**Error:** " + esc(r.error) : "_(running)_")) }),
    ]);
    main.append(page);
    if (r.status === "running") setTimeout(() => { if (location.hash === "#/briefs/" + runId) route(); }, 5000);
  }

  async function viewBriefForm(id) {
    const main = $("#main");
    if (!state.feeds) state.feeds = await api("/api/feeds");
    const s = await api("/api/settings");
    let b = { name: "", enabled: true, schedule: "30 6 * * *", feed_ids: [], category_ids: [], lookback_hours: 24, unread_only: false, min_score: 0, instructions: "", model: "", effort: "high", send_email: false };
    if (id) { const d = await api("/api/briefs"); b = d.briefs.find((x) => x.id === id) || b; }
    const f = {};
    f.name = el("input", { value: b.name, placeholder: "e.g. Zvi daily" });
    f.enabled = el("input", { type: "checkbox", checked: b.enabled ? "" : null });
    f.schedule = el("input", { value: b.schedule, placeholder: "cron, e.g. 30 6 * * *" });
    const presets = el("select", null, [["", "presets…"], ["30 6 * * *", "Daily 6:30"], ["0 7 * * 1-5", "Weekdays 7:00"], ["0 8 * * 6", "Saturday 8:00"], ["0 18 * * *", "Daily 18:00"]].map(([v, l]) => el("option", { value: v }, l)));
    presets.addEventListener("change", () => { if (presets.value) f.schedule.value = presets.value; });
    f.lookback_hours = el("input", { type: "number", value: b.lookback_hours, min: 1, max: 336, style: "width:90px" });
    f.unread_only = el("input", { type: "checkbox", checked: b.unread_only ? "" : null });
    f.min_score = el("input", { type: "number", value: b.min_score, min: 0, max: 10, style: "width:70px" });
    f.instructions = el("textarea", { rows: 6, placeholder: "How should this brief be written? e.g. 'Focus on AI policy and lab news; for each Zvi post give the thesis and the strongest three points; skip the rationalist in-jokes.'" }, b.instructions);
    f.model = el("select", null, [el("option", { value: "" }, "default (" + s.brief_model + ")")]);
    for (const m of state.models.models) f.model.append(el("option", { value: m.id, selected: m.id === b.model ? "" : null }, m.label.split(" (")[0]));
    f.effort = effortSelect(b.effort);
    f.send_email = el("input", { type: "checkbox", checked: b.send_email ? "" : null });
    const catChecks = el("div", { class: "checks" }), feedChecks = el("div", { class: "checks" });
    for (const c of state.feeds.categories) {
      catChecks.append(el("label", null, [el("input", { type: "checkbox", value: c.id, checked: b.category_ids.includes(c.id) ? "" : null }), " " + c.name]));
      for (const fd of c.feeds) feedChecks.append(el("label", null, [el("input", { type: "checkbox", value: fd.id, checked: b.feed_ids.includes(fd.id) ? "" : null }), " " + fd.name]));
    }
    const save = el("button", { class: "primary" }, "Save");
    const del = id ? el("button", { class: "danger" }, "Delete") : null;
    const err = el("div", { class: "error" });
    save.addEventListener("click", async () => {
      const body = { name: f.name.value, enabled: f.enabled.checked, schedule: f.schedule.value, lookback_hours: Number(f.lookback_hours.value), unread_only: f.unread_only.checked,
        min_score: Number(f.min_score.value), instructions: f.instructions.value, model: f.model.value, effort: f.effort.value, send_email: f.send_email.checked,
        category_ids: [...catChecks.querySelectorAll("input:checked")].map((i) => Number(i.value)), feed_ids: [...feedChecks.querySelectorAll("input:checked")].map((i) => Number(i.value)) };
      if (!body.category_ids.length && !body.feed_ids.length) { err.textContent = "Pick at least one category or feed."; return; }
      try { if (id) await api("/api/briefs/" + id, { method: "PUT", body }); else await api("/api/briefs", { method: "POST", body }); location.hash = "#/briefs"; }
      catch (e) { err.textContent = String(e.message || e); }
    });
    if (del) del.addEventListener("click", async () => { if (!confirm("Delete this brief and its runs?")) return; await api("/api/briefs/" + id, { method: "DELETE" }); location.hash = "#/briefs"; });
    const page = el("div", { class: "page" }, [el("h1", null, id ? "Edit brief" : "New brief"), el("div", { class: "card" }, [el("div", { class: "form-grid" }, [
      el("label", null, "Name"), f.name,
      el("label", null, "Enabled"), f.enabled,
      el("label", null, "Schedule"), el("div", { class: "row" }, [f.schedule, presets, el("span", { class: "small muted" }, "cron, " + (state.status ? "server timezone" : "")) ]),
      el("label", null, "Categories"), catChecks,
      el("label", null, "Feeds"), feedChecks,
      el("label", null, "First-run lookback (hours)"), el("div", { class: "row" }, [f.lookback_hours, el("span", { class: "small muted" }, "later runs cover everything since the previous run")]),
      el("label", null, "Unread only"), f.unread_only,
      el("label", null, "Minimum score"), el("div", { class: "row" }, [f.min_score, el("span", { class: "small muted" }, "0 = include unscored items too")]),
      el("label", null, "Instructions"), el("div", { class: "full" }, f.instructions),
      el("label", null, "Model"), el("div", { class: "row" }, [f.model, f.effort]),
      el("label", null, "Email it"), el("div", { class: "row" }, [f.send_email, el("span", { class: "small muted" }, state.status && !state.status.email_configured ? "SMTP not configured in .env" : "")]),
    ]), el("div", { class: "row", style: "margin-top:12px" }, [save, del, err])])]);
    main.append(page);
  }

  // ── Settings ───────────────────────────────────────────────────────────
  async function viewSettings() {
    const main = $("#main");
    const [s, feeds, usage] = await Promise.all([api("/api/settings"), api("/api/feeds"), api("/api/usage?days=30")]);
    state.feeds = feeds;
    const st = state.status || (await api("/api/status"));
    const page = el("div", { class: "page" }, [el("h1", null, "Settings")]);

    // Status card
    const runBtn = el("button", null, "Run worker now");
    runBtn.addEventListener("click", async () => { await api("/api/worker/run", { method: "POST" }); runBtn.textContent = "Kicked"; setTimeout(refreshStatus, 2000); });
    const pauseBtn = el("button", null, s.scoring_paused ? "Resume scoring" : "Pause scoring");
    pauseBtn.addEventListener("click", async () => { await api("/api/settings", { method: "PUT", body: { scoring_paused: !s.scoring_paused } }); route(); refreshStatus(); });
    page.append(el("div", { class: "card" }, [el("h2", { style: "margin-top:0" }, "Status"),
      el("div", null, `Worker: ${st.worker.enabled ? (st.worker.running ? "running (" + (st.worker.current || "") + ")" : "idle") : "disabled"} · every ${st.worker.interval_s}s · last cycle ${st.worker.last_cycle ? fmtDate(st.worker.last_cycle) : "never"}`),
      el("div", null, `Pending to score: ${st.pending} · unread entries: ${st.unread} · scored this session: ${st.worker.scored_total}, summarized: ${st.worker.summarized_total}`),
      st.worker.last_error ? el("div", { class: "error" }, "Last error: " + st.worker.last_error) : null,
      el("div", null, `Spend: ${money(st.usage_today_usd)} today · ${money(st.usage_30d_usd)} last 30 days`),
      el("div", { class: "row", style: "margin-top:8px" }, [runBtn, pauseBtn])]));

    // Models / thresholds
    const fields = {};
    const num = (k, w) => (fields[k] = el("input", { type: "number", value: s[k], style: "width:" + (w || 80) + "px" }));
    const chk = (k) => (fields[k] = el("input", { type: "checkbox", checked: s[k] ? "" : null }));
    const mod = (k) => (fields[k] = modelSelect(s[k]));
    const eff = (k) => (fields[k] = effortSelect(s[k]));
    fields.topic_tags = el("input", { value: (s.topic_tags || []).join(", "), style: "width:100%" });
    page.append(el("div", { class: "card" }, [el("h2", { style: "margin-top:0" }, "Models & behavior"), el("div", { class: "settings-grid" }, [
      el("label", null, "Scoring (batch, cheap)"), el("div", { class: "row" }, [mod("scoring_model"), eff("scoring_effort")]),
      el("label", null, "Summaries / details / feedback"), el("div", { class: "row" }, [mod("summary_model"), eff("summary_effort")]),
      el("label", null, "Chat default"), el("div", { class: "row" }, [mod("chat_model"), eff("chat_effort")]),
      el("label", null, "Briefs default"), el("div", { class: "row" }, [mod("brief_model"), eff("brief_effort")]),
      el("label", null, "Full summary at score ≥"), el("div", { class: "row" }, [num("summary_threshold", 70), el("span", { class: "small muted" }, "every scored entry gets a one-line gist; entries at or above this score get a full Opus summary. Feeds flagged Summarize get one from a medium score up.")]),
      el("label", null, "Score entries newer than (days)"), el("div", { class: "row" }, [num("score_lookback_days"), el("span", { class: "small muted" }, "unread entries are always scored")]),
      el("label", null, "Full summaries newer than (days)"), el("div", { class: "row" }, [num("summary_lookback_days"), el("span", { class: "small muted" }, "older entries get a full summary on demand (Full summary button, chat, briefs)")]),
      el("label", null, "YouTube enrichment newer than (days)"), el("div", { class: "row" }, [num("enrich_lookback_days"), el("span", { class: "small muted" }, "Shorts detection + transcripts")]),
      el("label", null, "Mark Shorts as read"), chk("mark_shorts_read"),
      el("label", null, "Write FreshRSS labels"), el("div", { class: "row" }, [chk("write_labels"), el("span", { class: "small muted" }, "AI: High / Medium / Low labels in the FreshRSS sidebar")]),
      el("label", null, "High / medium label from score"), el("div", { class: "row" }, [num("label_high_min", 60), num("label_medium_min", 60)]),
      el("label", null, "Write topic tags"), el("div", { class: "row" }, [chk("write_topic_tags"), el("span", { class: "small muted" }, "#ai/topic tags on entries")]),
      el("label", null, "Topic vocabulary"), fields.topic_tags,
    ])]));

    // Interest profile
    const profile = el("textarea", { id: "interest_profile" }, s.interest_profile);
    page.append(el("div", { class: "card" }, [el("h2", { style: "margin-top:0" }, "Interest profile"), el("div", { class: "small muted" }, "Markdown. Used for scoring, summaries, briefs and chat. The +/− buttons in FreshRSS and the chat can also edit it."), profile]));

    const saveBtn = el("button", { class: "primary" }, "Save settings");
    const saved = el("span", { class: "small muted" });
    saveBtn.addEventListener("click", async () => {
      const body = { interest_profile: profile.value, topic_tags: fields.topic_tags.value };
      for (const [k, i] of Object.entries(fields)) { if (k === "topic_tags") continue; body[k] = i.type === "checkbox" ? i.checked : i.value; }
      await api("/api/settings", { method: "PUT", body }); saved.textContent = "Saved."; setTimeout(() => (saved.textContent = ""), 2000); refreshStatus();
    });
    page.append(el("div", { class: "row", style: "margin-bottom:14px" }, [saveBtn, saved]));

    // Feed rules
    const tbl = el("table"), tb = el("tbody");
    tbl.append(el("thead", null, el("tr", null, [el("th", null, "Feed"), el("th", null, "Score"), el("th", null, "Summarize"), el("th", null, "Fetch full"), el("th", { class: "num" }, "Unread"), el("th", { class: "num" }, "Scored / total"), el("th", { class: "num" }, "Latest")])), tb);
    const ruleInputs = [];
    const mkChk = (scope, id, key, v) => { const i = el("input", { type: "checkbox", checked: v ? "" : null }); ruleInputs.push({ scope, id, key, i }); return i; };
    for (const c of feeds.categories) {
      tb.append(el("tr", { class: "cat" }, [el("td", null, c.name), el("td", null, mkChk("category", c.id, "score", c.rule.score)), el("td", null, mkChk("category", c.id, "summarize", c.rule.summarize)), el("td", null, mkChk("category", c.id, "fetch_full", c.rule.fetch_full)), el("td", { class: "num" }, c.feeds.reduce((a, f) => a + f.n_unread, 0)), el("td"), el("td")]));
      for (const f of c.feeds) tb.append(el("tr", null, [el("td", { style: "padding-left:22px" }, [f.name, f.error ? el("span", { class: "error small" }, " (feed error)") : null]),
        el("td", null, mkChk("feed", f.id, "score", f.rule.score)), el("td", null, mkChk("feed", f.id, "summarize", f.rule.summarize)), el("td", null, mkChk("feed", f.id, "fetch_full", f.rule.fetch_full)),
        el("td", { class: "num" }, f.n_unread), el("td", { class: "num" }, `${f.n_scored} / ${f.n_entries}`), el("td", { class: "num small muted" }, f.latest ? new Date(f.latest * 1000).toLocaleDateString() : "")]));
    }
    const saveRules = el("button", { class: "primary" }, "Save feed rules");
    const rulesMsg = el("span", { class: "small muted" });
    saveRules.addEventListener("click", async () => {
      const byKey = {};
      for (const r of ruleInputs) { const k = r.scope + ":" + r.id; byKey[k] = byKey[k] || { scope: r.scope, ref_id: r.id }; byKey[k][r.key] = r.i.checked; }
      const res = await api("/api/rules", { method: "PUT", body: Object.values(byKey) });
      rulesMsg.textContent = `Saved. ${res.pending} entries pending scoring.`; state.feeds = null; refreshStatus();
    });
    const rescore = el("button", { class: "danger" }, "Clear scores & rescore unread (90 days)");
    rescore.addEventListener("click", async () => { if (!confirm("Clear AI scores/summaries on unread entries from the last 90 days so they get rescored? This costs API credits.")) return; const r = await api("/api/rescore", { method: "POST", body: { since_days: 90, unread_only: true } }); rulesMsg.textContent = `Cleared ${r.cleared} entries; worker kicked.`; refreshStatus(); });
    page.append(el("div", { class: "card" }, [el("h2", { style: "margin-top:0" }, "Feed rules"), el("div", { class: "small muted", style: "margin-bottom:8px" }, "Category rules apply to every feed in the category. Score = relevance scoring + one-line gist + labels. Summarize = full summary for anything scoring medium or better (not just high). Fetch full = fetch the article page when the feed only has an excerpt."), tbl, el("div", { class: "row", style: "margin-top:10px" }, [saveRules, rulesMsg, el("span", { class: "grow" }), rescore])]));

    // Usage
    const ut = el("table"); ut.append(el("thead", null, el("tr", null, [el("th", null, "Purpose"), el("th", null, "Model"), el("th", { class: "num" }, "Calls"), el("th", { class: "num" }, "Input"), el("th", { class: "num" }, "Cache read"), el("th", { class: "num" }, "Output"), el("th", { class: "num" }, "Cost")])));
    const ub = el("tbody"); for (const r of usage.rows) ub.append(el("tr", null, [el("td", null, r.purpose), el("td", null, r.model), el("td", { class: "num" }, r.calls), el("td", { class: "num" }, r.input_tokens), el("td", { class: "num" }, r.cache_read_tokens), el("td", { class: "num" }, r.output_tokens), el("td", { class: "num" }, money(r.cost_usd))]));
    ut.append(ub);
    page.append(el("div", { class: "card" }, [el("h2", { style: "margin-top:0" }, `Usage, last 30 days: ${money(usage.total_usd)}`), ut]));
    main.append(page);
  }

  // ── Boot ───────────────────────────────────────────────────────────────
  async function boot() {
    try { state.models = await api("/api/models"); } catch (e) { return; }
    await refreshStatus(); await refreshChats(); route();
    setInterval(refreshStatus, 30000);
  }
  boot();
})();
