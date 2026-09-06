import platform
import sys
from pathlib import Path

import pytest

from knowledge_ingest.config import AppConfig
from knowledge_ingest.doctor import DoctorCheck, has_fail, run_doctor

from .test_config import make_config_dict


@pytest.fixture(autouse=True)
def fake_darwin_env(monkeypatch: pytest.MonkeyPatch):
    """doctor 的平台检查与被测环境解耦：CI (ubuntu) 与本机 (darwin) 都跑同一套断言。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr("knowledge_ingest.doctor.ORICO_ROOT", Path("/"))


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    for sub in ("kp", "media", "docchunk", "media-out", "corpus"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "docchunk" / "pyproject.toml").write_text("", encoding="utf-8")
    skills = tmp_path / "skills"
    for name in (
        "baidu-drive",
        "quarkclouddrive",
        "cangjie-skill",
        "personal-capability-distiller",
    ):
        (skills / name).mkdir(parents=True, exist_ok=True)
        (skills / name / "SKILL.md").write_text("x", encoding="utf-8")
    raw = make_config_dict(tmp_path)
    raw["skill_roots"] = [str(skills)]
    return AppConfig.model_validate(raw)


def test_all_green_has_no_fail(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("knowledge_ingest.doctor._run", lambda *a, **k: (0, "", ""))
    checks = run_doctor(config)
    assert checks, "doctor must produce checks"
    assert not has_fail(checks)
    assert all(c.status in {"PASS", "WARN"} for c in checks)


def test_missing_cloud_skill_is_warn_not_fail(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("knowledge_ingest.doctor._run", lambda *a, **k: (0, "", ""))
    for name in ("baidu-drive", "quarkclouddrive"):
        skill_md = config.skill_roots[0] / name / "SKILL.md"
        skill_md.unlink()
    checks = run_doctor(config)
    cloud = {c.name: c for c in checks}
    assert cloud["skill:baidu-drive"].status == "WARN"
    assert cloud["skill:quarkclouddrive"].status == "WARN"
    assert not has_fail(checks)


def test_missing_core_skill_fails(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("knowledge_ingest.doctor._run", lambda *a, **k: (0, "", ""))
    (config.skill_roots[0] / "cangjie-skill" / "SKILL.md").unlink()
    checks = run_doctor(config)
    by_name = {c.name: c for c in checks}
    assert by_name["skill:cangjie-skill"].status == "FAIL"
    assert has_fail(checks)


def test_missing_media_project_fails(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("knowledge_ingest.doctor._run", lambda *a, **k: (0, "", ""))
    (config.media_project / "pyproject.toml").unlink()
    checks = run_doctor(config)
    by_name = {c.name: c for c in checks}
    assert by_name["media_project"].status == "FAIL"
    assert has_fail(checks)


def test_tool_doctor_failure_fails(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    def fake_run(argv, cwd, timeout=None):
        if "media-transcriber" in argv:
            return 1, "", "boom"
        return 0, "", ""

    monkeypatch.setattr("knowledge_ingest.doctor._run", fake_run)
    checks = run_doctor(config)
    by_name = {c.name: c for c in checks}
    assert by_name["media-transcriber_doctor"].status == "FAIL"
    assert has_fail(checks)


def test_skill_roots_dedupe_views(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """实体库与 symlink 视图指向同一处时，去重后不产生重复 Skill 检查。"""
    monkeypatch.setattr("knowledge_ingest.doctor._run", lambda *a, **k: (0, "", ""))
    store = tmp_path / "store"
    (store / "cangjie-skill").mkdir(parents=True)
    (store / "cangjie-skill" / "SKILL.md").write_text("x", encoding="utf-8")
    raw = make_config_dict(tmp_path)
    raw["skill_roots"] = [str(store), str(store)]
    cfg = AppConfig.model_validate(raw)
    checks = run_doctor(cfg)
    names = [c.name for c in checks if c.name.startswith("skill:")]
    assert len(names) == len(set(names))


def test_doctor_check_is_frozen():
    check = DoctorCheck(name="x", status="PASS", detail="")
    with pytest.raises(Exception):
        check.status = "FAIL"  # type: ignore[misc]
