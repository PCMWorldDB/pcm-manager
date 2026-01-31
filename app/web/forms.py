"""Web forms for authentication and user input"""
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, TextAreaField, SelectField, IntegerField
from wtforms.validators import DataRequired, Length, Optional, Regexp


class LoginForm(FlaskForm):
    """User login form"""
    username = StringField('Username', validators=[
        DataRequired(message='Username is required'),
        Length(min=3, max=80, message='Username must be between 3 and 80 characters')
    ])
    password = PasswordField('Password', validators=[
        DataRequired(message='Password is required')
    ])
    remember = BooleanField('Remember Me')
    submit = SubmitField('Login')


class NamespaceForm(FlaskForm):
    """Namespace creation/edit form"""
    name = StringField('Name', validators=[
        DataRequired(message='Name is required'),
        Length(min=3, max=100, message='Name must be between 3 and 100 characters')
    ])
    slug = StringField('Slug', validators=[
        DataRequired(message='Slug is required'),
        Length(min=3, max=100, message='Slug must be between 3 and 100 characters'),
        Regexp(r'^[a-z0-9-]+$', message='Slug must contain only lowercase letters, numbers, and hyphens')
    ])
    description = TextAreaField('Description', validators=[Optional()])
    pcm_version = SelectField('PCM Version', validators=[Optional()])
    database_year = IntegerField('Database Year', validators=[Optional()])
    status = SelectField('Status', choices=[
        ('active', 'Active'),
        ('draft', 'Draft'),
        ('archived', 'Archived')
    ], default='draft')
    submit = SubmitField('Save Namespace')
