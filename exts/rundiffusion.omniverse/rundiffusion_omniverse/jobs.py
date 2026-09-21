"""Runs in flight: several at once, each cancellable on its own.

Before this, Generate superseded whatever was already running, which quietly
threw away a run the user had already paid for. A generate takes tens of
seconds, so wanting a second one started before the first lands is the normal
case rather than an edge one.

Three properties are the point:

**Jobs stack.** Starting a second run does not touch the first.

**Cancelling is per job.** Cancelling one leaves the others alone. Note what
cancel does and does not mean: it stops this plugin waiting for the result. The
run itself was already submitted and is already being billed, so the queue says
"stopped watching" rather than claiming it undid anything.

**Hiding is not cancelling.** They were the same call once, and that was a bug
with a real cost: the panel offered Hide, promised the results would still
arrive, and then cancelled the task that was polling for them. The run kept
going and kept being billed, and its images were never downloaded. A plugin run
never reaches the account library either, so the generation was simply gone.
Hiding now takes the row off the list and leaves the work alone.

**Nothing blocks the UI.** Every job is an asyncio task off Kit's update loop,
and the queue reports changes through one callback so the panel re-renders in
one place instead of each job poking at widgets.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

#: States that no longer change, so the panel can offer to clear them.
FINISHED_STATES = frozenset({STATE_DONE, STATE_FAILED, STATE_CANCELLED})


@dataclass
class Job:
    """One generation, from submit to results."""

    id: str
    tool_name: str
    state: str = STATE_RUNNING
    #: What the job is doing right now, in the server's own words where it has
    #: any: capturing, submitting, queued, processing.
    detail: str = "Starting..."
    request_id: str | None = None
    error: str | None = None
    results: list = field(default_factory=list)
    created: datetime = field(default_factory=datetime.now)
    task: asyncio.Task | None = None
    #: Taken off the runs list by the user. About the panel, not about the run:
    #: a hidden job is still polling, still downloading, and still delivering.
    hidden: bool = False
    #: What was asked for, for the tile that stands in for the render while it
    #: is being made. Two runs of one tool are told apart by their prompt and by
    #: nothing else on screen.
    prompt: str = ""

    @property
    def is_finished(self) -> bool:
        return self.state in FINISHED_STATES

    @property
    def label(self) -> str:
        if self.state == STATE_RUNNING:
            return f"{self.tool_name}: {self.detail}"
        if self.state == STATE_DONE:
            count = len(self.results)
            return f"{self.tool_name}: {count} result(s)"
        if self.state == STATE_CANCELLED:
            return f"{self.tool_name}: stopped watching"
        return f"{self.tool_name}: {self.error or 'failed'}"


class JobQueue:
    """Owns the running jobs and tells the panel when anything changes."""

    def __init__(self, on_changed: Callable[[], None]) -> None:
        self._jobs: list[Job] = []
        self._on_changed = on_changed

    def jobs(self) -> list[Job]:
        """Newest first, which is the order a person looks for them in."""
        return list(reversed(self._jobs))

    def visible_jobs(self) -> list[Job]:
        """The ones the user has not taken off the list."""
        return [job for job in self.jobs() if not job.hidden]

    @property
    def running_count(self) -> int:
        return sum(1 for job in self._jobs if job.state == STATE_RUNNING)

    @property
    def hidden_running_count(self) -> int:
        """Runs still going that nothing on screen is showing.

        Worth counting rather than forgetting: it is the number that makes
        "something is still coming" a true statement instead of a hopeful one.
        """
        return sum(
            1 for job in self._jobs if job.hidden and job.state == STATE_RUNNING
        )

    def submit(
        self, tool_name: str, work: Callable[[Job], Any], *, prompt: str = ""
    ) -> Job:
        """Start `work(job)`, which may update the job as it goes.

        `work` returns the list of results. Raising marks the job failed; there
        is no separate error channel to keep in sync.
        """
        job = Job(id=uuid.uuid4().hex, tool_name=tool_name, prompt=prompt)
        self._jobs.append(job)

        async def _run() -> None:
            try:
                job.results = await work(job) or []
                job.state = STATE_DONE
            except asyncio.CancelledError:
                job.state = STATE_CANCELLED
                # Deliberately not re-raised. The cancellation is an outcome of
                # this job rather than of the queue, and letting it propagate
                # would take the notify below with it.
            except Exception as error:  # noqa: BLE001 - carried on the job
                logger.exception("RunDiffusion: job %s failed.", job.id)
                job.state = STATE_FAILED
                job.error = str(error)
            finally:
                self._notify()

        # Keep the reference on the job: an unreferenced task can be garbage
        # collected before it runs, which would look like a run that never
        # started.
        job.task = asyncio.ensure_future(_run())
        self._notify()
        return job

    def update(self, job: Job, detail: str) -> None:
        """Called by the work itself as it moves through the stages."""
        job.detail = detail
        self._notify()

    def hide(self, job_id: str) -> None:
        """Take a job off the list and KEEP WATCHING it.

        The distinction from `cancel` is the whole point. Cancelling stops the
        polling task, so the images are never fetched and the render cannot be
        recovered from anywhere; hiding is a statement about the list.
        """
        for job in self._jobs:
            if job.id == job_id:
                job.hidden = True
                self._notify()
                return

    def cancel(self, job_id: str) -> None:
        for job in self._jobs:
            if job.id != job_id:
                continue
            if job.task is not None and not job.task.done():
                job.task.cancel()
            return

    def clear_finished(self) -> None:
        self._jobs = [job for job in self._jobs if not job.is_finished]
        self._notify()

    def cancel_all(self) -> None:
        for job in list(self._jobs):
            self.cancel(job.id)
        self._jobs = []

    def _notify(self) -> None:
        try:
            self._on_changed()
        except Exception:  # noqa: BLE001 - a render error must not kill a job
            logger.exception("RunDiffusion: job listener failed.")
