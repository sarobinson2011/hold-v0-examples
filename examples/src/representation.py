"""Canonical JSON for gate storage and evidence. Wire inputs use the normal JSON decoder."""
import json
import re


# Consume escaped backslashes as a unit so a literal backslash-u stays literal.
_ESCAPE = re.compile(r"\\(?:u(d[89a-f][0-9a-f]{2})|.)")


def canonical(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _restore_escape(match):
    digits = match.group(1)
    return chr(int(digits, 16)) if digits else match.group(0)


def load_canonical(text):
    # The writer emits supplementary scalars literally. Its escaped surrogate code
    # points are individual units, even when a direct caller supplied two in a row.
    if "\\u" in text:
        text = _ESCAPE.sub(_restore_escape, text)
    return json.loads(text)


def effect_action_key(action):
    try:
        action.encode("utf-8")
    except UnicodeEncodeError:
        return canonical(action).encode("utf-8")
    return action
