import importlib

from app.settings import get_settings


def test_engine_uses_settings_without_connecting(monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_DB_HOST", "engine.test.local")
    monkeypatch.setenv("ATLAS_DB_PORT", "25432")
    monkeypatch.setenv("ATLAS_DB_NAME", "engine_atlas")
    monkeypatch.setenv("ATLAS_DB_USER", "engine_user")
    monkeypatch.setenv("ATLAS_DB_PASSWORD", "test-only-password")
    get_settings.cache_clear()

    import app.db.session as session_module

    session_module = importlib.reload(session_module)

    assert session_module.engine.url.drivername == "postgresql+psycopg"
    assert session_module.engine.url.host == "engine.test.local"
    assert session_module.engine.url.port == 25432
    assert session_module.engine.url.database == "engine_atlas"
    assert session_module.engine.url.username == "engine_user"
    assert session_module.SessionLocal.kw["expire_on_commit"] is False

    session_module.engine.dispose()
    get_settings.cache_clear()
