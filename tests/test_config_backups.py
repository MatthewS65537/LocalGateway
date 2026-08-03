from __future__ import annotations

import json

import pytest

from localgateway import config as config_mod


@pytest.fixture()
def iso_config(tmp_path):
    """Isolated config module state for a temp config file."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"server": {}, "providers": [], "models": []}))

    original_path = config_mod._config_path
    original_cfg = config_mod._config
    original_mtime = config_mod._config_mtime
    original_recovered = config_mod._config_recovered

    config_mod.set_config_path(str(path))
    config_mod._config = None
    config_mod._config_mtime = -1.0
    config_mod._config_recovered = None
    yield path

    config_mod.set_config_path(original_path)
    config_mod._config = original_cfg
    config_mod._config_mtime = original_mtime
    config_mod._config_recovered = original_recovered


def _save_marker(path: Path, marker: str):
    cfg = config_mod.load_config()
    cfg.models = [config_mod.ModelConfig(id=f"model-{marker}")]
    config_mod.save_config(cfg)


def _read_marker(path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["models"][0]["id"]


def test_backup_rotation_keeps_five_generations(iso_config):
    path = iso_config
    for i in range(7):
        _save_marker(path, f"gen{i}")

    # The live file has the newest marker.
    assert _read_marker(path) == "model-gen6"

    # Slots 0..4 exist (BACKUP_KEEP = 5).
    for i in range(5):
        bak = config_mod._backup_at(i)
        assert bak.exists(), f"missing backup slot {i}"
    # Slot 5 was rotated out.
    assert not config_mod._backup_at(5).exists()

    # .bak is the previous save, .bak.1 the one before, etc.
    assert _read_marker(config_mod._backup_at(0)) == "model-gen5"
    assert _read_marker(config_mod._backup_at(1)) == "model-gen4"
    assert _read_marker(config_mod._backup_at(4)) == "model-gen1"


def test_load_recovers_from_newest_backup_on_corrupt_primary(iso_config):
    path = iso_config
    # Two saves: .bak now holds "good", the primary holds "newer".
    _save_marker(path, "good")
    _save_marker(path, "newer")
    path.write_text("{ this is not valid json")

    config_mod._config = None
    config_mod._config_mtime = -1.0

    cfg = config_mod.load_config()
    assert cfg.models and cfg.models[0].id == "model-good"
    assert config_mod.config_recovered_from() is not None
    assert str(config_mod._backup_at(0)) in config_mod.config_recovered_from()


def test_clean_save_clears_recovery_flag(iso_config):
    path = iso_config
    _save_marker(path, "good")
    path.write_text("{ nope")
    config_mod._config = None
    config_mod._config_mtime = -1.0
    config_mod.load_config()
    assert config_mod.config_recovered_from() is not None

    _save_marker(path, "fixed")
    assert config_mod.config_recovered_from() is None
    assert _read_marker(path) == "model-fixed"


def test_recovery_when_all_backups_corrupt(iso_config):
    path = iso_config
    _save_marker(path, "good")
    for i in range(5):
        config_mod._backup_at(i).write_text("corrupt{")
    path.write_text("corrupt{")
    config_mod._config = None
    config_mod._config_mtime = -1.0

    cfg = config_mod.load_config()
    assert cfg.models == []
    assert config_mod.config_recovered_from() is None


# ---------- config.example.json parity ----------

def test_config_example_validates_and_round_trips():
    from pathlib import Path as _Path

    example_path = _Path(__file__).resolve().parent.parent / "config.example.json"
    if not example_path.exists():
        pytest.skip("config.example.json not present")

    raw = json.loads(example_path.read_text(encoding="utf-8"))
    cfg = config_mod.GatewayConfig.model_validate(raw)
    assert cfg.server.port == 3456

    # Round-trip: dump -> re-validate stays stable.
    again = config_mod.GatewayConfig.model_validate(cfg.model_dump())
    assert again.model_dump() == cfg.model_dump()
