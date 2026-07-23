"""Small access-control helpers shared by the Streamlit app and tests."""

from __future__ import annotations

from typing import Any


PUBLIC_WORKSPACE_USERNAME = "__public_workspace__"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def parse_flag(value: Any, default: bool = False) -> bool:
    """Parse a boolean-like setting while keeping an explicit safe default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default
