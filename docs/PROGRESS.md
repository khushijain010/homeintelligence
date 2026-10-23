# Guardian: Progress Report

_Last updated: 23 Oct 2026_

Guardian is an AI incident-response agent for the home, built for the Ring track of the Amazon Developer Hackathon. This file records what has been built so far, how it was built and verified, and what remains.

---

## 1. Status at a glance

| Stage | Scope | Status |
|---|---|---|
| **Part 1, Core MVP** | Spec `GUARDIAN_PART1_CORE.md`, build steps 1–8 | ✅ Complete; all done-criteria met |
| **Part 2, Module 1** | Bedrock grounded reports | ✅ Complete; all module checks pass (with a fake Bedrock client) |
| Part 2, Module 2 | Alexa+ MCP server + OAuth + add-on | ⏳ Not started (waiting for review of Module 1, as the spec asks) |
| Part 2, Modules 3–9 | Routine learning, more playbooks, ledger, SNS, dashboard upgrades, reliability, submission | ⏳ Not started |

| Metric | Value |
|---|---|
| Automated tests | **59 passing**, fully offline (`make test`, ~9 s) |
| Demo scenarios | **5 / 5** give the expected result end to end over real HTTP |
| Application code | ~3,400 lines (Python + JS/HTML), plus ~550 lines of tests |

---

## 2. Environment and tooling

| What | Detail |
|---|---|
| OS | Windows 11; the Bash (Git Bash) and PowerShell tools were both used |
| Package manager | `uv` 0.13.5, installed with `pip install --user uv` because it wasn't present |
| Python | 3.12, downloaded and pinned by `uv` (the system Python is 3.14) |
| Project layout | Flat `guardian/guardian/...` package, as the spec lays out. `uv init` first generated a `src/` layout, which was removed. `pyproject.toml` sets `[tool.uv] package = false` |
| `make` | **Not installed** on this machine. The `Makefile` exists for the spec, and every target calls a script runnable without make (`scripts/dev.py`, `scripts/demo.py`) |
| Git | `uv init` created a git repository. **Nothing has been committed.** |
| Browser checks | Headless Microsoft Edge screenshots (desktop 1280 px and phone 360 px) |

### Libraries

| Library | Version | Used for |
|---|---|---|
| fastapi / starlette | ≥0.161 / 1.9.0 | API, webhook receiver, static dashboard |
| uvicorn | 0.54.0 | ASGI server |
| sqlmodel / SQLAlchemy | 0.0.48 / 2.1.6 | ORM over SQLite (`guardian.db`) |
| pydantic-settings | 2.16.1 | Configuration from env / `.env` |
| httpx | ≥0.28.1 | Async Ring Partner API client; test transports |
| sse-starlette | 3.5.0 | Server-Sent Events for the live dashboard |
| python-ulid | 4.0.1 | String ULID primary keys |
| tzdata | 2026.5 | Time-zone data (Windows has none for `zoneinfo`) |
| ring-sandbox[server] | 0.5.0 | Ring Partner API emulator, scenarios, pytest plugin |
| boto3 | 1.43.118 | Amazon Bedrock Converse API (Module 1) |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 | Tests (`asyncio_mode = auto`) |
| Tailwind CSS (CDN) | 3.4.17 | Dashboard styling, no build step |

---

## 3. Part 1: Core MVP

### Step 1: Verifying the Ring API (no guessed endpoints)

The spec forbids inventing Ring endpoints, event names or fields. All 3,427 lines of the installed `ring_sandbox` package source were read, and every VERIFY item is answered with file and line references in **`docs/ring-api-notes.md`**. The findings that shaped the code:

- **Webhook signature:** `X-Signature: sha256=<hex>`, an HMAC-SHA256 over the raw body bytes.
- **Dedupe key:** `meta.request_id`.
- **Event payload fields:** device id is `data.attributes.source`; time is `data.attributes.timestamp` in epoch ms.
- **Sensor events:**
  - `flood_detected` / `flood_cleared` → `water.detected` / `water.cleared`
  - `contact_sensor_faulted` / `contact_sensor_cleared` → `contact.opened` / `contact.closed`
- **Status fields:**
  - flood: `flood_detection.faulted`
  - contact: `contact_detection.faulted`
  - battery: `battery_status.percentage`, where **255 means "no reading"**
- **No device "kind" field** exists in the Partner API. Guardian infers the kind from status and capabilities, the same way ring-sandbox does.
- **Snapshots:** `POST /v1/devices/{id}/media/image/download` with `latest_in_range` returns a **303 redirect** to the image. Timestamps must not be in the future.
- **Control plane:** `/_sandbox/devices` accepts a **fixed device id**; `/_sandbox/events` injects an event; `/_sandbox/webhooks` registers a target; `/_sandbox/reset` restores a default world with random ids.
- **Pytest plugin limits:** its sync `ring_client` and `ring_control` fixtures call `asyncio.run`, so they can't be used in async tests. Guardian's tests use the plugin's `ring_app` fixture with an async ASGI transport instead.

### Step 2: Scaffold

| File | Purpose |
|---|---|
| `guardian/config.py` | `Settings` (pydantic-settings), with every `.env.example` key plus `corroboration_seconds`, `database_url`, `public_url` |
| `guardian/db.py` | Engine and session; `init_db("sqlite://")` gives tests a private in-memory DB (StaticPool); `clear_all()` |
| `guardian/models.py` | All tables from spec §5: Home, Device, Signal, Resident, Incident, Evidence, Action, CareSchedule, CareVisit (plus IncidentReport from Module 1). UTC timestamps, ULID ids |
| `guardian/bus.py` | In-process asyncio pub/sub; each subscriber gets its own queue with topic filtering |
| `guardian/clock.py` | `now()`, `freeze()` (tests), `set_now()` (demo; keeps ticking), `reset()`, `as_utc()` (SQLite drops tzinfo) |

### Step 3: Signal ingestion

| Feature | Implementation |
|---|---|
| **Ring client** (`ring/client.py`) | Async httpx client covering `devices?include=status,capabilities`, `status`, `snapshot_latest`. On 429 it honours `Retry-After`, otherwise backs off exponentially from 1 s. Snapshots use wall-clock time so the demo clock can't produce "future" timestamps. |
| **Webhooks** (`ring/webhooks.py`) | `POST /webhooks/ring`: reads the raw body, checks HMAC-SHA256 with `hmac.compare_digest` (401 on mismatch), dedupes on `meta.request_id` (duplicate → 200, no-op), normalizes, stores. Unknown or untracked events → 200, ignored. |
| **Normalizer** (`ring/normalize.py`) | Maps event types to the spec's signal names; motion with sub-type `human` → `motion.human`, else `motion.other`; infers device kind, sensor state and battery. |
| **Poller** (`ring/poller.py`) | Every 15 s, syncs devices and emits a signal only when a sensor's state changes. Creates Device rows for new Ring devices. Backs off on HTTP errors. **Race guard:** ignores poll data that isn't newer than the device's last report, so a stale poll can't undo a webhook. |
| **Registry** (`devices/registry.py`) | The single place a Signal is stored. It also updates device state and health, and publishes on the bus. Provides `reliability()`: usable only if online, reported within `SENSOR_STALE_MINUTES`, and battery ≥ 10 %. |
| **Virtual valve** (`devices/virtual.py`) | `open → closing → closed` (after `VALVE_CLOSE_SECONDS`) or `fault`. Has a demo flag `fault_on_close`, and a readback that also counts as a health report. Its position survives a restart. Labeled Simulated. |
| **Actuators** (`devices/actuators.py`) | `execute(decision, …)` **raises unless the policy decision is `allow`**. Handlers: `valve.close`, `snapshot.capture`, `notify.owner`, `notify.family`. |

### Step 4: Home state and policy engine

**Home state** (`state/home_state.py`):
- Mode `away` → occupancy `empty` (0.8).
- Mode `home` or `night` → `occupied` (0.8).
- Human motion or contact activity in the last 30 min while away → `unknown`, with the reason attached.

**Policy engine** (`engine/policy.py`) is one pure function, `evaluate(PolicyContext) → Decision(outcome, reasons)`, with no I/O and no LLM. Its gates run in order and the first failure decides:

1. **Hard deny:** `lock.unlock`, `alarm.disarm`, `emergency.call`, and `valve.open` while a water incident is open. Owner approval can't override these.
2. **Tier lookup:** `notify.*` and `snapshot.capture` are auto; `valve.close` is auto with conditions; an unknown action is denied.
3. **Actuator reliability:** the target must be online, not faulted, and fresh. Failure → deny, even with approval.
4. **Evidence:** confidence ≥ 0.8 and ≥ 2 independent signal devices.
5. **Occupancy:** must be `empty`; `unknown` counts as occupied.
6. **Rate limit:** each device action runs once per incident. Notifications aren't limited.

Gates 4–6 can only downgrade to `needs_approval`, and owner approval satisfies them ("1 signal + owner approval"). The policy is **re-evaluated right before an approved action runs**, and every decision is saved as `policy_decision` evidence listing every gate's reason.

### Step 5: Incident engine

| Part | Behaviour |
|---|---|
| **Playbooks** (`engine/playbooks.py`) | Plain Python step lists with the step kinds `gather`, `notify`, `act`, `wait_approval`, `verify`, `escalate`, `report`. |
| **Correlator** (`engine/correlator.py`) | A single consumer task, so incidents are never double-opened. A trigger opens an incident unless one from the same playbook opened within the last 10 min; in that case it attaches as corroborating evidence. Related signals attach while an incident is open. |
| **Incidents** (`engine/incidents.py`) | Evidence, status and action bookkeeping. Confidence: 0.6 for the trigger, +0.25 for each extra corroborating device, −0.3 for each unusable source device, plus adjustments recorded in evidence (e.g. a doorbell press −0.2). Clamped to 0–1. |
| **Runner** (`engine/runner.py`) | One asyncio task per incident. Saves `current_step` after every step. `request_action()` is the only path from a playbook to an actuator. Unexpected errors → honest escalation. |
| **Verifier** (`engine/verifier.py`) | Reads back the valve until `closed` (or `fault`, or a timeout), then watches a quiet window of `VERIFY_QUIET_SECONDS` in which no new flood sensor may turn wet. Unconfirmed counts as failed. |
| **Report** (`engine/report.py`) | The spec's template; every line comes from Evidence and Action rows. It prints the approval state (owner approved, owner rejected, approval expired) and "not run" for actions that never ran, and warns when any action is unverified. |

**Playbook `water_leak`** (headline):
1. Gather home state, all flood sensors, the valve, device health, and a snapshot if a camera covers that room.
2. Wait up to 30 s for a second sensor.
3. Notify the owner.
4. Request `valve.close` through policy; if policy needs approval, show an approval card for up to 120 s.
5. Verify.
6. Outcome: **contained**, or **escalated** with "Could not confirm water is off".
7. Write the report.

### Step 6: Suspicious entry and care visits

**`suspicious_entry`:**
- **Trigger:** the exterior door opens, or the doorbell camera sees a person. Only in Away or Night mode, and not while a caregiver visit is in progress.
- **Gathering:** a snapshot (through policy), doorbell presses in the 5 min before entry (−0.2 confidence), and other contact sensors.
- **Owner's choice:** three buttons. **Known person** → false alarm; **I'll handle it** → resolved; **Escalate** → escalated. The choice is stored as `human_decision` evidence.
- **Device actions:** none, ever.

**Care visits** (`state/care_visits.py`), for Sunita, Mon–Sat 10:00–12:00, minimum 90 min:
- **Arrival:** the first front-door person or doorbell press between 30 min before and 60 min after the start.
- **Departure:** the last front-door person before 20+ quiet minutes.
- **Statuses:** `completed`, `late` (>15 min), `short`, `no_show`, and `unverified` (camera offline in the window).
- **Schedule check:** runs every minute using `clock.now()`.
- **No false alarms at boot:** windows that started before monitoring began are not judged.

**`caregiver_no_show`:** a `no_show` verdict opens an incident, notifies the family (owner plus family role), escalates, and writes the report.

### Step 7: Dashboard, live updates, demo

**Dashboard** (`static/index.html` + `static/app.js`): vanilla JS, Tailwind via CDN, no build step. Sections:
1. **Status bar:** all clear or the active incident, a Home / Away / Night switcher, occupancy and reasons.
2. **Devices:** health dot, room, state, battery, problems; virtual devices carry a **Simulated** badge; the valve has "Jam on next close" and "Reset valve" demo controls.
3. **Incidents:** expandable live timeline mixing signals, home state, health, policy decisions with reasons, actions and verification; snapshot thumbnails noting "No facial recognition is performed"; an approval card with Approve / Reject and a live countdown; decision buttons; the report.
4. **Care visits:** the last 7 days.
5. **Demo panel:** one button per scenario, plus Reset.
- **Live updates:** an `EventSource` on `/api/stream` with debounced refreshes, auto-reconnect, and toasts for notifications.

**API** (`api/routes.py`, `api/stream.py`): all spec endpoints, plus `GET /api/demo/scenarios`.
- `POST /devices/{id}/simulate` only injects **valve failures** (or resets the valve when no water incident is open). It can't close the valve, so nothing bypasses policy.

**Seed** (`demo/seed.py`):
- **Home:** "Sharma Residence", Asia/Kolkata.
- **Residents:** Priya (owner), Kamla (elder, 74), Sunita (caregiver), Mr. Verma (neighbor, `family` role).
- **Emulator devices:** the defaults are deleted, then four devices are re-added with **fixed ids**: Front Door doorbell, Front Door Sensor, Kitchen and Bathroom leak sensors. The names match ring-sandbox's scenario steps. The webhook target is registered with the HMAC key.
- **Virtual:** the Main Water Valve.

**Scenarios** (`demo/director.py`): each one resets the home first, then injects Ring events into the emulator, which signs and delivers the webhooks the way Ring would. `play()` replays any built-in or example ring-sandbox scenario with a pinned start time.

| Scenario | Expected and observed result |
|---|---|
| `leak_empty_home` | Valve auto-closes, readback confirms → **contained** (~75 s) |
| `leak_valve_fault` | Readback shows `fault` → **escalated** with a warning (~15 s) |
| `leak_single_sensor` | `needs_approval` card → approve → **contained**; ignore it for 120 s → **escalated** (expiry also checked live) |
| `night_entry` | Snapshot plus decision buttons → the chosen status |
| `caregiver_no_show` | Demo clock jumps to the window; ring-sandbox `no_show` replays → visit **no_show**, incident, family notified |

**App** (`main.py`): its lifespan starts the correlator, poller and care checker. Incidents left open by a previous process are escalated as "outcome unconfirmed" rather than left hanging.

### Step 8: Docs and packaging

- **`README.md`:** what Guardian is, safety rules, the real-vs-simulated table, quick start (with and without make), demo steps, architecture, tests.
- **Other files:** `LICENSE` (MIT), `.env.example`, `Makefile` (`dev`, `emulator`, `app`, `demo`, `scenario`, `test`), `.gitignore`.

---

## 4. Part 2, Module 1: Bedrock grounded reports

**Goal:** an LLM writes the incident report, but every sentence must cite evidence, and a validator rejects anything unsupported. The template remains the fallback.

| File | Role |
|---|---|
| `guardian/ai/bedrock.py` | boto3 `bedrock-runtime` **Converse API** wrapper: temperature 0, 600 max tokens, 8 s connect and read timeouts, one attempt. Returns no client when `BEDROCK_MODEL_ID` is empty or there are no AWS credentials, so reports fall back to the template silently. `set_client()` lets tests inject a fake. |
| `guardian/ai/prompts/report.md` | System prompt: use only timeline facts; every sentence cites ids; no speculation about causes, identities or damage; say plainly when an action wasn't verified; never claim emergency services were contacted; under 120 words; JSON only. |
| `guardian/ai/report_llm.py` | Builds a compact timeline JSON with short ids (`I1` incident, `E1…` evidence, `A1…` actions) and **no images**. Calls the model off the event loop. Parses the JSON, even inside code fences. Validates; **retries once with the errors fed back**; otherwise saves a `template` record with the errors and returns None. |
| `guardian/ai/grounding.py` | The validator; checks listed below. |
| `models.IncidentReport` | New table holding the validated report, citation-to-row map, model id and rejection errors. Being a new table, it is created automatically, so existing databases need no migration. |
| `engine/runner.py` | The report step saves the template first, then tries the LLM report; the dashboard refreshes if it succeeds. |
| `api/routes.py` | `GET /api/incidents/{id}` adds `llm_report`, set only when the report is validated. |
| `static/app.js` | Report sentences with **clickable citation chips** that scroll to and highlight the cited timeline row. A **"Validated against evidence"** badge, with the model id and the template available behind a fold; otherwise a **"Template report"** badge. |

**What the grounding validator checks:**
- The title, summary and sentences are present.
- Every sentence has at least one citation, and all cited ids exist.
- Every **number or time** in a sentence appears in the items it cites ("21:52" may match "21:52:30").
- Every **device or resident name** in a sentence appears in the items it cites.
- **Forbidden claims** are rejected unless the cited evidence says the same: identity ("intruder", "identified"…), cause ("burst", "pipe", "probably"…), damage or costs ("damage", "₹…"…), and emergency services contacted. Negated statements ("did not contact emergency services") are allowed.
- An **action that ran but wasn't verified** must be called out by a sentence that cites it.
- The summary and sentences together are ≤ 120 words.

**Safety:** the model only produces text. Nothing it returns can reach the policy engine or an actuator.

---

## 5. Tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_policy.py` | 20 | Every gate, hard denies (including with approval), unknown occupancy, stale/offline/faulted actuators, rate limit, approval semantics, gate order |
| `tests/test_webhooks.py` | 6 | Valid signature, tampered body, wrong key, missing signature → 401; duplicate ignored; sensor normalization |
| `tests/test_leak_flow.py` | 3 | `leak_empty_home` → contained; `leak_valve_fault` → escalated; `leak_single_sensor` → approved via API → contained |
| `tests/test_care_visits.py` | 8 | `home_aide_visit` → completed (90 min); `late_arrival` → late; `short_visit` → short; `partial_blackout` → unverified; `no_show` → incident and family notified; no verdict mid-window; no judging before monitoring |
| `tests/test_report_llm.py` | 22 | Timeline ids, no images; valid report accepted; uncited sentence, invented number, unknown id, wrong device, five forbidden claims rejected; negation allowed; unverified action must be named; word limit; retry with feedback; fallback after two failures; Bedrock exception fallback; no client → no errors; no AWS credentials → no client; full leak flow producing a validated report via the API |

**How the tests run:** the ring-sandbox emulator (pytest plugin `ring_app`) and Guardian run in-process over `httpx.ASGITransport`. There are no sockets, no internet and no cloud accounts. Scenarios inject events with `deliver=false` and post the payload, signed with ring-sandbox's own `sign` and `encode`, to Guardian. Timers are shortened and the clock is frozen.

---

## 6. Verification beyond unit tests

1. **Live end-to-end run:** both servers were started (`scripts/dev.py`) and all five scenarios were driven over real HTTP, with the emulator delivering signed webhooks over the network. All five matched the expected results, twice (after the initial build and again after fixes). The server log showed no errors.
2. **Probe runs** printed full evidence trails and reports, to confirm the policy reasons (e.g. "confidence 0.85 (need 0.8) from 2 independent device(s)") and the night-entry decision flow.
3. **Approval expiry** was observed live: no answer within 120 s → escalated, and the owner was alerted.
4. **Dashboard** was screenshot in headless Edge at 1280 px and inside a true 360 px iframe. The page, approval card and timeline render correctly.
5. **JS** passes `node --check`.

---

## 7. Bugs found and fixed during the build

| Problem | Fix |
|---|---|
| A stale status poll processed after a webhook could revert sensor state | The poller skips data not newer than the device's last report |
| Cancelling a runner during a demo reset crashed on the deleted incident (`NoneType.current_step`) | `incidents.update` returns None when the incident is gone |
| The caregiver no-show notified the owner twice | The escalate step can skip its notification (`notify: False`) |
| Report said "Owner expired valve.close" and logged an expiry as a human decision | Expiry is now a `policy_decision` ("No owner answer within 120 s…"); the report reads "approval expired" |
| Report said "verified pending" for actions that never ran | Such actions print "not run" |
| Mode switcher overflowed the header at 360 px | Header wraps; buttons narrower on mobile |
| No-show evidence counted as a "signal" in the report | Recorded as `verification` evidence |

---

## 8. Design decisions not dictated by the spec

- **Fresh home per scenario:** each demo scenario resets the home first; otherwise one scenario changes the next one's result (for example, night-entry motion makes occupancy `unknown`, so the next leak needs approval). Reset also restores the real clock after the caregiver demo moves it.
- **Fixed device ids:** emulator devices are rebuilt with fixed ids so runs are deterministic.
- **Care monitoring start:** the caregiver checker only judges windows after monitoring started, so starting the app in the evening doesn't raise a false no-show.
- **Notifications:** the "once per incident" rate limit applies to device actions only, so notifications aren't limited. Notifications are delivered to the dashboard (SNS is Module 6).
- **Unknown actions:** anything without a policy rule is denied.
- **Restarts:** incidents still open when Guardian restarts are escalated, not resumed (full resume is Module 8).
- **Neighbor's role:** Mr. Verma uses the `family` role, since the spec's role list has no "neighbor".
- **Where LLM reports live:** in a separate table; `Incident.report_text` always holds the template.

---

## 9. Known limitations and open items

- **No live Bedrock call yet:** this machine has no AWS credentials, so Module 1 is tested with a fake client only. To try it live:
  1. Set `BEDROCK_MODEL_ID` (a model your account can use with the Converse API) and `AWS_REGION`.
  2. Provide credentials through the normal AWS chain.
  3. Run a scenario and look for the "Validated against evidence" badge.
- **Validator is pattern-based:** it can wrongly reject a correct report (the template is then shown) and can't catch every paraphrased claim. It's deliberately strict.
- **Leak demo timing:** 75–95 s, because of the spec's 30 s corroboration wait and 60 s quiet window. Shorten them in `.env` if needed. Clock speed controls are Module 8.
- **Night-entry follow-up:** if the owner closes a night-entry incident within 2 s, before the follow-up motion arrives, that motion opens a second incident.
- **Sensor staleness:** the emulator doesn't send heartbeats, so after more than 60 min without events, sensors count as stale until the next reset.
- **Git:** nothing has been committed.

---

## 10. How to run

```bash
uv sync
uv run python scripts/dev.py      # make dev: emulator :8787 + Guardian :8000
uv run python scripts/demo.py     # make demo: seed the home
uv run python scripts/demo.py leak_empty_home   # make scenario name=...
uv run pytest -q                  # make test
```

Dashboard: http://127.0.0.1:8000. Emulator docs: http://127.0.0.1:8787/_sandbox/docs

---

## 11. File map

```
guardian/
  README.md  LICENSE  Makefile  .env.example  pyproject.toml  uv.lock
  docs/ring-api-notes.md       VERIFY answers with source line refs
  docs/PROGRESS.md             this file
  scripts/dev.py, demo.py      make-free runners
  guardian/
    main.py config.py db.py models.py bus.py clock.py
    ring/      client.py webhooks.py poller.py normalize.py
    devices/   registry.py virtual.py actuators.py
    state/     home_state.py care_visits.py
    engine/    correlator.py incidents.py playbooks.py runner.py policy.py verifier.py report.py
    ai/        bedrock.py report_llm.py grounding.py prompts/report.md      (Module 1)
    api/       routes.py stream.py
    demo/      seed.py director.py
    static/    index.html app.js
  tests/       conftest.py test_policy.py test_webhooks.py test_leak_flow.py
               test_care_visits.py test_report_llm.py
```

---

## 12. Next steps

1. **Review Module 1.** Optionally try it live with AWS credentials.
2. **Module 2: Alexa+ MCP server + OAuth.** This needs from you:
   - a public URL (`cloudflared` tunnel);
   - an owner email and password;
   - Alexa developer access (`alexa-ai` CLI);
   - final privacy and terms text.
3. **Modules 3–9,** in spec order, stopping after each one for review.
