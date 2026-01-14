import csv
import io
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from config import Config, DATA_DIR, PROCESSED_DIR, UPLOAD_DIR
from db import db
from grading.schemas import safe_json_loads
from models import (
    Assignment,
    GradeResult,
    GradingJob,
    JobStatus,
    RubricStatus,
    RubricVersion,
    SubmissionFile,
    Submission,
)
from processing.file_ingest import (
    collect_submission_images,
    collect_submission_text,
    ingest_zip_upload,
    save_submission_files,
)
from processing.job_queue import enqueue_rubric_job, enqueue_submission_job, init_job_queue

logger = logging.getLogger(__name__)

_PRICE_ESTIMATE_RE = re.compile(r"price_estimate=\$([0-9]+(?:\.[0-9]+)?)")
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


def _utcnow():
    return datetime.now(timezone.utc)


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ensure_data_dirs():
    for path in (DATA_DIR, UPLOAD_DIR, PROCESSED_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _extract_price_estimate(message):
    if not message:
        return None
    match = _PRICE_ESTIMATE_RE.search(message)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _model_supports_images(model_name):
    if not model_name:
        return False
    name = model_name.strip().lower()
    for model in _IMAGE_CAPABLE_MODELS:
        if name == model or name.startswith(f"{model}-"):
            return True
    return False


def _submission_requires_images(submission):
    if not submission:
        return False
    for file_record in submission.files:
        if file_record.file_type in {"pdf", "image"}:
            return True
    return False


def _ensure_schema_updates():
    if db.engine.dialect.name != "sqlite":
        return
    try:
        result = db.session.execute(text("PRAGMA table_info(grading_job)"))
        columns = {row[1] for row in result.fetchall()}
        if "llm_model" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN llm_model VARCHAR(128)")
            )
            db.session.commit()
            logger.info("Added llm_model column to grading_job table")
        if "prompt_tokens" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN prompt_tokens INTEGER")
            )
            db.session.commit()
        if "completion_tokens" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN completion_tokens INTEGER")
            )
            db.session.commit()
        if "total_tokens" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN total_tokens INTEGER")
            )
            db.session.commit()
        if "price_estimate" not in columns:
            db.session.execute(
                text("ALTER TABLE grading_job ADD COLUMN price_estimate REAL")
            )
            db.session.commit()
        result = db.session.execute(text("PRAGMA table_info(rubric_version)"))
        rubric_columns = {row[1] for row in result.fetchall()}
        if "llm_model" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN llm_model VARCHAR(128)")
            )
            db.session.commit()
            logger.info("Added llm_model column to rubric_version table")
        if "error_message" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN error_message TEXT DEFAULT ''")
            )
            db.session.commit()
            logger.info("Added error_message column to rubric_version table")
        if "raw_response" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN raw_response TEXT DEFAULT ''")
            )
            db.session.commit()
            logger.info("Added raw_response column to rubric_version table")
        if "prompt_tokens" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN prompt_tokens INTEGER")
            )
            db.session.commit()
        if "completion_tokens" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN completion_tokens INTEGER")
            )
            db.session.commit()
        if "total_tokens" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN total_tokens INTEGER")
            )
            db.session.commit()
        if "price_estimate" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN price_estimate REAL")
            )
            db.session.commit()
        if "finished_at" not in rubric_columns:
            db.session.execute(
                text("ALTER TABLE rubric_version ADD COLUMN finished_at DATETIME")
            )
            db.session.commit()
    except Exception:
        logger.exception("Failed to apply schema updates")
        db.session.rollback()


def _init_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _get_approved_rubric(assignment_id):
    # "rubric" here refers to the grading guide shown in the UI.
    return (
        RubricVersion.query.filter_by(
            assignment_id=assignment_id, status=RubricStatus.APPROVED
        )
        .order_by(RubricVersion.created_at.desc())
        .first()
    )


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    _init_logging()
    db.init_app(app)

    with app.app_context():
        _ensure_data_dirs()
        db.create_all()
        _ensure_schema_updates()

    init_job_queue(app)

    @app.errorhandler(413)
    def too_large(_error):
        flash("Upload too large. Adjust MAX_CONTENT_LENGTH.")
        return redirect(request.referrer or url_for("list_assignments"))

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/assignments", methods=["GET", "POST"])
    def list_assignments():
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            assignment_text = request.form.get("assignment_text", "").strip()
            if not title or not assignment_text:
                flash("Title and assignment text are required.")
                return redirect(url_for("list_assignments"))

            assignment = Assignment(title=title, assignment_text=assignment_text)
            db.session.add(assignment)
            db.session.commit()
            return redirect(url_for("assignment_detail", assignment_id=assignment.id))

        assignments = Assignment.query.order_by(Assignment.created_at.desc()).all()
        return render_template("assignments.html", assignments=assignments)

    @app.route("/assignments/<int:assignment_id>")
    def assignment_detail(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        rubrics = (
            RubricVersion.query.filter_by(assignment_id=assignment_id)
            .order_by(RubricVersion.created_at.desc())
            .all()
        )
        submissions = (
            Submission.query.filter_by(assignment_id=assignment_id)
            .order_by(Submission.created_at.desc())
            .all()
        )
        jobs = (
            GradingJob.query.filter_by(assignment_id=assignment_id)
            .order_by(GradingJob.created_at.desc())
            .all()
        )
        has_pending_rubrics = any(
            rubric.status == RubricStatus.GENERATING for rubric in rubrics
        )
        for rubric in rubrics:
            if rubric.finished_at:
                finished_at = _as_utc(rubric.finished_at)
                created_at = _as_utc(rubric.created_at)
                if finished_at and created_at:
                    rubric.duration_seconds = (finished_at - created_at).total_seconds()
                else:
                    rubric.duration_seconds = None
            elif rubric.status == RubricStatus.GENERATING:
                created_at = _as_utc(rubric.created_at)
                if created_at:
                    rubric.duration_seconds = (_utcnow() - created_at).total_seconds()
                else:
                    rubric.duration_seconds = None
            else:
                rubric.duration_seconds = None
        for job in jobs:
            if job.started_at and job.finished_at:
                started_at = _as_utc(job.started_at)
                finished_at = _as_utc(job.finished_at)
                if started_at and finished_at:
                    job.duration_seconds = (finished_at - started_at).total_seconds()
                else:
                    job.duration_seconds = None
            elif job.started_at:
                started_at = _as_utc(job.started_at)
                if started_at:
                    job.duration_seconds = (_utcnow() - started_at).total_seconds()
                else:
                    job.duration_seconds = None
            else:
                job.duration_seconds = None
            job.price_estimate_display = job.price_estimate
            if job.price_estimate_display is None:
                job.price_estimate_display = _extract_price_estimate(job.message)
        has_active_jobs = any(
            job.status in {JobStatus.QUEUED, JobStatus.RUNNING} for job in jobs
        )
        approved_rubric = _get_approved_rubric(assignment_id)
        total_price_estimate = 0.0
        has_price_estimate = False
        for rubric in rubrics:
            if rubric.price_estimate is not None:
                total_price_estimate += rubric.price_estimate
                has_price_estimate = True
        for job in jobs:
            if job.price_estimate_display is not None:
                total_price_estimate += job.price_estimate_display
                has_price_estimate = True
        if not has_price_estimate:
            total_price_estimate = None

        return render_template(
            "assignment_detail.html",
            assignment=assignment,
            rubrics=rubrics,
            submissions=submissions,
            jobs=jobs,
            has_active_jobs=has_active_jobs,
            has_pending_rubrics=has_pending_rubrics,
            approved_rubric=approved_rubric,
            default_model=app.config.get("LLM_MODEL"),
            total_price_estimate=total_price_estimate,
        )

    @app.route("/assignments/<int:assignment_id>/delete", methods=["POST"])
    def delete_assignment(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        has_active_jobs = (
            GradingJob.query.filter(
                GradingJob.assignment_id == assignment_id,
                GradingJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            ).first()
            is not None
        )
        if has_active_jobs:
            flash("Cancel running jobs before deleting the assignment.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        submissions = Submission.query.filter_by(assignment_id=assignment_id).all()
        submission_ids = [s.id for s in submissions]

        if submission_ids:
            GradeResult.query.filter(
                GradeResult.submission_id.in_(submission_ids)
            ).delete(synchronize_session=False)
            SubmissionFile.query.filter(
                SubmissionFile.submission_id.in_(submission_ids)
            ).delete(synchronize_session=False)
            Submission.query.filter(
                Submission.id.in_(submission_ids)
            ).delete(synchronize_session=False)

        GradingJob.query.filter_by(assignment_id=assignment_id).delete(
            synchronize_session=False
        )
        RubricVersion.query.filter_by(assignment_id=assignment_id).delete(
            synchronize_session=False
        )
        db.session.delete(assignment)
        db.session.commit()

        shutil.rmtree(UPLOAD_DIR / f"assignment_{assignment_id}", ignore_errors=True)
        shutil.rmtree(PROCESSED_DIR / f"assignment_{assignment_id}", ignore_errors=True)

        flash("Assignment deleted.")
        return redirect(url_for("list_assignments"))

    @app.route("/assignments/<int:assignment_id>/status.json")
    def assignment_status(assignment_id):
        rubrics = RubricVersion.query.filter_by(assignment_id=assignment_id).all()
        jobs = GradingJob.query.filter_by(assignment_id=assignment_id).all()
        has_active_jobs = any(
            job.status in {JobStatus.QUEUED, JobStatus.RUNNING} for job in jobs
        )
        has_pending_rubrics = any(
            rubric.status == RubricStatus.GENERATING for rubric in rubrics
        )
        return jsonify(
            {
                "has_active_jobs": has_active_jobs,
                "has_pending_rubrics": has_pending_rubrics,
            }
        )

    @app.route("/assignments/<int:assignment_id>/rubrics/create", methods=["POST"])
    def create_rubric(assignment_id):
        rubric_text = request.form.get("rubric_text", "").strip()
        reference_solution_text = request.form.get("reference_solution_text", "").strip()
        if not rubric_text or not reference_solution_text:
            flash("Grading guide text and reference solution are required.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        rubric = RubricVersion(
            assignment_id=assignment_id,
            rubric_text=rubric_text,
            reference_solution_text=reference_solution_text,
            status=RubricStatus.DRAFT,
        )
        db.session.add(rubric)
        db.session.commit()
        flash("Grading guide saved as DRAFT.")
        return redirect(url_for("assignment_detail", assignment_id=assignment_id))

    @app.route("/assignments/<int:assignment_id>/rubrics/generate_draft", methods=["POST"])
    def generate_rubric(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        selected_model = request.form.get("llm_model", "").strip()
        if not selected_model:
            selected_model = app.config.get("LLM_MODEL")
        if _submission_requires_images(job.submission) and not _model_supports_images(
            selected_model
        ):
            flash("Selected model does not support images. Choose an image-capable model.")
            return redirect(url_for("job_detail", job_id=job.id))
        rubric = RubricVersion(
            assignment_id=assignment_id,
            rubric_text="",
            reference_solution_text="",
            status=RubricStatus.GENERATING,
            llm_model=selected_model,
            error_message="",
            raw_response="",
        )
        db.session.add(rubric)
        db.session.commit()

        enqueue_rubric_job(rubric.id)
        flash("Grading guide generation queued. It will appear when ready.")
        return redirect(url_for("assignment_detail", assignment_id=assignment_id))

    @app.route("/rubrics/<int:rubric_id>/approve", methods=["POST"])
    def approve_rubric(rubric_id):
        rubric = RubricVersion.query.get_or_404(rubric_id)
        if rubric.status != RubricStatus.DRAFT:
            flash("Only DRAFT guides can be approved.")
            return redirect(url_for("assignment_detail", assignment_id=rubric.assignment_id))

        RubricVersion.query.filter(
            RubricVersion.assignment_id == rubric.assignment_id,
            RubricVersion.id != rubric_id,
        ).update({"status": RubricStatus.ARCHIVED})

        rubric.status = RubricStatus.APPROVED
        db.session.commit()
        flash("Grading guide approved.")
        return redirect(url_for("assignment_detail", assignment_id=rubric.assignment_id))

    @app.route("/rubrics/<int:rubric_id>/cancel", methods=["POST"])
    def cancel_rubric(rubric_id):
        rubric = RubricVersion.query.get_or_404(rubric_id)
        if rubric.status != RubricStatus.GENERATING:
            flash("Grading guide is not generating.")
            return redirect(url_for("rubric_detail", rubric_id=rubric.id))
        rubric.status = RubricStatus.CANCELLED
        if "Cancelled by user." not in rubric.error_message:
            rubric.error_message = "Cancelled by user."
        if not rubric.finished_at:
            rubric.finished_at = _utcnow()
        db.session.commit()
        flash("Grading guide generation cancelled.")
        return redirect(url_for("rubric_detail", rubric_id=rubric.id))

    @app.route("/rubrics/<int:rubric_id>/delete", methods=["POST"])
    def delete_rubric(rubric_id):
        rubric = RubricVersion.query.get_or_404(rubric_id)
        if rubric.status == RubricStatus.GENERATING:
            flash("Cancel guide generation before deleting.")
            return redirect(url_for("rubric_detail", rubric_id=rubric.id))

        GradingJob.query.filter_by(rubric_version_id=rubric_id).delete(
            synchronize_session=False
        )
        GradeResult.query.filter_by(rubric_version_id=rubric_id).delete(
            synchronize_session=False
        )
        db.session.delete(rubric)
        db.session.commit()
        flash("Grading guide deleted.")
        return redirect(url_for("assignment_detail", assignment_id=rubric.assignment_id))

    @app.route("/rubrics/<int:rubric_id>")
    def rubric_detail(rubric_id):
        rubric = RubricVersion.query.get_or_404(rubric_id)
        assignment = Assignment.query.get_or_404(rubric.assignment_id)
        duration_seconds = None
        if rubric.finished_at:
            finished_at = _as_utc(rubric.finished_at)
            created_at = _as_utc(rubric.created_at)
            if finished_at and created_at:
                duration_seconds = (finished_at - created_at).total_seconds()
        elif rubric.status == RubricStatus.GENERATING:
            created_at = _as_utc(rubric.created_at)
            if created_at:
                duration_seconds = (_utcnow() - created_at).total_seconds()
        return render_template(
            "rubric_detail.html",
            rubric=rubric,
            assignment=assignment,
            duration_seconds=duration_seconds,
        )

    @app.route("/assignments/<int:assignment_id>/submissions/upload", methods=["POST"])
    def upload_submission(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        approved_rubric = _get_approved_rubric(assignment_id)
        if not approved_rubric:
            flash("Approve a grading guide before uploading submissions.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))
        zip_file = request.files.get("zip_file")
        selected_model = request.form.get("llm_model", "").strip()
        if not selected_model:
            selected_model = app.config.get("LLM_MODEL")

        submissions = []
        if zip_file and zip_file.filename:
            submissions = ingest_zip_upload(assignment_id, zip_file)
            db.session.commit()
        else:
            student_identifier = request.form.get("student_identifier", "").strip()
            submitted_text = request.form.get("submitted_text", "").strip()
            if not student_identifier:
                flash("Student identifier is required for single upload.")
                return redirect(url_for("assignment_detail", assignment_id=assignment_id))

            submission = Submission(
                assignment_id=assignment_id,
                student_identifier=student_identifier,
                submitted_text=submitted_text,
            )
            db.session.add(submission)
            db.session.commit()

            file_storages = request.files.getlist("files")
            save_submission_files(submission, file_storages)
            db.session.commit()
            submissions = [submission]

        if not submissions:
            flash("No submissions found in upload.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        requires_images = any(
            _submission_requires_images(submission) for submission in submissions
        )
        if requires_images and not _model_supports_images(selected_model):
            flash("Selected model does not support images. Choose an image-capable model.")
            return redirect(url_for("assignment_detail", assignment_id=assignment_id))

        for submission in submissions:
            job = GradingJob(
                assignment_id=assignment_id,
                submission_id=submission.id,
                rubric_version_id=approved_rubric.id,
                status=JobStatus.QUEUED,
                llm_model=selected_model,
            )
            db.session.add(job)
            db.session.commit()
            queue_id = enqueue_submission_job(job.id)
            job.queue_job_id = queue_id
            db.session.commit()

        flash(f"Queued {len(submissions)} submission(s) for grading.")
        return redirect(url_for("assignment_detail", assignment_id=assignment_id))

    @app.route("/submissions/<int:submission_id>")
    def submission_detail(submission_id):
        submission = Submission.query.get_or_404(submission_id)
        assignment = Assignment.query.get(submission.assignment_id)
        grade_result = (
            GradeResult.query.filter_by(submission_id=submission.id)
            .order_by(GradeResult.created_at.desc())
            .first()
        )
        images = collect_submission_images(submission)
        image_rel_paths = []
        for path in images:
            try:
                image_rel_paths.append(str(Path(path).relative_to(DATA_DIR)))
            except ValueError:
                image_rel_paths.append(path)
        student_text = collect_submission_text(submission)

        return render_template(
            "submission_detail.html",
            submission=submission,
            assignment=assignment,
            grade_result=grade_result,
            images=image_rel_paths,
            student_text=student_text,
        )

    @app.route("/assignments/<int:assignment_id>/export.csv")
    def export_csv(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        submissions = (
            Submission.query.filter_by(assignment_id=assignment_id)
            .order_by(Submission.created_at.asc())
            .all()
        )

        results_by_submission = {
            result.submission_id: result
            for result in GradeResult.query.join(Submission)
            .filter(Submission.assignment_id == assignment_id)
            .all()
        }

        max_parts = 0
        parsed_results = {}
        for submission in submissions:
            result = results_by_submission.get(submission.id)
            if not result:
                continue
            data, _error = safe_json_loads(result.json_result)
            if data:
                parts = data.get("parts", [])
                parsed_results[submission.id] = data
                max_parts = max(max_parts, len(parts))

        headers = ["student_identifier", "total_points"]
        for idx in range(1, max_parts + 1):
            headers.append(f"part{idx}_points")
        headers.append("rendered_text")

        def generate_rows():
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

            for submission in submissions:
                result = results_by_submission.get(submission.id)
                row = [submission.student_identifier, ""]
                parts_values = ["" for _ in range(max_parts)]
                rendered_text = ""

                if result:
                    data = parsed_results.get(submission.id)
                    if data:
                        row[1] = data.get("total_points", "")
                        parts = data.get("parts", [])
                        for idx, part in enumerate(parts[:max_parts]):
                            parts_values[idx] = part.get("points_awarded", "")
                        rendered_text = result.rendered_text or ""
                row.extend(parts_values)
                row.append(rendered_text)

                writer.writerow(row)
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)

        filename = f"assignment_{assignment.id}_grades.csv"
        return Response(
            generate_rows(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/jobs/<int:job_id>")
    def job_detail(job_id):
        job = GradingJob.query.get_or_404(job_id)
        auto_refresh = job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
        submission_requires_images = _submission_requires_images(job.submission)
        grade_result = (
            GradeResult.query.filter_by(
                submission_id=job.submission_id, rubric_version_id=job.rubric_version_id
            )
            .order_by(GradeResult.created_at.desc())
            .first()
        )
        duration_seconds = None
        if job.started_at and job.finished_at:
            started_at = _as_utc(job.started_at)
            finished_at = _as_utc(job.finished_at)
            if started_at and finished_at:
                duration_seconds = (finished_at - started_at).total_seconds()
        elif job.started_at:
            started_at = _as_utc(job.started_at)
            if started_at:
                duration_seconds = (_utcnow() - started_at).total_seconds()
        return render_template(
            "job_detail.html",
            job=job,
            duration_seconds=duration_seconds,
            grade_result=grade_result,
            auto_refresh=auto_refresh,
            default_model=app.config.get("LLM_MODEL"),
            submission_requires_images=submission_requires_images,
        )

    @app.route("/jobs/<int:job_id>/status.json")
    def job_status(job_id):
        job = GradingJob.query.get_or_404(job_id)
        duration_seconds = None
        if job.started_at and job.finished_at:
            started_at = _as_utc(job.started_at)
            finished_at = _as_utc(job.finished_at)
            if started_at and finished_at:
                duration_seconds = (finished_at - started_at).total_seconds()
        elif job.started_at:
            started_at = _as_utc(job.started_at)
            if started_at:
                duration_seconds = (_utcnow() - started_at).total_seconds()
        return jsonify(
            {
                "status": job.status,
                "duration_seconds": duration_seconds,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            }
        )

    @app.route("/jobs/<int:job_id>/terminate", methods=["POST"])
    def terminate_job(job_id):
        job = GradingJob.query.get_or_404(job_id)
        if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            flash("Job is not running or queued.")
            return redirect(url_for("job_detail", job_id=job.id))
        job.status = JobStatus.CANCELLED
        if not job.finished_at:
            job.finished_at = _utcnow()
        if "Cancelled by user." not in job.message:
            job.message = (job.message + "\n" if job.message else "") + "Cancelled by user."
        db.session.commit()
        flash("Job cancelled.")
        return redirect(url_for("job_detail", job_id=job.id))

    @app.route("/jobs/<int:job_id>/delete", methods=["POST"])
    def delete_job(job_id):
        job = GradingJob.query.get_or_404(job_id)
        if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
            flash("Cancel the job before deleting.")
            return redirect(url_for("job_detail", job_id=job.id))
        db.session.delete(job)
        db.session.commit()
        flash("Job deleted.")
        return redirect(url_for("assignment_detail", assignment_id=job.assignment_id))

    @app.route("/jobs/<int:job_id>/rerun", methods=["POST"])
    def rerun_job(job_id):
        job = GradingJob.query.get_or_404(job_id)
        rubric = RubricVersion.query.get(job.rubric_version_id)
        if not rubric or rubric.status != RubricStatus.APPROVED:
            flash("Approved grading guide required to rerun job.")
            return redirect(url_for("job_detail", job_id=job.id))

        selected_model = request.form.get("llm_model", "").strip()
        if not selected_model:
            selected_model = app.config.get("LLM_MODEL")

        grade_result = GradeResult(
            submission_id=job.submission_id,
            rubric_version_id=job.rubric_version_id,
            total_points=None,
            json_result="{}",
            rendered_text="",
            raw_response="",
            error_message="",
        )
        db.session.add(grade_result)
        db.session.commit()

        new_job = GradingJob(
            assignment_id=job.assignment_id,
            submission_id=job.submission_id,
            rubric_version_id=job.rubric_version_id,
            status=JobStatus.QUEUED,
            llm_model=selected_model,
        )
        db.session.add(new_job)
        db.session.commit()
        queue_id = enqueue_submission_job(new_job.id)
        new_job.queue_job_id = queue_id
        db.session.commit()
        flash(f"Queued rerun as job {new_job.id}.")
        return redirect(url_for("job_detail", job_id=new_job.id))

    @app.route("/data/<path:filepath>")
    def data_file(filepath):
        return send_from_directory(DATA_DIR, filepath)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
