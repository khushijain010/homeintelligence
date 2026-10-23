# Ring API notes (VERIFY answers)

Read from the installed `ring-sandbox==0.5.0` source (`.venv/Lib/site-packages/ring_sandbox/`).
Every Guardian integration point below is grounded in a file + line of that package.

## Webhooks

| Question | Answer | Source |
|---|---|---|
| Signature header & format | `X-Signature: sha256=<hex>`; HMAC-SHA256 over the **raw body** | `webhooks.py:20, 24-27` |
| Constant-time verify | `hmac.compare_digest` on the full `sha256=...` string | `webhooks.py:30-44` |
| Dedupe field | `meta.request_id` (uuid4 string, one per delivery; chaos duplicates reuse it) | `webhooks.py:130`, `models.py:350-354`, `emulator.py:643-656` |
| Event type | `data.type` | `models.py:385-387` |
| Device id | `data.attributes.source` (with `source_type == "devices"`) | `webhooks.py:112-114`, `models.py:389-391` |
| Motion sub type | `data.attributes.sub_type` (`human`, `vehicle`, `animal`, `package`, `motion`, `other_motion`) | `models.py:341-347` |
| Event time | `data.attributes.timestamp` (epoch **ms**); falls back to `meta.time` | `webhooks.py:115`, `models.py:367-371, 401-403` |
| Body encoding the emulator signs | compact JSON, sorted keys | `webhooks.py:142-144`, `emulator.py:623` |

## Sensor (Early Access) event names

| Ring webhook type | Guardian signal | Source |
|---|---|---|
| `flood_detected` | `water.detected` | `models.py:327` |
| `flood_cleared` | `water.cleared` | `models.py:328` |
| `contact_sensor_faulted` | `contact.opened` (faulted = contact broken = open) | `models.py:323`, `world.py:440-444` |
| `contact_sensor_cleared` | `contact.closed` | `models.py:324`, `world.py:445-449` |

Other EA types (`tamper_*`, `freeze_*`, `temperature_*`, …, `models.py:325-338`) are ignored in Part 1.
`device_added` / `device_removed` are accepted (200) but not turned into signals.

## Device status fields (poller)

From `SandboxDevice.status_resource()` (`world.py:161-196`) and `StatusAttributes` (`models.py:199-215`):

- `online: bool`, `reported_at: ISO-8601` (sensors / battery devices only).
- `battery_status.percentage` — **255 is a "no reading" sentinel**; treat anything outside 0–100 as unknown (`models.py:212-215`).
- Flood sensor: `flood_detection.faulted: bool` (wet = `true`), plus `freeze_detection`, `tamper_detection`.
- Contact sensor: `contact_detection.faulted: bool` (open = `true`), `tamper_detection.detected`.
- There is **no device "kind" field** in the Partner API. The sandbox infers kind from `image_url` and
  capabilities (`world.py:552-563`); Guardian does the same: `doorbell` in `image_url` → doorbell, video
  capabilities → camera, `flood_detection` in status → flood sensor, `contact_detection` → contact sensor.
- Sandbox device kinds: `doorbell`, `camera`, `multi_camera`, `chime`, `contact_sensor`,
  `flood_freeze_sensor`, `temp_humidity_sensor`, `air_quality_monitor` (`world.py:22-30`).

## Endpoints used

- `GET /v1/devices?include=status,capabilities` — JSON:API with `included` (`emulator.py:355-377`).
- `GET /v1/devices/{id}/status` (`emulator.py:383-385`).
- Snapshot (`snapshot_latest`): `POST /v1/devices/{id}/media/image/download` with
  `{"type":"latest_in_range","start_timestamp":ms,"image_options":{"format":"jpeg"}}` → **303** redirect to the
  image blob (`emulator.py:436-478`, `client.py:462-510`). Timestamps must be ≤ real now (`emulator.py:461`), so
  Guardian uses wall-clock time (not the demo clock) for media requests.
- 429 → `Retry-After` header (`emulator.py:165-172`); the reference client backs off `2**attempt` from 1 s
  when the header is missing (`client.py:658-670`).

## Control plane (`/_sandbox`)

| Purpose | Request | Source |
|---|---|---|
| Add device | `POST /_sandbox/devices {"kind","name","id"?,"battery"?}` — `id` may be fixed | `emulator.py:123-128, 830-843` |
| Remove device | `DELETE /_sandbox/devices/{id}` | `emulator.py:845-857` |
| Inject event | `POST /_sandbox/events {"device_id","type","sub_type","at","duration_ms","deliver"}` → `{"webhook": payload}` | `emulator.py:109-115, 679-700` |
| Register webhook | `POST /_sandbox/webhooks {"url","signing_key"}` (appends) / `DELETE` clears | `emulator.py:724-732` |
| State | `GET /_sandbox/state` | `emulator.py:859-880` |
| Reset | `POST /_sandbox/reset` — restores `default_world()` with **random** ids; keeps webhooks | `emulator.py:897-905`, `world.py:636-643` |

`default_world()` contains Front Door (doorbell), Backyard (camera), Kitchen Chime, Front Door Sensor (contact).
Guardian's seed removes them and re-adds the demo devices with fixed ids so runs are deterministic.

## Scenarios & pytest plugin

- Built-in: `delivery`, `home_aide_visit`, `short_visit`, `camera_only_visit`, `no_show`, `device_flap`
  (`scenarios.py:43-101`). Wheel examples: `late_arrival`, `partial_blackout`, `visitor_not_worker`,
  `subscription_lapse` (`ring_sandbox/examples/*.yml`). `scenarios.resolve(name)` loads either (`scenarios.py:160`).
- Steps reference devices by **name** (`"Front Door Sensor"`) or default to the first camera (`scenarios.py:175-181`,
  `emulator.py:681`), so Guardian keeps those names.
- `scenarios.run(..., start=dt)` lets the caller pin the virtual start time (`scenarios.py:184-216`).
- Plugin fixtures: `ring_world`, `ring_app`, `ring_transport`, `ring_client`, `ring_control`
  (`pytest_plugin.py:18-43`). `ring_client`/`ring_control` are synchronous and call `asyncio.run` per request
  (`pytest_plugin.py:46-67`), so async tests use the `ring_app` fixture with `httpx.ASGITransport` instead.
- The emulator delivers webhooks with its own `httpx.AsyncClient` (real sockets, `emulator.py:622-677`), so tests
  inject with `deliver=false` and post the returned payload (signed) to Guardian themselves.
