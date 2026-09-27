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
                 timeout: float | None = None):
        self.env = dict(env) if env is not None else dict(os.environ)
        self.timeout = timeout

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
        cwd = self.env.get("KI_INSIGHT_MODEL_CWD", "").strip() or None
        try:
            proc = subprocess.run(
                argv, input=request, capture_output=True, text=True,
                timeout=self._timeout_seconds(), check=False, cwd=cwd)
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
