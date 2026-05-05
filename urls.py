"""URL helpers — pure string manipulation, no network."""

from urllib.parse import urlparse, urlunparse


def sanitize_url(url):
    """Strip whitespace and drop query/fragment from a URL.

    Many product URLs get copied with tracking params (utm_*, gclid, etc.)
    or stray whitespace. We keep only scheme + netloc + path so the stored
    URL is canonical and deduplicable.
    """
    if not url:
        return url
    # Remove all whitespace anywhere in the string (including accidental newlines)
    url = "".join(url.split())
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.scheme or not parsed.netloc:
        return url
    # params (the rarely-used path-level ";..." bit) is preserved; query + fragment dropped
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))
