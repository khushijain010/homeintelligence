import json
from datetime import UTC, datetime

import pytest

from guardian import clock, db
from guardian.ai import bedrock, grounding, report_llm
from guardian.demo import director, seed
from guardian.devices import registry
from guardian.engine import incidents
from guardian.models import Action, Device

from .conftest import wait_terminal

T0 = datetime(2026, 10, 23, 16, 22, 20, tzinfo=UTC)  # 21:52:20 Asia/Kolkata


class FakeModel:
    """Stands in for Bedrock. Replies are strings, or callables given the timeline."""

    model_id = "fake-model"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def converse(self, system: str, messages: list[dict]) -> str:
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            timeline = json.loads(messages[0]["content"][0]["text"].split("\n", 1)[1])
            return reply(timeline)
        return reply


@pytest.fixture
def leak_incident():
    db.init_db("sqlite://")
    seed.seed_db()
    clock.freeze(T0)
    with db.session() as s:
        for name, room in (("Kitchen Leak Sensor", "Kitchen"), ("Bathroom Leak Sensor", "Bathroom")):
            s.add(Device(source="ring", external_id=name, kind="flood_sensor", name=name, room=room))
        s.commit()
    kitchen = registry.by_external_id("Kitchen Leak Sensor")
    valve = registry.all_devices("valve")[0]
    inc = incidents.open_incident("water_leak", "Water leak — Kitchen")
    incidents.add_evidence(inc.id, "signal", "Kitchen Leak Sensor turned wet", {"device_id": kitchen.id, "type": "water.detected"})
    incidents.add_evidence(inc.id, "home_state", "Home is empty (away mode): Mode is Away", {"occupancy": "empty"})
    incidents.add_evidence(inc.id, "signal", "Bathroom Leak Sensor turned wet", {"type": "water.detected"})
    incidents.add_evidence(inc.id, "verification", "Valve readback: closed", {"passed": True})
    incidents.save_action(
        Action(
            incident_id=inc.id,
            action_type="valve.close",
            target_device_id=valve.id,
            policy_outcome="allow",
            result="ok",
            verified="yes",
        )
    )
    incidents.update(inc.id, status="contained", confidence=0.85)
    yield inc.id
    clock.reset()


def report(*sentences, summary="Guardian closed the water valve after two leak sensors turned wet."):
    return {
        "title": "Water leak in the Kitchen",
        "summary": summary,
        "sentences": [{"text": t, "cites": c} for t, c in sentences],
        "open_questions": [],
    }


GOOD = report(
    ("At 21:52:20 the Kitchen Leak Sensor turned wet while the home was empty.", ["E1", "E2"]),
    ("The Bathroom Leak Sensor also turned wet.", ["E3"]),
    ("Guardian closed the Main Water Valve and the readback confirmed it closed.", ["A1", "E4"]),
)


def errors_for(incident_id, rep):
    timeline, _ = report_llm.build_timeline(incident_id)
    return grounding.validate(rep, timeline, report_llm.known_names())


# --------------------------------------------------------------------------- validator


def test_timeline_has_short_ids(leak_incident):
    timeline, refs = report_llm.build_timeline(leak_incident)
    assert [i["id"] for i in timeline["items"]] == ["I1", "E1", "E2", "E3", "E4", "A1"]
    assert refs["A1"]["type"] == "action"
    assert "data_uri" not in json.dumps(timeline)


def test_valid_report_accepted(leak_incident):
    assert errors_for(leak_incident, GOOD) == []


def test_time_prefix_accepted(leak_incident):
    rep = report(("At 21:52 the Kitchen Leak Sensor turned wet.", ["E1"]), ("The valve was closed.", ["A1"]))
    assert errors_for(leak_incident, rep) == []


def test_uncited_sentence_rejected(leak_incident):
    rep = report(("The Kitchen Leak Sensor turned wet.", []))
    assert any("no citation" in e for e in errors_for(leak_incident, rep))


def test_invented_number_rejected(leak_incident):
    rep = report(("Three sensors reported water within 45 seconds.", ["E1"]))
    assert any("'45'" in e for e in errors_for(leak_incident, rep))


def test_unknown_id_rejected(leak_incident):
    rep = report(("The Kitchen Leak Sensor turned wet.", ["E99"]))
    assert any("unknown id" in e for e in errors_for(leak_incident, rep))


def test_device_not_in_cited_items_rejected(leak_incident):
    rep = report(("The Bathroom Leak Sensor turned wet.", ["E1"]))
    assert any("Bathroom Leak Sensor" in e for e in errors_for(leak_incident, rep))


@pytest.mark.parametrize(
    "text,kind",
    [
        ("A burst pipe caused the Kitchen Leak Sensor to turn wet.", "cause"),
        ("The leak was probably under the sink.", "cause"),
        ("Guardian notified the fire department.", "emergency"),
        ("An intruder was seen at the door.", "identity"),
        ("Water damage is estimated at ₹20000.", "damage"),
    ],
)
def test_forbidden_claims_rejected(leak_incident, text, kind):
    errs = errors_for(leak_incident, report((text, ["E1"])))
    assert any(kind in e for e in errs), errs


def test_negated_emergency_statement_allowed(leak_incident):
    rep = report(("Guardian did not contact emergency services.", ["I1"]), ("The valve was closed.", ["A1"]))
    assert errors_for(leak_incident, rep) == []


def test_unverified_action_must_be_called_out(leak_incident):
    with db.session() as s:
        a = s.get(Action, incidents.actions(leak_incident)[0].id)
        a.verified, a.result = "no", "failed"
        s.add(a)
        s.commit()
    silent = report(("Guardian sent a close command to the Main Water Valve.", ["A1"]))
    assert any("was not verified" in e for e in errors_for(leak_incident, silent))
    honest = report(("Guardian could not confirm the Main Water Valve closed.", ["A1"]))
    assert errors_for(leak_incident, honest) == []


def test_word_limit(leak_incident):
    long = report(*[("The Kitchen Leak Sensor turned wet.", ["E1"])] * 25)
    assert any("limit is 120" in e for e in errors_for(leak_incident, long))


# --------------------------------------------------------------------------- generate + fallback


async def test_generate_accepts_valid_report(leak_incident):
    fake = FakeModel(json.dumps(GOOD))
    bedrock.set_client(fake)
    rep = await report_llm.generate(leak_incident)
    assert rep is not None and rep.source == "bedrock" and rep.model_id == "fake-model"
    assert len(fake.calls) == 1
    assert report_llm.latest(leak_incident).sentences[0]["cites"] == ["E1", "E2"]


async def test_retry_once_with_errors_then_accept(leak_incident):
    bad = report(("A burst pipe flooded the kitchen.", ["E1"]))
    fake = FakeModel("```json\n" + json.dumps(bad) + "\n```", json.dumps(GOOD))
    bedrock.set_client(fake)
    rep = await report_llm.generate(leak_incident)
    assert rep is not None and rep.source == "bedrock"
    assert len(fake.calls) == 2
    feedback = fake.calls[1][-1]["content"][0]["text"]
    assert "failed validation" in feedback and "burst" in feedback


async def test_falls_back_to_template_after_two_failures(leak_incident):
    bad = json.dumps(report(("Three sensors were wet.", [])))
    bedrock.set_client(FakeModel(bad, "not json at all"))
    assert await report_llm.generate(leak_incident) is None
    saved = report_llm.latest(leak_incident)
    assert saved.source == "template" and len(saved.errors) >= 2


async def test_bedrock_error_falls_back(leak_incident):
    bedrock.set_client(FakeModel(TimeoutError("read timeout")))
    assert await report_llm.generate(leak_incident) is None
    assert "Bedrock call failed" in report_llm.latest(leak_incident).errors[0]


async def test_no_client_means_template_without_errors(leak_incident):
    assert await report_llm.generate(leak_incident) is None
    assert report_llm.latest(leak_incident) is None


def test_no_aws_credentials_means_no_client(monkeypatch):
    monkeypatch.setattr(bedrock.settings, "bedrock_model_id", "some-model")
    monkeypatch.setattr(bedrock, "_checked", False)
    import boto3

    monkeypatch.setattr(boto3.Session, "get_credentials", lambda self: None)
    assert bedrock.get_client() is None


# --------------------------------------------------------------------------- end to end


def _cite_first_signal(timeline):
    first = next(i for i in timeline["items"] if i["kind"] == "signal")
    action = next(i for i in timeline["items"] if i["kind"] == "action" and i["action"] == "valve.close")
    return json.dumps(
        report(
            (first["text"] + ".", [first["id"]]),
            ("Guardian closed the Main Water Valve and verified it.", [action["id"]]),
            summary="Guardian closed the water valve.",
        )
    )


async def test_leak_flow_produces_validated_report(guardian):
    bedrock.set_client(FakeModel(_cite_first_signal))
    await director.run("leak_empty_home")
    inc = await wait_terminal("water_leak")
    detail = None
    for _ in range(50):
        detail = (await guardian.get(f"/api/incidents/{inc.id}")).json()
        if detail["llm_report"]:
            break
        import asyncio

        await asyncio.sleep(0.05)
    assert detail["llm_report"]["source"] == "bedrock"
    assert detail["report_text"].startswith("Water leak")  # template kept as fallback
    ref = detail["llm_report"]["refs"][detail["llm_report"]["sentences"][0]["cites"][0]]
    assert ref["id"] in {e["id"] for e in detail["evidence"]}
