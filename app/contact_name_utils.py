import re
from typing import Iterable


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_contact_name(value: str) -> str:
    collapsed = _collapse_whitespace(value)
    if not collapsed:
        return ""

    stripped = _strip_avatar_initial_prefix(collapsed)
    return _collapse_whitespace(stripped)


def choose_best_contact_name(candidates: Iterable[str], fallback: str = "") -> str:
    unique_candidates: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        candidate = _collapse_whitespace(raw)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        unique_candidates.append(candidate)

    for candidate in unique_candidates:
        cleaned = normalize_contact_name(candidate)
        if cleaned and not _looks_like_avatar_initials(cleaned):
            return cleaned

    fallback_name = normalize_contact_name(fallback)
    if fallback_name:
        return fallback_name

    return normalize_contact_name(unique_candidates[0]) if unique_candidates else ""


def _collapse_whitespace(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", (value or "").strip())


def _strip_avatar_initial_prefix(value: str) -> str:
    max_prefix = min(4, len(value) - 1)
    for prefix_len in range(1, max_prefix + 1):
        prefix = value[:prefix_len]
        remainder = value[prefix_len:].lstrip()
        if not _looks_like_avatar_initials(prefix):
            continue
        if not _looks_like_name_start(remainder):
            continue
        if _prefix_matches_name(prefix, remainder):
            return remainder
    return value


def _looks_like_avatar_initials(value: str) -> bool:
    compact = value.replace(" ", "")
    if not 1 <= len(compact) <= 4:
        return False
    letters = [char for char in compact if char.isalpha()]
    if len(letters) != len(compact):
        return False
    return all(_is_upper_letter(char) for char in letters)


def _looks_like_name_start(value: str) -> bool:
    if len(value) < 2:
        return False
    return _is_upper_letter(value[0]) and value[1].islower()


def _prefix_matches_name(prefix: str, remainder: str) -> bool:
    words = [word for word in remainder.split(" ") if word]
    if not words:
        return False

    initials = [_first_alpha(word) for word in words]
    initials = [item for item in initials if item]
    if not initials:
        return False

    comparisons = {initials[0]}
    if len(initials) >= 2:
        comparisons.add(initials[0] + initials[1])
        comparisons.add(initials[0] + initials[-1])
    if len(initials) >= len(prefix):
        comparisons.add("".join(initials[: len(prefix)]))

    normalized_prefix = prefix.upper()
    return normalized_prefix in {item.upper() for item in comparisons}


def _first_alpha(value: str) -> str:
    for char in value:
        if char.isalpha():
            return char.upper()
    return ""


def _is_upper_letter(char: str) -> bool:
    return char.isalpha() and char == char.upper() and char != char.lower()
