# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from pathlib import Path

import pytest

from pants.backend.python import target_types_rules
from pants.backend.python.lint.ruff import skip_field
from pants.backend.python.lint.ruff.rules import RuffFixRequest, RuffLintRequest
from pants.backend.python.lint.ruff.rules import rules as ruff_rules
from pants.backend.python.lint.ruff.subsystem import RuffFieldSet
from pants.backend.python.lint.ruff.subsystem import rules as ruff_subsystem_rules
from pants.backend.python.target_types import PythonSourcesGeneratorTarget
from pants.core.goals.fix import FixResult
from pants.core.goals.lint import LintResult
from pants.core.util_rules import config_files
from pants.core.util_rules.partitions import _EmptyMetadata
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.engine.addresses import Address
from pants.engine.fs import CreateDigest, FileContent
from pants.engine.internals.native_engine import Digest, Snapshot
from pants.engine.target import Target
from pants.testutil.python_interpreter_selection import all_major_minor_python_versions
from pants.testutil.rule_runner import QueryRule, RuleRunner

GOOD_FILE = 'a = "string without any placeholders"'
BAD_FILE = 'a = f"string without any placeholders"'


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        rules=[
            *ruff_rules(),
            *skip_field.rules(),
            *ruff_subsystem_rules(),
            *config_files.rules(),
            *target_types_rules.rules(),
            QueryRule(FixResult, [RuffFixRequest.Batch]),
            QueryRule(LintResult, [RuffLintRequest.Batch]),
            QueryRule(SourceFiles, (SourceFilesRequest,)),
        ],
        target_types=[PythonSourcesGeneratorTarget],
    )


def run_ruff(
    rule_runner: RuleRunner,
    targets: list[Target],
    *,
    extra_args: list[str] | None = None,
) -> tuple[FixResult, LintResult]:
    args = ["--backend-packages=pants.backend.python.lint.ruff", *(extra_args or ())]
    rule_runner.set_options(args, env_inherit={"PATH", "PYENV_ROOT", "HOME"})

    field_sets = [RuffFieldSet.create(tgt) for tgt in targets]
    source_reqs = [SourceFilesRequest(field_set.source for field_set in field_sets)]
    input_sources = rule_runner.request(SourceFiles, source_reqs)

    fix_result = rule_runner.request(
        FixResult,
        [
            RuffFixRequest.Batch(
                "",
                input_sources.snapshot.files,
                partition_metadata=None,
                snapshot=input_sources.snapshot,
            ),
        ],
    )
    lint_result = rule_runner.request(
        LintResult,
        [
            RuffLintRequest.Batch(
                "",
                tuple(field_sets),
                partition_metadata=_EmptyMetadata(),
            ),
        ],
    )

    return fix_result, lint_result


def get_snapshot(rule_runner: RuleRunner, source_files: dict[str, str]) -> Snapshot:
    files = [FileContent(path, content.encode()) for path, content in source_files.items()]
    digest = rule_runner.request(Digest, [CreateDigest(files)])
    return rule_runner.request(Snapshot, [digest])


@pytest.mark.platform_specific_behavior
@pytest.mark.parametrize(
    "major_minor_interpreter",
    all_major_minor_python_versions(["CPython>=3.7,<4"]),
)
def test_passing(rule_runner: RuleRunner, major_minor_interpreter: str) -> None:
    rule_runner.write_files({"f.py": GOOD_FILE, "BUILD": "python_sources(name='t')"})
    tgt = rule_runner.get_target(Address("", target_name="t", relative_file_path="f.py"))
    fix_result, lint_result = run_ruff(
        rule_runner,
        [tgt],
        extra_args=[f"--python-interpreter-constraints=['=={major_minor_interpreter}.*']"],
    )
    assert lint_result.exit_code == 0
    assert fix_result.stderr == ""
    assert fix_result.stdout == ""
    assert not fix_result.did_change
    assert fix_result.output == get_snapshot(rule_runner, {"f.py": GOOD_FILE})


def test_failing(rule_runner: RuleRunner) -> None:
    rule_runner.write_files({"f.py": BAD_FILE, "BUILD": "python_sources(name='t')"})
    tgt = rule_runner.get_target(Address("", target_name="t", relative_file_path="f.py"))
    fix_result, lint_result = run_ruff(rule_runner, [tgt])
    assert lint_result.exit_code == 1
    assert fix_result.stdout == "Found 1 error (1 fixed, 0 remaining).\n"
    assert fix_result.stderr == ""
    assert fix_result.did_change
    assert fix_result.output == get_snapshot(rule_runner, {"f.py": GOOD_FILE})


def test_multiple_targets(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "good.py": GOOD_FILE,
            "bad.py": BAD_FILE,
            "BUILD": "python_sources(name='t')",
        }
    )
    tgts = [
        rule_runner.get_target(Address("", target_name="t", relative_file_path="good.py")),
        rule_runner.get_target(Address("", target_name="t", relative_file_path="bad.py")),
    ]
    fix_result, lint_result = run_ruff(rule_runner, tgts)
    assert lint_result.exit_code == 1
    assert fix_result.output == get_snapshot(
        rule_runner, {"good.py": GOOD_FILE, "bad.py": GOOD_FILE}
    )
    assert fix_result.did_change is True


@pytest.mark.parametrize(
    "file_path,config_path,extra_args,should_change",
    (
        [Path("f.py"), Path("pyproject.toml"), [], False],
        [Path("f.py"), Path("ruff.toml"), [], False],
        [Path("custom/f.py"), Path("custom/ruff.toml"), [], False],
        [Path("f.py"), Path("custom/ruff.toml"), ["--ruff-config=custom/ruff.toml"], False],
        [Path("f.py"), Path("custom/ruff.toml"), [], True],
    ),
)
def test_config_file(
    rule_runner: RuleRunner,
    file_path: Path,
    config_path: Path,
    extra_args: list[str],
    should_change: bool,
) -> None:
    hierarchy = "[tool.ruff]\n" if config_path.stem == "pyproject" else ""
    rule_runner.write_files(
        {
            file_path: BAD_FILE,
            file_path.parent / "BUILD": "python_sources()",
            config_path: f'{hierarchy}ignore = ["F541"]',
        }
    )
    spec_path = str(file_path.parent).replace(".", "")
    rel_file_path = file_path.relative_to(*file_path.parts[:1]) if spec_path else file_path
    addr = Address(spec_path, relative_file_path=str(rel_file_path))
    tgt = rule_runner.get_target(addr)
    fix_result, lint_result = run_ruff(rule_runner, [tgt], extra_args=extra_args)
    assert lint_result.exit_code == bool(should_change)
    assert fix_result.did_change is should_change
