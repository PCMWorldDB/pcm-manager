"""Web forms for authentication and user input"""
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import StringField, PasswordField, BooleanField, SubmitField, TextAreaField, SelectField, IntegerField
from wtforms.validators import DataRequired, Length, Optional, Regexp, ValidationError, NumberRange
from webapp.security import PasswordPolicy


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


class UserCreateForm(FlaskForm):
    """User creation form"""
    username = StringField('Username', validators=[
        DataRequired(message='Username is required'),
        Length(min=3, max=80, message='Username must be between 3 and 80 characters'),
        Regexp(r'^[a-zA-Z0-9_-]+$', message='Username can only contain letters, numbers, hyphens, and underscores')
    ])
    submit = SubmitField('Create Invite')


class PasswordChangeForm(FlaskForm):
    """Password change form for users with temporary passwords"""
    current_password = PasswordField('Current Password', validators=[
        DataRequired(message='Current password is required')
    ])
    new_password = PasswordField('New Password', validators=[
        DataRequired(message='New password is required'),
        PasswordPolicy()
    ])
    confirm_password = PasswordField('Confirm New Password', validators=[
        DataRequired(message='Please confirm the new password')
    ])
    submit = SubmitField('Change Password')
    
    def validate_confirm_password(self, field):
        if field.data != self.new_password.data:
            raise ValidationError('Passwords must match')


class PasswordSetupForm(FlaskForm):
    """Password form used by one-time invite and reset links."""
    new_password = PasswordField('New Password', validators=[
        DataRequired(message='New password is required'),
        PasswordPolicy()
    ])
    confirm_password = PasswordField('Confirm Password', validators=[
        DataRequired(message='Please confirm the password')
    ])
    submit = SubmitField('Set Password')
    
    def validate_confirm_password(self, field):
        if field.data != self.new_password.data:
            raise ValidationError('Passwords must match')


class AdminPasswordResetForm(FlaskForm):
    """Admin action form that issues a one-time reset link."""
    submit = SubmitField('Create Reset Link')


class NamespaceMetadataForm(FlaskForm):
    """Current season and game version; neither changes namespace identity."""
    database_year = IntegerField('Database Year', validators=[DataRequired(), NumberRange(min=1900, max=9999)])
    pcm_version = SelectField('PCM Version', validators=[DataRequired()])
    submit = SubmitField('Save current season and version')


class NamespaceForm(FlaskForm):
    """Namespace creation/edit form"""
    creation_method = SelectField('Creation method', choices=[
        ('import', 'Import CDB'), ('clone', 'Clone Existing Namespace')], default='import')
    source_namespace = SelectField('Source namespace', choices=[], validators=[Optional()])
    copy_rider_stats = BooleanField('Rider Stats', default=True)
    copy_rider_attributes = BooleanField('Rider Attributes', default=True)
    copy_teams = BooleanField('Teams', default=True)
    copy_races = BooleanField('Races', default=True)
    name = StringField('Name', default='WDBO', validators=[
        DataRequired(message='Name is required'),
        Length(min=3, max=100, message='Name must be between 3 and 100 characters')
    ])
    slug = StringField('Slug', validators=[
        DataRequired(message='Slug is required'),
        Length(min=3, max=100, message='Slug must be between 3 and 100 characters'),
        Regexp(r'^[a-z0-9-]+$', message='Slug must contain only lowercase letters, numbers, and hyphens')
    ])
    description = TextAreaField('Description', validators=[Optional()])
    pcm_version = SelectField('PCM Version', validators=[Optional()], default='PCM2025')
    database_year = IntegerField('Database Year', validators=[Optional()], default=2026)
    gender = SelectField('Gender', choices=[('Men', 'Men'), ('Women', 'Women')], default='Men')
    cdb_file = FileField('CDB Database File', validators=[
        Optional(),
        FileAllowed(['cdb', 'sqlite'], 'Only .cdb or .sqlite files are allowed')
    ])
    status = SelectField('Status', choices=[
        ('active', 'Active'),
        ('draft', 'Draft'),
        ('archived', 'Archived')
    ], default='draft')
    submit = SubmitField('Create Namespace')
