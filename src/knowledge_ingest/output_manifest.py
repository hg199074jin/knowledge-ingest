"""Cangjie Output Manifest（冻结规格：事务化 + 决定性）。

`target complete --target cangjie` 时：
- 锁外扫描 output_path（本模块），生成决定性 manifest 内容
- 锁内（edit() 事务）校验 → rename tmp → TargetState 登记

决定性保证：skills 按 name 排序、canonical JSON（sort_keys、固定缩进 2、
ensure_ascii=False、\n 结尾）、无 generated_at → 两次生成 byte-identical。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

TOP_LEVEL_FILES = ("DIGEST", "INDEX", "GLOSSARY", "PIPELINE_STATE")
SCHEMA_VERSION = 1


def skill_tree_sha256(skill_dir: Path) -> str:
    """sha256(∑ sorted relative_path + file_bytes)，遍历 skill 目录全部文件。"""
    digest = hashlib.sha256()
    files = sorted(
        (p for p in skill_dir.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(skill_dir).as_posix(),
    )
    for file_path in files:
        rel = file_path.relative_to(skill_dir).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(file_path.read_bytes())
    return digest.hexdigest()


def parse_frontmatter(skill_md: Path) -> dict:
    """解析 SKILL.md 顶部 YAML frontmatter；缺失/损坏返回 {}。"""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            block = "\n".join(lines[1:idx])
            try:
                parsed = yaml.safe_load(block)
            except yaml.YAMLError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _has_artifact(skill_dir: Path, name: str) -> bool:
    """存在性判定：兼容 test_prompts* / test-prompts*（cangjie 实际产物为
    test-prompts.json 与 test-results.md）。"""
    for stem in sorted({name, name.replace("_", "-")}):
        if any(skill_dir.glob(f"{stem}*")):
            return True
    return False


def scan_skills(output_path: Path) -> list[dict]:
    """有 SKILL.md 的子目录 → skill 记录（按 name 排序，决定性）。"""
    skills: list[dict] = []
    if not output_path.is_dir():
        return skills
    for child in output_path.iterdir():
        if not (child.is_dir() and (child / "SKILL.md").is_file()):
            continue
        meta = parse_frontmatter(child / "SKILL.md")
        name = str(meta.get("name") or child.name)
        description = meta.get("description")
        skills.append({
            "name": name,
            "path": str(child),
            "description": (str(description)
                            if description is not None else None),
            "sha256": skill_tree_sha256(child),
            "artifacts": {
                "test_prompts": _has_artifact(child, "test_prompts"),
                "test_results": _has_artifact(child, "test_results"),
            },
        })
    skills.sort(key=lambda s: s["name"])
    return skills


def build_output_manifest(output_path: Path) -> dict:
    output_path = Path(output_path)
    skills = scan_skills(output_path)
    top_level = {name: (output_path / name).exists()
                 for name in TOP_LEVEL_FILES}
    return {
        "schema_version": SCHEMA_VERSION,
        "output_path": str(output_path),
        "skills": skills,
        "top_level_files": top_level,
        "counts": {
            "skills": len(skills),
            "top_level_files_present": sum(1 for v in top_level.values() if v),
            "top_level_files_expected": len(TOP_LEVEL_FILES),
        },
    }


def render_output_manifest(data: dict) -> str:
    """canonical JSON：sort_keys、缩进 2、ensure_ascii=False、\n 结尾；
    无 generated_at（决定性 → 两次生成 byte-identical）。"""
    return json.dumps(
        data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
