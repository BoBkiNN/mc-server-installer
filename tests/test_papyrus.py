import pytest
from mcsi.providers.api.papyrus import Papyrus


@pytest.fixture
def client():
    return Papyrus()

PROJECT_NAME = "purpur"
VERSION = "1.21.8"

def test_list_projects(client: Papyrus):
    client.list_projects()


def test_get_project(client: Papyrus):
    client.get_project(PROJECT_NAME)


def test_get_version(client: Papyrus):
    client.get_version(PROJECT_NAME, VERSION)


def test_get_version_detailed(client: Papyrus):
    client.get_version(PROJECT_NAME, VERSION, True)

def test_get_build(client: Papyrus):
    ver = client.get_version(PROJECT_NAME, VERSION)
    assert ver is not None
    builds = ver.builds
    if builds.all:
        b = builds.all[0]
        client.get_build(PROJECT_NAME, VERSION,
                         b if isinstance(b, str) else b.build)

