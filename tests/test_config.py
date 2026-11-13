"""Environment-selected YAML and environment-only secret contracts."""
import importlib
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from flask import Flask

from webapp.config import ConfigError, _ENV_KEYS, get_base_data_path, load_config

VALUES_ROOT = Path(__file__).resolve().parents[1] / 'envs'
LOCAL_VALUES = VALUES_ROOT / 'local' / 'values.yaml'


@pytest.fixture
def document():
    return yaml.safe_load(LOCAL_VALUES.read_text(encoding='utf-8-sig'))


def write_config(tmp_path, document):
    path = tmp_path / 'values.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding='utf-8')
    return path


def test_environment_values_types_and_derived_settings():
    local = load_config(environ={'ENV': 'local'})
    dev = load_config(environ={'ENV': 'dev'})
    prod = load_config(environ={'ENV': 'prod'})
    testing = load_config(environment='testing', environ={})
    assert local['DEBUG'] and dev['DEBUG'] and not prod['DEBUG']
    assert prod['SESSION_COOKIE_SECURE'] and prod['SESSION_COOKIE_HTTPONLY']
    assert prod['SESSION_COOKIE_SAMESITE'] == 'Strict' and prod['WTF_CSRF_ENABLED']
    assert testing['TESTING'] and not testing['WTF_CSRF_ENABLED']
    assert testing['SQLALCHEMY_BINDS'] == {'main': testing['SQLALCHEMY_DATABASE_URI']}
    assert local['PERMANENT_SESSION_LIFETIME'] == timedelta(days=7)
    assert local['ALLOWED_EXTENSIONS'] == {'db', 'cdb', 'sqlite', 'sqlite3'}
    assert local['PCM_VERSIONS'][0] == ('PCM2024', 'PCM 2024')
    assert local['STATIC_ASSET_VERSION'] == local['SITE_VERSION']
    assert [local['ENV'], dev['ENV'], prod['ENV']] == ['local', 'dev', 'prod']


def test_environment_values_are_typed_and_derived():
    settings = load_config(environ={
        'ENV': 'prod', 'DATABASE_URL': 'postgresql://test:password@database/main',
        'REDIS_URL': 'redis://cache:6379/2', 'SECRET_KEY': 'test-only-secret',
        'FIRST_CYCLING_SYNC_VERBOSE': 'yes', 'FIRST_CYCLING_RETRY_ATTEMPTS': '2',
        'FIRST_CYCLING_REQUEST_DELAY_SECONDS': '7.5',
    })
    assert settings['SECRET_KEY'] == 'test-only-secret'
    assert settings['FIRST_CYCLING_SYNC_VERBOSE'] is True
    assert settings['FIRST_CYCLING_RETRY_ATTEMPTS'] == 2
    assert settings['FIRST_CYCLING_REQUEST_DELAY_SECONDS'] == 7.5
    assert settings['SQLALCHEMY_BINDS']['main'] == settings['SQLALCHEMY_DATABASE_URI']
    assert settings['CELERY_BROKER_URL'] == settings['CELERY_RESULT_BACKEND'] == settings['REDIS_URL']


def test_file_selection_does_not_depend_on_working_directory(tmp_path, document, monkeypatch):
    config_module = importlib.import_module('webapp.config')
    document['SITE_VERSION'] = 'yaml-version'
    document['PCS_REQUEST_DELAY_SECONDS'] = 8
    write_config(tmp_path / 'prod', document)
    monkeypatch.chdir(tmp_path)
    assert load_config(environ={})['ENV'] == 'local'
    monkeypatch.setattr(config_module, 'ENVIRONMENTS_ROOT', tmp_path)
    settings = load_config(environ={'ENV': 'prod', 'FLASK_ENV': 'development'})
    assert settings['ENV'] == 'prod'
    assert settings['FLASK_ENV'] == 'production'
    assert settings['SITE_VERSION'] == 'yaml-version'
    assert settings['PCS_REQUEST_DELAY_SECONDS'] == 8
    with pytest.raises(ConfigError, match='Cannot read'):
        load_config(environ={'ENV': 'dev'})  # never fall back to another environment


@pytest.mark.parametrize('environment', ['production', 'testing', '../prod', '', 'unknown'])
def test_invalid_env_selector_is_rejected(environment):
    with pytest.raises(ConfigError, match='ENV must be'):
        load_config(environ={'ENV': environment})


@pytest.mark.parametrize('key,value', [
    ('WTF_CSRF_ENABLED', 'false'),
    ('PCS_REQUEST_DELAY_SECONDS', 0), ('PCS_REQUEST_DELAY_SECONDS', float('nan')),
    ('EXTERNAL_CALL_POLL_SECONDS', 0), ('SESSION_COOKIE_SAMESITE', 'bad'),
    ('PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_MAX_SIZE', 0),
    ('PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_MAX_SIZE', True),
    ('PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_CELERY_MAX_QUEUED', -1),
    ('PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_CELERY_MAX_QUEUED', 1.5),
    ('WORKER_TIMEZONE', 'No/SuchTimezone'),
    ('ALLOWED_EXTENSIONS', 'db'), ('PCM_VERSIONS', [['only-one']]),
    ('SEASONS', [{'year': True, 'gender': 'Men', 'is_default': True}]),
    ('NAMESPACE_ENGINE_OPTIONS', {'pool_size': 1}),
])
def test_invalid_settings_are_rejected(tmp_path, document, key, value):
    document[key] = value
    with pytest.raises(ConfigError, match=key):
        load_config(write_config(tmp_path, document), environ={})


def test_unknown_and_missing_settings_fail(tmp_path, document):
    document['MISSPELLED_SETTING'] = True
    with pytest.raises(ConfigError, match='unknown'):
        load_config(write_config(tmp_path, document), environ={})
    del document['MISSPELLED_SETTING']
    del document['SITE_VERSION']
    with pytest.raises(ConfigError, match='missing'):
        load_config(write_config(tmp_path, document), environ={})


def test_duplicate_malformed_unsafe_and_missing_yaml(tmp_path):
    path = tmp_path / 'values.yaml'
    for content in ('DEBUG: true\nDEBUG: false', 'DEBUG: [', '!!python/object/apply:os.system [echo unsafe]'):
        path.write_text(content, encoding='utf-8')
        with pytest.raises(ConfigError):
            load_config(path, environ={})
    with pytest.raises(ConfigError, match='Cannot read'):
        load_config(tmp_path / 'missing.yaml', environ={})


@pytest.mark.parametrize('key,value', [('SQLALCHEMY_ECHO', 'not-bool'), ('BCRYPT_LOG_ROUNDS', 'not-int'),
                                       ('FIRST_CYCLING_REQUEST_DELAY_SECONDS', 'not-float'),
                                       ('BCRYPT_LOG_ROUNDS', '3'), ('AUTH_LOGIN_FAILURE_LIMIT', '0'),
                                       ('FIRST_CYCLING_RETRY_ATTEMPTS', '-1'),
                                       ('DATABASE_URL', 'credential-value-that-must-not-appear')])
def test_invalid_environment_setting(key, value):
    setting = 'SQLALCHEMY_DATABASE_URI' if key == 'DATABASE_URL' else key
    with pytest.raises(ConfigError, match=setting) as error:
        load_config(environ={key: value})
    assert value not in str(error.value)


def test_storage_helpers_use_yaml_setting(tmp_path, document):
    document['FILESTORE_ROOT'] = str(tmp_path / 'custom-storage')
    app = Flask(__name__)
    app.config.from_mapping(load_config(write_config(tmp_path, document), environ={}))
    with app.app_context():
        assert Path(get_base_data_path()) == tmp_path / 'custom-storage'


def test_invalid_yaml_stops_before_app_initialization(tmp_path, document, monkeypatch):
    import webapp
    document['PCS_REQUEST_DELAY_SECONDS'] = 0
    write_config(tmp_path / 'local', document)
    monkeypatch.setattr(importlib.import_module('webapp.config'), 'ENVIRONMENTS_ROOT', tmp_path)
    monkeypatch.setenv('ENV', 'local')
    monkeypatch.setattr(webapp, 'init_extensions', lambda app: pytest.fail('must not initialize extensions'))
    with pytest.raises(ConfigError, match='PCS_REQUEST_DELAY_SECONDS'):
        webapp.create_app()


@pytest.mark.parametrize('environment', ['local', 'dev', 'prod'])
def test_values_contain_no_environment_settings(environment):
    document = yaml.safe_load((VALUES_ROOT / environment / 'values.yaml').read_text(encoding='utf-8-sig'))
    assert not set(document) & _ENV_KEYS


@pytest.mark.parametrize('key', sorted(_ENV_KEYS))
def test_environment_keys_are_rejected_in_yaml_even_when_null(tmp_path, document, key):
    document[key] = None
    with pytest.raises(ConfigError, match='Environment-only'):
        load_config(write_config(tmp_path, document), environ={})


def test_yaml_settings_cannot_be_overridden_by_environment(document, tmp_path):
    document['PCS_REQUEST_DELAY_SECONDS'] = 8
    settings = load_config(write_config(tmp_path, document), environ={'PCS_REQUEST_DELAY_SECONDS': '2'})
    assert settings['PCS_REQUEST_DELAY_SECONDS'] == 8


def test_environment_secrets_are_loaded_without_yaml_placeholders():
    secrets = {key: 'test-only-value' for key in (
        'DISCORD_BOT_TOKEN', 'DISCORD_CLIENT_ID', 'DISCORD_CLIENT_SECRET',
        'DISCORD_WEBHOOK_URL', 'ADMIN_USERNAME', 'ADMIN_PASSWORD', 'FIRST_CYCLING_API_PATH',
    )}
    settings = load_config(environ=secrets)
    for key, value in secrets.items():
        assert settings[key] == value
