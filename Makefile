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

# Generate icon files from PNG
icons:
	@echo "Generating icons..."
	@if [ -f resources/icon.png ]; then \
		sips -z 1024 1024 resources/icon.png --out resources/icon_1024.png; \
		iconutil -c icns resources/icon.iconset -o resources/icon.icns; \
	fi
