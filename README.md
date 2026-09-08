# knowledge-ingest

[![Tests](https://github.com/hg199074jin/knowledge-ingest/actions/workflows/tests.yml/badge.svg)](https://github.com/hg199074jin/knowledge-ingest/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/hg199074jin/knowledge-ingest)](https://github.com/hg199074jin/knowledge-ingest/releases)

[简体中文](README.zh-CN.md)

A multi-source knowledge ingestion and capability-distillation pipeline for
macOS (Apple Silicon). It routes material from **Baidu Netdisk, Quark Drive,
or local paths** through the existing toolchain:

```text
media-transcriber (video/audio → Markdown)
docchunk (long documents → verifiable Corpus)
cangjie-skill (methodology → executable Agent Skills)
personal-capability-distiller (capability cards / SOPs / prompts → Obsidian)
```

`knowledge-ingest` itself does **no** OCR, ASR, chunking, or distillation —
it provides orchestration, durable state, resumable breakpoints, dedup,
caching, human confirmation gates, and final reporting.

```text
Baidu / Quark / local material → verifiable Corpus → Agent Skills + capability assets
```

## How it works

```text
Cloud skills (baidu-drive / quarkclouddrive) download into the job's source dir
  → Source Handoff (canonical source.json)
  → file-type router (documents / media / unsupported)
  → media transcription (cached by content hash + tool revision)
  → Document Set (symlinks + provenance map, never merged into one blob)
  → docchunk split → verify PASS (hard gate)
  → cangjie-skill → personal-capability-distiller (serial, human gates)
  → final report (secrets-redacted)
```

Key guarantees:

- **`docchunk verify` FAIL blocks distillation** — structurally enforced by the
  explicit state machine, not by convention.
- **Human confirmation gates are never fabricated.** Gate resolutions can only
  come from a real user reply, and every gate event is recorded.
- **No credentials anywhere.** Cloud tokens/cookies are never read; free-text
  prompts and reports pass through a redactor; event logs are structured and
  sanitized.
- **Dedup across sources.** Same content from Baidu and Quark reuses the same
  verified corpus (handoff fingerprint + tool HEAD + config in the cache key).
- **Resumable.** Every stage transition is atomically persisted; `next` always
  returns the exact remaining action, never a re-run of completed stages.

## Install

```bash
git clone git@github.com:hg199074jin/knowledge-ingest.git
cd knowledge-ingest
uv sync
uv run knowledge-ingest doctor --config ./config.example.yaml
```

Optional CLI wrapper (`~/.local/bin/knowledge-ingest`):

```zsh
#!/bin/zsh
cd /path/to/knowledge-ingest || exit 1
exec uv run knowledge-ingest "$@"
```

## Quick start

```bash
knowledge-ingest job create --provider local --source /path/book.pdf --target cangjie
knowledge-ingest source init JOB --local-path DIR [--remote-path ...]  # builds handoff
knowledge-ingest source register JOB --handoff .../handoff/source.json
knowledge-ingest route JOB
knowledge-ingest preprocess JOB       # long task: background; per-file progress in status
knowledge-ingest status JOB / resume  # N/M progress; resume = next step for every job
knowledge-ingest next JOB --json
knowledge-ingest distill prepare JOB --target cangjie   # scaffolds distill workspace
knowledge-ingest target start JOB --target cangjie
knowledge-ingest gate enter|resolve JOB --target cangjie --name GATE [--decision ...]
knowledge-ingest target complete JOB --target cangjie --output-path PATH
knowledge-ingest report JOB
```

As an Agent Skill, the natural-language entry points are: *"turn this Baidu
Netdisk PDF into a skill"*, *"learn this Quark course and distill it"*, *"continue
the ingestion task"*, *"where is that job at?"* — see [SKILL.md](SKILL.md).

## Repository layout

| Path | Purpose |
|---|---|
| `src/knowledge_ingest/` | Python CLI (stdlib + PyYAML + pydantic, managed by uv) |
| `SKILL.md` | Agent Skill entry point |
| `references/` | Architecture, routing, cloud sources, target gates, recovery, handoff contracts |
| `schemas/` | Source/Target handoff contract examples |
| `docs/` | Design v1.1, implementation plan v1.1, runtime inventory, acceptance record |
| `tests/` | Unit + integration tests (TDD throughout) |

## v0.2.0 — evolved from the first production run

The first end-to-end job (11 GB / 32 videos / two overnight reboots) drove five upgrades:

- **Per-file progress persistence**: `status` shows real N/M during hours-long ASR;
  every file emits a `media_transcribed` event (survives crashes).
- **`resume` command**: one call reports the next action for every job and
  `--exec` continues the first resumable one — reboot survival is built-in.
- **Shipped watchdog**: `ops/ki-resume.sh` + LaunchAgent plist (generic, no hardcoded
  job ids) — the hand-built script from the production run, productized.
- **`job amend --add-target`**: guarded by a pidfile lock so manifest edits are
  refused (not silently lost) while preprocess holds its in-memory copy.
- **`source init` / `distill prepare`**: handoff JSON and distill-workspace
  scaffolding are generated, not hand-authored.

## Docs

- [Design document v1.1](docs/knowledge-ingest-design-v1.md)
- [Implementation plan v1.1](docs/knowledge-ingest-implementation-v1.md)
- [Acceptance record](docs/acceptance-v1.md)

## Related projects

- [docchunk](https://github.com/hg199074jin/docchunk) — verifiable long-document corpora
- [media-transcriber](https://github.com/hg199074jin/media-transcriber) — local ASR to structured Markdown

## License

[MIT](LICENSE)
