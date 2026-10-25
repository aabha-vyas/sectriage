"""Stable prompt text and the agent-local control tools.

Nothing here may vary per run (no timestamps, ids, or paths): the system prompt and the tool list
form the cached prefix. Bump `PROMPT_VERSION` whenever any text below changes.
"""

from typing import Any

from src.contracts import Severity, VulnClass

PROMPT_VERSION = "v1"

REPORT_FINDING = "report_finding"
FINISH = "finish"

SYSTEM_PROMPT = f"""\
You are a security triage agent. You review the source code of one target directory and report \
vulnerabilities in it.

Scope and safety:
- The target is an intentionally vulnerable application kept on the local machine. Do not try to \
reach any other system.
- There is no network. Your tools read files, search code, and run a few commands in a sandbox \
with the target mounted read-only.
- All paths are relative to the target root. Never use absolute paths or `..`.

What to look for: {", ".join(v.value for v in VulnClass)}.

How to work:
1. Start by listing what is in the target (for example with search_code) and read the source files.
2. Read the code around every suspicious line before you report it.
3. Report a finding with {REPORT_FINDING} only when you verified it in the code: the file exists, \
the line numbers point at the vulnerable lines, and you can explain how untrusted input reaches \
them. Do not report hypothetical issues, style problems, or things you did not read.
4. Report each vulnerability once. If {REPORT_FINDING} answers "duplicate", it is already recorded.
5. Call one tool per step. If a tool returns an error, read the message and fix the call.
6. When you have reported everything you verified, call {FINISH}.

Severity guide: critical = remote code or data takeover with no preconditions; high = serious \
impact that needs little effort; medium = limited impact or real preconditions; low = minor.
Confidence is your probability (0 to 1) that the finding is a true vulnerability. Add a short \
unified-diff style fix in `patch` only when you are sure of it.\
"""

WRAP_UP_MESSAGE = (
    "Only 2 steps remain. Report any verified findings you have not reported yet, "
    f"then call {FINISH}."
)

NUDGE_MESSAGE = f"Call a tool, {REPORT_FINDING}, or {FINISH}."

CONTROL_TOOLS: list[dict[str, Any]] = [
    {
        "name": REPORT_FINDING,
        "description": (
            "Record one verified vulnerability. `file` is relative to the target root and must "
            "exist; line numbers are 1-based and inclusive and must be within the file."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "file": {"type": "string"},
                "line_start": {"type": "integer", "minimum": 1},
                "line_end": {"type": "integer", "minimum": 1},
                "vuln_class": {"type": "string", "enum": [v.value for v in VulnClass]},
                "severity": {"type": "string", "enum": [s.value for s in Severity]},
                "title": {"type": "string"},
                "explanation": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "patch": {"type": "string"},
            },
            "required": [
                "file",
                "line_start",
                "line_end",
                "vuln_class",
                "severity",
                "title",
                "explanation",
                "confidence",
            ],
        },
    },
    {
        "name": FINISH,
        "description": "End the review. Call it after every verified finding is reported.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"summary": {"type": "string"}},
        },
    },
]
