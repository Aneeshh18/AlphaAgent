"""Safe systemd-user scheduling for the single-process local deployment."""

from __future__ import annotations

import os
import pwd
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MANAGED_MARKER = "# Managed by AI Investment OS. Re-run `aios scheduler-install` to update."
TIMER_NAMES = (
    "aios-us-daily.timer",
    "aios-us-filings.timer",
    "aios-backup.timer",
)
EMAIL_TIMER_NAME = "aios-notifications-email.timer"
EMAIL_SERVICE_NAME = "aios-notifications-email.service"
OPTIONAL_TIMER_NAMES = (EMAIL_TIMER_NAME,)
ALL_TIMER_NAMES = TIMER_NAMES + OPTIONAL_TIMER_NAMES
SERVICE_BY_TIMER = {
    "aios-us-daily.timer": "aios-us-daily.service",
    "aios-us-filings.timer": "aios-us-filings.service",
    "aios-backup.timer": "aios-backup.service",
    EMAIL_TIMER_NAME: EMAIL_SERVICE_NAME,
}
SERVICE_NAMES = tuple(SERVICE_BY_TIMER.values())
LEGACY_TIMER_NAMES = (
    "aios-us-current.timer",
    "aios-universe-review.timer",
)
LEGACY_SERVICE_NAMES = (
    "aios-us-current.service",
    "aios-universe-review.service",
)
LEGACY_UNIT_NAMES = LEGACY_SERVICE_NAMES + LEGACY_TIMER_NAMES
MANAGED_SERVICE_NAMES = SERVICE_NAMES + LEGACY_SERVICE_NAMES
ALERT_SERVICE_NAME = "aios-alert@.service"
STATUS_QUERY_TIMEOUT_SECONDS = 5.0
SCHEDULED_LOCK_WAIT_SECONDS = 30 * 60
DAILY_SERVICE_TIMEOUT = "45min"
FILINGS_SERVICE_TIMEOUT = "3h"
BACKUP_SERVICE_TIMEOUT = "2h"
FLOCK_PATH = Path("/usr/bin/flock")
UNIT_NAMES = (
    "aios-us-daily.service",
    "aios-us-daily.timer",
    "aios-us-filings.service",
    "aios-us-filings.timer",
    "aios-backup.service",
    "aios-backup.timer",
    "aios-notifications-email.service",
    "aios-notifications-email.timer",
    ALERT_SERVICE_NAME,
)
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class SchedulerInstallResult:
    """Installed unit paths and enabled timer names."""

    unit_dir: Path
    units: tuple[Path, ...]
    timers: tuple[str, ...]
    linger_enabled: bool | None = None


def render_systemd_units(project_root: Path) -> dict[str, str]:
    """Render user units with an exact checkout and virtual-environment path."""
    root = Path(project_root).resolve()
    launcher = root / ".venv" / "bin" / "aios"
    root_value = _unit_scalar_path(root)
    launcher_condition = _unit_scalar_path(launcher)
    launcher_value = _unit_exec_path(launcher)
    flock_value = _unit_exec_path(FLOCK_PATH)
    scheduler_lock = _unit_exec_path(root / "data" / "operations" / "scheduler.lock")
    serialized_launcher = (
        f"{flock_value} --exclusive --wait {SCHEDULED_LOCK_WAIT_SECONDS} "
        f"{scheduler_lock} {launcher_value}"
    )
    common_service = f"""{MANAGED_MARKER}
[Unit]
After=network-online.target
Wants=network-online.target
ConditionPathExists={launcher_condition}
OnFailure=aios-alert@%n.service

[Service]
Type=oneshot
WorkingDirectory={root_value}
Environment=PYTHONUNBUFFERED=1
Environment=DUCKDB_LOCK_WAIT_SECONDS=300
UMask=0077
Nice=10
TimeoutStartSec={DAILY_SERVICE_TIMEOUT}
"""
    units = {
        "aios-us-daily.service": (
            common_service.replace(
                "[Unit]\n",
                "[Unit]\nDescription=AIOS recoverable weekday U.S. daily update\n"
                "StartLimitIntervalSec=45min\n"
                "StartLimitBurst=3\n",
                1,
            )
            + "Restart=on-failure\n"
            + "RestartSec=5min\n"
            + f"ExecStart={serialized_launcher} refresh-us-daily\n"
            # The unattended paper cycle runs here rather than on its own
            # timer because it must follow the refresh (it needs the newly
            # certified close) and because 02:00 America/New_York sits inside
            # both the execution window of the proposal whose entry session
            # just closed and the creation window of the next one.
            #
            # Leading '-' on purpose: a refused or skipped simulated fill must
            # not fail the data-refresh unit. Paper progress and data freshness
            # are separate concerns, and coupling them is what previously
            # turned an ordinary governed refusal into a critical
            # scheduler-failure incident.
            #
            # Deliberately WITHOUT --confirm-simulated. The unattended cycle
            # stages the next proposal and reports what is due, but recording
            # a fill stays a manual act: "Recording a simulated fill is the one
            # deliberate, always-manual confirmation point ... nothing records
            # automatically" (PRODUCT.md). Passing the confirmation flag from a
            # timer would make the scheduler the approver, which is exactly the
            # principle this system exists to hold.
            + f"ExecStartPost=-{serialized_launcher} autopilot\n"
            + f"ExecStartPost={serialized_launcher} health\n"
            + f"ExecStartPost=-{launcher_value} alert-service-recovered --unit %n\n"
        ),
        "aios-us-daily.timer": _timer_unit(
            "AIOS recoverable weekday U.S. daily update",
            "Tue..Sat *-*-* 02:00:00 America/New_York",
            "aios-us-daily.service",
            startup_delay="3min",
        ),
        "aios-us-filings.service": (
            common_service.replace(
                "[Unit]\n",
                "[Unit]\nDescription=AIOS weekly SEC filing refresh\n",
                1,
            )
            .replace(
                f"TimeoutStartSec={DAILY_SERVICE_TIMEOUT}",
                f"TimeoutStartSec={FILINGS_SERVICE_TIMEOUT}",
                1,
            )
            + (
                f"ExecStart={serialized_launcher} refresh-us-current "
                "--no-prices --no-macro\n"
            )
            + f"ExecStartPost={serialized_launcher} health\n"
            + f"ExecStartPost=-{launcher_value} alert-service-recovered --unit %n\n"
        ),
        "aios-us-filings.timer": _timer_unit(
            "AIOS weekly SEC filing refresh",
            "Sat *-*-* 09:00:00",
            "aios-us-filings.service",
        ),
        "aios-backup.service": (
            common_service.replace(
                "[Unit]\n",
                "[Unit]\nDescription=AIOS weekly verified local backup\n",
                1,
            )
            .replace(
                f"TimeoutStartSec={DAILY_SERVICE_TIMEOUT}",
                f"TimeoutStartSec={BACKUP_SERVICE_TIMEOUT}",
                1,
            )
            + f"ExecStart={serialized_launcher} backup\n"
            + f"ExecStartPost=-{launcher_value} alert-service-recovered --unit %n\n"
        ),
        "aios-backup.timer": _timer_unit(
            "AIOS weekly verified local backup",
            "Sun *-*-* 09:00:00",
            "aios-backup.service",
        ),
        EMAIL_SERVICE_NAME: f"""{MANAGED_MARKER}
[Unit]
Description=AIOS bounded external email delivery
After=network-online.target
Wants=network-online.target
ConditionPathExists={launcher_condition}

[Service]
Type=oneshot
WorkingDirectory={root_value}
Environment=PYTHONUNBUFFERED=1
UMask=0077
Nice=10
TimeoutStartSec=8min
ExecStart={launcher_value} email-deliver --limit 2
""",
        EMAIL_TIMER_NAME: _interval_timer_unit(
            "AIOS bounded external email delivery",
            EMAIL_SERVICE_NAME,
        ),
        ALERT_SERVICE_NAME: f"""{MANAGED_MARKER}
[Unit]
Description=AIOS local failure recorder for %i
ConditionPathExists={launcher_condition}

[Service]
Type=oneshot
WorkingDirectory={root_value}
Environment=PYTHONUNBUFFERED=1
UMask=0077
TimeoutStartSec=30s
ExecStart={launcher_value} alert-service-failure --unit %i
""",
    }
    if set(units) != set(UNIT_NAMES):
        raise RuntimeError("internal scheduler unit set is incomplete")
    return units


def install_user_scheduler(
    project_root: Path,
    *,
    confirm: bool = False,
    enable_linger: bool = False,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> SchedulerInstallResult:
    """Install and enable only marked AIOS user timers; never require root."""
    if not confirm:
        raise ValueError("scheduler installation requires explicit confirmation")
    root = Path(project_root).resolve()
    launcher = root / ".venv" / "bin" / "aios"
    if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError(f"AIOS virtual-environment launcher is missing or unsafe: {launcher}")
    if not FLOCK_PATH.is_file() or not os.access(FLOCK_PATH, os.X_OK):
        raise ValueError(f"required scheduler lock utility is unavailable: {FLOCK_PATH}")
    scheduler_lock_dir = root / "data" / "operations"
    scheduler_lock_dir.mkdir(parents=True, exist_ok=True)
    if scheduler_lock_dir.is_symlink() or not scheduler_lock_dir.is_dir():
        raise ValueError(f"scheduler lock directory is unsafe: {scheduler_lock_dir}")
    destination = (
        Path(unit_dir).expanduser().resolve()
        if unit_dir is not None
        else (Path.home() / ".config" / "systemd" / "user").resolve()
    )
    units = render_systemd_units(root)
    _preflight_managed_paths(destination, units, LEGACY_UNIT_NAMES)
    destination.mkdir(parents=True, exist_ok=True)
    _run(
        runner,
        ["systemctl", "--user", "disable", "--now", *LEGACY_TIMER_NAMES],
        check=False,
    )
    for name in LEGACY_UNIT_NAMES:
        path = destination / name
        if path.exists():
            path.unlink()
    paths: list[Path] = []
    for name, content in units.items():
        target = destination / name
        _atomic_write(target, content)
        paths.append(target)

    _run(runner, ["systemctl", "--user", "daemon-reload"])
    _run(runner, ["systemctl", "--user", "enable", "--now", *TIMER_NAMES])
    linger_enabled = None
    if enable_linger:
        enable_user_linger(confirm=True, runner=runner)
        linger_enabled = user_linger_status(runner=runner)
        if linger_enabled is not True:
            raise RuntimeError("login manager did not confirm keep-running-after-logout")
    return SchedulerInstallResult(destination, tuple(paths), TIMER_NAMES, linger_enabled)


def set_user_scheduler_active(
    active: bool,
    *,
    runner: Runner = subprocess.run,
) -> None:
    """Resume or pause every managed timer without editing its unit files."""
    action = "enable" if active else "disable"
    _run(runner, ["systemctl", "--user", action, "--now", *TIMER_NAMES])


def set_email_scheduler_active(
    active: bool,
    *,
    runner: Runner = subprocess.run,
) -> None:
    """Enable or disable only the explicitly opted-in external email timer."""
    action = "enable" if active else "disable"
    _run(runner, ["systemctl", "--user", action, "--now", EMAIL_TIMER_NAME])


def user_scheduler_status(
    *,
    runner: Runner = subprocess.run,
    unit_dir: Path | None = None,
) -> dict[str, dict[str, bool | str]]:
    """Return core timer state without hanging on an unavailable user bus."""
    return _scheduler_status(
        TIMER_NAMES,
        runner=runner,
        unit_dir=unit_dir,
    )


def email_scheduler_status(
    *,
    runner: Runner = subprocess.run,
    unit_dir: Path | None = None,
) -> dict[str, bool | str]:
    """Return the optional email timer state without enabling or starting it."""
    return _scheduler_status(
        OPTIONAL_TIMER_NAMES,
        runner=runner,
        unit_dir=unit_dir,
    )[EMAIL_TIMER_NAME]


def _scheduler_status(
    timer_names: tuple[str, ...],
    *,
    runner: Runner,
    unit_dir: Path | None,
) -> dict[str, dict[str, bool | str]]:
    """Return bounded runtime or installed-file evidence for selected timers."""
    result: dict[str, dict[str, bool | str]] = {}
    try:
        for timer in timer_names:
            enabled = _status_query(
                runner,
                ["systemctl", "--user", "is-enabled", timer],
            )
            active = _status_query(
                runner,
                ["systemctl", "--user", "is-active", timer],
            )
            if _user_bus_unavailable(enabled) or _user_bus_unavailable(active):
                return _scheduler_file_status(unit_dir, timer_names=timer_names)
            timer_properties = _show_properties(
                runner,
                timer,
                ("LastTriggerUSec", "NextElapseUSecRealtime"),
            )
            service_properties = _show_properties(
                runner,
                SERVICE_BY_TIMER[timer],
                (
                    "ActiveState",
                    "SubState",
                    "Result",
                    "ExecMainStatus",
                    "ExecMainStartTimestamp",
                    "ExecMainExitTimestamp",
                ),
            )
            last_trigger = timer_properties.get("LastTriggerUSec", "never")
            service_exit = service_properties.get("ExecMainExitTimestamp")
            service_start = service_properties.get("ExecMainStartTimestamp")
            service_last_run = service_exit or service_start or "never"
            # A service can also be started manually for a recovery proof. Its
            # actual execution time is newer and more useful than the timer's
            # last trigger; fall back to the trigger only before any service run.
            last_run = service_last_run if service_last_run != "never" else last_trigger
            service_result = service_properties.get("Result", "not-run")
            exit_status = service_properties.get("ExecMainStatus", "unknown")
            service_active = service_properties.get("ActiveState") in {
                "active",
                "activating",
                "reloading",
            }
            if service_start and not service_exit and service_active:
                service_result = "running"
                exit_status = "unknown"
            # systemd reports Result=success and ExecMainStatus=0 for a freshly
            # loaded oneshot service even when it has never executed. Do not turn
            # those defaults into a false successful-run claim.
            if last_run == "never":
                service_result = "not-run"
                exit_status = "unknown"
            result[timer] = {
                "enabled": enabled.returncode == 0,
                "active": active.returncode == 0,
                "last_trigger": last_trigger,
                "last_run": last_run,
                "next_trigger": timer_properties.get("NextElapseUSecRealtime", "unknown"),
                "service_result": service_result,
                "exit_status": exit_status,
                "runtime_verified": True,
            }
    except subprocess.TimeoutExpired:
        return _scheduler_file_status(unit_dir, timer_names=timer_names)
    return result


def _scheduler_file_status(
    unit_dir: Path | None,
    *,
    timer_names: tuple[str, ...] = TIMER_NAMES,
) -> dict[str, dict[str, bool | str]]:
    """Report safe installation evidence when runtime systemd state is unavailable."""
    destination = (
        Path(unit_dir).expanduser().resolve()
        if unit_dir is not None
        else (Path.home() / ".config" / "systemd" / "user").resolve()
    )
    wants = destination / "timers.target.wants"
    result: dict[str, dict[str, bool | str]] = {}
    for timer in timer_names:
        unit = destination / timer
        enabled_link = wants / timer
        installed = unit.is_file() and not unit.is_symlink() and _is_managed(unit)
        enabled = enabled_link.is_symlink() and enabled_link.resolve() == unit
        result[timer] = {
            "enabled": installed and enabled,
            "active": False,
            "last_trigger": "unavailable",
            "last_run": "unavailable",
            "next_trigger": "unavailable",
            "service_result": "unverified",
            "exit_status": "unknown",
            "runtime_verified": False,
        }
    return result


def remove_user_scheduler(
    *,
    confirm: bool = False,
    unit_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> tuple[Path, ...]:
    """Disable and remove only files carrying the AIOS managed marker."""
    if not confirm:
        raise ValueError("scheduler removal requires explicit confirmation")
    destination = (
        Path(unit_dir).expanduser().resolve()
        if unit_dir is not None
        else (Path.home() / ".config" / "systemd" / "user").resolve()
    )
    all_names = UNIT_NAMES + LEGACY_UNIT_NAMES
    existing = [destination / name for name in all_names if (destination / name).exists()]
    for path in existing:
        if path.is_symlink() or not _is_managed(path):
            raise ValueError(f"refusing to remove unmanaged scheduler file: {path}")
    _run(
        runner,
        [
            "systemctl",
            "--user",
            "disable",
            "--now",
            *TIMER_NAMES,
            *OPTIONAL_TIMER_NAMES,
            *LEGACY_TIMER_NAMES,
        ],
        check=False,
    )
    for path in existing:
        path.unlink()
    _run(runner, ["systemctl", "--user", "daemon-reload"])
    return tuple(existing)


def enable_user_linger(
    *,
    confirm: bool = False,
    runner: Runner = subprocess.run,
) -> None:
    """Keep the user service manager alive after logout; never disable it implicitly."""
    if not confirm:
        raise ValueError("keep-running-after-logout requires explicit confirmation")
    username = pwd.getpwuid(os.getuid()).pw_name
    _run(runner, ["loginctl", "enable-linger", username])


def user_linger_status(*, runner: Runner = subprocess.run) -> bool | None:
    """Return linger state, or None when the login manager cannot be queried."""
    username = pwd.getpwuid(os.getuid()).pw_name
    try:
        completed = _status_query(
            runner,
            ["loginctl", "show-user", username, "--property", "Linger", "--value"],
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode:
        return None
    value = completed.stdout.strip().lower()
    if value not in {"yes", "no"}:
        return None
    return value == "yes"


def _timer_unit(
    description: str,
    calendar: str,
    service: str,
    *,
    startup_delay: str | None = None,
) -> str:
    startup = f"OnStartupSec={startup_delay}\n" if startup_delay else ""
    return f"""{MANAGED_MARKER}
[Unit]
Description={description}

[Timer]
OnCalendar={calendar}
{startup}Persistent=true
AccuracySec=5min
RandomizedDelaySec=5min
Unit={service}

[Install]
WantedBy=timers.target
"""


def _interval_timer_unit(description: str, service: str) -> str:
    return f"""{MANAGED_MARKER}
[Unit]
Description={description}

[Timer]
OnStartupSec=3min
OnUnitActiveSec=5min
AccuracySec=30s
RandomizedDelaySec=15s
Unit={service}

[Install]
WantedBy=timers.target
"""


def _preflight_managed_paths(
    destination: Path,
    units: dict[str, str],
    legacy_names: tuple[str, ...] = (),
) -> None:
    for name in (*units, *legacy_names):
        target = destination / name
        if target.is_symlink():
            raise ValueError(f"refusing to replace symbolic-link scheduler file: {target}")
        if target.exists() and not _is_managed(target):
            raise ValueError(f"refusing to replace unmanaged scheduler file: {target}")


def _is_managed(path: Path) -> bool:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
    except (IndexError, OSError, UnicodeError):
        return False
    return first_line == MANAGED_MARKER


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _run(runner: Runner, command: list[str], *, check: bool = True) -> None:
    completed = runner(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout or "unknown systemctl error").strip()
        raise RuntimeError(f"{' '.join(command[:3])} failed: {detail}")


def _show_properties(
    runner: Runner,
    unit: str,
    properties: tuple[str, ...],
) -> dict[str, str]:
    command = ["systemctl", "--user", "show", unit]
    for name in properties:
        command.extend(("--property", name))
    completed = _status_query(runner, command)
    if completed.returncode:
        return {}
    output: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in properties and value.strip():
            output[name] = value.strip()
    return output


def _status_query(runner: Runner, command: list[str]) -> subprocess.CompletedProcess[str]:
    """Bound every read-only systemd status call so the CLI always returns."""
    # ``subprocess.run(timeout=...)`` kills only the direct process. A stuck
    # systemctl/D-Bus helper can retain the captured pipes and keep
    # ``communicate()`` blocked. On the real Linux deployment, coreutils
    # ``timeout`` bounds the complete process group; injected test runners keep
    # receiving the exact systemctl command.
    executed_command = command
    runner_timeout = STATUS_QUERY_TIMEOUT_SECONDS
    if runner is subprocess.run:
        executed_command = [
            "timeout",
            "--kill-after=1s",
            f"{STATUS_QUERY_TIMEOUT_SECONDS:g}s",
            *command,
        ]
        runner_timeout += 2.0
    completed = runner(
        executed_command,
        check=False,
        capture_output=True,
        text=True,
        timeout=runner_timeout,
    )
    if completed.returncode in {124, 137}:
        raise subprocess.TimeoutExpired(command, STATUS_QUERY_TIMEOUT_SECONDS)
    return completed


def _user_bus_unavailable(completed: subprocess.CompletedProcess[str]) -> bool:
    """Distinguish an inaccessible user bus from a genuinely disabled timer."""
    if completed.returncode == 0:
        return False
    detail = f"{completed.stderr}\n{completed.stdout}".lower()
    return any(
        marker in detail
        for marker in (
            "failed to connect to bus",
            "no medium found",
            "transport endpoint is not connected",
        )
    )


def _path_text(value: Path) -> str:
    text = str(value)
    if not text or "\n" in text or "\r" in text or "\0" in text:
        raise ValueError("project path cannot be represented in a systemd unit")
    return text


def _unit_scalar_path(value: Path) -> str:
    text = _path_text(value)
    return (
        text.replace("\\", "\\x5c")
        .replace(" ", "\\x20")
        .replace("\t", "\\x09")
        .replace("%", "%%")
    )


def _unit_exec_path(value: Path) -> str:
    text = _path_text(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'
