import json
import logging
from datetime import datetime, timezone

from config import Config
from db import db
from models import (
    Assignment,
    GradeResult,
    GradingJob,
    JobStatus,
    RubricStatus,
    RubricVersion,
    Submission,
)
from grading.llm_client import LLMResponseError, grade_submission_and_raw
from grading.pricing import get_model_rates
from grading.schemas import render_grade_output, validate_grade_result
from processing.file_ingest import (
    collect_submission_images,
    submission_processed_dir,
    resolve_data_path,
)
from processing.pdf_render import render_pdf_to_images
from processing.pdf_text import extract_pdf_text

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


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_cancelled(job_id):
    job = db.session.get(GradingJob, job_id)
    if not job:
        return True
    db.session.refresh(job)
    return job.status == JobStatus.CANCELLED


def _finalize_cancelled(job_id, summary_lines):
    job = db.session.get(GradingJob, job_id)
    if not job:
        return
    if not job.finished_at:
        job.finished_at = _utcnow()
    if "Cancelled by user." not in job.message:
        summary_lines.append("Cancelled by user.")
        job.message = "\n".join(summary_lines)
    db.session.commit()


def _collect_text_with_stats(submission):
    submitted_text = submission.submitted_text or ""
    submitted_chars = len(submitted_text)
    text_file_count = 0
    text_file_chars = 0
    pdf_text_files = 0
    pdf_text_chars = 0
    parts = []

    if submitted_text:
        parts.append(submitted_text)

    for file_record in submission.files:
        if file_record.file_type != "text":
            continue
        try:
            content = resolve_data_path(file_record.file_path).read_text(errors="ignore")
            text_file_count += 1
            text_file_chars += len(content)
            if content:
                parts.append(content)
        except OSError:
            logger.exception("Failed reading text file %s", file_record.file_path)

    processed_dir = submission_processed_dir(submission.assignment_id, submission.id)
    if processed_dir.exists():
        for text_path in sorted(processed_dir.glob("**/text.txt")):
            try:
                content = text_path.read_text(errors="ignore")
                pdf_text_files += 1
                pdf_text_chars += len(content)
                if content:
                    parts.append(content)
            except OSError:
                logger.exception("Failed reading extracted PDF text %s", text_path)

    combined = "\n\n".join([p for p in parts if p])
    return combined, submitted_chars, text_file_count, text_file_chars, pdf_text_files, pdf_text_chars


def _estimate_price(prompt_tokens, completion_tokens, model):
    input_rate, output_rate = get_model_rates(
        model, Config.LLM_PRICE_INPUT_PER_1K, Config.LLM_PRICE_OUTPUT_PER_1K
    )
    if input_rate <= 0 and output_rate <= 0:
        return None
    return (prompt_tokens / 1000.0) * input_rate + (completion_tokens / 1000.0) * output_rate


def _log_summary(job_id, summary_lines):
    for line in summary_lines:
        logger.info("Job %s summary: %s", job_id, line)


def _get_or_create_grade_result(submission_id, rubric_version_id):
    result = (
        GradeResult.query.filter_by(
            submission_id=submission_id, rubric_version_id=rubric_version_id
        )
        .order_by(GradeResult.created_at.desc())
        .first()
    )
    if result:
        return result
    result = GradeResult(
        submission_id=submission_id,
        rubric_version_id=rubric_version_id,
        total_points=None,
        json_result="{}",
        rendered_text="",
        raw_response="",
        error_message="",
    )
    db.session.add(result)
    return result


def process_submission_job(job_id):
    job = GradingJob.query.get(job_id)
    if not job:
        logger.error("Job %s not found", job_id)
        return

    if job.status == JobStatus.CANCELLED:
        _finalize_cancelled(job_id, ["Cancelled by user."])
        return

    summary_lines = []
    job.status = JobStatus.RUNNING
    provider_key = job.llm_provider or Config.LLM_PROVIDER
    provider_cfg = _provider_config(provider_key)
    if not job.llm_model:
        job.llm_model = provider_cfg["default_model"]
    job.started_at = _utcnow()
    job.message = ""
    db.session.commit()

    raw_response = ""
    try:
        # "rubric" here refers to the grading guide for the assignment.
        rubric = RubricVersion.query.get(job.rubric_version_id)
        if not rubric or rubric.status != RubricStatus.APPROVED:
            raise ValueError("Approved grading guide not found for this job.")

        assignment = Assignment.query.get(job.assignment_id)
        submission = Submission.query.get(job.submission_id)
        if not assignment or not submission:
            raise ValueError("Assignment or submission missing for job.")

        summary_lines.append(f"Provider: {provider_cfg['name']}")
        summary_lines.append(f"Model: {job.llm_model or provider_cfg['default_model']}")
        json_mode_label = "on" if Config.LLM_USE_JSON_MODE else "off"
        summary_lines.append(f"JSON mode: {json_mode_label}")
        logger.info(
            "Job %s started for submission %s (assignment %s)",
            job_id,
            submission.id,
            assignment.id,
        )

        file_type_counts = {}
        other_files = []
        for file_record in submission.files:
            file_type_counts[file_record.file_type] = (
                file_type_counts.get(file_record.file_type, 0) + 1
            )
            if file_record.file_type == "other":
                other_files.append(file_record.original_filename)
        if file_type_counts:
            summary_lines.append(f"Uploaded files: {file_type_counts}")
        if other_files:
            summary_lines.append(f"Unsupported files: {', '.join(other_files)}")

        pdf_files = [f for f in submission.files if f.file_type == "pdf"]
        if pdf_files:
            summary_lines.append("PDF processing:")
        else:
            summary_lines.append("PDF processing: none")

        pdf_errors = []
        min_chars = max(Config.PDF_TEXT_MIN_CHARS, 1)
        min_ratio = max(min(Config.PDF_TEXT_MIN_RATIO, 1.0), 0.0)
        for file_record in pdf_files:
            pdf_path = resolve_data_path(file_record.file_path)
            base_dir = submission_processed_dir(submission.assignment_id, submission.id)
            out_dir = base_dir / f"pdf_{file_record.id}"
            try:
                text, stats = extract_pdf_text(pdf_path, out_dir)
                pages = stats["pages"] or 0
                pages_with_text = stats["pages_with_text"] or 0
                page_ratio = (pages_with_text / pages) if pages else 0.0
                has_images = stats.get("image_count", 0) > 0
                cached = "cached" if stats["cached"] else "extracted"
                summary_lines.append(
                    f"- {file_record.original_filename}: text {cached} "
                    f"({pages_with_text}/{pages} pages, {stats['total_chars']} chars, "
                    f"images={stats.get('image_count', 0)})"
                )
                if has_images:
                    summary_lines.append(
                        f"  -> images detected, rendering pages"
                    )
                elif page_ratio >= min_ratio and stats["total_chars"] >= min_chars:
                    summary_lines.append(
                        f"  -> text sufficient (ratio {page_ratio:.2f}), skipping image render"
                    )
                    continue
                else:
                    summary_lines.append(
                        f"  -> text ratio {page_ratio:.2f} below {min_ratio:.2f}, rendering images"
                    )
            except Exception as exc:
                error_text = str(exc).splitlines()[0] if str(exc) else "unknown error"
                summary_lines.append(
                    f"- {file_record.original_filename}: text extraction failed ({error_text}), rendering images"
                )
            try:
                existing = sorted(out_dir.glob("page_*.png"))
                rendered_paths = render_pdf_to_images(
                    pdf_path, out_dir, dpi=Config.PDF_DPI
                )
                page_count = len(rendered_paths)
                cached = "cached" if existing else "rendered"
                summary_lines.append(
                    f"  -> {page_count} page(s) {cached}"
                )
            except Exception as exc:
                error_text = str(exc).splitlines()[0] if str(exc) else "unknown error"
                summary_lines.append(
                    f"- {file_record.original_filename}: render failed ({error_text})"
                )
                pdf_errors.append(f"{file_record.original_filename} ({error_text})")

        if _is_cancelled(job_id):
            _finalize_cancelled(job_id, summary_lines)
            return

        processed_dir = submission_processed_dir(submission.assignment_id, submission.id)
        rendered_images = list(processed_dir.glob("**/*.png"))
        rendered_images += list(processed_dir.glob("**/*.jpg"))
        rendered_images += list(processed_dir.glob("**/*.jpeg"))
        rendered_image_count = len(rendered_images)
        uploaded_image_count = len(
            [f for f in submission.files if f.file_type == "image"]
        )
        summary_lines.append(
            "Images: uploaded=%s, rendered=%s, total=%s"
            % (uploaded_image_count, rendered_image_count, uploaded_image_count + rendered_image_count)
        )
        summary_lines.append(
            "OCR: not performed; grading uses extracted PDF text + submitted text + images."
        )

        student_text, submitted_chars, text_file_count, text_file_chars, pdf_text_files, pdf_text_chars = (
            _collect_text_with_stats(submission)
        )
        summary_lines.append(
            "Text: submitted_chars=%s, text_files=%s, text_file_chars=%s, pdf_text_files=%s, pdf_text_chars=%s, total_chars=%s"
            % (
                submitted_chars,
                text_file_count,
                text_file_chars,
                pdf_text_files,
                pdf_text_chars,
                len(student_text),
            )
        )

        image_paths = collect_submission_images(submission)
        summary_lines.append(f"Images passed to LLM: {len(image_paths)}")

        if not student_text and not image_paths:
            if pdf_errors:
                raise ValueError(
                    "No pages rendered from PDFs. Possibly corrupt files: "
                    + "; ".join(pdf_errors)
                )
            raise ValueError("Submission has no text or images to grade.")

        if _is_cancelled(job_id):
            _finalize_cancelled(job_id, summary_lines)
            return

        llm_model = job.llm_model or provider_cfg["default_model"]
        llm_data, raw_response, usage, meta = grade_submission_and_raw(
            assignment.assignment_text,
            rubric.rubric_text,
            rubric.reference_solution_text,
            student_text,
            image_paths,
            llm_model,
            provider_cfg["base_url"],
            provider_cfg["api_key"],
            json_mode=Config.LLM_USE_JSON_MODE,
            max_tokens=Config.LLM_MAX_OUTPUT_TOKENS,
            timeout=Config.LLM_REQUEST_TIMEOUT,
        )

        valid, error = validate_grade_result(llm_data)
        if not valid:
            raise ValueError(f"Invalid grade JSON: {error}")

        rendered_text = render_grade_output(llm_data)

        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
        image_tokens_estimate = len(image_paths) * Config.LLM_IMAGE_TOKENS_PER_IMAGE
        prompt_tokens_estimated = prompt_tokens + image_tokens_estimate
        price_estimate = _estimate_price(
            prompt_tokens_estimated, completion_tokens, llm_model
        )
        if price_estimate is None:
            price_text = "not configured"
        else:
            price_text = f"${price_estimate:.4f}"
        summary_lines.append(
            "LLM usage: prompt_tokens=%s, completion_tokens=%s, total_tokens=%s, image_tokens_estimate=%s, price_estimate=%s"
            % (prompt_tokens, completion_tokens, total_tokens, image_tokens_estimate, price_text)
        )
        if meta:
            api_used = meta.get("api_used")
            if api_used:
                summary_lines.append(f"LLM API: {api_used}")
            if meta.get("api_fallback"):
                summary_lines.append("LLM API fallback: responses -> chat")
            if meta.get("json_mode_fallback"):
                summary_lines.append("JSON mode fallback: disabled for retry")

        job.prompt_tokens = prompt_tokens
        job.completion_tokens = completion_tokens
        job.total_tokens = total_tokens
        job.price_estimate = price_estimate

        if _is_cancelled(job_id):
            _finalize_cancelled(job_id, summary_lines)
            return

        grade_result = _get_or_create_grade_result(submission.id, rubric.id)
        grade_result.total_points = llm_data.get("total_points")
        grade_result.json_result = json.dumps(llm_data)
        grade_result.rendered_text = rendered_text
        grade_result.raw_response = raw_response
        grade_result.error_message = ""

        job.status = JobStatus.SUCCESS
        job.finished_at = _utcnow()
        duration_seconds = (_as_utc(job.finished_at) - _as_utc(job.started_at)).total_seconds()
        summary_lines.append(f"Duration: {duration_seconds:.2f} seconds")
        job.message = "\n".join(summary_lines)
        db.session.commit()
        _log_summary(job_id, summary_lines)
        logger.info("Job %s completed in %.2f seconds", job_id, duration_seconds)
    except LLMResponseError as exc:
        logger.exception("LLM response error for job %s", job_id)
        grade_result = _get_or_create_grade_result(job.submission_id, job.rubric_version_id)
        grade_result.json_result = "{}"
        grade_result.rendered_text = ""
        grade_result.raw_response = exc.raw_text or ""
        grade_result.error_message = str(exc)
        job.status = JobStatus.ERROR
        job.finished_at = _utcnow()
        duration_seconds = (_as_utc(job.finished_at) - _as_utc(job.started_at)).total_seconds()
        summary_lines.append(f"Duration: {duration_seconds:.2f} seconds")
        summary_lines.append(f"Error: {exc}")
        job.message = "\n".join(summary_lines)
        db.session.commit()
        _log_summary(job_id, summary_lines)
    except Exception as exc:
        logger.exception("Processing error for job %s", job_id)
        grade_result = _get_or_create_grade_result(job.submission_id, job.rubric_version_id)
        grade_result.json_result = "{}"
        grade_result.rendered_text = ""
        grade_result.raw_response = raw_response or ""
        grade_result.error_message = str(exc)
        job.status = JobStatus.ERROR
        job.finished_at = _utcnow()
        duration_seconds = (_as_utc(job.finished_at) - _as_utc(job.started_at)).total_seconds()
        summary_lines.append(f"Duration: {duration_seconds:.2f} seconds")
        summary_lines.append(f"Error: {exc}")
        job.message = "\n".join(summary_lines)
        db.session.commit()
        _log_summary(job_id, summary_lines)
