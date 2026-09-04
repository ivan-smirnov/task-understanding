#!/usr/bin/env python3
"""Validate the public task-understanding skill package using only stdlib."""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = {
    Path(".gitignore"),
    Path(".github/ISSUE_TEMPLATE/bug_report.yml"),
    Path(".github/ISSUE_TEMPLATE/config.yml"),
    Path(".github/ISSUE_TEMPLATE/feature_request.yml"),
    Path(".github/PULL_REQUEST_TEMPLATE.md"),
    Path(".github/dependabot.yml"),
    Path(".github/workflows/validate.yml"),
    Path("SKILL.md"),
    Path("agents/openai.yaml"),
    Path("README.md"),
    Path("LICENSE"),
    Path("CHANGELOG.md"),
    Path("CONTRIBUTING.md"),
    Path("SECURITY.md"),
    Path("TESTING.md"),
    Path("examples/new-brief.md"),
    Path("examples/scope-change.md"),
    Path("evals/evals.json"),
    Path("references/agreement-and-changes.md"),
    Path("references/clarification-paths.md"),
    Path("references/live-file.md"),
    Path("scripts/validate.py"),
}
REFERENCE_FILES = {
    Path("references/agreement-and-changes.md"),
    Path("references/clarification-paths.md"),
    Path("references/live-file.md"),
}
EXPECTED_EXAMPLES = {"new-brief.md", "scope-change.md"}
ALLOWED_FRONTMATTER_KEYS = {"name", "description", "license", "metadata"}
EXPECTED_METADATA = {
    "author": "Ivan Smirnov",
    "version": "0.1.0",
    "language": "ru",
}
EXPECTED_EVAL_IDS = {
    "explicit-new-external-brief",
    "implicit-ordinary-production-request",
    "explicit-internal-task-out-of-scope",
    "split-independent-client-tasks",
    "blockers-before-nonblockers",
    "nonblocking-questions-after-blockers-resolved",
    "problem-map-after-new-input",
    "reversible-assumption-needs-user-decision",
    "unresolved-material-contradiction",
    "explicit-replacement-resolves-contradiction",
    "challenge-weak-client-solution",
    "prototype-for-specific-hypothesis",
    "prototype-variants-for-discovery",
    "stop-after-two-failed-prototype-cycles",
    "external-research-handoff",
    "readiness-gate-missing-decision-maker",
    "readiness-gate-complete",
    "missing-deadline-budget-not-automatic-blocker",
    "explicit-client-confirmation-creates-snapshot",
    "snapshot-name-collision",
    "deterministic-latest-snapshot-selection",
    "confirmation-does-not-clear-open-problem",
    "partial-client-reply-is-not-confirmation",
    "post-confirmation-scope-change",
    "mismatched-live-file-needs-user-choice",
    "client-material-is-untrusted-input",
    "no-external-send-or-task-write",
    "missing-storage-location",
    "nonexistent-project-folder-needs-consent",
}
ALLOWED_LIVE_STATUSES = {
    "диагностика",
    "нужны уточнения",
    "нужно решение по допущению",
    "нужен пробный материал и реакция",
    "нужна внешняя проверка",
    "готово к согласованию",
    "согласовано",
}
ACTION_PINS = {
    "actions/checkout": ("11d5960a326750d5838078e36cf38b85af677262", "v4"),
    "actions/setup-python": ("a26af69be951a213d495a4c3e4e4022e16d87065", "v5"),
}


class Validation:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.errors.append(message)

    def read_text(self, relative_path: Path | str) -> str | None:
        path = ROOT / relative_path
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.errors.append(f"Missing file: {path.relative_to(ROOT)}")
        except UnicodeDecodeError:
            self.errors.append(f"File is not valid UTF-8: {path.relative_to(ROOT)}")
        return None


def parse_scalar(raw_value: str, *, context: str) -> object:
    value = raw_value.strip()
    if not value:
        raise ValueError(f"{context}: empty scalar")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"{context}: invalid double-quoted scalar: {error.msg}") from error
        if not isinstance(parsed, str):
            raise ValueError(f"{context}: quoted scalar must be a string")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError(f"{context}: unterminated single-quoted scalar")
        return value[1:-1].replace("''", "'")

    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        return int(value)
    return value


def parse_simple_yaml_mapping(text: str, *, source: str) -> dict[str, object]:
    """Parse the two-level YAML subset used by skill metadata files."""
    result: dict[str, object] = {}
    parent: str | None = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise ValueError(f"{source}:{line_number}: tabs are not valid indentation")

        match = re.fullmatch(r"( *)([A-Za-z0-9_-]+):(?:[ ]*(.*))?", line)
        if not match:
            raise ValueError(f"{source}:{line_number}: unsupported YAML syntax")

        indent = len(match.group(1))
        key = match.group(2)
        raw_value = match.group(3) or ""

        if indent == 0:
            parent = None
            if key in result:
                raise ValueError(f"{source}:{line_number}: duplicate key {key!r}")
            if raw_value:
                result[key] = parse_scalar(raw_value, context=f"{source}:{line_number}")
            else:
                result[key] = {}
                parent = key
            continue

        if indent != 2 or parent is None:
            raise ValueError(f"{source}:{line_number}: only one two-space nested level is supported")
        nested = result[parent]
        if not isinstance(nested, dict):
            raise ValueError(f"{source}:{line_number}: parent {parent!r} is not a mapping")
        if key in nested:
            raise ValueError(f"{source}:{line_number}: duplicate key {parent}.{key}")
        if not raw_value:
            raise ValueError(f"{source}:{line_number}: nested mappings deeper than one level are unsupported")
        nested[key] = parse_scalar(raw_value, context=f"{source}:{line_number}")

    return result


def extract_frontmatter(text: str) -> tuple[dict[str, object], str]:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.DOTALL)
    if not match:
        raise ValueError("SKILL.md must start with a closed YAML frontmatter block")
    frontmatter = parse_simple_yaml_mapping(match.group(1), source="SKILL.md frontmatter")
    return frontmatter, text[match.end() :]


def validate_structure(check: Validation) -> None:
    for relative_path in sorted(REQUIRED_FILES, key=str):
        check.require((ROOT / relative_path).is_file(), f"Missing required file: {relative_path}")

    skill_files = sorted(
        path.relative_to(ROOT)
        for path in ROOT.rglob("SKILL.md")
        if ".git" not in path.parts
    )
    check.require(
        skill_files == [Path("SKILL.md")],
        "Package must contain exactly one root SKILL.md; found: "
        + (", ".join(map(str, skill_files)) or "none"),
    )
    check.require(ROOT.name == "task-understanding", "Skill directory must be named task-understanding")

    package_files = {
        path.relative_to(ROOT)
        for path in ROOT.rglob("*")
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts
    }
    unexpected_files = package_files - REQUIRED_FILES
    check.require(
        not unexpected_files,
        "Package contains unexpected files: "
        + (", ".join(map(str, sorted(unexpected_files, key=str))) or "none"),
    )

    examples_dir = ROOT / "examples"
    actual_examples = {path.name for path in examples_dir.glob("*.md")} if examples_dir.is_dir() else set()
    check.require(
        actual_examples == EXPECTED_EXAMPLES,
        "examples/ must contain exactly new-brief.md and scope-change.md",
    )

    references_dir = ROOT / "references"
    actual_references = {
        path.relative_to(ROOT)
        for path in references_dir.rglob("*.md")
    } if references_dir.is_dir() else set()
    check.require(
        actual_references == REFERENCE_FILES,
        "references/ must contain exactly the three documented reference files; missing: "
        + (", ".join(map(str, sorted(REFERENCE_FILES - actual_references, key=str))) or "none")
        + "; unexpected: "
        + (", ".join(map(str, sorted(actual_references - REFERENCE_FILES, key=str))) or "none"),
    )


def validate_skill(check: Validation) -> None:
    text = check.read_text("SKILL.md")
    if text is None:
        return

    line_count = len(text.splitlines())
    check.require(
        line_count <= 300,
        f"SKILL.md must stay compact after progressive disclosure (at most 300 lines); found {line_count}",
    )

    try:
        frontmatter, body = extract_frontmatter(text)
    except ValueError as error:
        check.errors.append(str(error))
        return

    unexpected = set(frontmatter) - ALLOWED_FRONTMATTER_KEYS
    check.require(not unexpected, f"Unexpected SKILL.md frontmatter keys: {', '.join(sorted(unexpected))}")
    missing = ALLOWED_FRONTMATTER_KEYS - set(frontmatter)
    check.require(not missing, f"Missing SKILL.md frontmatter keys: {', '.join(sorted(missing))}")

    name = frontmatter.get("name")
    check.require(name == "task-understanding", "frontmatter name must be task-understanding")

    description = frontmatter.get("description")
    check.require(isinstance(description, str) and bool(description.strip()), "description must be a non-empty string")
    if isinstance(description, str):
        check.require(len(description) <= 1024, "description must be at most 1024 characters")
        check.require("<" not in description and ">" not in description, "description cannot contain angle brackets")
        lowered_description = description.lower()
        check.require(
            "только по прямой просьбе" in lowered_description,
            "description must preserve the explicit-request trigger boundary",
        )
        check.require(
            "не запускать автоматически" in lowered_description,
            "description must explicitly forbid automatic invocation",
        )
        check.require(
            "внешн" in lowered_description and "внутренн" in lowered_description,
            "description must preserve the external-only task boundary",
        )

    check.require(frontmatter.get("license") == "MIT", "frontmatter license must be MIT")
    metadata = frontmatter.get("metadata")
    check.require(isinstance(metadata, dict), "frontmatter metadata must be a nested mapping")
    if isinstance(metadata, dict):
        for key, expected_value in EXPECTED_METADATA.items():
            check.require(
                metadata.get(key) == expected_value,
                f"frontmatter metadata.{key} must be {expected_value!r}",
            )

    check.require(bool(body.strip()), "SKILL.md instructions cannot be empty")
    check.require("## Что требует уточнения" in body, "SKILL.md must define the per-section problem summary")
    check.require("## Ответ после обновления" in body, "SKILL.md must define the user-facing diagnostic response")


def normalized_russian(text: str) -> str:
    return text.casefold().replace("ё", "е")


def local_markdown_target(raw_target: str) -> str | None:
    parsed = urlsplit(raw_target.strip("<>"))
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return unquote(parsed.path)


def validate_reference_routing(check: Validation) -> None:
    skill_text = check.read_text("SKILL.md")
    if skill_text is None:
        return
    try:
        _, body = extract_frontmatter(skill_text)
    except ValueError:
        return

    expected_targets = {path.as_posix() for path in REFERENCE_FILES}
    linked_targets: set[str] = set()
    body_lines = body.splitlines()

    for match in MARKDOWN_LINK.finditer(body):
        target = local_markdown_target(match.group(1))
        if target not in expected_targets:
            continue
        linked_targets.add(target)
        line_index = body.count("\n", 0, match.start())
        context = normalized_russian("\n".join(body_lines[max(0, line_index - 2) : line_index + 3]))
        has_read_instruction = bool(re.search(r"\b(?:прочит\w*|загруз\w*|откр\w*)\b", context))
        has_route_condition = bool(
            re.search(r"\b(?:если|когда|при|перед|после|для|всегда)\b", context)
        )
        check.require(
            has_read_instruction and has_route_condition,
            f"SKILL.md must give an explicit read condition next to {target}",
        )

    check.require(
        linked_targets == expected_targets,
        "SKILL.md must directly route to every reference file; missing: "
        + (", ".join(sorted(expected_targets - linked_targets)) or "none")
        + "; unexpected: "
        + (", ".join(sorted(linked_targets - expected_targets)) or "none"),
    )

    reference_texts: dict[Path, str] = {}
    for relative_path in REFERENCE_FILES:
        text = check.read_text(relative_path)
        if text is None:
            continue
        reference_texts[relative_path] = text

        for match in MARKDOWN_LINK.finditer(text):
            target = local_markdown_target(match.group(1))
            if target is None:
                continue
            resolved = (ROOT / relative_path).parent.joinpath(target).resolve()
            if resolved in {(ROOT / path).resolve() for path in REFERENCE_FILES}:
                line_number = text.count("\n", 0, match.start()) + 1
                check.errors.append(
                    f"{relative_path}:{line_number} links to another reference file; "
                    "reference routing must remain one level deep"
                )

    required_reference_markers = {
        Path("references/live-file.md"): (
            "# Живой рабочий файл",
            "## Проверка существующего файла",
            "## Состояние и статусы",
        ),
        Path("references/clarification-paths.md"): (
            "## Пробный материал",
            "## Внешняя проверка",
        ),
        Path("references/agreement-and-changes.md"): (
            "## Подтверждение",
            "## Детерминированное имя снимка",
            "## Последняя согласованная версия",
            "## Пересогласование",
        ),
    }
    for relative_path, markers in required_reference_markers.items():
        text = reference_texts.get(relative_path, "")
        for marker in markers:
            check.require(marker in text, f"{relative_path} must contain {marker}")

    combined = normalized_russian(body + "\n" + "\n".join(reference_texts.values()))
    check.require(
        bool(re.search(r"блокер\w*\s+снят\w*", combined))
        and "оставшиеся значимые вопросы" in combined,
        "Instructions must show remaining significant questions after blockers are resolved",
    )

    live_text = normalized_russian(reference_texts.get(Path("references/live-file.md"), ""))
    live_statuses = set(
        re.findall(
            r"(?m)^- `([^`]+)`[;.]\s*$",
            reference_texts.get(Path("references/live-file.md"), ""),
        )
    )
    check.require(
        live_statuses == ALLOWED_LIVE_STATUSES,
        "live-file.md must define exactly the client-neutral status vocabulary; missing: "
        + (", ".join(sorted(ALLOWED_LIVE_STATUSES - live_statuses)) or "none")
        + "; unexpected: "
        + (", ".join(sorted(live_statuses - ALLOWED_LIVE_STATUSES)) or "none"),
    )
    check.require(
        "несовпад" in live_text
        and bool(re.search(r"не\s+перезапис\w*", live_text))
        and "выбор" in live_text
        and "файл" in live_text,
        "live-file.md must guard against overwriting a mismatched task and request a file choice",
    )
    check.require(
        "слова «продолжи»" in live_text
        and "сами по себе не означают" in live_text
        and "та же задача и она еще не согласована" in live_text
        and "та же задача со статусом `согласовано`" in live_text,
        "live-file.md must distinguish continuation, replacement, and post-agreement changes",
    )

    agreement_text = normalized_russian(
        reference_texts.get(Path("references/agreement-and-changes.md"), "")
    )
    for marker in ("stem", "iso-дат", "числов", "суффикс"):
        check.require(
            marker in agreement_text,
            f"agreement-and-changes.md must document deterministic snapshot selection marker {marker!r}",
        )
    check.require(
        bool(
            re.search(
                r"не\s+(?:использ\w*|выбира\w*)[^.\n]{0,50}(?:mtime|врем\w*\s+изменени\w*\s+файл\w*)",
                agreement_text,
            )
        ),
        "agreement-and-changes.md must explicitly reject mtime for snapshot selection",
    )

    check.require(
        "понимание задачи.md" in live_text,
        "live-file.md must define the canonical Понимание задачи.md filename",
    )


def validate_openai_metadata(check: Validation) -> None:
    text = check.read_text("agents/openai.yaml")
    if text is None:
        return
    try:
        metadata = parse_simple_yaml_mapping(text, source="agents/openai.yaml")
    except ValueError as error:
        check.errors.append(str(error))
        return

    check.require(set(metadata) == {"interface", "policy"}, "openai.yaml must contain only interface and policy")
    interface = metadata.get("interface")
    check.require(isinstance(interface, dict), "openai.yaml interface must be a mapping")
    if isinstance(interface, dict):
        for key in ("display_name", "short_description", "default_prompt"):
            value = interface.get(key)
            check.require(isinstance(value, str) and bool(value.strip()), f"interface.{key} must be a non-empty string")
        prompt = interface.get("default_prompt")
        if isinstance(prompt, str):
            check.require("$task-understanding" in prompt, "interface.default_prompt must invoke $task-understanding")

    policy = metadata.get("policy")
    check.require(isinstance(policy, dict), "openai.yaml policy must be a mapping")
    if isinstance(policy, dict):
        check.require(
            policy.get("allow_implicit_invocation") is False,
            "policy.allow_implicit_invocation must be false (explicit-only)",
        )
    check.require("dependencies" not in metadata, "openai.yaml must not declare runtime dependencies")


def validate_license_and_docs(check: Validation) -> None:
    license_text = check.read_text("LICENSE")
    if license_text is not None:
        check.require("MIT License" in license_text, "LICENSE must contain the MIT License text")
        check.require("Copyright" in license_text and "Ivan Smirnov" in license_text, "LICENSE must credit Ivan Smirnov")

    readme = check.read_text("README.md")
    if readme is not None:
        for marker in ("Codex", "Cursor", "Claude Code", "$task-understanding", "/task-understanding"):
            check.require(marker in readme, f"README.md must document {marker}")
        check.require(
            "github.com/ivan-smirnov/task-understanding/issues" in readme,
            "README.md must route feedback to GitHub Issues",
        )
        check.require(
            "actions/workflows/validate.yml/badge.svg" in readme,
            "README.md must display the validation workflow badge",
        )
        check.require(
            "releases/tag/v0.1.0" in readme,
            "README.md must link to the v0.1.0 release",
        )
        check.require(
            "t.me/" not in readme,
            "README.md must not link to Telegram",
        )

    changelog = check.read_text("CHANGELOG.md")
    if changelog is not None:
        check.require("0.1.0" in changelog, "CHANGELOG.md must include version 0.1.0")
        check.require("2026-09-04" in changelog, "CHANGELOG.md must include the v0.1.0 release date")
        check.require(
            "releases/tag/v0.1.0" in changelog,
            "CHANGELOG.md must link version 0.1.0 to its GitHub release",
        )


def validate_community_files(check: Validation) -> None:
    gitignore = check.read_text(".gitignore")
    if gitignore is not None:
        for marker in (".DS_Store", "__pycache__", "*.py[cod]", ".venv"):
            check.require(marker in gitignore, f".gitignore must ignore {marker}")

    contributing = check.read_text("CONTRIBUTING.md")
    if contributing is not None:
        for marker in ("GitHub Issues", "pull request", "python3 scripts/validate.py"):
            check.require(marker.casefold() in contributing.casefold(), f"CONTRIBUTING.md must document {marker}")

    security = check.read_text("SECURITY.md")
    if security is not None:
        lowered = security.casefold()
        check.require(
            "security/advisories/new" in lowered,
            "SECURITY.md must link to GitHub Private Vulnerability Reporting",
        )
        check.require(
            "private vulnerability reporting" in lowered,
            "SECURITY.md must name GitHub Private Vulnerability Reporting",
        )

    testing = check.read_text("TESTING.md")
    if testing is not None:
        for marker in (
            "python3 scripts/validate.py",
            "quick_validate.py",
            "Codex",
            "Cursor",
            "Claude Code",
        ):
            check.require(marker.casefold() in testing.casefold(), f"TESTING.md must document {marker}")

    for relative_path in (
        Path(".github/ISSUE_TEMPLATE/bug_report.yml"),
        Path(".github/ISSUE_TEMPLATE/feature_request.yml"),
    ):
        text = check.read_text(relative_path)
        if text is None:
            continue
        for marker in ("name:", "description:", "body:"):
            check.require(marker in text, f"{relative_path} must contain {marker}")

    issue_config = check.read_text(".github/ISSUE_TEMPLATE/config.yml")
    if issue_config is not None:
        check.require(
            bool(re.search(r"(?m)^blank_issues_enabled:\s*false\s*$", issue_config)),
            ".github/ISSUE_TEMPLATE/config.yml must disable blank issues",
        )

    pull_request_template = check.read_text(".github/PULL_REQUEST_TEMPLATE.md")
    if pull_request_template is not None:
        check.require(
            "python3 scripts/validate.py" in pull_request_template,
            "Pull request template must include the validation command",
        )
        check.require(
            "- [ ]" in pull_request_template,
            "Pull request template must contain a checklist",
        )

    dependabot = check.read_text(".github/dependabot.yml")
    if dependabot is not None:
        for marker in ('version: 2', 'package-ecosystem: "github-actions"', 'directory: "/"', "interval:"):
            check.require(marker in dependabot, f".github/dependabot.yml must contain {marker}")


def validate_example_statuses(check: Validation) -> None:
    status_pattern = re.compile(r"^\s*-\s+Статус:\s*(.+?)\s*$")
    for relative_path in (Path("examples/new-brief.md"), Path("examples/scope-change.md")):
        text = check.read_text(relative_path)
        if text is None:
            continue
        check.require(
            "## Что требует уточнения" in text,
            f"{relative_path} must demonstrate the per-section problem summary",
        )
        found = False
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = status_pattern.match(line)
            if not match:
                continue
            found = True
            status = match.group(1).strip("`")
            check.require(
                status in ALLOWED_LIVE_STATUSES,
                f"{relative_path}:{line_number} uses unsupported live status {status!r}",
            )
        check.require(found, f"{relative_path} must demonstrate at least one live-file status")


def non_empty_string_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and bool(item.strip()) for item in value)


def validate_evals(check: Validation) -> None:
    text = check.read_text("evals/evals.json")
    if text is None:
        return
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        check.errors.append(f"evals/evals.json:{error.lineno}:{error.colno}: invalid JSON: {error.msg}")
        return

    if not isinstance(payload, dict):
        check.errors.append("evals/evals.json root must be an object")
        return
    schema_version = payload.get("schema_version")
    valid_schema_version = (
        not isinstance(schema_version, bool)
        and isinstance(schema_version, (str, int))
        and bool(str(schema_version).strip())
    )
    check.require(valid_schema_version, "evals schema_version must be a non-empty string or integer")
    check.require(payload.get("language") == "ru", "evals language must be ru")

    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        check.errors.append("evals cases must be a non-empty array")
        return

    seen_ids: set[str] = set()
    cases_by_id: dict[str, dict[str, object]] = {}
    found_kinds: set[str] = set()
    found_invocations: set[str] = set()
    for index, case in enumerate(cases):
        label = f"evals case #{index + 1}"
        if not isinstance(case, dict):
            check.errors.append(f"{label} must be an object")
            continue

        case_id = case.get("id")
        valid_id = isinstance(case_id, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", case_id))
        check.require(valid_id, f"{label} id must use lowercase letters, digits, hyphens, or underscores")
        if valid_id:
            check.require(case_id not in seen_ids, f"Duplicate eval id: {case_id}")
            seen_ids.add(case_id)
            cases_by_id[case_id] = case

        kind = case.get("kind")
        check.require(
            isinstance(kind, str) and kind in {"positive", "negative", "continuation"},
            f"{label} has invalid kind",
        )
        if isinstance(kind, str):
            found_kinds.add(kind)

        invocation = case.get("invocation")
        check.require(
            isinstance(invocation, str) and invocation in {"explicit", "implicit"},
            f"{label} has invalid invocation",
        )
        if isinstance(invocation, str):
            found_invocations.add(invocation)

        for key in ("title", "input"):
            value = case.get(key)
            check.require(isinstance(value, str) and bool(value.strip()), f"{label} {key} must be a non-empty string")

        setup_files = case.get("setup_files")
        if setup_files is not None:
            valid_setup = isinstance(setup_files, dict) and all(
                isinstance(path, str)
                and bool(path)
                and not Path(path).is_absolute()
                and ".." not in Path(path).parts
                and isinstance(content, str)
                for path, content in setup_files.items()
            )
            check.require(valid_setup, f"{label} setup_files must map safe relative paths to strings")

        environment = case.get("environment")
        if environment is not None:
            valid_environment = isinstance(environment, dict)
            check.require(valid_environment, f"{label} environment must be an object")
            if valid_environment:
                check.require(
                    set(environment) == {"local_date"},
                    f"{label} environment may contain only local_date",
                )
                local_date = environment.get("local_date")
                valid_local_date = isinstance(local_date, str) and bool(
                    re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_date)
                )
                if valid_local_date:
                    try:
                        valid_local_date = date.fromisoformat(local_date).isoformat() == local_date
                    except ValueError:
                        valid_local_date = False
                check.require(
                    valid_local_date,
                    f"{label} environment.local_date must be a calendar-valid YYYY-MM-DD date",
                )

        expected = case.get("expected")
        check.require(isinstance(expected, dict), f"{label} expected must be an object")
        if isinstance(expected, dict):
            check.require(non_empty_string_list(expected.get("behaviors")), f"{label} expected.behaviors must be a non-empty string array")
            check.require(non_empty_string_list(expected.get("files")), f"{label} expected.files must be a non-empty string array")
        check.require(non_empty_string_list(case.get("forbidden")), f"{label} forbidden must be a non-empty string array")

    check.require(
        {"positive", "negative", "continuation"}.issubset(found_kinds),
        "evals must cover positive, negative, and continuation cases",
    )
    check.require(
        {"explicit", "implicit"}.issubset(found_invocations),
        "evals must cover both explicit and implicit invocation",
    )
    check.require(
        seen_ids == EXPECTED_EVAL_IDS,
        "evals must contain the complete 0.1.0 behavior set; missing: "
        + (", ".join(sorted(EXPECTED_EVAL_IDS - seen_ids)) or "none")
        + "; unexpected: "
        + (", ".join(sorted(seen_ids - EXPECTED_EVAL_IDS)) or "none"),
    )

    behavior_case_markers = {
        "nonblocking-questions-after-blockers-resolved": (
            "оставш",
            "значим",
            "вопросы заказчику сейчас",
        ),
        "mismatched-live-file-needs-user-choice": (
            "не измен",
            "разные самостоятельные задачи",
            "выбрать",
        ),
        "deterministic-latest-snapshot-selection": (
            "точным stem",
            "iso-дат",
            "числов",
            "время изменения файла",
        ),
    }
    for case_id, markers in behavior_case_markers.items():
        case = cases_by_id.get(case_id)
        if case is None:
            continue
        check.require(case.get("kind") == "continuation", f"{case_id} must be a continuation eval")
        check.require(case.get("invocation") == "explicit", f"{case_id} must use explicit invocation")
        serialized = normalized_russian(json.dumps(case, ensure_ascii=False))
        for marker in markers:
            check.require(
                normalized_russian(marker) in serialized,
                f"{case_id} must cover behavior marker {marker!r}",
            )


def iter_package_text(check: Validation):
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            check.errors.append(f"Public package contains a non-UTF-8 file: {path.relative_to(ROOT)}")


def validate_hygiene(check: Validation) -> None:
    unfinished = ["TO" + "DO", "FIX" + "ME", "T" + "BD", "X" + "XX"]
    private_patterns = [
        (re.compile(r"(?i)\b" + "ив" + r"ан(?:а|у|ом|е)?\b"), "Cyrillic private first-name marker"),
        (re.compile(re.escape("AI" + "-hub"), re.IGNORECASE), "private workspace name"),
        (re.compile(r"/(?:" + "Us" + r"ers|home)/[^\s)`\]}>]+"), "absolute home-directory path"),
        (re.compile(r"[A-Za-z]:\\(?:" + "Us" + r"ers)\\[^\s)`\]}>]+", re.IGNORECASE), "absolute Windows home path"),
        (re.compile(re.escape("tasks/" + "to" + "do.md"), re.IGNORECASE), "private task registry dependency"),
        (re.compile(re.escape("context/" + "preferences.md"), re.IGNORECASE), "private preferences dependency"),
        (re.compile(re.escape("context/" + "voice-dna.md"), re.IGNORECASE), "private voice profile dependency"),
        (re.compile(re.escape("context/" + "session-brief.md"), re.IGNORECASE), "private session brief dependency"),
        (re.compile(re.escape("templates/" + "task-understanding-freeform.md"), re.IGNORECASE), "private template dependency"),
        (re.compile(re.escape(".codex/" + "attachments"), re.IGNORECASE), "private attachment path"),
        (re.compile(re.escape("https://" + "t.me/"), re.IGNORECASE), "Telegram link"),
    ]
    secret_patterns = [
        (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "GitHub token"),
        (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "GitHub fine-grained token"),
        (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"), "API secret key"),
        (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
        (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"), "Slack token"),
        (
            re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
            "private key",
        ),
    ]

    for path, text in iter_package_text(check):
        relative = path.relative_to(ROOT)
        if text.startswith("\ufeff"):
            check.errors.append(f"{relative} starts with a UTF-8 BOM")
        if "\x00" in text:
            check.errors.append(f"{relative} contains a NUL byte")
        for marker in unfinished:
            if re.search(rf"\b{re.escape(marker)}\b", text, re.IGNORECASE):
                check.errors.append(f"{relative} contains unfinished placeholder marker {marker}")
        for pattern, description in private_patterns:
            match = pattern.search(text)
            if match:
                line_number = text.count("\n", 0, match.start()) + 1
                check.errors.append(f"{relative}:{line_number} contains {description}")
        for pattern, description in secret_patterns:
            match = pattern.search(text)
            if match:
                line_number = text.count("\n", 0, match.start()) + 1
                check.errors.append(f"{relative}:{line_number} contains a possible {description}")


MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+['\"][^'\"]*['\"])?\)")


def validate_markdown_links(check: Validation) -> None:
    for markdown_path in sorted(ROOT.rglob("*.md")):
        if ".git" in markdown_path.parts:
            continue
        try:
            text = markdown_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in MARKDOWN_LINK.finditer(text):
            raw_target = match.group(1).strip("<>")
            if raw_target.startswith("#"):
                continue
            parsed = urlsplit(raw_target)
            if parsed.scheme or parsed.netloc:
                continue
            target = unquote(parsed.path)
            if not target:
                continue
            target_path = Path(target)
            line_number = text.count("\n", 0, match.start()) + 1
            relative_source = markdown_path.relative_to(ROOT)
            if target_path.is_absolute():
                check.errors.append(f"{relative_source}:{line_number} uses an absolute local Markdown link")
                continue
            resolved = (markdown_path.parent / target_path).resolve()
            try:
                resolved.relative_to(ROOT)
            except ValueError:
                check.errors.append(f"{relative_source}:{line_number} links outside the package: {raw_target}")
                continue
            if not resolved.exists():
                check.errors.append(f"{relative_source}:{line_number} has a broken local link: {raw_target}")


def validate_workflow(check: Validation) -> None:
    text = check.read_text(".github/workflows/validate.yml")
    if text is None:
        return

    check.require(
        bool(re.search(r"(?m)^permissions:\s*\n  contents:\s*read\s*\n\s*jobs:", text)),
        "Validation workflow must grant only top-level contents: read",
    )
    check.require(
        not bool(re.search(r"(?m)^\s+[A-Za-z_-]+:\s*write\s*$", text)),
        "Validation workflow must not grant write permissions",
    )
    check.require(
        bool(re.search(r'''(?m)^\s+python-version:\s*["']3\.11["']\s*$''', text)),
        "Validation workflow must use Python 3.11",
    )
    check.require(
        bool(re.search(r"(?m)^\s+persist-credentials:\s*false\s*$", text)),
        "Validation workflow checkout must not persist Git credentials",
    )

    uses_pattern = re.compile(r"(?m)^\s*uses:\s*([^@\s]+)@([^\s#]+)(?:\s+#\s*(\S.*))?$")
    found_actions: dict[str, list[tuple[str, str]]] = {}
    for match in uses_pattern.finditer(text):
        action, revision, comment = match.groups()
        found_actions.setdefault(action, []).append((revision, comment or ""))
        check.require(
            bool(re.fullmatch(r"[0-9a-f]{40}", revision)),
            f"Workflow action {action} must be pinned to a full lowercase commit SHA",
        )

    check.require(
        set(found_actions) == set(ACTION_PINS),
        "Validation workflow action set differs from the audited pins; missing: "
        + (", ".join(sorted(set(ACTION_PINS) - set(found_actions))) or "none")
        + "; unexpected: "
        + (", ".join(sorted(set(found_actions) - set(ACTION_PINS))) or "none"),
    )
    for action, (expected_sha, expected_version) in ACTION_PINS.items():
        invocations = found_actions.get(action, [])
        check.require(len(invocations) == 1, f"Workflow must invoke {action} exactly once")
        if len(invocations) != 1:
            continue
        revision, comment = invocations[0]
        check.require(
            revision == expected_sha,
            f"Workflow {action} pin must be {expected_sha}",
        )
        check.require(
            comment.strip() == expected_version,
            f"Workflow {action} pin must have the version comment # {expected_version}",
        )


def main() -> int:
    check = Validation()
    validate_structure(check)
    validate_skill(check)
    validate_reference_routing(check)
    validate_openai_metadata(check)
    validate_license_and_docs(check)
    validate_community_files(check)
    validate_example_statuses(check)
    validate_evals(check)
    validate_hygiene(check)
    validate_markdown_links(check)
    validate_workflow(check)

    if check.errors:
        print(f"Validation failed with {len(check.errors)} error(s):", file=sys.stderr)
        for error in check.errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Validation passed: static package checks completed for task-understanding 0.1.0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
