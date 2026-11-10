
from agent_dev.core import io as core_io


def test_write_json_is_atomic_and_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "state.json"
    core_io.write_json(path, {"a": 1})

    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert core_io.read_json(path) == {"a": 1}


def test_write_json_keeps_backup_and_read_json_recovers_from_corruption(tmp_path):
    path = tmp_path / "state.json"
    core_io.write_json(path, {"a": 1})
    core_io.write_json(path, {"a": 2})

    backup_path = path.with_suffix(path.suffix + ".bak")
    assert core_io.read_json(backup_path) == {"a": 1}

    path.write_text("{not valid json", encoding="utf-8")
    assert core_io.read_json(path) == {"a": 1}










