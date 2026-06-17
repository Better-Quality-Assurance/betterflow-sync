"""URL allowlist for binary/release downloads (defense-in-depth)."""

from src.url_safety import is_safe_fetch_url


def test_accepts_github_release_urls():
    assert is_safe_fetch_url(
        "https://github.com/Better-Quality-Assurance/betterflow-sync/releases/download/v1.5.50/BetterFlow-macOS-arm64.dmg"
    )
    assert is_safe_fetch_url(
        "https://github.com/ActivityWatch/activitywatch/releases/download/v0.13.2/activitywatch-v0.13.2-macos-x86_64.zip"
    )


def test_accepts_github_asset_cdn_subdomain():
    # GitHub redirects asset downloads to objects.githubusercontent.com.
    assert is_safe_fetch_url("https://objects.githubusercontent.com/github-production-release-asset/x.dmg")


def test_rejects_non_https():
    assert not is_safe_fetch_url("http://github.com/x/y/releases/download/z.dmg")


def test_rejects_off_allowlist_hosts():
    assert not is_safe_fetch_url("https://evil.example.com/payload.dmg")
    # Substring tricks must not pass — only exact host or true subdomain.
    assert not is_safe_fetch_url("https://github.com.evil.com/x.dmg")
    assert not is_safe_fetch_url("https://notgithub.com/x.dmg")


def test_rejects_garbage():
    assert not is_safe_fetch_url("")
    assert not is_safe_fetch_url("not a url")
    assert not is_safe_fetch_url("file:///etc/passwd")
