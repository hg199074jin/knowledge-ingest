"""Generic target registry (frozen spec 2): targets become data, not code paths.

每个 TargetRuntime 声明调度、门控、脚手架、handoff 所需的一切；
新增 target = register() 一条数据，不改编排代码。
family_router 是最小验证实例：不做真实构建/分发/宿主执行/sampling/
cost 执行器/600s runner。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TargetRuntime:
    name: str
    display_name: str
    # next_action 输出的调用键名（向后兼容 v0.2 的 invoke_cangjie/
    # invoke_personal_distiller）
    invoke_key: str
    # AppConfig.skills 里的键名
    skill_config_key: str
    # 默认依赖（规格 2：personal depends_on=[]；family_router ← cangjie）
    depends_on: tuple[str, ...] = ()
    # 规格 10：可预授权 gate 白名单；不在名单内 = live-only
    preauthorizable_gates: tuple[str, ...] = ()
    # distill prepare 在 workspace 根下额外创建的子目录（cangjie=books/）
    workspace_subdirs: tuple[str, ...] = ()
    # target complete 时在 handoff/ 下生成并登记的 output manifest 文件名
    output_manifest: str | None = None
    # distill prepare 追加到 target-<name>.yaml 的钩子；
    # 签名 handoff_extra(*, manifest, job_dir) -> list[str]
    handoff_extra: Callable[..., list[str]] | None = None


REGISTRY: dict[str, TargetRuntime] = {}


def register(runtime: TargetRuntime) -> TargetRuntime:
    """注册（或覆盖）一个 target runtime；测试可用它注册假 target。"""
    REGISTRY[runtime.name] = runtime
    return runtime


def get(name: str) -> TargetRuntime:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown target: {name!r} "
            f"(registered: {', '.join(REGISTRY) or '<none>'})"
        ) from None


def target_choices() -> list[str]:
    """argparse choices 用：当前注册的全部 target 名（声明顺序）。"""
    return list(REGISTRY)


# ---- 内置 handoff extra 钩子（冻结规格：cangjie/personal/family_router） ----


def _cangjie_handoff_extra(**_) -> list[str]:
    return ["first_pilot: false"]


def _personal_handoff_extra(**_) -> list[str]:
    return [
        "depth: null",
        "obsidian_vault: /Volumes/ORICO/Obsidian/Skill_Library",
    ]


def _family_router_handoff_extra(*, manifest, job_dir, **_) -> list[str]:
    """input_manifest（cangjie COMPLETED 时才指向其 output manifest）+ 预算默认。"""
    cangjie = manifest.targets.get("cangjie")
    input_manifest = (
        Path(job_dir) / "handoff" / "cangjie-output-manifest.json"
        if cangjie is not None and cangjie.status == "COMPLETED" else None
    )
    lines = [f"input_manifest: {input_manifest if input_manifest else 'null'}"]
    lines += [
        "budget:",
        "  mode: balanced",
        "  max_external_calls: 20",
        "  max_retries_per_case: 1",
        "  breaker:",
        "    consecutive_empty: 3",
        "    consecutive_rate_limit: 2",
    ]
    return lines


register(TargetRuntime(
    name="cangjie",
    display_name="Cangjie",
    invoke_key="invoke_cangjie",
    skill_config_key="cangjie",
    depends_on=(),
    preauthorizable_gates=("stage5_install_location",),
    workspace_subdirs=("books",),
    output_manifest="cangjie-output-manifest.json",
    handoff_extra=_cangjie_handoff_extra,
))
register(TargetRuntime(
    name="personal",
    display_name="Personal",
    invoke_key="invoke_personal_distiller",
    skill_config_key="personal_distiller",
    depends_on=(),
    preauthorizable_gates=(),  # personal 全部门（含 installation_approved）= live
    handoff_extra=_personal_handoff_extra,
))
register(TargetRuntime(
    name="family_router",
    display_name="Family Router",
    invoke_key="invoke_family_router",
    skill_config_key="family_router",
    depends_on=("cangjie",),
    preauthorizable_gates=("cost_budget_confirmed",),
    handoff_extra=_family_router_handoff_extra,
))
