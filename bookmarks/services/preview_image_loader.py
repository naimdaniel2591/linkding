import hashlib
import logging
import mimetypes
import os.path
import uuid
from collections.abc import Callable
from pathlib import Path

import requests
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile

from bookmarks.services import website_loader

logger = logging.getLogger(__name__)


def _ensure_preview_folder():
    Path(settings.LD_PREVIEW_FOLDER).mkdir(parents=True, exist_ok=True)


def _url_to_filename(preview_image: str) -> str:
    return hashlib.md5(preview_image.encode()).hexdigest()


def _get_image_path(preview_image_file: str) -> Path:
    return Path(os.path.join(settings.LD_PREVIEW_FOLDER, preview_image_file))


class PreviewImageUploadError(Exception):
    pass


class PreviewImageDownloadError(Exception):
    def __init__(
        self,
        code: str,
        *,
        error: Exception | None = None,
        details: dict | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.error = error
        self.details = details or {}


def _map_download_error_to_message(code: str) -> str:
    if code == "content_length_exceeds_max":
        return "File exceeds maximum size."
    if code == "unsupported_content_type":
        return "Unsupported file type."
    return "Failed to download image."


def _download_preview_image(
    image_url: str,
    file_name_generator: Callable[[str], str],
    *,
    log_message: str,
) -> str:
    _ensure_preview_folder()

    try:
        with requests.get(image_url, stream=True) as response:
            if response.status_code < 200 or response.status_code >= 300:
                raise PreviewImageDownloadError(
                    "bad_status", details={"status_code": response.status_code}
                )

            content_length_header = response.headers.get("Content-Length")
            if not content_length_header:
                raise PreviewImageDownloadError("missing_content_length")

            try:
                content_length = int(content_length_header)
            except (TypeError, ValueError):
                raise PreviewImageDownloadError("missing_content_length")

            if content_length > settings.LD_PREVIEW_MAX_SIZE:
                raise PreviewImageDownloadError(
                    "content_length_exceeds_max",
                    details={"content_length": content_length},
                )

            content_type_header = response.headers.get("Content-Type")
            if not content_type_header:
                raise PreviewImageDownloadError("missing_content_type")

            content_type = content_type_header.split(";", 1)[0]
            file_extension = mimetypes.guess_extension(content_type)

            if not file_extension or file_extension not in settings.LD_PREVIEW_ALLOWED_EXTENSIONS:
                raise PreviewImageDownloadError(
                    "unsupported_content_type",
                    details={"content_type": content_type},
                )

            preview_image_file = file_name_generator(file_extension)
            preview_image_path = _get_image_path(preview_image_file)

            bytes_written = 0

            try:
                with open(preview_image_path, "wb") as file:
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue

                        bytes_written += len(chunk)
                        if bytes_written > settings.LD_PREVIEW_MAX_SIZE:
                            raise PreviewImageDownloadError(
                                "content_length_exceeds_max",
                                details={"content_length": bytes_written},
                            )
                        if bytes_written > content_length:
                            raise PreviewImageDownloadError(
                                "content_length_mismatch",
                                details={
                                    "content_length": content_length,
                                    "downloaded": bytes_written,
                                },
                            )

                        file.write(chunk)
            except PreviewImageDownloadError:
                if preview_image_path.exists():
                    preview_image_path.unlink()
                raise
            except Exception:
                if preview_image_path.exists():
                    preview_image_path.unlink()
                raise

    except PreviewImageDownloadError:
        raise
    except Exception as error:
        raise PreviewImageDownloadError("request_error", error=error) from error

    logger.debug(log_message.format(preview_image_path))

    return preview_image_file


def load_preview_image(url: str) -> str | None:
    _ensure_preview_folder()

    metadata = website_loader.load_website_metadata(url)
    if not metadata.preview_image:
        logger.debug(f"Could not find preview image in metadata: {url}")
        return None

    image_url = metadata.preview_image

    logger.debug(f"Loading preview image: {image_url}")

    try:
        return _download_preview_image(
            image_url,
            lambda extension: f"{_url_to_filename(url)}{extension}",
            log_message="Saved preview image as: {}",
        )
    except PreviewImageDownloadError as error:
        if error.code == "bad_status":
            logger.debug(
                "Bad response status code for preview image: %s status_code=%s",
                image_url,
                error.details.get("status_code"),
            )
        elif error.code == "missing_content_length":
            logger.debug(f"Empty Content-Length for preview image: {image_url}")
        elif error.code == "content_length_exceeds_max":
            logger.debug(
                "Content-Length exceeds LD_PREVIEW_MAX_SIZE: %s length=%s",
                image_url,
                error.details.get("content_length"),
            )
        elif error.code == "missing_content_type":
            logger.debug(f"Empty Content-Type for preview image: {image_url}")
        elif error.code == "unsupported_content_type":
            logger.debug(
                "Unsupported Content-Type for preview image: %s content_type=%s",
                image_url,
                error.details.get("content_type"),
            )
        elif error.code == "content_length_mismatch":
            logger.debug(
                "Content-Length mismatch for preview image: %s length=%s downloaded=%s",
                image_url,
                error.details.get("content_length"),
                error.details.get("downloaded"),
            )
        elif error.code == "request_error":
            logger.debug(
                f"Failed to download preview image: {image_url}",
                exc_info=error.error,
            )
        return None


def save_uploaded_preview_image(uploaded_file: UploadedFile) -> str:
    _ensure_preview_folder()

    file_extension = Path(uploaded_file.name).suffix.lower()
    if not file_extension or file_extension not in settings.LD_PREVIEW_ALLOWED_EXTENSIONS:
        raise PreviewImageUploadError("Unsupported file type.")

    file_size = getattr(uploaded_file, "size", None)
    if file_size and file_size > settings.LD_PREVIEW_MAX_SIZE:
        raise PreviewImageUploadError("File exceeds maximum size.")

    preview_image_file = f"{uuid.uuid4().hex}{file_extension}"
    preview_image_path = _get_image_path(preview_image_file)

    bytes_written = 0

    try:
        with open(preview_image_path, "wb") as file:
            for chunk in uploaded_file.chunks():
                bytes_written += len(chunk)
                if bytes_written > settings.LD_PREVIEW_MAX_SIZE:
                    raise PreviewImageUploadError("File exceeds maximum size.")
                file.write(chunk)
    except PreviewImageUploadError:
        if preview_image_path.exists():
            preview_image_path.unlink()
        raise
    except Exception:
        if preview_image_path.exists():
            preview_image_path.unlink()
        raise

    logger.debug(f"Saved uploaded preview image as: {preview_image_path}")

    return preview_image_file


def save_preview_image_from_url(image_url: str) -> str:
    if not image_url or not image_url.strip():
        raise PreviewImageUploadError("No image URL provided.")

    image_url = image_url.strip()

    try:
        return _download_preview_image(
            image_url,
            lambda extension: f"{uuid.uuid4().hex}{extension}",
            log_message="Saved uploaded preview image as: {}",
        )
    except PreviewImageDownloadError as error:
        message = _map_download_error_to_message(error.code)
        if error.code == "request_error":
            logger.debug(
                f"Failed to download preview image: {image_url}",
                exc_info=error.error,
            )
        raise PreviewImageUploadError(message)
