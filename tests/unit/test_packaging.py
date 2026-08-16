from __future__ import annotations

import os
import subprocess
import sys

import pytest

from scripts.check_deployment_policy import _check_python_entrypoints

pytestmark = pytest.mark.unit


def test_project_packages_are_installed_outside_the_repository(tmp_path) -> None:
    """Imports must come from the environment, not an accidental current directory."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", "import apps, platform_core, workloads"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_automation_and_docs_use_module_entrypoints() -> None:
    assert _check_python_entrypoints() == []
