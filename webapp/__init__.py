"""Flask application factory"""
import os
from flask import Flask
from webapp.config import config
from webapp.extensions import init_extensions


def create_app(config_name=None):
    """Create and configure the Flask application"""
    
    if config_name is None:
        config_name = os.getenv('FLASK_ENV', 'development')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # Initialize extensions
    init_extensions(app)
    
    # Configure User model relationships on every app startup
    # This is safe because configure_user_relationships() checks if tables exist
    with app.app_context():
        from webapp.models.main.user import configure_user_relationships
        configure_user_relationships()
    
    # Register blueprints
    register_blueprints(app)
    
    # Register CLI commands
    register_commands(app)
    
    # Register error handlers
    register_error_handlers(app)

    
    return app


def register_blueprints(app):
    """Register Flask blueprints"""
    from webapp.web import web_bp
    app.register_blueprint(web_bp)
    
    # TODO: Register API blueprint when ready
    # from app.api import api_bp
    # app.register_blueprint(api_bp, url_prefix='/api/v1')


def register_commands(app):
    """Register CLI commands"""
    from webapp.cli import (
        init_db_command, 
        seed_roles_command, 
        namespace,
        cdb
    )
    
    app.cli.add_command(init_db_command)
    app.cli.add_command(seed_roles_command)
    app.cli.add_command(namespace)
    app.cli.add_command(cdb)


def register_error_handlers(app):
    """Register error handlers"""
    from flask import jsonify, request, render_template
    
    @app.route('/health')
    def health_check():
        return jsonify({'status': 'healthy'}), 200
    
    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not found'}), 404
        return render_template('errors/404.html'), 404
    
    @app.errorhandler(403)
    def forbidden(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Forbidden'}), 403
        return render_template('errors/403.html'), 403
    
    @app.errorhandler(500)
    def internal_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Internal server error'}), 500
        return render_template('errors/500.html'), 500

