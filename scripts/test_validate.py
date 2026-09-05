#!/usr/bin/env python3
"""Regression tests for package and task artifact validation."""

from __future__ import annotations

import json
import contextlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import validate as package_validator  # noqa: E402
from validate_artifacts import (  # noqa: E402
    REQUIRED_GATE_HEADINGS,
    build_record,
    compare_record,
    is_valid_snapshot_name,
    main as artifact_main,
    validate_assertions,
    validate_live_file,
)


def live_document(
    *,
    status: str = "нужны уточнения",
    revision: str = "R-001",
    awaiting: str = "нет",
    baseline: str = "нет",
    open_lines: str = "- Q-001 — Конечный результат — блокирующий: какой результат нужен?",
    current_lines: str = "- Q-001 — Какой результат нужен?",
    map_reference: str = "Q-001",
    decision_rows: str = "| — | — | Закрытых решений пока нет. |",
    proposed_gate: bool = False,
) -> str:
    result_state = "предлагается" if proposed_gate else "подтверждено"
    return f"""# Понимание задачи: синтетическая проверка

## Состояние

- Статус: {status}
- Текущая редакция: {revision}
- Ожидается подтверждение редакции: {awaiting}
- Действующая согласованная версия: {baseline}
- Что мешает перейти дальше: тестовое уточнение
- Следующий вход: ответ заказчика

## Что требует уточнения

| Раздел | Проблема | Влияние на результат | Способ разрешения |
|---|---|---|---|
| Конечный результат | Не выбран формат | Меняется результат | {map_reference} |

## Проблема

подтверждено: исходные сведения разрознены.

## Задача и цель

подтверждено: собрать основу для решения.

## Аудитория или пользователь

подтверждено: руководитель программы.

## Ожидаемое воздействие

подтверждено: принять решение.

## Конечный результат

{result_state}: аналитическая записка.

## Границы работы

подтверждено: анализ входит, публикация не входит.

## Критерии приёмки

подтверждено: выводы проверяются по исходным данным.

## Кто принимает итог

подтверждено: руководитель программы.

## Открытые вопросы

{open_lines}

### Вопросы заказчику сейчас

{current_lines}

## Решения по вопросам

| ID | Вопрос | Решение |
|---|---|---|
{decision_rows}
"""


class TemporaryRepositoryTest(unittest.TestCase):
    def copy_repository(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        destination = Path(temporary.name) / "task-understanding"
        shutil.copytree(
            REPOSITORY_ROOT,
            destination,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        return temporary, destination

    def test_package_baseline_is_green(self) -> None:
        result = package_validator.validate_package(REPOSITORY_ROOT)
        self.assertEqual([], result.errors, "\n".join(result.errors))

    def test_removed_readiness_section_fails_addressably(self) -> None:
        temporary, root = self.copy_repository()
        self.addCleanup(temporary.cleanup)
        skill_path = root / "SKILL.md"
        original = skill_path.read_text(encoding="utf-8")
        mutated, replacements = re.subn(
            r"(?ms)^## Порог готовности\s*$\n.*?(?=^##\s|\Z)",
            "",
            original,
            count=1,
        )
        self.assertEqual(1, replacements)
        mutated += (
            "\n```markdown\n## Порог готовности\n"
            "1. one\n2. two\n3. three\n4. four\n5. five\n6. six\n7. seven\n```\n"
        )
        skill_path.write_text(mutated, encoding="utf-8")

        result = package_validator.validate_package(root)
        self.assertTrue(
            any("[skill.readiness-gate.missing]" in error for error in result.errors),
            "\n".join(result.errors),
        )

    def test_missing_required_fixture_section_fails_addressably(self) -> None:
        temporary, root = self.copy_repository()
        self.addCleanup(temporary.cleanup)
        payload = json.loads((root / "evals/evals.json").read_text(encoding="utf-8"))
        target: Path | None = None
        for case in payload["cases"]:
            if not case.get("fixture_dir") or case.get("legacy_fixture"):
                continue
            fixture = root / case["fixture_dir"]
            target = next(
                (
                    path
                    for path in fixture.rglob("Понимание задачи*.md")
                    if " — согласовано " not in path.name
                ),
                None,
            )
            if target is not None:
                break
        self.assertIsNotNone(target, "no full non-legacy live fixture found")
        assert target is not None
        original = target.read_text(encoding="utf-8")
        mutated, replacements = re.subn(
            r"(?ms)^## Кто принимает итог\s*$\n.*?(?=^##\s|\Z)",
            "",
            original,
            count=1,
        )
        self.assertEqual(1, replacements)
        target.write_text(mutated, encoding="utf-8")

        result = package_validator.validate_package(root)
        self.assertTrue(
            any("[eval.fixture.section]" in error and "Кто принимает итог" in error for error in result.errors),
            "\n".join(result.errors),
        )


class LiveArtifactValidationTest(unittest.TestCase):
    def write_live(self, text: str) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        project = Path(temporary.name) / "project"
        work = project / "work"
        work.mkdir(parents=True)
        live = work / "Понимание задачи.md"
        live.write_text(text, encoding="utf-8")
        self.addCleanup(temporary.cleanup)
        return temporary, project, live

    def test_duplicate_canonical_question_id_fails(self) -> None:
        document = live_document(
            open_lines=(
                "- Q-001 — Результат — блокирующий: какой результат нужен?\n"
                "- Q-001 — Границы — блокирующий: что входит в работу?"
            )
        )
        _, project, live = self.write_live(document)
        errors = validate_live_file(live, project_root=project).errors
        self.assertTrue(any("[live.question-id.duplicate]" in error for error in errors), "\n".join(errors))

    def test_question_reference_is_not_a_duplicate_definition(self) -> None:
        document = live_document(
            open_lines=(
                "- Q-001 — Результат — блокирующий: какой результат нужен?\n"
                "- Q-002 — Критерии — блокирующий; после Q-001: какие критерии подтвердить?"
            ),
            current_lines="- Q-001 — Какой результат нужен?",
            map_reference="Q-001 и Q-002",
        )
        _, project, live = self.write_live(document)
        errors = validate_live_file(live, project_root=project).errors
        self.assertFalse(any("question-id.duplicate" in error for error in errors), "\n".join(errors))

    def test_broken_question_map_reference_fails(self) -> None:
        document = live_document(map_reference="Q-002")
        _, project, live = self.write_live(document)
        errors = validate_live_file(live, project_root=project).errors
        self.assertTrue(any("[live.question-map.unknown]" in error for error in errors), "\n".join(errors))
        self.assertTrue(any("[live.question-map.missing]" in error for error in errors), "\n".join(errors))

    def test_plain_state_labels_and_large_ids_are_valid(self) -> None:
        document = live_document(
            revision="R-1000",
            open_lines="- Q-1000 — Результат — блокирующий: какой результат нужен?",
            current_lines="- Q-1000 — Какой результат нужен?",
            map_reference="Q-1000",
        )
        _, project, live = self.write_live(document)
        errors = validate_live_file(live, project_root=project).errors
        self.assertFalse(errors, "\n".join(errors))
        self.assertTrue(is_valid_snapshot_name("Понимание задачи — согласовано 2026-09-05 10.md", live))

    def test_unknown_or_missing_status_fails(self) -> None:
        document = live_document(status="ожидание чуда")
        _, project, live = self.write_live(document)
        errors = validate_live_file(live, project_root=project).errors
        self.assertTrue(any("[live.status.value]" in error for error in errors), "\n".join(errors))

        live.write_text(re.sub(r"(?m)^- Статус:.*\n", "", document), encoding="utf-8")
        errors = validate_live_file(live, project_root=project).errors
        self.assertTrue(any("[live.status.count]" in error for error in errors), "\n".join(errors))

    def test_ready_file_with_proposed_gate_fact_fails(self) -> None:
        document = live_document(
            status="готово к согласованию",
            awaiting="R-001",
            open_lines="Актуальных открытых вопросов нет.",
            current_lines="Актуальных вопросов заказчику нет.",
            map_reference="допущение",
            proposed_gate=True,
        )
        _, project, live = self.write_live(document)
        errors = validate_live_file(live, project_root=project).errors
        self.assertTrue(any("[live.gate.proposed]" in error for error in errors), "\n".join(errors))

    def test_structured_question_and_revision_assertions(self) -> None:
        _, project, live = self.write_live(live_document())
        assertions = {
            "files": [{"artifact": "live", "exists": True}],
            "sections": list(REQUIRED_GATE_HEADINGS),
            "questions": {"open_count_min": 1, "map_references": True},
            "revision": {"current": "R-001", "awaiting": "нет", "baseline": "нет"},
            "snapshots": {"exact_names": [], "new_names": [], "content_equals_live": False},
        }
        errors = validate_assertions(
            project,
            {
                "live": live.relative_to(project).as_posix(),
                "snapshots": "work/Понимание задачи — согласовано *.md",
            },
            assertions,
        )
        self.assertEqual([], errors, "\n".join(errors))

        assertions["revision"]["current"] = "R-002"
        errors = validate_assertions(
            project,
            {
                "live": live.relative_to(project).as_posix(),
                "snapshots": "work/Понимание задачи — согласовано *.md",
            },
            assertions,
        )
        self.assertTrue(any("[assert.revision.current]" in error for error in errors), "\n".join(errors))

        assertions["revision"]["current"] = "R-001"
        assertions["questions"]["open_count_min"] = 2
        errors = validate_assertions(
            project,
            {
                "live": live.relative_to(project).as_posix(),
                "snapshots": "work/Понимание задачи — согласовано *.md",
            },
            assertions,
        )
        self.assertTrue(any("[assert.questions.open_count_min]" in error for error in errors), "\n".join(errors))

    def test_mutated_prior_snapshot_fails(self) -> None:
        snapshot_name = "Понимание задачи — согласовано 2026-09-05.md"
        document = live_document(
            status="согласовано",
            baseline=snapshot_name,
            open_lines="Актуальных открытых вопросов нет.",
            current_lines="Актуальных вопросов заказчику нет.",
            map_reference="допущение",
        )
        _, project, live = self.write_live(document)
        snapshot = live.parent / snapshot_name
        snapshot.write_text(document, encoding="utf-8")
        before, errors = build_record(project, live.relative_to(project))
        self.assertEqual([], errors, "\n".join(errors))

        snapshot.write_text(document + "\nизменено\n", encoding="utf-8")
        errors = compare_record(before, project, live.relative_to(project))
        self.assertTrue(any("[snapshot.mutated]" in error for error in errors), "\n".join(errors))

    def test_agreed_baseline_cannot_be_a_symlink(self) -> None:
        snapshot_name = "Понимание задачи — согласовано 2026-09-05.md"
        document = live_document(
            status="согласовано",
            baseline=snapshot_name,
            open_lines="Актуальных открытых вопросов нет.",
            current_lines="Актуальных вопросов заказчику нет.",
            map_reference="допущение",
        )
        _, project, live = self.write_live(document)
        outside = project.parent / "outside.md"
        outside.write_text(document, encoding="utf-8")
        (live.parent / snapshot_name).symlink_to(outside)
        errors = validate_live_file(live, project_root=project).errors
        self.assertTrue(any("[live.baseline.symlink]" in error for error in errors), "\n".join(errors))

    def test_agreed_baseline_cannot_disappear(self) -> None:
        snapshot_name = "Понимание задачи — согласовано 2026-09-05.md"
        document = live_document(
            status="согласовано",
            baseline=snapshot_name,
            open_lines="Актуальных открытых вопросов нет.",
            current_lines="Актуальных вопросов заказчику нет.",
            map_reference="допущение",
        )
        _, project, live = self.write_live(document)
        (live.parent / snapshot_name).write_text(document, encoding="utf-8")
        before, errors = build_record(project, live.relative_to(project))
        self.assertEqual([], errors, "\n".join(errors))

        changed = document.replace("Статус: согласовано", "Статус: нужны уточнения").replace(
            f"Действующая согласованная версия: {snapshot_name}",
            "Действующая согласованная версия: нет",
        )
        live.write_text(changed, encoding="utf-8")
        errors = compare_record(before, project, live.relative_to(project))
        self.assertTrue(any("[compare.baseline.lost]" in error for error in errors), "\n".join(errors))

    def test_resolved_question_id_cannot_be_reopened(self) -> None:
        before_text = live_document(
            open_lines="Актуальных открытых вопросов нет.",
            current_lines="Актуальных вопросов заказчику нет.",
            map_reference="допущение",
            decision_rows="| Q-001 | Какой результат нужен? | Подтверждено: записка. |",
        )
        _, project, live = self.write_live(before_text)
        before, errors = build_record(project, live.relative_to(project))
        self.assertEqual([], errors, "\n".join(errors))

        live.write_text(live_document(), encoding="utf-8")
        errors = compare_record(before, project, live.relative_to(project))
        self.assertTrue(any("[compare.question-id.reopened]" in error for error in errors), "\n".join(errors))

    def test_compare_cli_can_require_no_new_snapshot(self) -> None:
        _, project, live = self.write_live(live_document())
        record_path = project.parent / "before.json"
        record, errors = build_record(project, live.relative_to(project))
        self.assertEqual([], errors, "\n".join(errors))
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

        command = [
            "compare",
            str(record_path),
            str(project),
            "--live",
            live.relative_to(project).as_posix(),
            "--expect-no-new",
        ]
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = artifact_main(command)
        self.assertEqual(0, result, sink.getvalue())

        (live.parent / "Понимание задачи — согласовано 2026-09-05.md").write_text(
            live.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = artifact_main(command)
        self.assertEqual(1, result)
        self.assertIn("[snapshot.new-set]", sink.getvalue())

    def test_sequence_cli_checks_actual_handoff(self) -> None:
        _, project, live = self.write_live(live_document())
        records = project.parent / "records"
        manifest = project.parent / "evals.json"
        assertions = {
            "files": [{"artifact": "live", "exists": True}],
            "sections": list(REQUIRED_GATE_HEADINGS),
            "questions": {"open_count_min": 1, "map_references": True},
            "revision": {"current": "R-001", "awaiting": "нет", "baseline": "нет"},
            "snapshots": {"exact_names": [], "new_names": [], "content_equals_live": False},
        }
        manifest.write_text(
            json.dumps(
                {
                    "sequences": [
                        {
                            "id": "handoff",
                            "artifacts": {
                                "live": live.relative_to(project).as_posix(),
                                "snapshots": "work/Понимание задачи — согласовано *.md",
                            },
                            "steps": [
                                {"id": "first", "chat": "new", "input": "first", "expected": {"assertions": assertions}},
                                {
                                    "id": "second",
                                    "chat": "new",
                                    "input": "second",
                                    "carry_actual": [{"artifact": "live", "from_step": "first"}],
                                    "expected": {"assertions": assertions},
                                },
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = artifact_main(
                [
                    "check-step",
                    str(manifest),
                    "handoff",
                    "first",
                    str(project),
                    "--records-dir",
                    str(records),
                ]
            )
        self.assertEqual(0, result, sink.getvalue())

        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = artifact_main(
                [
                    "begin-step",
                    str(manifest),
                    "handoff",
                    "second",
                    str(project),
                    "--records-dir",
                    str(records),
                ]
            )
        self.assertEqual(0, result, sink.getvalue())

        live.write_text(live.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = artifact_main(
                [
                    "begin-step",
                    str(manifest),
                    "handoff",
                    "second",
                    str(project),
                    "--records-dir",
                    str(records),
                ]
            )
        self.assertEqual(1, result)
        self.assertIn("[carry.live]", sink.getvalue())


if __name__ == "__main__":
    unittest.main()
