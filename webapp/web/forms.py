"""Web forms for authentication and user input"""
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import StringField, PasswordField, BooleanField, SubmitField, TextAreaField, SelectField, IntegerField
from wtforms.validators import DataRequired, Length, Optional, Regexp, ValidationError


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
    temporary_password = PasswordField('Temporary Password', validators=[
        DataRequired(message='Temporary password is required'),
        Length(min=8, message='Password must be at least 8 characters')
    ])
    confirm_password = PasswordField('Confirm Password', validators=[
        DataRequired(message='Please confirm the password')
    ])
    submit = SubmitField('Create User')
    
    def validate_confirm_password(self, field):
        if field.data != self.temporary_password.data:
            raise ValidationError('Passwords must match')


class PasswordChangeForm(FlaskForm):
    """Password change form for users with temporary passwords"""
    current_password = PasswordField('Current Password', validators=[
        DataRequired(message='Current password is required')
    ])
    new_password = PasswordField('New Password', validators=[
        DataRequired(message='New password is required'),
        Length(min=8, message='Password must be at least 8 characters')
    ])
    confirm_password = PasswordField('Confirm New Password', validators=[
        DataRequired(message='Please confirm the new password')
    ])
    submit = SubmitField('Change Password')
    
    def validate_confirm_password(self, field):
        if field.data != self.new_password.data:
            raise ValidationError('Passwords must match')


class PasswordResetForm(FlaskForm):
    """Admin password reset form"""
    new_password = PasswordField('New Temporary Password', validators=[
        DataRequired(message='New password is required'),
        Length(min=8, message='Password must be at least 8 characters')
    ])
    confirm_password = PasswordField('Confirm Password', validators=[
        DataRequired(message='Please confirm the password')
    ])
    submit = SubmitField('Reset Password')
    
    def validate_confirm_password(self, field):
        if field.data != self.new_password.data:
            raise ValidationError('Passwords must match')


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
    pcm_version = SelectField('PCM Version', validators=[Optional()], default='PCM2025')
    database_year = IntegerField('Database Year', validators=[Optional()], default=2025)
    cdb_file = FileField('CDB Database File (SQLite)', validators=[
        Optional(),
        FileAllowed(['sqlite'], 'Only .sqlite files are allowed')
    ])
    status = SelectField('Status', choices=[
        ('active', 'Active'),
        ('draft', 'Draft'),
        ('archived', 'Archived')
    ], default='draft')
    submit = SubmitField('Save Namespace')
