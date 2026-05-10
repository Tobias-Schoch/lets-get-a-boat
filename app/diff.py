from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class DiffResult:
    changed: bool
    unified_diff: str
    reason: str  # 'unchanged' | 'whitespace_only' | 'changed' | 'first_seen'


def compute_diff(old: str | None, new: str, *, label_a: str = "before", label_b: str = "after") -> DiffResult:
    if old is None:
        return DiffResult(changed=True, unified_diff="", reason="first_seen")

    if old == new:
        return DiffResult(changed=False, unified_diff="", reason="unchanged")

    diff_lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=label_a,
            tofile=label_b,
            n=3,
            lineterm="",
        )
    )
    diff_text = "\n".join(diff_lines)

    payload_lines = [
        ln for ln in diff_lines if ln and ln[0] in "+-" and not ln.startswith(("+++", "---"))
    ]
    if payload_lines and all(not ln[1:].strip() for ln in payload_lines):
        return DiffResult(changed=False, unified_diff=diff_text, reason="whitespace_only")

    return DiffResult(changed=True, unified_diff=diff_text, reason="changed")
