import logging
from datetime import datetime

from config import Config
from db import db
from grading.llm_client import LLMResponseError, generate_rubric_draft
from grading.pricing import get_model_rates
from models import Assignment, RubricStatus, RubricVersion

logger = logging.getLogger(__name__)


def process_rubric_generation(rubric_id):
    rubric = RubricVersion.query.get(rubric_id)
    if not rubric:
        logger.error("Grading guide %s not found", rubric_id)
        return

    if rubric.status == RubricStatus.CANCELLED:
        return

    assignment = Assignment.query.get(rubric.assignment_id)
    if not assignment:
        rubric.status = RubricStatus.ERROR
        rubric.error_message = "Assignment missing for grading guide generation."
        db.session.commit()
        return

    model = rubric.llm_model or Config.LLM_MODEL
    try:
        data, usage, raw_text, meta = generate_rubric_draft(
            assignment.assignment_text,
            model,
            Config.LLM_API_BASE_URL,
            Config.LLM_API_KEY,
            json_mode=Config.LLM_USE_JSON_MODE,
            max_tokens=Config.LLM_MAX_OUTPUT_TOKENS,
            timeout=Config.LLM_REQUEST_TIMEOUT,
        )
        db.session.refresh(rubric)
        if rubric.status == RubricStatus.CANCELLED:
            return
        rubric.rubric_text = data.get("rubric_text", "").strip()
        rubric.reference_solution_text = data.get("reference_solution_text", "").strip()
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
        rubric.finished_at = datetime.utcnow()
        db.session.commit()
        logger.info(
            "Grading guide %s generated with model %s (prompt_tokens=%s completion_tokens=%s)",
            rubric_id,
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
        rubric.finished_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        logger.exception("Grading guide generation error for %s", rubric_id)
        rubric.status = RubricStatus.ERROR
        rubric.error_message = str(exc)
        rubric.raw_response = ""
        rubric.finished_at = datetime.utcnow()
        db.session.commit()
