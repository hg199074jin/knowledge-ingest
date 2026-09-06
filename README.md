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
knowledge-ingest source register JOB --handoff source.json   # after cloud download
knowledge-ingest route JOB
knowledge-ingest preprocess JOB       # long task: run in background, poll status
knowledge-ingest status JOB
knowledge-ingest next JOB --json
knowledge-ingest target start JOB --target cangjie
knowledge-ingest gate enter JOB --target cangjie --name GATE
knowledge-ingest gate resolve JOB --target cangjie --name GATE --decision confirmed
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

## Docs

- [Design document v1.1](docs/knowledge-ingest-design-v1.md)
- [Implementation plan v1.1](docs/knowledge-ingest-implementation-v1.md)
- [Acceptance record](docs/acceptance-v1.md)

## Related projects

- [docchunk](https://github.com/hg199074jin/docchunk) — verifiable long-document corpora
- [media-transcriber](https://github.com/hg199074jin/media-transcriber) — local ASR to structured Markdown

## License

[MIT](LICENSE)
