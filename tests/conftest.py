import pytest
from fastapi.testclient import TestClient

from agroguard.api import create_app
from agroguard.config import load_settings


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def client(tmp_path, settings):
    app = create_app(f"sqlite:///{tmp_path / 'test.db'}", settings=settings, auto_demo=False, api_key="")
    with TestClient(app) as client:
        yield client
