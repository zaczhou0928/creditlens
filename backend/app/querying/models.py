from dataclasses import dataclass, field
from typing import Any


@dataclass
class SqlExecution:
    sql: str
    success: bool
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    truncated: bool = False
    returned_rows: int = 0
    total_rows: int | None = None
    data_version: str = ""
    data_as_of: str = ""
    column_metadata: dict[str, dict] = field(default_factory=dict)
