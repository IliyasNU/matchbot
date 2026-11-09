"""Validation shared by registration, settings and persistence."""

import json

MIN_AGE = 18
MAX_AGE = 100
MAX_INTERESTS = 5
MAX_DATABASE_ID = (1 << 63) - 1
GENDERS = {"male", "female", "other"}
PREFERENCES = GENDERS | {"any"}
INTERESTS = (
    "music",
    "movies",
    "books",
    "gaming",
    "sports",
    "travel",
    "art",
    "tech",
    "cooking",
    "nature",
    "study",
    "fitness",
)
PURPOSES = frozenset({"any", "relationship", "friendship", "chat"})


def parse_integer(value, *, minimum=1, maximum=MAX_DATABASE_ID):
    """Parse untrusted ASCII integers without overflowing a SQLite parameter."""
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > len(str(maximum)):
        return None
    number = int(value)
    return number if minimum <= number <= maximum else None


def normalize_interests(value) -> list[str]:
    """Strict validation for newly supplied public interest tags."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Interests must be a JSON list") from exc
    if not isinstance(value, (list, tuple)):
        raise ValueError("Interests must be a list")
    tags = []
    for tag in value:
        if not isinstance(tag, str):
            raise ValueError("Invalid interest")
        tag = tag.strip().casefold()
        if tag not in INTERESTS:
            raise ValueError("Unknown interest")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_INTERESTS:
        raise ValueError("Choose at most five interests")
    return tags


def read_interests(value) -> list[str]:
    """Read legacy optional metadata without rejecting the rest of a profile.

    Retired tags and malformed entries are neutral. Keep stored values untouched
    until the owner next saves their profile using strict write validation.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return []
    if not isinstance(value, (list, tuple)):
        return []
    tags = []
    for tag in value:
        if not isinstance(tag, str):
            continue
        tag = tag.strip().casefold()
        if tag in INTERESTS and tag not in tags:
            tags.append(tag)
            if len(tags) >= MAX_INTERESTS:
                break
    return tags


def read_purpose(value) -> str:
    """Unknown or retired optional purposes fall back to the neutral value."""
    return value if isinstance(value, str) and value in PURPOSES else "any"


def clean_text(value: str, minimum: int, maximum: int) -> str:
    value = " ".join(value.split())
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"Expected {minimum}–{maximum} characters")
    return value


def age_range(value: str) -> tuple[int, int]:
    parts = value.replace("–", "-").replace("—", "-").split("-")
    if len(parts) != 2:
        raise ValueError("Use 18-35")
    low, high = map(int, parts)
    if not MIN_AGE <= low <= high <= MAX_AGE:
        raise ValueError("Invalid age range")
    return low, high


def validate_profile(data: dict) -> dict:
    result = dict(data)
    result["name"] = clean_text(data["name"], 1, 40)
    result["city"] = clean_text(data["city"], 2, 60)
    result["bio"] = clean_text(data["bio"], 0, 500)
    result["interests"] = normalize_interests(data.get("interests", []))
    result["purpose"] = data.get("purpose", "any")
    if result["purpose"] not in PURPOSES:
        raise ValueError("Invalid meeting purpose")
    if not MIN_AGE <= int(data["age"]) <= MAX_AGE:
        raise ValueError("Registration is 18+ only")
    if data["gender"] not in GENDERS or data["seeking"] not in PREFERENCES:
        raise ValueError("Invalid preference")
    if not data.get("photo") or not data.get("consent"):
        raise ValueError("Photo and consent required")
    return result
