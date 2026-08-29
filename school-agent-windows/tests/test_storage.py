import json

from school_agent.storage import atomic_write_json, load_json_self_healing, safe_map


def test_atomic_write_then_load_roundtrip(tmp_path):
    p = tmp_path / "data.json"
    atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2, 3]}


def test_atomic_write_leaves_no_tmp_file_behind(tmp_path):
    p = tmp_path / "data.json"
    atomic_write_json(p, {"x": 1})
    leftovers = [f for f in tmp_path.iterdir() if f.name != "data.json"]
    assert leftovers == []


def test_load_missing_file_returns_default(tmp_path):
    assert load_json_self_healing(tmp_path / "missing.json", default=[]) == []
    assert load_json_self_healing(tmp_path / "missing.json", default={"x": 1}) == {"x": 1}


def test_load_corrupt_file_quarantines_and_returns_default(tmp_path):
    p = tmp_path / "data.json"
    p.write_text("{not valid json", encoding="utf-8")

    result = load_json_self_healing(p, default=[])

    assert result == []
    assert not p.exists()  # moved, not left in place
    quarantined = list(tmp_path.glob("data.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not valid json"


def test_load_valid_file_is_unaffected(tmp_path):
    p = tmp_path / "data.json"
    p.write_text('{"ok": true}', encoding="utf-8")
    assert load_json_self_healing(p, default=None) == {"ok": True}
    assert p.exists()


def test_safe_map_skips_bad_items_keeps_good_ones():
    def fn(x):
        if x == "bad":
            raise ValueError("boom")
        return x.upper()

    result = safe_map(["a", "bad", "b"], fn, on_item_name=lambda x: x)
    assert result == ["A", "B"]
