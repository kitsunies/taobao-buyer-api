"""Validate raster uploads and re-encode pixels without embedded metadata."""

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import SourcingError


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_SIDE = 8192


def normalize_image(data: bytes) -> tuple[bytes, str]:
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise SourcingError("invalid_image", "Supply a JPEG, PNG or WebP image up to 10 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if (source.format not in {"JPEG", "PNG", "WEBP"}
                        or source.width * source.height > MAX_IMAGE_PIXELS
                        or max(source.size) > MAX_IMAGE_SIDE or getattr(source, "is_animated", False)):
                    raise ValueError
                source.load()
                rotated = ImageOps.exif_transpose(source)
                has_alpha = "A" in rotated.getbands() or "transparency" in rotated.info
                mode = "RGBA" if has_alpha else "RGB"
                converted = rotated.convert(mode)
                clean = Image.frombytes(mode, converted.size, converted.tobytes())
                output = io.BytesIO()
                image_format = "PNG" if has_alpha else "JPEG"
                clean.save(output, format=image_format, **({"quality": 90} if image_format == "JPEG" else {}))
        result = output.getvalue()
        if len(result) > MAX_IMAGE_BYTES:
            raise ValueError
        return result, "image/png" if image_format == "PNG" else "image/jpeg"
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise SourcingError("invalid_image", "Supply a valid non-animated JPEG, PNG or WebP within the image limits.") from None
