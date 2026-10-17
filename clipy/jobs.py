import json
import logging
import threading
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .common import write_json_file

logger = logging.getLogger(__name__)

LOG_LINES = 40

# Kinds safe to restart from the top after the server dies mid-run. The
# pipeline caches every step; an interrupted upload may already exist on
# YouTube, so publishes are failed instead of retried.
RESUMABLE = {"prepare_source", "process_source"}


class _JobLog(logging.Handler):
    def __init__(self, job: dict, runner: JobRunner):
        super().__init__(logging.INFO)
        self.job = job
        self.runner = runner
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        with self.runner._lock:
            lines = self.job.setdefault("log", [])
            lines.append(self.format(record))
            del lines[:-LOG_LINES]
            self.runner._save()


class JobRunner:
    """Runs every channel's long work one job at a time on a background thread,
    persisted to disk. Each job belongs to a channel."""

    def __init__(self, path: Path, handlers: dict[str, Callable[[str, dict], object]]):
        self.path = path
        self.handlers = handlers
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._jobs: list[dict] = []
        if path.exists():
            self._jobs = json.loads(path.read_text(encoding="utf-8"))
        for job in self._jobs:
            if job["status"] == "running":
                if job["kind"] in RESUMABLE:
                    job["status"] = "queued"
                else:
                    job["status"] = "failed"
                    job["error"] = (
                        "Server restarted mid-job; check YouTube before retrying."
                    )
        self._save()
        threading.Thread(target=self._loop, name="clipy-jobs", daemon=True).start()

    def _save(self) -> None:
        with self._lock:
            write_json_file(self.path, self._jobs)

    def submit(self, channel: str, kind: str, args: dict) -> dict:
        job = {
            "id": uuid.uuid4().hex[:8],
            "channel": channel,
            "kind": kind,
            "args": args,
            "status": "queued",
            "submitted_at": datetime.now(UTC).isoformat(),
        }
        with self._lock:
            self._jobs.append(job)
            self._save()
        self._wake.set()
        return job

    def list(self, channel: str, limit: int = 10) -> list[dict]:
        """The channel's newest jobs first."""
        with self._lock:
            mine = [job for job in self._jobs if job["channel"] == channel]
            return json.loads(json.dumps(mine[::-1][:limit]))

    def active(self, channel: str, kind: str) -> list[dict]:
        return [
            job
            for job in self.list(channel, len(self._jobs))
            if job["kind"] == kind and job["status"] in {"queued", "running"}
        ]

    def _next(self) -> dict | None:
        with self._lock:
            return next((j for j in self._jobs if j["status"] == "queued"), None)

    def _loop(self) -> None:
        while True:
            job = self._next()
            if job is None:
                self._wake.wait()
                self._wake.clear()
                continue
            self._run(job)

    def _run(self, job: dict) -> None:
        with self._lock:
            job["status"] = "running"
            job["started_at"] = datetime.now(UTC).isoformat()
            self._save()
        handler = _JobLog(job, self)
        package_logger = logging.getLogger("clipy")
        package_logger.addHandler(handler)
        try:
            result = self.handlers[job["kind"]](job["channel"], job["args"])
            status, error = "succeeded", None
        except Exception as exc:  # noqa: BLE001 - a job failure must not kill the worker
            logger.error("Job %s failed: %s", job["id"], exc)
            logger.debug("%s", traceback.format_exc())
            result, status, error = None, "failed", f"{type(exc).__name__}: {exc}"
        finally:
            package_logger.removeHandler(handler)
        with self._lock:
            job["status"] = status
            job["result"] = result
            job["error"] = error
            job["finished_at"] = datetime.now(UTC).isoformat()
            self._save()
