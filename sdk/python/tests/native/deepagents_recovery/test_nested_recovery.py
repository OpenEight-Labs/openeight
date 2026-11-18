"""A grandchild is a separate native delegation level, not a direct child alias."""

import json

from test_recovery import invoke


def test_native_two_delegation_levels_keep_namespaces_and_effect_across_restart(
    tmp_path,
):
    invoke("nested_probe.py", tmp_path, "start")
    waiting = json.loads((tmp_path / "start.json").read_text())
    assert len(waiting["interrupts"]) == 1 and waiting["next"]
    assert not (tmp_path / "effects.jsonl").exists()
    invoke("nested_probe.py", tmp_path, "resume")
    finished = json.loads((tmp_path / "resume.json").read_text())
    assert finished["next"] == [] and finished["interrupts"] == []
    assert finished["output"] == ["done"]
    assert (tmp_path / "effects.jsonl").read_text().splitlines() == [
        '{"value": "once"}'
    ]
    calls = [
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    for label, depth in (("root", 1), ("worker", 2), ("worker/leaf", 3)):
        selected = [
            item
            for item in calls
            if item["label"] == label and item["id"] == "same-call"
        ]
        assert len(selected) == 2
        assert len({item["namespace"] for item in selected}) == 1
        assert len(selected[0]["namespace"].split("|")) == depth
        assert len(selected[0]["checkpoint_map"]) == depth
