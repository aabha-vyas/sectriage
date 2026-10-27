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
    SYSTEM_PROMPT,
    WRAP_UP_MESSAGE,
)

# Fingerprint of every prompt text per version. Editing any text changes the hash, so the edit has
# to bump PROMPT_VERSION and add a row here. The v2 experiment lives on tag prompt-v2-experiment.
FINGERPRINTS = {
    "v1": "4ccec56723ac6a05f5a7ca12e50c1ee31df911ccc82315517eb8f05c584b5759",
}


def fingerprint() -> str:
    parts = [SYSTEM_PROMPT, WRAP_UP_MESSAGE, NUDGE_MESSAGE, CONTROL_TOOLS]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def test_prompt_text_is_pinned_to_its_version():
    assert PROMPT_VERSION in FINGERPRINTS, "bump PROMPT_VERSION and add its fingerprint"
    assert fingerprint() == FINGERPRINTS[PROMPT_VERSION], (
        f"prompt text changed without a new PROMPT_VERSION (still {PROMPT_VERSION})"
    )


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
