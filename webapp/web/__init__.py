"""Web blueprint for UI routes"""
from flask import Blueprint

web_bp = Blueprint('web', __name__)

from webapp.web import routes
