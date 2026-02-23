import io
import json
import logging
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from config import Config, DATA_DIR
from db import db
from grading.llm_client import LLMResponseError, generate_rubric_draft
from models import (
    Assignment,
    AssignmentImport,
    GradingTemplate,
    GradingJob,
    JobStatus,
    RubricStatus,
    RubricVersion,
    Submission,
    SubmissionFile,
)
from processing.file_ingest import (
    detect_file_type,
    relpath_from_data,
    resolve_data_path,
    submission_upload_dir,
)

logger = logging.getLogger(__name__)

_ZIP_TEXT_EXTENSIONS = {".txt", ".md"}
_ZIP_BINARY_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
_ZIP_ASSIGNMENT_NAMES = {"assignment", "assigment"}
_ZIP_GUIDE_NAMES = {"guide"}
_ZIP_REFERENCE_NAMES = {"ref_solution"}
_IMAGE_CAPABLE_MODELS = {
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "o4-mini",
}
_NON_IMAGE_MODELS = {"o3-mini"}


def _utcnow():
    return datetime.now(timezone.utc)


def _decode_text_blob(data):
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore").strip()


def _normalize_generated_text(value, field_name):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(item.strip() for item in value if item is not None).strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, indent=2)
    raise ValueError(
        f"Draft response expected {field_name} as string or object, got {type(value).__name__}."
    )


def _guess_assignment_title_from_zip(filename):
    stem = Path(filename or "").stem.strip()
    if not stem:
        return "Imported Assignment"
    cleaned = re.sub(r"[_-]+", " ", stem).strip()
    return cleaned[:255] or "Imported Assignment"


def _extract_assignment_zip_payload(raw_zip):
    if not raw_zip:
        raise ValueError("ZIP file is empty.")

    assignment_text = ""
    guide_text = ""
    reference_solution_text = ""
    student_files = {}

    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                member_path = Path(info.filename)
                if any(part.startswith("__MACOSX") for part in member_path.parts):
                    continue
                filename = member_path.name
                if not filename or filename.startswith("."):
                    continue
                stem = Path(filename).stem.strip()
                ext = Path(filename).suffix.lower()
                if ext not in (_ZIP_TEXT_EXTENSIONS | _ZIP_BINARY_EXTENSIONS):
                    continue

                data = archive.read(info)
                lower_stem = stem.lower()
                if ext in _ZIP_TEXT_EXTENSIONS and lower_stem in _ZIP_ASSIGNMENT_NAMES:
                    assignment_text = _decode_text_blob(data)
                    continue
                if ext in _ZIP_TEXT_EXTENSIONS and lower_stem in _ZIP_GUIDE_NAMES:
                    guide_text = _decode_text_blob(data)
                    continue
                if ext in _ZIP_TEXT_EXTENSIONS and lower_stem in _ZIP_REFERENCE_NAMES:
                    reference_solution_text = _decode_text_blob(data)
                    continue

                if not stem:
                    continue
                student_files.setdefault(stem, []).append(
                    {"filename": filename, "ext": ext, "data": data}
                )
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded file is not a valid ZIP archive.") from exc

    if not assignment_text:
        raise ValueError(
            "ZIP must contain assigment.md/txt or assignment.md/txt with the assignment text."
        )
    if not student_files:
        raise ValueError("No student files found in ZIP.")

    return {
        "assignment_text": assignment_text,
        "guide_text": guide_text,
        "reference_solution_text": reference_solution_text,
        "student_files": student_files,
    }


def _provider_config(provider_key):
    if provider_key in {"other", "custom1"}:
        return {
            "name": Config.CUSTOM_LLM_PROVIDER_1_NAME or "Other 1",
            "api_key": Config.CUSTOM_LLM_PROVIDER_1_API_KEY,
            "base_url": Config.CUSTOM_LLM_PROVIDER_1_API_BASE_URL,
            "default_model": Config.CUSTOM_LLM_PROVIDER_1_DEFAULT_MODEL or Config.LLM_MODEL,
            "models": Config.CUSTOM_LLM_PROVIDER_1_MODELS or "",
        }
    if provider_key == "custom2":
        return {
            "name": Config.CUSTOM_LLM_PROVIDER_2_NAME or "Other 2",
            "api_key": Config.CUSTOM_LLM_PROVIDER_2_API_KEY,
            "base_url": Config.CUSTOM_LLM_PROVIDER_2_API_BASE_URL,
            "default_model": Config.CUSTOM_LLM_PROVIDER_2_DEFAULT_MODEL or Config.LLM_MODEL,
            "models": Config.CUSTOM_LLM_PROVIDER_2_MODELS or "",
        }
    if provider_key == "custom3":
        return {
            "name": Config.CUSTOM_LLM_PROVIDER_3_NAME or "Other 3",
            "api_key": Config.CUSTOM_LLM_PROVIDER_3_API_KEY,
            "base_url": Config.CUSTOM_LLM_PROVIDER_3_API_BASE_URL,
            "default_model": Config.CUSTOM_LLM_PROVIDER_3_DEFAULT_MODEL or Config.LLM_MODEL,
            "models": Config.CUSTOM_LLM_PROVIDER_3_MODELS or "",
        }
    return {
        "name": "OpenAI",
        "api_key": Config.LLM_API_KEY,
        "base_url": Config.LLM_API_BASE_URL,
        "default_model": Config.LLM_MODEL,
        "models": Config.OPENAI_MODEL_OPTIONS or "",
    }


def _model_supports_images(model_name):
    if not model_name:
        return True
    name = model_name.strip().lower()
    for model in _NON_IMAGE_MODELS:
        if name == model or name.startswith(f"{model}-"):
            return False
    for model in _IMAGE_CAPABLE_MODELS:
        if name == model or name.startswith(f"{model}-"):
            return True
    return True


def _parse_model_options(raw):
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _pick_default_import_model(provider_key, requires_images):
    provider_cfg = _provider_config(provider_key)
    model = provider_cfg["default_model"] or Config.LLM_MODEL
    if not requires_images or _model_supports_images(model):
        return provider_cfg, model

    for option in _parse_model_options(provider_cfg.get("models", "")):
        if _model_supports_images(option):
            return provider_cfg, option
    raise ValueError(
        "Default model does not support images and no image-capable model was found."
    )


def _store_submission_binary_file(submission, filename, data):
    ext = Path(filename).suffix.lower()
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest_dir = submission_upload_dir(submission.assignment_id, submission.id)
    dest_path = dest_dir / unique_name
    dest_path.write_bytes(data)

    submission_file = SubmissionFile(
        submission_id=submission.id,
        file_path=relpath_from_data(dest_path),
        file_type=detect_file_type(filename),
        original_filename=filename,
    )
    db.session.add(submission_file)


def _set_message(import_job, message):
    import_job.message = message
    db.session.commit()
    logger.info("Assignment import %s: %s", import_job.id, message)


def process_assignment_import(import_id):
    import_job = db.session.get(AssignmentImport, import_id)
    if not import_job:
        logger.error("Assignment import %s not found", import_id)
        return

    import_job.status = JobStatus.RUNNING
    import_job.started_at = _utcnow()
    import_job.error_message = ""
    db.session.commit()

    try:
        _set_message(import_job, "Reading ZIP package...")
        raw_zip = resolve_data_path(import_job.zip_path).read_bytes()
        payload = _extract_assignment_zip_payload(raw_zip)
        assignment_text = payload["assignment_text"]
        guide_text = payload["guide_text"]
        reference_solution_text = payload["reference_solution_text"]
        student_files = payload["student_files"]
        has_guide_from_zip = bool(guide_text.strip() and reference_solution_text.strip())

        _set_message(
            import_job,
            f"ZIP parsed. Found {len(student_files)} student solution group(s).",
        )

        requires_images = any(
            any(item["ext"] in _ZIP_BINARY_EXTENSIONS for item in files)
            for files in student_files.values()
        )
        provider_key = import_job.llm_provider or Config.LLM_PROVIDER
        provider_cfg, selected_model = _pick_default_import_model(
            provider_key, requires_images
        )
        import_job.llm_provider = provider_key
        import_job.llm_model = selected_model
        db.session.commit()

        generated_raw_response = ""
        if import_job.use_template_guide:
            template = db.session.get(GradingTemplate, import_job.template_id)
            if not template:
                raise ValueError("Selected template was not found.")
            guide_text = (template.rubric_text or "").strip()
            if not guide_text:
                raise ValueError("Selected template has empty grading guide.")
            has_guide_from_zip = False
            _set_message(import_job, f"Using grading guide from template '{template.name}'.")

        if not guide_text or not reference_solution_text:
            _set_message(import_job, "Guide/reference missing. Generating missing content...")
            draft_data, _usage, generated_raw_response, _meta = generate_rubric_draft(
                assignment_text,
                selected_model,
                provider_cfg["base_url"],
                provider_cfg["api_key"],
                formatted_output=Config.LLM_FORMATTED_OUTPUT,
                additional_instructions="",
                json_mode=Config.LLM_USE_JSON_MODE,
                max_tokens=Config.LLM_MAX_OUTPUT_TOKENS,
                timeout=Config.LLM_REQUEST_TIMEOUT,
            )
            if not guide_text:
                guide_text = _normalize_generated_text(
                    draft_data.get("rubric_text"), "rubric_text"
                )
            if not reference_solution_text:
                reference_solution_text = _normalize_generated_text(
                    draft_data.get("reference_solution_text"),
                    "reference_solution_text",
                )
            _set_message(import_job, "Missing guide/reference content generated.")
        elif import_job.use_template_guide:
            _set_message(import_job, "Using guide from template and reference solution from ZIP.")
        else:
            _set_message(import_job, "Using guide and reference solution from ZIP.")

        should_auto_approve = has_guide_from_zip or bool(import_job.run_right_away)
        import_job.wait_for_guide_approval = not should_auto_approve

        assignment_title = (
            (import_job.import_title or "").strip()
            or _guess_assignment_title_from_zip(import_job.original_filename)
        )
        assignment = Assignment(
            title=assignment_title[:255],
            assignment_text=assignment_text,
            folder_name=import_job.folder_name or None,
        )
        db.session.add(assignment)
        db.session.flush()

        rubric = RubricVersion(
            assignment_id=assignment.id,
            rubric_text=guide_text,
            reference_solution_text=reference_solution_text,
            status=RubricStatus.APPROVED if should_auto_approve else RubricStatus.DRAFT,
            llm_provider=provider_key,
            llm_model=selected_model,
            formatted_output=Config.LLM_FORMATTED_OUTPUT,
            raw_response=generated_raw_response,
            error_message="",
            finished_at=_utcnow(),
        )
        db.session.add(rubric)
        db.session.flush()

        total = len(student_files)
        jobs = [] if should_auto_approve else None
        for index, student_identifier in enumerate(
            sorted(student_files.keys(), key=str.lower), start=1
        ):
            entries = student_files[student_identifier]
            submission = Submission(
                assignment_id=assignment.id,
                student_identifier=student_identifier,
                submitted_text="",
            )
            db.session.add(submission)
            db.session.flush()

            text_parts = []
            for entry in sorted(entries, key=lambda item: item["filename"].lower()):
                ext = entry["ext"]
                if ext in _ZIP_TEXT_EXTENSIONS:
                    text_value = _decode_text_blob(entry["data"])
                    if text_value:
                        text_parts.append(text_value)
                elif ext in _ZIP_BINARY_EXTENSIONS:
                    _store_submission_binary_file(
                        submission, entry["filename"], entry["data"]
                    )
            submission.submitted_text = "\n\n".join(text_parts).strip()

            if should_auto_approve:
                job = GradingJob(
                    assignment_id=assignment.id,
                    submission_id=submission.id,
                    rubric_version_id=rubric.id,
                    status=JobStatus.QUEUED,
                    llm_provider=provider_key,
                    llm_model=selected_model,
                    formatted_output=Config.LLM_FORMATTED_OUTPUT,
                    extra_instructions="",
                )
                db.session.add(job)
                jobs.append(job)
            import_job.imported_submissions = index
            db.session.flush()
            _set_message(import_job, f"Loaded {index}/{total} solutions.")

        db.session.commit()

        import_job.assignment_id = assignment.id
        import_job.status = JobStatus.SUCCESS
        import_job.finished_at = _utcnow()
        if should_auto_approve:
            from processing.job_queue import enqueue_submission_job

            _set_message(import_job, "Queueing grading jobs...")
            for job in jobs:
                job.queue_job_id = enqueue_submission_job(job.id)
            import_job.wait_for_guide_approval = False
            import_job.message = (
                f"Done. Loaded {import_job.imported_submissions} solutions and queued grading."
            )
        else:
            import_job.message = (
                "Guide/reference were not fully provided in ZIP. "
                "Loaded solutions and waiting for manual guide approval before grading."
            )
        db.session.commit()
    except LLMResponseError as exc:
        logger.exception("Assignment import failed for %s", import_id)
        import_job.status = JobStatus.ERROR
        import_job.error_message = str(exc)
        import_job.raw_response = exc.raw_text or ""
        import_job.finished_at = _utcnow()
        db.session.commit()
    except Exception as exc:
        logger.exception("Assignment import error for %s", import_id)
        import_job.status = JobStatus.ERROR
        import_job.error_message = str(exc)
        import_job.finished_at = _utcnow()
        db.session.commit()
