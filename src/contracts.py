"""Event, task, and tool schemas shared by every lane. Change only on main."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolName(StrEnum):
    READ_FILE = "read_file"
    SEARCH_CODE = "search_code"
    RUN_COMMAND = "run_command"
    SEMGREP = "semgrep"
    GIT = "git"


class VulnClass(StrEnum):
    SQLI = "sqli"
    XSS = "xss"
    PATH_TRAVERSAL = "path_traversal"
    COMMAND_INJECTION = "command_injection"
    HARDCODED_SECRET = "hardcoded_secret"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Task(_Model):
    task_id: str
    target_path: str
    goal: str
    max_steps: int = Field(gt=0)
    timeout_s: int = Field(gt=0)
    model: str
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)


class ReadFileArgs(_Model):
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _range_ordered(self) -> "ReadFileArgs":
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must be >= start_line")
        return self


class SearchCodeArgs(_Model):
    pattern: str
    path: str | None = None


class RunCommandArgs(_Model):
    argv: list[str] = Field(min_length=1)
    timeout_s: int = Field(gt=0)


class SemgrepArgs(_Model):
    config: str
    path: str


class GitArgs(_Model):
    subcommand: str
    args: list[str] = Field(default_factory=list)


TOOL_ARGS: dict[ToolName, type[_Model]] = {
    ToolName.READ_FILE: ReadFileArgs,
    ToolName.SEARCH_CODE: SearchCodeArgs,
    ToolName.RUN_COMMAND: RunCommandArgs,
    ToolName.SEMGREP: SemgrepArgs,
    ToolName.GIT: GitArgs,
}


class ToolCall(_Model):
    call_id: str
    tool: ToolName
    args: dict[str, Any]

    @model_validator(mode="after")
    def _args_match_tool(self) -> "ToolCall":
        TOOL_ARGS[self.tool].model_validate(self.args)
        return self


class ToolResult(_Model):
    call_id: str
    ok: bool
    output: str = ""
    truncated: bool = False
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _ok_implies_no_error(self) -> "ToolResult":
        if self.ok and self.error is not None:
            raise ValueError("ok=True requires error to be None")
        return self


class Finding(_Model):
    finding_id: str
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    vuln_class: VulnClass
    severity: Severity
    title: str
    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
    patch: str | None = None

    @field_validator("file")
    @classmethod
    def _relative_posix(cls, v: str) -> str:
        if v.startswith("/"):
            raise ValueError("file must be relative, not start with '/'")
        if "\\" in v:
            raise ValueError("file must use '/' separators, not backslashes")
        if len(v) > 1 and v[1] == ":":
            raise ValueError("file must not start with a drive letter")
        if any(seg in ("", ".", "..") for seg in v.split("/")):
            raise ValueError("file must be normalized: no empty, '.', or '..' segments")
        return v

    @model_validator(mode="after")
    def _range_ordered(self) -> "Finding":
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self

    @property
    def dedupe_key(self) -> tuple[str, int, int, VulnClass]:
        return (self.file, self.line_start, self.line_end, self.vuln_class)


class Event(_Model):
    """One JSONL line per agent step.

    Fields are append-only: new fields must be optional with a default so old JSONL logs
    still load. `result` and `model_output` carry what replay (F8) needs to re-run a task
    without calling the model; `result_summary` is the human-readable form.
    """

    schema_version: int = 1
    task_id: str
    step: int = Field(ge=0)
    ts: datetime
    trace_id: str | None = None
    call_id: str | None = None
    model: str | None = None
    model_output: str | None = None
    stop_reason: str | None = None
    tool: ToolName | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    result: ToolResult | None = None
    result_summary: str = ""
    ok: bool = True
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    latency_ms: int = Field(default=0, ge=0)
    error: str | None = None

    @field_validator("ts")
    @classmethod
    def _ts_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("ts must be timezone-aware")
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _ok_implies_no_error(self) -> "Event":
        if self.ok and self.error is not None:
            raise ValueError("ok=True requires error to be None")
        return self

    def to_jsonl(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> "Event":
        return cls.model_validate_json(line)
