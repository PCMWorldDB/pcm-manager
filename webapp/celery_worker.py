"""Celery worker entrypoint for background tasks."""
import os

from webapp import create_app
from webapp.extensions import celery

# Build Flask app so Celery tasks run with app context.
flask_app = create_app(os.getenv('FLASK_ENV', 'development'))

# Import task modules so Celery registers task definitions.
import webapp.tasks  # noqa: E402,F401
