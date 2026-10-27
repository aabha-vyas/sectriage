import hashlib
import json
import re

import pytest

from evals.frozen import TARGETS_DIR
from evals.scorer import load_ground_truth
from src.agent.prompts import (
    CONTROL_TOOLS,
    NUDGE_MESSAGE,
    PROMPT_VERSION,
    REPORT_FINDING,
    SYSTEM_PROMPT,
    WRAP_UP_MESSAGE,
)

# Fingerprint of every prompt text per version. Editing any text changes the hash, so the edit has
# to bump PROMPT_VERSION and add a row here. v1 is kept so that a revert is recognised.
FINGERPRINTS = {
    "v1": "4ccec56723ac6a05f5a7ca12e50c1ee31df911ccc82315517eb8f05c584b5759",
    "v2": "d734766029212a0255b559074605d9bdbe51504ff615677c814c8b23a2182eba",
}


def fingerprint() -> str:
    parts = [SYSTEM_PROMPT, WRAP_UP_MESSAGE, NUDGE_MESSAGE, CONTROL_TOOLS]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def test_prompt_text_is_pinned_to_its_version():
    assert PROMPT_VERSION in FINGERPRINTS, "bump PROMPT_VERSION and add its fingerprint"
    assert fingerprint() == FINGERPRINTS[PROMPT_VERSION], (
        f"prompt text changed without a new PROMPT_VERSION (still {PROMPT_VERSION})"
    )


def test_system_prompt_tells_the_agent_to_report_when_verified():
    assert re.search(r"as soon as you have verified it", SYSTEM_PROMPT)
    assert REPORT_FINDING in SYSTEM_PROMPT.split("as soon as you have verified it")[1][:300]
    # the verification bar is the same as before
    assert "only when you verified it in the code" in SYSTEM_PROMPT
    assert "line numbers point at the vulnerable lines" in SYSTEM_PROMPT
    assert "untrusted input reaches" in SYSTEM_PROMPT


def _all_prompt_text() -> str:
    tools = json.dumps(CONTROL_TOOLS)
    return "\n".join([SYSTEM_PROMPT, WRAP_UP_MESSAGE, NUDGE_MESSAGE, tools])


@pytest.mark.parametrize("target", sorted(p.name for p in TARGETS_DIR.iterdir() if p.is_dir()))
def test_prompts_do_not_name_the_target_or_its_bugs(target: str):
    text = _all_prompt_text()
    assert target not in text
    for item in load_ground_truth(target).findings:
        assert item.file not in text
        assert item.id not in text


def test_system_prompt_states_no_step_count():
    # a count would vary per run and break the cached prefix
    assert not re.search(r"\d+\s+steps?\b", SYSTEM_PROMPT)
