import pytest

from app.validation import ValidationError, validate_alias, validate_url


@pytest.mark.parametrize("url", ["https://example.com/a?b=1", "http://example.com"])
def test_valid_urls(url):
    assert validate_url(url) == url


@pytest.mark.parametrize("url", ["ftp://x.com", "example.com", "javascript:alert(1)", "https://",
                                 "http://a b.com", "https://example.com/" + "a" * 2100,
                                 "http://localhost:8000/abc"])
def test_invalid_urls(url):
    with pytest.raises(ValidationError):
        validate_url(url)


def test_alias_rules():
    assert validate_alias("my-link_1") == "my-link_1"
    for bad in ("ab", "has space", "shorten", "Analytics", "x" * 33):
        with pytest.raises(ValidationError):
            validate_alias(bad)
