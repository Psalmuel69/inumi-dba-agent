"""Data Minimization / Data Policy Layer (spec §22).

Database results are untrusted and potentially sensitive. Nothing coming
back from the Execution Service reaches the Agent (and therefore the LLM)
without passing through here first: sensitive fields are masked, rows and
columns are capped, and the whole result is capped in size.

Sensitive-field classification is configurable, not a hardcoded finite list
— `SENSITIVE_FIELD_PATTERNS` is a sensible default the config can extend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SENSITIVE_FIELD_PATTERNS: list[str] = [
    r"password",
    r"pass[_-]?hash",
    r"token",
    r"secret",
    r"api[_-]?key",
    r"card[_-]?number",
    r"account[_-]?number",
    r"\bbvn\b",
    r"\bnin\b",
    r"ssn",
    r"phone",
    r"e[-_]?mail",
    r"address",
    r"auth",
    r"credential",
    r"connection[_-]?string",
]


@dataclass
class DataPolicyConfig:
    sensitive_field_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_SENSITIVE_FIELD_PATTERNS)
    )
    field_allowlist: set[str] | None = None  # if set, ONLY these fields pass through
    field_denylist: set[str] = field(default_factory=set)
    max_rows: int = 100
    max_columns: int = 50
    max_result_bytes: int = 512_000


class DataMinimizer:
    def __init__(self, config: DataPolicyConfig | None = None):
        self._config = config or DataPolicyConfig()
        self._sensitive_re = re.compile(
            "|".join(self._config.sensitive_field_patterns), re.IGNORECASE
        )

    def _is_sensitive(self, field_name: str) -> bool:
        return bool(self._sensitive_re.search(field_name))

    def apply(
        self, rows: list[dict[str, Any]], *, max_rows: int | None = None
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        """Returns (minimized_rows, masked_field_names, truncated)."""
        cfg = self._config
        row_cap = max_rows or cfg.max_rows
        truncated = len(rows) > row_cap
        rows = rows[:row_cap]

        masked_fields: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            clean: dict[str, Any] = {}
            col_count = 0
            for k, v in row.items():
                if cfg.field_allowlist is not None and k not in cfg.field_allowlist:
                    continue
                if k in cfg.field_denylist:
                    continue
                if col_count >= cfg.max_columns:
                    truncated = True
                    break
                if self._is_sensitive(k):
                    clean[k] = "***MASKED***"
                    masked_fields.add(k)
                else:
                    clean[k] = v
                col_count += 1
            result.append(clean)

        return result, sorted(masked_fields), truncated
