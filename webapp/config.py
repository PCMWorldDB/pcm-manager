"""Load, validate and adapt root YAML settings for Flask and Celery."""
import math
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from flask import current_app, has_app_context
from sqlalchemy.engine import make_url


class ConfigError(ValueError):
    """Invalid configuration, with values deliberately excluded from errors."""


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConfigError('YAML mapping keys must be strings')
            if key in result:
                raise ConfigError('Duplicate YAML mapping key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


# Types and constraints live here; editable settings live in envs/<ENV>/values.yaml.
_BOOLS = set('''SQLALCHEMY_TRACK_MODIFICATIONS SQLALCHEMY_ECHO
SESSION_COOKIE_HTTPONLY SESSION_COOKIE_SECURE FIRST_CYCLING_SYNC_VERBOSE
FIRST_CYCLING_FETCH_ONLY_MISSING ACTIVITY_MONITOR_PCS_ENABLED ACTIVITY_MONITOR_FC_ENABLED
WTF_CSRF_ENABLED DEBUG DEBUG_TB_ENABLED TESTING'''.split())
_STRINGS = set('''SECRET_KEY REDIS_URL WORKER_TIMEZONE FILESTORE_ROOT SESSION_COOKIE_NAME
SESSION_COOKIE_SAMESITE FIRST_CYCLING_BASE_URL FIRST_CYCLING_SYNC_METHOD SITE_VERSION'''.split())
_OPTIONAL_STRINGS = set('''SQLALCHEMY_DATABASE_URI PROVISIONER_DATABASE_URL DISCORD_BOT_TOKEN
DISCORD_CLIENT_ID DISCORD_CLIENT_SECRET DISCORD_WEBHOOK_URL ADMIN_USERNAME ADMIN_PASSWORD
FIRST_CYCLING_API_PATH STATIC_ASSET_VERSION'''.split())
_POSITIVE_INTS = set('''MAX_CONTENT_LENGTH FIRST_CYCLING_SYNC_LOG_INTERVAL AUTH_INVITE_TOKEN_HOURS
AUTH_RESET_TOKEN_MINUTES AUTH_LOGIN_FAILURE_LIMIT AUTH_LOGIN_LOCK_SECONDS
AUTH_LOGIN_LOCK_MAX_SECONDS PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_MAX_SIZE
PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_CELERY_MAX_QUEUED'''.split())
_NUMBERS = {
    'PERMANENT_SESSION_LIFETIME_DAYS': (0, False),
    'FIRST_CYCLING_REQUEST_DELAY_SECONDS': (0, True),
    'FIRST_CYCLING_RETRY_BACKOFF_SECONDS': (0, False),
    'PCS_REQUEST_DELAY_SECONDS': (1, True),
    'ACTIVITY_MONITOR_REQUEST_DELAY_SECONDS': (0, True),
    'EXTERNAL_CALL_POLL_SECONDS': (0, False),
    'ACTIVITY_MONITOR_POLL_SECONDS': (0, False),
}
_COMPLEX = {'SQLALCHEMY_ENGINE_OPTIONS', 'NAMESPACE_ENGINE_OPTIONS',
            'PCM_VERSIONS', 'SEASONS', 'ALLOWED_EXTENSIONS'}
_KEYS = (_BOOLS | _STRINGS | _OPTIONAL_STRINGS | _POSITIVE_INTS | set(_NUMBERS)
         | _COMPLEX | {'BCRYPT_LOG_ROUNDS', 'FIRST_CYCLING_RETRY_ATTEMPTS', 'WTF_CSRF_TIME_LIMIT'})

# Existing environment settings are environment-only, even when not credentials.
# Their names and fallback values must never be placed in values.yaml.
_ENV_SETTINGS = {
    'SECRET_KEY': ('SECRET_KEY', 'dev-secret-key-change-this'),
    'SQLALCHEMY_DATABASE_URI': ('DATABASE_URL', None),
    'PROVISIONER_DATABASE_URL': ('PROVISIONER_DATABASE_URL', None),
    'REDIS_URL': ('REDIS_URL', 'redis://localhost:6379/0'),
    'DISCORD_BOT_TOKEN': ('DISCORD_BOT_TOKEN', None),
    'DISCORD_CLIENT_ID': ('DISCORD_CLIENT_ID', None),
    'DISCORD_CLIENT_SECRET': ('DISCORD_CLIENT_SECRET', None),
    'DISCORD_WEBHOOK_URL': ('DISCORD_WEBHOOK_URL', None),
    'ADMIN_USERNAME': ('ADMIN_USERNAME', None),
    'ADMIN_PASSWORD': ('ADMIN_PASSWORD', None),
    'SQLALCHEMY_ECHO': ('SQLALCHEMY_ECHO', False),
    'FIRST_CYCLING_BASE_URL': ('FIRST_CYCLING_BASE_URL', 'https://firstcycling.com'),
    'FIRST_CYCLING_API_PATH': ('FIRST_CYCLING_API_PATH', None),
    'FIRST_CYCLING_SYNC_VERBOSE': ('FIRST_CYCLING_SYNC_VERBOSE', False),
    'FIRST_CYCLING_SYNC_LOG_INTERVAL': ('FIRST_CYCLING_SYNC_LOG_INTERVAL', 50),
    'FIRST_CYCLING_SYNC_METHOD': ('FIRST_CYCLING_SYNC_METHOD', 'bulk_refresh'),
    'FIRST_CYCLING_FETCH_ONLY_MISSING': ('FIRST_CYCLING_FETCH_ONLY_MISSING', False),
    'FIRST_CYCLING_REQUEST_DELAY_SECONDS': ('FIRST_CYCLING_REQUEST_DELAY_SECONDS', 1),
    'FIRST_CYCLING_RETRY_ATTEMPTS': ('FIRST_CYCLING_RETRY_ATTEMPTS', 4),
    'FIRST_CYCLING_RETRY_BACKOFF_SECONDS': ('FIRST_CYCLING_RETRY_BACKOFF_SECONDS', 3),
    'ACTIVITY_MONITOR_PCS_ENABLED': ('ACTIVITY_MONITOR_PCS_ENABLED', False),
    'ACTIVITY_MONITOR_FC_ENABLED': ('ACTIVITY_MONITOR_FC_ENABLED', False),
    'ACTIVITY_MONITOR_REQUEST_DELAY_SECONDS': ('ACTIVITY_MONITOR_REQUEST_DELAY_SECONDS', 1.5),
    'BCRYPT_LOG_ROUNDS': ('BCRYPT_LOG_ROUNDS', 12),
    'AUTH_INVITE_TOKEN_HOURS': ('AUTH_INVITE_TOKEN_HOURS', 72),
    'AUTH_RESET_TOKEN_MINUTES': ('AUTH_RESET_TOKEN_MINUTES', 30),
    'AUTH_LOGIN_FAILURE_LIMIT': ('AUTH_LOGIN_FAILURE_LIMIT', 5),
    'AUTH_LOGIN_LOCK_SECONDS': ('AUTH_LOGIN_LOCK_SECONDS', 60),
    'AUTH_LOGIN_LOCK_MAX_SECONDS': ('AUTH_LOGIN_LOCK_MAX_SECONDS', 900),
    'STATIC_ASSET_VERSION': ('STATIC_ASSET_VERSION', None),
}
_YAML_KEYS = _KEYS - _ENV_SETTINGS.keys()
_ENV_KEYS = set(_ENV_SETTINGS) | {variable for variable, _ in _ENV_SETTINGS.values()} | {'ENV', 'FLASK_ENV', 'WORLDDB_CONFIG_FILE'}
ENVIRONMENTS_ROOT = Path(__file__).resolve().parents[1] / 'envs'


def _fail(key):
    raise ConfigError(f'Invalid configuration setting: {key}') from None


def _number(value, minimum, inclusive=True, integer=False):
    return (type(value) in ({int} if integer else {int, float})
            and math.isfinite(value)
            and (value >= minimum if inclusive else value > minimum))


def _validate(settings):
    for key in _BOOLS:
        if type(settings[key]) is not bool:
            _fail(key)
    for key in _STRINGS:
        if not isinstance(settings[key], str) or not settings[key].strip():
            _fail(key)
    for key in _OPTIONAL_STRINGS:
        if settings[key] is not None and not isinstance(settings[key], str):
            _fail(key)
    for key in _POSITIVE_INTS:
        if not _number(settings[key], 1, integer=True):
            _fail(key)
    for key, (minimum, inclusive) in _NUMBERS.items():
        if not _number(settings[key], minimum, inclusive):
            _fail(key)
    for key, minimum, maximum in [('BCRYPT_LOG_ROUNDS', 4, 31), ('FIRST_CYCLING_RETRY_ATTEMPTS', 0, None)]:
        if not _number(settings[key], minimum, integer=True) or (maximum and settings[key] > maximum):
            _fail(key)
    if settings['WTF_CSRF_TIME_LIMIT'] is not None and not _number(settings['WTF_CSRF_TIME_LIMIT'], 0, False):
        _fail('WTF_CSRF_TIME_LIMIT')
    if settings['AUTH_LOGIN_LOCK_MAX_SECONDS'] < settings['AUTH_LOGIN_LOCK_SECONDS']:
        _fail('AUTH_LOGIN_LOCK_MAX_SECONDS')
    for key in ('SQLALCHEMY_ENGINE_OPTIONS', 'NAMESPACE_ENGINE_OPTIONS'):
        pool = settings[key]
        if not isinstance(pool, dict) or set(pool) != {'pool_size', 'pool_recycle', 'pool_pre_ping', 'max_overflow'}:
            _fail(key)
        if (not _number(pool['pool_size'], 1, integer=True)
                or not _number(pool['pool_recycle'], 1, integer=True)
                or not _number(pool['max_overflow'], 0, integer=True)
                or type(pool['pool_pre_ping']) is not bool):
            _fail(key)
    for key in ('SQLALCHEMY_DATABASE_URI', 'PROVISIONER_DATABASE_URL'):
        if settings[key] is not None:
            try:
                make_url(settings[key])
            except Exception:
                _fail(key)
    for key, schemes in [('FIRST_CYCLING_BASE_URL', {'http', 'https'}), ('REDIS_URL', {'redis', 'rediss', 'unix'})]:
        try:
            url = urlsplit(settings[key])
            if url.scheme not in schemes or not (url.netloc or (url.scheme == 'unix' and url.path)):
                _fail(key)
        except ValueError:
            _fail(key)
    try:
        ZoneInfo(settings['WORKER_TIMEZONE'])
    except (ZoneInfoNotFoundError, ValueError):
        _fail('WORKER_TIMEZONE')
    if settings['SESSION_COOKIE_SAMESITE'] not in {'Lax', 'Strict', 'None'}:
        _fail('SESSION_COOKIE_SAMESITE')
    if settings['FIRST_CYCLING_SYNC_METHOD'] not in {'bulk_refresh', 'incremental_upsert'}:
        _fail('FIRST_CYCLING_SYNC_METHOD')
    extensions = settings['ALLOWED_EXTENSIONS']
    if not isinstance(extensions, list) or not extensions or any(not isinstance(x, str) or not x.isalnum() for x in extensions):
        _fail('ALLOWED_EXTENSIONS')
    versions = settings['PCM_VERSIONS']
    if not isinstance(versions, list) or not versions or any(
            not isinstance(pair, list) or len(pair) != 2
            or any(not isinstance(x, str) or not x.strip() for x in pair) for pair in versions):
        _fail('PCM_VERSIONS')
    seasons = settings['SEASONS']
    if not isinstance(seasons, list) or any(
            not isinstance(row, dict) or set(row) != {'year', 'gender', 'is_default'}
            or not _number(row['year'], 1, integer=True)
            or not isinstance(row['gender'], str) or not row['gender'].strip()
            or type(row['is_default']) is not bool for row in seasons):
        _fail('SEASONS')


def _environment_value(key, value):
    if key in _BOOLS:
        token = value.strip().lower()
        if token in {'1', 'true', 'yes', 'on'}:
            return True
        if token in {'0', 'false', 'no', 'off'}:
            return False
        _fail(key)
    if key in _POSITIVE_INTS or key in {'BCRYPT_LOG_ROUNDS', 'FIRST_CYCLING_RETRY_ATTEMPTS'}:
        try:
            return int(value)
        except ValueError:
            _fail(key)
    if key in _NUMBERS or key == 'WTF_CSRF_TIME_LIMIT':
        try:
            return float(value)
        except ValueError:
            _fail(key)
    return value


def load_config(path=None, *, environment=None, environ=None):
    """Load YAML-only settings and separately load environment-only settings.

    File lookup is rooted at this repository, independent of the working folder.
    No .env file is read and no credential values appear in validation errors.
    """
    environ = os.environ if environ is None else environ
    testing = environment == 'testing'
    if environment is None or environment == 'default':
        selected = environ.get('ENV', 'local')
    else:
        selected = {'development': 'dev', 'production': 'prod', 'testing': 'local'}.get(environment, environment)
    if selected not in {'local', 'dev', 'prod'}:
        raise ConfigError('ENV must be local, dev or prod')
    path = Path(path) if path is not None else ENVIRONMENTS_ROOT / selected / 'values.yaml'
    try:
        with path.open(encoding='utf-8-sig') as source:
            document = yaml.load(source, Loader=_UniqueKeyLoader)
    except OSError:
        raise ConfigError('Cannot read selected environment values.yaml') from None
    except yaml.YAMLError:
        raise ConfigError('Invalid YAML in application configuration') from None
    if isinstance(document, dict) and set(document) & _ENV_KEYS:
        raise ConfigError('Environment-only settings must not appear in YAML')
    if not isinstance(document, dict) or set(document) != _YAML_KEYS:
        raise ConfigError('YAML values contain missing or unknown settings')
    environment_defaults = {key: fallback for key, (_, fallback) in _ENV_SETTINGS.items()}
    _validate({**environment_defaults, **document})
    settings = {**environment_defaults, **document}
    if testing:
        settings.update(TESTING=True, WTF_CSRF_ENABLED=False)
    for key, (variable, _) in _ENV_SETTINGS.items():
        if variable in environ:
            settings[key] = _environment_value(key, environ[variable])
    _validate(settings)
    settings['ENV'] = selected
    settings['FLASK_ENV'] = 'testing' if testing else ('production' if selected == 'prod' else 'development')
    settings['SQLALCHEMY_BINDS'] = {'main': settings['SQLALCHEMY_DATABASE_URI']}
    settings['CELERY_BROKER_URL'] = settings['REDIS_URL']
    settings['CELERY_RESULT_BACKEND'] = settings['REDIS_URL']
    settings['PERMANENT_SESSION_LIFETIME'] = timedelta(days=settings.pop('PERMANENT_SESSION_LIFETIME_DAYS'))
    if settings['WTF_CSRF_TIME_LIMIT'] is not None:
        settings['WTF_CSRF_TIME_LIMIT'] = timedelta(seconds=settings['WTF_CSRF_TIME_LIMIT'])
    settings['ALLOWED_EXTENSIONS'] = set(settings['ALLOWED_EXTENSIONS'])
    settings['PCM_VERSIONS'] = [tuple(pair) for pair in settings['PCM_VERSIONS']]
    settings['STATIC_ASSET_VERSION'] = settings['STATIC_ASSET_VERSION'] or settings['SITE_VERSION']
    return settings


class Config:
    """Default YAML profile; remains subclassable by tests and extensions."""


class LocalConfig(Config):
    """Local YAML settings."""


class DevelopmentConfig(Config):
    """Development YAML profile."""


class ProductionConfig(Config):
    """Production YAML profile."""


class TestingConfig(Config):
    """Testing YAML profile."""


for _config_class, _profile in ((Config, None), (LocalConfig, 'local'), (DevelopmentConfig, 'dev'),
                                (ProductionConfig, 'prod'), (TestingConfig, 'testing')):
    for _key, _value in load_config(environment=_profile).items():
        setattr(_config_class, _key, _value)

SITE_VERSION = Config.SITE_VERSION
config = {'local': LocalConfig, 'dev': DevelopmentConfig, 'prod': ProductionConfig,
          'development': DevelopmentConfig, 'production': ProductionConfig,
          'testing': TestingConfig, 'default': Config}


def get_setting(name):
    """Use the current Flask config, or load YAML for tools outside Flask."""
    if has_app_context():
        return current_app.config.get(name)
    return load_config()[name]


# Path Helper Functions
def get_base_data_path():
    """Get the base data directory path"""
    storage_root = get_setting('FILESTORE_ROOT')
    # Handle both relative and absolute paths
    if not os.path.isabs(storage_root):
        return os.path.join(os.getcwd(), storage_root)
    return storage_root


def get_namespace_path(namespace_name):
    """Get the base path for a specific namespace"""
    return os.path.join(get_base_data_path(), 'namespaces', namespace_name)


def get_cdb_import_path(namespace_name):
    """Get the CDB import directory path for a namespace"""
    return os.path.join(get_namespace_path(namespace_name), 'cdb', 'import')


def get_cdb_releases_path(namespace_name):
    """Get the releases directory path for a namespace."""
    return os.path.join(get_namespace_path(namespace_name), 'releases')


def get_photos_path(namespace_name=None):
    """Get the photos directory path, optionally for a specific namespace"""
    if namespace_name:
        return os.path.join(get_namespace_path(namespace_name), 'photos')
    return os.path.join(get_base_data_path(), 'photos')


def ensure_namespace_directories(namespace_name):
    """Create all necessary directories for a namespace"""
    directories = [
        get_cdb_import_path(namespace_name),
        get_cdb_releases_path(namespace_name),
        get_photos_path(namespace_name)
    ]
    
    created = []
    for directory in directories:
        os.makedirs(directory, exist_ok=True)
        
        # Create .gitkeep to preserve structure in git
        gitkeep_path = os.path.join(directory, '.gitkeep')
        if not os.path.exists(gitkeep_path):
            with open(gitkeep_path, 'w') as f:
                f.write('')
        
        created.append(directory)
    
    return created


def ensure_base_directories():
    """Create base storage directories"""
    directories = [
        get_base_data_path(),
        get_photos_path()  # Global photos directory
    ]
    
    for directory in directories:
        os.makedirs(directory, exist_ok=True)
    
    return directories
