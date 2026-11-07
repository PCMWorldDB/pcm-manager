"""Flask extensions initialization"""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect
from flask_marshmallow import Marshmallow
from celery import Celery
from kombu import Queue

# Initialize extensions (will be bound to app in create_app)
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
bcrypt = Bcrypt()
csrf = CSRFProtect()
ma = Marshmallow()

# Initialize Celery with broker URL
celery = Celery('pcm_manager')


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
    celery_config = dict(app.config)
    # Celery 5.6 rejects mixing the legacy ``CELERY_*`` names in Flask's
    # config with the modern setting names used by the Celery instance.
    celery_config.pop('CELERY_BROKER_URL', None)
    celery_config.pop('CELERY_RESULT_BACKEND', None)
    celery.conf.update(celery_config)
    celery.conf.update(
        broker_url=app.config['REDIS_URL'],
        result_backend=app.config['REDIS_URL'],
        worker_concurrency=1,
        worker_prefetch_multiplier=1,
        task_queues=(Queue('celery'), Queue('external.high'), Queue('external.low')),
        # Lexicographic order: control work, high groups, then low groups.
        # Redis active_queues is a set; 'priority' would retain arbitrary order.
        broker_transport_options={'queue_order_strategy': 'sorted'},
    )
    celery.conf.timezone = app.config['WORKER_TIMEZONE']
    celery.conf.beat_schedule = {
        **(celery.conf.beat_schedule or {}),
        'poll-external-calls': {
            'task': 'webapp.tasks.poll_external_calls',
            'schedule': app.config['EXTERNAL_CALL_POLL_SECONDS'],
        },
        'poll-activity-monitor-schedules': {
            'task': 'webapp.tasks.poll_activity_monitor_schedules',
            'schedule': app.config['ACTIVITY_MONITOR_POLL_SECONDS'],
        },
    }
    
    class ContextTask(celery.Task):
        """Make celery tasks work with Flask app context"""
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
