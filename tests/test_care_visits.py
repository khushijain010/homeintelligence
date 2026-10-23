from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import select

from guardian import clock, db
from guardian.demo import director
from guardian.engine import incidents
from guardian.models import CareSchedule
from guardian.state import care_visits

from .conftest import wait_terminal

MONDAY = date(2026, 10, 19)


def _window() -> tuple[datetime, datetime]:
    with db.session() as s:
        schedule = s.exec(select(CareSchedule)).one()
    return care_visits.window(schedule, MONDAY)


async def _assess(scenario: str, start_offset: timedelta = timedelta(0)):
    start, end = _window()
    care_visits.monitoring_since = start - timedelta(hours=1)
    clock.freeze(start - timedelta(minutes=31))
    await director.play(scenario, start + start_offset)
    clock.freeze(end + timedelta(hours=2))
    return care_visits.check()


@pytest.mark.parametrize(
    "scenario,offset,expected",
    [
        ("home_aide_visit", timedelta(0), "completed"),
        ("late_arrival", timedelta(0), "late"),
        ("short_visit", timedelta(0), "short"),
        ("partial_blackout", timedelta(0), "unverified"),
    ],
)
async def test_visit_status(guardian, scenario, offset, expected):
    [visit] = await _assess(scenario, offset)
    assert visit.status == expected
    assert not incidents.open_incidents("caregiver_no_show")


async def test_home_aide_visit_duration(guardian):
    [visit] = await _assess("home_aide_visit")
    start, _ = _window()
    assert clock.as_utc(visit.arrived_at) == start
    assert visit.duration_min == 90


async def test_no_show_opens_incident_and_notifies_family(guardian):
    [visit] = await _assess("no_show", timedelta(minutes=10))
    assert visit.status == "no_show"
    inc = await wait_terminal("caregiver_no_show")
    assert inc.status == "escalated"
    family = [a for a in incidents.actions(inc.id) if a.action_type == "notify.family"]
    assert family and family[0].result == "ok"
    assert "Caregiver no-show — Sunita" in inc.report_text


async def test_no_verdict_while_window_open(guardian):
    start, _ = _window()
    care_visits.monitoring_since = start - timedelta(hours=1)
    clock.freeze(start + timedelta(minutes=20))
    assert care_visits.check() == []


async def test_windows_before_monitoring_are_not_judged(guardian):
    start, end = _window()
    care_visits.monitoring_since = datetime.now(UTC)  # started monitoring after the window
    clock.freeze(end + timedelta(hours=2))
    assert care_visits.check() == []
