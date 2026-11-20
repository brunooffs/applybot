import shutil
from pathlib import Path

import pytest

from applybot import cli, config, doctor
from applybot.config import DEFAULTS, default_home, load_config


def test_home_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYBOT_HOME", str(tmp_path))
    assert default_home() == tmp_path


def test_home_falls_back_to_dot_applybot(monkeypatch, tmp_path):
    monkeypatch.delenv("APPLYBOT_HOME", raising=False)
    monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path / "no-checkout")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_home() == tmp_path / ".applybot"


def test_set_up_checkout_keeps_working(monkeypatch, tmp_path):
    monkeypatch.delenv("APPLYBOT_HOME", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "facts.yaml").write_text("identity: {}\n")
    monkeypatch.setattr(config, "DEFAULT_ROOT", tmp_path)
    assert default_home() == tmp_path


def test_missing_criteria_and_sources_use_packaged_defaults(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.criteria["thresholds"]["good"] and "prefilter" in cfg.criteria
    assert "remotive" in cfg.sources


def test_init_creates_everything_and_never_overwrites(tmp_path, capsys):
    args = type("A", (), {"home": str(tmp_path)})()
    cli.cmd_init(args)
    for rel in ("config/facts.yaml", "config/profile.yaml", "config/settings.yaml", "cvs/cvs.yaml",
                "config/criteria.yaml", "config/sources.yaml"):
        assert (tmp_path / rel).exists(), rel
    assert (tmp_path / "data").is_dir() and (tmp_path / "outbox").is_dir()
    (tmp_path / "config" / "facts.yaml").write_text("mine: true\n")
    cli.cmd_init(args)
    assert (tmp_path / "config" / "facts.yaml").read_text() == "mine: true\n"
    assert "kept" in capsys.readouterr().out


def test_packaged_defaults_are_complete():
    for f in ("config/facts.yaml.example", "config/profile.yaml.example", "config/settings.yaml.example",
              "config/criteria.yaml", "config/sources.yaml", "cvs/cvs.yaml.example", "models.yaml"):
        assert (DEFAULTS / f).exists(), f


TIERS = [{"min_gb": 0, "model": None}, {"min_gb": 8, "model": "a"}, {"min_gb": 16, "model": "b"}, {"min_gb": 32, "model": "c"}]


@pytest.mark.parametrize("gb,model", [(4, None), (8, "a"), (15.9, "a"), (16, "b"), (36, "c")])
def test_recommend_picks_highest_tier_that_fits(gb, model):
    assert doctor.recommend(gb, TIERS)["model"] == model


def test_recommend_unknown_memory():
    assert doctor.recommend(None, TIERS) is None


def test_packaged_model_table_loads():
    assert doctor.recommend(16)["model"]


def test_has_model_matches_latest_tag():
    assert doctor.has_model(["llama3:latest"], "llama3")
    assert doctor.has_model(["qwen2.5:14b"], "qwen2.5:14b")
    assert not doctor.has_model(["qwen2.5:7b"], "qwen2.5:14b")


def test_open_items_counts_markers():
    assert doctor.open_items("a: 1  # TODO fix\nb: CONFIRM this\nc: todo-list") == 2


def test_doctor_reports_missing_files_and_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "ollama_models", lambda host: None)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    items = doctor.checks(load_config(tmp_path))
    text = "\n".join(m for _, m in items)
    assert "config/facts.yaml is missing" in text and "applybot init" in text


def test_doctor_flags_missing_model_and_key(tmp_path, monkeypatch):
    cli.cmd_init(type("A", (), {"home": str(tmp_path)})())
    monkeypatch.setattr(doctor, "ollama_models", lambda host: ["other:1b"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    items = doctor.checks(load_config(tmp_path))
    text = "\n".join(m for lv, m in items if lv == "fail")
    assert "ollama pull" in text and "ANTHROPIC_API_KEY" in text


def test_doctor_ok_when_everything_present(tmp_path, monkeypatch):
    cli.cmd_init(type("A", (), {"home": str(tmp_path)})())
    cfg = load_config(tmp_path)
    model = cfg.settings["llm"]["ollama"]["model"]
    monkeypatch.setattr(doctor, "ollama_models", lambda host: [model])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert not [1 for lv, _ in doctor.checks(cfg) if lv == "fail"]


def test_hardware_has_a_system_and_never_crashes():
    hw = doctor.hardware()
    assert hw["system"] and "kind" in hw
