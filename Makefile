# BetterFlow Sync - Build Makefile

.PHONY: install install-dev test lint format clean build build-mac build-windows run

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

# Build for current platform
build:
	pyinstaller build.spec --clean

# Build for macOS
build-mac:
	pyinstaller build.spec --clean
	@echo "Built: dist/BetterFlow Sync.app"

# Build for Windows (run on Windows)
build-windows:
	pyinstaller build.spec --clean
	@echo "Built: dist/BetterFlow Sync.exe"

# Run the application (development)
run:
	python -m src.main

# Create macOS DMG (requires create-dmg)
dmg: build-mac
	create-dmg \
		--volname "BetterFlow Sync" \
		--window-pos 200 120 \
		--window-size 600 400 \
		--icon-size 100 \
		--icon "BetterFlow Sync.app" 150 190 \
		--app-drop-link 450 185 \
		"dist/BetterFlow Sync.dmg" \
		"dist/BetterFlow Sync.app"

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
