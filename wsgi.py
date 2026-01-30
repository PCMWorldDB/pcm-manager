"""WSGI entry point for production deployment"""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from app import create_app

# Create Flask app
app = create_app(os.getenv('FLASK_ENV', 'production'))

if __name__ == '__main__':
    app.run()
