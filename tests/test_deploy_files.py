import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import time


DEPLOY = Path("deploy")


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_linux_commands(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    units_dir = tmp_path / "protected-units"
    proc_dir = tmp_path / "proc"
    bin_dir.mkdir()
    units_dir.mkdir()
    proc_dir.mkdir()

    services = (
        "ssh.service",
        "sing-box.service",
        "cdn-subscription.service",
        "friend-clash-sub.service",
        "share-100gb-sub.service",
    )
    for service in services:
        (units_dir / service).write_text(f"unit={service}\n")

    ports = (22, 22222, 2082, 2086, 2095, 2052, 8880)
    for port in ports:
        pid_dir = proc_dir / str(100000 + port)
        pid_dir.mkdir()
        (pid_dir / "cgroup").write_text(f"0::/system.slice/listener-{port}.service\n")
    replacement = proc_dir / "999999"
    replacement.mkdir()
    (replacement / "cgroup").write_text("0::/system.slice/replacement.service\n")

    _write_executable(
        bin_dir / "id",
        "#!/bin/sh\nif [ \"${1:-}\" = -u ]; then echo 0; fi\nexit 0\n",
    )
    _write_executable(
        bin_dir / "systemctl",
        """#!/bin/sh
case "${1:-}" in
  is-active) echo active ;;
  show) for service do :; done; echo "$FAKE_UNITS_DIR/$service" ;;
  daemon-reload) : ;;
  *) exit 90 ;;
esac
""",
    )
    _write_executable(
        bin_dir / "ss",
        """#!/bin/sh
args="$*"
case "$args" in
  *":1201"*|*":1200"*)
    port=1200
    case "$args" in *":1201"*) port=1201 ;; esac
    if [ "${SS_OCCUPIED_PROJECT_PORT:-}" = "$port" ]; then
      echo "LISTEN 0 128 127.0.0.1:$port 0.0.0.0:*"
    elif [ -n "${RUNNER_EVENTS:-}" ] && [ -s "$RUNNER_EVENTS" ]; then
      pid="$(sed -n 's/^started //p' "$RUNNER_EVENTS" | head -n 1)"
      pid="${SS_LISTENER_PID_PREFIX:-}$pid"
      echo "LISTEN 0 511 127.0.0.1:$port 0.0.0.0:* users:((\"node\",pid=$pid,fd=19))"
    fi
    exit 0
    ;;
esac
for port in 22 22222 2082 2086 2095 2052 8880; do
  case "$args" in
    *"sport = :$port")
      pid=$((100000 + port))
      if [ "${SS_REPLACED_PORT:-}" = "$port" ]; then pid=999999; fi
      echo "LISTEN 0 128 127.0.0.1:$port 0.0.0.0:* users:((\"listener-$port\",pid=$pid,fd=3))"
      exit 0
      ;;
  esac
done
exit 1
""",
    )
    _write_executable(
        bin_dir / "sha256sum",
        "#!/bin/sh\nprintf 'stable-hash  %s\\n' \"$1\"\n",
    )
    for name in ("free", "df", "useradd", "chown"):
        _write_executable(bin_dir / name, "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "systemd-run",
        """#!/bin/sh
if [ -n "${SYSTEMD_RUN_ARGS_FILE:-}" ]; then
  printf '%s\n' "$@" > "$SYSTEMD_RUN_ARGS_FILE"
fi
exit 0
""",
    )
    _write_executable(bin_dir / "cp", "#!/bin/sh\nexit 97\n")
    _write_executable(bin_dir / "curl", "#!/bin/sh\nexit 0\n")
    return bin_dir, units_dir, proc_dir


def _test_environment(tmp_path: Path, root: Path) -> dict[str, str]:
    bin_dir, units_dir, proc_dir = _fake_linux_commands(tmp_path)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "CODEX_MONITOR_TESTING": "1",
        "CODEX_MONITOR_TEST_ROOT": str(root),
        "CODEX_MONITOR_TEST_UNIT_DIR": str(tmp_path / "systemd"),
        "CODEX_MONITOR_TEST_LOGROTATE_DIR": str(tmp_path / "logrotate"),
        "CODEX_MONITOR_TEST_PROC_ROOT": str(proc_dir),
        "CODEX_MONITOR_TEST_SS": str(bin_dir / "ss"),
        "FAKE_UNITS_DIR": str(units_dir),
    }


def test_scripts_never_touch_network_or_existing_services() -> None:
    scripts = "\n".join(path.read_text() for path in DEPLOY.glob("*.sh"))
    for forbidden in (
        "apt ",
        "apt-get",
        "iptables",
        "nft ",
        "ufw",
        "systemctl restart",
        "docker",
    ):
        assert forbidden not in scripts

    for protected_service in (
        "ssh.service",
        "sing-box.service",
        "cdn-subscription.service",
        "friend-clash-sub.service",
        "share-100gb-sub.service",
    ):
        assert protected_service in scripts

    for protected_port in (22, 22222, 2082, 2086, 2095, 2052, 8880):
        assert str(protected_port) in scripts


def test_service_has_hard_limits_and_sandbox() -> None:
    unit = (DEPLOY / "codex-quota-monitor.service").read_text()
    assert "User=codex-monitor" in unit
    assert "EnvironmentFile=/opt/codex-quota-monitor/.env" in unit
    assert "Environment=PORT=1200" in unit
    assert "1201" not in unit
    assert "LISTEN_INADDR_ANY=0" in unit
    assert "MemoryMax=384M" in unit
    assert "CPUQuota=30%" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ExecStartPre=+/opt/codex-quota-monitor/runtime/node/bin/node" in unit
    assert "--refresh-query-ids" in unit
    assert (
        "ReadWritePaths=/opt/codex-quota-monitor/data "
        "/var/log/codex-quota-monitor "
        "/opt/codex-quota-monitor/rsshub/node_modules/rsshub/dist-lib"
    ) in unit


def test_runner_always_kills_private_rsshub() -> None:
    runner = (DEPLOY / "run-monitor.sh").read_text()
    assert "trap cleanup EXIT INT TERM" in runner
    assert 'kill "$rsshub_pid"' in runner
    assert 'health_url="$RSSHUB_BASE_URL/healthz"' in runner
    assert "curl --fail --silent --connect-timeout 2 --max-time 2" in runner
    assert "RSSHUB_BASE_URL must be loopback" in runner
    assert "ss_bin=/usr/bin/ss" in runner
    assert '\"$ss_bin\" -H -ltnp "sport = :$PORT"' in runner
    health_loop = runner[runner.index("for _ in $(seq 1 30)") :]
    assert health_loop.index('kill -0 "$rsshub_pid"') < health_loop.index(
        "curl --fail --silent --connect-timeout 2 --max-time 2"
    )
    assert "python_bin=/usr/bin/python3" in runner
    assert 'PYTHONPATH="$root/src" "$python_bin" -m codex_quota_monitor' in runner
    assert "export LISTEN_INADDR_ANY=0" in runner
    assert "export NODE_OPTIONS=--max-old-space-size=256" in runner
    assert '"$root/rsshub/server.mjs"' in runner
    assert "dist/index.mjs" not in runner


def test_dry_run_uses_a_fixed_resource_limited_transient_unit(tmp_path: Path) -> None:
    dry_run = (DEPLOY / "dry-run.sh").read_text()
    makefile = Path("Makefile").read_text()
    assert "sudo ./deploy/dry-run.sh" in makefile
    assert "run-monitor.sh dry-run" not in makefile
    assert "fixed name" in dry_run
    assert "codex-quota-monitor-refresh" in dry_run
    assert dry_run.index("codex-quota-monitor-refresh") < dry_run.index(
        "codex-quota-monitor-dry-run"
    )
    assert "--refresh-query-ids" in dry_run

    root = tmp_path / "project"
    root.mkdir()
    args_file = tmp_path / "systemd-run.args"
    env = _test_environment(tmp_path / "commands", root)
    env["SYSTEMD_RUN_ARGS_FILE"] = str(args_file)

    result = subprocess.run(
        [str(DEPLOY / "dry-run.sh")],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = args_file.read_text().splitlines()
    for expected in (
        "--unit=codex-quota-monitor-dry-run",
        "--uid=codex-monitor",
        "--gid=codex-monitor",
        f"--working-directory={root}",
        f"--property=EnvironmentFile={root}/.env",
        "--setenv=PORT=1201",
        "--setenv=RSSHUB_BASE_URL=http://127.0.0.1:1201",
        "--setenv=DATABASE_PATH=/tmp/codex-quota-monitor-dry-run.db",
        "--setenv=LISTEN_INADDR_ANY=0",
        "--setenv=NODE_OPTIONS=--max-old-space-size=256",
        "--property=MemoryMax=384M",
        "--property=CPUQuota=30%",
        "--property=TimeoutStartSec=5min",
        f"--property=ReadWritePaths={root}/data {root}/log",
        f"{root}/deploy/run-monitor.sh",
        "dry-run",
    ):
        assert expected in args
    assert "--wait" in args
    assert "--collect" in args
    assert "--replace" not in args
    assert "--property=PrivateTmp=yes" in args
    assert args.index(f"--property=EnvironmentFile={root}/.env") < args.index(
        "--setenv=PORT=1201"
    )
    env_command = args.index("/usr/bin/env")
    assert env_command > args.index(f"--property=EnvironmentFile={root}/.env")
    assert args[env_command + 1 : env_command + 4] == [
        "PORT=1201",
        "RSSHUB_BASE_URL=http://127.0.0.1:1201",
        "DATABASE_PATH=/tmp/codex-quota-monitor-dry-run.db",
    ]


def test_reprocess_uses_a_fixed_resource_limited_transient_unit(tmp_path: Path) -> None:
    script = (DEPLOY / "reprocess-post.sh").read_text()
    runner = (DEPLOY / "run-monitor.sh").read_text()
    assert "codex-quota-monitor-refresh" in script
    assert "codex-quota-monitor-reprocess" in script
    assert "reprocess-post" in runner

    root = tmp_path / "project"
    root.mkdir()
    args_file = tmp_path / "systemd-run.args"
    env = _test_environment(tmp_path / "commands", root)
    env["SYSTEMD_RUN_ARGS_FILE"] = str(args_file)

    result = subprocess.run(
        [str(DEPLOY / "reprocess-post.sh"), "2076735790567338203"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = args_file.read_text().splitlines()
    for expected in (
        "--unit=codex-quota-monitor-reprocess",
        "--uid=codex-monitor",
        "--gid=codex-monitor",
        f"--working-directory={root}",
        f"--property=EnvironmentFile={root}/.env",
        "--setenv=PORT=1200",
        "--setenv=RSSHUB_BASE_URL=http://127.0.0.1:1200",
        "--property=MemoryMax=384M",
        "--property=CPUQuota=30%",
        "--property=TimeoutStartSec=5min",
        f"--property=ReadWritePaths={root}/data {root}/log",
        f"{root}/deploy/run-monitor.sh",
        "reprocess-post",
        "2076735790567338203",
    ):
        assert expected in args
    assert "--wait" in args
    assert "--collect" in args
    assert "--replace" not in args


def test_reprocess_rejects_non_numeric_post_id_without_starting_unit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    args_file = tmp_path / "systemd-run.args"
    env = _test_environment(tmp_path / "commands", root)
    env["SYSTEMD_RUN_ARGS_FILE"] = str(args_file)

    result = subprocess.run(
        [str(DEPLOY / "reprocess-post.sh"), "not-a-post"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode != 0
    assert not args_file.exists()


def _resource_systemctl(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "systemctl",
        """#!/bin/sh
cat <<EOF
MemoryMax=${RESOURCE_MEMORY:-402653184}
CPUQuotaPerSecUSec=${RESOURCE_CPU:-300ms}
EOF
""",
    )
    return bin_dir


def test_resource_check_validates_effective_systemd_limits(tmp_path: Path) -> None:
    script = (DEPLOY / "resource-check.sh").read_text()
    assert "systemctl show" in script
    assert "MemoryMax" in script
    assert "CPUQuotaPerSecUSec" in script
    assert "./deploy/resource-check.sh" in Path("Makefile").read_text()

    bin_dir = _resource_systemctl(tmp_path)
    result = subprocess.run(
        [str(DEPLOY / "resource-check.sh")],
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert "resource limits verified" in result.stdout


def test_resource_check_rejects_missing_or_wrong_limits(tmp_path: Path) -> None:
    bin_dir = _resource_systemctl(tmp_path)
    base_env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"}

    wrong_memory = subprocess.run(
        [str(DEPLOY / "resource-check.sh")],
        text=True,
        capture_output=True,
        env={**base_env, "RESOURCE_MEMORY": "infinity"},
    )
    wrong_cpu = subprocess.run(
        [str(DEPLOY / "resource-check.sh")],
        text=True,
        capture_output=True,
        env={**base_env, "RESOURCE_CPU": "400ms"},
    )

    assert wrong_memory.returncode != 0
    assert "unexpected MemoryMax" in wrong_memory.stderr
    assert wrong_cpu.returncode != 0
    assert "unexpected CPUQuotaPerSecUSec" in wrong_cpu.stderr


def _make_enable_project(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    project = tmp_path / "enable-project"
    deploy = project / "deploy"
    bin_dir = project / "bin"
    deploy.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy("Makefile", project / "Makefile")
    events = project / "events"
    events.write_text("")
    _write_executable(
        deploy / "postflight.sh",
        "#!/bin/sh\nprintf 'postflight\\n' >> \"$ENABLE_EVENTS\"\n"
        "exit \"${POSTFLIGHT_STATUS:-0}\"\n",
    )
    _write_executable(
        deploy / "resource-check.sh",
        "#!/bin/sh\nprintf 'resource-check\\n' >> \"$ENABLE_EVENTS\"\n"
        "exit \"${RESOURCE_STATUS:-0}\"\n",
    )
    _write_executable(
        bin_dir / "sudo",
        "#!/bin/sh\nexec \"$@\"\n",
    )
    _write_executable(
        bin_dir / "systemctl",
        "#!/bin/sh\nprintf 'systemctl %s\\n' \"$*\" >> \"$ENABLE_EVENTS\"\n",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "ENABLE_EVENTS": str(events),
    }
    return project, env, events


def test_runtime_dependencies_are_pinned() -> None:
    package = json.loads(Path("rsshub/package.json").read_text())
    assert package == {
        "private": True,
        "dependencies": {"rsshub": "1.0.0-master.4436842"},
    }
    lock = json.loads(Path("rsshub/package-lock.json").read_text())
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    locked_rsshub = lock["packages"]["node_modules/rsshub"]
    assert locked_rsshub["version"] == "1.0.0-master.4436842"
    assert locked_rsshub["resolved"].startswith("https://registry.npmjs.org/rsshub/-/")
    assert locked_rsshub["integrity"].startswith("sha512-")

    installer = (DEPLOY / "install.sh").read_text()
    assert "node_version=22.20.0" in installer
    assert "00bbd05e306ea68b6e13e17360d0e2f680b493ef95f2fea1c4296ff7437530bc" in installer
    assert "chmod 640" in installer
    assert "chown root:codex-monitor" in installer
    assert 'install -m 0644 "$source_root/rsshub/package-lock.json"' in installer
    assert 'install -m 0644 "$source_root/rsshub/server.mjs"' in installer
    assert '"$root/runtime/node/bin/npm" ci' in installer
    assert '"$root/runtime/node/bin/npm" install' not in installer


def test_make_enable_stops_before_systemctl_when_safety_check_fails(
    tmp_path: Path,
) -> None:
    project, env, events = _make_enable_project(tmp_path)

    postflight_failure = subprocess.run(
        ["make", "--silent", "enable"],
        cwd=project,
        text=True,
        capture_output=True,
        env={**env, "POSTFLIGHT_STATUS": "11"},
    )
    assert postflight_failure.returncode != 0
    assert events.read_text().splitlines() == ["postflight"]

    events.write_text("")
    resource_failure = subprocess.run(
        ["make", "--silent", "enable"],
        cwd=project,
        text=True,
        capture_output=True,
        env={**env, "RESOURCE_STATUS": "12"},
    )
    assert resource_failure.returncode != 0
    assert events.read_text().splitlines() == ["postflight", "resource-check"]


def test_make_enable_runs_safety_checks_before_enabling_timer(tmp_path: Path) -> None:
    project, env, events = _make_enable_project(tmp_path)

    result = subprocess.run(
        ["make", "--silent", "enable"],
        cwd=project,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert events.read_text().splitlines() == [
        "postflight",
        "resource-check",
        "systemctl enable --now codex-quota-monitor.timer",
    ]


def test_timer_and_rollback_only_manage_project_units() -> None:
    timer = (DEPLOY / "codex-quota-monitor.timer").read_text()
    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer

    makefile = Path("Makefile").read_text()
    assert "codex-quota-monitor.timer" in makefile
    assert "codex-quota-monitor.service" in makefile
    for protected_service in (
        "ssh.service",
        "sing-box.service",
        "cdn-subscription.service",
        "friend-clash-sub.service",
        "share-100gb-sub.service",
    ):
        assert f"stop {protected_service}" not in makefile
        assert f"disable {protected_service}" not in makefile


def test_make_test_imports_the_src_layout_package() -> None:
    makefile = Path("Makefile").read_text()
    assert "PYTHONPATH=src python -m pytest" in makefile


def test_install_succeeds_when_checkout_is_already_the_target(tmp_path: Path) -> None:
    installer = (DEPLOY / "install.sh").read_text()
    assert "CODEX_MONITOR_TESTING" in installer

    root = tmp_path / "project"
    shutil.copytree(DEPLOY, root / "deploy")
    (root / "config").mkdir()
    (root / "config/sources.json").write_text("{}")
    (root / "rsshub").mkdir()
    shutil.copy("rsshub/package.json", root / "rsshub/package.json")
    shutil.copy("rsshub/server.mjs", root / "rsshub/server.mjs")
    (root / "src/package").mkdir(parents=True)
    (root / "src/package/__init__.py").write_text("")
    (root / ".env").write_text("placeholder=true\n")
    node = root / "runtime/node/bin/node"
    node.parent.mkdir(parents=True)
    _write_executable(node, "#!/bin/sh\nexit 0\n")

    env = _test_environment(tmp_path / "commands", root)
    Path(env["CODEX_MONITOR_TEST_UNIT_DIR"]).mkdir()
    Path(env["CODEX_MONITOR_TEST_LOGROTATE_DIR"]).mkdir()
    result = subprocess.run(
        [str(root / "deploy/install.sh")],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "installed but timer remains disabled" in result.stdout


def test_preflight_does_not_change_existing_data_permissions(tmp_path: Path) -> None:
    preflight = (DEPLOY / "preflight.sh").read_text()
    assert "CODEX_MONITOR_TESTING" in preflight
    assert '"$root/data"' not in preflight

    root = tmp_path / "project"
    data = root / "data"
    data.mkdir(parents=True)
    data.chmod(0o731)
    before = data.stat()
    env = _test_environment(tmp_path / "commands", root)

    result = subprocess.run(
        [str(DEPLOY / "preflight.sh")],
        text=True,
        capture_output=True,
        env=env,
    )
    after = data.stat()

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_uid == before.st_uid
    assert (root / "preflight/services.tsv").is_file()
    assert (root / "preflight/listeners.tsv").is_file()


def test_postflight_compares_listener_identity_not_just_ports(tmp_path: Path) -> None:
    postflight = (DEPLOY / "postflight.sh").read_text()
    assert "listeners.tsv" in postflight

    root = tmp_path / "project"
    root.mkdir()
    env = _test_environment(tmp_path / "commands", root)
    before = subprocess.run(
        [str(DEPLOY / "preflight.sh")],
        text=True,
        capture_output=True,
        env=env,
    )
    assert before.returncode == 0, before.stderr

    unchanged = subprocess.run(
        [str(DEPLOY / "postflight.sh")],
        text=True,
        capture_output=True,
        env=env,
    )
    assert unchanged.returncode == 0, unchanged.stderr

    replaced = subprocess.run(
        [str(DEPLOY / "postflight.sh")],
        text=True,
        capture_output=True,
        env={**env, "SS_REPLACED_PORT": "22"},
    )
    assert replaced.returncode != 0
    assert "protected listener changed" in replaced.stderr


def test_preflight_and_postflight_reject_an_occupied_dry_run_port(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    env = _test_environment(tmp_path / "commands", root)

    occupied_before = subprocess.run(
        [str(DEPLOY / "preflight.sh")],
        text=True,
        capture_output=True,
        env={**env, "SS_OCCUPIED_PROJECT_PORT": "1201"},
    )
    assert occupied_before.returncode != 0
    assert "port 1201 is already in use" in occupied_before.stderr

    before = subprocess.run(
        [str(DEPLOY / "preflight.sh")],
        text=True,
        capture_output=True,
        env=env,
    )
    assert before.returncode == 0, before.stderr
    occupied_after = subprocess.run(
        [str(DEPLOY / "postflight.sh")],
        text=True,
        capture_output=True,
        env={**env, "SS_OCCUPIED_PROJECT_PORT": "1201"},
    )
    assert occupied_after.returncode != 0
    assert "project port 1201 remained in use" in occupied_after.stderr


def _runner_root(tmp_path: Path, monitor: str) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "runner-root"
    (root / "deploy").mkdir(parents=True)
    shutil.copy(DEPLOY / "run-monitor.sh", root / "deploy/run-monitor.sh")
    (root / "rsshub").mkdir(parents=True)
    (root / "rsshub/server.mjs").write_text("")
    (root / "config").mkdir()
    (root / "config/sources.json").write_text("{}")
    (root / "src").mkdir()

    node = root / "runtime/node/bin/node"
    node.parent.mkdir(parents=True)
    _write_executable(
        node,
        """#!/usr/bin/python3
import os
import signal
from pathlib import Path

events = Path(os.environ["RUNNER_EVENTS"])
events.write_text(f"started {os.getpid()}\\n")
def stop(_signum, _frame):
    with events.open("a") as stream:
        stream.write("terminated\\n")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while True:
    signal.pause()
""",
    )
    monitor_bin = tmp_path / "monitor"
    _write_executable(monitor_bin, monitor)
    env = _test_environment(tmp_path / "commands", root)
    env["CODEX_MONITOR_TEST_PYTHON"] = str(monitor_bin)
    env["RUNNER_EVENTS"] = str(tmp_path / "runner-events")
    return root, env


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.02)


def test_runner_reaps_rsshub_when_monitor_fails(tmp_path: Path) -> None:
    runner = (DEPLOY / "run-monitor.sh").read_text()
    assert "CODEX_MONITOR_TEST_PYTHON" in runner
    root, env = _runner_root(tmp_path, "#!/bin/sh\nexit 7\n")

    result = subprocess.run(
        [str(root / "deploy/run-monitor.sh"), "run"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 7
    assert Path(env["RUNNER_EVENTS"]).read_text().splitlines()[-1] == "terminated"


def test_runner_dry_run_overrides_production_port_and_database(tmp_path: Path) -> None:
    values = tmp_path / "monitor-env"
    root, env = _runner_root(
        tmp_path,
        "#!/bin/sh\nprintf '%s\\n' \"$PORT|$RSSHUB_BASE_URL|$DATABASE_PATH\" > \"$MONITOR_ENV\"\n",
    )
    env.update(
        {
            "PORT": "1200",
            "RSSHUB_BASE_URL": "http://127.0.0.1:1200",
            "DATABASE_PATH": "/opt/codex-quota-monitor/data/monitor.db",
            "MONITOR_ENV": str(values),
        }
    )

    result = subprocess.run(
        [str(root / "deploy/run-monitor.sh"), "dry-run"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert values.read_text().strip() == (
        "1201|http://127.0.0.1:1201|/tmp/codex-quota-monitor-dry-run.db"
    )


def test_runner_does_not_accept_another_process_health_response(
    tmp_path: Path,
) -> None:
    monitor_started = tmp_path / "monitor-started"
    root, env = _runner_root(
        tmp_path,
        f"#!/bin/sh\ntouch '{monitor_started}'\n",
    )
    _write_executable(root / "runtime/node/bin/node", "#!/bin/sh\nexit 9\n")
    curl = Path(env["PATH"].split(":", 1)[0]) / "curl"
    _write_executable(curl, "#!/bin/sh\nsleep 0.1\nexit 0\n")

    result = subprocess.run(
        [str(root / "deploy/run-monitor.sh"), "run"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode != 0
    assert "private RSSHub failed to become healthy" in result.stderr
    assert not monitor_started.exists()


def test_runner_bounds_health_requests_and_reaps_rsshub_on_timeout(
    tmp_path: Path,
) -> None:
    runner = (DEPLOY / "run-monitor.sh").read_text()
    assert "for _ in $(seq 1 30)" in runner

    monitor_started = tmp_path / "monitor-started"
    curl_events = tmp_path / "curl-events"
    root, env = _runner_root(
        tmp_path,
        f"#!/bin/sh\ntouch '{monitor_started}'\n",
    )
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _write_executable(
        bin_dir / "curl",
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CURL_EVENTS\"\nexit 28\n",
    )
    _write_executable(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
    env["CURL_EVENTS"] = str(curl_events)

    result = subprocess.run(
        [str(root / "deploy/run-monitor.sh"), "run"],
        text=True,
        capture_output=True,
        env=env,
        timeout=3,
    )

    assert result.returncode != 0
    assert "private RSSHub failed to become healthy" in result.stderr
    calls = curl_events.read_text().splitlines()
    assert 1 <= len(calls) <= 30
    assert all("--connect-timeout 2" in call for call in calls)
    assert all("--max-time 2" in call for call in calls)
    assert Path(env["RUNNER_EVENTS"]).read_text().splitlines()[-1] == "terminated"
    assert not monitor_started.exists()


def test_runner_rejects_listener_pid_substring_from_another_process(
    tmp_path: Path,
) -> None:
    monitor_started = tmp_path / "monitor-started"
    root, env = _runner_root(
        tmp_path,
        f"#!/bin/sh\ntouch '{monitor_started}'\n",
    )
    env["SS_LISTENER_PID_PREFIX"] = "3"

    result = subprocess.run(
        [str(root / "deploy/run-monitor.sh"), "run"],
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
    )

    assert result.returncode != 0
    assert "private RSSHub failed to become healthy" in result.stderr
    assert not monitor_started.exists()


def test_runner_accepts_its_own_loopback_listener(tmp_path: Path) -> None:
    monitor_started = tmp_path / "monitor-started"
    root, env = _runner_root(
        tmp_path,
        f"#!/bin/sh\ntouch '{monitor_started}'\n",
    )

    result = subprocess.run(
        [str(root / "deploy/run-monitor.sh"), "run"],
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert monitor_started.exists()


def test_runner_rejects_non_loopback_rsshub_base_url(tmp_path: Path) -> None:
    monitor_started = tmp_path / "monitor-started"
    root, env = _runner_root(
        tmp_path,
        f"#!/bin/sh\ntouch '{monitor_started}'\n",
    )
    env["PORT"] = "1200"
    env["RSSHUB_BASE_URL"] = "https://example.test:1200"

    result = subprocess.run(
        [str(root / "deploy/run-monitor.sh"), "run"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode != 0
    assert "RSSHUB_BASE_URL must be loopback" in result.stderr
    assert not monitor_started.exists()


def test_runner_reaps_rsshub_when_signalled(tmp_path: Path) -> None:
    runner = (DEPLOY / "run-monitor.sh").read_text()
    assert "trap cleanup EXIT INT TERM" in runner
    assert "CODEX_MONITOR_TEST_PYTHON" in runner
    root, env = _runner_root(
        tmp_path,
        "#!/usr/bin/python3\nimport time\ntime.sleep(30)\n",
    )
    process = subprocess.Popen(
        [str(root / "deploy/run-monitor.sh"), "run"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        events = Path(env["RUNNER_EVENTS"])
        _wait_for_file(events)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        assert process.returncode != 0
        assert events.read_text().splitlines()[-1] == "terminated"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
