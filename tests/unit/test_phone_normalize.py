import pytest

from app.utils.phone_normalize import normalize_uz_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # ── Originally passing ────────────────────────────────────────────────
        ("+998901234567",   "+998901234567"),   # full E.164
        ("998901234567",    "+998901234567"),   # 12 digits, no +
        ("90 123 45 67",    "+998901234567"),   # spaces, 8-digit local
        # ── Previously failing ────────────────────────────────────────────────
        ("0901234567",      "+998901234567"),   # leading-zero 10-digit local
        # ── New: 9-digit without leading zero ─────────────────────────────────
        ("901234567",       "+998901234567"),   # bare 9-digit subscriber
        # ── New: formatting characters ────────────────────────────────────────
        ("(90) 123-45-67",  "+998901234567"),   # parens + dashes
        # ── New: realistic customer message formats ───────────────────────────
        ("+998 (90) 123-45-67", "+998901234567"),  # full with decorators
        (" +998901234567 ",     "+998901234567"),  # leading/trailing whitespace
        # ── New: invalid operator code ────────────────────────────────────────
        ("121234567",   None),   # 9 digits but operator code 12 is not valid
        # ── New: wrong length ─────────────────────────────────────────────────
        ("+998901234",       None),   # too short (11 digits total)
        ("+9989012345678",   None),   # too long (13 digits total)
        # ── New: old Soviet-era leading-8 format ─────────────────────────────
        # "8 90 123 45 67" strips to "8901234567" (10 digits, doesn't start
        # with 0 or 998), so we reject it rather than guess the country code.
        ("8 90 123 45 67",  None),
        # ── Originally passing: garbage inputs ───────────────────────────────
        ("invalid",         None),
        ("",                None),
    ],
)
def test_normalize_uz_phone(raw: str, expected: str | None) -> None:
    assert normalize_uz_phone(raw) == expected
