"""Acceptance criteria and their tests: the pure rules shared by links, proof and drift."""

from agent_dev.domain import criteria_tests


def test_named_tests_group_by_requirement_and_file():
    named = {
        "REQ-001:AC-01": "tests/test_a.py::test_one",
        "REQ-001:AC-02": " tests/test_a.py::test_two",
        "REQ-001:*0": "tests/test_changed.py",
        "REQ-002:AC-01": "tests/test_b.py",
        "REQ-009:AC-01": "tests/test_other_task.py",
        "no-colon": "tests/test_x.py",
        "REQ-001:AC-03": "",
        "REQ-001:AC-04": 3,
        5: "tests/test_y.py",
    }
    assert criteria_tests.grouped(named, ["REQ-001", "REQ-002"]) == {
        ("REQ-001", "tests/test_a.py"): {"REQ-001:AC-01", "REQ-001:AC-02"},
        ("REQ-001", "tests/test_changed.py"): set(),
        ("REQ-002", "tests/test_b.py"): {"REQ-002:AC-01"},
    }


def test_a_link_is_covered_only_by_a_mapping_of_its_target_and_criteria():
    definition = {
        "verification_mapping": [
            {"requirement_id": "REQ-001", "target": "tests/test_a.py", "criteria_ids": ["REQ-001:AC-01", "REQ-001:AC-02"], "gate": "unit"},
            {"requirement_id": "REQ-001", "target": "tests/test_b.py", "criteria_ids": ["REQ-001:AC-03"], "gate": "e2e"},
        ]
    }
    link = {"requirement_id": "REQ-001", "target_ref": "tests/test_a.py", "criteria_ids": ["REQ-001:AC-01"], "module_id": "app"}
    assert [m["gate"] for m in criteria_tests.mappings(link, definition)] == ["unit"]
    assert criteria_tests.mappings(dict(link, criteria_ids=["REQ-001:AC-03"]), definition) == []
    assert [m["gate"] for m in criteria_tests.mappings(dict(link, criteria_ids=None), definition)] == ["unit"]
    assert criteria_tests.mappings(link, {}) == []
    commands = [
        {"name": "unit", "repository": "app", "command": ["python", "-m", "pytest", "tests/test_a.py"]},
        {"name": "unit", "repository": "other", "command": ["python", "-m", "pytest", "tests/test_a.py"]},
        {"name": "e2e", "repository": "app", "command": ["python", "-m", "pytest", "tests/test_a.py"]},
        {"name": "unit", "repository": "app", "command": ["python", "-m", "pytest"]},
    ]
    assert criteria_tests.registered_commands(link, commands, {"unit"}) == [commands[0]]
    assert criteria_tests.registered_commands(link, commands, set()) == []
