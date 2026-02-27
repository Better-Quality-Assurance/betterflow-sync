"""Screenshot capture and compression."""

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from PIL import ImageGrab

logger = logging.getLogger(__name__)

__all__ = ["ScreenshotData", "capture"]


@dataclass
class ScreenshotData:
    """Captured screenshot data ready for upload."""

    image_bytes: bytes
    filename: str
    timestamp: str  # ISO-8601 UTC


def capture(quality: int = 80) -> ScreenshotData:
    """Capture the screen and compress to JPEG.

    Args:
        quality: JPEG compression quality (1-100).

    Returns:
        ScreenshotData with compressed image bytes.

    Raises:
        OSError: If screen capture fails (e.g. no display).
    """
    now = datetime.now(timezone.utc)
    image = ImageGrab.grab()

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True)
    image_bytes = buf.getvalue()

    filename = f"screenshot_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
    timestamp = now.isoformat()

    logger.debug(f"Screenshot captured: {filename} ({len(image_bytes)} bytes)")
    return ScreenshotData(
        image_bytes=image_bytes,
        filename=filename,
        timestamp=timestamp,
    )
