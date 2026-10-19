"""Deterministic value coercion: dates, money, durations, enums and lists.

Every coercion returns ``None`` (or the raw text) instead of guessing when the
input is ambiguous; callers surface that as an issue for the human.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ][0-9:.+\-Z]*)?$")
_YMD_SLASH = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})$")
_NUMERIC = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})$")
_D_MON_Y = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-/]+([A-Za-z]{3,9})\.?,?[\s\-/]+(\d{2}|\d{4})$")
_MON_D_Y = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def clean(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def _year(text: str) -> int:
    year = int(text)
    if len(text) == 2:
        year += 2000 if year < 70 else 1900
    return year


def _make(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def date_order(value: object) -> str | None:
    """'dmy' / 'mdy' when a numeric date is unambiguous, else None."""
    match = _NUMERIC.match(clean(value))
    if not match:
        return None
    a, b = int(match.group(1)), int(match.group(2))
    if a > 12 >= b:
        return "dmy"
    if b > 12 >= a:
        return "mdy"
    return None


def infer_dayfirst(values: list[str]) -> bool | None:
    orders = {date_order(v) for v in values} - {None}
    if orders == {"dmy"}:
        return True
    if orders == {"mdy"}:
        return False
    return None


def parse_date(value: object, *, dayfirst: bool | None = None) -> tuple[str | None, str]:
    """Return (ISO date or None, note). Ambiguous numeric dates need ``dayfirst``."""
    if isinstance(value, datetime):
        return value.date().isoformat(), ""
    if isinstance(value, date):
        return value.isoformat(), ""
    text = clean(value)
    if not text:
        return None, "empty"
    if m := _ISO.match(text):
        return _make(int(m[1]), int(m[2]), int(m[3])), ""
    if m := _YMD_SLASH.match(text):
        return _make(int(m[1]), int(m[2]), int(m[3])), ""
    if m := _COMPACT.match(text):
        return _make(int(m[1]), int(m[2]), int(m[3])), ""
    if m := _NUMERIC.match(text):
        a, b, y = int(m[1]), int(m[2]), _year(m[3])
        order = date_order(text)
        if order is None and a != b:
            if dayfirst is None:
                return None, f"ambiguous date {text!r} (day/month order unknown)"
            order = "dmy" if dayfirst else "mdy"
        d, mo = (a, b) if order in (None, "dmy") else (b, a)
        return _make(y, mo, d), ""
    if m := _D_MON_Y.match(text):
        month = _MONTHS.get(m[2].lower())
        if month:
            return _make(_year(m[3]), month, int(m[1])), ""
    if m := _MON_D_Y.match(text):
        month = _MONTHS.get(m[1].lower())
        if month:
            return _make(int(m[3]), month, int(m[2])), ""
    found = _DATE_IN_TEXT.search(text)
    if found and found.group(0) != text:
        value, note = parse_date(found.group(0), dayfirst=dayfirst)
        if value:
            return value, ""
    return None, f"unrecognised date {text[:40]!r}"


_DATE_IN_TEXT = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4}|"
                           r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{1,2}[/.]\d{1,2}[/.]\d{4}")


_CCY_SYMBOLS = (("US$", "USD"), ("A$", "AUD"), ("C$", "CAD"), ("S$", "SGD"), ("HK$", "HKD"),
                ("£", "GBP"), ("€", "EUR"), ("$", "USD"), ("¥", "JPY"), ("₹", "INR"), ("CHF", "CHF"))
CURRENCY_CODES = frozenset({
    "GBP", "USD", "EUR", "CHF", "JPY", "SGD", "HKD", "AUD", "CAD", "INR", "ZAR", "SEK",
    "NOK", "DKK", "PLN", "CNY", "BRL", "MXN", "AED", "NZD", "CZK", "HUF", "TRY", "KRW",
})
_SCALES = (
    (re.compile(r"(?i)\b(?:bn|billion|bln)\b|(?<=\d)(?:bn|bln|b)\b"), 1e9),
    (re.compile(r"(?i)\b(?:m|mn|mm|mio|million|millions|mln)\b|(?<=\d)(?:m|mn|mm|mln)\b"), 1e6),
    (re.compile(r"(?i)\b(?:k|thousand|thousands)\b|'000|\b000s\b|(?<=\d)k\b"), 1e3),
)
_NUMBER = re.compile(r"\d[\d,.\s']*\d|\d")


def currency_of(text: str) -> str | None:
    text = clean(text)
    for symbol, code in _CCY_SYMBOLS:
        if symbol in text:
            return code
    for token in re.findall(r"\b[A-Z]{3}\b", text.upper()):
        if token in CURRENCY_CODES:
            return token
    return None


def scale_of(text: str) -> float:
    for pattern, factor in _SCALES:
        if pattern.search(text):
            return factor
    return 1.0


def _number(text: str) -> float | None:
    match = _NUMBER.search(text)
    if not match:
        return None
    raw = re.sub(r"[\s']", "", match.group(0))
    if "," in raw and "." in raw:
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        raw = raw.replace("." if decimal == "," else ",", "").replace(",", ".")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_money(value: object, *, header_hint: str = "") -> tuple[float | None, str | None]:
    """Parse '£1.2m', 'GBP 1,200,000', '(450k)', '1.234.567,89 EUR'.

    The header hint ('Gross loss (GBP m)') supplies currency and scale only when
    the value does not carry its own.
    """
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        amount, own_scale, ccy = float(value), None, None
    else:
        text = clean(value)
        if not text or text.lower() in {"n/a", "na", "-", "tbc", "unknown", "nil"}:
            return None, None
        amount = _number(text)
        if amount is None:
            return None, None
        if text.startswith("(") and text.endswith(")") or text.startswith("-"):
            amount = -amount
        factor = scale_of(re.sub(r"\b[A-Z]{3}\b", " ", text))
        own_scale = factor if factor != 1.0 else None
        ccy = currency_of(text)
    if own_scale is None:
        own_scale = scale_of(header_hint) if header_hint else 1.0
    return amount * own_scale, ccy or (currency_of(header_hint) if header_hint else None)


def format_money(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return ""
    for factor, suffix in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if abs(amount) >= factor:
            return f"{currency or ''} {amount / factor:g}{suffix}".strip()
    return f"{currency or ''} {amount:g}".strip()


_DURATION = re.compile(
    r"(?i)^\s*(\d+(?:\.\d+)?)\s*(business\s+days?|working\s+days?|d|days?|h|hrs?|hours?|"
    r"m|mins?|minutes?|w|wks?|weeks?)\s*$"
)
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
_UNIT_MINUTES = {"d": 1440, "day": 1440, "days": 1440, "h": 60, "hr": 60, "hrs": 60, "hour": 60,
                 "hours": 60, "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
                 "w": 10080, "wk": 10080, "wks": 10080, "week": 10080, "weeks": 10080}


def parse_duration_minutes(value: object) -> tuple[float | None, str]:
    text = clean(value).lower().replace("<", "").replace("≤", "").replace("up to", "").strip()
    if not text:
        return None, ""
    if m := _HHMM.match(text):
        return int(m[1]) * 60 + int(m[2]), ""
    if m := _DURATION.match(text):
        unit = m[2].lower()
        if unit.startswith(("business", "working")):
            return None, "business-day durations are kept as text"
        return float(m[1]) * _UNIT_MINUTES[unit], ""
    return None, f"unrecognised duration {text[:40]!r}"


def format_minutes(minutes: float) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        n, unit = minutes / 1440, "day"
    elif minutes >= 60 and minutes % 60 == 0:
        n, unit = minutes / 60, "hour"
    else:
        n, unit = minutes, "minute"
    return f"{n:g} {unit}{'' if n == 1 else 's'}"


def tolerance_text(rto: object = "", rpo: object = "", tolerance: object = "") -> str:
    parts = []
    if clean(tolerance):
        minutes, _ = parse_duration_minutes(tolerance)
        parts.append(format_minutes(minutes) if minutes is not None else clean(tolerance))
    for label, raw in (("RTO", rto), ("RPO", rpo)):
        raw = clean(raw)
        if not raw:
            continue
        minutes, _ = parse_duration_minutes(raw)
        if minutes is None:
            parts.append(f"{label} {raw}")
        else:
            text = format_minutes(minutes)
            parts.append(f"{label} {text}" if text.endswith(("minute", "minutes")) else f"{label} {text} ({minutes:g} minutes)")
    return "; ".join(parts)


def criticality(value: object) -> str | None:
    text = clean(value).lower()
    if not text:
        return None
    if re.fullmatch(r"(tier|t|p|level|cat|category)?\s*-?\s*0?1|critical|very high|gold|mission critical|"
                    r"important business service|ibs|cbs|vital", text):
        return "critical"
    if re.fullmatch(r"(tier|t|p|level|cat|category)?\s*-?\s*0?2|high|important|silver|essential", text):
        return "high"
    if re.fullmatch(r"(tier|t|p|level|cat|category)?\s*-?\s*0?[345]|medium|low|standard|bronze|"
                    r"normal|moderate|non-critical|non critical", text):
        return "standard"
    return None


def boolean(value: object) -> bool | None:
    text = clean(value).lower()
    if text in {"y", "yes", "true", "1", "x", "✓", "near miss", "near-miss"}:
        return True
    if text in {"n", "no", "false", "0", ""}:
        return False if text else None
    return None


def split_list(value: object) -> list[str]:
    text = str(value or "")
    return [clean(part) for part in re.split(r"[;|\n]", text) if clean(part)]


_STATUS_MAPS = {
    "controls": {"validated": ("effective", "pass", "passed", "validated", "operating effectively",
                               "tested effective", "satisfactory", "green"),
                 "failed": ("ineffective", "fail", "failed", "not effective", "deficient", "red",
                            "tested ineffective", "unsatisfactory"),
                 "untested": ("untested", "not tested", "n/a", "na", "pending", "design only",
                              "not yet tested", "tbc", "planned")},
    "actions": {"open": ("open", "new", "not started", "raised", "outstanding"),
                "in_progress": ("in progress", "in-progress", "ongoing", "wip", "started", "amber"),
                "blocked": ("blocked", "on hold", "stalled"),
                "closed": ("closed", "complete", "completed", "done", "resolved", "implemented"),
                "cancelled": ("cancelled", "canceled", "withdrawn", "superseded")},
    "assumptions": {"open": ("open", "unvalidated", "untested", "active"),
                    "validated": ("validated", "confirmed", "holds"),
                    "invalidated": ("invalidated", "disproved", "broken", "failed")},
    "exercises": {"planned": ("planned", "scheduled", "proposed", "not started"),
                  "completed": ("completed", "complete", "done", "held", "run")},
    "internal_loss_events": {"open": ("open", "provisional", "under investigation", "pending"),
                             "closed": ("closed", "final", "settled", "complete", "completed")},
}


def status(kind: str, value: object) -> str | None:
    """Exact vocabulary first, then the leading segment ('Closed - near miss' -> closed)."""
    text = clean(value).lower()
    lead = re.split(r"\s+[-–—(]\s*|,|;", text, maxsplit=1)[0].strip()
    for candidate in (text, lead):
        for target, words in _STATUS_MAPS.get(kind, {}).items():
            if candidate in words:
                return target
    return None


BASEL_EVENT_TYPES = {
    "ET1": ("internal fraud",),
    "ET2": ("external fraud",),
    "ET3": ("employment practices", "workplace safety"),
    "ET4": ("clients, products", "clients products", "business practices"),
    "ET5": ("damage to physical assets", "physical assets"),
    "ET6": ("business disruption", "system failures", "systems failures"),
    "ET7": ("execution, delivery", "execution delivery", "process management"),
}
_ET_CODE = re.compile(r"(?i)\b(?:ET|EL|BET)\s*[-_]?\s*0?([1-7])\b")


def basel_event_type(value: object) -> str | None:
    text = clean(value)
    if m := _ET_CODE.search(text):
        return f"ET{m[1]}"
    lowered = text.lower()
    for code, names in BASEL_EVENT_TYPES.items():
        if any(name in lowered for name in names):
            return code
    if re.fullmatch(r"[1-7]", text):
        return f"ET{text}"
    return None


_ONE_IN = re.compile(r"(?i)\b1\s*(?:[-–]\s*in\s*[-–]|in|:|/)\s*(\d+(?:[.,]\d+)?)\s*(?:years?|yrs?|y)?")  # '1-in-10'
_PER_YEAR = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(?:x|times)?\s*(?:per|a|/)\s*(?:year|annum|yr|pa)\b")


def annual_frequency(value: object) -> float | None:
    """'1 in 20 years' -> 0.05, '0.5 per year' -> 0.5, '5%' -> 0.05."""
    text = clean(value)
    if not text:
        return None
    if m := _ONE_IN.search(text):
        n = float(m[1].replace(",", "."))
        return round(1.0 / n, 10) if n > 0 else None
    if m := _PER_YEAR.search(text):
        return float(m[1])
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*%(?:\s*(?:p\.?a\.?|per year|annual))?", text):
        return float(m[1]) / 100.0
    if re.fullmatch(r"\d*\.\d+|0", text):
        return float(text)
    return None
