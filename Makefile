# BetterFlow - Build Makefile

.PHONY: install install-dev install-mac test lint format clean build build-mac build-windows build-linux appimage run download-aw clean-aw

# Install production dependencies
install:
	pip install -r requirements.txt

# Install development dependencies
install-dev:
	pip install -r requirements-dev.txt

# Run tests
test:
	pytest tests/ -v --cov=src --cov-report=term-missing

# Run linter
lint:
	ruff check src/ tests/

# Format code
format:
	ruff format src/ tests/

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Download ActivityWatch binaries for current platform
download-aw:
	python scripts/download_aw.py

# Clean tracker binaries
clean-aw:
	rm -rf resources/trackers/

# Build for current platform
build: download-aw
	pyinstaller build.spec --clean

# Build for macOS
build-mac: download-aw
	pyinstaller build.spec --clean
	@$(MAKE) sign-mac
	@echo "Built: dist/BetterFlow.app"

# Install dist/BetterFlow.app into /Applications, terminating any running
# instance first and waiting for its shutdown to complete before replacing
# the bundle. See scripts/install-mac.sh for the kill-grace logic.
install-mac:
	./scripts/install-mac.sh

# Sign the built .app via scripts/sign-mac.sh (inside-out signing).
# Identity is hardcoded in the script: "Developer ID Application:
# Better Quality Assurance SRL (87NVC57J44)". The script refuses to
# run if the cert is missing from the Keychain. See docs/SIGNING.md.
sign-mac:
	@if [ ! -d "dist/BetterFlow.app" ]; then \
		echo "[sign-mac] dist/BetterFlow.app not found — run build-mac first"; \
		exit 1; \
	fi
	./scripts/sign-mac.sh dist/BetterFlow.app

# Submit a signed DMG to Apple's notary service and wait synchronously
# for the verdict. On rejection, prints the notary log before failing.
# Uses keychain profile 'betterqa' — set up via PF.3 in
# docs/superpowers/plans/2026-06-03-notarized-ship.md.
#
# Override DMG path:  make notarize-mac NOTARIZE_DMG=path/to/file.dmg
NOTARIZE_DMG ?= dist/BetterFlow-macOS-arm64.dmg

notarize-mac:
	python3 scripts/notarize-mac.py "$(NOTARIZE_DMG)"

# Staple the notarization ticket onto the DMG (and the .app inside
# dist/, if present). Stapling embeds the ticket so Gatekeeper does
# not need to phone home on first launch.
#
# Override:  make staple-mac STAPLE_DMG=path/to/file.dmg
STAPLE_DMG ?= dist/BetterFlow-macOS-arm64.dmg
STAPLE_APP ?= dist/BetterFlow.app

staple-mac:
	@echo "[staple-mac] Stapling $(STAPLE_DMG)"
	xcrun stapler staple "$(STAPLE_DMG)"
	xcrun stapler validate "$(STAPLE_DMG)"
	@if [ -d "$(STAPLE_APP)" ]; then \
		echo "[staple-mac] Stapling $(STAPLE_APP)"; \
		xcrun stapler staple "$(STAPLE_APP)"; \
		xcrun stapler validate "$(STAPLE_APP)"; \
	fi
	@echo "[staple-mac] Done"

# Build for Windows (run on Windows)
build-windows: download-aw
	pyinstaller build.spec --clean
	@echo "Built: dist/BetterFlow.exe"

# Build for Linux (run on Linux) — produces dist/BetterFlow/ one-dir bundle
build-linux: download-aw
	pyinstaller build.spec --clean
	@echo "Built: dist/BetterFlow/"

# Package the Linux build as a single portable AppImage
appimage: build-linux
	./scripts/build-appimage.sh
	@echo "Built: dist/BetterFlow-linux-x86_64.AppImage"

# Run the application (development)
run:
	.venv-arm64/bin/python -m src.main

# Create an arch-suffixed macOS DMG. TARGET_ARCH defaults to the host
# arch (via uname). update_checker._find_platform_asset requires the
# arch string in the asset filename, so the suffix is mandatory for
# the in-app updater to pick the correct download.
TARGET_ARCH ?= $(shell uname -m | sed 's/aarch64/arm64/')

dmg: build-mac
	@dmg_path="dist/BetterFlow-macOS-$(TARGET_ARCH).dmg"; \
	rm -f "$$dmg_path"; \
	create-dmg \
		--volname "BetterFlow" \
		--volicon "resources/icon.icns" \
		--window-pos 200 120 \
		--window-size 600 400 \
		--icon-size 100 \
		--icon "BetterFlow.app" 150 190 \
		--app-drop-link 450 185 \
		"$$dmg_path" \
		"dist/BetterFlow.app"; \
	echo "[dmg] Created $$dmg_path"
	rm -rf "dist/BetterFlow"
	@# Set custom file icon on the DMG so it shows BetterFlow logo in Finder
	@dmg_path="dist/BetterFlow-macOS-$(TARGET_ARCH).dmg"; \
	python3 -c "import Cocoa, os; \
ws = Cocoa.NSWorkspace.sharedWorkspace(); \
img = Cocoa.NSImage.alloc().initWithContentsOfFile_(os.path.abspath('resources/icon.png')); \
ws.setIcon_forFile_options_(img, os.path.abspath('$$dmg_path'), 0); \
print('Custom icon set on', '$$dmg_path')"

# Development server (auto-reload)
dev:
	watchmedo auto-restart -d src/ -p "*.py" -- python -m src.main

# Full release pipeline: build, sign, notarize, staple for both
# architectures. Runs serially. Each architecture takes ~5-15 min
# wall-clock (PyInstaller build + Apple notary turnaround).
#
# Does NOT tag or push — those remain manual gates to avoid
# accidentally cutting a release from a dirty working copy.
# Both archs share dist/, so they must run serially even under `make -jN`.
.NOTPARALLEL: ship ship-arm64 ship-x86_64

ship: ship-arm64 ship-x86_64
	@echo "[ship] Both architectures shipped:"
	@ls -la dist/BetterFlow-macOS-*.dmg

ship-arm64:
	@echo "[ship] === arm64 ==="
	rm -rf dist build
	# Use the arm64 venv directly. `make build-mac` resolves to
	# /usr/local/bin/pyinstaller (Homebrew x86_64 Python), which
	# cannot satisfy TARGET_ARCH=arm64.
	TARGET_ARCH=arm64 .venv-arm64/bin/python -m PyInstaller build.spec --clean
	./scripts/sign-mac.sh dist/BetterFlow.app
	TARGET_ARCH=arm64 $(MAKE) _dmg-only
	NOTARIZE_DMG=dist/BetterFlow-macOS-arm64.dmg $(MAKE) notarize-mac
	STAPLE_DMG=dist/BetterFlow-macOS-arm64.dmg $(MAKE) staple-mac
	mv dist/BetterFlow.app dist/BetterFlow-arm64.app
	# Drop the COLLECT one-dir orphan before x86_64 runs PyInstaller,
	# which refuses to overwrite a non-empty output dir.
	rm -rf dist/BetterFlow

ship-x86_64:
	@echo "[ship] === x86_64 ==="
	# Explicit cleanup of every PyInstaller output path before re-build.
	# `rm -rf build` alone leaves dist/BetterFlow/ from arm64 in place
	# and PyInstaller aborts: "output directory ... is not empty".
	rm -rf build dist/BetterFlow dist/BetterFlow.app
	# build.spec reads TARGET_ARCH; PyInstaller runs under Rosetta via
	# the x86_64 venv. .app overwrites the renamed arm64 build above.
	TARGET_ARCH=x86_64 arch -x86_64 .venv-x86_64/bin/python -m PyInstaller build.spec --clean
	./scripts/sign-mac.sh dist/BetterFlow.app
	TARGET_ARCH=x86_64 $(MAKE) _dmg-only
	NOTARIZE_DMG=dist/BetterFlow-macOS-x86_64.dmg $(MAKE) notarize-mac
	STAPLE_DMG=dist/BetterFlow-macOS-x86_64.dmg $(MAKE) staple-mac
	mv dist/BetterFlow.app dist/BetterFlow-x86_64.app
	rm -rf dist/BetterFlow

# Internal: package whatever is currently in dist/BetterFlow.app as
# dist/BetterFlow-macOS-$(TARGET_ARCH).dmg. Used by both ship targets
# so the create-dmg block stays in one place.
_dmg-only:
	@dmg_path="dist/BetterFlow-macOS-$(TARGET_ARCH).dmg"; \
	rm -f "$$dmg_path"; \
	create-dmg \
		--volname "BetterFlow" \
		--volicon "resources/icon.icns" \
		--window-pos 200 120 \
		--window-size 600 400 \
		--icon-size 100 \
		--icon "BetterFlow.app" 150 190 \
		--app-drop-link 450 185 \
		"$$dmg_path" \
		"dist/BetterFlow.app"; \
	echo "[ship] Created $$dmg_path"

# Generate icon files from PNG
icons:
	@echo "Generating icons..."
	@if [ -f resources/icon.png ]; then \
		sips -z 1024 1024 resources/icon.png --out resources/icon_1024.png; \
		iconutil -c icns resources/icon.iconset -o resources/icon.icns; \
	fi
