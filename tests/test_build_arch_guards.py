"""The two build-time paths that can ship an x86_64 tree inside an arm64 DMG.

Found auditing #217 (native arm64 trackers). Neither is a runtime defect -- the
shipped gate correctly asks what the BINARY needs -- and neither fires on the
`make ship-*` path CI uses. They fire on the path the project's own CLAUDE.md
tells a human to use, and they compose: gap 1 is how the tree stays wrong, gap 2
is why nothing downstream catches it.

GAP 1  scripts/download_aw.py
  The SKIP clause was widened to `binaries_exist(...) and not arch_mismatch(...)`
  so a leftover tree of the other architecture no longer counts as "already
  present". The POST-EXTRACTION verification was not: it still asks
  `binaries_exist` alone. So when extract_binaries hits its missing-launcher
  branch and returns with no exit code, a complete stale tree of the WRONG
  architecture satisfies the check and main prints
  "Done! All binaries downloaded successfully." and exits 0.

GAP 2  Makefile / build.spec
  build.spec's mismatch guard is behind `if TARGET_ARCH:`, reading it from the
  ENVIRONMENT. The Makefile declares `TARGET_ARCH ?=` as a MAKE variable and
  never exports it, and `build-mac` invokes PyInstaller with no prefix -- so the
  guard is inert on `make dmg`, `make build-mac` and `make pkg`. Only CI and
  `make ship-*`, which prefix the command themselves, arm it. The Makefile even
  carries a comment asserting the export exists.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import download_aw  # noqa: E402


class TestGap1PostExtractionAsksTheArchQuestion:
    def test_a_stale_wrong_arch_tree_is_not_reported_as_success(self, monkeypatch, capsys, tmp_path):
        """The exact sequence: a complete tree of the OTHER architecture is on
        disk, the archive turns out to be missing launchers, extraction returns
        without raising -- and main must NOT claim success."""
        monkeypatch.setattr(download_aw, "binaries_exist", lambda *a, **k: True)
        # the tree present is the WRONG architecture -- this is what the
        # post-extraction check was blind to
        monkeypatch.setattr(download_aw, "arch_mismatch", lambda *a, **k: True)
        monkeypatch.setattr(download_aw, "download_release", lambda *a, **k: str(tmp_path / "x.zip"))
        monkeypatch.setattr(download_aw, "verify_digest", lambda *a, **k: None)
        monkeypatch.setattr(download_aw, "extract_binaries", lambda *a, **k: None)  # the silent-return branch
        monkeypatch.setattr(download_aw, "fix_permissions", lambda *a, **k: None)
        monkeypatch.setattr(download_aw.os, "unlink", lambda *a, **k: None)
        monkeypatch.setattr(sys, "argv", ["download_aw.py"])

        with pytest.raises(SystemExit) as exc:
            download_aw.main()

        assert exc.value.code != 0, (
            "exited 0 leaving the previous architecture's trackers on disk -- "
            "the next PyInstaller run bundles them"
        )
        assert "Done!" not in capsys.readouterr().out

    def test_a_correct_tree_still_reports_success(self, monkeypatch, capsys, tmp_path):
        """Control: the ordinary happy path must not start failing."""
        monkeypatch.setattr(download_aw, "binaries_exist", lambda *a, **k: True)
        monkeypatch.setattr(download_aw, "arch_mismatch", lambda *a, **k: False)
        monkeypatch.setattr(download_aw, "download_release", lambda *a, **k: str(tmp_path / "x.zip"))
        monkeypatch.setattr(download_aw, "verify_digest", lambda *a, **k: None)
        monkeypatch.setattr(download_aw, "extract_binaries", lambda *a, **k: None)
        monkeypatch.setattr(download_aw, "fix_permissions", lambda *a, **k: None)
        monkeypatch.setattr(download_aw.os, "unlink", lambda *a, **k: None)
        monkeypatch.setattr(sys, "argv", ["download_aw.py"])

        # skip clause returns early on a correct, present tree -- no SystemExit
        download_aw.main()
        assert "skipping download" in capsys.readouterr().out


    def test_a_real_extraction_that_succeeds_reports_success(self, monkeypatch, capsys, tmp_path):
        """Witness the ALLOWANCE, through the extraction path.

        The other control returns early on the SKIP clause and never reaches the
        verify block, so it cannot see an over-refusal there. Without this, a
        mutant that makes the verify always fail -- which would break every
        build -- passes the whole suite. It did: found by the mutation matrix,
        not by reading.
        """
        calls = {"n": 0}

        def _exists(*a, **k):
            # absent at the skip clause (so we download), present after extraction
            calls["n"] += 1
            return calls["n"] > 1

        monkeypatch.setattr(download_aw, "binaries_exist", _exists)
        monkeypatch.setattr(download_aw, "arch_mismatch", lambda *a, **k: False)
        monkeypatch.setattr(download_aw, "download_release", lambda *a, **k: str(tmp_path / "x.zip"))
        monkeypatch.setattr(download_aw, "verify_digest", lambda *a, **k: None)
        monkeypatch.setattr(download_aw, "extract_binaries", lambda *a, **k: None)
        monkeypatch.setattr(download_aw, "fix_permissions", lambda *a, **k: None)
        monkeypatch.setattr(download_aw.os, "unlink", lambda *a, **k: None)
        monkeypatch.setattr(sys, "argv", ["download_aw.py"])

        download_aw.main()

        out = capsys.readouterr().out
        assert calls["n"] > 1, "precondition: the fixture must reach the post-extraction verify"
        assert "Done!" in out
        assert "ERROR" not in out


class TestGap2TheMakefileExportsTargetArch:
    """Drives the REAL Makefile, rather than asserting on its text.

    A source-shape check ("does the file contain `export`") would pass on a
    commented-out export, and this Makefile already carries a comment CLAIMING
    the export exists. Only asking make what a recipe's child process sees
    distinguishes the two.
    """

    def _child_sees(self, extra_env=None):
        probe = (REPO / "Makefile").read_text(encoding="utf-8") + (
            "\n_probe_target_arch:\n\t@echo \"[$$TARGET_ARCH]\"\n"
        )
        out = subprocess.run(
            ["make", "-f", "-", "_probe_target_arch"],
            input=probe, capture_output=True, text=True, cwd=REPO,
            env={**os.environ, **(extra_env or {})},
        )
        return out.stdout.strip()

    def test_a_recipe_child_process_sees_target_arch(self):
        seen = self._child_sees()
        assert seen not in ("[]", ""), (
            "TARGET_ARCH is a make variable only, so build.spec's `if TARGET_ARCH:` "
            "mismatch guard is INERT on make dmg / build-mac / pkg"
        )

    def test_an_explicit_override_reaches_the_child(self):
        """Control: `make TARGET_ARCH=x86_64 ...` must still win, or ship-* breaks."""
        assert self._child_sees({"TARGET_ARCH": "x86_64"}) == "[x86_64]"
