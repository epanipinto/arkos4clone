import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
collect_ignore_glob = ["device/*"]  # these run inside the dArkOS chroot (test_device_container.py starts them)


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def remote():
    return load("r36s_remote", "r36s-remote.py")


@pytest.fixture(scope="session")
def installer():
    return load("install_pc_link", "install-pc-link.py")
