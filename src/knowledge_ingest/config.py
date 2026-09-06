"""Application configuration loaded from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator
from pydantic.fields import Field as pydantic_field


def _default_processing() -> "ProcessingConfig":
    return ProcessingConfig()



class SkillNames(BaseModel):
    baidu: str
    quark: str
    cangjie: str
    personal_distiller: str


class ProcessingConfig(BaseModel):
    media_device: str = "auto"
    media_timestamp: str = "10m"
    require_orico: bool = True


class AppConfig(BaseModel):
    pipeline_root: Path
    media_project: Path
    docchunk_project: Path
    media_output_root: Path
    docchunk_corpus_root: Path
    skill_roots: list[Path]
    skills: SkillNames
    processing: ProcessingConfig = pydantic_field(default_factory=_default_processing)

    @field_validator(
        "pipeline_root",
        "media_project",
        "docchunk_project",
        "media_output_root",
        "docchunk_corpus_root",
        mode="before",
    )
    @classmethod
    def expand_path(cls, value):
        return Path(value).expanduser().resolve()

    @field_validator("skill_roots", mode="before")
    @classmethod
    def expand_roots(cls, values):
        return [Path(v).expanduser().resolve() for v in values]

    @classmethod
    def load(cls, path: Path | None) -> "AppConfig":
        if path is None:
            path = _default_config_path()
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _default_config_path() -> Path:
    for candidate in (
        Path("config.yaml"),
        Path(__file__).resolve().parents[2] / "config.example.yaml",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("config not found: pass --config PATH explicitly")
