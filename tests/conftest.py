import pytest


@pytest.fixture(autouse=True)
def _scene_cache(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("VECTOR_ENV_SCENE_CACHE", str(tmp_path_factory.mktemp("scene-cache")))
