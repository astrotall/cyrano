from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_RELATIVE = Path(".githooks") / "pre-commit"
HOOK_SOURCE = REPO_ROOT / HOOK_RELATIVE
SHELL = "/bin/sh"
SHEBANG = "#!/bin/sh"
SKIP_VARIABLE = "CYRANO_HOOK_SKIP_TESTS"
DATABASE_VARIABLE = "TEST_DATABASE_URL"
SETUP_TARGET = "setup-hooks"
SETUP_COMMAND = "git config core.hooksPath .githooks"
CHECK_TARGET = "check"
TEST_TARGET = "test"
FAKE_MAKE = f"""#!/bin/sh
printf '%s\\t%s\\n' "$PWD" "$*" >> "$FAKE_MAKE_LOG"
case "$1" in
{CHECK_TARGET}) exit "${{FAKE_MAKE_CHECK_EXIT:-0}}" ;;
{TEST_TARGET}) exit "${{FAKE_MAKE_TEST_EXIT:-0}}" ;;
esac
exit 0
"""
FORBIDDEN_COMMANDS = (
    "curl",
    "wget",
    "git",
    "pip",
    "pip3",
    "uv",
    "npm",
    "ssh",
    "scp",
    "rsync",
    "nc",
    "ncat",
    "telnet",
    "ftp",
    "docker",
    "ruff",
)
DATABASE_URL = "postgresql+asyncpg://fake@127.0.0.1:5432/postgres"


@dataclass(frozen=True)
class HookRun:
    result: subprocess.CompletedProcess[str]
    calls: list[tuple[Path, str]]
    root: Path
    before: dict[str, tuple[str, int, int]]
    after: dict[str, tuple[str, int, int]]

    @property
    def targets(self) -> list[str]:
        return [target for _, target in self.calls]

    @property
    def output(self) -> str:
        return self.result.stdout + self.result.stderr


def build_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path.resolve() / "repo"
    hook = root / HOOK_RELATIVE
    hook.parent.mkdir(parents=True)
    shutil.copy2(HOOK_SOURCE, hook)
    (root / "src").mkdir()
    (root / "src" / "module.py").write_text("x=1\n")
    (root / "notes.md").write_text("fake notes\n")
    bin_dir = tmp_path.resolve() / "bin"
    bin_dir.mkdir()
    fake_make = bin_dir / "make"
    fake_make.write_text(FAKE_MAKE)
    fake_make.chmod(0o755)
    return root, hook, bin_dir


def run_hook(
    tmp_path: Path,
    *,
    check_exit: int = 0,
    test_exit: int = 0,
    extra_env: dict[str, str] | None = None,
    start_in: str = "root",
) -> HookRun:
    root, hook, bin_dir = build_workspace(tmp_path)
    log = tmp_path.resolve() / "make.log"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_MAKE_LOG": str(log),
        "FAKE_MAKE_CHECK_EXIT": str(check_exit),
        "FAKE_MAKE_TEST_EXIT": str(test_exit),
        **(extra_env or {}),
    }
    if start_in == "root":
        cwd = root
        argument = str(HOOK_RELATIVE)
    elif start_in == "subdirectory":
        cwd = root / "src"
        argument = str(Path("..") / HOOK_RELATIVE)
    else:
        cwd = tmp_path.resolve()
        argument = str(hook)
    before = snapshot(root)
    result = subprocess.run(  # noqa: S603
        [SHELL, argument],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls: list[tuple[Path, str]] = []
    if log.exists():
        for line in log.read_text().splitlines():
            directory, _, target = line.partition("\t")
            calls.append((Path(directory), target))
    return HookRun(result=result, calls=calls, root=root, before=before, after=snapshot(root))


def snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    entries: dict[str, tuple[str, int, int]] = {}
    for path in sorted(root.rglob("*")):
        info = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        mode = stat.S_IMODE(info.st_mode)
        entries[str(path.relative_to(root))] = (digest, mode, info.st_mtime_ns)
    return entries


def test_runs_check_then_test(tmp_path: Path) -> None:
    run = run_hook(tmp_path)

    assert run.result.returncode == 0
    assert run.targets == [CHECK_TARGET, TEST_TARGET]


def test_failing_check_stops_before_test(tmp_path: Path) -> None:
    run = run_hook(tmp_path, check_exit=3)

    assert run.result.returncode == 3
    assert run.targets == [CHECK_TARGET]


def test_failing_test_exits_non_zero(tmp_path: Path) -> None:
    run = run_hook(tmp_path, test_exit=5)

    assert run.result.returncode == 5
    assert run.targets == [CHECK_TARGET, TEST_TARGET]


def test_skip_variable_set_to_one_skips_only_tests(tmp_path: Path) -> None:
    run = run_hook(tmp_path, extra_env={SKIP_VARIABLE: "1"})

    assert run.result.returncode == 0
    assert run.targets == [CHECK_TARGET]
    assert "skipped on purpose" in run.output


def test_skip_variable_does_not_skip_check(tmp_path: Path) -> None:
    run = run_hook(tmp_path, check_exit=2, extra_env={SKIP_VARIABLE: "1"})

    assert run.result.returncode == 2
    assert run.targets == [CHECK_TARGET]


@pytest.mark.parametrize("value", ["0", "", "yes", "true", "11", " 1", "1 "])
def test_other_values_do_not_skip_tests(tmp_path: Path, value: str) -> None:
    run = run_hook(tmp_path, extra_env={SKIP_VARIABLE: value})

    assert run.targets == [CHECK_TARGET, TEST_TARGET]
    assert "skipped" not in run.output


def test_unset_variable_does_not_skip_tests(tmp_path: Path) -> None:
    run = run_hook(tmp_path)

    assert TEST_TARGET in run.targets
    assert "skipped" not in run.output


@pytest.mark.parametrize("start_in", ["root", "subdirectory", "elsewhere"])
def test_make_runs_in_the_repository_root_wherever_it_started(
    tmp_path: Path, start_in: str
) -> None:
    run = run_hook(tmp_path, start_in=start_in)

    assert run.result.returncode == 0
    assert run.targets == [CHECK_TARGET, TEST_TARGET]
    assert {directory for directory, _ in run.calls} == {run.root}


def test_hint_appears_when_database_url_is_unset(tmp_path: Path) -> None:
    run = run_hook(tmp_path)

    assert DATABASE_VARIABLE in run.output
    assert run.targets == [CHECK_TARGET, TEST_TARGET]
    assert run.output.index(DATABASE_VARIABLE) < run.output.index("running make test")


def test_hint_is_absent_when_database_url_is_set(tmp_path: Path) -> None:
    run = run_hook(tmp_path, extra_env={DATABASE_VARIABLE: DATABASE_URL})

    assert DATABASE_VARIABLE not in run.output
    assert run.targets == [CHECK_TARGET, TEST_TARGET]


def test_hint_is_absent_when_tests_are_skipped(tmp_path: Path) -> None:
    run = run_hook(tmp_path, extra_env={SKIP_VARIABLE: "1"})

    assert DATABASE_VARIABLE not in run.output


def test_each_step_announces_itself(tmp_path: Path) -> None:
    run = run_hook(tmp_path)

    assert "running make check" in run.output
    assert "running make test" in run.output
    assert run.output.index("running make check") < run.output.index("running make test")


def test_hook_does_not_modify_the_tree(tmp_path: Path) -> None:
    run = run_hook(tmp_path)

    assert run.result.returncode == 0
    assert run.before
    assert run.after == run.before


def test_hook_does_not_modify_the_tree_when_a_step_fails(tmp_path: Path) -> None:
    run = run_hook(tmp_path, check_exit=1)

    assert run.result.returncode == 1
    assert run.after == run.before


def test_script_contains_no_network_or_other_commands() -> None:
    text = HOOK_SOURCE.read_text()

    for command in FORBIDDEN_COMMANDS:
        assert re.search(rf"(?<![\w-]){re.escape(command)}(?![\w-])", text) is None, command
    assert "pip install" not in text
    assert "git push" not in text


def test_script_runs_only_make() -> None:
    lines = [
        line.strip()
        for line in HOOK_SOURCE.read_text().splitlines()
        if line.strip().startswith("make ")
    ]

    assert lines == [f"make {CHECK_TARGET}", f"make {TEST_TARGET}"]


def test_hook_file_is_an_executable_sh_script() -> None:
    assert HOOK_SOURCE.is_file()
    assert HOOK_SOURCE.read_text().splitlines()[0] == SHEBANG
    assert os.access(HOOK_SOURCE, os.X_OK)
    assert HOOK_SOURCE.stat().st_mode & stat.S_IXUSR


def test_hook_uses_strict_shell_mode() -> None:
    assert "set -eu" in HOOK_SOURCE.read_text().splitlines()


def test_makefile_has_the_setup_hooks_target() -> None:
    lines = (REPO_ROOT / "Makefile").read_text().splitlines()

    assert f"{SETUP_TARGET}:" in lines
    assert lines[lines.index(f"{SETUP_TARGET}:") + 1] == f"\t{SETUP_COMMAND}"
    phony = next(line for line in lines if line.startswith(".PHONY:"))
    assert SETUP_TARGET in phony.split()
