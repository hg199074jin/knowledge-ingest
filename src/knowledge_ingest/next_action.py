"""Next-action protocol and human confirmation gates.

`next` returns the action an agent should take; it never fabricates user
confirmation. Gates are recorded in the manifest before questions are shown.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from knowledge_ingest.models import JobManifest, TargetState
from knowledge_ingest.state_machine import InvalidTransition, transition_to


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _target_state(manifest: JobManifest, target: str) -> TargetState:
    if target not in manifest.request.targets:
        raise ValueError(f"target not requested by this job: {target}")
    if target == "cangjie":
        return manifest.cangjie
    if target == "personal":
        return manifest.personal
    raise ValueError(f"unknown target: {target}")


def _pending_targets(manifest: JobManifest) -> list[str]:
    pending = []
    for target in manifest.request.targets:
        state = _target_state(manifest, target)
        if state.status in {"pending", "running", "waiting_user"}:
            pending.append(target)
    return pending


def next_action(manifest: JobManifest) -> dict:
    status = manifest.status
    action: dict = {
        "job_id": manifest.job_id,
        "status": status,
    }

    if status == "CREATED":
        action["next_action"] = "await_source"
        action["detail"] = ("register source handoff after the cloud skill "
                            "finishes downloading (knowledge-ingest source register)")
    elif status in {"DISCOVERING", "DOWNLOADING"}:
        action["next_action"] = "wait_download"
    elif status == "DOWNLOADED":
        action["next_action"] = "route"
    elif status == "ROUTING":
        action["next_action"] = "preprocess"
    elif status in {"TRANSCRIBING", "DOCCHUNKING", "VERIFYING"}:
        action["next_action"] = "preprocess_resume"
    elif status == "CORPUS_READY":
        pending = _pending_targets(manifest)
        action["targets_remaining"] = pending
        action["corpus_path"] = (str(manifest.docchunk.corpus_path)
                                 if manifest.docchunk.corpus_path else None)
        if pending and pending[0] == "cangjie":
            action["next_action"] = "invoke_cangjie"
        elif pending:
            action["next_action"] = "invoke_personal_distiller"
        else:
            action["next_action"] = "complete_job"
    elif status == "DISTILLING_CANGJIE":
        action["next_action"] = "invoke_cangjie"
        action["corpus_path"] = (str(manifest.docchunk.corpus_path)
                                 if manifest.docchunk.corpus_path else None)
    elif status == "DISTILLING_PERSONAL":
        action["next_action"] = "invoke_personal_distiller"
        action["corpus_path"] = (str(manifest.docchunk.corpus_path)
                                 if manifest.docchunk.corpus_path else None)
    elif status == "WAITING_USER":
        target = ("cangjie" if manifest.cangjie.waiting_for
                  else "personal" if manifest.personal.waiting_for else None)
        gate = (manifest.cangjie.waiting_for if target == "cangjie"
                else manifest.personal.waiting_for if target == "personal"
                else None)
        action["next_action"] = "ask_user"
        action["target"] = target
        action["gate"] = gate
    elif status == "BLOCKED":
        action["next_action"] = "resolve_blocked"
        action["reason"] = (manifest.errors[-1].get("reason")
                            if manifest.errors else "unknown")
    elif status == "FAILED":
        action["next_action"] = "inspect_failure"
    elif status == "COMPLETED":
        action["next_action"] = "report"
        action["cangjie_output"] = (str(manifest.cangjie.output_path)
                                    if manifest.cangjie.output_path else None)
        action["personal_output"] = (str(manifest.personal.output_path)
                                     if manifest.personal.output_path else None)
    return action


def gate_enter(manifest: JobManifest, target: str, name: str) -> None:
    state = _target_state(manifest, target)
    if manifest.status not in {"DISTILLING_CANGJIE", "DISTILLING_PERSONAL"}:
        raise InvalidTransition(
            f"gate enter requires an active distillation state, got {manifest.status}")
    state.status = "waiting_user"
    state.waiting_for = name
    manifest.gate_history.append({
        "ts": _now_iso(), "target": target, "gate": name, "action": "enter",
    })
    transition_to(manifest, "WAITING_USER")


def gate_resolve(
    manifest: JobManifest, target: str, name: str, decision: str
) -> None:
    if decision not in {"confirmed", "rejected"}:
        raise ValueError(f"decision must be confirmed|rejected, got {decision}")
    state = _target_state(manifest, target)
    if manifest.status != "WAITING_USER" or state.waiting_for != name:
        raise ValueError(
            f"no open gate {name!r} for target {target!r} "
            f"(status={manifest.status}, waiting_for={state.waiting_for!r})")
    manifest.gate_history.append({
        "ts": _now_iso(), "target": target, "gate": name,
        "action": "resolve", "decision": decision,
    })
    state.waiting_for = None
    if decision == "confirmed":
        state.status = "running"
        distill_status = ("DISTILLING_CANGJIE" if target == "cangjie"
                          else "DISTILLING_PERSONAL")
        transition_to(manifest, distill_status)
    else:
        state.status = "skipped"
        manifest.errors.append({
            "reason": "target_rejected", "target": target, "gate": name,
        })
        transition_to(manifest, "COMPLETED")


def target_complete(manifest: JobManifest, target: str, output_path: Path) -> None:
    state = _target_state(manifest, target)
    state.status = "success"
    state.output_path = Path(output_path)
    if target == "cangjie" and "personal" in manifest.request.targets \
            and manifest.personal.status in {"pending", "running"}:
        transition_to(manifest, "DISTILLING_PERSONAL")
    else:
        transition_to(manifest, "COMPLETED")
