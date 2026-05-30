"""Multimedia route handlers."""

from .image_route import process_image
from .pdf_route import process_pdf
from .text_route import process_text
from .video_route import process_video

__all__ = ["process_pdf", "process_image", "process_video", "process_text"]
