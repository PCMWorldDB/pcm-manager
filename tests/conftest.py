"""Integration tests require an explicitly selected disposable PostgreSQL server."""
import os

import pytest
from sqlalchemy.engine import make_url


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    database_url = os.environ.get("WORLDDB_TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("Set WORLDDB_TEST_DATABASE_URL to a disposable PostgreSQL database")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql" or url.database != "worlddb_test":
        pytest.fail("Tests require PostgreSQL with database name worlddb_test")

    # Set configuration before importing the app; never load a developer's .env.
    os.environ["DATABASE_URL"] = database_url
    os.environ["PROVISIONER_DATABASE_URL"] = url.set(database="postgres").render_as_string(hide_password=False)
    os.environ["SECRET_KEY"] = "ci-only-session-key"
    for name in ("ADMIN_USERNAME", "ADMIN_PASSWORD", "DISCORD_BOT_TOKEN"):
        os.environ.pop(name, None)

    from webapp import create_app
    from webapp.config import Config, config

    class TestConfig(Config):
        TESTING = True
        SQLALCHEMY_DATABASE_URI = database_url
        SQLALCHEMY_BINDS = {"main": database_url}
        WTF_CSRF_ENABLED = False
        BCRYPT_LOG_ROUNDS = 4

    config["ci"] = TestConfig
    original_cwd = os.getcwd()
    os.chdir(tmp_path_factory.mktemp("worlddb"))
    try:
        application = create_app("ci")
        yield application
        from webapp.extensions import db
        with application.app_context():
            db.session.remove()
            for engine in db.engines.values():
                engine.dispose()
    finally:
        os.chdir(original_cwd)
        config.pop("ci", None)


@pytest.fixture
def client(app):
    return app.test_client()
