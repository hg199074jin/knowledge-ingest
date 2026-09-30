"""V3 M3: injectable model port — stdin→stdout JSON 契约（fail-closed）。

冻结 env 契约（实施方案 Task 3；A4 修订：模型名/档位只属于部署配置，
代码只认环境变量）：
- KI_INSIGHT_MODEL_CMD：基础命令（shlex 切分；stdin 进、stdout 出）；
- KI_INSIGHT_MODEL_TIMEOUT：秒，默认 180；
- KI_INSIGHT_MODEL_CWD：可选（中立工作目录，防加载项目上下文）；
- stage 覆盖（缺失回落基础命令）：KI_INSIGHT_CANDIDATE_CMD /
  KI_INSIGHT_VALUE_CMD / KI_INSIGHT_RETRIEVAL_PLAN_CMD /
  KI_INSIGHT_RETRIEVAL_SELECT_CMD / KI_INSIGHT_EVIDENCE_CMD /
  KI_INSIGHT_THINK_CMD / KI_INSIGHT_CRITIC_CMD。

行为契约：
- stdin = {"schema_version":1,"stage":...,"payload":{...}}；
- stdout 必须含一个完整 JSON 对象（容忍前后杂讯；自实现抽取，
  不 import cli 私有函数）；
- 非零退出/超时/空输出/坏 JSON → 类型化错误；绝不伪造判定；
- 错误消息 ≤200 字符，绝不包含 stdin payload（隐私 §30）。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

STAGE_ENV_VARS = {
    "candidate_filter": "KI_INSIGHT_CANDIDATE_CMD",
    "deep_value_gate": "KI_INSIGHT_VALUE_CMD",
    "retrieval_query_plan": "KI_INSIGHT_RETRIEVAL_PLAN_CMD",
    "retrieval_select": "KI_INSIGHT_RETRIEVAL_SELECT_CMD",
    "evidence_extract": "KI_INSIGHT_EVIDENCE_CMD",
    "thinking": "KI_INSIGHT_THINK_CMD",
    "critic": "KI_INSIGHT_CRITIC_CMD",
}
DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_ERROR_CHARS = 200


class ModelPortError(RuntimeError):
    """模型端口错误基类（全部可恢复：绝不映射为 REJECT/无价值）。"""


class ModelNotConfiguredError(ModelPortError):
    """该 stage 未配置命令（ neither stage override nor base）。"""


class ModelIsolationConfigurationError(ModelPortError):
    """R1.1：strict isolation 配置缺失/非法（construction-time fail-closed）。

    消息只含环境变量名与简短原因，绝不含 payload 或 secret value。
    """


@dataclass(frozen=True)
class IsolationProfile:
    """R1.1：generic 子进程隔离边界（不含任何 provider/Codex 专有语义）。

    职责仅限：给定宿主 ambient env，构造经 allowlist 过滤 + 显式覆盖的
    child env；strict 时指定 workspace cwd。Codex 专有校验（版本/config/
    network/fs deny）不在本类，归 provider-specific adapter（R1.2）。
    """

    profile_id: str
    isolated_home: Path
    workspace_cwd: Path
    child_env_allowlist: tuple[str, ...] = ("TMPDIR", "LANG", "LC_CTYPE")
    child_env_overrides: Mapping[str, str] = field(default_factory=dict)
    require_strict: bool = True

    def build_child_env(self, ambient: Mapping[str, str]) -> dict[str, str]:
        """allowlist 放行 + 显式覆盖；绝不整批继承 ambient。"""
        child = {k: ambient[k] for k in self.child_env_allowlist
                 if k in ambient}
        child.update(self.child_env_overrides)
        return child


class ModelCommandFailedError(ModelPortError):
    """子进程非零退出。"""


class ModelTimeoutError(ModelPortError):
    """子进程超时。"""


class ModelEmptyOutputError(ModelPortError):
    """stdout 为空。"""


class ModelBadOutputError(ModelPortError):
    """stdout 无完整可解析 JSON 对象。"""


class InsightModelPort(Protocol):
    def run(self, stage: str, payload: dict) -> dict: ...


def _sanitize(message: str) -> str:
    message = " ".join(str(message).split())
    return message[:_MAX_ERROR_CHARS]


def extract_json_object(raw: str) -> dict | None:
    """从混有杂讯的 stdout 中取出第一个完整 JSON 对象。

    与生产验证过的 telegram 通道语义一致（首遇即取）；扫描时尊重
    字符串与转义，避免被消息体内的花括号截断。
    """
    if not raw:
        return None
    start = None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(raw):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidate = raw[start:index + 1]
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    start, depth = None, 0     # 假起始，继续扫描
                    continue
                return parsed if isinstance(parsed, dict) else None
    return None


class CommandInsightModelPort:
    """InsightModelPort 的命令行实现（不绑定任何 provider）。"""

    def __init__(self, env: dict | None = None, *,
                 timeout: float | None = None,
                 isolation: IsolationProfile | None = None):
        # control-plane 配置源：仅用于解析 KI_INSIGHT_*；
        # 子进程 env 由 isolation 另行构造，二者绝不含混。
        self.env = dict(env) if env is not None else dict(os.environ)
        self.timeout = timeout
        self.isolation = isolation
        if isolation is not None and isolation.require_strict:
            overrides = [name for name in STAGE_ENV_VARS.values()
                         if self.env.get(name, "").strip()]
            if overrides:
                raise ModelIsolationConfigurationError(
                    "strict isolation forbids stage overrides: "
                    + ",".join(overrides))

    def resolve_command(self, stage: str) -> str:
        override = self.env.get(STAGE_ENV_VARS[stage], "").strip()
        if override:
            return override
        base = self.env.get("KI_INSIGHT_MODEL_CMD", "").strip()
        if base:
            return base
        raise ModelNotConfiguredError(
            f"no model command configured for stage {stage!r} "
            f"(set {STAGE_ENV_VARS[stage]} or KI_INSIGHT_MODEL_CMD)")

    def _timeout_seconds(self) -> float:
        if self.timeout is not None:
            return self.timeout
        raw = self.env.get("KI_INSIGHT_MODEL_TIMEOUT", "").strip()
        if not raw:
            return DEFAULT_TIMEOUT_SECONDS
        try:
            return float(raw)
        except ValueError as exc:
            raise ModelPortError(
                f"invalid KI_INSIGHT_MODEL_TIMEOUT: {raw!r}") from exc

    def run(self, stage: str, payload: dict) -> dict:
        argv = shlex.split(self.resolve_command(stage))
        request = json.dumps({"schema_version": 1, "stage": stage,
                              "payload": payload}, ensure_ascii=False)
        strict = self.isolation is not None and self.isolation.require_strict
        if strict:
            # R1.1：strict 下 workspace cwd 优先，child env 由 profile 构造，
            # 绝不继承宿主 ambient env。
            child_env = self.isolation.build_child_env(os.environ)
            cwd = str(self.isolation.workspace_cwd)
        else:
            child_env = None
            cwd = self.env.get("KI_INSIGHT_MODEL_CWD", "").strip() or None
        try:
            proc = subprocess.run(
                argv, input=request, capture_output=True, text=True,
                timeout=self._timeout_seconds(), check=False, cwd=cwd,
                env=child_env)
        except subprocess.TimeoutExpired as exc:
            raise ModelTimeoutError(
                f"model command timed out after "
                f"{self._timeout_seconds():g}s") from exc
        except OSError as exc:
            raise ModelCommandFailedError(_sanitize(exc)) from exc
        if proc.returncode != 0:
            raise ModelCommandFailedError(_sanitize(
                f"model command exit {proc.returncode}: {proc.stderr!s}"))
        if not proc.stdout.strip():
            raise ModelEmptyOutputError("model command produced no stdout")
        parsed = extract_json_object(proc.stdout)
        if parsed is None:
            raise ModelBadOutputError(
                "model stdout contains no complete JSON object")
        return parsed


# ---------- R1.1：construction-time strict factory（A5） ----------

_ISOLATION_HOME_ENV = "KI_INSIGHT_ISOLATION_HOME"
_ISOLATION_CODEX_HOME_ENV = "KI_INSIGHT_ISOLATION_CODEX_HOME"
_ISOLATION_WORKSPACE_ENV = "KI_INSIGHT_ISOLATION_WORKSPACE"
_ISOLATION_PROFILE_ID_ENV = "KI_INSIGHT_ISOLATION_PROFILE_ID"
_ISOLATION_CHILD_PATH_ENV = "KI_INSIGHT_ISOLATION_CHILD_PATH"


def build_insight_model_port_from_env(
        env: Mapping[str, str] | None = None) -> CommandInsightModelPort:
    """R1.1 薄工厂：读 control-plane env → 校验/构造 strict profile → 返回 port。

    仅负责 generic 子进程边界；Codex 专有校验（版本/config/network/fs deny）
    归 R1.2 provider-specific adapter。strict 校验在任何模型调用前 fail-closed。
    """
    env = dict(os.environ) if env is None else dict(env)
    require = env.get("KI_INSIGHT_REQUIRE_ISOLATION", "").strip() == "1"
    if not require:
        # 非 strict：开发/测试兼容（旧 ambient 语义 + stage override）
        return CommandInsightModelPort(env=env)

    def _required(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise ModelIsolationConfigurationError(
                f"strict isolation requires {name}")
        return value

    isolated_home = Path(_required(_ISOLATION_HOME_ENV))
    codex_home = _required(_ISOLATION_CODEX_HOME_ENV)
    workspace = Path(_required(_ISOLATION_WORKSPACE_ENV))
    if not isolated_home.is_dir():
        raise ModelIsolationConfigurationError(
            f"strict isolation HOME directory does not exist: "
            f"{_ISOLATION_HOME_ENV}")
    if not workspace.is_dir():
        raise ModelIsolationConfigurationError(
            f"strict isolation workspace directory does not exist: "
            f"{_ISOLATION_WORKSPACE_ENV}")
    overrides = [name for name in STAGE_ENV_VARS.values()
                 if env.get(name, "").strip()]
    if overrides:
        raise ModelIsolationConfigurationError(
            "strict isolation forbids stage overrides: " + ",".join(overrides))
    command = env.get("KI_INSIGHT_MODEL_CMD", "").strip()
    if not command:
        raise ModelIsolationConfigurationError(
            "strict isolation requires KI_INSIGHT_MODEL_CMD")
    if not os.path.isabs(shlex.split(command)[0]):
        raise ModelIsolationConfigurationError(
            "strict isolation requires an absolute model command path")
    isolation = IsolationProfile(
        profile_id=(env.get(_ISOLATION_PROFILE_ID_ENV, "").strip()
                    or "strict-default"),
        isolated_home=isolated_home,
        workspace_cwd=workspace,
        child_env_allowlist=("TMPDIR", "LANG", "LC_CTYPE"),
        child_env_overrides={
            "HOME": str(isolated_home),
            "CODEX_HOME": codex_home,
            "PATH": env.get(_ISOLATION_CHILD_PATH_ENV, "").strip()
                    or "/usr/bin:/bin",
        },
        require_strict=True)
    return CommandInsightModelPort(env=env, isolation=isolation)
