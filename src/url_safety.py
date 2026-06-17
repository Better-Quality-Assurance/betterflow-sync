"""Safety checks for outbound binary/release fetches (defense-in-depth).

The updater (``self_updater``) and the tracker bootstrap (``aw_manager``) both
download executables. The URLs are effectively constant (GitHub releases) or
come from the GitHub releases API, but validating scheme + host before fetching
ensures a spoofed/redirected value can't point the download at an arbitrary
host. Releases live on GitHub, so the allowlist is GitHub + its asset CDN.
"""

from urllib.parse import urlparse

# Suffix match so GitHub's asset-redirect CDN (objects.githubusercontent.com)
# is covered without enumerating every host.
ALLOWED_FETCH_SUFFIXES = (
    "github.com",
    "githubusercontent.com",
)


def is_safe_fetch_url(url: str, allowed_suffixes: tuple[str, ...] = ALLOWED_FETCH_SUFFIXES) -> bool:
    """Return True iff ``url`` is HTTPS and its host is (a subdomain of) an
    allowed host. Used to gate binary downloads."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except (ValueError, AttributeError):
        # urlparse / .hostname can raise on malformed authority (bad port,
        # broken IPv6 brackets). Treat anything unparseable as unsafe.
        return False
    if parsed.scheme != "https":
        return False
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes)
