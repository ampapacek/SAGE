import json
import logging
from datetime import datetime, timezone

from config import Config
from db import db
from grading.llm_client import LLMResponseError, generate_assignment_draft
from grading.pricing import get_model_rates
from models import Assignment, AssignmentGeneration, JobStatus

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


def process_assignment_generation(generation_id):
    generation = db.session.get(AssignmentGeneration, generation_id)
    if not generation:
        logger.error("Assignment generation %s not found", generation_id)
        return

    if generation.status == JobStatus.CANCELLED:
        return

    provider_key = generation.llm_provider or Config.LLM_PROVIDER
    provider_cfg = _provider_config(provider_key)
    model = generation.llm_model or provider_cfg["default_model"]
    formatted_output = generation.formatted_output
    if formatted_output is None:
        formatted_output = Config.LLM_FORMATTED_OUTPUT
    global_instructions = (Config.PROMPT_ASSIGNMENT_ADDITIONAL or "").strip()
    extra_instructions = (generation.extra_instructions or "").strip()
    additional_instructions = "\n".join(
        [text for text in [global_instructions, extra_instructions] if text]
    )

    generation.status = JobStatus.RUNNING
    generation.started_at = _utcnow()
    generation.error_message = ""
    db.session.commit()

    raw_text = ""
    try:
        data, usage, raw_text, meta = generate_assignment_draft(
            generation.topic_text,
            model,
            provider_cfg["base_url"],
            provider_cfg["api_key"],
            formatted_output=formatted_output,
            additional_instructions=additional_instructions,
            json_mode=Config.LLM_USE_JSON_MODE,
            max_tokens=Config.LLM_MAX_OUTPUT_TOKENS,
            timeout=Config.LLM_REQUEST_TIMEOUT,
        )
        db.session.refresh(generation)
        if generation.status == JobStatus.CANCELLED:
            return
        if not isinstance(data, dict):
            raise ValueError("Assignment generation response must be a JSON object.")

        title = _normalize_text(data.get("title"), "title")
        assignment_text = _normalize_text(data.get("assignment_text"), "assignment_text")
        if not title or not assignment_text:
            raise ValueError("Draft response missing title or assignment_text.")

        assignment = Assignment(
            title=title,
            assignment_text=assignment_text,
            folder_name=generation.folder_name or None,
        )
        db.session.add(assignment)
        db.session.flush()

        usage = usage or {}
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

        generation.assignment_id = assignment.id
        generation.prompt_tokens = prompt_tokens
        generation.completion_tokens = completion_tokens
        generation.total_tokens = total_tokens
        generation.price_estimate = price_estimate
        generation.status = JobStatus.SUCCESS
        generation.error_message = ""
        generation.raw_response = raw_text
        generation.finished_at = _utcnow()
        db.session.commit()
        logger.info(
            "Assignment generation %s completed with %s/%s",
            generation_id,
            provider_cfg["name"],
            model,
        )
        if meta and meta.get("api_fallback"):
            logger.info("Assignment generation %s API fallback used", generation_id)
    except LLMResponseError as exc:
        logger.exception("Assignment generation failed for %s", generation_id)
        generation.status = JobStatus.ERROR
        generation.error_message = str(exc)
        generation.raw_response = exc.raw_text or ""
        generation.finished_at = _utcnow()
        db.session.commit()
    except Exception as exc:
        logger.exception("Assignment generation error for %s", generation_id)
        generation.status = JobStatus.ERROR
        generation.error_message = str(exc)
        generation.raw_response = raw_text or ""
        generation.finished_at = _utcnow()
        db.session.commit()
