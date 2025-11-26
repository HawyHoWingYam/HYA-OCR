import os
import json
import tempfile
import logging
from typing import Any, Dict, List, Optional

from fastapi import UploadFile

from main import extract_text_from_image, extract_text_from_pdf
from utils.prompt_schema_manager import load_prompt_and_schema

logger = logging.getLogger(__name__)


class GeminiOcrResult:
    """Container for a single file's OCR result."""

    def __init__(
        self,
        index: int,
        filename: str,
        content_type: Optional[str],
        data: Any,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        processing_time: Optional[float] = None,
        status_updates: Optional[Dict[str, Any]] = None,
    ):
        self.index = index
        self.filename = filename
        self.content_type = content_type
        self.data = data
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.processing_time = processing_time
        self.status_updates = status_updates or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "filename": self.filename,
            "content_type": self.content_type,
            "data": self.data,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "processing_time": self.processing_time,
            "status_updates": self.status_updates,
        }


class GeminiOcrError:
    """Container for a single file's OCR error."""

    def __init__(self, index: int, filename: str, error: str):
        self.index = index
        self.filename = filename
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "filename": self.filename,
            "error": self.error,
        }


async def process_ocr_batch(
    files: List[UploadFile],
    company_code: str,
    doc_type_code: str,
) -> Dict[str, Any]:
    """
    Process a batch of uploaded files (images/PDFs) with Gemini OCR.

    This function:
    - Loads prompt and schema via PromptSchemaManager using company/doc type codes.
    - Runs Gemini OCR once per file (sequentially for now).
    - Parses the JSON text payload (if possible).
    - Returns an aggregated structure suitable for JSON response and CSV generation.
    """
    if not files:
        return {
            "summary": {
                "company_code": company_code,
                "doc_type_code": doc_type_code,
                "total_files": 0,
                "processed_files": 0,
                "failed_files": 0,
            },
            "results": [],
            "errors": [],
        }

    # Load prompt and schema once for this batch
    prompt, schema = await load_prompt_and_schema(company_code, doc_type_code)

    if not prompt:
        # Fallback generic prompt to avoid hard failure
        prompt = (
            f"Extract structured information as JSON from the provided document. "
            f"Document type: {doc_type_code}, Company: {company_code}."
        )
        logger.warning(
            "No prompt found via PromptSchemaManager for %s/%s, using generic prompt",
            company_code,
            doc_type_code,
        )

    results: List[GeminiOcrResult] = []
    errors: List[GeminiOcrError] = []

    for index, upload in enumerate(files):
        filename = upload.filename or f"file_{index}"
        content_type = upload.content_type

        # Only support common image/PDF types for this batch OCR
        ext = os.path.splitext(filename)[1].lower()
        is_pdf = ext == ".pdf" or (content_type == "application/pdf")
        is_image = ext in [".jpg", ".jpeg", ".png"] or (
            content_type is not None and content_type.startswith("image/")
        )

        if not (is_pdf or is_image):
            error_msg = "Unsupported file type for OCR. Please upload images or PDFs."
            logger.warning("Skipping unsupported file %s: %s", filename, error_msg)
            errors.append(GeminiOcrError(index=index, filename=filename, error=error_msg))
            continue

        # Persist to a temporary file for processing
        tmp_path: Optional[str] = None
        try:
            suffix = ext if ext else ""
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                file_bytes = await upload.read()
                tmp.write(file_bytes)
                tmp_path = tmp.name

            raw_result = None
            if is_pdf:
                raw_result = await extract_text_from_pdf(tmp_path, prompt, schema)
            else:
                raw_result = await extract_text_from_image(tmp_path, prompt, schema)

            # Normalise the Gemini response
            text_payload: Optional[str] = None
            input_tokens: Optional[int] = None
            output_tokens: Optional[int] = None
            processing_time: Optional[float] = None
            status_updates: Dict[str, Any] = {}

            if isinstance(raw_result, dict):
                text_payload = raw_result.get("text")
                input_tokens = raw_result.get("input_tokens")
                output_tokens = raw_result.get("output_tokens")
                processing_time = raw_result.get("processing_time")
                status_updates = raw_result.get("status_updates") or {}
            elif isinstance(raw_result, str):
                text_payload = raw_result

            parsed_data: Any = None
            if text_payload:
                try:
                    parsed_data = json.loads(text_payload)
                except json.JSONDecodeError:
                    # Fallback: Keep raw text under a dedicated key
                    parsed_data = {"raw_text": text_payload}
            else:
                parsed_data = {"raw_text": None}

            result = GeminiOcrResult(
                index=index,
                filename=filename,
                content_type=content_type,
                data=parsed_data,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                processing_time=processing_time,
                status_updates=status_updates,
            )
            results.append(result)

        except Exception as exc:
            logger.error("Error processing file %s: %s", filename, exc)
            errors.append(
                GeminiOcrError(index=index, filename=filename, error=str(exc))
            )
        finally:
            # Ensure temporary file is removed
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception as cleanup_exc:
                    logger.warning(
                        "Failed to remove temporary file %s: %s", tmp_path, cleanup_exc
                    )

    aggregated: Dict[str, Any] = {
        "summary": {
            "company_code": company_code,
            "doc_type_code": doc_type_code,
            "total_files": len(files),
            "processed_files": len(results),
            "failed_files": len(errors),
        },
        "results": [r.to_dict() for r in results],
        "errors": [e.to_dict() for e in errors],
    }

    return aggregated

