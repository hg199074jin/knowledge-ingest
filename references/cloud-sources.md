# Cloud Sources — 百度 / 夸克 Source Adapter（Agent 层）

> 云端文件的选择、下载全部由官方 Skill 完成；Python 核心不写死任何云盘 CLI 语法，
> 不读取任何 Token 配置。总控只消费"下载完成后的 Source Handoff"。

## 本机安装状态（2026-09-06）

| Skill | 版本 | 位置 | 登录 |
|---|---|---|---|
| `baidu-drive` | v1.7.5（bdpan CLI 3.8.7） | `~/.agents/skills/baidu-drive` | **待用户扫码**（`scripts/login.sh`） |
| `quarkclouddrive` | 1.0.17-ea0ddf3（`scripts/quark-drive.cjs`） | `~/.agents/skills/quarkclouddrive` | 首次使用按 Skill 流程验证 |

两者均已通过 `skill-sync` 同步视图；缺失时 doctor 记 WARN，云端任务保持 BLOCKED。

## 通用流程

```text
1. knowledge-ingest job create --provider baidu|quark --source <云盘路径或链接> --target ...
2. Agent 调用对应 Skill：搜索 / 浏览 → 选择文件 → 下载到
   /Volumes/ORICO/KnowledgePipeline/jobs/<job-id>/source/
3. 校验：本地目标存在、大小稳定、Skill 明确"下载完成"
4. 生成 canonical source.json（schemas/source-handoff.example.json）
5. knowledge-ingest source register JOB --handoff .../source.json
6. 之后进入本地流水线（route → preprocess → distill）
```

**下载完成门**：中断/未完成维持 DOWNLOADING 或 FAILED，绝不 route 半文件。

## 百度（baidu-drive）

### V1 范围（必须写进 SKILL.md）

官方 `bdpan` 的普通文件路径均相对于 `/apps/bdpan/`（"我的应用数据/bdpan"）。
因此 V1 只支持两种模式：

```text
A. app_path：文件已在“我的应用数据/bdpan”内
B. share_link：用户提供百度分享链接，baidu-drive 转存到应用目录后下载
```

**路径规范（V1.1 修订）**：handoff 的 `remote.path` 必须是应用目录全路径
（`/apps/bdpan/<相对路径>`）；bdpan CLI 返回的普通文件路径均以 `/apps/bdpan/`
为前缀，照抄即可。裸相对路径（如 `审计/课程.pdf`）无法与普通网盘目录区分，
一律按越界处理。

用户要求搜索普通百度网盘其它目录时：

```text
status=BLOCKED
reason=baidu_scope_limited
```

并告知用户两种恢复方式：把文件移到"我的应用数据/bdpan"，或提供该资料的分享链接。
（CLI 在 `source register` 阶段强制执行该校验。）

### 安全边界

- 禁止读取 `~/.config/bdpan/config.json` 或任何 bdpan 认证文件；认证全部交给 Skill。
- 只做 search / list / download / stat；不 delete / move / rename / overwrite / upload，
  除非用户明确要求并确认。
- Job Manifest 只存 remote path/name/size，不存凭据。

## 夸克（quarkclouddrive）

### 集成原则

- 不把 Quark CLI 内部命令写死进 Python；CLI 由 Skill 自己安装/升级
  （`node scripts/quark-drive.cjs`）。
- **环境标记（2026-09-06 实测）**：CLI 按环境变量识别宿主 Agent
  （`CLAUDECODE=1` / `CODEX_ENV=1` / `AI_AGENT` 等），裸终端调用直接报
  `code -104 无法识别当前 Agent 环境`。本机约定：**所有 quark CLI 调用统一带
  `CLAUDECODE=1` 前缀**，授权与 Search/Browse Artifact 均落在
  `~/.agents/skills/quarkclouddrive/claudecode/` 配置桶；换标记会读到空授权。
- 搜索文件夹：Search dir；枚举直接子项：Browse all；
  **Search/Browse 完整结果以 Artifact 为准**，最多 5 条的预览绝不当全部候选。
- Session/OAuth 参数由 Skill 自维护；总控 Manifest 可存非敏感的 remote fid/路径/名称，
  用户报告默认不展示内部 ID。

### 下载要求

- 断点续传由 Skill 处理；总控只认"Skill 明确完成 + 本地文件存在 + 大小稳定"。
- 一个目录（2 视频 + 1 PDF 等）必须完整下载后一次性形成 mixed Document Set。

## fixtures

- `tests/fixtures/baidu-source-handoff.json` / `tests/fixtures/quark-source-handoff.json`
  供单元测试与文档示例使用（仅示例数据，无真实凭据）。
- 真实下载 smoke test 前置：对应 Skill 已完成登录授权（用户配合）。
