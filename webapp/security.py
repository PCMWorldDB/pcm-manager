"""Authentication security helpers."""

from wtforms.validators import ValidationError


COMMON_PASSWORDS = {
    '123456789012345',
    'correcthorsebatterystaple',
    'letmeinletmeinletmein',
    'passwordpassword',
    'qwertyqwertyqwerty',
    'worlddbworlddb',
}


def validate_password_policy(password, username=None):
    """Return a user-facing password policy error, or None."""
    if len(password or '') < 15:
        return 'Password must be at least 15 characters.'
    if len(password) > 128:
        return 'Password must be 128 characters or fewer.'

    normalized = password.casefold()
    if normalized in COMMON_PASSWORDS:
        return 'Choose a less common password.'
    if username and username.casefold() in normalized:
        return 'Password must not contain your username.'
    if 'worlddb' in normalized:
        return 'Password must not contain the site name.'
    return None


class PasswordPolicy:
    """WTForms validator for local WorldDB passwords."""

    def __init__(self, username_field=None):
        self.username_field = username_field

    def __call__(self, form, field):
        username = None
        if self.username_field:
            username = getattr(form, self.username_field).data
        error = validate_password_policy(field.data, username=username)
        if error:
            raise ValidationError(error)
