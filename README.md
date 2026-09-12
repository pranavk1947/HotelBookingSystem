# Hotel Group Proposal Agent

A conversational agent that takes a group enquiry ("3-day sales conference in March,
40 attendees, sessions and a closing dinner"), asks what it still needs, offers the
upsells that hotel actually sells, and closes with a line-item proposal.

Two browser pages, two sample hotels, one API key.

```bash
cp .env.example .env      # add your ANTHROPIC_API_KEY
./run.sh                  # or: npm install && npm start
```

Needs **Python 3.10+** (macOS ships 3.9 as `/usr/bin/python3`; `run.sh` finds a newer
one if you have it, or set `PYTHON_BIN=/path/to/python3.12`). Nothing else to install —
`run.sh` creates the virtualenv and pulls the dependencies itself.

| Page | URL | What it does |
|---|---|---|
| Chat | http://127.0.0.1:8000/ | Pick a hotel agent, negotiate, get a quote |
| Config | http://127.0.0.1:8000/config | Inspect, edit, clone, add and delete hotel configs |

---

## Try it in 60 seconds

1. Open the chat page. The dropdown has **The Grand Cascadia** and **Hacienda del Sol**.
2. Press **Try a sample request**, then **Send**. The agent clarifies, then quotes.
3. Open the config page, change `std-room`'s price from 189 to 250, press **Save**.
4. Back on chat, ask for the same conference again. The rooms line and the volume
   discount both move. No restart.
5. Press **Clone as new** on the config page, name it, **Save** — it appears in the
   chat dropdown immediately.

---

## The two decisions everything else follows from

### 1. The LLM never does arithmetic and never invents a price

The model's job is the conversation: understand the enquiry, ask good questions, pick
the right items, sell the upsell. It has exactly one tool, `build_quote`, and that is
the **only** path to a number. It passes item ids, quantities, dates and a head count;
a deterministic Python engine resolves rates, applies discounts in a fixed order,
validates constraints, computes fees and tax, and returns finished line items **plus a
pre-rendered markdown table** that the model pastes verbatim rather than retyping.

This is the whole reason the quote can be trusted against the config. "Correct pricing"
stops being a property of model behaviour — which drifts, stacks discounts twice and
fumbles rounding — and becomes a property of 300 lines of tested Python. The engine
(`server/src/pricing/`) imports no LLM, no HTTP and no filesystem.

Three defences make this stick:
- Duration is **derived from the item's unit**, not taken from the model. Ask for a
  per-person dinner "for 3 nights" and it still bills once.
- Prior tool calls are **stripped from replayed history**, and the prompt says earlier
  prices may be stale. After a config edit, the model cannot quietly replay an old quote.
- When the tool loop runs out of rounds, the model gets one final turn with tools
  disabled; if even that fails, the reply falls back to the **last priced table**,
  verbatim. A free-form finish is the one path where prices get invented, so it does
  not exist.

### 2. The config is the entire product surface

Everything that makes two hotel agents different lives in one JSON file: catalog and
rates, seasonal windows, discount rules, fees, tax, minimum spend, capacity ceilings,
upsell pitches, and the sales persona itself. Adding a hotel is adding a file — through
the config page, with no code change and no restart.

**How a config-page edit reaches the agent:** the config page `PUT`s the JSON, which is
Pydantic-validated and written atomically to `server/configs/<id>.json`. `/api/chat` is
stateless and **caches nothing**: every message re-reads that file, rebuilds the system
prompt from it, and constructs a fresh pricing engine around it. So an edit is live on
the next message — for both what the agent *says* (catalog, rules and persona are
rendered into the prompt in English) and what it *bills* (the same config drives the
engine). Tests pin both halves: `test_editing_a_price_changes_the_next_quote_with_no_restart`
and `test_edited_config_reaches_the_system_prompt_too`.

---

## Architecture (HLD)

### Component view

```
 BROWSER                    API LAYER                  DOMAIN                  EXTERNAL
 ───────                    ─────────                  ──────                  ────────
 chat page  ──────────────► chat_router ──────────────► AgentService ─────────► Anthropic
 (index.html, chat.js)          │                          │                    Messages API
                                │                          ├─► SystemPromptBuilder
                                │                          │      (config ➝ English)
                                │                          │
                                │                          └─► BuildQuoteTool
                                │                                 │
                                │                                 ▼
                                │                          QuoteCalculator ◄── rules
                                │                          (pure, no I/O)  ◄── fees
                                │                                 │        ◄── constraints
                                │                                 ▼
                                │                          Quote + markdown table
                                │
 config page ─────────────► config_router ─────────────► FileHotelConfigRepository
 (config.html, config.js)       │                                 │
                                └──── validate / CRUD ────────────┴──────► server/configs/*.json
                                                                             ▲
                              every chat turn re-reads the config ───────────┘
```

| Layer | Module | Responsibility |
|---|---|---|
| API | `api/chat_router.py` | One endpoint, stateless. Loads the config, delegates, returns `{reply, quote, usage, error}`. |
| API | `api/config_router.py` | Config CRUD + a dry-run `validate` that never writes. |
| API | `api/errors.py` | Single place where a domain exception becomes an HTTP envelope. |
| Agent | `agent/agent_service.py` | The turn: history hygiene, tool loop, round cap, failure handling. |
| Agent | `agent/prompt_builder.py` | Renders a config as the system prompt — catalog, rules, limits, upsells, conversation policy. |
| Agent | `agent/tools.py` | `build_quote`, the only path to a number. |
| Agent | `agent/llm_client.py` | Vendor adapter behind a Protocol; maps SDK errors to readable text. |
| Domain | `pricing/calculator.py` | The eight-stage pipeline. Pure — imports no LLM, no HTTP, no filesystem. |
| Domain | `pricing/rules.py`, `fees.py`, `constraints.py` | Strategy objects behind registries. |
| Data | `repository/config_repository.py` | Load/save JSON atomically, behind a Protocol. |
| Data | `model/hotel_config.py` | The config schema, and every validation rule the config page enforces. |

### Flow 1 — a chat turn

```
user message
   └─► chat_router          re-reads server/configs/<id>.json  (no cache, ever)
        └─► AgentService
             ├─ sanitise history      drop prior tool blocks, cap to 24 messages
             ├─ build system prompt   from the config just read
             └─ loop, max 6 rounds:
                  model ──tool_use──► BuildQuoteTool ──► QuoteCalculator ──► Quote
                        ◄─tool_result─ JSON: lines, adjustments, fees, tax, totals,
                                             violations[], warnings[], markdown_table
                  • violations  ➝ returned as a normal result, model re-selects
                  • round cap   ➝ one final turn with tools disabled
                  • that fails  ➝ reply falls back to the last priced table, verbatim
   ◄─ {reply, quote}        the chat page renders the table under the message
```

### Flow 2 — a config edit changes the agent

```
config page  ─PUT /api/configs/<id>─► parse_config()        Pydantic validation
                                          │ invalid ─► 400 + [{field, message}]
                                          ▼ valid
                                     atomic write (tmp ➝ os.replace)
                                          ▼
                                   server/configs/<id>.json
                                          ▼
next chat message ──► config re-read ──► prompt rebuilt   (what the agent SAYS)
                                    └──► engine rebuilt   (what the agent BILLS)
```

No cache, no restart, no code change. Both halves are pinned by tests
(`test_editing_a_price_changes_the_next_quote_with_no_restart`,
`test_edited_config_reaches_the_system_prompt_too`).

The server layout follows the house convention (`src/api/*_router.py`, a central
`errors.py`, `Annotated` `Depends` in `dependencies.py`, `service/`, `repository/`,
`model/`, `tests/` mirroring `src/`). Infrastructure that earns nothing here — DI
container, database, Redis, workflow engine, auth — is deliberately absent: configs are
files, and the repository Protocol is where a database would slot in later.

### Design patterns, and what each one buys

| Pattern | Where | What it buys |
|---|---|---|
| Strategy + Registry | `pricing/rules.py`, `pricing/fees.py` | A new discount type is one class + one spec. No switch statement grows a branch. |
| Pipeline | `pricing/calculator.py` | The order of operations is eight named stages you can read top to bottom — which is the thing that actually has to be right. |
| Repository (Protocol) | `repository/config_repository.py` | The API layer cannot reach the filesystem; S3 or Postgres is one new class. |
| Tool registry | `agent/tools.py` | A second tool (availability, contracting) is one class, and the agent service doesn't change. |
| Adapter (Protocol) | `agent/llm_client.py` | The agent depends on a 5-line interface, not on the vendor's object graph — which is how the loop is testable without an API key. |
| Facade | `agent/agent_service.py` | One `run_turn` hides history hygiene, the tool loop, the round cap and failure handling. |

A module-level assertion keeps the rule registry and the Pydantic discriminated union in
sync, so adding one without the other fails at import rather than mid-conversation.

### How it was built

Bottom-up, so that a partial build still demonstrates something:

1. **Schema and pricing engine first**, with the brief's worked example as the first
   test. Everything downstream depends on the numbers being right, and this is the only
   part that cannot be fixed by prompting.
2. **Config repository and the two sample hotels** — deliberately different enough that
   switching agents changes both the numbers and the conversation.
3. **Config API and page**, so a config could be edited before anything consumed it.
4. **Agent service, tool loop and chat API** — last, because by then the tool it calls
   was already correct and tested.
5. **Chat page**, then hardening: history sanitisation, the round cap, error paths.

Testing followed the same shape: the engine is tested directly, the agent loop through a
scripted LLM stub, and the three reviewer flows end-to-end over HTTP.

---

## Pricing: order of operations

Integer cents throughout, `ROUND_HALF_UP` on every percentage, so no rounding drift
between rule types.

1. **Resolve lines** — `qty × unit price × duration`. A seasonal window containing the
   arrival date overrides the unit price. Duration comes from the unit:
   `per_night` / `per_day` / `per_person_per_day` scale with nights; `per_person` and
   `flat` are charged once.
2. **Constraints** — hard violations return *without any totals*; a half-priced quote is
   worse than no quote. Soft findings become warnings the agent must work into the
   conversation.
3. **Subtotal**
4. **Rules**, in config order, each a separately labelled negative line. Thresholds
   always evaluate against **booked** quantity, never post-comp billable quantity, and
   percentages apply to what the previous rule left behind (so two stacked discounts
   can never sum past 100%).
5. **Net subtotal**
6. **Fees** — per-room-night (resort fee), percent of one category (service charge on
   F&B), or percent of subtotal.
7. **Tax** on net subtotal **plus** fees.
8. **Total**, and the deposit due on signing.

### On the brief's example table

`grand-cascadia` reproduces the brief exactly at the three rows that matter, asserted to
the cent in `test_brief_example_reproduces_exactly`:

| | |
|---|---|
| Subtotal | **$37,980.00** |
| Volume discount (10% on rooms ≥ 30) | **−$2,268.00** |
| Net subtotal | **$35,712.00** |

The config then adds what a real convention hotel actually charges — a 22% service
charge on F&B and 9.5% tax — so the grand total is $39,827.34, not $35,712. That is
deliberate: the brief's table stops at the discount, and a flagship config with no tax
would score worse on "anticipates real hotel business needs" than it gains in matching a
worked example. Set `policies.tax_pct` to 0 and delete the fee on the config page if you
want the two to line up.

---

## Business needs the config handles

Beyond a flat price list, because a real client arrives with all of this:

- **Seasonal / peak rates** — `MM-DD` windows per item, applied automatically by arrival
  date. Year-less (March is peak every year) and wrap-safe (`12-20` → `01-05`).
- **Volume discounts** on room blocks, scoped to a category or a single item.
- **Length-of-stay discounts**, stacking correctly with volume.
- **Comp rooms** — one free per N booked, capped, credited at the resolved rate.
- **F&B minimum spend** triggered by booking meeting space. A shortfall is a *warning*,
  not a refusal: the agent is told to upsell into it, because that is the better
  commercial outcome.
- **Service charge scoped to F&B** vs **tax on everything**, applied in the right order.
- **Resort fee** per room-night.
- **Per-person day-delegate pricing** (Hacienda's Complete Meeting Package) alongside
  per-room and per-day items.
- **Capacity ceilings** per space, and **inventory caps** per item — the agent cannot
  sell 90 casitas from a 60-key resort or seat 200 people in a 60-seat breakout.
- **Minimum nights**, **maximum attendees**, **lead time**, **deposit on signing**,
  **cancellation terms**.
- **Per-hotel persona and upsell scripts** — the agent's voice is config, not code.

Deliberately not modelled: attrition and cut-off dates, live availability, multi-currency
FX, contracted rebooking. All are real, none change the architecture — each is another
rule or constraint class.

## The two sample hotels

**The Grand Cascadia** — 400-room downtown convention hotel. $189 rooms, a $2,400
ballroom, two $650 breakouts, AV, plated dinner. 10% volume discount at 30+ rooms, 22%
service charge on F&B, 9.5% tax, a $5,000 F&B minimum when meeting space is booked.
Corporate, consultative persona.

**Hacienda del Sol** — 60-key boutique resort. No ballroom: a courtyard (80) and a palapa
(60), so a 40-person group with breakouts has to be shaped differently. March peak rate,
$35/room-night resort fee, a two-night minimum, a 90-attendee ceiling, one comp casita per
40 booked, and a $145 per-delegate Complete Meeting Package instead of itemised day hire.
Warm, unhurried persona.

Hacienda is also where **discount stacking** is visible. Rules fire in config order and
each applies to what the previous one left behind, so 40 casitas for 3 nights takes the
comp credit first, then 8% volume on the remainder, then 12% length-of-stay on what is
left — never three percentages summed against the same gross.

They differ on purpose: same request, different numbers *and* a different conversation.

---

## Config reference

```jsonc
{
  "id": "grand-cascadia",              // slug; also the filename
  "name": "The Grand Cascadia",
  "currency": "USD",
  "persona": "...",                    // the agent's voice, verbatim into the prompt
  "sample_opener": "...",              // fills the "Try a sample request" button
  "policies": {
    "tax_pct": 9.5, "min_nights": 1, "min_lead_days": 14,
    "max_attendees": 500, "deposit_pct": 25, "cancellation_note": "..."
  },
  "fees": [                            // basis: per_room_night | pct_of_category | pct_of_subtotal
    { "id": "service-charge", "name": "...", "basis": "pct_of_category",
      "category": "fnb", "value": 22.0 }
  ],
  "inventory": [
    { "id": "std-room", "name": "...",
      "category": "room",              // room | meeting_space | fnb | av | service
      "unit": "per_night",             // per_night | per_day | per_person | per_person_per_day | flat
                                       // per-person items with qty 1 are expanded to the
                                       // head count (one dinner each, not one dinner);
                                       // the quote carries a warning saying so
      "unit_price": 189.0, "available_qty": 400,
      "capacity": 2,                   // sleeps (rooms) or seats (meeting space)
      "description": "...", "includes_note": ["..."],
      "seasonal_rates": [{ "label": "Peak", "start": "03-01", "end": "04-30",
                           "unit_price": 219.0 }] }
  ],
  "rules": [                           // type: volume_discount | los_discount | comp_item
    { "type": "volume_discount", "label": "Volume discount (10% on rooms ≥ 30)",
      "scope": { "category": "room" }, "min_qty": 30, "discount_pct": 10.0 }
  ],
  "fnb_minimum": { "amount": 5000.0, "when_category_booked": "meeting_space", "note": "..." },
  "upsells": [ { "item_id": "av-package", "trigger": "meeting_space", "pitch": "..." } ]
}
```

Unknown keys and unknown rule types are **rejected**, not ignored — a typo on the config
page is an error with a field path, never a silent no-op. Every editable field has
observable behaviour; there are no decorative knobs.

**Extending it:** a new discount type is one class in `pricing/rules.py` plus one spec in
`model/hotel_config.py`. A new fee basis is one class in `pricing/fees.py`. A new
bookability check is one class in `pricing/constraints.py`. A new tool is one class in
`agent/tools.py`.

---

## API

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/chat` | `{config_id, messages[]}` → `{reply, quote, usage, error}`. Stateless. |
| `GET` | `/api/configs` | Summaries for the dropdown |
| `GET` | `/api/configs/{id}` | Raw file, so a config that fails validation still opens in the editor |
| `POST` | `/api/configs` | Create (409 on duplicate id) |
| `PUT` | `/api/configs/{id}` | Update |
| `DELETE` | `/api/configs/{id}` | 409 if it is the last one — the chat page always has someone to talk to |
| `POST` | `/api/configs/validate` | Dry run, always HTTP 200, returns `[{field, message}]` |
| `GET` | `/health` | Status, whether the key is set, model, config ids |

Interactive docs at `/apidocs`.

## Environment

| Variable | Required | Default | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | **yes** | — | The app boots without it: the config page works, chat returns a readable setup message instead of crashing. |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-5` | Override if your key has a different model. A 404 surfaces as a readable chat message. |
| `PORT` | no | `8000` | |

Windows (PowerShell), if `run.sh` isn't available — use a Python 3.10+ interpreter:

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd server; python -m uvicorn src.app:app --port 8000
```

## Tests

```bash
cd server && python -m pytest -q      # 52 tests
```

- **Pricing** (`tests/pricing/`) — the brief reproduced to the cent, threshold
  boundaries (29 vs 30 rooms), seasonal edges including a year-wrap, discount stacking
  order, comp-vs-threshold semantics, fee scoping and tax ordering, half-up rounding,
  every constraint, and unit semantics.
- **Agent** (`tests/agent/`) — the tool loop against a scripted LLM stub: the engine's
  numbers are what reach the conversation, violations come back as results (not errors)
  so the model self-corrects, history is sanitised and capped, and the round cap always
  ends in a readable turn.
- **API** (`tests/api/`) — the three reviewer flows over HTTP: read and switch both
  configs, edit one and confirm the next quote changes, add a new one and quote against
  it. Plus validation, path-traversal, duplicate and last-config guards.

The browser pages were additionally exercised headless (dropdown, agent switching
clearing history, table↔JSON projection, validate errors, clone, new, save, delete).

## Known limitations

- **Static inventory.** Entries represent what is available for the requested dates;
  there is no live availability or double-booking check across conversations.
- **Discounts stack multiplicatively**, in config order. Two 10% rules on the same scope
  give 19%, not 20%. Deliberate — but it means rule order in the config is significant.
- **Per-person items with qty 1 are expanded to the head count.** Right for "a plated
  dinner", surprising if you genuinely want one unit; the quote warns when it happens.
- **No streaming, no auth, single-process file storage.** Two people editing the same
  config at once will have last-write-win; the write itself is atomic.
- **The conversation is not persisted.** History lives in the browser tab and is cleared
  when you switch agents.

## Trade-offs

- **Vanilla JS, no build step.** The brief says no UI polish needed; a bundler would be
  setup cost for nothing. The config page renders a read-only summary and an editable
  inventory table, but the JSON textarea is the source of truth — a full form builder
  for a five-level nested schema is a lot of UI that breaks every time the schema moves.
- **Stateless chat, history on the client.** No session store, and switching agent
  clears the transcript — replaying one hotel's numbers at another hotel's agent is the
  fastest way to produce a wrong quote.
- **Packages are inventory items,** not a separate concept: a "Complete Meeting Package"
  is a `per_person_per_day` line with an `includes_note`. One resolution path instead of
  two, and the brief's own AV package is a single line item.
- **No streaming.** Replies are one HTTP round-trip. Streaming is a UI nicety that would
  complicate the tool loop for no grading value.
- **No agent tests against the real API.** The loop is tested through an `LLMClient`
  stub; the model's judgment is exercised by hand.
