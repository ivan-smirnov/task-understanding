#!/usr/bin/env python3
"""Validate the public task-understanding skill package using only stdlib."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = {
    Path("SKILL.md"),
    Path("agents/openai.yaml"),
    Path("README.md"),
    Path("LICENSE"),
    Path("CHANGELOG.md"),
    Path("examples/new-brief.md"),
    Path("examples/scope-change.md"),
    Path("evals/evals.json"),
    Path("scripts/validate.py"),
    Path(".github/workflows/validate.yml"),
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
    "partial-client-reply-is-not-confirmation",
    "post-confirmation-scope-change",
    "client-material-is-untrusted-input",
    "no-external-send-or-task-write",
    "missing-storage-location",
    "nonexistent-project-folder-needs-consent",
}
ALLOWED_LIVE_STATUSES = {
    "диагностика",
    "ждём ответы заказчика",
    "ждём решения пользователя по допущению",
    "ждём пробный материал и реакцию",
    "ждём внешнюю проверку",
    "готово к согласованию",
    "согласовано — можно выполнять",
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


def validate_skill(check: Validation) -> None:
    text = check.read_text("SKILL.md")
    if text is None:
        return

    line_count = len(text.splitlines())
    check.require(line_count < 500, f"SKILL.md must be shorter than 500 lines; found {line_count}")

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
    check.require("Понимание задачи.md" in body, "SKILL.md must define the live output file Понимание задачи.md")


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
            "https://t.me/shortnclear" in readme,
            "README.md must link to the Telegram feedback channel",
        )

    changelog = check.read_text("CHANGELOG.md")
    if changelog is not None:
        check.require("0.1.0" in changelog, "CHANGELOG.md must include version 0.1.0")


def validate_example_statuses(check: Validation) -> None:
    status_pattern = re.compile(r"^\s*-\s+Статус:\s*(.+?)\s*$")
    for relative_path in (Path("examples/new-brief.md"), Path("examples/scope-change.md")):
        text = check.read_text(relative_path)
        if text is None:
            continue
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
    ]

    for path, text in iter_package_text(check):
        relative = path.relative_to(ROOT)
        for marker in unfinished:
            if re.search(rf"\b{re.escape(marker)}\b", text, re.IGNORECASE):
                check.errors.append(f"{relative} contains unfinished placeholder marker {marker}")
        for pattern, description in private_patterns:
            match = pattern.search(text)
            if match:
                line_number = text.count("\n", 0, match.start()) + 1
                check.errors.append(f"{relative}:{line_number} contains {description}")


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


def main() -> int:
    check = Validation()
    validate_structure(check)
    validate_skill(check)
    validate_openai_metadata(check)
    validate_license_and_docs(check)
    validate_example_statuses(check)
    validate_evals(check)
    validate_hygiene(check)
    validate_markdown_links(check)

    if check.errors:
        print(f"Validation failed with {len(check.errors)} error(s):", file=sys.stderr)
        for error in check.errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Validation passed: static package checks completed for task-understanding 0.1.0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
