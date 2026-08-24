"""The pinned ActivityWatch release: version, assets, digests, and naming.

**One copy, imported by everyone.** This module exists because the pin used to
live in two places that nothing compared:

    src/aw_manager.py     AW_VERSION + RELEASE_ASSETS + RELEASE_SHA256
    scripts/download_aw.py  a second AW_VERSION + a second RELEASE_ASSETS

`scripts/verify_tracker_pins.py` imports the first, so only the first was ever
checked — while `.github/workflows/build.yml` fetches the shipped binaries with
the *second*. A drift between them means the build downloads one version and the
running agent pins the digests of another, with the nightly pin guard green
throughout. The two copies happened to agree, which is not the same as being
prevented from disagreeing.

**Why macOS has an architecture dimension and the others do not.** Upstream
publishes both `macos-arm64` and `macos-x86_64` from v0.14.0b2 onward, and
picking the wrong one is the whole Rosetta problem (#216). Windows and Linux
each publish a single x86_64 build, so there is nothing to choose:

- Windows on ARM transparently emulates x64, so an ARM Windows host running the
  x86_64 trackers works today. Adding an arch dimension there would key on
  `arm64`, find no asset, and break a configuration that currently functions.
- Linux publishes no arm64 build at all, so an arm64 Linux host has no working
  option either way. It now fails with "no release for this platform" instead of
  downloading an x86_64 archive it cannot execute, which is the same outcome
  reported honestly.

Keep this module dependency-free. `scripts/download_aw.py` imports it during the
build, before the app's dependencies are necessarily importable, and
`verify_tracker_pins.py` runs it in CI.
"""

import platform
from typing import Optional

ARM64 = "arm64"
X86_64 = "x86_64"

AW_VERSION = "v0.14.0b4"

RELEASE_BASE = (
    f"https://github.com/ActivityWatch/activitywatch/releases/download/{AW_VERSION}"
)

# Keys are the ASSET KEY (see `asset_key`), not the bare platform: macOS carries
# an architecture, Windows and Linux do not. See the module docstring for why
# that asymmetry is deliberate rather than an oversight.
RELEASE_ASSETS = {
    f"darwin-{ARM64}": f"activitywatch-{AW_VERSION}-macos-arm64.zip",
    f"darwin-{X86_64}": f"activitywatch-{AW_VERSION}-macos-x86_64.zip",
    "windows": f"activitywatch-{AW_VERSION}-windows-x86_64.zip",
    "linux": f"activitywatch-{AW_VERSION}-linux-x86_64.zip",
}

# SHA-256 of the vetted RELEASE_ASSETS archives above. GitHub release assets on
# a pinned tag are mutable by the upstream account, so the version pin alone is
# not an integrity guarantee — a compromised ActivityWatch account could swap
# the binaries under the same tag. The download is verified against these
# hashes before extraction (fail closed on mismatch). MUST be recomputed on
# every AW_VERSION bump (shasum -a 256 on the freshly-vetted zips).
#
# Provenance: computed 2026-08-24 from the upstream v0.14.0b4 release assets.
# These literals are NOT self-verifying — the unit test only compares them to a
# second hand-copied record, so a wrong value passes tests and instead fails
# closed on the fleet (no trackers installed). scripts/verify_tracker_pins.py
# fetches the real archives and checks the digests; it runs nightly via
# .github/workflows/verify-tracker-pins.yml and on any change to this file. Run
# it locally after every AW_VERSION bump.
RELEASE_SHA256 = {
    f"darwin-{ARM64}": "98a142c47aadc3873cf3e6f4e71c28c4897a4b48868e4586ed08680c23f06584",
    f"darwin-{X86_64}": "090b91b269b2d18049c44b4d10f9142bcd7c72269b199a570665927d5521f665",
    "windows": "c7acb66d5824aeeef17e0c941efd1f0dbaf216e112260972efa21cff40c25832",
    "linux": "5f608c7c1a717e98b9e46738a0d6aca2906b73d70271fc9882bbabb9aebbbf76",
}

# Mapping from original AW names to our branded names (used during
# download/extract). The archive lays each one out as
# `<prefix>/<component>/<launcher>`; the extractor derives the component root
# from the launcher's path, so an upstream change to `<prefix>` is a no-op.
AW_TO_BF_NAMES = {
    "aw-server-rust": "bf-data-service",
    "aw-watcher-window": "bf-window-tracker",
    "aw-watcher-afk": "bf-idle-tracker",
}


def platform_key(system: Optional[str] = None) -> str:
    """`darwin` / `windows` / `linux` — the on-disk tracker directory name.

    Kept separate from `asset_key` on purpose. This one names the directory the
    binaries are installed into and bundled from, and that layout is per-OS: a
    build ships exactly one architecture, so there is nothing for an arch
    suffix to disambiguate on disk.

    Args:
        system: Override `platform.system()` for testing.
    """
    system = system or platform.system()
    if system == "Darwin":
        return "darwin"
    if system == "Windows":
        return "windows"
    return "linux"


def asset_key(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """The RELEASE_ASSETS / RELEASE_SHA256 key for this host.

    Keys on the **running process's** architecture rather than the hardware's,
    and that is the load-bearing choice. `machine_arch.true_machine_arch()`
    resolves through Rosetta and would answer `arm64` for an x86_64 build
    translated on Apple Silicon — which would hand that install arm64 trackers
    chosen on the strength of silicon it is not itself using. What matters here
    is what this host has *proven* it can execute, and a running process is that
    proof for its own architecture.

    So an Intel build under Rosetta keeps getting x86_64 trackers (Rosetta is
    demonstrably present — it is running the app), and a native arm64 build gets
    arm64 trackers and needs no Rosetta at all.

    Args:
        system: Override `platform.system()` for testing.
        machine: Override `platform.machine()` for testing.
    """
    plat = platform_key(system)
    if plat != "darwin":
        return plat
    # `is None`, NOT `or`. `platform.machine()` is documented to return "" when
    # it cannot determine one, so an empty string is a real value this has to
    # handle — and with `or` it is also the one value a test could never inject,
    # because the override would silently fall back to the host's real answer.
    if machine is None:
        machine = platform.machine()
    # Anything that is not arm64 takes the Intel asset. macOS runs on exactly
    # these two architectures, and defaulting the unknown case to x86_64 keeps
    # a host reporting something unexpected on the build Rosetta can translate.
    return f"darwin-{ARM64}" if machine == ARM64 else f"darwin-{X86_64}"
