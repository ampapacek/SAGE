import json
import logging
from datetime import datetime, timezone

from config import Config
from db import db
from grading.llm_client import LLMResponseError, generate_rubric_draft
from grading.pricing import get_model_rates
from models import Assignment, RubricStatus, RubricVersion

logger = logging.getLogger(__name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _provider_config(provider_key):
    if provider_key in {"other", "custom1"}:
        return {
            "name": Config.CUSTOM_LLM_PROVIDER_1_NAME or "Other 1",
            "api_key": Config.CUSTOM_LLM_PROVIDER_1_API_KEY,
            "base_url": Config.CUSTOM_LLM_PROVIDER_1_API_BASE_URL,
            "default_model": Config.CUSTOM_LLM_PROVIDER_1_DEFAULT_MODEL or Config.LLM_MODEL,
        }
    if provider_key == "custom2":
        return {
            "name": Config.CUSTOM_LLM_PROVIDER_2_NAME or "Other 2",
            "api_key": Config.CUSTOM_LLM_PROVIDER_2_API_KEY,
            "base_url": Config.CUSTOM_LLM_PROVIDER_2_API_BASE_URL,
            "default_model": Config.CUSTOM_LLM_PROVIDER_2_DEFAULT_MODEL or Config.LLM_MODEL,
        }
    if provider_key == "custom3":
        return {
            "name": Config.CUSTOM_LLM_PROVIDER_3_NAME or "Other 3",
            "api_key": Config.CUSTOM_LLM_PROVIDER_3_API_KEY,
            "base_url": Config.CUSTOM_LLM_PROVIDER_3_API_BASE_URL,
            "default_model": Config.CUSTOM_LLM_PROVIDER_3_DEFAULT_MODEL or Config.LLM_MODEL,
        }
    return {
        "name": "OpenAI",
        "api_key": Config.LLM_API_KEY,
        "base_url": Config.LLM_API_BASE_URL,
        "default_model": Config.LLM_MODEL,
    }


def _normalize_text(value, field_name):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(item.strip() for item in value if item is not None).strip()
    if isinstance(value, dict) or isinstance(value, list):
        return json.dumps(value, ensure_ascii=True, indent=2)
    raise ValueError(
        f"Draft response expected {field_name} as string or object, got {type(value).__name__}."
    )


def process_rubric_generation(rubric_id):
    # "rubric" here means grading guide generation.
    rubric = db.session.get(RubricVersion, rubric_id)
    if not rubric:
        logger.error("Grading guide %s not found", rubric_id)
        return

    if rubric.status == RubricStatus.CANCELLED:
        return

    assignment = db.session.get(Assignment, rubric.assignment_id)
    if not assignment:
        rubric.status = RubricStatus.ERROR
        rubric.error_message = "Assignment missing for grading guide generation."
        db.session.commit()
        return

    provider_key = rubric.llm_provider or Config.LLM_PROVIDER
    provider_cfg = _provider_config(provider_key)
    model = rubric.llm_model or provider_cfg["default_model"]
    formatted_output = rubric.formatted_output
    if formatted_output is None:
        formatted_output = Config.LLM_FORMATTED_OUTPUT
    global_instructions = (Config.PROMPT_RUBRIC_ADDITIONAL or "").strip()
    extra_instructions = (rubric.extra_instructions or "").strip()
    additional_instructions = "\n".join(
        [text for text in [global_instructions, extra_instructions] if text]
    )
    raw_text = ""
    try:
        data, usage, raw_text, meta = generate_rubric_draft(
            assignment.assignment_text,
            model,
            provider_cfg["base_url"],
            provider_cfg["api_key"],
            formatted_output=formatted_output,
            additional_instructions=additional_instructions,
            json_mode=Config.LLM_USE_JSON_MODE,
            max_tokens=Config.LLM_MAX_OUTPUT_TOKENS,
            timeout=Config.LLM_REQUEST_TIMEOUT,
        )
        db.session.refresh(rubric)
        if rubric.status == RubricStatus.CANCELLED:
            return
        rubric.rubric_text = _normalize_text(
            data.get("rubric_text", ""), "rubric_text"
        )
        rubric.reference_solution_text = _normalize_text(
            data.get("reference_solution_text", ""), "reference_solution_text"
        )
        if not rubric.rubric_text or not rubric.reference_solution_text:
            raise ValueError(
                "Draft response missing grading guide or reference solution text "
                "(rubric_text/reference_solution_text)."
            )
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
        input_rate, output_rate = get_model_rates(
            model, Config.LLM_PRICE_INPUT_PER_1K, Config.LLM_PRICE_OUTPUT_PER_1K
        )
        if input_rate <= 0 and output_rate <= 0:
            price_estimate = None
        else:
            price_estimate = (prompt_tokens / 1000.0) * input_rate + (
                completion_tokens / 1000.0
            ) * output_rate
        rubric.prompt_tokens = prompt_tokens
        rubric.completion_tokens = completion_tokens
        rubric.total_tokens = total_tokens
        rubric.price_estimate = price_estimate
        rubric.status = RubricStatus.DRAFT
        rubric.error_message = ""
        rubric.raw_response = raw_text
        rubric.finished_at = _utcnow()
        db.session.commit()
        logger.info(
            "Grading guide %s generated with %s/%s (prompt_tokens=%s completion_tokens=%s)",
            rubric_id,
            provider_cfg["name"],
            model,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
        if meta and meta.get("api_fallback"):
            logger.info("Grading guide %s API fallback used", rubric_id)
    except LLMResponseError as exc:
        logger.exception("Grading guide generation failed for %s", rubric_id)
        rubric.status = RubricStatus.ERROR
        rubric.error_message = str(exc)
        rubric.raw_response = exc.raw_text or ""
        rubric.finished_at = _utcnow()
        db.session.commit()
    except Exception as exc:
        logger.exception("Grading guide generation error for %s", rubric_id)
        rubric.status = RubricStatus.ERROR
        rubric.error_message = str(exc)
        rubric.raw_response = raw_text or ""
        rubric.finished_at = _utcnow()
        db.session.commit()
