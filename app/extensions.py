"""Flask extensions initialization"""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect
from flask_marshmallow import Marshmallow
from celery import Celery
import os

# Initialize extensions (will be bound to app in create_app)
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
bcrypt = Bcrypt()
csrf = CSRFProtect()
ma = Marshmallow()

# Initialize Celery with broker URL
celery = Celery(
    'pcm_manager',
    broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('REDIS_URL', 'redis://localhost:6379/0')
)


def init_extensions(app):
    """Initialize Flask extensions"""
    
    # SQLAlchemy
    db.init_app(app)
    
    # Migrations
    migrate.init_app(app, db)
    
    # Login Manager
    login_manager.init_app(app)
    login_manager.login_view = 'web.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'info'
    
    # Bcrypt
    bcrypt.init_app(app)
    
    # CSRF Protection
    csrf.init_app(app)
    
    # Marshmallow
    ma.init_app(app)
    
    # Celery
    celery.conf.update(app.config)
    
    class ContextTask(celery.Task):
        """Make celery tasks work with Flask app context"""
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
