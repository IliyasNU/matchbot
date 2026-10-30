"""Validation shared by registration, settings and persistence."""

import json

MIN_AGE = 18
MAX_AGE = 100
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


def normalize_interests(value) -> list[str]:
    """Canonical public tags, shared by stored profiles and registration drafts."""
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
    if len(tags) > 5:
        raise ValueError("Choose at most five interests")
    return tags


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
