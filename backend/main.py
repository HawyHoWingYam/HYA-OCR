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

# 導入配置管理器
try:
    from config_loader import config_loader, get_api_key_manager, get_model_tier_manager

    CONFIG_AVAILABLE = True
except ImportError:
    CONFIG_AVAILABLE = False
    logging.warning("Config loader not available, using fallback methods")

logger = logging.getLogger(__name__)


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

    try:
        # Update status
        status_updates["step"] = "calling_gemini_api"
        print(f"Gemini API processing started at {start_time}")
        # Make API request with proper structure for response schema

        # Use asyncio.to_thread to run the blocking API call in a separate thread
        response = await asyncio.to_thread(
            model.generate_content,
            contents=[enhanced_prompt, processed_image],
        )
        # Calculate processing time
        processing_time = time.time() - start_time
        status_updates["processing_time_seconds"] = processing_time
        status_updates["status"] = "success"

        print(f"Gemini API processing completed in {processing_time:.2f} seconds")
        print(response.usage_metadata)
        # print(response.text)
        # Return both the text and token counts
        return {
            "text": response.text,
            "input_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
            "processing_time": processing_time,
            "status_updates": status_updates,
        }
    except Exception as e:
        print(f"Error generating content: {e}")
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
            print(f"Fallback also failed: {f_e}")
            return {"text": f"Error: {e}", "input_tokens": 0, "output_tokens": 0}


@api_error_handler
async def extract_text_from_pdf(
    pdf_path, enhanced_prompt, response_schema=None, api_key=None, model_name=None
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

    try:
        # Update status
        status_updates["step"] = "calling_gemini_api"
        print(f"Gemini API processing started at {start_time}")
        # Make API request with PDF
        response = await asyncio.to_thread(
            model.generate_content,
            contents=[
                enhanced_prompt,
                {"mime_type": "application/pdf", "data": pdf_data},
            ],
        )

        # Calculate processing time
        processing_time = time.time() - start_time
        status_updates["processing_time_seconds"] = processing_time
        status_updates["status"] = "success"

        print(f"Gemini API processing completed in {processing_time:.2f} seconds")
        print(response.usage_metadata)
        # print(response.text)
        # Return both the text, token counts and timing metrics
        return {
            "text": response.text,
            "input_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
            "processing_time": processing_time,
            "status_updates": status_updates,
        }
    except Exception as e:
        # Calculate time until error
        error_time = time.time() - start_time
        status_updates["processing_time_seconds"] = error_time
        status_updates["status"] = "error"
        status_updates["error_message"] = str(e)

        print(f"Error generating content from PDF after {error_time:.2f} seconds: {e}")

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

            print(
                f"Fallback succeeded in {fallback_time:.2f} seconds (total: {total_time:.2f}s)"
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
            fallback_error_time = time.time() - fallback_start
            total_time = time.time() - start_time
            status_updates["fallback_time_seconds"] = fallback_error_time
            status_updates["total_processing_time_seconds"] = total_time
            status_updates["status"] = "failed"
            status_updates["fallback_error"] = str(f_e)

            print(f"PDF processing fallback also failed after {total_time:.2f}s: {f_e}")

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
