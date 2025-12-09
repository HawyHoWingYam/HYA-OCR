import google.generativeai as genai
import os
import PIL.Image
try:
    import cv2  # Optional; used only for advanced preprocessing
except ImportError:  # pragma: no cover - environment without OpenCV
    cv2 = None
import numpy as np
import json
from datetime import datetime
import asyncio
import time
import logging
from functools import wraps
import contextlib
import uuid
import hashlib

# 導入配置管理器
try:
    from config_loader import config_loader, get_api_key_manager, get_model_tier_manager

    CONFIG_AVAILABLE = True
except ImportError:
    CONFIG_AVAILABLE = False
    logging.warning("Config loader not available, using fallback methods")

logger = logging.getLogger(__name__)


def get_gemini_timeout_seconds() -> int:
    """Resolve Gemini API timeout; fall back to 300s if config unavailable."""
    if not CONFIG_AVAILABLE:
        return 300
    try:
        return config_loader.get_gemini_timeout_seconds()
    except Exception as e:
        logger.warning("GEMINI_API_TIMEOUT invalid or missing (%s); defaulting to 300s", e)
        return 300


def get_api_key_and_model() -> tuple[str, str]:
    """獲取 API key 和模型名稱（嚴格依賴 config_loader/env，並支持模型分層）。"""
    if not CONFIG_AVAILABLE:
        raise RuntimeError("Config loader not available")
    try:
        api_key = get_api_key_manager().get_least_used_key()

        # 優先使用模型分層管理器（GEMINI_MODEL_TIERS）
        try:
            model_tier_manager = get_model_tier_manager()
            model_name = model_tier_manager.get_current_model()
        except Exception:
            app_config = config_loader.get_app_config()
            model_name = app_config.get("model_name")

        if not model_name:
            raise ValueError("MODEL_NAME not configured")
        return api_key, model_name
    except Exception as e:
        logger.error(f"Failed to get API key/model from config loader: {e}")
        raise


def configure_gemini_with_retry(api_key: str, max_retries: int = 3):
    """配置 Gemini API 並支持重試機制"""
    for attempt in range(max_retries):
        try:
            genai.configure(api_key=api_key)
            logger.info(
                f"✅ Gemini API configured successfully (attempt {attempt + 1})"
            )
            return True
        except Exception as e:
            logger.warning(
                f"⚠️  Failed to configure Gemini API (attempt {attempt + 1}): {e}"
            )
            if attempt == max_retries - 1:
                raise ValueError(
                    f"Failed to configure Gemini API after {max_retries} attempts: {e}"
                )
            time.sleep(1)  # Wait before retry
    return False


def api_error_handler(func):
    """API 錯誤處理裝飾器"""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        max_retries = 3
        last_exception = None

        for attempt in range(max_retries):
            try:
                # 如果有API key manager，嘗試獲取不同的key
                if CONFIG_AVAILABLE and attempt > 0:
                    try:
                        api_key_manager = get_api_key_manager()
                        old_index = api_key_manager.current_index
                        old_key = api_key_manager.get_current_key()

                        new_api_key = api_key_manager.get_next_key()
                        new_index = api_key_manager.current_index

                        logger.info(f"🔄 API Key 切換: 從索引 {old_index} ({old_key[:20]}...) 切換到索引 {new_index} ({new_api_key[:20]}...)")

                        configure_gemini_with_retry(new_api_key)
                        # 更新函數參數中的api_key
                        if "api_key" in kwargs:
                            kwargs["api_key"] = new_api_key
                        elif len(args) >= 4:  # 假設api_key是第4個參數
                            args = list(args)
                            args[3] = new_api_key
                            args = tuple(args)

                        logger.info(f"✅ API Key 切換成功，重試中...")
                    except Exception as e:
                        logger.warning(f"❌ API Key 切換失敗: {e}")

                return await func(*args, **kwargs)

            except Exception as e:
                last_exception = e
                error_msg = str(e).lower()

                # 檢查是否是可重試的錯誤
                retryable_errors = [
                    "quota",
                    "rate limit",
                    "timeout",
                    "connection",
                    "service unavailable",
                    "429",  # HTTP 429 狀態碼
                    "exceeded",  # "exceeded your current quota" 錯誤
                ]
                if any(err in error_msg for err in retryable_errors):
                    matched_error = [err for err in retryable_errors if err in error_msg][0]
                    logger.warning(
                        f"⚠️  可重試的 API 錯誤 (匹配: {matched_error}) - 嘗試 {attempt + 1}/{max_retries}: {e}"
                    )

                    # 標記當前API key有問題
                    if CONFIG_AVAILABLE:
                        try:
                            api_key_manager = get_api_key_manager()
                            current_api_key = api_key_manager.get_current_key()
                            current_index = api_key_manager.current_index

                            logger.info(f"🔴 標記 API Key 索引 {current_index} ({current_api_key[:20]}...) 有問題")
                            api_key_manager.mark_key_error(current_api_key)

                            usage_stats = api_key_manager.get_usage_stats()
                            logger.info(f"📊 API Key 使用統計: {usage_stats}")
                        except Exception as key_error:
                            logger.warning(f"⚠️  無法標記 API Key 錯誤: {key_error}")

                        # 如果是配額/速率相關的錯誤，同時嘗試切換模型 tier
                        quota_signals = [
                            "quota",
                            "rate limit",
                            "429",
                            "exceeded",
                            "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                        ]
                        if any(sig in error_msg for sig in quota_signals):
                            try:
                                model_tier_manager = get_model_tier_manager()
                                old_model = model_tier_manager.get_current_model()
                                new_model = model_tier_manager.move_to_next_tier(
                                    reason="quota_or_rate_limit"
                                )
                                if new_model and new_model != old_model:
                                    logger.info(
                                        f"🔄 模型 Tier 切換: {old_model} -> {new_model}"
                                    )
                                    # 更新函數參數中的 model_name（如有）
                                    if "model_name" in kwargs:
                                        kwargs["model_name"] = new_model
                                    elif len(args) >= 5:
                                        args = list(args)
                                        args[4] = new_model
                                        args = tuple(args)
                            except Exception as model_err:
                                logger.warning(f"⚠️  模型 tier 切換失敗: {model_err}")

                    if attempt < max_retries - 1:
                        wait_time = (2**attempt) + 1  # 指數退避
                        logger.info(f"⏳ 等待 {wait_time}s 後進行重試...")
                        await asyncio.sleep(wait_time)
                        continue
                else:
                    # 特判：API key 無效時，標記降級並嘗試切換一次 key
                    invalid_key_signals = [
                        "api key not valid",
                        "api_key_invalid",
                        "invalid api key",
                    ]
                    if any(sig in error_msg for sig in invalid_key_signals) and CONFIG_AVAILABLE:
                        try:
                            api_key_manager = get_api_key_manager()
                            bad_key = api_key_manager.get_current_key()
                            bad_index = api_key_manager.current_index
                            logger.warning(
                                f"🔑 Detected INVALID API key at index {bad_index} ({bad_key[:20]}...). Deprioritizing and rotating."
                            )
                            # 強降級，使其後續極少被選中
                            api_key_manager.mark_key_invalid(bad_key)

                            # 嘗試切換到下一把 key 後重試當前 attempt（不增加 attempt 次數）
                            new_api_key = api_key_manager.get_next_key()
                            configure_gemini_with_retry(new_api_key)
                            if "api_key" in kwargs:
                                kwargs["api_key"] = new_api_key
                            elif len(args) >= 4:
                                args = list(args)
                                args[3] = new_api_key
                                args = tuple(args)
                            logger.info("✅ Switched to next API key after invalid key; retrying current attempt...")
                            continue
                        except Exception as key_exc:
                            logger.error(f"Failed to rotate after invalid key: {key_exc}")

                    # 其他不可重試錯誤直接拋出
                    logger.error(f"Non-retryable API error: {e}")
                    raise e

        # 所有重試都失敗了
        logger.error(f"All API retry attempts failed. Last error: {last_exception}")
        raise last_exception

    return wrapper


def preprocess_image(image_path):
    """
    Enhance image to improve text detection.
    """
    if cv2 is None:
        # Fallback: return original path when OpenCV is unavailable
        return image_path

    # Open the image
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Could not open image: {image_path}")

    # Convert to grayscale
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Apply adaptive thresholding to enhance text
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 2
    )

    # Dilate to connect nearby text components
    kernel = np.ones((2, 2), np.uint8)
    dilated = cv2.dilate(thresh, kernel, iterations=1)

    # Find contours to identify potential text regions
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Create a mask for text regions
    mask = np.zeros_like(gray)
    for contour in contours:
        # Filter contours by size and shape to target text content
        area = cv2.contourArea(contour)
        if 100 < area < 10000:  # Adjust these thresholds based on your images
            cv2.drawContours(mask, [contour], -1, 255, -1)

    # Enhance the original image in text regions
    enhanced = image.copy()
    enhanced_gray = cv2.cvtColor(enhanced, cv2.COLOR_BGR2GRAY)
    enhanced_gray[mask > 0] = cv2.equalizeHist(enhanced_gray)[mask > 0]

    # Convert back to RGB
    enhanced = cv2.cvtColor(enhanced_gray, cv2.COLOR_GRAY2BGR)

    # Save processed image
    processed_path = image_path.replace(".jpg", "_processed.jpg")
    cv2.imwrite(processed_path, enhanced)

    return processed_path


def configure_prompt(doc_type, provider_name):
    """
    Configure a prompt based on the document type and provider.

    Args:
        doc_type: The type of document (e.g., invoice, receipt)
        provider_name: The provider/company name

    Returns:
        The prompt text to use for OCR
    """
    try:
        # Look for provider-specific prompt
        prompt_file = os.path.join(
            os.getcwd(),
            "document_type",
            doc_type,
            provider_name,
            "prompt",
            f"{provider_name}.txt",
        )
        if os.path.exists(prompt_file):
            with open(prompt_file, "r", encoding="utf-8") as file:
                prompt = file.read()
            return prompt

        # Fallback to generic document type prompt if available
        generic_prompt = os.path.join(
            os.getcwd(), "document_type", doc_type, "prompt", f"{doc_type}.txt"
        )
        if os.path.exists(generic_prompt):
            with open(generic_prompt, "r", encoding="utf-8") as file:
                prompt = file.read()
            return prompt

        print(f"No prompt found for {doc_type}/{provider_name}")
        return ""
    except Exception as e:
        print(f"Error reading prompt file: {e}")
        return ""


def load_config():
    """
    Load configuration from config.json
    """
    try:
        with open(
            os.path.join(os.getcwd(), "env", "config.json"),
            "r",
            encoding="utf-8",
        ) as file:
            config = json.load(file)
        return config
    except Exception as e:
        print(f"Error loading config: {e}")
        return None


def clean_schema_for_gemini(schema):
    """
    Clean JSON schema for Gemini API compatibility by removing unsupported fields.
    
    Args:
        schema: The JSON schema dictionary
        
    Returns:
        Cleaned schema dictionary safe for Gemini API
    """
    if not isinstance(schema, dict):
        return schema
    
    # Fields that cause Gemini API errors
    problematic_fields = [
        "$schema",
        "$id",
        "$ref",
        "definitions",
        "patternProperties",
        "additionalProperties",
    ]
    
    cleaned_schema = {}
    for key, value in schema.items():
        if key in problematic_fields:
            print(f"Removing problematic schema field: {key}")
            continue
            
        if isinstance(value, dict):
            cleaned_schema[key] = clean_schema_for_gemini(value)
        elif isinstance(value, list):
            cleaned_schema[key] = [
                clean_schema_for_gemini(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned_schema[key] = value
    
    return cleaned_schema


def get_response_schema(doc_type, provider_name):
    """
    Read and parse a JSON schema file, cleaning it for Gemini API compatibility.

    Args:
        doc_type: The type of document (e.g., invoice, receipt)
        provider_name: The provider/company name

    Returns:
        A dictionary containing the parsed and cleaned JSON schema
    """
    try:
        # Look for provider-specific schema
        schema_file = os.path.join(
            os.getcwd(),
            "document_type",
            doc_type,
            provider_name,
            "schema",
            f"{provider_name}.json",
        )
        if os.path.exists(schema_file):
            with open(schema_file, "r", encoding="utf-8") as file:
                schema = json.load(file)
            return clean_schema_for_gemini(schema)

        # Fallback to generic document type schema
        generic_schema = os.path.join(
            os.getcwd(), "document_type", doc_type, "schema", f"{doc_type}.json"
        )
        if os.path.exists(generic_schema):
            with open(generic_schema, "r", encoding="utf-8") as file:
                schema = json.load(file)
            return clean_schema_for_gemini(schema)

        print(f"Schema file not found for {doc_type}/{provider_name}")
        return None
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in schema file: {e}")
        return None
    except Exception as e:
        print(f"Error reading schema file: {e}")
        return None


@api_error_handler
async def extract_text_from_image(
    image_path, enhanced_prompt, response_schema=None, api_key=None, model_name=None
):
    """
    Extract text from image using the enhanced pipeline (async version with retry).
    """
    # 如果沒有提供 API key 和模型名稱，從配置獲取
    if not api_key or not model_name:
        api_key, model_name = get_api_key_and_model()

    processed_image = PIL.Image.open(image_path)

    # 配置 Gemini API（帶重試）
    configure_gemini_with_retry(api_key)

    # Configure the model
    generation_config_kwargs = {
        "temperature": 0.3,
        "top_p": 0.95,
        "top_k": 40,
        "response_mime_type": "application/json",
    }
    # Only include response_schema when provided to avoid API issues with None
    if response_schema is not None:
        generation_config_kwargs["response_schema"] = response_schema

    model = genai.GenerativeModel(
        model_name=model_name,
        generation_config=genai.GenerationConfig(**generation_config_kwargs),
    )
    # Start timing
    start_time = time.time()
    status_updates = {}
    status_updates["status"] = "processing"
    status_updates["started_at"] = start_time

    timeout_seconds = get_gemini_timeout_seconds()

    # Generate trace ID for correlation
    trace_id = f"{uuid.uuid4().hex[:8]}"

    # Get file metadata
    file_size = os.path.getsize(image_path) if os.path.exists(image_path) else 0
    image_width = processed_image.width if hasattr(processed_image, 'width') else 0
    image_height = processed_image.height if hasattr(processed_image, 'height') else 0

    # Log schema complexity
    schema_field_count = len(response_schema.get("properties", {})) if response_schema else 0

    async def _log_gemini_progress():
        """Log progress every 30 seconds during API call."""
        elapsed = 0
        try:
            while True:
                await asyncio.sleep(30)
                elapsed += 30
                logger.info(
                    "⏳ [%s] Gemini API call in progress - elapsed=%ss/%ss (%.0f%%)",
                    trace_id,
                    elapsed,
                    timeout_seconds,
                    (elapsed/timeout_seconds)*100,
                )
        except asyncio.CancelledError:
            return

    try:
        # Update status
        status_updates["step"] = "calling_gemini_api"
        logger.info(
            "🚀 [%s] Starting Gemini image API call - model=%s, timeout=%ss, file=%s, size=%d bytes, width=%d, height=%d, prompt_chars=%d, schema_fields=%d",
            trace_id,
            model_name,
            timeout_seconds,
            image_path,
            file_size,
            image_width,
            image_height,
            len(enhanced_prompt) if enhanced_prompt else 0,
            schema_field_count,
        )

        progress_task = asyncio.create_task(_log_gemini_progress())

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    model.generate_content,
                    contents=[enhanced_prompt, processed_image],
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            processing_time = time.time() - start_time
            status_updates["processing_time_seconds"] = processing_time
            status_updates["status"] = "timeout"
            status_updates["error_message"] = f"API call timed out after {timeout_seconds}s"

            logger.error(
                "⏰ [%s] Gemini image API call TIMED OUT - elapsed=%.2fs, timeout=%ss, model=%s, file=%s, prompt_chars=%d, schema_fields=%d",
                trace_id,
                processing_time,
                timeout_seconds,
                model_name,
                image_path,
                len(enhanced_prompt) if enhanced_prompt else 0,
                schema_field_count,
            )

            return {
                "text": f"Error: API call timed out after {timeout_seconds}s",
                "input_tokens": 0,
                "output_tokens": 0,
                "processing_time": processing_time,
                "status_updates": status_updates,
            }
        finally:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task

        # Success path
        processing_time = time.time() - start_time
        status_updates["processing_time_seconds"] = processing_time
        status_updates["status"] = "success"

        logger.info("✅ [%s] Gemini image API call completed in %.2fs", trace_id, processing_time)
        if hasattr(response, "usage_metadata"):
            logger.info(
                "📊 [%s] Token usage - input=%s, output=%s",
                trace_id,
                response.usage_metadata.prompt_token_count,
                response.usage_metadata.candidates_token_count,
            )

        return {
            "text": response.text,
            "input_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
            "processing_time": processing_time,
            "status_updates": status_updates,
        }
    except asyncio.TimeoutError:
        # Already handled above
        pass
    except Exception as e:
        logger.error("Error generating content from image: %s", e)
        # Try a fallback approach without the schema if there's an error
        try:
            fallback_response = await asyncio.to_thread(
                model.generate_content,
                contents=[enhanced_prompt, processed_image],
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                ),
            )
            return {
                "text": fallback_response.text,
                "input_tokens": 0,  # Default values for error case
                "output_tokens": 0,
            }
        except Exception as f_e:
            logger.error("Fallback also failed: %s", f_e)
            return {"text": f"Error: {e}", "input_tokens": 0, "output_tokens": 0}


def _validate_pdf_file(pdf_data: bytes, trace_id: str) -> dict:
    """
    Validate PDF file and extract metadata for diagnostic logging.
    Returns dict with: is_valid, page_count, is_encrypted, error_type, error_message, magic_bytes, file_size, content_hash
    """
    result = {
        "is_valid": False,
        "page_count": 0,
        "is_encrypted": False,
        "error_type": None,
        "error_message": None,
        "magic_bytes": pdf_data[:8].hex() if pdf_data else "empty",
        "file_size": len(pdf_data) if pdf_data else 0,
        "content_hash": hashlib.sha256(pdf_data).hexdigest()[:16] if pdf_data else "empty",
    }

    # Check magic bytes
    if not pdf_data or len(pdf_data) < 8:
        result["error_type"] = "EMPTY_OR_TOO_SMALL"
        result["error_message"] = f"PDF data is empty or too small ({len(pdf_data) if pdf_data else 0} bytes)"
        return result

    if not pdf_data.startswith(b'%PDF-'):
        result["error_type"] = "INVALID_MAGIC_BYTES"
        result["error_message"] = f"File does not start with %PDF- magic bytes (got: {pdf_data[:8]})"
        return result

    try:
        import PyPDF2
        import io
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_data))
        result["page_count"] = len(pdf_reader.pages)
        result["is_encrypted"] = pdf_reader.is_encrypted

        if result["is_encrypted"]:
            # Try empty password decrypt
            try:
                pdf_reader.decrypt("")
                result["error_type"] = "ENCRYPTED_EMPTY_PASSWORD_OK"
            except Exception:
                result["error_type"] = "ENCRYPTED_CANNOT_DECRYPT"
                result["error_message"] = "PDF is encrypted and cannot be decrypted with empty password"
                return result

        if result["page_count"] == 0:
            result["error_type"] = "ZERO_PAGES"
            result["error_message"] = "PDF has 0 pages"
            return result

        # Try to get first page dimensions as additional validation
        try:
            first_page = pdf_reader.pages[0]
            if hasattr(first_page, 'mediabox'):
                result["first_page_size"] = f"{first_page.mediabox.width}x{first_page.mediabox.height}"
        except Exception:
            pass

        result["is_valid"] = True
        return result

    except Exception as e:
        result["error_type"] = type(e).__name__
        result["error_message"] = str(e)
        return result


@api_error_handler
async def extract_text_from_pdf(
    pdf_path, enhanced_prompt, response_schema=None, api_key=None, model_name=None, context=None
):
    """
    Extract text directly from PDF using Gemini API (async version with retry).
    With timing and status tracking.
    """
    # 如果沒有提供 API key 和模型名稱，從配置獲取
    if not api_key or not model_name:
        api_key, model_name = get_api_key_and_model()

    # 配置 Gemini API（帶重試）
    configure_gemini_with_retry(api_key)

    # Load PDF as bytes
    with open(pdf_path, "rb") as f:
        pdf_data = f.read()

    # Configure the model
    generation_config_kwargs = {
        "temperature": 0.3,
        "top_p": 0.95,
        "top_k": 40,
        "response_mime_type": "application/json",
    }
    # Only include response_schema when provided
    if response_schema is not None:
        generation_config_kwargs["response_schema"] = response_schema

    model = genai.GenerativeModel(
        model_name=model_name,
        generation_config=genai.GenerationConfig(**generation_config_kwargs),
    )

    # Start timing
    start_time = time.time()
    status_updates = {}
    status_updates["status"] = "processing"
    status_updates["started_at"] = start_time

    timeout_seconds = get_gemini_timeout_seconds()

    # Generate trace ID for correlation
    trace_id = f"{uuid.uuid4().hex[:8]}"

    # Extract context for correlation
    ctx = context or {}
    order_id = ctx.get("order_id", "unknown")
    item_id = ctx.get("item_id", "unknown")
    file_id = ctx.get("file_id", "unknown")
    filename = ctx.get("filename", "unknown")

    # Get PDF metadata with diagnostic validation
    pdf_size = len(pdf_data)
    pdf_validation = _validate_pdf_file(pdf_data, trace_id)
    pdf_page_count = pdf_validation["page_count"]

    if pdf_validation["is_valid"]:
        logger.info(
            "📄 [%s] PDF metadata - valid=True, pages=%d, encrypted=%s, size=%d bytes, hash=%s, first_page=%s",
            trace_id,
            pdf_validation["page_count"],
            pdf_validation["is_encrypted"],
            pdf_validation["file_size"],
            pdf_validation["content_hash"],
            pdf_validation.get("first_page_size", "unknown"),
        )
    else:
        logger.error(
            "❌ [%s] PDF VALIDATION FAILED - error_type=%s, error_message=%s, magic_bytes=%s, size=%d bytes, hash=%s",
            trace_id,
            pdf_validation["error_type"],
            pdf_validation["error_message"],
            pdf_validation["magic_bytes"],
            pdf_validation["file_size"],
            pdf_validation["content_hash"],
        )

    # Log schema complexity
    schema_field_count = len(response_schema.get("properties", {})) if response_schema else 0

    # Pre-flight validation - fail fast for invalid PDFs
    if pdf_page_count == 0:
        error_time = time.time() - start_time
        status_updates["processing_time_seconds"] = error_time
        # Use generic 'error' status so it passes DB constraints;
        # keep a more specific error_code for diagnostics.
        status_updates["status"] = "error"
        status_updates["error_code"] = "invalid_pdf"
        status_updates["error_message"] = "PDF has 0 pages - cannot process"
        status_updates["trace_id"] = trace_id

        logger.error(
            "🚫 [%s] ABORTING Gemini call - PDF has 0 pages, file=%s, size=%d bytes, error_type=%s",
            trace_id,
            pdf_path,
            pdf_size,
            pdf_validation.get("error_type", "unknown"),
        )

        return {
            "text": "Error: PDF has 0 pages - file may be corrupt or invalid",
            "input_tokens": 0,
            "output_tokens": 0,
            "processing_time": error_time,
            "status_updates": status_updates,
        }

    async def _log_gemini_progress():
        """Log progress every 30 seconds during API call."""
        elapsed = 0
        try:
            while True:
                await asyncio.sleep(30)
                elapsed += 30
                logger.info(
                    "⏳ [%s] Gemini API call in progress - elapsed=%ss/%ss (%.0f%%)",
                    trace_id,
                    elapsed,
                    timeout_seconds,
                    (elapsed/timeout_seconds)*100,
                )
        except asyncio.CancelledError:
            return

    try:
        # Update status
        status_updates["step"] = "calling_gemini_api"
        logger.info(
            "🚀 [%s] Starting Gemini PDF API call - order=%s, item=%s, file=%s, model=%s, timeout=%ss, size=%d bytes, pages=%d, prompt_chars=%d, schema_fields=%d",
            trace_id,
            order_id,
            item_id,
            file_id,
            model_name,
            timeout_seconds,
            pdf_size,
            pdf_page_count,
            len(enhanced_prompt) if enhanced_prompt else 0,
            schema_field_count,
        )

        progress_task = asyncio.create_task(_log_gemini_progress())

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    model.generate_content,
                    contents=[
                        enhanced_prompt,
                        {"mime_type": "application/pdf", "data": pdf_data},
                    ],
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            processing_time = time.time() - start_time
            status_updates["processing_time_seconds"] = processing_time
            status_updates["status"] = "timeout"
            status_updates["error_message"] = f"API call timed out after {timeout_seconds}s"

            logger.error(
                "⏰ [%s] Gemini PDF API call TIMED OUT - order=%s, item=%s, file=%s, elapsed=%.2fs, timeout=%ss, model=%s, pages=%d, prompt_chars=%d, schema_fields=%d",
                trace_id,
                order_id,
                item_id,
                file_id,
                processing_time,
                timeout_seconds,
                model_name,
                pdf_page_count,
                len(enhanced_prompt) if enhanced_prompt else 0,
                schema_field_count,
            )

            return {
                "text": f"Error: API call timed out after {timeout_seconds}s",
                "input_tokens": 0,
                "output_tokens": 0,
                "processing_time": processing_time,
                "status_updates": status_updates,
            }
        finally:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task

        # Success path
        processing_time = time.time() - start_time
        status_updates["processing_time_seconds"] = processing_time
        status_updates["status"] = "success"

        logger.info("✅ [%s] Gemini PDF API call completed in %.2fs", trace_id, processing_time)
        if hasattr(response, "usage_metadata"):
            logger.info(
                "📊 [%s] Token usage - input=%s, output=%s",
                trace_id,
                response.usage_metadata.prompt_token_count,
                response.usage_metadata.candidates_token_count,
            )

        return {
            "text": response.text,
            "input_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
            "processing_time": processing_time,
            "status_updates": status_updates,
        }
    except asyncio.TimeoutError:
        # Already handled above
        pass
    except Exception as e:
        # Calculate time until error
        error_time = time.time() - start_time
        status_updates["processing_time_seconds"] = error_time
        status_updates["status"] = "error"
        status_updates["error_message"] = str(e)

        logger.error("Error generating content from PDF after %.2fs: %s", error_time, e)

        # Try a fallback approach without the schema if there's an error
        try:
            fallback_start = time.time()
            status_updates["step"] = "fallback_attempt"

            fallback_response = await asyncio.to_thread(
                model.generate_content,
                contents=[
                    enhanced_prompt,
                    {"mime_type": "application/pdf", "data": pdf_data},
                ],
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                ),
            )

            fallback_time = time.time() - fallback_start
            total_time = time.time() - start_time
            status_updates["fallback_time_seconds"] = fallback_time
            status_updates["total_processing_time_seconds"] = total_time
            status_updates["status"] = "success_with_fallback"

            logger.info(
                "Fallback succeeded in %.2fs seconds (total: %.2fs)",
                fallback_time,
                total_time,
            )

            return {
                "text": fallback_response.text,
                "input_tokens": (
                    fallback_response.usage_metadata.prompt_token_count
                    if hasattr(fallback_response, "usage_metadata")
                    else 0
                ),
                "output_tokens": (
                    fallback_response.usage_metadata.candidates_token_count
                    if hasattr(fallback_response, "usage_metadata")
                    else 0
                ),
                "processing_time": total_time,
                "status_updates": status_updates,
            }
        except Exception as f_e:
            # Fallback also failed – treat this as a generic API error so that
            # it complies with the api_usage.status CHECK constraint
            # (allowed values: success, error, success_with_fallback, timeout, rate_limited).
            fallback_error_time = time.time() - fallback_start
            total_time = time.time() - start_time
            status_updates["fallback_time_seconds"] = fallback_error_time
            status_updates["total_processing_time_seconds"] = total_time
            status_updates["status"] = "error"
            status_updates["error_code"] = "fallback_failed"
            status_updates["fallback_error"] = str(f_e)

            logger.error("PDF processing fallback also failed after %.2fs: %s", total_time, f_e)

            return {
                "text": f"Error: {e}",
                "input_tokens": 0,
                "output_tokens": 0,
                "processing_time": total_time,
                "status_updates": status_updates,
            }


def main():
    try:
        with open(
            os.path.join(os.getcwd(), "env", "config.json"), "r", encoding="utf-8"
        ) as file:
            config = json.load(file)
            API_KEY = config["api_key"]

        if not API_KEY:
            print("Please set your GEMINI_API_KEY in config.json")
            return

        try:
            # Load configuration
            config = load_config()
            if not config:
                print("Failed to load configuration")
                return

            # Get available document types
            doc_types = [
                d
                for d in os.listdir(os.path.join(os.getcwd(), "document_type"))
                if os.path.isdir(os.path.join(os.getcwd(), "document_type", d))
            ]

            print("Available document types:")
            for i, doc_type in enumerate(doc_types, 1):
                print(f"{i}. {doc_type}")

            # Select document type
            while True:
                try:
                    choice = int(input("Select document type (enter number): "))
                    if 1 <= choice <= len(doc_types):
                        selected_doc_type = doc_types[choice - 1]
                        break
                    print(f"Please enter a number between 1 and {len(doc_types)}.")
                except ValueError:
                    print("Please enter a valid number.")

            # Step 1: Ask how many documents to process
            while True:
                try:
                    num_docs = int(
                        input(
                            f"How many {selected_doc_type} documents do you want to process? "
                        )
                    )
                    if num_docs > 0:
                        break
                    print("Please enter a positive number.")
                except ValueError:
                    print("Please enter a valid number.")

            # Step 2: Show provider list for the selected document type
            providers = [
                d
                for d in os.listdir(
                    os.path.join(os.getcwd(), "document_type", selected_doc_type)
                )
                if os.path.isdir(
                    os.path.join(os.getcwd(), "document_type", selected_doc_type, d)
                )
            ]

            print(f"Available {selected_doc_type} providers:")
            for i, provider in enumerate(providers, 1):
                print(f"{i}. {provider}")

            selected_providers = []
            for i in range(num_docs):
                while True:
                    try:
                        choice = int(
                            input(
                                f"Select provider for {selected_doc_type} #{i + 1} (enter number): "
                            )
                        )
                        if 1 <= choice <= len(providers):
                            selected_providers.append(providers[choice - 1])
                            break
                        print(f"Please enter a number between 1 and {len(providers)}.")
                    except ValueError:
                        print("Please enter a valid number.")

            # Step 3: Process each document
            for i, provider in enumerate(selected_providers):
                # Ask for file name with file extension
                file_name = input(
                    f"Enter the file name for {provider} {selected_doc_type} with file type (e.g. document_01.jpg): "
                )

                # Construct file path
                file_path = os.path.join(
                    os.getcwd(),
                    "document_type",
                    selected_doc_type,
                    provider,
                    "upload",
                    file_name,
                )

                # Check if file exists
                if not os.path.exists(file_path):
                    print(f"File {file_path} does not exist. Skipping.")
                    continue

                # Get prompt and schema
                prompt = configure_prompt(selected_doc_type, provider)
                schema = get_response_schema(selected_doc_type, provider)

                if not prompt:
                    print(f"No prompt found for {provider}. Skipping.")
                    continue

                if not schema:
                    print(f"No schema found for {provider}. Skipping.")
                    continue

                # Process the document
                print(f"Processing {provider} {selected_doc_type}: {file_name}...")
                extracted_text = asyncio.run(
                    extract_text_from_image(file_path, prompt, schema, API_KEY)
                )

                # Create output directory if it doesn't exist
                output_dir = os.path.join(
                    os.getcwd(), "document_type", selected_doc_type, provider, "output"
                )
                os.makedirs(output_dir, exist_ok=True)

                # Generate output filename
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_filename = os.path.join(
                    output_dir, f"{provider}_{timestamp}.json"
                )

                # Save extracted text to JSON file
                try:
                    # Parse the extracted text as JSON
                    json_data = json.loads(extracted_text)
                    with open(output_filename, "w", encoding="utf-8") as json_file:
                        json.dump(json_data, json_file, indent=2, ensure_ascii=False)
                    print(f"Results saved to {output_filename}")
                except json.JSONDecodeError:
                    # If the extracted text is not valid JSON, save it as a plain text value
                    with open(output_filename, "w", encoding="utf-8") as json_file:
                        json.dump(
                            {"raw_text": extracted_text},
                            json_file,
                            indent=2,
                            ensure_ascii=False,
                        )
                    print(f"Results saved to {output_filename} as raw text")

        except Exception as e:
            print(f"Error: {e}")

    except Exception as e:
        print(f"Error loading config: {e}")


if __name__ == "__main__":
    main()
