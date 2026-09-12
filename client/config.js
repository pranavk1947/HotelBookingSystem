/* Config page. The JSON textarea is the source of truth; the inventory table is
   a projection of it, and greys out while the JSON does not parse. */

const el = {
  list: document.getElementById("list"),
  title: document.getElementById("title"),
  subtitle: document.getElementById("subtitle"),
  json: document.getElementById("json"),
  status: document.getElementById("status"),
  errors: document.getElementById("errors"),
  inventory: document.querySelector("#inventory tbody"),
  inventoryCard: document.getElementById("inventory-card"),
  summary: document.getElementById("summary"),
  chatLink: document.getElementById("chat-link"),
};

const state = {
  configs: [],
  selected: null,
  mode: "edit", // or "new" — decides PUT vs POST
};

const escapeHtml = (text) =>
  String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const money = (value, currency) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: currency || "USD" }).format(value);

function setStatus(text, kind) {
  el.status.textContent = text;
  el.status.className = `status ${kind || ""}`;
}

function showErrors(errors) {
  if (!errors || !errors.length) {
    el.errors.hidden = true;
    el.errors.innerHTML = "";
    return;
  }
  el.errors.hidden = false;
  el.errors.innerHTML = errors
    .map((e) => `<li><code>${escapeHtml(e.field)}</code> — ${escapeHtml(e.message)}</li>`)
    .join("");
}

/* --- projections ------------------------------------------------------ */

function parsed() {
  try {
    return JSON.parse(el.json.value);
  } catch (error) {
    return null;
  }
}

function writeJson(config) {
  el.json.value = JSON.stringify(config, null, 2);
}

function project() {
  const config = parsed();
  if (!config) {
    el.inventoryCard.classList.add("paused");
    setStatus("JSON is not parseable — the table is paused until it is.", "err");
    return;
  }
  el.inventoryCard.classList.remove("paused");
  renderInventory(config);
  renderSummary(config);
  el.title.textContent = config.name || "(unnamed)";
  el.subtitle.textContent = `id: ${config.id || "—"} · ${config.currency || "USD"}`;
}

function renderInventory(config) {
  const items = Array.isArray(config.inventory) ? config.inventory : [];
  el.inventory.innerHTML = items
    .map(
      (item, index) => `
      <tr data-index="${index}">
        <td><input data-field="name" value="${escapeHtml(item.name ?? "")}" />
            <div class="muted" style="font-size:11px">${escapeHtml(item.id ?? "")}</div></td>
        <td class="tight">${escapeHtml(item.category ?? "")}</td>
        <td class="tight">${escapeHtml(item.unit ?? "")}</td>
        <td class="tight"><input class="num" data-field="unit_price" value="${escapeHtml(item.unit_price ?? 0)}" /></td>
        <td class="tight"><input class="num" data-field="available_qty" value="${escapeHtml(item.available_qty ?? 0)}" /></td>
        <td class="tight"><input class="num" data-field="capacity" value="${item.capacity ?? ""}" /></td>
        <td><button type="button" data-remove="${index}" title="Remove item">×</button></td>
      </tr>`
    )
    .join("");
}

function renderSummary(config) {
  const currency = config.currency || "USD";
  const policies = config.policies || {};
  const blocks = [];

  const rules = (config.rules || []).map((r) => `<li>${escapeHtml(r.label || r.type)}</li>`);
  blocks.push(section("Pricing rules", rules.length ? `<ul>${rules.join("")}</ul>` : "<p class='muted'>None.</p>"));

  const fees = (config.fees || []).map((f) => {
    const how =
      f.basis === "per_room_night"
        ? `${money(f.value, currency)} per room-night`
        : f.basis === "pct_of_category"
        ? `${f.value}% of ${f.category}`
        : `${f.value}% of subtotal`;
    return `<li>${escapeHtml(f.name)} — ${escapeHtml(how)}</li>`;
  });
  blocks.push(section("Fees", fees.length ? `<ul>${fees.join("")}</ul>` : "<p class='muted'>None.</p>"));

  const pills = [];
  if (policies.tax_pct) pills.push(`Tax ${policies.tax_pct}%`);
  if (policies.deposit_pct) pills.push(`Deposit ${policies.deposit_pct}%`);
  if (policies.min_nights > 1) pills.push(`Min ${policies.min_nights} nights`);
  if (policies.min_lead_days) pills.push(`${policies.min_lead_days} days lead`);
  if (policies.max_attendees) pills.push(`Max ${policies.max_attendees} attendees`);
  if (config.fnb_minimum) pills.push(`F&B minimum ${money(config.fnb_minimum.amount, currency)}`);
  blocks.push(
    section(
      "Policies",
      pills.map((p) => `<span class="pill">${escapeHtml(p)}</span>`).join("") ||
        "<p class='muted'>Defaults.</p>"
    )
  );

  const upsells = (config.upsells || []).map(
    (u) => `<li><strong>${escapeHtml(u.item_id)}</strong> — ${escapeHtml(u.pitch)}</li>`
  );
  blocks.push(section("Upsells", upsells.length ? `<ul>${upsells.join("")}</ul>` : "<p class='muted'>None.</p>"));

  if (config.persona) blocks.push(section("Persona", `<p class="muted">${escapeHtml(config.persona)}</p>`));

  el.summary.innerHTML = blocks.join("");
}

const section = (heading, body) =>
  `<div style="margin-bottom:12px"><strong style="font-size:13px">${heading}</strong>${body}</div>`;

/* --- table -> JSON ---------------------------------------------------- */

el.inventory.addEventListener("input", (event) => {
  const input = event.target;
  if (!input.dataset.field) return;
  const config = parsed();
  if (!config) return;

  const index = Number(input.closest("tr").dataset.index);
  const field = input.dataset.field;
  const raw = input.value.trim();

  if (field === "name") {
    config.inventory[index].name = input.value;
  } else if (field === "capacity") {
    config.inventory[index].capacity = raw === "" ? null : Number(raw);
  } else {
    const value = Number(raw);
    if (Number.isNaN(value)) return; // let them finish typing
    config.inventory[index][field] = field === "unit_price" ? value : Math.round(value);
  }
  // Rewrite the JSON without re-rendering the row being typed in.
  el.json.value = JSON.stringify(config, null, 2);
  renderSummary(config);
  setStatus("Edited — not saved yet.", "");
});

el.inventory.addEventListener("click", (event) => {
  const index = event.target.dataset.remove;
  if (index === undefined) return;
  const config = parsed();
  if (!config) return;
  config.inventory.splice(Number(index), 1);
  writeJson(config);
  project();
  setStatus("Item removed — not saved yet.", "");
});

document.getElementById("add-item").addEventListener("click", () => {
  const config = parsed();
  if (!config) return;
  const n = (config.inventory || []).length + 1;
  config.inventory.push({
    id: `new-item-${n}`,
    name: "New item",
    category: "service",
    unit: "flat",
    unit_price: 100.0,
    available_qty: 10,
    capacity: null,
    description: "",
    includes_note: [],
    seasonal_rates: [],
  });
  writeJson(config);
  project();
  setStatus("Item added — edit it in the JSON below, then Save.", "");
});

el.json.addEventListener("input", () => {
  project();
  if (parsed()) setStatus("Edited — not saved yet.", "");
});

/* --- server ----------------------------------------------------------- */

async function loadList(selectId) {
  state.configs = await fetch("/api/configs").then((r) => r.json());
  el.list.innerHTML = state.configs
    .map(
      (c) => `<button class="config-item${c.id === selectId ? " active" : ""}" data-id="${escapeHtml(c.id)}">
        ${escapeHtml(c.name)}<small>${escapeHtml(c.id)}</small></button>`
    )
    .join("");
  if (selectId) el.chatLink.href = `/?agent=${encodeURIComponent(selectId)}`;
}

async function select(configId) {
  const config = await fetch(`/api/configs/${configId}`).then((r) => r.json());
  state.selected = configId;
  state.mode = "edit";
  writeJson(config);
  project();
  await loadList(configId);
  setStatus("Loaded.", "ok");
  showErrors([]);
}

el.list.addEventListener("click", (event) => {
  const button = event.target.closest(".config-item");
  if (button) select(button.dataset.id);
});

document.getElementById("validate").addEventListener("click", async () => {
  const config = parsed();
  if (!config) return setStatus("JSON is not parseable.", "err");
  const result = await fetch("/api/configs/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  }).then((r) => r.json());

  if (result.valid) {
    showErrors([]);
    const s = result.summary;
    setStatus(
      `Valid — ${s.inventory_count} items, ${s.rule_count} rules, ${s.fee_count} fees.`,
      "ok"
    );
  } else {
    showErrors(result.errors);
    setStatus(`${result.errors.length} problem(s) to fix.`, "err");
  }
});

document.getElementById("save").addEventListener("click", async () => {
  const config = parsed();
  if (!config) return setStatus("JSON is not parseable.", "err");

  const isNew = state.mode === "new";
  const response = await fetch(
    isNew ? "/api/configs" : `/api/configs/${state.selected}`,
    {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    }
  );
  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    showErrors(data.errors || []);
    setStatus(data.message || "Save failed.", "err");
    return;
  }
  showErrors([]);
  state.mode = "edit";
  state.selected = config.id;
  await loadList(config.id);
  setStatus("Saved. The next chat message uses this config — no restart needed.", "ok");
});

document.getElementById("clone").addEventListener("click", () => {
  const config = parsed();
  if (!config) return setStatus("JSON is not parseable.", "err");
  const name = prompt("Name for the new hotel config:", `${config.name} (copy)`);
  if (!name) return;

  config.name = name;
  config.id = slugify(name);
  writeJson(config);
  project();
  state.mode = "new";
  setStatus(`Cloned as "${config.id}". Edit prices as you like, then press Save.`, "ok");
});

document.getElementById("new").addEventListener("click", () => {
  const name = prompt("Name for the new hotel config:", "New Hotel");
  if (!name) return;
  writeJson(skeleton(name));
  project();
  state.mode = "new";
  setStatus("Blank config ready. Edit it, Validate, then Save.", "ok");
});

document.getElementById("delete").addEventListener("click", async () => {
  if (!state.selected || state.mode === "new") return;
  if (!confirm(`Delete config "${state.selected}"? This cannot be undone.`)) return;

  const response = await fetch(`/api/configs/${state.selected}`, { method: "DELETE" });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    return setStatus(data.message || "Delete failed.", "err");
  }
  await loadList();
  if (state.configs.length) await select(state.configs[0].id);
  setStatus("Deleted.", "ok");
});

const slugify = (name) =>
  name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48) || "new-hotel";

function skeleton(name) {
  return {
    id: slugify(name),
    name,
    currency: "USD",
    persona:
      "You are the group sales manager. Professional, concise and warm. You ask what the meeting needs to achieve before you talk about space.",
    sample_opener:
      "We need rooms and meeting space for 30 people over two nights next month, plus one group dinner.",
    policies: {
      tax_pct: 10.0,
      min_nights: 1,
      min_lead_days: 14,
      max_attendees: 200,
      deposit_pct: 25,
      cancellation_note: "Free cancellation up to 30 days before arrival.",
    },
    fees: [
      {
        id: "service-charge",
        name: "Service charge (food & beverage)",
        basis: "pct_of_category",
        category: "fnb",
        value: 20.0,
      },
    ],
    inventory: [
      {
        id: "std-room",
        name: "Standard guest rooms",
        category: "room",
        unit: "per_night",
        unit_price: 175.0,
        available_qty: 100,
        capacity: 2,
        description: "Standard king or twin.",
        includes_note: ["Wifi"],
        seasonal_rates: [],
      },
      {
        id: "main-hall",
        name: "Main function room",
        category: "meeting_space",
        unit: "per_day",
        unit_price: 1500.0,
        available_qty: 1,
        capacity: 200,
        description: "Divisible function space, theatre or banquet.",
        includes_note: ["Setup and teardown"],
        seasonal_rates: [],
      },
      {
        id: "group-dinner",
        name: "Group dinner (plated)",
        category: "fnb",
        unit: "per_person",
        unit_price: 70.0,
        available_qty: 200,
        capacity: null,
        description: "Three-course plated dinner.",
        includes_note: [],
        seasonal_rates: [],
      },
    ],
    rules: [
      {
        type: "volume_discount",
        label: "Volume discount (8% on rooms ≥ 25)",
        scope: { category: "room" },
        min_qty: 25,
        discount_pct: 8.0,
      },
    ],
    fnb_minimum: {
      amount: 2500.0,
      when_category_booked: "meeting_space",
      note: "Booking meeting space carries a $2,500 food & beverage minimum.",
    },
    upsells: [
      {
        item_id: "group-dinner",
        trigger: "meeting_space",
        pitch:
          "Most groups add a plated dinner on the final night at $70 a head — it is the easiest way to clear the food and beverage minimum.",
      },
    ],
  };
}

(async function start() {
  await loadList();
  if (state.configs.length) await select(state.configs[0].id);
  else setStatus("No configs yet — press '+ New config'.", "");
})();
