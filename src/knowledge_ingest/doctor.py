"""Environment doctor: verifies the machine baseline before any job runs."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from knowledge_ingest.config import AppConfig

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

ORICO_ROOT = Path("/Volumes/ORICO")

# 缺失即阻断的下游 Skill；云端 Skill 缺失只降级为 WARN
CORE_SKILL_KEYS = ("cangjie", "personal_distiller")
CLOUD_SKILL_KEYS = ("baidu", "quark")

_RUN_TIMEOUT = 300


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str


def _run(argv: list[str], cwd: Path, timeout: int = _RUN_TIMEOUT) -> tuple[int, str, str]:
    proc = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _check(name: str, ok: bool, pass_detail: str, fail_detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=PASS if ok else FAIL,
                       detail=pass_detail if ok else fail_detail)


def _unique_skill_roots(config: AppConfig) -> list[Path]:
    seen: set[Path] = set()
    roots: list[Path] = []
    for root in config.skill_roots:
        resolved = Path(root).expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return roots


def _find_skill(config: AppConfig, skill_name: str) -> Path | None:
    for root in _unique_skill_roots(config):
        candidate = root / skill_name / "SKILL.md"
        if candidate.is_file():
            return candidate
    return None


def _project_check(name: str, project: Path) -> DoctorCheck:
    marker = project / "pyproject.toml"
    ok = marker.is_file()
    return _check(name, ok, str(project), f"missing {marker}")


def _tool_doctor_check(name: str, argv: list[str], cwd: Path,
                       project_ok: bool) -> DoctorCheck:
    if not project_ok:
        return DoctorCheck(name=name, status=FAIL, detail="skipped: project missing")
    try:
        returncode, stdout, stderr = _run(argv, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(name=name, status=FAIL, detail=f"error: {exc}")
    if returncode == 0:
        return DoctorCheck(name=name, status=PASS, detail=str(cwd))
    detail = (stderr or stdout).strip().splitlines()
    return DoctorCheck(name=name, status=FAIL,
                       detail=detail[-1] if detail else f"exit {returncode}")


def run_doctor(config: AppConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []

    checks.append(_check(
        "platform",
        sys.platform == "darwin",
        f"darwin {platform.mac_ver()[0]}",
        f"expected darwin, got {sys.platform}",
    ))
    checks.append(_check(
        "arch",
        platform.machine() == "arm64",
        "arm64",
        f"expected arm64, got {platform.machine()}",
    ))
    checks.append(_check(
        "python_version",
        sys.version_info[:2] == (3, 12),
        ".".join(map(str, sys.version_info[:3])),
        f"expected 3.12.x, got {platform.python_version()}",
    ))
    checks.append(_check(
        "uv",
        shutil.which("uv") is not None,
        shutil.which("uv") or "",
        "uv not found on PATH",
    ))
    checks.append(_check(
        "orico_mounted",
        ORICO_ROOT.is_dir(),
        str(ORICO_ROOT),
        f"{ORICO_ROOT} not mounted",
    ))

    try:
        config.pipeline_root.mkdir(parents=True, exist_ok=True)
        writable = os.access(config.pipeline_root, os.W_OK)
        detail = str(config.pipeline_root)
    except OSError as exc:
        writable, detail = False, f"error: {exc}"
    checks.append(_check("pipeline_root_writable", writable, detail,
                         f"{config.pipeline_root} not writable"))

    media_ok = (config.media_project / "pyproject.toml").is_file()
    docchunk_ok = (config.docchunk_project / "pyproject.toml").is_file()
    checks.append(_project_check("media_project", config.media_project))
    checks.append(_project_check("docchunk_project", config.docchunk_project))
    checks.append(_tool_doctor_check(
        "media-transcriber_doctor",
        ["uv", "run", "media-transcriber", "doctor"],
        config.media_project,
        media_ok,
    ))
    checks.append(_tool_doctor_check(
        "docchunk_doctor",
        ["uv", "run", "docchunk", "doctor"],
        config.docchunk_project,
        docchunk_ok,
    ))

    resolved_roots = ", ".join(str(r) for r in _unique_skill_roots(config))
    for key in (*CORE_SKILL_KEYS, *CLOUD_SKILL_KEYS):
        skill_name = getattr(config.skills, key)
        found = _find_skill(config, skill_name)
        if found is not None:
            checks.append(DoctorCheck(
                name=f"skill:{skill_name}", status=PASS, detail=str(found)))
        elif key in CLOUD_SKILL_KEYS:
            checks.append(DoctorCheck(
                name=f"skill:{skill_name}", status=WARN,
                detail=f"not found in skill roots ({resolved_roots}); "
                       "cloud tasks will stay BLOCKED until installed"))
        else:
            checks.append(DoctorCheck(
                name=f"skill:{skill_name}", status=FAIL,
                detail=f"not found in skill roots ({resolved_roots})"))

    return checks


def has_fail(checks: list[DoctorCheck]) -> bool:
    return any(c.status == FAIL for c in checks)
