"""R1.3 unit tests 共享资产构造器（Issue #14 §11）。

单一 factory 供 doctor / shadow-agent / provider loader 三个测试文件复用：
hash、manifest、config.toml、control-plane env 语义只写一次，避免测试自身
与生产单一来源（R1.2 manifest / R1.1 env 契约）漂移。

binary 用 native-style stub（Mach-O magic + 版本行），version_probe 注入；
绝不执行未知二进制、绝不在单测中触网。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from knowledge_ingest.insight.codex_isolation import (
    CONFIG_FILENAME,
    MANIFEST_FILENAME,
    CertificationState,
    CodexIsolationProfile,
    build_certified_model_command,
    build_manifest,
    generate_config,
    write_manifest,
)

MACHO_MAGIC = b"\xcf\xfa\xed\xfe"
AUTH_SECRET = "Bearer test-only-secret-never-echoed"

ENV_REQUIRE_ISOLATION = "KI_INSIGHT_REQUIRE_ISOLATION"
ENV_MODEL_CMD = "KI_INSIGHT_MODEL_CMD"
ENV_ISOLATION_HOME = "KI_INSIGHT_ISOLATION_HOME"
ENV_ISOLATION_CODEX_HOME = "KI_INSIGHT_ISOLATION_CODEX_HOME"
ENV_ISOLATION_WORKSPACE = "KI_INSIGHT_ISOLATION_WORKSPACE"
ENV_ISOLATION_PROFILE_ID = "KI_INSIGHT_ISOLATION_PROFILE_ID"
ENV_CODEX_MANIFEST = "KI_INSIGHT_CODEX_MANIFEST"
ENV_DENY_REAL_HOME = "KI_INSIGHT_DENY_REAL_HOME"


@dataclass(frozen=True)
class CertifiedAssets:
    profile: CodexIsolationProfile
    manifest_path: Path
    real_home: Path
    env: dict[str, str]

    def version_probe(self, _path: Path) -> str:
        return f"codex-cli {self.profile.required_binary_version}"

    def env_with(self, **overrides) -> dict[str, str]:
        env = dict(self.env)
        env.update(overrides)
        return env


def stub_native_binary(path: Path) -> Path:
    path.write_bytes(MACHO_MAGIC + b"\x00" * 16 + b"codex-cli 0.159.2")
    path.chmod(0o755)
    return path


@pytest.fixture
def certified(tmp_path: Path):
    """构造隔离资产 + 对应 control-plane env；默认 full PASS。"""

    def _build(*,
               state: CertificationState = CertificationState.HOSTILE_SMOKE_PASS,
               evidence: str = "evidence/hostile-smoke-2026-10-02.json",
               auth: bool = True,
               config: bool = True,
               manifest: bool = True,
               tamper=None,
               real_home: Path | None = None,
               root: Path | None = None) -> CertifiedAssets:
        base = root or tmp_path
        home = base / "iso-home"
        codex = base / "iso-codex-home"
        workspace = base / "ws"
        for directory in (home, codex, workspace):
            directory.mkdir(parents=True, exist_ok=True)
        real = real_home or (base / "real-home")
        real.mkdir(parents=True, exist_ok=True)
        binary = stub_native_binary(base / "codex-stub")
        profile = CodexIsolationProfile(
            profile_id="r1-3-test",
            binary_path=binary,
            binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            isolated_home=home,
            codex_home=codex,
            workspace=workspace,
            deny_real_home=real,
            certification_state=state,
            hostile_smoke_ref=evidence,
        )
        if config:
            (codex / CONFIG_FILENAME).write_text(generate_config(profile),
                                                 encoding="utf-8")
        if auth:
            auth_path = codex / "auth.json"
            auth_path.write_text(AUTH_SECRET, encoding="utf-8")
            auth_path.chmod(0o600)
        manifest_path = codex / MANIFEST_FILENAME
        if manifest:
            document = build_manifest(profile)
            if tamper is not None:
                document = tamper(dict(document))
            write_manifest(manifest_path, document)
        env = {
            ENV_REQUIRE_ISOLATION: "1",
            ENV_ISOLATION_HOME: str(home),
            ENV_ISOLATION_CODEX_HOME: str(codex),
            ENV_ISOLATION_WORKSPACE: str(workspace),
            ENV_ISOLATION_PROFILE_ID: profile.profile_id,
            ENV_CODEX_MANIFEST: str(manifest_path),
            ENV_DENY_REAL_HOME: str(real),
            ENV_MODEL_CMD: build_certified_model_command(profile),
        }
        return CertifiedAssets(profile=profile, manifest_path=manifest_path,
                              real_home=real, env=env)

    return _build
