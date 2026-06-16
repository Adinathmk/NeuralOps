import hashlib
import logging
import os

import google.generativeai as genai
from google.api_core.exceptions import RetryError, InternalServerError, ResourceExhausted

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_configured = False

def configure_gemini():
    global _configured
    if not _configured:
        # We rely on GEMINI_API_KEY environment variable. If not set, it will fail gracefully.
        api_key = os.environ.get("GEMINI_API_KEY") or settings.GEMINI_API_KEY
        if api_key:
            genai.configure(api_key=api_key)
            _configured = True
        else:
            logger.warning("GEMINI_API_KEY is not set. Embeddings will fail if called.")

def build_playbook_embed_text(error_pattern: str, instructions: str) -> str:
    """
    Construct embedding input for a playbook.
    Encodes both trigger condition and remediation intent.
    """
    parts = []
    if error_pattern and error_pattern.strip():
        parts.append(f"Error pattern: {error_pattern.strip()}")
    if instructions and instructions.strip():
        parts.append(f"Instructions: {instructions.strip()}")
    if not parts:
        raise ValueError("Playbook has no content to embed")
    return " | ".join(parts)


def build_query_embed_text(
    error_type: str,
    stack_trace_summary: str,
    service_name: str,
    file_path: str,
) -> str:
    """Query embedding text for ANN playbook search at agent runtime."""
    return (
        f"Error type: {error_type} | "
        f"Service: {service_name} | "
        f"File: {file_path} | "
        f"Stack: {stack_trace_summary}"
    )


@retry(
    retry=retry_if_exception_type(
        (RetryError, InternalServerError, ResourceExhausted)
    ),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
def embed_text(text_input: str) -> list[float]:
    """
    Embed a single text string using Google Gemini's embedding model.
    Returns a 768-dimensional float list.
    Retries on transient Google AI errors up to 4 times with exponential backoff.
    Re-raises on non-transient errors (auth failure, invalid input).
    """
    if not text_input or not text_input.strip():
        raise ValueError("Cannot embed empty text")

    configure_gemini()
    
    response = genai.embed_content(
        model=settings.EMBEDDING_MODEL,
        content=text_input.strip(),
        task_type="retrieval_document",
        output_dimensionality=settings.EMBEDDING_DIMENSIONS
    )
    return response['embedding']


def query_text_hash(text_input: str) -> str:
    """16-char SHA-256 hex digest — used as Redis cache key suffix."""
    return hashlib.sha256(text_input.encode()).hexdigest()[:16]
