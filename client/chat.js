/* Chat page. The server is stateless, so this file owns the transcript. */

const el = {
  agent: document.getElementById("agent"),
  chat: document.getElementById("chat"),
  form: document.getElementById("composer"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  sample: document.getElementById("sample"),
  reset: document.getElementById("reset"),
  banner: document.getElementById("banner"),
};

const state = {
  configs: [],
  configId: null,
  messages: [], // [{role, content}] — exactly what POST /api/chat wants
  busy: false,
};

/* --- rendering -------------------------------------------------------- */

const escapeHtml = (text) =>
  text.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

/* Minimal markdown: tables, bold, line breaks — all the agent ever sends. */
function renderMarkdown(text) {
  const lines = text.split("\n");
  const out = [];
  let row = 0;

  while (row < lines.length) {
    if (lines[row].trim().startsWith("|")) {
      const block = [];
      while (row < lines.length && lines[row].trim().startsWith("|")) {
        block.push(lines[row]);
        row += 1;
      }
      out.push(renderTable(block));
      continue;
    }
    const paragraph = [];
    while (row < lines.length && !lines[row].trim().startsWith("|")) {
      paragraph.push(lines[row]);
      row += 1;
    }
    const body = paragraph.join("\n").trim();
    if (body) out.push(`<p>${inline(body).replace(/\n/g, "<br>")}</p>`);
  }
  return out.join("");
}

function inline(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function renderTable(block) {
  const rows = block
    .map((line) => line.trim().replace(/^\|/, "").replace(/\|$/, ""))
    .map((line) => line.split("|").map((cell) => cell.trim()))
    .filter((cells) => !cells.every((cell) => /^:?-{2,}:?$/.test(cell) || cell === ""));
  if (rows.length < 2) return `<pre>${escapeHtml(block.join("\n"))}</pre>`;

  const [head, ...body] = rows;
  const th = head.map((cell) => `<th>${inline(cell)}</th>`).join("");
  const tr = body
    .map((cells) => `<tr>${cells.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
    .join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
}

function addMessage(role, text, kind) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${kind || role}`;
  const who = role === "user" ? "You" : kind === "system" ? "" : agentName();
  wrap.innerHTML =
    (who ? `<div class="who">${escapeHtml(who)}</div>` : "") +
    `<div class="bubble">${renderMarkdown(text)}</div>`;
  el.chat.appendChild(wrap);
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  return wrap;
}

function agentName() {
  const config = state.configs.find((c) => c.id === state.configId);
  return config ? config.name : "Agent";
}

/* --- data ------------------------------------------------------------- */

async function loadConfigs(preserveSelection = true) {
  const previous = state.configId;
  const response = await fetch("/api/configs");
  state.configs = await response.json();

  el.agent.innerHTML = state.configs
    .map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`)
    .join("");

  if (!state.configs.length) {
    state.configId = null;
    el.banner.hidden = false;
    el.banner.textContent = "No hotel configs found. Add one on the config page.";
    return;
  }
  const stillThere = preserveSelection && state.configs.some((c) => c.id === previous);
  state.configId = stillThere ? previous : state.configs[0].id;
  el.agent.value = state.configId;
}

async function checkSetup() {
  const health = await fetch("/health").then((r) => r.json());
  if (!health.api_key_configured) {
    el.banner.hidden = false;
    el.banner.innerHTML =
      "<strong>No LLM API key is set.</strong> Add <code>ANTHROPIC_API_KEY</code> or " +
      "<code>OPENAI_API_KEY</code> to <code>.env</code> in the project root and " +
      "restart. The config page works without one.";
  }
}

/* --- conversation ----------------------------------------------------- */

function resetConversation(note) {
  state.messages = [];
  el.chat.innerHTML = "";
  if (note) addMessage("assistant", note, "system");
}

async function send(text) {
  if (state.busy || !text.trim() || !state.configId) return;
  state.busy = true;
  el.send.disabled = true;

  state.messages.push({ role: "user", content: text });
  addMessage("user", text);
  el.input.value = "";
  el.input.style.height = "auto";

  const pending = addMessage("assistant", "_thinking…_", "assistant");
  pending.querySelector(".bubble").innerHTML = '<span class="thinking">thinking…</span>';

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config_id: state.configId, messages: state.messages }),
    });
    const data = await response.json();

    if (!response.ok) {
      pending.className = "msg error";
      pending.querySelector(".bubble").textContent =
        data.message || "The server rejected that request.";
      // Drop the unanswered turn so the next send isn't two user messages deep.
      state.messages.pop();
      return;
    }

    pending.querySelector(".bubble").innerHTML = renderMarkdown(data.reply);
    if (data.error) pending.className = "msg error";
    else state.messages.push({ role: "assistant", content: data.reply });
  } catch (error) {
    pending.className = "msg error";
    pending.querySelector(".bubble").textContent =
      "Could not reach the server. Is it still running?";
    state.messages.pop();
  } finally {
    state.busy = false;
    el.send.disabled = false;
    el.input.focus();
  }
}

/* --- wiring ----------------------------------------------------------- */

el.form.addEventListener("submit", (event) => {
  event.preventDefault();
  send(el.input.value);
});

el.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send(el.input.value);
  }
});

el.input.addEventListener("input", () => {
  el.input.style.height = "auto";
  el.input.style.height = `${Math.min(el.input.scrollHeight, 160)}px`;
});

el.agent.addEventListener("change", () => {
  state.configId = el.agent.value;
  // Replaying one hotel's numbers at another hotel's agent produces wrong quotes.
  resetConversation(`Now talking to **${agentName()}**. Previous conversation cleared.`);
  el.input.focus();
});

// A config added in the other tab should appear without a reload.
el.agent.addEventListener("focus", () => loadConfigs(true));

el.sample.addEventListener("click", () => {
  const config = state.configs.find((c) => c.id === state.configId);
  el.input.value = config?.sample_opener || "";
  el.input.focus();
  el.input.dispatchEvent(new Event("input"));
});

el.reset.addEventListener("click", () => resetConversation("New conversation."));

(async function start() {
  await loadConfigs(false);
  await checkSetup();
  if (state.configId) {
    addMessage(
      "assistant",
      `You're talking to **${agentName()}**. Describe your event — dates, headcount, ` +
        "sessions and meals — and you'll get a line-item proposal.",
      "system"
    );
  }
  el.input.focus();
})();
