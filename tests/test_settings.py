from app.settings import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "env": "test",
        "log_level": "INFO",
        "db_host": "db.test.local",
        "db_port": "15432",
        "db_name": "atlas_test",
        "db_user": "atlas_user",
        "db_password": "unit-test-password",
        "mbe_outbox": "/tmp/outbox",
        "source_cleanup_enabled": "false",
        "storage_backend": "local",
        "storage_root": "/tmp/storage",
    }
    values.update(overrides)
    return Settings(**values)


def test_settings_parse_port_and_false_boolean() -> None:
    settings = make_settings()

    assert settings.db_port == 15432
    assert settings.source_cleanup_enabled is False


def test_database_url_uses_psycopg_and_expected_parts() -> None:
    settings = make_settings()
    url = settings.database_url

    assert url.drivername == "postgresql+psycopg"
    assert url.username == "atlas_user"
    assert url.host == "db.test.local"
    assert url.port == 15432
    assert url.database == "atlas_test"


def test_settings_repr_does_not_include_password() -> None:
    settings = make_settings(db_password="never-show-this")

    assert "db_password" not in repr(settings)
    assert "never-show-this" not in repr(settings)
