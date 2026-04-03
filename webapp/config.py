import os
from datetime import timedelta


def _env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


# Path Helper Functions
def get_base_data_path():
    """Get the base data directory path"""
    storage_root = './filestore'
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
    """Get the CDB releases directory path for a namespace"""
    return os.path.join(get_namespace_path(namespace_name), 'cdb', 'releases')


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


class Config:
    """Base configuration"""
    
    # Flask
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-this')
    
    # SQLAlchemy - Main Database
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ECHO = _env_bool('SQLALCHEMY_ECHO', False)
    
    # SQLAlchemy - Connection Pooling
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 10,
        'pool_recycle': 3600,
        'pool_pre_ping': True,
        'max_overflow': 20
    }
    
    # Namespace database connection pool settings (smaller pools)
    NAMESPACE_ENGINE_OPTIONS = {
        'pool_size': 3,
        'pool_recycle': 3600,
        'pool_pre_ping': True,
        'max_overflow': 5
    }
    
    # Database binds (main database explicitly)
    SQLALCHEMY_BINDS = {
        'main': os.getenv('DATABASE_URL')
    }
    
    # Provisioning database (for creating new namespaces)
    PROVISIONER_DATABASE_URL = os.getenv('PROVISIONER_DATABASE_URL')
    
    # Redis & Celery
    REDIS_URL = os.getenv('REDIS_URL', 'cache://localhost:6379/0')
    CELERY_BROKER_URL = REDIS_URL
    CELERY_RESULT_BACKEND = REDIS_URL
    
    # Session
    SESSION_COOKIE_NAME = 'pcm_session'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    
    # File Upload
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500MB for SQLite files
    ALLOWED_EXTENSIONS = {'db', 'cdb', 'sqlite', 'sqlite3'}
    
    # External API
    FIRST_CYCLING_BASE_URL = os.getenv('FIRST_CYCLING_BASE_URL', 'https://firstcycling.com')
    FIRST_CYCLING_API_PATH = os.getenv('FIRST_CYCLING_API_PATH')
    
    # Security
    BCRYPT_LOG_ROUNDS = int(os.getenv('BCRYPT_LOG_ROUNDS', 12))
    
    # WTForms
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None
    
    # PCM Versions ('value', 'label')
    PCM_VERSIONS = [
        ('PCM2024', 'PCM 2024'), 
        ('PCM2025', 'PCM 2025'),
    ]

    SEASONS = [
        {'year': 2026, 'gender': 'Men', 'is_default': True}
    ]


class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    FLASK_ENV = 'development'


class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    FLASK_ENV = 'production'
    
    # Stricter session settings
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Strict'
    
    # Disable debug toolbar
    DEBUG_TB_ENABLED = False


class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'postgresql://test_user:test_pass@localhost:5432/pcm_test'
    WTF_CSRF_ENABLED = False


# Config dictionary
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
