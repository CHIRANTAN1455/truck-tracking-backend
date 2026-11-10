"""Indian mobile numbers. Drivers are stored with the 10-digit national number; telecom APIs use 91XXXXXXXXXX."""

import re

_MOBILE = re.compile(r"[6-9]\d{9}")


def normalize_in_mobile(raw: str) -> str:
    """'+91 98765-43210', '098765 43210', '919876543210' -> '9876543210'. Raises ValueError if not a mobile."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not _MOBILE.fullmatch(digits):
        raise ValueError("Enter a 10-digit Indian mobile number (starting with 6, 7, 8 or 9)")
    return digits


def to_msisdn(national: str) -> str:
    return "91" + normalize_in_mobile(national)
