"""Message ids remembered so /clear can delete them."""

from __future__ import annotations

def message_ids_for_clear(row: dict, command_id: int | None) -> list[int]:
    found: list[int] = []
    seen: set[int] = set()
    for value in row.get("message_ids") or []:
        if isinstance(value, int) and value not in seen:
            seen.add(value)
            found.append(value)
    if isinstance(command_id, int) and command_id not in seen:
        found.append(command_id)
    return found


def forget_messages(row: dict) -> dict:
    kept = dict(row)
    kept["message_ids"] = []
    return kept
