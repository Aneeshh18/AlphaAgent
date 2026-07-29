from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from aios.maintenance import (
    MaintenanceLockBusyError,
    MaintenanceLockError,
    project_maintenance_lock,
)


def _project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return project


def test_maintenance_lock_records_bounded_holder_metadata(tmp_path) -> None:
    project = _project(tmp_path)

    with project_maintenance_lock(project, operation="paper-execute") as lease:
        payload = json.loads(lease.path.read_text(encoding="utf-8"))
        assert payload["operation"] == "paper-execute"
        assert payload["pid"] == os.getpid()
        assert payload["project_root"] == str(project)
        assert lease.path.stat().st_mode & 0o777 == 0o600


def test_maintenance_lock_is_non_reentrant_and_releases_after_exception(tmp_path) -> None:
    project = _project(tmp_path)

    with (
        pytest.raises(RuntimeError, match="boom"),
        project_maintenance_lock(project, operation="backup"),
    ):
        with (
            pytest.raises(MaintenanceLockBusyError),
            project_maintenance_lock(project, operation="restore"),
        ):
            pass
        raise RuntimeError("boom")

    with project_maintenance_lock(project, operation="restore") as lease:
        assert lease.operation == "restore"


def test_maintenance_lock_rejects_a_concurrent_thread(tmp_path) -> None:
    project = _project(tmp_path)

    def contend() -> str:
        with (
            pytest.raises(MaintenanceLockBusyError),
            project_maintenance_lock(project, operation="forward-restart"),
        ):
            pass
        return "blocked"

    with (
        project_maintenance_lock(project, operation="forward-freeze"),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        assert executor.submit(contend).result(timeout=5) == "blocked"


def test_maintenance_lock_rejects_symlink_and_hardlink_targets(tmp_path) -> None:
    project = _project(tmp_path)
    operations = project / "data" / "operations"
    operations.mkdir(parents=True)
    outside = tmp_path / "outside.lock"
    outside.write_text("outside", encoding="utf-8")
    lock = operations / "maintenance.lock"
    lock.symlink_to(outside)
    with (
        pytest.raises(MaintenanceLockError, match="symbolic link"),
        project_maintenance_lock(project, operation="backup"),
    ):
        pass

    lock.unlink()
    os.link(outside, lock)
    with (
        pytest.raises(MaintenanceLockError, match="hard-linked"),
        project_maintenance_lock(project, operation="backup"),
    ):
        pass


def test_maintenance_lock_rejects_symlinked_parent_without_outside_write(
    tmp_path,
) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "data").symlink_to(outside, target_is_directory=True)

    with (
        pytest.raises(MaintenanceLockError, match="symbolic link"),
        project_maintenance_lock(project, operation="restore"),
    ):
        pass

    assert not (outside / "operations").exists()


def test_maintenance_lock_rejects_symbolic_root_and_nonregular_lock(tmp_path) -> None:
    project = _project(tmp_path)
    linked_root = tmp_path / "linked-project"
    linked_root.symlink_to(project, target_is_directory=True)
    with (
        pytest.raises(MaintenanceLockError, match="project root"),
        project_maintenance_lock(linked_root, operation="backup"),
    ):
        pass

    lock_path = project / "data" / "operations" / "maintenance.lock"
    lock_path.mkdir(parents=True)
    with (
        pytest.raises(MaintenanceLockError, match="regular"),
        project_maintenance_lock(project, operation="backup"),
    ):
        pass


def test_stale_metadata_never_blocks_a_new_kernel_lease(tmp_path) -> None:
    project = _project(tmp_path)
    lock_path = project / "data" / "operations" / "maintenance.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        '{"operation":"stale","pid":999999,"acquired_at":"2020-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    with project_maintenance_lock(project, operation="backup") as lease:
        payload = json.loads(lease.path.read_text(encoding="utf-8"))
        assert payload["operation"] == "backup"
        assert payload["pid"] == os.getpid()


def test_abrupt_process_exit_releases_flock_despite_stale_metadata(tmp_path) -> None:
    project = _project(tmp_path)
    script = """
from pathlib import Path
import os
from aios.maintenance import project_maintenance_lock

with project_maintenance_lock(Path(os.environ["AIOS_TEST_PROJECT"]), operation="child"):
    os._exit(17)
"""
    environment = dict(os.environ)
    environment["AIOS_TEST_PROJECT"] = str(project)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=environment,
        timeout=10,
    )

    assert completed.returncode == 17
    with project_maintenance_lock(project, operation="parent") as lease:
        payload = json.loads(lease.path.read_text(encoding="utf-8"))
        assert payload["operation"] == "parent"


@pytest.mark.parametrize("operation", ["", "paper execute", "unsafe/path"])
def test_maintenance_lock_rejects_unsafe_operation_labels(
    tmp_path,
    operation,
) -> None:
    project = _project(tmp_path)

    with (
        pytest.raises(MaintenanceLockError),
        project_maintenance_lock(project, operation=operation),
    ):
        pass
