"""Tests for the job queue.

Before it, Generate superseded whatever was running, which quietly discarded a
run the user had already been billed for. These pin the properties that
replaced that behaviour: jobs stack, cancelling is per job, a failure is carried
on the job rather than escaping into the queue, and hiding a run is a statement
about the LIST rather than about the run.
"""

from __future__ import annotations

import asyncio

import pytest

from rundiffusion_omniverse.jobs import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_RUNNING,
    JobQueue,
)


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
    asyncio.set_event_loop(None)


class TestJobsStack:
    def test_two_jobs_run_without_touching_each_other(self, loop):
        changes = []
        queue = JobQueue(on_changed=lambda: changes.append(1))

        async def work(_job):
            await asyncio.sleep(0)
            return ["image"]

        first = queue.submit("Tool A", work)
        second = queue.submit("Tool B", work)

        loop.run_until_complete(asyncio.gather(first.task, second.task))

        assert first.state == STATE_DONE
        assert second.state == STATE_DONE
        assert queue.running_count == 0
        # The panel is told to re-render rather than each job poking widgets.
        assert changes

    def test_jobs_are_listed_newest_first(self, loop):
        queue = JobQueue(on_changed=lambda: None)

        async def work(_job):
            return []

        first = queue.submit("First", work)
        second = queue.submit("Second", work)
        # Let them finish, so neither task is left unawaited at teardown.
        loop.run_until_complete(asyncio.gather(first.task, second.task))

        assert [job.tool_name for job in queue.jobs()] == ["Second", "First"]


class TestCancelling:
    def test_cancelling_one_leaves_the_other_running(self, loop):
        queue = JobQueue(on_changed=lambda: None)
        started = asyncio.Event()

        async def slow(_job):
            started.set()
            await asyncio.sleep(10)
            return []

        async def quick(_job):
            return ["image"]

        doomed = queue.submit("Slow", slow)
        survivor = queue.submit("Quick", quick)

        async def scenario():
            await started.wait()
            queue.cancel(doomed.id)
            await asyncio.gather(doomed.task, survivor.task)

        loop.run_until_complete(scenario())

        assert doomed.state == STATE_CANCELLED
        assert survivor.state == STATE_DONE

    def test_a_cancelled_job_says_it_stopped_watching(self, loop):
        """Cancel stops this plugin waiting. The run was already submitted and
        is already being billed, so the label must not claim it undid anything.
        """
        queue = JobQueue(on_changed=lambda: None)

        async def slow(_job):
            await asyncio.sleep(10)
            return []

        job = queue.submit("Tool", slow)

        async def scenario():
            await asyncio.sleep(0)
            queue.cancel(job.id)
            await job.task

        loop.run_until_complete(scenario())

        assert "stopped watching" in job.label

    def test_cancelling_does_not_take_the_queue_down(self, loop):
        """The CancelledError is an outcome of the job, not of the queue, so it
        is not re-raised: doing so would skip the notify that redraws the strip.
        """
        notified = []
        queue = JobQueue(on_changed=lambda: notified.append(1))

        async def slow(_job):
            await asyncio.sleep(10)
            return []

        job = queue.submit("Tool", slow)

        async def scenario():
            await asyncio.sleep(0)
            queue.cancel(job.id)
            await job.task

        loop.run_until_complete(scenario())

        # A notify AFTER the cancellation is the thing being asserted.
        assert len(notified) >= 2


class TestFailures:
    def test_a_raising_job_is_marked_failed_and_carries_its_message(self, loop):
        queue = JobQueue(on_changed=lambda: None)

        async def work(_job):
            raise RuntimeError("still needs Seed")

        job = queue.submit("Tool", work)
        loop.run_until_complete(job.task)

        assert job.state == STATE_FAILED
        assert job.error == "still needs Seed"
        assert "still needs Seed" in job.label

    def test_one_failure_does_not_affect_another_job(self, loop):
        queue = JobQueue(on_changed=lambda: None)

        async def boom(_job):
            raise RuntimeError("nope")

        async def fine(_job):
            return ["image"]

        bad = queue.submit("Bad", boom)
        good = queue.submit("Good", fine)
        loop.run_until_complete(asyncio.gather(bad.task, good.task))

        assert bad.state == STATE_FAILED
        assert good.state == STATE_DONE


class TestClearing:
    def test_clear_finished_keeps_running_jobs(self, loop):
        queue = JobQueue(on_changed=lambda: None)

        async def done(_job):
            return []

        async def slow(_job):
            await asyncio.sleep(10)
            return []

        finished = queue.submit("Done", done)
        running = queue.submit("Running", slow)

        async def scenario():
            await finished.task
            queue.clear_finished()
            remaining = [job.tool_name for job in queue.jobs()]
            assert remaining == ["Running"]
            assert running.state == STATE_RUNNING
            # Settled inside the loop, so the cancelled task is awaited rather
            # than left dangling for the interpreter to complain about.
            queue.cancel_all()
            await asyncio.gather(running.task, return_exceptions=True)

        loop.run_until_complete(scenario())


class TestHiding:
    """Hiding a run and cancelling one were the same call, and that was a bug.

    The panel offered Hide, promised the results would still arrive, and then
    cancelled the task that was polling for them. The run kept going and kept
    being billed, its images were never downloaded, and a plugin run never
    reaches the account library either, so the render was simply unreachable.
    """

    def test_a_hidden_job_still_delivers_its_results(self, loop):
        queue = JobQueue(on_changed=lambda: None)
        released = asyncio.Event()

        async def slow(_job):
            await released.wait()
            return ["image"]

        job = queue.submit("Tool", slow)

        async def scenario():
            await asyncio.sleep(0)
            queue.hide(job.id)
            released.set()
            await job.task

        loop.run_until_complete(scenario())

        assert job.hidden
        assert job.state == STATE_DONE
        assert job.results == ["image"]

    def test_hiding_takes_it_off_the_list_but_not_out_of_the_queue(self, loop):
        queue = JobQueue(on_changed=lambda: None)

        async def quick(_job):
            return ["image"]

        first = queue.submit("First", quick)
        second = queue.submit("Second", quick)
        queue.hide(first.id)
        loop.run_until_complete(asyncio.gather(first.task, second.task))

        assert [job.tool_name for job in queue.visible_jobs()] == ["Second"]
        # Still counted, because it still happened and was still billed.
        assert len(queue.jobs()) == 2

    def test_hidden_runs_still_in_flight_are_counted(self, loop):
        """The number that makes "something is still coming" true rather than
        hopeful. Nothing on screen is showing these."""
        queue = JobQueue(on_changed=lambda: None)
        released = asyncio.Event()

        async def slow(_job):
            await released.wait()
            return []

        hidden = queue.submit("Hidden", slow)
        queue.submit("Shown", slow)

        async def scenario():
            await asyncio.sleep(0)
            queue.hide(hidden.id)
            assert queue.hidden_running_count == 1
            assert queue.running_count == 2
            released.set()
            await asyncio.gather(*[job.task for job in queue.jobs()])

        loop.run_until_complete(scenario())

        # Finished, so no longer something that is still coming.
        assert queue.hidden_running_count == 0

    def test_hiding_notifies_so_the_list_redraws(self, loop):
        changes = []
        queue = JobQueue(on_changed=lambda: changes.append(1))

        async def quick(_job):
            return []

        job = queue.submit("Tool", quick)
        before = len(changes)
        queue.hide(job.id)
        loop.run_until_complete(job.task)

        assert len(changes) > before

    def test_hiding_an_unknown_job_is_not_an_error(self, loop):
        queue = JobQueue(on_changed=lambda: None)

        queue.hide("no-such-job")

    def test_cancelling_is_still_available_and_still_means_stop_watching(self, loop):
        """Hide is not a rename of cancel. Cancel still exists and still stops
        the watch, which is what shutting the panel down does to every run."""
        queue = JobQueue(on_changed=lambda: None)

        async def slow(_job):
            await asyncio.sleep(10)
            return []

        job = queue.submit("Tool", slow)

        async def scenario():
            await asyncio.sleep(0)
            queue.cancel(job.id)
            await job.task

        loop.run_until_complete(scenario())

        assert job.state == STATE_CANCELLED
        assert not job.hidden
