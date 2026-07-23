#!/usr/bin/env bash
#
# Resolve a Python interpreter that (a) has PyInstaller installed and (b) runs
# on the architecture we intend to build for. Prints the interpreter path on
# stdout; every diagnostic goes to stderr so callers can do:
#
#     PY=$(./scripts/resolve-pyinstaller.sh) && "$PY" -m PyInstaller build.spec
#
# Why this exists: `which pyinstaller` on a machine that has been through a
# Rosetta migration can resolve to /usr/local/bin/pyinstaller — the *Intel*
# Homebrew prefix — which either dies with "bad interpreter" or, worse, builds
# a silently x86_64 .app on an arm64 host. A wrong-arch bundle ships to an
# Apple Silicon fleet and runs under Rosetta with nothing in the artifact name
# to say so. So: never trust PATH, and always verify the arch.
#
# Search order (first candidate that satisfies BOTH checks wins):
#   1. $PYINSTALLER_PYTHON  (explicit override; must still pass both checks)
#   2. $VIRTUAL_ENV/bin/python3
#   3. .venv-<arch>/bin/python3
#   4. venv/bin/python3
#   5. .venv/bin/python3
#   6. python3 from PATH
#
# Desired arch: $1, else $TARGET_ARCH, else `uname -m` (aarch64 -> arm64).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

host_arch="$(uname -m | sed 's/aarch64/arm64/')"
want_arch="${1:-${TARGET_ARCH:-$host_arch}}"

say() { printf '[resolve-pyinstaller] %s\n' "$*" >&2; }

# Prints "<version> <machine>" if the interpreter has PyInstaller, else nothing.
probe() {
    "$1" -c 'import PyInstaller, platform; print(PyInstaller.__version__, platform.machine())' 2>/dev/null
}

candidates=()
[ -n "${PYINSTALLER_PYTHON:-}" ] && candidates+=("$PYINSTALLER_PYTHON")
[ -n "${VIRTUAL_ENV:-}" ] && candidates+=("$VIRTUAL_ENV/bin/python3")
candidates+=(
    "$PROJECT_ROOT/.venv-$want_arch/bin/python3"
    "$PROJECT_ROOT/venv/bin/python3"
    "$PROJECT_ROOT/.venv/bin/python3"
)
path_python="$(command -v python3 || true)"
[ -n "$path_python" ] && candidates+=("$path_python")

rejected=()
reject() {
    rejected+=("$1")
    # Announce every rejection as it happens: a candidate skipped silently is
    # exactly how the wrong toolchain gets used without anyone noticing.
    say "rejected $1"
}

for py in "${candidates[@]}"; do
    if [ ! -x "$py" ]; then
        reject "$py — not executable / does not exist"
        continue
    fi
    info="$(probe "$py" || true)"
    if [ -z "$info" ]; then
        reject "$py — PyInstaller not importable"
        continue
    fi
    version="${info%% *}"
    machine="${info##* }"
    if [ "$machine" != "$want_arch" ]; then
        reject "$py — PyInstaller $version but runs as $machine, wanted $want_arch"
        continue
    fi
    say "using $py (PyInstaller $version, $machine)"
    printf '%s\n' "$py"
    exit 0
done

say "ERROR: no Python with PyInstaller running as $want_arch was found."
say "Rejected candidates, in search order:"
for r in "${rejected[@]}"; do say "  - $r"; done
say ""
say "This guard exists because PATH's pyinstaller on this machine can be the"
say "stale Intel Homebrew one (/usr/local/bin/pyinstaller), which builds an"
say "x86_64 app on an arm64 host without saying so."
say ""
say "Fix: create/refresh the arch-matched venv, e.g."
say "  python3 -m venv .venv-$want_arch && .venv-$want_arch/bin/pip install -r requirements.txt pyinstaller"
say "or point the build at an existing one:"
say "  PYINSTALLER_PYTHON=/path/to/venv/bin/python3 make build-mac"
exit 1
