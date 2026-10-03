import re
from urllib.parse import urlparse

from . import config

ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
RESERVED = {"shorten", "analytics", "health", "metrics", "docs", "redoc", "openapi.json", "favicon.ico"}
MAX_URL_LENGTH = 2048


class ValidationError(ValueError):
    pass


def validate_url(url: str) -> str:
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"URL longer than {MAX_URL_LENGTH} characters")
    if any(c.isspace() for c in url):
        raise ValidationError("URL must not contain whitespace")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValidationError("URL must be an absolute http(s) URL")
    if parsed.netloc.lower() == urlparse(config.BASE_URL).netloc.lower():
        raise ValidationError("cannot shorten a URL that points at this service (redirect loop)")
    return url


def validate_alias(alias: str) -> str:
    if not ALIAS_RE.match(alias):
        raise ValidationError("alias must be 3-32 chars of A-Z a-z 0-9 _ -")
    if alias.lower() in RESERVED:
        raise ValidationError("alias is reserved")
    return alias
