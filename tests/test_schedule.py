from datetime import datetime, timezone

from aiverse_automations.schedule import first_run_at, advance_after_fire, validate_trigger_spec
from aiverse_automations.errors import ValidationError


def test_interval_coalesces_missed_occurrences():
    spec={"seconds":60}
    scheduled=datetime(2026,1,1,0,0,tzinfo=timezone.utc)
    now=datetime(2026,1,1,0,5,10,tzinfo=timezone.utc)
    nxt=advance_after_fire("interval",spec,scheduled_for=scheduled,now=now)
    assert nxt=="2026-01-01T00:06:00Z"


def test_cron_timezone_is_validated():
    validate_trigger_spec("cron",{"expr":"0 9 * * 1","timezone":"Europe/Bucharest"})
    try:
        validate_trigger_spec("cron",{"expr":"bad cron","timezone":"UTC"})
    except ValidationError:
        pass
    else:
        raise AssertionError("bad cron must fail")
