"""Field semantics for the operator's explicit profile."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from .contract import MAX_COLUMNS, MAX_CONFIG, CanonicalRow, EventTime, PreviewError
from .reader import JsonNumber, check_json_fields, load_json

FIELDS = {
    "source",
    "source_place_id",
    "source_review_id",
    "reviewer_id",
    "text",
    "rating",
    "published_at",
    "source_url",
    "observed_at",
}
STAMP = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})[T ]([0-9]{2}:[0-9]{2})"
    r"(?::([0-9]{2})(?:\.([0-9]{1,6}))?)?(Z|[+-][0-9]{2}:[0-9]{2})?"
)


def event_time(raw, zone: ZoneInfo | None, local_date: str | None = None):
    if raw is None or raw == "":
        return EventTime("missing", raw), "missing_date"
    if not isinstance(raw, str):
        return EventTime("invalid"), "invalid_date"
    match = STAMP.fullmatch(raw)
    try:
        if not match:
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
                day = date.fromisoformat(raw)
            elif local_date == "DD.MM.YYYY" and re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", raw):
                day = datetime.strptime(raw, "%d.%m.%Y").date()
            else:
                return EventTime("invalid", raw), "invalid_date"
            return EventTime("date", raw, day.isoformat(), "day"), None
        _, _, seconds, fraction, offset = match.groups()
        precision = f"fraction-{len(fraction)}" if fraction else "second" if seconds else "minute"
        if offset and offset != "Z" and int(offset[-2:]) >= 60:
            raise ValueError
        stamp = datetime.fromisoformat(raw)
        if stamp.tzinfo is not None:
            instant = stamp.astimezone(UTC)
        else:
            naive = stamp
            local = EventTime("local", raw, naive.isoformat(), precision, offset)
            if not zone:
                return local, "unknown_timezone"
            candidates = set()
            for fold in (0, 1):
                utc = naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                if utc.astimezone(zone).replace(tzinfo=None) == naive:
                    candidates.add(utc)
            if len(candidates) != 1:
                return local, "ambiguous_time" if candidates else "nonexistent_time"
            (instant,) = candidates
        return EventTime("instant", raw, instant.isoformat(), precision, offset), None
    except (ValueError, OverflowError):
        return EventTime("invalid", raw), "invalid_date"


def nonblank(value):
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True, slots=True)
class MappingProfile:
    version: str
    fields: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class Passport:
    source: str | None
    source_place_id: str | None
    encoding: str
    delimiter: str | None
    event_basis: str
    timezone: str | None
    local_date: str | None
    rating_scale: list[int] | None
    observed_at: EventTime | None
    reviewer_id_allowed: bool


def configuration(fmt: str, mapping_bytes: bytes, passport_bytes: bytes):
    if max(len(mapping_bytes), len(passport_bytes)) > MAX_CONFIG:
        raise PreviewError("over_limit")
    if fmt not in ("csv", "json"):
        raise PreviewError("invalid_format")
    mapping = load_json(mapping_bytes)
    p = load_json(passport_bytes)
    check_json_fields(mapping)
    check_json_fields(p)
    if (
        not isinstance(mapping, dict)
        or mapping.keys() != {"version", "fields"}
        or not nonblank(mapping["version"])
    ):
        raise PreviewError("invalid_mapping")
    fields = mapping["fields"]
    if not isinstance(fields, dict) or not 0 < len(fields) <= MAX_COLUMNS:
        raise PreviewError("invalid_mapping")
    if any(
        not nonblank(k) or v is not None and (not isinstance(v, str) or v not in FIELDS)
        for k, v in fields.items()
    ):
        raise PreviewError("invalid_mapping")
    targets = [v for v in fields.values() if v is not None]
    if len(set(targets)) != len(targets):
        raise PreviewError("conflicting_mapping")
    if not targets:
        raise PreviewError("invalid_mapping")
    if not isinstance(p, dict) or p.keys() - Passport.__dataclass_fields__.keys():
        raise PreviewError("invalid_passport")
    for key in ("source", "source_place_id", "timezone"):
        if key in p and not nonblank(p[key]):
            raise PreviewError("invalid_passport")
    zone_name = p.get("timezone")
    if zone_name and (zone_name == "localtime" or zone_name not in available_timezones()):
        raise PreviewError("invalid_passport")
    try:
        zone = ZoneInfo(zone_name) if zone_name else None
    except (ValueError, ZoneInfoNotFoundError):
        raise PreviewError("invalid_passport") from None
    encoding = p.get("encoding", "utf-8")
    delimiter = p.get("delimiter")
    basis = p.get("event_basis", "unknown")
    scale = p.get("rating_scale")
    allow = p.get("reviewer_id_allowed", False)
    if (
        encoding not in ("utf-8", "utf-8-sig", "cp1251")
        or fmt == "json"
        and (encoding == "cp1251" or delimiter is not None)
        or fmt == "csv"
        and delimiter not in (",", ";", "\t", "|")
        or basis not in ("published", "submitted", "edited", "unknown")
        or p.get("local_date") not in (None, "DD.MM.YYYY")
        or type(allow) is not bool
        or "reviewer_id" in targets
        and not allow
    ):
        raise PreviewError("invalid_passport")
    if scale is not None and (
        not isinstance(scale, list)
        or len(scale) != 2
        or any(type(n) is not int for n in scale)
        or scale[0] >= scale[1]
    ):
        raise PreviewError("invalid_passport")
    observed = None
    if "observed_at" in p:
        observed, _ = event_time(p["observed_at"], None)
        if observed.kind != "instant":
            raise PreviewError("invalid_passport")
    if "observed_at" in targets and not observed:
        raise PreviewError("invalid_passport")
    passport = Passport(
        p.get("source"),
        p.get("source_place_id"),
        encoding,
        delimiter,
        basis,
        zone_name,
        p.get("local_date"),
        scale,
        observed,
        allow,
    )
    return MappingProfile(mapping["version"], fields), passport, zone


def stars(raw, scale):
    if raw is None or raw == "":
        return None, "missing_rating"
    if not isinstance(raw, str) or not re.fullmatch(
        r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", raw
    ):
        return None, "invalid_rating"
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None, "invalid_rating"
    if scale is None:
        return None, "unmapped_scale"
    if not scale[0] <= number <= scale[1]:
        return None, "invalid_rating"
    if scale != [1, 5]:
        return None, "unmapped_scale"
    if number != number.to_integral_value():
        return None, "invalid_rating"
    return int(number), None


def clean_url(raw):
    if not raw:
        return None, None
    try:
        url = urlsplit(raw)
        if (
            url.scheme in ("https", "http")
            and url.hostname
            and re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]+)?", url.netloc)
            and all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in url.hostname.split(".")
            )
            and re.fullmatch(r"[/A-Za-z0-9._~-]*", url.path)
            and url.port != 0
            and not any(c in raw for c in "?#\\")
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw)
        ):
            return raw, None
    except ValueError:
        pass
    return None, "invalid_url"


def normalize(position, record, errors, profile, p, zone):
    row = CanonicalRow(position, event_basis=p.event_basis)
    for code in errors:
        row.quarantine(code)
    if "invalid_json_item" in errors:
        return row
    values = {}
    for name, raw in record.items():
        target = profile.fields.get(name)
        if isinstance(raw, (dict, list)):
            row.quarantine("invalid_field_type")
            continue
        if target is None:
            continue
        row.field_provenance[target] = f"field:{name}"
        if target == "rating":
            values[target] = raw.raw if isinstance(raw, JsonNumber) else raw
        elif raw is None or isinstance(raw, str):
            values[target] = raw
        elif target == "published_at":
            values[target] = raw
        else:
            row.quarantine("invalid_field_type")
    for key, missing in (("source", "missing_source"), ("source_place_id", "missing_place")):
        cell = values.get(key)
        binding = getattr(p, key)
        if nonblank(cell):
            setattr(row, key, cell)
            if binding is not None and cell != binding:
                row.quarantine("binding_mismatch")
        elif binding is not None:
            setattr(row, key, binding)
            row.field_provenance[key] = f"passport:{key}"
        else:
            row.quarantine(missing)
    if "observed_at" in row.field_provenance:
        observed, _ = event_time(values.get("observed_at"), None)
        if observed.kind != "instant" or observed.value != p.observed_at.value:
            row.quarantine("observation_mismatch")
    row.text = values.get("text")
    row.source_review_id = (
        values.get("source_review_id") if nonblank(values.get("source_review_id")) else None
    )
    row.reviewer_id = values.get("reviewer_id") if nonblank(values.get("reviewer_id")) else None
    row.rating_raw = values.get("rating")
    row.rating_1_to_5, rating_issue = stars(row.rating_raw, p.rating_scale)
    row.event, date_issue = event_time(values.get("published_at"), zone, p.local_date)
    row.source_url, url_issue = clean_url(values.get("source_url"))
    row.warnings.extend(
        code
        for code in (
            "missing_review_id" if row.source_review_id is None else None,
            "missing_reviewer" if row.reviewer_id is None else None,
            "missing_text" if not nonblank(row.text) else None,
            rating_issue,
            date_issue,
            url_issue,
            "ineligible_event_basis"
            if row.event.kind != "missing" and p.event_basis in ("unknown", "edited")
            else None,
        )
        if code
    )
    return row
