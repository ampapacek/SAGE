import logging
import os
import queue
import threading

from redis import Redis
from rq import Queue

from processing.assignment_runner import process_assignment_generation
from processing.job_runner import process_submission_job
from processing.rubric_runner import process_rubric_generation

logger = logging.getLogger(__name__)

_local_queue = queue.Queue()
_worker_started = False
_use_rq = False
_rq_queue = None


def init_job_queue(app):
    global _use_rq, _rq_queue
    redis_url = app.config.get("REDIS_URL")

    if redis_url:
        try:
            redis_conn = Redis.from_url(redis_url)
            redis_conn.ping()
            _rq_queue = Queue(connection=redis_conn)
            _use_rq = True
            logger.info("Using RQ with Redis at %s", redis_url)
        except Exception as exc:
            logger.warning("Redis unavailable; falling back to local queue: %s", exc)
            _use_rq = False

    if not _use_rq:
        _start_local_worker(app)


def enqueue_submission_job(job_id):
    if _use_rq:
        job = _rq_queue.enqueue(process_submission_job, job_id)
        return job.id

    _local_queue.put((process_submission_job, (job_id,)))
    return f"local-{job_id}"


def enqueue_rubric_job(rubric_id):
    if _use_rq:
        job = _rq_queue.enqueue(process_rubric_generation, rubric_id)
        return job.id

    _local_queue.put((process_rubric_generation, (rubric_id,)))
    return f"local-rubric-{rubric_id}"


def enqueue_assignment_job(generation_id):
    if _use_rq:
        job = _rq_queue.enqueue(process_assignment_generation, generation_id)
        return job.id

    _local_queue.put((process_assignment_generation, (generation_id,)))
    return f"local-assignment-{generation_id}"


def _start_local_worker(app):
    global _worker_started
    if _worker_started:
        return

    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    _worker_started = True

    def _worker():
        with app.app_context():
            while True:
                job_item = _local_queue.get()
                if job_item is None:
                    break
                try:
                    if isinstance(job_item, tuple):
                        func, args = job_item
                        func(*args)
                    else:
                        process_submission_job(job_item)
                except Exception:
                    logger.exception("Local worker failed job %s", job_item)
                finally:
                    _local_queue.task_done()

    thread = threading.Thread(target=_worker, name="local-grading-worker", daemon=True)
    thread.start()
    logger.info("Started local background worker thread")
