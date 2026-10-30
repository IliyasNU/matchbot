"""Validation shared by registration, settings and persistence."""

MIN_AGE = 18
MAX_AGE = 100
GENDERS = {"male", "female", "other"}
PREFERENCES = GENDERS | {"any"}


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
    if not MIN_AGE <= int(data["age"]) <= MAX_AGE:
        raise ValueError("Registration is 18+ only")
    if data["gender"] not in GENDERS or data["seeking"] not in PREFERENCES:
        raise ValueError("Invalid preference")
    if not data.get("photo") or not data.get("consent"):
        raise ValueError("Photo and consent required")
    return result
