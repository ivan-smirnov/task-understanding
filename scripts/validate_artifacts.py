#!/usr/bin/env python3
"""Validate task-understanding live files and immutable snapshot transitions.

The checker is deliberately domain-specific.  It validates the stable parts of
the public artifact format and byte-level snapshot invariants; semantic quality
remains a manual evaluation concern.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable


REQUIRED_GATE_HEADINGS = (
    "Проблема",
    "Задача и цель",
    "Аудитория или пользователь",
    "Ожидаемое воздействие",
    "Конечный результат",
    "Границы работы",
    "Критерии приёмки",
    "Кто принимает итог",
)
REQUIRED_SUPPORT_HEADINGS = (
    "Состояние",
    "Что требует уточнения",
    "Открытые вопросы",
    "Решения по вопросам",
)
STATE_FIELDS = (
    "Текущая редакция",
    "Ожидается подтверждение редакции",
    "Действующая согласованная версия",
)
ALLOWED_LIVE_STATUSES = {
    "диагностика",
    "нужны уточнения",
    "нужно решение по допущению",
    "нужен пробный материал и реакция",
    "нужна внешняя проверка",
    "готово к согласованию",
    "согласовано",
}

H2_RE = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*$")
H3_RE = re.compile(r"(?m)^###[ \t]+(.+?)[ \t]*$")
QUESTION_ID_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё0-9])Q-(\d+)(?![A-Za-zА-Яа-яЁё0-9])")
QUESTION_DEFINITION_RE = re.compile(
    r"(?m)^\s*(?:(?:[-*+]|\d+\.)\s+|\|\s*)`?(Q-(\d+))`?(?=\s|[—–|:.;,-]|$)"
)
STATE_MARKER_RE = re.compile(
    r"(?:\[\?\]|\b(?:подтверждено|допущение|противоречие)\s*[:—–-]|"
    r"\bпредлагается\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LiveState:
    current_revision: str | None
    awaiting_revision: str | None
    baseline_snapshot: str | None
    open_questions: frozenset[str]
    closed_questions: frozenset[str]
    cancelled_questions: frozenset[str]

    @property
    def used_questions(self) -> frozenset[str]:
        return self.open_questions | self.closed_questions | self.cancelled_questions


@dataclass(frozen=True)
class LiveValidation:
    errors: tuple[str, ...]
    state: LiveState


def issue(code: str, message: str) -> str:
    return f"[{code}] {message}"


def normalize_russian(value: str) -> str:
    return value.casefold().replace("ё", "е")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def markdown_h2_sections(text: str) -> tuple[dict[str, str], dict[str, int]]:
    """Return H2 bodies and occurrence counts without treating H3 as H2."""
    matches: list[re.Match[str]] = []
    offset = 0
    fence: tuple[str, int] | None = None
    for line in text.splitlines(keepends=True):
        fence_match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            offset += len(line)
            continue
        if fence is None:
            match = re.match(r"^##[ \t]+(.+?)[ \t]*(?:\r?\n)?$", line)
            if match:
                absolute = H2_RE.search(text, offset, offset + len(line))
                if absolute is not None:
                    matches.append(absolute)
        offset += len(line)
    sections: dict[str, str] = {}
    counts: dict[str, int] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        counts[heading] = counts.get(heading, 0) + 1
        sections.setdefault(heading, text[start:end])
    return sections, counts


def before_h3(section: str, heading: str) -> str:
    for match in H3_RE.finditer(section):
        if match.group(1).strip() == heading:
            return section[: match.start()]
    return section


def h3_body(section: str, heading: str) -> str:
    matches = list(H3_RE.finditer(section))
    for index, match in enumerate(matches):
        if match.group(1).strip() != heading:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        return section[match.end() : end]
    return ""


def question_occurrences(text: str) -> list[str]:
    return [f"Q-{match.group(1)}" for match in QUESTION_ID_RE.finditer(text)]


def valid_numbered_id(value: str, prefix: str) -> bool:
    match = re.fullmatch(rf"{re.escape(prefix)}-(\d{{3,}})", value)
    return match is not None and int(match.group(1)) >= 1


def validate_question_spelling(text: str, *, context: str) -> list[str]:
    errors: list[str] = []
    for match in QUESTION_ID_RE.finditer(text):
        digits = match.group(1)
        if len(digits) < 3 or int(digits) < 1:
            errors.append(
                issue(
                    "live.question-id.format",
                    f"{context} uses Q-{digits}; question IDs need at least three digits and a positive number",
                )
            )
    return errors


def canonical_question_ids(text: str, *, context: str) -> tuple[set[str], list[str]]:
    errors = validate_question_spelling(text, context=context)
    occurrences = [match.group(1) for match in QUESTION_DEFINITION_RE.finditer(text)]
    seen: set[str] = set()
    for question_id in occurrences:
        if question_id in seen:
            errors.append(
                issue(
                    "live.question-id.duplicate",
                    f"{context} defines {question_id} more than once",
                )
            )
        seen.add(question_id)
    return seen, errors


def parse_state_fields(state_section: str) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    values: dict[str, str] = {}
    for field in STATE_FIELDS:
        pattern = re.compile(rf"(?m)^\s*-\s+{re.escape(field)}:\s*(.+?)\s*$")
        matches = pattern.findall(state_section)
        if len(matches) != 1:
            errors.append(
                issue(
                    "live.state-field.count",
                    f"section 'Состояние' must contain exactly one '{field}' field; found {len(matches)}",
                )
            )
            continue
        values[field] = matches[0].strip().strip("`")
    return values, errors


def snapshot_name_pattern(live_path: Path) -> re.Pattern[str]:
    return re.compile(
        rf"^{re.escape(live_path.stem)} — согласовано "
        r"(\d{4}-\d{2}-\d{2})(?: (\d+))?\.md$"
    )


def is_valid_snapshot_name(name: str, live_path: Path) -> bool:
    match = snapshot_name_pattern(live_path).fullmatch(name)
    if match is None:
        return False
    try:
        valid_date = date.fromisoformat(match.group(1)).isoformat() == match.group(1)
    except ValueError:
        return False
    suffix = match.group(2)
    return valid_date and (
        suffix is None or (int(suffix) >= 2 and str(int(suffix)) == suffix)
    )


def snapshot_hashes(project_root: Path, live_relative: Path) -> dict[str, str]:
    live_path = project_root / live_relative
    if not live_path.parent.is_dir():
        return {}
    snapshots: dict[str, str] = {}
    for candidate in sorted(live_path.parent.glob("*.md")):
        if candidate.is_file() and not candidate.is_symlink() and is_valid_snapshot_name(candidate.name, live_relative):
            snapshots[candidate.relative_to(project_root).as_posix()] = sha256_file(candidate)
    return snapshots


def _empty_state() -> LiveState:
    return LiveState(None, None, None, frozenset(), frozenset(), frozenset())


def validate_live_file(
    live_path: Path,
    *,
    project_root: Path | None = None,
    legacy: bool = False,
) -> LiveValidation:
    errors: list[str] = []
    try:
        text = live_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LiveValidation((issue("live.file.missing", f"missing live file: {live_path}"),), _empty_state())
    except UnicodeDecodeError:
        return LiveValidation((issue("live.file.utf8", f"live file is not UTF-8: {live_path}"),), _empty_state())

    if not text.strip():
        return LiveValidation((issue("live.file.empty", f"live file is empty: {live_path}"),), _empty_state())

    if live_path.is_symlink():
        errors.append(issue("live.file.symlink", f"live file must not be a symlink: {live_path}"))

    sections, counts = markdown_h2_sections(text)
    if legacy:
        missing_gate = [heading for heading in REQUIRED_GATE_HEADINGS if counts.get(heading, 0) != 1]
        if not re.search(r"(?m)^#[ \t]+\S", text) or missing_gate:
            errors.append(
                issue(
                    "live.legacy.structure",
                    "legacy fixture must still be a complete Markdown document with all gate areas; "
                    f"missing or duplicated: {', '.join(missing_gate) or 'top-level title'}",
                )
            )
        return LiveValidation(tuple(errors), _empty_state())

    for heading in REQUIRED_SUPPORT_HEADINGS + REQUIRED_GATE_HEADINGS:
        count = counts.get(heading, 0)
        if count != 1:
            errors.append(
                issue(
                    "live.section.required",
                    f"{live_path} must contain exactly one H2 '{heading}'; found {count}",
                )
            )

    for heading in REQUIRED_GATE_HEADINGS:
        body = sections.get(heading, "")
        if body and not STATE_MARKER_RE.search(body):
            errors.append(
                issue(
                    "live.fact-state.missing",
                    f"H2 '{heading}' must distinguish confirmed, proposed, assumed, conflicting, or unknown content",
                )
            )

    state_values, state_errors = parse_state_fields(sections.get("Состояние", ""))
    errors.extend(state_errors)
    current = state_values.get("Текущая редакция")
    awaiting = state_values.get("Ожидается подтверждение редакции")
    baseline = state_values.get("Действующая согласованная версия")

    if current is not None and not valid_numbered_id(current, "R"):
        errors.append(issue("live.revision.current", f"invalid current revision: {current!r}"))
    if awaiting is not None and awaiting != "нет" and not valid_numbered_id(awaiting, "R"):
        errors.append(issue("live.revision.awaiting", f"invalid awaited revision: {awaiting!r}"))
    if awaiting not in {None, "нет"} and current is not None and awaiting != current:
        errors.append(
            issue(
                "live.revision.mismatch",
                f"awaited revision {awaiting!r} must equal current revision {current!r}",
            )
        )

    if baseline is not None and baseline != "нет":
        baseline_path = Path(baseline)
        if baseline_path.name != baseline or baseline in {".", ".."} or "/" in baseline or "\\" in baseline:
            errors.append(
                issue(
                    "live.baseline.basename",
                    "the agreed baseline must be a snapshot basename, not a path",
                )
            )
        elif not is_valid_snapshot_name(baseline, live_path):
            errors.append(
                issue(
                    "live.baseline.theme",
                    f"baseline {baseline!r} is not a valid snapshot name for {live_path.name!r}",
                )
            )
        elif project_root is not None:
            candidate = live_path.parent / baseline
            if candidate.is_symlink():
                errors.append(issue("live.baseline.symlink", f"baseline snapshot must not be a symlink: {baseline}"))
            elif not candidate.is_file():
                errors.append(
                    issue(
                        "live.baseline.missing",
                        f"baseline snapshot does not exist beside the live file: {baseline}",
                    )
                )
            else:
                try:
                    candidate.resolve().relative_to(live_path.parent.resolve())
                except ValueError:
                    errors.append(issue("live.baseline.escape", f"baseline snapshot escapes the live-file folder: {baseline}"))

    open_section = sections.get("Открытые вопросы", "")
    current_heading_count = sum(
        1
        for match in H3_RE.finditer(open_section)
        if match.group(1).strip() == "Вопросы заказчику сейчас"
    )
    if current_heading_count != 1:
        errors.append(
            issue(
                "live.current-questions.section",
                "H2 'Открытые вопросы' must contain exactly one H3 'Вопросы заказчику сейчас'",
            )
        )
    canonical_open = before_h3(open_section, "Вопросы заказчику сейчас")
    open_ids, open_errors = canonical_question_ids(canonical_open, context="Открытые вопросы")
    errors.extend(open_errors)

    decisions = sections.get("Решения по вопросам", "")
    registry_ids, registry_errors = canonical_question_ids(decisions, context="Решения по вопросам")
    errors.extend(registry_errors)
    closed_ids: set[str] = set()
    cancelled_ids: set[str] = set()
    for line in decisions.splitlines():
        ids = [match.group(1) for match in QUESTION_DEFINITION_RE.finditer(line)]
        if not ids:
            continue
        normalized = normalize_russian(line)
        if "закрыт" in normalized:
            closed_ids.update(ids)
        elif "отменен" in normalized:
            cancelled_ids.update(ids)
        else:
            closed_ids.update(ids)

    overlap = open_ids & registry_ids
    if overlap:
        errors.append(
            issue(
                "live.question-id.collision",
                "question IDs cannot be both open and resolved: " + ", ".join(sorted(overlap)),
            )
        )

    map_text = sections.get("Что требует уточнения", "")
    errors.extend(validate_question_spelling(map_text, context="Что требует уточнения"))
    map_ids = set(question_occurrences(map_text))
    unknown_map_ids = map_ids - open_ids
    missing_map_ids = open_ids - map_ids
    if unknown_map_ids:
        errors.append(
            issue(
                "live.question-map.unknown",
                "problem map references non-open IDs: " + ", ".join(sorted(unknown_map_ids)),
            )
        )
    if missing_map_ids:
        errors.append(
            issue(
                "live.question-map.missing",
                "open questions missing from the problem map: " + ", ".join(sorted(missing_map_ids)),
            )
        )

    current_block = h3_body(open_section, "Вопросы заказчику сейчас")
    errors.extend(validate_question_spelling(current_block, context="Вопросы заказчику сейчас"))
    current_ids = set(question_occurrences(current_block))
    unknown_current_ids = current_ids - open_ids
    if unknown_current_ids:
        errors.append(
            issue(
                "live.current-questions.unknown",
                "current customer questions reference non-open IDs: "
                + ", ".join(sorted(unknown_current_ids)),
            )
        )

    status_matches = re.findall(r"(?m)^\s*-\s+Статус:\s*(.+?)\s*$", sections.get("Состояние", ""))
    if len(status_matches) != 1:
        errors.append(
            issue(
                "live.status.count",
                f"section 'Состояние' must contain exactly one status; found {len(status_matches)}",
            )
        )
        status = ""
    else:
        status = normalize_russian(status_matches[0].strip("`"))
        if status not in ALLOWED_LIVE_STATUSES:
            errors.append(issue("live.status.value", f"unsupported live-file status: {status_matches[0]!r}"))
    gate_text = "\n".join(sections.get(heading, "") for heading in REQUIRED_GATE_HEADINGS)
    current_task_text = "\n".join(
        body
        for heading, body in sections.items()
        if heading
        not in {
            "Состояние",
            "Изменения относительно последней согласованной версии",
            "Что требует уточнения",
            "Решения по вопросам",
            "Решения по противоречиям",
            "Открытые вопросы",
            "Подтверждение",
        }
    )
    if status in {"готово к согласованию", "согласовано"}:
        if "[?]" in gate_text:
            errors.append(issue("live.gate.unknown", "a ready or agreed live file cannot contain unknown gate facts"))
        if re.search(
            r"(?:\[предлагается\]|\bпредлагается\b)",
            current_task_text,
            re.IGNORECASE,
        ):
            errors.append(
                issue(
                    "live.gate.proposed",
                    "proposed gate facts cannot make a live file ready or agreed",
                )
            )
        if re.search(r"(?:\[противоречие\]|\bпротиворечие\s*[:—–-])", current_task_text, re.IGNORECASE):
            errors.append(
                issue(
                    "live.gate.contradiction",
                    "an unresolved contradiction cannot make a live file ready or agreed",
                )
            )
        if open_ids:
            errors.append(issue("live.gate.open-questions", "a ready or agreed live file cannot retain open questions"))
    if status == "готово к согласованию" and current is not None and awaiting != current:
        errors.append(
            issue(
                "live.gate.awaiting",
                "a ready live file must await confirmation of its current revision",
            )
        )
    if status not in {"", "готово к согласованию", "согласовано"} and awaiting not in {None, "нет"}:
        errors.append(
            issue(
                "live.revision.awaiting-status",
                "only a ready live file may await confirmation of a revision",
            )
        )
    if status == "согласовано":
        if awaiting != "нет":
            errors.append(issue("live.agreed.awaiting", "an agreed live file must not await confirmation"))
        if baseline in {None, "нет"}:
            errors.append(issue("live.agreed.baseline", "an agreed live file must name its snapshot baseline"))

    state = LiveState(
        current,
        awaiting,
        baseline,
        frozenset(open_ids),
        frozenset(closed_ids),
        frozenset(cancelled_ids),
    )
    return LiveValidation(tuple(errors), state)


def build_record(project_root: Path, live_relative: Path) -> tuple[dict[str, object], list[str]]:
    project_root = project_root.resolve()
    live_relative = Path(live_relative)
    if live_relative.is_absolute() or ".." in live_relative.parts:
        return {}, [issue("record.live-path", "live_file must be a safe project-relative path")]
    live_path = project_root / live_relative
    try:
        live_path.parent.resolve().relative_to(project_root)
    except ValueError:
        return {}, [issue("record.live-path", "live_file parent escapes the project root")]
    validation = validate_live_file(live_path, project_root=project_root)
    errors = list(validation.errors)
    if live_path.parent.is_dir():
        for candidate in live_path.parent.glob("*.md"):
            if candidate.is_symlink() and is_valid_snapshot_name(candidate.name, live_relative):
                errors.append(
                    issue(
                        "snapshot.symlink",
                        f"same-theme snapshots must not be symlinks: {candidate.name}",
                    )
                )
    if errors:
        return {}, errors
    state = validation.state
    record: dict[str, object] = {
        "schema_version": "1.0",
        "live_file": live_relative.as_posix(),
        "live_sha256": sha256_file(live_path),
        "state": {
            "current_revision": state.current_revision,
            "awaiting_revision": state.awaiting_revision,
            "baseline_snapshot": state.baseline_snapshot,
            "open_questions": sorted(state.open_questions),
            "closed_questions": sorted(state.closed_questions),
            "cancelled_questions": sorted(state.cancelled_questions),
        },
        "snapshots": snapshot_hashes(project_root, live_relative),
    }
    return record, []


def load_record(path: Path) -> tuple[dict[str, object], list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, [issue("record.missing", f"missing before-state record: {path}")]
    except UnicodeDecodeError:
        return {}, [issue("record.utf8", f"before-state record is not UTF-8: {path}")]
    except json.JSONDecodeError as error:
        return {}, [issue("record.json", f"invalid record JSON at {path}:{error.lineno}:{error.colno}")]
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        return {}, [issue("record.schema", "before-state record must use schema_version 1.0")]
    return value, []


def compare_record(
    before: dict[str, object],
    project_root: Path,
    live_relative: Path,
    *,
    expected_new: Iterable[str] | None = None,
    expect_live: str = "any",
    snapshot_equals_live: Iterable[str] = (),
) -> list[str]:
    current, errors = build_record(project_root, live_relative)
    if errors:
        return errors

    before_live = before.get("live_file")
    if before_live != Path(live_relative).as_posix():
        errors.append(
            issue(
                "record.live-file",
                f"record belongs to {before_live!r}, not {Path(live_relative).as_posix()!r}",
            )
        )
        return errors

    before_hash = before.get("live_sha256")
    current_hash = current.get("live_sha256")
    if expect_live == "changed" and before_hash == current_hash:
        errors.append(issue("compare.live.unchanged", "the live file was expected to change"))
    if expect_live == "unchanged" and before_hash != current_hash:
        errors.append(issue("compare.live.changed", "the live file was expected to remain byte-identical"))

    before_snapshots = before.get("snapshots")
    current_snapshots = current.get("snapshots")
    if not isinstance(before_snapshots, dict) or not isinstance(current_snapshots, dict):
        errors.append(issue("record.snapshots", "snapshot hashes must be JSON objects"))
        return errors
    for name, old_hash in before_snapshots.items():
        if name not in current_snapshots:
            errors.append(issue("snapshot.deleted", f"previous snapshot was deleted: {name}"))
        elif current_snapshots[name] != old_hash:
            errors.append(issue("snapshot.mutated", f"previous snapshot changed: {name}"))

    new_names = set(current_snapshots) - set(before_snapshots)
    if expected_new is not None:
        expected_set = {Path(name).as_posix() for name in expected_new}
        if new_names != expected_set:
            errors.append(
                issue(
                    "snapshot.new-set",
                    "unexpected new snapshot set; expected "
                    + repr(sorted(expected_set))
                    + ", found "
                    + repr(sorted(new_names)),
                )
            )

    live_path = project_root / live_relative
    live_hash = sha256_file(live_path)
    for raw_name in snapshot_equals_live:
        name = Path(raw_name).as_posix()
        digest = current_snapshots.get(name)
        if digest is None:
            errors.append(issue("snapshot.equals-live.missing", f"snapshot not found: {name}"))
        elif digest != live_hash:
            errors.append(issue("snapshot.equals-live.content", f"snapshot differs from live file: {name}"))

    before_state = before.get("state", {})
    current_state = current.get("state", {})
    if isinstance(before_state, dict) and isinstance(current_state, dict):
        before_used = set(before_state.get("open_questions", [])) | set(before_state.get("closed_questions", [])) | set(
            before_state.get("cancelled_questions", [])
        )
        current_used = set(current_state.get("open_questions", [])) | set(current_state.get("closed_questions", [])) | set(
            current_state.get("cancelled_questions", [])
        )
        lost = before_used - current_used
        if lost:
            errors.append(
                issue(
                    "compare.question-history.lost",
                    "question history lost IDs: " + ", ".join(sorted(lost)),
                )
            )
        previous_numbers = [int(value[2:]) for value in before_used if valid_numbered_id(value, "Q")]
        max_previous = max(previous_numbers, default=0)
        reused = {
            value
            for value in current_used - before_used
            if valid_numbered_id(value, "Q") and int(value[2:]) <= max_previous
        }
        if reused:
            errors.append(
                issue(
                    "compare.question-id.reused",
                    "new questions must continue after the greatest prior ID: " + ", ".join(sorted(reused)),
                )
            )
        reopened = (set(before_state.get("closed_questions", [])) | set(before_state.get("cancelled_questions", []))) & set(
            current_state.get("open_questions", [])
        )
        if reopened:
            errors.append(
                issue(
                    "compare.question-id.reopened",
                    "resolved question IDs cannot be reused as open questions: " + ", ".join(sorted(reopened)),
                )
            )

        before_revision = before_state.get("current_revision")
        current_revision = current_state.get("current_revision")
        if isinstance(before_revision, str) and isinstance(current_revision, str):
            if int(current_revision[2:]) < int(before_revision[2:]):
                errors.append(
                    issue(
                        "compare.revision.regressed",
                        f"revision regressed from {before_revision} to {current_revision}",
                    )
                )

        before_baseline = before_state.get("baseline_snapshot")
        current_baseline = current_state.get("baseline_snapshot")
        if before_baseline not in {None, "нет"} and current_baseline in {None, "нет"}:
            errors.append(
                issue(
                    "compare.baseline.lost",
                    f"agreed baseline {before_baseline!r} disappeared from the live file",
                )
            )
        if current_baseline not in {None, "нет", before_baseline}:
            baseline_relative = (Path(live_relative).parent / str(current_baseline)).as_posix()
            if baseline_relative not in new_names:
                errors.append(
                    issue(
                        "compare.baseline.not-new",
                        "a changed baseline must point to a snapshot created in this transition",
                    )
                )

    return errors


def _record_question_sets(record: dict[str, object]) -> tuple[set[str], set[str], set[str]]:
    state = record.get("state", {})
    if not isinstance(state, dict):
        return set(), set(), set()
    values: list[set[str]] = []
    for key in ("open_questions", "closed_questions", "cancelled_questions"):
        raw = state.get(key, [])
        values.append(set(raw) if isinstance(raw, list) and all(isinstance(item, str) for item in raw) else set())
    return values[0], values[1], values[2]


def validate_assertions(
    project_root: Path,
    artifacts: dict[str, str],
    assertions: dict[str, object],
    *,
    prior_records: dict[str, dict[str, object]] | None = None,
) -> list[str]:
    """Evaluate the small, task-specific assertion vocabulary used by evals."""
    errors: list[str] = []
    prior_records = prior_records or {}
    live_relative = Path(artifacts["live"])
    live_path = project_root / live_relative
    live_validation = validate_live_file(live_path, project_root=project_root)
    errors.extend(live_validation.errors)
    if live_validation.errors:
        return errors
    live_state = live_validation.state
    live_text = live_path.read_text(encoding="utf-8")
    sections, _ = markdown_h2_sections(live_text)
    current_snapshots = snapshot_hashes(project_root, live_relative)

    for entry in assertions.get("files", []):
        if not isinstance(entry, dict):
            continue
        artifact = entry.get("artifact")
        expected_exists = entry.get("exists")
        if artifact == "live":
            found = live_path.is_file() and not live_path.is_symlink()
        elif artifact == "snapshots":
            found = bool(current_snapshots)
        else:
            raw_path = artifacts.get(str(artifact), "")
            found = bool(raw_path) and (project_root / raw_path).exists()
        if isinstance(expected_exists, bool) and found != expected_exists:
            errors.append(
                issue(
                    "assert.file.exists",
                    f"artifact {artifact!r} existence expected {expected_exists}, found {found}",
                )
            )

    expected_sections = assertions.get("sections", [])
    if isinstance(expected_sections, list):
        for heading in expected_sections:
            if isinstance(heading, str) and heading not in sections:
                errors.append(issue("assert.section.missing", f"live file is missing H2 {heading!r}"))

    questions = assertions.get("questions", {})
    if isinstance(questions, dict):
        actual_groups = {
            "open": set(live_state.open_questions),
            "closed": set(live_state.closed_questions),
            "cancelled": set(live_state.cancelled_questions),
        }
        for key in ("open", "closed", "cancelled"):
            if key in questions and isinstance(questions[key], list):
                expected = set(questions[key])
                if actual_groups[key] != expected:
                    errors.append(
                        issue(
                            f"assert.questions.{key}",
                            f"expected {key} IDs {sorted(expected)!r}, found {sorted(actual_groups[key])!r}",
                        )
                    )
        absent = set(questions.get("absent", [])) if isinstance(questions.get("absent", []), list) else set()
        present_absent = absent & live_state.used_questions
        if present_absent:
            errors.append(
                issue(
                    "assert.questions.absent",
                    "IDs expected absent are present: " + ", ".join(sorted(present_absent)),
                )
            )
        for key, group in (
            ("open_count_min", actual_groups["open"]),
            ("closed_count_min", actual_groups["closed"]),
            ("cancelled_count_min", actual_groups["cancelled"]),
        ):
            minimum = questions.get(key)
            if isinstance(minimum, int) and not isinstance(minimum, bool) and len(group) < minimum:
                errors.append(issue(f"assert.questions.{key}", f"expected at least {minimum}, found {len(group)}"))

        preserve_from = questions.get("preserve_from_step")
        if isinstance(preserve_from, str):
            prior = prior_records.get(preserve_from)
            if prior is None:
                errors.append(issue("assert.questions.prior", f"missing record for step {preserve_from!r}"))
            else:
                old_open, old_closed, old_cancelled = _record_question_sets(prior)
                old_used = old_open | old_closed | old_cancelled
                lost = old_used - live_state.used_questions
                if lost:
                    errors.append(
                        issue(
                            "assert.questions.history",
                            "prior question IDs disappeared: " + ", ".join(sorted(lost)),
                        )
                    )
                max_old = max((int(value[2:]) for value in old_used if valid_numbered_id(value, "Q")), default=0)
                reused = {
                    value
                    for value in live_state.used_questions - old_used
                    if valid_numbered_id(value, "Q") and int(value[2:]) <= max_old
                }
                if reused:
                    errors.append(
                        issue(
                            "assert.questions.reused",
                            "new question IDs do not continue the prior sequence: " + ", ".join(sorted(reused)),
                        )
                    )
                reopened = (old_closed | old_cancelled) & set(live_state.open_questions)
                if reopened:
                    errors.append(
                        issue(
                            "assert.questions.reopened",
                            "resolved IDs became open again: " + ", ".join(sorted(reopened)),
                        )
                    )

        moved_from = questions.get("all_open_moved_to_registry_from_step")
        if moved_from is True:
            moved_from = preserve_from
        if isinstance(moved_from, str):
            prior = prior_records.get(moved_from)
            if prior is None:
                errors.append(issue("assert.questions.prior", f"missing record for step {moved_from!r}"))
            else:
                old_open, _, _ = _record_question_sets(prior)
                unresolved = old_open - (set(live_state.closed_questions) | set(live_state.cancelled_questions))
                if unresolved:
                    errors.append(
                        issue(
                            "assert.questions.not-moved",
                            "prior open IDs did not move to the registry: " + ", ".join(sorted(unresolved)),
                        )
                    )

    revision = assertions.get("revision", {})
    if isinstance(revision, dict):
        actual_revision = {
            "current": live_state.current_revision,
            "awaiting": live_state.awaiting_revision,
            "baseline": live_state.baseline_snapshot,
        }
        for key, expected in revision.items():
            if key in actual_revision and actual_revision[key] != expected:
                errors.append(
                    issue(
                        f"assert.revision.{key}",
                        f"expected {key} {expected!r}, found {actual_revision[key]!r}",
                    )
                )

    snapshots = assertions.get("snapshots", {})
    if isinstance(snapshots, dict):
        if "exact_names" in snapshots and isinstance(snapshots["exact_names"], list):
            expected_names = set(snapshots["exact_names"])
            if set(current_snapshots) != expected_names:
                errors.append(
                    issue(
                        "assert.snapshots.exact",
                        f"expected snapshots {sorted(expected_names)!r}, found {sorted(current_snapshots)!r}",
                    )
                )
        prior_step = snapshots.get("unchanged_from_step")
        if isinstance(prior_step, str):
            prior = prior_records.get(prior_step)
            if prior is None:
                errors.append(issue("assert.snapshots.prior", f"missing record for step {prior_step!r}"))
            else:
                before_snapshots = prior.get("snapshots", {})
                if not isinstance(before_snapshots, dict):
                    errors.append(issue("assert.snapshots.record", f"invalid snapshot record for {prior_step!r}"))
                else:
                    for name, digest in before_snapshots.items():
                        if current_snapshots.get(name) != digest:
                            errors.append(
                                issue(
                                    "assert.snapshots.immutable",
                                    f"prior snapshot missing or changed: {name}",
                                )
                            )
                    if isinstance(snapshots.get("new_names"), list):
                        found_new = set(current_snapshots) - set(before_snapshots)
                        expected_new = set(snapshots["new_names"])
                        if found_new != expected_new:
                            errors.append(
                                issue(
                                    "assert.snapshots.new",
                                    f"expected new snapshots {sorted(expected_new)!r}, found {sorted(found_new)!r}",
                                )
                            )
        if snapshots.get("content_equals_live") is True:
            baseline = live_state.baseline_snapshot
            baseline_relative = (
                (live_relative.parent / baseline).as_posix()
                if isinstance(baseline, str) and baseline != "нет"
                else None
            )
            if baseline_relative is None or current_snapshots.get(baseline_relative) != sha256_file(live_path):
                errors.append(
                    issue(
                        "assert.snapshots.content",
                        "the active agreed snapshot must be byte-identical to the live file",
                    )
                )

    return errors


def find_sequence_step(
    manifest_path: Path,
    sequence_id: str,
    step_id: str,
) -> tuple[dict[str, object], dict[str, object], list[str]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {}, {}, [issue("manifest.read", f"cannot read eval manifest: {error}")]
    if not isinstance(payload, dict):
        return {}, {}, [issue("manifest.schema", "eval manifest root must be an object")]
    for sequence in payload.get("sequences", []):
        if isinstance(sequence, dict) and sequence.get("id") == sequence_id:
            for step in sequence.get("steps", []):
                if isinstance(step, dict) and step.get("id") == step_id:
                    return sequence, step, []
            return {}, {}, [issue("manifest.step", f"unknown step {step_id!r} in sequence {sequence_id!r}")]
    return {}, {}, [issue("manifest.sequence", f"unknown sequence {sequence_id!r}")]


def load_prior_records(records_dir: Path) -> tuple[dict[str, dict[str, object]], list[str]]:
    records: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    if not records_dir.exists():
        return records, errors
    for path in records_dir.glob("*.json"):
        record, record_errors = load_record(path)
        errors.extend(record_errors)
        if not record_errors:
            records[path.stem] = record
    return records, errors


def safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise argparse.ArgumentTypeError("path must be a safe relative path")
    return path


def print_errors(errors: Iterable[str]) -> int:
    values = list(errors)
    if not values:
        print("Artifact validation passed.")
        return 0
    print(f"Artifact validation failed with {len(values)} error(s):", file=sys.stderr)
    for value in values:
        print(f"- {value}", file=sys.stderr)
    return 1


def records_outside_project(records_dir: Path, project_root: Path) -> bool:
    try:
        records_dir.resolve().relative_to(project_root.resolve())
    except ValueError:
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    live_parser = subparsers.add_parser("live", help="validate one live Markdown file")
    live_parser.add_argument("live_file", type=Path)
    live_parser.add_argument("--project-root", type=Path)

    record_parser = subparsers.add_parser("record", help="record live and snapshot SHA-256 state")
    record_parser.add_argument("project_root", type=Path)
    record_parser.add_argument("--live", type=safe_relative_path, required=True)
    record_parser.add_argument("--output", type=Path, required=True)

    compare_parser = subparsers.add_parser("compare", help="compare current artifacts with a prior record")
    compare_parser.add_argument("before_record", type=Path)
    compare_parser.add_argument("project_root", type=Path)
    compare_parser.add_argument("--live", type=safe_relative_path, required=True)
    live_group = compare_parser.add_mutually_exclusive_group()
    live_group.add_argument("--expect-live-changed", action="store_true")
    live_group.add_argument("--expect-live-unchanged", action="store_true")
    new_snapshot_group = compare_parser.add_mutually_exclusive_group()
    new_snapshot_group.add_argument(
        "--expect-new",
        action="append",
        help="require this exact new snapshot path; repeat for multiple snapshots",
    )
    new_snapshot_group.add_argument(
        "--expect-no-new",
        action="store_true",
        help="require that no snapshot was created since the record",
    )
    compare_parser.add_argument("--snapshot-equals-live", action="append", default=[])

    begin_parser = subparsers.add_parser(
        "begin-step",
        help="verify carried artifacts, then print a sequence step input",
    )
    begin_parser.add_argument("manifest", type=Path)
    begin_parser.add_argument("sequence")
    begin_parser.add_argument("step")
    begin_parser.add_argument("project_root", type=Path)
    begin_parser.add_argument("--records-dir", type=Path, required=True)

    check_parser = subparsers.add_parser(
        "check-step",
        help="check one completed sequence step and record its artifact hashes",
    )
    check_parser.add_argument("manifest", type=Path)
    check_parser.add_argument("sequence")
    check_parser.add_argument("step")
    check_parser.add_argument("project_root", type=Path)
    check_parser.add_argument("--records-dir", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "live":
        root = args.project_root.resolve() if args.project_root else None
        return print_errors(validate_live_file(args.live_file.resolve(), project_root=root).errors)

    if args.command == "record":
        record, errors = build_record(args.project_root, args.live)
        if errors:
            return print_errors(errors)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Recorded artifact state: {args.output}")
        return 0

    if args.command in {"begin-step", "check-step"}:
        project_root = args.project_root.resolve()
        if not records_outside_project(args.records_dir, project_root):
            return print_errors(
                [issue("records.location", "records-dir must be outside the client-visible project root")]
            )
        sequence, step, errors = find_sequence_step(
            args.manifest,
            args.sequence,
            args.step,
        )
        if errors:
            return print_errors(errors)
        artifacts = sequence.get("artifacts", {})
        if not isinstance(artifacts, dict) or not all(isinstance(value, str) for value in artifacts.values()):
            return print_errors([issue("manifest.artifacts", "sequence artifacts must map names to paths")])
        live_relative = Path(str(artifacts.get("live", "")))
        prior_records, record_errors = load_prior_records(args.records_dir)
        if record_errors:
            return print_errors(record_errors)

        if args.command == "begin-step":
            carry_errors: list[str] = []
            current_record: dict[str, object] | None = None
            for carry in step.get("carry_actual", []):
                if not isinstance(carry, dict):
                    continue
                from_step = carry.get("from_step")
                artifact = carry.get("artifact")
                prior = prior_records.get(str(from_step))
                if prior is None:
                    carry_errors.append(
                        issue("carry.record", f"missing actual artifact record for step {from_step!r}")
                    )
                    continue
                if current_record is None:
                    current_record, build_errors = build_record(project_root, live_relative)
                    carry_errors.extend(build_errors)
                    if build_errors:
                        break
                if artifact == "live" and current_record.get("live_sha256") != prior.get("live_sha256"):
                    carry_errors.append(
                        issue(
                            "carry.live",
                            f"current live file is not the recorded actual output of step {from_step!r}",
                        )
                    )
                if artifact == "snapshots" and current_record.get("snapshots") != prior.get("snapshots"):
                    carry_errors.append(
                        issue(
                            "carry.snapshots",
                            f"current snapshots differ from the recorded output of step {from_step!r}",
                        )
                    )
            if carry_errors:
                return print_errors(carry_errors)
            if step.get("chat") == "new":
                print("Start a new client chat/process in the same project before sending this input.", file=sys.stderr)
            print(step.get("input", ""))
            return 0

        expected = step.get("expected", {})
        assertions = expected.get("assertions", {}) if isinstance(expected, dict) else {}
        if not isinstance(assertions, dict):
            return print_errors([issue("manifest.assertions", "step assertions must be an object")])
        assertion_errors = validate_assertions(
            project_root,
            {str(key): str(value) for key, value in artifacts.items()},
            assertions,
            prior_records=prior_records,
        )
        if assertion_errors:
            return print_errors(assertion_errors)
        record, build_errors = build_record(project_root, live_relative)
        if build_errors:
            return print_errors(build_errors)
        args.records_dir.mkdir(parents=True, exist_ok=True)
        output = args.records_dir / f"{args.step}.json"
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Sequence step passed; recorded artifact state: {output}")
        return 0

    before, errors = load_record(args.before_record)
    if errors:
        return print_errors(errors)
    expect_live = "changed" if args.expect_live_changed else "unchanged" if args.expect_live_unchanged else "any"
    errors = compare_record(
        before,
        args.project_root.resolve(),
        args.live,
        expected_new=[] if args.expect_no_new else args.expect_new,
        expect_live=expect_live,
        snapshot_equals_live=args.snapshot_equals_live,
    )
    return print_errors(errors)


if __name__ == "__main__":
    raise SystemExit(main())
