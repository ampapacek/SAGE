import io
import logging
import uuid
import zipfile
from pathlib import Path

from werkzeug.utils import secure_filename

from config import DATA_DIR, UPLOAD_DIR, PROCESSED_DIR
from db import db
from models import Submission, SubmissionFile

logger = logging.getLogger(__name__)


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def submission_upload_dir(assignment_id, submission_id):
    return ensure_dir(UPLOAD_DIR / f"assignment_{assignment_id}" / f"submission_{submission_id}")


def submission_processed_dir(assignment_id, submission_id):
    return ensure_dir(PROCESSED_DIR / f"assignment_{assignment_id}" / f"submission_{submission_id}")


def relpath_from_data(path):
    return str(Path(path).relative_to(DATA_DIR))


def resolve_data_path(rel_path):
    return DATA_DIR / rel_path


def detect_file_type(filename, mimetype=None):
    if mimetype:
        mimetype = mimetype.lower()
        if mimetype == "application/pdf":
            return "pdf"
        if mimetype.startswith("image/"):
            return "image"
        if mimetype.startswith("text/"):
            return "text"
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in {".png", ".jpg", ".jpeg"}:
        return "image"
    if ext in {".txt"}:
        return "text"
    return "other"


def _store_file_bytes(dest_dir, original_filename, data_bytes):
    safe_name = secure_filename(original_filename)
    ext = Path(safe_name).suffix.lower()
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest_path = dest_dir / unique_name
    dest_path.write_bytes(data_bytes)
    return dest_path, safe_name


def save_submission_files(submission, file_storages):
    stored_files = []
    dest_dir = submission_upload_dir(submission.assignment_id, submission.id)

    for storage in file_storages:
        if not storage or not storage.filename:
            continue
        original_filename = storage.filename
        safe_name = secure_filename(original_filename)
        ext = Path(safe_name).suffix.lower()
        unique_name = f"{uuid.uuid4().hex}{ext}"
        dest_path = dest_dir / unique_name
        storage.save(dest_path)

        submission_file = SubmissionFile(
            submission_id=submission.id,
            file_path=relpath_from_data(dest_path),
            file_type=detect_file_type(safe_name, storage.mimetype),
            original_filename=safe_name,
        )
        db.session.add(submission_file)
        stored_files.append(submission_file)

    return stored_files


def ingest_zip_upload(assignment_id, zip_storage):
    submissions = []
    submissions_by_student = {}

    if not zip_storage or not zip_storage.filename:
        return submissions

    zip_data = io.BytesIO(zip_storage.read())
    with zipfile.ZipFile(zip_data) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = Path(info.filename).parts
            if not parts:
                continue
            student_identifier = parts[0]
            filename = parts[-1]
            if not filename:
                continue

            submission = submissions_by_student.get(student_identifier)
            if submission is None:
                submission = Submission(
                    assignment_id=assignment_id,
                    student_identifier=student_identifier,
                    submitted_text="",
                )
                db.session.add(submission)
                db.session.flush()
                submissions_by_student[student_identifier] = submission
                submissions.append(submission)

            dest_dir = submission_upload_dir(assignment_id, submission.id)
            with zf.open(info) as file_obj:
                data = file_obj.read()

            dest_path, safe_name = _store_file_bytes(dest_dir, filename, data)
            submission_file = SubmissionFile(
                submission_id=submission.id,
                file_path=relpath_from_data(dest_path),
                file_type=detect_file_type(safe_name),
                original_filename=safe_name,
            )
            db.session.add(submission_file)

    logger.info("Ingested %s submissions from zip", len(submissions))
    return submissions


def collect_submission_images(submission):
    image_paths = []
    for file_record in submission.files:
        if file_record.file_type == "image":
            image_paths.append(resolve_data_path(file_record.file_path))

    processed_dir = submission_processed_dir(submission.assignment_id, submission.id)
    if processed_dir.exists():
        image_paths.extend(sorted(processed_dir.glob("**/*.png")))
        image_paths.extend(sorted(processed_dir.glob("**/*.jpg")))
        image_paths.extend(sorted(processed_dir.glob("**/*.jpeg")))

    return [str(p) for p in image_paths]


def collect_submission_text(submission):
    parts = []
    if submission.submitted_text:
        parts.append(submission.submitted_text)

    for file_record in submission.files:
        if file_record.file_type == "text":
            try:
                text = resolve_data_path(file_record.file_path).read_text(errors="ignore")
                parts.append(text)
            except OSError:
                logger.exception("Failed reading text file %s", file_record.file_path)

    return "\n\n".join([p for p in parts if p])
