from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ValidationError
from .util import iso, parse_time

_MONTH_NAMES = {name: i for i, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
)}
_DOW_NAMES = {name: i for i, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_wildcard: bool
    weekday_wildcard: bool


def _number(token: str, *, low: int, high: int, names: dict[str, int] | None = None, dow: bool = False) -> int:
    raw = token.strip().upper()
    if names and raw in names:
        value = names[raw]
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValidationError(f"invalid cron token: {token}") from exc
    if dow and value == 7:
        value = 0
    if not low <= value <= high:
        raise ValidationError(f"cron value {token} outside {low}..{high}")
    return value


def _field(text: str, *, low: int, high: int, names: dict[str, int] | None = None, dow: bool = False) -> tuple[frozenset[int], bool]:
    text = text.strip()
    if not text:
        raise ValidationError("empty cron field")
    wildcard = text == "*"
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValidationError("empty cron list item")
        base, step_text = (part.split("/", 1) + [None])[:2] if "/" in part else (part, None)
        step = 1
        if step_text is not None:
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValidationError("cron step must be an integer") from exc
            if step < 1 or step > (high - low + 1):
                raise ValidationError("cron step is outside the valid range")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _number(start_text, low=low, high=high, names=names, dow=dow)
            end = _number(end_text, low=low, high=high, names=names, dow=dow)
            if end < start:
                raise ValidationError("cron ranges may not wrap")
        else:
            value = _number(base, low=low, high=high, names=names, dow=dow)
            start, end = value, value
        values.update(range(start, end + 1, step))
    if not values:
        raise ValidationError("cron field resolves to no values")
    if dow:
        values = {0 if value == 7 else value for value in values}
    return frozenset(values), wildcard


def parse_cron(expr: str) -> CronSpec:
    fields = expr.split()
    if len(fields) != 5:
        raise ValidationError("cron expression must contain exactly five fields: minute hour day month weekday")
    minutes, _ = _field(fields[0], low=0, high=59)
    hours, _ = _field(fields[1], low=0, high=23)
    days, day_wildcard = _field(fields[2], low=1, high=31)
    months, _ = _field(fields[3], low=1, high=12, names=_MONTH_NAMES)
    weekdays, weekday_wildcard = _field(fields[4], low=0, high=7, names=_DOW_NAMES, dow=True)
    return CronSpec(minutes, hours, days, months, weekdays, day_wildcard, weekday_wildcard)


def _zone(name: str):
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError(f"invalid or unavailable timezone: {name}") from exc


def _cron_matches(spec: CronSpec, local: datetime) -> bool:
    if local.minute not in spec.minutes or local.hour not in spec.hours or local.month not in spec.months:
        return False
    dom = local.day in spec.days
    cron_dow = (local.weekday() + 1) % 7
    dow = cron_dow in spec.weekdays
    if spec.day_wildcard and spec.weekday_wildcard:
        return True
    if spec.day_wildcard:
        return dow
    if spec.weekday_wildcard:
        return dom
    # Traditional cron semantics: day-of-month and day-of-week are ORed when both are restricted.
    return dom or dow


def _next_cron(expr: str, tz_name: str, after: datetime) -> datetime:
    spec = parse_cron(expr)
    zone = _zone(tz_name)
    # Scan UTC minutes. This avoids constructing nonexistent local DST instants and naturally
    # represents repeated fall-back instants as distinct scheduled occurrences.
    candidate = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = candidate + timedelta(days=366 * 5)
    while candidate <= limit:
        if _cron_matches(spec, candidate.astimezone(zone)):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValidationError("cron expression produced no occurrence within five years")


def validate_trigger_spec(kind: str, spec: dict) -> None:
    if kind == "once":
        if set(spec) - {"at"} or not isinstance(spec.get("at"), str):
            raise ValidationError("once trigger requires only string field 'at'")
        parse_time(spec["at"])
        return
    if kind == "interval":
        if set(spec) - {"seconds"} or not isinstance(spec.get("seconds"), int):
            raise ValidationError("interval trigger requires only integer field 'seconds'")
        if spec["seconds"] < 60 or spec["seconds"] > 365 * 86400:
            raise ValidationError("interval seconds must be between 60 and 31536000")
        return
    if kind == "cron":
        allowed = {"expr", "timezone"}
        if set(spec) - allowed or not isinstance(spec.get("expr"), str):
            raise ValidationError("cron trigger requires expr and optional timezone")
        tz = spec.get("timezone", "UTC")
        if not isinstance(tz, str) or not tz:
            raise ValidationError("cron timezone must be a non-empty string")
        _zone(tz)
        parse_cron(spec["expr"])
        return
    if kind == "webhook":
        allowed = {"secret_ref", "max_skew_seconds", "event_type"}
        if set(spec) - allowed:
            raise ValidationError("unknown webhook trigger field")
        secret_ref = spec.get("secret_ref")
        if not isinstance(secret_ref, str) or not (secret_ref.startswith("env:") or secret_ref.startswith("file:")):
            raise ValidationError("webhook secret_ref must use env:NAME or file:/absolute/path")
        skew = spec.get("max_skew_seconds", 300)
        if not isinstance(skew, int) or skew < 30 or skew > 3600:
            raise ValidationError("webhook max_skew_seconds must be 30..3600")
        if "event_type" in spec and (not isinstance(spec["event_type"], str) or not spec["event_type"].strip()):
            raise ValidationError("webhook event_type must be a non-empty string")
        return
    if kind == "event":
        allowed = {"source", "event_type"}
        if set(spec) - allowed:
            raise ValidationError("unknown event trigger field")
        if not isinstance(spec.get("source"), str) or not spec["source"].strip():
            raise ValidationError("event trigger requires source")
        if "event_type" in spec and (not isinstance(spec["event_type"], str) or not spec["event_type"].strip()):
            raise ValidationError("event_type must be a non-empty string")
        return
    raise ValidationError(f"unsupported trigger kind: {kind}")


def first_run_at(kind: str, spec: dict, now: datetime | None = None) -> str | None:
    now = now or datetime.now(timezone.utc)
    if kind == "once":
        return iso(parse_time(spec["at"]))
    if kind == "interval":
        return iso(now + timedelta(seconds=spec["seconds"]))
    if kind == "cron":
        return iso(_next_cron(spec["expr"], spec.get("timezone", "UTC"), now))
    return None


def advance_after_fire(kind: str, spec: dict, *, scheduled_for: datetime, now: datetime) -> str | None:
    if kind == "once":
        return None
    if kind == "interval":
        seconds = spec["seconds"]
        candidate = scheduled_for + timedelta(seconds=seconds)
        if candidate <= now:
            missed = int((now - candidate).total_seconds() // seconds) + 1
            candidate += timedelta(seconds=missed * seconds)
        return iso(candidate)
    if kind == "cron":
        # Coalesce missed occurrences by asking for the next occurrence strictly after now.
        return iso(_next_cron(spec["expr"], spec.get("timezone", "UTC"), max(scheduled_for, now)))
    return None
