from datetime import datetime, timezone
from db import db


class RubricStatus:
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    ARCHIVED = "ARCHIVED"
    GENERATING = "GENERATING"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class JobStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


def _utcnow():
    return datetime.now(timezone.utc)


class Assignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    assignment_text = db.Column(db.Text, nullable=False)
    folder_name = db.Column(db.String(255))
    archived_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    rubrics = db.relationship("RubricVersion", backref="assignment", lazy=True)
    submissions = db.relationship("Submission", backref="assignment", lazy=True)


# "Rubric" in the codebase corresponds to the grading guide shown in the UI.
class RubricVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignment.id"), nullable=False)
    rubric_text = db.Column(db.Text, nullable=False)
    reference_solution_text = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default=RubricStatus.DRAFT, nullable=False)
    llm_provider = db.Column(db.String(64))
    llm_model = db.Column(db.String(128))
    error_message = db.Column(db.Text, default="", nullable=False)
    raw_response = db.Column(db.Text, default="", nullable=False)
    prompt_tokens = db.Column(db.Integer)
    completion_tokens = db.Column(db.Integer)
    total_tokens = db.Column(db.Integer)
    price_estimate = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    finished_at = db.Column(db.DateTime)


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignment.id"), nullable=False)
    student_identifier = db.Column(db.String(255), nullable=False)
    submitted_text = db.Column(db.Text, default="", nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    files = db.relationship("SubmissionFile", backref="submission", lazy=True)
    grade_results = db.relationship("GradeResult", backref="submission", lazy=True)


class SubmissionFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"), nullable=False)
    file_path = db.Column(db.Text, nullable=False)
    file_type = db.Column(db.String(20), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class GradingJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignment.id"), nullable=False)
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"), nullable=False)
    rubric_version_id = db.Column(db.Integer, db.ForeignKey("rubric_version.id"), nullable=False)
    status = db.Column(db.String(20), default=JobStatus.QUEUED, nullable=False)
    message = db.Column(db.Text, default="", nullable=False)
    queue_job_id = db.Column(db.String(128), default="", nullable=False)
    llm_provider = db.Column(db.String(64))
    llm_model = db.Column(db.String(128))
    prompt_tokens = db.Column(db.Integer)
    completion_tokens = db.Column(db.Integer)
    total_tokens = db.Column(db.Integer)
    price_estimate = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)

    submission = db.relationship("Submission", backref="jobs", lazy=True)
    rubric_version = db.relationship("RubricVersion", backref="jobs", lazy=True)


class GradeResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"), nullable=False)
    rubric_version_id = db.Column(db.Integer, db.ForeignKey("rubric_version.id"), nullable=False)
    total_points = db.Column(db.Float)
    json_result = db.Column(db.Text, nullable=False)
    rendered_text = db.Column(db.Text, nullable=False)
    raw_response = db.Column(db.Text, default="", nullable=False)
    error_message = db.Column(db.Text, default="", nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    rubric_version = db.relationship("RubricVersion", backref="grade_results", lazy=True)


class FolderOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True)
    sort_key = db.Column(db.String(255), nullable=False, unique=True)
    position = db.Column(db.Integer, nullable=False, default=0)
