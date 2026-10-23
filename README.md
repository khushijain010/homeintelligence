# Guardian

An AI incident-response agent for the home, built for the **Ring track** of the Amazon Developer Hackathon.

When something goes wrong (a water leak, a suspicious entry, a caregiver who didn't show up), Guardian:

1. combines evidence from several devices,
2. checks whether anyone is home,
3. follows a predefined playbook,
4. takes only actions the owner pre-approved, through a strict policy engine,
5. reads back each action to confirm it worked,
6. writes a short incident report where every line comes from stored evidence.

**Headline demo:** a leak sensor turns wet while the home is empty. Guardian checks occupancy and the second leak sensor, alerts the owner, closes the water valve (policy-approved), confirms the valve is closed and no new sensors turn wet, and shows the report.

### Safety rules (enforced in code)

- The policy engine (`guardian/engine/policy.py`, a pure function) decides every action. `devices/actuators.execute` refuses anything without an `allow` decision.
- A consequential action needs 2 independent signals, or 1 signal plus owner approval.
- Every action is followed by a readback. If Guardian can't confirm it, the incident escalates and the report says so.
- Guardian never unlocks doors, disarms alarms, or claims to have called emergency services (hard-coded denies).
- No facial recognition. Identity is never inferred from images; a caregiver "visit" only means someone came to the door.
- Everything simulated is labeled **Simulated** in the UI.

## Real vs simulated

| Capability | Source | Status |
|---|---|---|
| Doorbell press, human motion, device online/offline | Ring Partner API, run locally via the `ring-sandbox` emulator | Real API shape |
| Leak (flood) and contact sensors | Ring Early Access sensor events/status via `ring-sandbox` | Real API shape |
| Camera snapshot as evidence | Ring Partner API `latest_in_range` image download | Real API shape (emulator returns a placeholder image) |
| Motorized water shut-off valve | Virtual device | **Simulated** |

Every Ring field and event name Guardian relies on is documented, with source line references, in [docs/ring-api-notes.md](docs/ring-api-notes.md).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 automatically).

```bash
uv sync
make dev        # emulator on :8787 + Guardian on :8000
make demo       # (second terminal) seed Sharma Residence into Guardian and the emulator
```

Open http://127.0.0.1:8000.

No `make` (e.g. on Windows)? Use the equivalent commands:

| make | without make |
|---|---|
| `make dev` | `uv run python scripts/dev.py` |
| `make emulator` | `uv run ring-sandbox serve --port 8787` |
| `make demo` | `uv run python scripts/demo.py` |
| `make scenario name=leak_empty_home` | `uv run python scripts/demo.py leak_empty_home` |
| `make test` | `uv run pytest -q` |

Configuration is read from the environment or `.env` (see `.env.example`). The defaults point at the local emulator.

## Demo

The demo home is **Sharma Residence** (Asia/Kolkata), with these residents:

- Priya, the owner, who lives in another city
- Kamla, a resident elder (74)
- Sunita, the caregiver (Mon–Sat, 10:00–12:00, at least 90 min)
- Mr. Verma, a neighbor

Devices: Front Door doorbell, Front Door contact sensor, and Kitchen and Bathroom leak sensors, all in the emulator, plus the Main Water Valve (virtual).

Run each scenario from the **Demo** panel or with `make scenario name=...`. Each scenario first resets the home, then injects Ring events into the emulator. The emulator signs and delivers the webhooks to Guardian as Ring would.

| Scenario | What happens | Expected result |
|---|---|---|
| `leak_empty_home` | Mode Away; Kitchen sensor wet; 10 s later Bathroom sensor wet | Valve auto-closes, readback confirms it, 60 s quiet window passes → **contained**, report (~75 s) |
| `leak_valve_fault` | Same, but the valve jams while closing | Readback shows `fault` → **escalated**, owner told "could not confirm water is off" |
| `leak_single_sensor` | Mode Away; only Kitchen wet | After 30 s with no corroboration, policy → `needs_approval`; approval card with countdown. Approve → valve closes → **contained**. Ignore it for 120 s → **escalated** |
| `night_entry` | Mode Night; front door opens, then a person on the doorbell camera, no doorbell press | Snapshot + three buttons: Known person → false alarm, I'll handle it → resolved, Escalate → escalated |
| `caregiver_no_show` | Demo clock jumps to Sunita's window; ring-sandbox `no_show` replays (only a vehicle passes) | Visit **no_show** on the Care table, incident opened, family notified |

The incident timeline updates live over Server-Sent Events and shows signals, home state, device health, every policy decision with its reasons, actions and verification. **Reset** restores the home and the real clock.

## How it works

```
Ring (emulator) ──signed webhook──▶ /webhooks/ring ─┐  verify HMAC, dedupe on meta.request_id
              ◀──status poll (15 s)── poller ───────┤  emit only on state change
                                                    ▼
                                          Signal ──bus──▶ correlator ──▶ Incident + runner task
                                                                           │ gather / notify / act /
                                                                           │ wait_approval / verify /
                                                                           │ escalate / report
                                          policy.evaluate() ◀── every action
                                          actuators.execute() (allow only) ──▶ valve / snapshot / notify
                                          verifier readback ──▶ Evidence ──▶ report
```

- **Confidence:** the trigger counts 0.6; +0.25 for each corroborating device; −0.3 for each unusable source device; −0.2 if the doorbell was pressed before an entry.
- **Policy gates, in order** (the first failure decides): hard deny → tier → actuator reliability → evidence → occupancy → rate limit. Gates 4–6 can only downgrade to `needs_approval`. The policy is re-evaluated right before an approved action runs.
- **Care visits:** arrival is the first front-door person or doorbell press from 30 min before to 60 min after the start. Departure is the last front-door person before 20+ quiet minutes. Statuses are `completed`, `late`, `short`, `no_show`, and `unverified` (camera offline).

## AI-written reports (Amazon Bedrock, optional)

When `BEDROCK_MODEL_ID` is set and AWS credentials are available, Guardian asks Bedrock (Converse API, temperature 0, 600 tokens, 8 s timeout) to write each incident report from a compact timeline in which every evidence item and action has a short id (`I1`, `E1`, `A1`, …). No images are sent.

The reply is shown only if `guardian/ai/grounding.py` accepts it:
- every sentence cites at least one valid id;
- every number, time, device name and resident name in a sentence appears in the items it cites;
- no unsupported claims about causes, identities, damage, or emergency services being contacted;
- any action that wasn't verified is called out plainly;
- under 120 words.

On failure Guardian retries once, passing the errors back, and then falls back to the template report. The dashboard marks which one you are reading ("Validated against evidence" or "Template report"), and each citation chip highlights its evidence row. The model only writes text; nothing it returns can reach the policy engine or an actuator.

## Tests

```bash
make test
```

The tests run fully offline. The ring-sandbox pytest plugin's `ring_app` emulator and Guardian talk over in-process ASGI transports.

- `test_policy.py`: table-driven cases for every gate.
- `test_webhooks.py`: signature, tampering, duplicates.
- `test_leak_flow.py`: end-to-end leak flows.
- `test_care_visits.py`: `home_aide_visit`, `late_arrival`, `short_visit`, `partial_blackout` and `no_show`.

## License

MIT
