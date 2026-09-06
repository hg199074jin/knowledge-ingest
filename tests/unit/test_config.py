from pathlib import Path

from knowledge_ingest.config import AppConfig


def make_config_dict(tmp_path: Path) -> dict:
    return {
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus"),
        "skill_roots": ["~/.agents/skills", "~/.zcode/skills", "~/.claude/skills"],
        "skills": {
            "baidu": "baidu-drive",
            "quark": "quarkclouddrive",
            "cangjie": "cangjie-skill",
            "personal_distiller": "personal-capability-distiller",
        },
        "processing": {
            "media_device": "auto",
            "media_timestamp": "10m",
            "require_orico": False,
        },
    }


def test_expand_user_and_paths(tmp_path: Path):
    cfg = AppConfig.model_validate(make_config_dict(tmp_path))
    assert cfg.pipeline_root.is_absolute()
    assert cfg.skill_roots[0].is_absolute()
    assert cfg.skill_roots[0] == Path.home() / ".agents" / "skills"
    assert cfg.media_output_root == (tmp_path / "media-out").resolve()


def test_processing_defaults(tmp_path: Path):
    raw = make_config_dict(tmp_path)
    del raw["processing"]
    cfg = AppConfig.model_validate(raw)
    assert cfg.processing.media_device == "auto"
    assert cfg.processing.media_timestamp == "10m"
    assert cfg.processing.require_orico is True


def test_load_from_yaml(tmp_path: Path):
    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(make_config_dict(tmp_path), allow_unicode=True),
        encoding="utf-8",
    )
    cfg = AppConfig.load(config_path)
    assert cfg.docchunk_project == (tmp_path / "docchunk").resolve()
    assert cfg.skills.cangjie == "cangjie-skill"
