from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_RELATIVE = Path("scripts") / "deploy.sh"
SCRIPT_SOURCE = REPO_ROOT / SCRIPT_RELATIVE
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
README = REPO_ROOT / "README.md"
SHELL = "/bin/sh"
SHEBANG = "#!/bin/sh"
TARGET_SHA = "a1b2c3d4e5" * 4
PREVIOUS_SHA = "0f1e2d3c4b" * 4
MARKER = "marker-value-that-must-never-be-printed"
COMPOSE_FILE_NAME = "docker-compose.yml"
PROJECT_NAME = "cyrano"
HEALTH_SERVICES = ["app", "worker", "postgres", "redis"]
MIGRATE_SERVICE = "migrate"
EXIT_FAILED = 1
EXIT_BAD_ARGUMENT = 2
EXIT_LOCKED = 3
EXIT_NOT_ON_MAIN = 4
EXIT_PRECONDITION = 5
SECRET_NAMES = (
    "DEPLOY_SSH_KEY",
    "DEPLOY_HOST",
    "DEPLOY_USER",
    "DEPLOY_KNOWN_HOSTS",
    "DEPLOY_PORT",
)
REAL_TOOLS = ("dirname", "cut", "head", "date", "cat")
SKIPPED_DIRECTORIES = frozenset(
    {".git", ".venv", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
)
FORBIDDEN_WORDS = ("adduser", "ufw", "useradd", "authorized_keys", "fail2ban", "sshd_config")

FAKE_GIT = """#!/bin/sh
printf 'git %s\\n' "$*" >> "$FAKE_LOG"
state="$FAKE_STATE"
case "$1" in
rev-parse) cat "$state/current" ;;
fetch) exit "${FAKE_FETCH_EXIT:-0}" ;;
cat-file) exit "${FAKE_CATFILE_EXIT:-0}" ;;
merge-base) exit "${FAKE_ANCESTOR_EXIT:-0}" ;;
checkout)
    for last; do :; done
    case " ${FAKE_CHECKOUT_FAIL_SHAS:-} " in
    *" $last "*) exit 1 ;;
    esac
    printf '%s\\n' "$last" > "$state/current"
    ;;
*) exit 99 ;;
esac
"""
FAKE_DOCKER = """#!/bin/sh
printf 'docker %s\\t%s\\t%s\\t%s\\n' "$*" "${COMPOSE_FILE-unset}" \\
    "${COMPOSE_PROJECT_NAME-unset}" "$PWD" >> "$FAKE_LOG"
state="$FAKE_STATE"
current=$(cat "$state/current")
bad=no
case " ${FAKE_BAD_SHAS:-} " in
*" $current "*) bad=yes ;;
esac
listed() {
    case " $1 " in
    *" $2 "*) return 0 ;;
    esac
    return 1
}
case "$1" in
compose)
    shift
    case "$1" in
    up)
        if [ "$bad" = yes ] && [ "${FAKE_UP_FAILS:-0}" = 1 ]; then
            exit 1
        fi
        exit 0
        ;;
    ps)
        shift
        quiet=no
        name=""
        for argument; do
            if [ "$argument" = "-q" ]; then
                quiet=yes
            fi
            name="$argument"
        done
        if [ "$quiet" = no ]; then
            printf 'NAME STATUS\\n'
            exit 0
        fi
        if [ "$bad" = yes ] && listed "${FAKE_MISSING:-}" "$name"; then
            exit 0
        fi
        printf 'id-%s\\n' "$name"
        exit 0
        ;;
    esac
    ;;
inspect)
    shift
    format="$2"
    service="${3#id-}"
    case "$format" in
    *Health.Status*)
        status=healthy
        if [ "$bad" = yes ] && listed "${FAKE_BAD_SERVICES:-}" "$service"; then
            status="${FAKE_BAD_STATUS:-unhealthy}"
        fi
        if [ -n "${FAKE_STARTING_POLLS:-}" ]; then
            polls=0
            if [ -f "$state/polls.$service" ]; then
                polls=$(cat "$state/polls.$service")
            fi
            polls=$((polls + 1))
            printf '%s\\n' "$polls" > "$state/polls.$service"
            if [ "$polls" -le "$FAKE_STARTING_POLLS" ]; then
                status=starting
            fi
        fi
        printf '%s\\n' "$status"
        exit 0
        ;;
    *ExitCode*)
        code=0
        if [ "$bad" = yes ] && listed "${FAKE_BAD_SERVICES:-}" "$service"; then
            code="${FAKE_MIGRATE_EXIT:-1}"
        fi
        printf '%s\\n' "$code"
        exit 0
        ;;
    esac
    ;;
esac
exit 99
"""
FAKE_FLOCK = """#!/bin/sh
printf 'flock %s\\n' "$*" >> "$FAKE_LOG"
exit "${FAKE_FLOCK_EXIT:-0}"
"""
FAKE_SLEEP = """#!/bin/sh
printf 'sleep %s\\n' "$*" >> "$FAKE_LOG"
exit 0
"""


@dataclass(frozen=True)
class DeployRun:
    result: subprocess.CompletedProcess[str]
    log: list[str]
    root: Path
    state: Path

    @property
    def output(self) -> str:
        return self.result.stdout + self.result.stderr

    @property
    def commands(self) -> list[str]:
        return [line.split("\t")[0] for line in self.log]

    @property
    def returncode(self) -> int:
        return self.result.returncode

    def count(self, prefix: str) -> int:
        return sum(1 for command in self.commands if command.startswith(prefix))

    @property
    def checkouts(self) -> list[str]:
        return [
            command.rsplit(" ", 1)[1]
            for command in self.commands
            if command.startswith("git checkout")
        ]

    @property
    def current(self) -> str:
        return (self.state / "current").read_text().strip()


def write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def build_bin(tmp_path: Path, *, with_flock: bool = True) -> Path:
    bin_dir = tmp_path.resolve() / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "git", FAKE_GIT)
    write_executable(bin_dir / "docker", FAKE_DOCKER)
    write_executable(bin_dir / "sleep", FAKE_SLEEP)
    if with_flock:
        write_executable(bin_dir / "flock", FAKE_FLOCK)
    for tool in REAL_TOOLS:
        found = shutil.which(tool)
        assert found is not None, tool
        (bin_dir / tool).symlink_to(found)
    return bin_dir


def build_repository(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "repo"
    (root / ".git").mkdir(parents=True)
    script = root / SCRIPT_RELATIVE
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT_SOURCE, script)
    shutil.copy2(COMPOSE_FILE, root / COMPOSE_FILE_NAME)
    (root / ".env").write_text(f"POSTGRES_PASSWORD={MARKER}\n")
    return root


def run_deploy(
    tmp_path: Path,
    *,
    arguments: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    with_flock: bool = True,
    start_elsewhere: bool = False,
) -> DeployRun:
    root = build_repository(tmp_path)
    bin_dir = build_bin(tmp_path, with_flock=with_flock)
    state = tmp_path.resolve() / "state"
    state.mkdir()
    (state / "current").write_text(PREVIOUS_SHA + "\n")
    log = tmp_path.resolve() / "calls.log"
    env = {
        "PATH": str(bin_dir),
        "FAKE_LOG": str(log),
        "FAKE_STATE": str(state),
        "DEPLOY_HEALTH_TIMEOUT": "20",
        "DEPLOY_HEALTH_INTERVAL": "5",
        **(extra_env or {}),
    }
    elsewhere = tmp_path.resolve() / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(  # noqa: S603
        [SHELL, str(root / SCRIPT_RELATIVE), *([TARGET_SHA] if arguments is None else arguments)],
        cwd=elsewhere if start_elsewhere else root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = log.read_text().splitlines() if log.exists() else []
    return DeployRun(result=result, log=lines, root=root, state=state)


def bad_target(**extra: str) -> dict[str, str]:
    return {"FAKE_BAD_SHAS": TARGET_SHA, **extra}


def expected_success_commands(sha: str = TARGET_SHA) -> list[str]:
    commands = [
        "flock -n 9",
        "git rev-parse HEAD",
        "git fetch --quiet origin",
        f"git cat-file -e {sha}^{{commit}}",
        f"git merge-base --is-ancestor {sha} origin/main",
        f"git checkout --quiet --detach {sha}",
        "docker compose up -d --build --remove-orphans",
    ]
    for service in HEALTH_SERVICES:
        commands.append(f"docker compose ps -q {service}")
        commands.append(f"docker inspect --format {{{{.State.Health.Status}}}} id-{service}")
    commands.append(f"docker compose ps -a -q {MIGRATE_SERVICE}")
    commands.append(f"docker inspect --format {{{{.State.ExitCode}}}} id-{MIGRATE_SERVICE}")
    commands.append("docker compose ps")
    return commands


def test_success_runs_the_exact_command_sequence(tmp_path: Path) -> None:
    run = run_deploy(tmp_path)

    assert run.returncode == 0, run.output
    assert run.commands == expected_success_commands()
    assert run.current == TARGET_SHA


def test_success_prints_short_sha_utc_time_and_the_table(tmp_path: Path) -> None:
    run = run_deploy(tmp_path)

    assert TARGET_SHA[:7] in run.output
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", run.output)
    assert "NAME STATUS" in run.output


@pytest.mark.parametrize(
    "argument",
    [
        "",
        "abc",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        "a" * 39 + " ",
        "a" * 20 + " " + "a" * 19,
        "a" * 39 + ";",
        "a" * 39 + "$",
        "a" * 39 + "`",
        "a" * 39 + "\n",
        "a" * 30 + ";touch x;",
        "a" * 30 + "$(touch x)",
        "a" * 30 + "`touch x`",
        "a" * 40 + "\n" + "a" * 40,
    ],
)
def test_invalid_argument_exits_two_and_runs_nothing(tmp_path: Path, argument: str) -> None:
    run = run_deploy(tmp_path, arguments=[argument])

    assert run.returncode == EXIT_BAD_ARGUMENT
    assert run.log == []
    assert not (run.root / "x").exists()
    assert len(run.output.strip().splitlines()) == 1


@pytest.mark.parametrize("arguments", [[], [TARGET_SHA, TARGET_SHA], [TARGET_SHA, "extra"]])
def test_wrong_argument_count_exits_two_and_runs_nothing(
    tmp_path: Path, arguments: list[str]
) -> None:
    run = run_deploy(tmp_path, arguments=arguments)

    assert run.returncode == EXIT_BAD_ARGUMENT
    assert run.log == []


def test_a_sha_that_is_not_on_main_exits_four_and_checks_nothing_out(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env={"FAKE_ANCESTOR_EXIT": "1"})

    assert run.returncode == EXIT_NOT_ON_MAIN
    assert run.checkouts == []
    assert run.count("docker") == 0
    assert run.current == PREVIOUS_SHA
    assert len(run.output.strip().splitlines()) == 1


def test_an_unknown_sha_exits_four(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env={"FAKE_CATFILE_EXIT": "1"})

    assert run.returncode == EXIT_NOT_ON_MAIN
    assert run.checkouts == []
    assert run.count("git merge-base") == 0
    assert run.count("docker") == 0


def test_a_held_lock_exits_three_before_git_and_docker(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env={"FAKE_FLOCK_EXIT": "1"})

    assert run.returncode == EXIT_LOCKED
    assert run.commands == ["flock -n 9"]
    assert len(run.output.strip().splitlines()) == 1


def test_the_lock_file_lives_in_the_git_directory(tmp_path: Path) -> None:
    run = run_deploy(tmp_path)

    assert (run.root / ".git" / "deploy.lock").exists()


def test_a_missing_flock_fails_with_a_clear_message(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, with_flock=False)

    assert run.returncode == EXIT_PRECONDITION
    assert "flock" in run.output
    assert run.log == []


def test_a_failed_fetch_exits_five_and_changes_nothing(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env={"FAKE_FETCH_EXIT": "1"})

    assert run.returncode == EXIT_PRECONDITION
    assert run.checkouts == []
    assert run.count("docker") == 0


def test_a_failed_checkout_exits_five_and_starts_nothing(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env={"FAKE_CHECKOUT_FAIL_SHAS": TARGET_SHA})

    assert run.returncode == EXIT_PRECONDITION
    assert run.count("docker") == 0
    assert run.current == PREVIOUS_SHA


@pytest.mark.parametrize("value", ["0", "-1", "x", "1.5", "1 2"])
def test_invalid_wait_settings_are_refused(tmp_path: Path, value: str) -> None:
    for name in ("DEPLOY_HEALTH_TIMEOUT", "DEPLOY_HEALTH_INTERVAL"):
        run = run_deploy(tmp_path / name, extra_env={name: value})

        assert run.returncode == EXIT_PRECONDITION
        assert run.log == []


def test_the_wait_ends_when_a_slow_service_turns_healthy(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env={"FAKE_STARTING_POLLS": "2"})

    assert run.returncode == 0, run.output
    assert run.count("sleep") == 2
    assert run.count("docker compose up") == 1
    assert run.checkouts == [TARGET_SHA]


def test_an_unhealthy_service_triggers_exactly_one_rollback(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env=bad_target(FAKE_BAD_SERVICES="worker"))

    assert run.returncode == EXIT_FAILED
    assert run.checkouts == [TARGET_SHA, PREVIOUS_SHA]
    assert run.count("docker compose up") == 2
    assert run.current == PREVIOUS_SHA
    assert "failed services: worker" in run.output
    assert "rollback came up" in run.output
    assert run.count("sleep") == 0
    assert "manual look" not in run.output
    assert run.commands.index(f"git checkout --quiet --detach {PREVIOUS_SHA}") > run.commands.index(
        "docker compose up -d --build --remove-orphans"
    )


def test_a_migrate_exit_code_other_than_zero_triggers_the_rollback(tmp_path: Path) -> None:
    run = run_deploy(
        tmp_path, extra_env=bad_target(FAKE_BAD_SERVICES=MIGRATE_SERVICE, FAKE_MIGRATE_EXIT="3")
    )

    assert run.returncode == EXIT_FAILED
    assert run.checkouts == [TARGET_SHA, PREVIOUS_SHA]
    assert run.count("docker compose up") == 2
    assert f"failed services: {MIGRATE_SERVICE}" in run.output


def test_a_missing_container_triggers_the_rollback(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env=bad_target(FAKE_MISSING="redis"))

    assert run.returncode == EXIT_FAILED
    assert run.checkouts == [TARGET_SHA, PREVIOUS_SHA]
    assert "failed services: redis" in run.output


def test_a_timeout_triggers_the_rollback(tmp_path: Path) -> None:
    run = run_deploy(
        tmp_path,
        extra_env={
            **bad_target(FAKE_BAD_SERVICES="app", FAKE_BAD_STATUS="starting"),
            "DEPLOY_HEALTH_TIMEOUT": "10",
            "DEPLOY_HEALTH_INTERVAL": "5",
        },
    )

    assert run.returncode == EXIT_FAILED
    assert run.checkouts == [TARGET_SHA, PREVIOUS_SHA]
    assert run.count("docker compose up") == 2
    assert "failed services: app" in run.output
    sleeps = [command for command in run.commands if command.startswith("sleep")]
    assert sleeps == ["sleep 5", "sleep 5"]


def test_a_failing_compose_up_triggers_the_rollback(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env=bad_target(FAKE_UP_FAILS="1"))

    assert run.returncode == EXIT_FAILED
    assert run.checkouts == [TARGET_SHA, PREVIOUS_SHA]
    assert run.count("docker inspect") == len(HEALTH_SERVICES) + 1
    assert "compose-up" in run.output


def test_a_failed_rollback_asks_for_a_manual_look_and_never_loops(tmp_path: Path) -> None:
    run = run_deploy(
        tmp_path,
        extra_env={
            "FAKE_BAD_SHAS": f"{TARGET_SHA} {PREVIOUS_SHA}",
            "FAKE_BAD_SERVICES": "worker",
        },
    )

    assert run.returncode == EXIT_FAILED
    assert run.checkouts == [TARGET_SHA, PREVIOUS_SHA]
    assert run.count("docker compose up") == 2
    assert "needs a manual look" in run.output


def test_a_failed_rollback_checkout_asks_for_a_manual_look(tmp_path: Path) -> None:
    run = run_deploy(
        tmp_path,
        extra_env=bad_target(FAKE_BAD_SERVICES="app", FAKE_CHECKOUT_FAIL_SHAS=PREVIOUS_SHA),
    )

    assert run.returncode == EXIT_FAILED
    assert run.count("docker compose up") == 1
    assert "needs a manual look" in run.output


def test_the_override_file_is_never_used(tmp_path: Path) -> None:
    overriding = {"COMPOSE_FILE": "docker-compose.yml:docker-compose.override.yml"}
    run = run_deploy(tmp_path, extra_env={**overriding, **bad_target(FAKE_BAD_SERVICES="app")})

    docker_lines = [line.split("\t") for line in run.log if line.startswith("docker")]
    assert docker_lines
    assert {fields[1] for fields in docker_lines} == {COMPOSE_FILE_NAME}
    assert {fields[2] for fields in docker_lines} == {PROJECT_NAME}
    assert all("override" not in "".join(fields[:3]) for fields in docker_lines)
    assert "override" not in run.output


def test_the_project_name_can_be_overridden_by_one_named_variable(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env={"DEPLOY_PROJECT_NAME": "other"})

    docker_lines = [line.split("\t") for line in run.log if line.startswith("docker")]
    assert {fields[2] for fields in docker_lines} == {"other"}


def test_no_secret_looking_value_appears_in_the_output(tmp_path: Path) -> None:
    secrets = {
        name: MARKER
        for name in ("POSTGRES_PASSWORD", "BOT_TOKEN", "DEEPSEEK_API_KEY", "DEPLOY_SSH_KEY")
    }
    for index, extra in enumerate(
        ({}, bad_target(FAKE_BAD_SERVICES="app"), {"FAKE_BAD_SHAS": f"{TARGET_SHA} {PREVIOUS_SHA}"})
    ):
        env = {**secrets, **extra}
        if "FAKE_BAD_SHAS" in env:
            env["FAKE_BAD_SERVICES"] = "app"
        run = run_deploy(tmp_path / str(index), extra_env=env)

        assert MARKER not in run.output
        assert MARKER not in "\n".join(run.log)


def test_it_works_when_started_from_another_directory(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, start_elsewhere=True)

    assert run.returncode == 0, run.output
    assert run.commands == expected_success_commands()
    docker_lines = [line.split("\t") for line in run.log if line.startswith("docker")]
    assert {Path(fields[3]) for fields in docker_lines} == {run.root}


def test_the_recorded_commands_never_reset_clean_or_prune(tmp_path: Path) -> None:
    runs = [
        run_deploy(tmp_path / "ok"),
        run_deploy(tmp_path / "bad", extra_env=bad_target(FAKE_BAD_SERVICES="app")),
    ]

    for run in runs:
        joined = "\n".join(run.commands)
        for word in ("reset", "clean", "prune", "rmi", " rm ", "down", "volume"):
            assert word not in joined, word


def test_the_untracked_files_stay_untouched(tmp_path: Path) -> None:
    run = run_deploy(tmp_path, extra_env=bad_target(FAKE_BAD_SERVICES="app"))

    assert (run.root / ".env").read_text() == f"POSTGRES_PASSWORD={MARKER}\n"


def script_lines() -> list[str]:
    return SCRIPT_SOURCE.read_text().splitlines()


def test_the_script_is_an_executable_posix_sh_script() -> None:
    assert script_lines()[0] == SHEBANG
    assert os.access(SCRIPT_SOURCE, os.X_OK)
    assert SCRIPT_SOURCE.stat().st_mode & stat.S_IXUSR
    assert "set -eu" in script_lines()


def test_the_script_and_the_workflow_have_no_comments() -> None:
    for path in (SCRIPT_SOURCE, WORKFLOW):
        lines = path.read_text().splitlines()
        offenders = [
            line for index, line in enumerate(lines) if line.lstrip().startswith("#") and index
        ]
        assert not offenders, (path.name, offenders)
        if path == WORKFLOW:
            assert not lines[0].startswith("#")


def test_the_script_never_uses_forbidden_commands() -> None:
    text = SCRIPT_SOURCE.read_text()

    assert re.search(r"set\s+-\w*x", text) is None
    assert re.search(r"\bsh\s+-\w*x", text) is None
    for word in ("git reset", "git clean", "prune", "eval ", "printenv", "compose config"):
        assert word not in text, word
    assert re.search(r"(?<![\w-])env(?![\w-])", text) is None
    assert "override" not in text
    assert ".env" not in text
    assert re.search(r"PASSWORD|SECRET|TOKEN|API_KEY|API_HASH|DEEPSEEK|SSH_KEY", text) is None


def test_the_script_exports_the_compose_file_and_a_project_name() -> None:
    text = SCRIPT_SOURCE.read_text()

    assert 'export COMPOSE_FILE="$COMPOSE_FILE_NAME"' in text
    assert f'COMPOSE_FILE_NAME="{COMPOSE_FILE_NAME}"' in text
    assert 'export COMPOSE_PROJECT_NAME="$PROJECT_NAME"' in text
    assert f'PROJECT_NAME="${{DEPLOY_PROJECT_NAME:-{PROJECT_NAME}}}"' in text


def compose_services() -> dict[str, str]:
    text = COMPOSE_FILE.read_text()
    section = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    blocks: dict[str, list[str]] = {}
    current = ""
    for line in section.splitlines():
        match = re.fullmatch(r"  ([a-z][\w-]*):", line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def test_the_healthcheck_services_match_the_compose_file() -> None:
    declared = re.search(r'^HEALTHCHECK_SERVICES="([^"]*)"$', SCRIPT_SOURCE.read_text(), re.M)
    assert declared is not None
    in_compose = [
        name
        for name, block in compose_services().items()
        if re.search(r"^    healthcheck:$", block, re.M)
    ]

    assert sorted(declared.group(1).split()) == sorted(in_compose)
    assert sorted(in_compose) == sorted(HEALTH_SERVICES)


def test_the_migrate_service_exists_and_has_no_healthcheck() -> None:
    services = compose_services()
    migrate = re.search(r'^MIGRATE_SERVICE="([^"]*)"$', SCRIPT_SOURCE.read_text(), re.M)

    assert migrate is not None
    assert migrate.group(1) == MIGRATE_SERVICE
    assert MIGRATE_SERVICE in services
    assert "healthcheck:" not in services[MIGRATE_SERVICE]


def test_the_exit_codes_in_the_script_are_the_documented_ones() -> None:
    text = SCRIPT_SOURCE.read_text()
    codes = dict(re.findall(r"^(EXIT_\w+)=(\d+)$", text, re.M))

    assert codes == {
        "EXIT_FAILED": str(EXIT_FAILED),
        "EXIT_BAD_ARGUMENT": str(EXIT_BAD_ARGUMENT),
        "EXIT_LOCKED": str(EXIT_LOCKED),
        "EXIT_NOT_ON_MAIN": str(EXIT_NOT_ON_MAIN),
        "EXIT_PRECONDITION": str(EXIT_PRECONDITION),
    }


def workflow_text() -> str:
    return WORKFLOW.read_text()


def indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parent_line(lines: list[str], index: int) -> str:
    level = indentation(lines[index])
    for position in range(index - 1, -1, -1):
        line = lines[position]
        if line.strip() and indentation(line) < level:
            return line.strip()
    return ""


def ci_workflow_name() -> str:
    match = re.search(r"^name:\s*(.+?)\s*$", CI_WORKFLOW.read_text(), re.M)
    assert match is not None
    return match.group(1)


def test_the_trigger_is_the_completed_ci_workflow_on_main() -> None:
    text = workflow_text()

    assert re.search(
        rf"workflow_run:\n    workflows: \[{re.escape(ci_workflow_name())}\]\n"
        r"    types: \[completed\]\n    branches: \[main\]\n",
        text,
    )


def test_the_manual_trigger_requires_a_sha_input() -> None:
    text = workflow_text()

    assert re.search(
        r"workflow_dispatch:\n    inputs:\n      sha:\n(?:        .*\n)*?        required: true\n",
        text,
    )


def test_no_pull_request_trigger_exists() -> None:
    text = workflow_text()

    assert "pull_request" not in text
    assert re.search(r"^  push:", text, re.M) is None


def test_the_job_runs_only_after_a_successful_push_to_main_or_manually() -> None:
    text = workflow_text()

    assert "github.event_name == 'workflow_dispatch'" in text
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "github.event.workflow_run.event == 'push'" in text
    assert "github.event.workflow_run.head_branch == 'main'" in text
    assert "github.event.workflow_run.head_sha || inputs.sha" in text


def test_the_job_uses_the_production_environment_and_one_serial_group() -> None:
    text = workflow_text()

    assert re.search(r"^    environment: production$", text, re.M)
    assert re.search(
        r"^concurrency:\n  group: deploy-production\n  cancel-in-progress: false\n", text, re.M
    )
    assert len(re.findall(r"^  deploy:$", text, re.M)) == 1
    assert len(re.findall(r"^  [a-z][\w-]*:$", text.split("\njobs:\n", 1)[1], re.M)) == 1


def test_permissions_are_read_only() -> None:
    text = workflow_text()

    assert re.search(r"^permissions:\n  contents: read\n", text, re.M)
    assert re.search(r":\s*write\b", text) is None
    assert "write-all" not in text


def test_the_job_checks_out_nothing_and_uses_no_action() -> None:
    text = workflow_text()

    assert "actions/checkout" not in text
    assert re.search(r"^\s*-?\s*uses:", text, re.M) is None


def test_secrets_are_referenced_only_in_env_blocks() -> None:
    lines = workflow_text().splitlines()
    referenced = [index for index, line in enumerate(lines) if "secrets." in line]

    assert {
        name for index in referenced for name in SECRET_NAMES if f"secrets.{name}" in lines[index]
    } == set(SECRET_NAMES)
    for index in referenced:
        assert re.fullmatch(r"\s+[A-Z_]+: \$\{\{ secrets\.[A-Z_]+ \}\}", lines[index]), lines[index]
        assert parent_line(lines, index) == "env:", lines[index]


def test_no_expression_appears_inside_a_run_body() -> None:
    lines = workflow_text().splitlines()
    inside = False
    run_level = 0
    for line in lines:
        if inside and line.strip() and indentation(line) <= run_level:
            inside = False
        if inside:
            assert "${{" not in line, line
        if re.match(r"\s+run:", line):
            assert "${{" not in line, line
            inside = line.rstrip().endswith("|")
            run_level = indentation(line)


def test_no_shell_tracing_and_no_echo_of_a_secret() -> None:
    text = workflow_text()

    assert re.search(r"set\s+-\w*x", text) is None
    assert re.search(r"(?:bash|sh)\s+-\w*x", text) is None
    assert "ACTIONS_STEP_DEBUG" not in text
    for line in text.splitlines():
        if re.search(r"\becho\b", line):
            assert re.search(r"\$\{?(DEPLOY_|key_file|hosts_file)", line) is None, line
        if re.search(r"\$\{?(DEPLOY_SSH_KEY|DEPLOY_KNOWN_HOSTS)\b", line):
            assert line.strip().startswith("printf '%s\\n'"), line
            assert re.search(r'> "\$(key_file|hosts_file)"$', line), line


def test_the_key_file_has_mode_600_and_is_removed_by_an_always_step() -> None:
    text = workflow_text()

    assert 'chmod 600 "$key_file"' in text
    assert "umask 077" in text
    assert 'key_file="$RUNNER_TEMP/deploy_key"' in text
    assert re.search(
        r"      - name: Remove the key\n        if: always\(\)\n"
        r'        run: rm -f "\$RUNNER_TEMP/deploy_key" "\$RUNNER_TEMP/deploy_known_hosts"\n',
        text,
    )


def test_ssh_is_strict_and_runs_the_script_with_the_sha() -> None:
    text = workflow_text()

    assert "-o StrictHostKeyChecking=yes" in text
    assert "-o BatchMode=yes" in text
    assert '-o UserKnownHostsFile="$hosts_file"' in text
    assert '-i "$key_file"' in text
    assert '-p "${DEPLOY_PORT:-22}"' in text
    assert '"$DEPLOY_USER@$DEPLOY_HOST"' in text
    assert '"$DEPLOY_PATH/scripts/deploy.sh $DEPLOY_SHA"' in text
    assert "StrictHostKeyChecking=no" not in text
    assert "ssh-keyscan" not in text
    assert re.search(r"^env:\n  DEPLOY_PATH: /opt/cyrano\n", text, re.M)


def test_the_sha_is_validated_before_ssh_is_called() -> None:
    text = workflow_text()

    assert text.index("*[!0-9a-f]*") < text.index("ssh \\")
    assert '"${#DEPLOY_SHA}" -ne 40' in text


def readme_section() -> str:
    text = README.read_text()
    match = re.search(r"^## Deployment\n(.*?)(?=^## )", text, re.M | re.S)
    assert match is not None, "README.md has no Deployment section"
    return match.group(1)


def test_the_readme_has_a_short_deployment_section() -> None:
    section = readme_section()
    lines = [line for line in section.splitlines() if line.strip()]

    assert 0 < len(lines) <= 12
    assert "scripts/deploy.sh" in section
    assert "deploy.yml" in section
    assert "production" in section
    assert "/opt/cyrano" in section
    assert "docker-compose.yml" in section
    for name in SECRET_NAMES:
        assert name in section, name


def test_the_readme_section_lists_every_exit_code() -> None:
    section = readme_section()
    codes = re.findall(r"^EXIT_\w+=(\d+)$", SCRIPT_SOURCE.read_text(), re.M)

    assert len(codes) == 5
    for code in ["0", *codes]:
        assert re.search(rf"(?<![\w./-]){code}(?![\w./-])", section), code


def test_the_readme_layout_lists_the_script_and_the_workflow() -> None:
    layout = README.read_text().split("## Repository layout", 1)[1].split("\n## ", 1)[0]

    assert "scripts/" in layout
    assert "deploy.sh" in layout
    assert "deploy.yml" in layout


def markdown_files() -> list[Path]:
    found: list[Path] = []
    for directory, names, files in os.walk(REPO_ROOT):
        names[:] = [name for name in names if name not in SKIPPED_DIRECTORIES]
        found.extend(Path(directory) / name for name in files if name.lower().endswith(".md"))
    return found


def test_the_repository_holds_no_deployment_guide() -> None:
    files = markdown_files()

    assert files
    assert not (REPO_ROOT / ".claude" / "docs" / "deployment.md").exists()
    assert [path for path in files if "deploy" in path.name.lower()] == []
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for word in FORBIDDEN_WORDS:
            assert re.search(rf"(?<![\w-]){word}(?![\w-])", text) is None, (path.name, word)
