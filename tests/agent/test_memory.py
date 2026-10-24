from src.agent.memory import (
    MAX_NOTE_CHARS,
    MAX_NOTES,
    FindingStore,
    ScratchMemory,
    call_key,
    make_finding_id,
)
from src.contracts import Finding, Severity, ToolResult, VulnClass


def finding(
    file: str = "app.py",
    start: int = 4,
    end: int = 4,
    vuln: VulnClass = VulnClass.COMMAND_INJECTION,
    **kw: object,
) -> Finding:
    fields: dict[str, object] = {
        "finding_id": make_finding_id(file, start, end, vuln),
        "file": file,
        "line_start": start,
        "line_end": end,
        "vuln_class": vuln,
        "severity": Severity.HIGH,
        "title": "t",
        "explanation": "e",
        "confidence": 0.9,
    }
    fields.update(kw)
    return Finding.model_validate(fields)


def test_store_dedupes_by_file_range_and_class():
    store = FindingStore()
    assert store.add(finding())
    assert not store.add(finding(title="different wording", confidence=0.1))
    assert len(store) == 1


def test_store_keeps_the_first_finding():
    store = FindingStore()
    store.add(finding(title="first"))
    store.add(finding(title="second"))
    assert [f.title for f in store.all()] == ["first"]


def test_store_distinguishes_range_file_and_class():
    store = FindingStore()
    assert store.add(finding())
    assert store.add(finding(start=5, end=5))
    assert store.add(finding(start=4, end=6))
    assert store.add(finding(file="other.py"))
    assert store.add(finding(vuln=VulnClass.SQLI))
    assert len(store) == 5


def test_store_preserves_insertion_order():
    store = FindingStore()
    for line in (9, 3, 6):
        store.add(finding(start=line, end=line))
    assert [f.line_start for f in store.all()] == [9, 3, 6]


def test_finding_id_is_stable_and_depends_on_the_key():
    a = make_finding_id("app.py", 4, 4, VulnClass.SQLI)
    assert a == make_finding_id("app.py", 4, 4, "sqli")
    assert a.startswith("f_")
    assert a != make_finding_id("app.py", 4, 5, VulnClass.SQLI)
    assert a != make_finding_id("app.py", 4, 4, VulnClass.XSS)
    assert a != make_finding_id("b.py", 4, 4, VulnClass.SQLI)


def test_call_key_ignores_argument_order():
    assert call_key("git", {"a": 1, "b": 2}) == call_key("git", {"b": 2, "a": 1})
    assert call_key("git", {"a": 1}) != call_key("read_file", {"a": 1})
    assert call_key("git", {"a": 1}) != call_key("git", {"a": 2})


def test_scratch_remembers_tool_results():
    mem = ScratchMemory()
    key = call_key("read_file", {"path": "app.py"})
    assert mem.seen(key) is None
    result = ToolResult(call_id="c1", ok=True, output="x", duration_ms=1)
    mem.remember(key, result)
    assert mem.seen(key) is result


def test_scratch_notes_are_capped():
    mem = ScratchMemory()
    for i in range(MAX_NOTES + 5):
        mem.note(f"{i}")
    assert len(mem.notes) == MAX_NOTES
    assert mem.notes[-1] == str(MAX_NOTES + 4)
    mem.note("x" * (MAX_NOTE_CHARS + 100))
    assert len(mem.notes[-1]) == MAX_NOTE_CHARS
