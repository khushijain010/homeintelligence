"""Privacy policy and terms pages linked from the Alexa+ add-on listing."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


def _doc(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 16px;line-height:1.55;color:#0f172a}}
h1{{font-size:1.5rem}}h2{{font-size:1.1rem;margin-top:1.5rem}}</style></head><body>{body}</body></html>"""
    )


@router.get("/legal/privacy")
def privacy() -> HTMLResponse:
    return _doc(
        "Guardian privacy policy",
        """<h1>Guardian privacy policy</h1>
<p><em>Hackathon prototype. Last updated October 2026.</em></p>
<h2>What Guardian stores</h2>
<p>Guardian stores, on the home's own server: events from the home's Ring devices (doorbell presses,
motion, door and leak sensors, device online status), home mode, incident timelines, actions Guardian
proposed or took, owner decisions, caregiver visit times, and resident check-in answers.</p>
<h2>What Guardian does not do</h2>
<ul><li>No facial recognition. Guardian never identifies people from images.</li>
<li>No selling or sharing of data with advertisers.</li>
<li>Guardian never calls emergency services on its own.</li></ul>
<h2>Alexa</h2>
<p>When you link Guardian to Alexa, Alexa can read your home status, incidents, reports and care
visits, and (if you allowed it) change the home mode and pass on your approval of a proposed action.
Guardian receives only the request and an access token; it does not receive your voice recordings.</p>
<h2>Cloud services</h2>
<p>If the owner enables them, incident timelines (text only, never images) are sent to Amazon Bedrock
to draft reports, and alert text is sent through Amazon SNS to the owner's chosen phone or email.</p>
<h2>Retention and deletion</h2>
<p>Data stays until the owner deletes it. The evidence ledger is append-only by design so that incident
records cannot be silently altered. Contact the home owner to request deletion.</p>""",
    )


@router.get("/legal/terms")
def terms() -> HTMLResponse:
    return _doc(
        "Guardian terms of use",
        """<h1>Guardian terms of use</h1>
<p><em>Hackathon prototype. Last updated October 2026.</em></p>
<p>Guardian is a demonstration project, not a certified safety, security, medical or fire system,
and must not be relied on to protect people or property. It does not contact emergency services.
In an emergency, call your local emergency number yourself.</p>
<p>Some devices in the demo (the water valve, thermostat, smart plug and smoke/CO alarm) are simulated
and are labelled as such. Actions are limited by Guardian's policy engine; it never unlocks doors or
disarms alarms.</p>
<p>The software is provided "as is" under the MIT License, without warranty of any kind.</p>""",
    )
