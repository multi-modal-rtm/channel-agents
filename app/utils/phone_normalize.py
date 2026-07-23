import re

# Two-digit operator codes for Uzbek mobile numbers.
_VALID_OPERATOR_CODES = frozenset({
    "33", "50", "55", "61", "62", "65", "66", "67", "69",
    "70", "71", "72", "73", "74", "75", "76", "77", "78", "79",
    "88", "90", "91", "93", "94", "95", "97", "98", "99",
})


def normalize_uz_phone(raw: str) -> str | None:
    """Return E.164 (+998XXXXXXXXX) for any common Uzbek phone format, or None."""
    if not raw or not raw.strip():
        return None

    stripped = raw.strip()
    # Remember whether the caller explicitly signalled an international prefix (+).
    # If so, the only valid interpretation is +998XXXXXXXXX (12 digits total).
    has_explicit_plus = stripped.startswith("+")

    digits = re.sub(r"\D", "", stripped)

    if not digits:
        return None

    # Resolve to a 9-digit subscriber number.
    if has_explicit_plus:
        # Explicit + means the caller claims this is already in international form.
        # Accept only exactly +998XXXXXXXXX.
        if len(digits) == 12 and digits.startswith("998"):
            subscriber = digits[3:]
        else:
            return None
    elif len(digits) == 12 and digits.startswith("998"):
        subscriber = digits[3:]
    elif len(digits) == 10 and digits.startswith("0"):
        subscriber = digits[1:]
    elif len(digits) == 9:
        subscriber = digits
    else:
        return None

    if subscriber[:2] not in _VALID_OPERATOR_CODES:
        return None

    return f"+998{subscriber}"
