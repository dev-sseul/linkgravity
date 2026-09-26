import re
from datetime import date, datetime, time, timedelta

_DURATION = re.compile(r"^\s*(\d+)\s*(m|min|h|d)\s*$", re.IGNORECASE)
_UNITS = {"m": "minutes", "min": "minutes", "h": "hours", "d": "days"}
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_DAYS = "sun mon tue wed thu fri sat".split()


class SpecError(ValueError):
    pass


def now() -> datetime:
    # Naive local wall-clock time: "every 1d from 09:00" must stay at 09:00 across DST shifts.
    return datetime.now().replace(microsecond=0)


def parse_duration(text: str) -> timedelta:
    m = _DURATION.match(text or "")
    if not m or int(m.group(1)) <= 0:
        raise SpecError(f"Bad duration {text!r} - use a positive number with m, h or d (e.g. 30m, 2h, 3d).")
    return timedelta(**{_UNITS[m.group(2).lower()]: int(m.group(1))})


def parse_datetime(text: str, ref: datetime) -> datetime:
    text = (text or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        t = time.fromisoformat(text.zfill(5))
        candidate = datetime.combine(ref.date(), t)
        return candidate if candidate > ref else candidate + timedelta(days=1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise SpecError(
            f"Bad time {text!r} - use ISO like 2026-09-27T09:00, or HH:MM for the next occurrence."
        ) from None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _parse_field(text: str, lo: int, hi: int, names: list[str] | None = None) -> set[int]:
    values = set()
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise ValueError
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _field_value(a, names), _field_value(b, names)
        else:
            start = _field_value(part, names)
            end = hi if step > 1 else start
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise ValueError
        values.update(range(start, end + 1, step))
    return values


def _field_value(text: str, names: list[str] | None) -> int:
    if names and text in names:
        return names.index(text) + (1 if len(names) == 12 else 0)
    return int(text)


def validate_cron(expr: str) -> tuple:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise SpecError(f"Bad cron {expr!r} - need 5 fields: minute hour day-of-month month day-of-week.")
    try:
        minutes = _parse_field(parts[0], 0, 59)
        hours = _parse_field(parts[1], 0, 23)
        doms = _parse_field(parts[2], 1, 31)
        months = _parse_field(parts[3], 1, 12, _MONTHS)
        # Both 0 and 7 mean Sunday in cron.
        dows = {d % 7 for d in _parse_field(parts[4], 0, 7, _DAYS)}
    except ValueError:
        raise SpecError(f"Bad cron {expr!r}.") from None
    return minutes, hours, doms, months, dows, parts[2] == "*", parts[4] == "*"


def cron_next(expr: str, after: datetime) -> datetime | None:
    minutes, hours, doms, months, dows, dom_any, dow_any = validate_cron(expr)
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day: date = start.date()
    for _ in range(366 * 5):
        dom_ok, dow_ok = day.day in doms, (day.weekday() + 1) % 7 in dows
        # Standard cron: when both day fields are restricted, either one matching is enough.
        day_ok = (dom_ok or dow_ok) if not dom_any and not dow_any else (dom_ok and dow_ok)
        if day.month in months and day_ok:
            for h in sorted(hours):
                for m in sorted(minutes):
                    candidate = datetime.combine(day, time(h, m))
                    if candidate >= start:
                        return candidate
        day += timedelta(days=1)
    return None


def build(args: dict, ref: datetime) -> dict:
    given = [k for k in ("at", "delay", "every", "cron") if args.get(k)]
    if len(given) != 1:
        raise SpecError("Give exactly one of: at, delay, every, cron.")
    kind = given[0]
    if kind == "at":
        first = parse_datetime(args["at"], ref)
        if first <= ref:
            raise SpecError(f"{first:%Y-%m-%d %H:%M} is already in the past.")
        return {"kind": "at", "anchor": first.isoformat()}
    if kind == "delay":
        return {"kind": "at", "anchor": (ref + parse_duration(args["delay"])).isoformat()}
    if kind == "every":
        interval = parse_duration(args["every"])
        first = parse_datetime(args["start"], ref) if args.get("start") else ref + interval
        return {"kind": "every", "every": args["every"].strip(), "anchor": first.isoformat()}
    validate_cron(args["cron"])
    return {"kind": "cron", "cron": " ".join(args["cron"].split())}


def next_after(spec: dict, after: datetime) -> datetime | None:
    kind = spec["kind"]
    if kind == "cron":
        return cron_next(spec["cron"], after)
    anchor = datetime.fromisoformat(spec["anchor"])
    if kind == "at":
        return anchor if anchor > after else None
    if anchor > after:
        return anchor
    interval = parse_duration(spec["every"])
    # Counted from the anchor rather than the last run, so late or skipped runs never shift the grid.
    steps = (after - anchor) // interval + 1
    return anchor + interval * steps


def describe(spec: dict) -> str:
    kind = spec["kind"]
    if kind == "cron":
        return f"cron `{spec['cron']}`"
    anchor = datetime.fromisoformat(spec["anchor"])
    if kind == "at":
        return f"once at {anchor:%Y-%m-%d %H:%M}"
    return f"every {spec['every']} from {anchor:%Y-%m-%d %H:%M}"
