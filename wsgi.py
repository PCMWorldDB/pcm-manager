"""WSGI entry point for production deployment"""
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from webapp import create_app

# Create Flask app
app = create_app()

if __name__ == '__main__':
    app.run()
