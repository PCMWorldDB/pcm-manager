"""PCS rider slug selection independent of Activity Monitor."""
import re
import unicodedata


PCS_SLUG_PATTERN = r'[a-z0-9]+(?:-[a-z0-9]+)*'
_RIDER_URL_PREFIX = 'https://www.procyclingstats.com/rider/'
_RIDER_URL_SUFFIX = '/road'


def assumed_rider_slug(first_name, last_name):
    """Build an unverified PCS candidate from a rider's name."""
    name = f"{first_name or ''} {last_name or ''}"
    name = ''.join('-' if char in "'’‘`ʼʻ＇´" else char
                   for char in name if unicodedata.category(char) != 'Cf')
    name = unicodedata.normalize('NFKD', name)
    name = ''.join(char for char in name if not unicodedata.combining(char))
    name = name.casefold().translate(str.maketrans({
        'ø': 'o', 'ł': 'l', 'đ': 'd', 'ð': 'd', 'þ': 'th', 'æ': 'ae',
        'œ': 'oe', 'ħ': 'h', 'ı': 'i', 'ŋ': 'ng', 'ŧ': 't', 'ƶ': 'z',
        'ƒ': 'f', 'ĸ': 'k',
    }))
    slug = re.sub(r'[^a-z0-9-]+', '-', name)
    return re.sub(r'-+', '-', slug).strip('-') or None


def valid_rider_slug(slug):
    return bool(isinstance(slug, str) and slug != '0' and len(slug) <= 255
                and re.fullmatch(PCS_SLUG_PATTERN, slug))


def rider_profile_url(slug):
    return f'{_RIDER_URL_PREFIX}{slug}{_RIDER_URL_SUFFIX}'


def rider_slug_from_profile_url(url):
    if isinstance(url, str) and url.startswith(_RIDER_URL_PREFIX) and url.endswith(_RIDER_URL_SUFFIX):
        slug = url[len(_RIDER_URL_PREFIX):-len(_RIDER_URL_SUFFIX)]
        if valid_rider_slug(slug):
            return slug
    return None


def request_rider_slug(stored_slug, first_name, last_name):
    """Use a saved slug, or a name candidate for an unset/legacy zero slug."""
    stored_slug = (stored_slug or '').strip()
    if valid_rider_slug(stored_slug):
        return stored_slug
    if stored_slug not in ('', '0'):
        return None
    candidate = assumed_rider_slug(first_name, last_name)
    return candidate if valid_rider_slug(candidate) else None
