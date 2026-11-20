// Pocket Superset phone app. Plain JS, no build step. All server data goes into the DOM
// through textContent, never innerHTML.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const STATE_TEXT = { finished: "finished", needs_input: "needs you", working: "working",
                       idle: "idle", stale: "stale", unknown: "no transcript" };
  const app = { cards: [], filter: "all", cur: null, es: null, seen: new Set(), prev: new Map(),
                screen: null, card: null, pollTimer: null };

  // ------------------------------------------------------------------ helpers
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function ago(ts) {
    if (!ts) return "";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }
  function clock(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function toast(msg) {
    const t = el("div", "toast", msg);
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }
  function live(on, text) {
    $("live").className = "live " + (on ? "on" : "off");
    $("live-text").textContent = text;
  }
  async function api(path, body) {
    const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify(body), credentials: "same-origin" }
                     : { credentials: "same-origin" };
    const r = await fetch(path, opt);
    if (r.status === 401) { location.href = "/pair"; throw new Error("not paired"); }
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty body */ }
    return { status: r.status, data };
  }
  // light formatting: `code` spans and **bold** markers dropped
  function richText(node, text) {
    const parts = text.replace(/\*\*/g, "").split("`");
    parts.forEach((p, i) => node.appendChild(i % 2 ? el("code", null, p) : document.createTextNode(p)));
  }

  // ------------------------------------------------------------------ list
  function bucket(c) { return c.state === "needs_input" || c.state === "finished" ? "you" : c.state; }

  function renderCards() {
    const box = $("cards");
    box.replaceChildren();
    const counts = { all: app.cards.length, you: 0, working: 0 };
    app.cards.forEach((c) => { const b = bucket(c); if (b in counts) counts[b]++; });
    $("n-all").textContent = counts.all;
    $("n-you").textContent = counts.you;
    $("n-working").textContent = counts.working;
    const shown = app.cards.filter((c) => app.filter === "all" || bucket(c) === app.filter);
    $("empty").hidden = shown.length > 0;
    if (!shown.length && app.cards.length) $("empty").textContent = "Nothing in this filter right now.";
    for (const c of shown) {
      const b = el("button", "card " + c.state);
      b.type = "button";
      const row = el("div", "row");
      row.append(el("span", "where", `${c.project} · ${c.workspace || ""}`), el("span", "pill " + c.state, STATE_TEXT[c.state] || c.state));
      b.append(row, el("h2", null, c.title));
      if (c.last_reply) b.append(el("p", "reply", c.last_reply.replace(/\*\*/g, "")));
      b.append(el("span", "foot", [c.label, ago(c.updated_at)].filter(Boolean).join(" · ")));
      b.addEventListener("click", () => { location.hash = `s/${c.ws}/${c.term}`; });
      box.appendChild(b);
    }
  }

  async function pollCards() {
    try {
      const { status, data } = await api("/api/cards");
      if (status !== 200) throw new Error(data.error || `HTTP ${status}`);
      app.cards = data.cards || [];
      $("list-error").hidden = !data.error;
      $("list-error").textContent = data.error ? `The laptop could not read Superset: ${data.error}` : "";
      if (!app.es) live(true, "live");
      notifyChanges();
      if (!app.cur) renderCards();
    } catch (e) {
      if (!app.es) live(false, "offline");
    }
  }

  // ------------------------------------------------------------------ alerts
  function notifyChanges() {
    const granted = "Notification" in window && Notification.permission === "granted";
    for (const c of app.cards) {
      const key = `${c.ws}/${c.term}`;
      const before = app.prev.get(key);
      if (granted && before && before !== c.state && (c.state === "finished" || c.state === "needs_input")
          && !(app.cur && app.cur.ws === c.ws && app.cur.term === c.term && document.visibilityState === "visible")) {
        const body = c.state === "needs_input" ? `${c.label}` : (c.last_reply || "Finished its turn").slice(0, 140);
        navigator.serviceWorker?.ready.then((reg) => reg.showNotification(
          `${c.workspace || c.project}: ${STATE_TEXT[c.state]}`,
          { body, tag: key, icon: "/icon-192.png", badge: "/icon-192.png", data: { hash: `#s/${key}` } }));
      }
      app.prev.set(key, c.state);
    }
  }
  function setupAlerts() {
    const btn = $("alerts");
    if (!("Notification" in window) || !("serviceWorker" in navigator)) return;
    btn.hidden = Notification.permission === "granted";
    btn.addEventListener("click", async () => {
      const p = await Notification.requestPermission();
      btn.hidden = p === "granted";
      toast(p === "granted" ? "Alerts are on while the app is open or in recent apps" : "Alerts stay off");
    });
  }

  // ------------------------------------------------------------------ chat
  function openChat(ws, term) {
    closeChat();
    app.cur = { ws, term };
    app.seen = new Set();
    app.screen = null;
    $("list-view").hidden = true;
    $("chat-view").hidden = false;
    $("back").hidden = false;
    $("turns").replaceChildren(el("p", "turn event", "Loading the conversation…"));
    $("ask").hidden = true;
    $("sheet").hidden = true;
    const c = app.cards.find((x) => x.ws === ws && x.term === term);
    if (c) setCard(c);
    const es = new EventSource(`/api/stream?ws=${encodeURIComponent(ws)}&term=${encodeURIComponent(term)}`);
    app.es = es;
    es.addEventListener("open", () => live(true, "live"));
    es.addEventListener("error", () => live(false, "reconnecting"));
    es.addEventListener("card", (e) => setCard(JSON.parse(e.data)));
    es.addEventListener("reset", (e) => { $("turns").replaceChildren(); app.seen = new Set(); addTurns(JSON.parse(e.data).turns, true); });
    es.addEventListener("turns", (e) => addTurns(JSON.parse(e.data).turns, false));
    es.addEventListener("screen", (e) => setScreen(JSON.parse(e.data)));
    es.addEventListener("gone", () => { toast("That Claude session closed"); location.hash = ""; });
  }
  function closeChat() {
    if (app.es) { app.es.close(); app.es = null; }
    app.cur = null;
  }
  function showList() {
    closeChat();
    $("chat-view").hidden = true;
    $("list-view").hidden = false;
    $("back").hidden = true;
    $("heading").textContent = "Pocket Superset";
    $("subheading").textContent = "Claude sessions on your laptop";
    renderCards();
  }

  function setCard(c) {
    app.card = c;
    $("heading").textContent = c.title;
    $("subheading").textContent = `${c.project} · ${c.workspace || ""} · ${STATE_TEXT[c.state] || c.state}${c.label ? " · " + c.label : ""}`;
    $("stop").hidden = c.state !== "working";
  }

  function addTurns(turns, initial) {
    const box = $("turns");
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    for (const t of turns) {
      if (app.seen.has(t.id)) continue;
      app.seen.add(t.id);
      const d = el("div", "turn " + t.role);
      if (t.role === "you" || t.role === "claude") {
        const who = t.role === "you" ? (t.from_phone ? "you · phone" : "you") : "claude";
        d.appendChild(el("small", null, `${who}${t.ts ? " · " + clock(t.ts) : ""}`));
      }
      if (t.role === "tool") d.textContent = "↳ " + t.text;
      else richText(d, t.text);
      box.appendChild(d);
    }
    if (initial && !turns.length) box.appendChild(el("p", "turn event", "No messages in this session yet."));
    if (initial || nearBottom) box.scrollTop = box.scrollHeight;
  }

  function setScreen(s) {
    app.screen = s;
    const pre = $("screen");
    pre.textContent = s.error ? `Could not read the screen: ${s.error}` : s.text;
    if (!pre.hidden) pre.scrollTop = pre.scrollHeight;
    $("mode").textContent = s.footer || "";
    const ask = $("ask");
    const btns = $("ask-btns");
    if (s.choices && s.choices.length) {
      ask.hidden = false;
      $("ask-q").textContent = s.question || "Claude is asking you to choose";
      btns.replaceChildren();
      for (const ch of s.choices) {
        const b = el("button");
        b.type = "button";
        b.append(el("b", null, s.menu ? (ch.selected ? "❯" : "·") : ch.key), document.createTextNode(ch.label));
        b.addEventListener("click", () => (s.menu ? choose(ch.index, ch.label) : sendKey(ch.key)));
        btns.appendChild(b);
      }
      const esc = el("button", null, "Dismiss (Esc)");
      esc.type = "button";
      esc.addEventListener("click", () => sendKey("esc"));
      btns.appendChild(esc);
    } else {
      ask.hidden = true;
    }
  }

  function sheet(msg, buttons) {
    $("sheet-msg").textContent = msg;
    const box = $("sheet-btns");
    box.replaceChildren();
    for (const [label, fn] of buttons) {
      const b = el("button", null, label);
      b.type = "button";
      b.addEventListener("click", () => { $("sheet").hidden = true; if (fn) fn(); });
      box.appendChild(b);
    }
    $("sheet").hidden = false;
  }

  async function sendPrompt(mode) {
    const text = $("prompt").value.trim();
    if (!text || !app.cur) return;
    $("send").disabled = true;
    try {
      const { status, data } = await api("/api/prompt", { ...app.cur, text, mode });
      if (status === 200) {
        $("prompt").value = "";
        autosize();
        toast("Sent to the laptop");
      } else if (status === 409 && data.reason === "input_not_empty") {
        sheet(`The laptop's input box already shows:\n“${data.input}”\nIt may be your draft or Claude's suggestion.`,
          [["Replace it and send", () => sendPrompt("clear")], ["Add mine after it", () => sendPrompt("anyway")], ["Cancel", null]]);
      } else if (status === 409 && data.reason === "busy") {
        sheet("Claude is still working. If you send now, Claude reads it after this turn.",
          [["Queue it", () => sendPrompt("anyway")], ["Cancel", null]]);
      } else if (status === 409 && data.reason === "no_input_box") {
        if (data.screen) setScreen(data.screen);
        sheet("Claude's input box is not on the laptop screen, so nothing was sent. Claude may have exited or be showing a menu. Open the live screen to see it.", [["OK", null]]);
      } else if (status === 409 && data.reason === "choice_open") {
        if (data.screen) setScreen(data.screen);
        toast("Answer Claude's question first");
      } else {
        toast(data.error || `Could not send (HTTP ${status})`);
      }
    } catch (e) {
      toast("The laptop is not reachable");
    } finally {
      $("send").disabled = false;
    }
  }

  async function sendKey(key) {
    if (!app.cur) return;
    try {
      const { status, data } = await api("/api/key", { ...app.cur, key });
      toast(status === 200 ? (key === "esc" ? "Sent Esc" : `Chose ${key}`) : (data.error || "Could not send"));
    } catch (_) {
      toast("The laptop is not reachable");
    }
  }

  async function choose(index, label) {
    if (!app.cur) return;
    try {
      const { status, data } = await api("/api/key", { ...app.cur, choose: index, label });
      if (status === 200) toast(`Chose “${label}”`);
      else { if (data.screen) setScreen(data.screen); toast(data.error || "Could not send"); }
    } catch (_) {
      toast("The laptop is not reachable");
    }
  }

  function autosize() {
    const t = $("prompt");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, window.innerHeight * 0.3) + "px";
  }

  // ------------------------------------------------------------------ routing + wiring
  function route() {
    const m = /^#s\/([0-9a-f-]{36})\/([0-9a-f-]{36})$/.exec(location.hash);
    if (m) {
      if (!app.cur || app.cur.ws !== m[1] || app.cur.term !== m[2]) openChat(m[1], m[2]);
    } else {
      showList();
    }
  }

  document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach((c) => { c.classList.toggle("on", c === chip); c.setAttribute("aria-selected", c === chip); });
    app.filter = chip.dataset.filter;
    renderCards();
  }));
  $("back").addEventListener("click", () => { location.hash = ""; });
  $("screen-toggle").addEventListener("click", () => {
    const pre = $("screen");
    pre.hidden = !pre.hidden;
    $("screen-toggle").setAttribute("aria-expanded", String(!pre.hidden));
    if (!pre.hidden) pre.scrollTop = pre.scrollHeight;
  });
  $("compose").addEventListener("submit", (e) => { e.preventDefault(); sendPrompt("safe"); });
  $("prompt").addEventListener("input", autosize);
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendPrompt("safe"); }
  });
  $("stop").addEventListener("click", () => sheet("Stop Claude's current turn? This presses Esc in the laptop terminal.",
    [["Stop it", () => sendKey("esc")], ["Cancel", null]]));
  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") pollCards(); });

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  setupAlerts();
  pollCards().then(route);
  app.pollTimer = setInterval(pollCards, 4000);
})();
