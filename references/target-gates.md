# Target Gates — 确认门映射表

> 原则：gate 名称是 `knowledge-ingest` 总控自定义的 Job 状态标识；
> 原始 Skill（cangjie-skill / personal-capability-distiller）的 Gate 条件本身
> 不得改变。Agent 只做"原样升级问题 → 等真实回复 → 记录真实 decision"。

## 通用规则

1. `gate enter` 必须先落 Manifest（状态 WAITING_USER），之后才向用户展示问题。
2. `gate resolve --decision` 只能来自真实用户回复；没有真实用户消息时**禁止执行**。
3. 用户拒绝（rejected）→ 该 target 记 `skipped`，Job 进入 `COMPLETED` 并在
   errors 中记录 `target_rejected`；报告如实呈现。
4. 全部 gate 事件（enter/resolve）追加进 `manifest.gate_history`（含时间戳）。

## Cangjie（cangjie-skill）

原 Skill 内部没有命名 gate；真实确认点为 3 处（见 runtime-inventory §4）：

| Job gate 名称 | 原 Skill 真实确认点 | 说明 |
|---|---|---|
| `stage0_overview` | 阶段 0：展示 BOOK_OVERVIEW.md（"骨架我理解对了吗？有没有你希望重点突出的方向？"） | 确认后才进入阶段 1 |
| `stage1_5_candidates` | 阶段 1.5：候选清单轻确认（"这 N 个会做成 skill，有想捞回或砍掉的吗？"） | 确认后才进入阶段 2 |
| `stage5_install_location` | 阶段 5：询问安装位置（用户级 / 项目级） | 本机推荐答案：实体库 `~/.agents/skills/`（视图自动同步）；仍须用户真实确认 |

~~compile mode confirmation~~：不存在——原 Skill 无"编译模式"概念。

## Personal（personal-capability-distiller）

原 Skill 的确认门是 workflow-states 命名状态；总控直接沿用其名称：

| Job gate 名称 | 原 Skill 状态 | 触发时机 |
|---|---|---|
| `inventory_reviewed` | 资料清点后确认主领域与档位 | 用户须确认主领域、A/B/C 档（未指定默认 C） |
| `faithful_reconstruction_reviewed` | 保真还原审阅 | 用户核对保真版 |
| `human_material_approved` | 材料定稿 | 用户必须明示"定稿" |
| `skill_simulation_passed` | 候选 Skill 模拟测试通过 | Agent 报告模拟结果 |
| `installation_approved` | 安装确认 | 用户明示授权才安装；绝不自动安装 |

其余命名状态（`intake`、`applied_in_real_task`、`feedback_reviewed`、
`archived`）由原 Skill 自行流转，不需要 Job gate。

## depth 档位

- 用户显式指定 A/B/C → 原样传给原 Skill。
- 未指定 → `depth=null`，原 Skill 按自身默认（实测：**默认 C** 并显式声明）。
- 总控不得替用户改档位。
