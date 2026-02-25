"""
tests/test_task_board.py
Core TaskBoard tests — lifecycle, timeout recovery, cancel/pause/retry.
"""

import time
import pytest
from core.task_board import (
    TaskBoard, TaskStatus, Task,
    CLAIMED_TIMEOUT, REVIEW_TIMEOUT,
)


class TestTaskBoardBasics:
    """Basic task lifecycle: create → claim → review → complete."""

    def test_create_and_claim(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        assert task.status == TaskStatus.PENDING
        assert task.task_id

        claimed = board.claim_next("jerry")
        assert claimed is not None
        assert claimed.task_id == task.task_id
        assert claimed.status == TaskStatus.CLAIMED
        assert claimed.agent_id == "jerry"

    def test_claim_returns_none_when_empty(self, tmp_workdir):
        board = TaskBoard()
        assert board.claim_next("jerry") is None

    def test_submit_review_complete(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")

        board.submit_for_review(task.task_id, "result text")
        t = board.get(task.task_id)
        assert t.status == TaskStatus.REVIEW
        assert t.result == "result text"
        assert t.review_submitted_at is not None

        board.add_review(task.task_id, "alic", 80, "good")
        completed = board.complete(task.task_id)
        assert completed.status == TaskStatus.COMPLETED

    def test_complete_simplified(self, tmp_workdir):
        """complete() now always marks as completed (no score-based rejection)."""
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.submit_for_review(task.task_id, "result")
        board.add_review(task.task_id, "alic", 30, "bad")

        result = board.complete(task.task_id)
        # Simplified: always completes regardless of score
        assert result.status == TaskStatus.COMPLETED

    def test_fail_task(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.fail(task.task_id, "some error")
        t = board.get(task.task_id)
        assert t.status == TaskStatus.FAILED

    def test_role_based_routing(self, tmp_workdir):
        board = TaskBoard()
        board.create("review code", required_role="review")
        # jerry (executor) should NOT match review role
        claimed = board.claim_next("jerry", agent_role="Implementation agent")
        assert claimed is None
        # alic (reviewer) should match
        claimed = board.claim_next("alic", agent_role="Peer reviewer")
        assert claimed is not None


class TestSafeAccess:
    """Verify .get() is used instead of direct subscript — no KeyError."""

    def test_submit_nonexistent_task(self, tmp_workdir):
        board = TaskBoard()
        # Should NOT raise KeyError
        board.submit_for_review("nonexistent", "result")

    def test_add_review_nonexistent_task(self, tmp_workdir):
        board = TaskBoard()
        board.add_review("nonexistent", "alic", 80, "ok")

    def test_complete_nonexistent_task(self, tmp_workdir):
        board = TaskBoard()
        result = board.complete("nonexistent")
        assert result is None

    def test_fail_nonexistent_task(self, tmp_workdir):
        board = TaskBoard()
        board.fail("nonexistent", "error")  # should not raise

    def test_flag_nonexistent_task(self, tmp_workdir):
        board = TaskBoard()
        board.flag("nonexistent", "tag")  # should not raise


class TestCancelPauseRetry:
    """Test cancel, pause, resume, and retry operations."""

    def test_cancel_pending(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        assert board.cancel(task.task_id) is True
        t = board.get(task.task_id)
        assert t.status == TaskStatus.CANCELLED

    def test_cancel_claimed(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        assert board.cancel(task.task_id) is True

    def test_cannot_cancel_completed(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.submit_for_review(task.task_id, "result")
        board.complete(task.task_id)
        assert board.cancel(task.task_id) is False

    def test_pause_and_resume(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        assert board.pause(task.task_id) is True
        t = board.get(task.task_id)
        assert t.status == TaskStatus.PAUSED

        # Cannot claim paused task
        assert board.claim_next("jerry") is None

        # Resume
        assert board.resume(task.task_id) is True
        t = board.get(task.task_id)
        assert t.status == TaskStatus.PENDING

        # Now claimable
        assert board.claim_next("jerry") is not None

    def test_retry_failed(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.fail(task.task_id, "error")

        assert board.retry(task.task_id) is True
        t = board.get(task.task_id)
        assert t.status == TaskStatus.PENDING
        assert t.retry_count == 1
        assert t.agent_id is None

    def test_retry_cancelled(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        board.cancel(task.task_id)
        assert board.retry(task.task_id) is True

    def test_cancel_all(self, tmp_workdir):
        board = TaskBoard()
        board.create("task 1")
        board.create("task 2")
        board.create("task 3")
        count = board.cancel_all()
        assert count == 3


class TestTimeoutRecovery:
    """Test automatic timeout recovery for stale tasks."""

    def test_recover_stale_claimed(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")

        # Simulate stale: set claimed_at far in the past
        data = board._read()
        data[task.task_id]["claimed_at"] = time.time() - CLAIMED_TIMEOUT - 10
        board._write(data)

        recovered = board.recover_stale_tasks()
        assert task.task_id in recovered

        t = board.get(task.task_id)
        assert t.status == TaskStatus.PENDING
        assert "timeout_recovered:claimed" in t.evolution_flags

    def test_recover_stale_review_no_scores(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.submit_for_review(task.task_id, "result")

        # Simulate stale review
        data = board._read()
        data[task.task_id]["review_submitted_at"] = time.time() - REVIEW_TIMEOUT - 10
        board._write(data)

        recovered = board.recover_stale_tasks()
        assert task.task_id in recovered

        t = board.get(task.task_id)
        # No reviews → auto-complete
        assert t.status == TaskStatus.COMPLETED

    def test_no_recovery_for_fresh_tasks(self, tmp_workdir):
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        # Fresh claim — should NOT be recovered
        recovered = board.recover_stale_tasks()
        assert len(recovered) == 0


class TestClearConfirmation:
    """Test that clear requires confirmation when active tasks exist."""

    def test_clear_refuses_with_active_tasks(self, tmp_workdir):
        board = TaskBoard()
        board.create("active task")
        result = board.clear(force=False)
        assert result == -1  # refused

    def test_clear_works_when_empty(self, tmp_workdir):
        board = TaskBoard()
        result = board.clear(force=False)
        assert result == 0

    def test_clear_force(self, tmp_workdir):
        board = TaskBoard()
        board.create("task 1")
        board.create("task 2")
        result = board.clear(force=True)
        assert result == 2


class TestResultAttribution:
    """Test that collect_results includes agent attribution."""

    def test_attribution_in_results(self, tmp_workdir):
        board = TaskBoard()
        t1 = board.create("plan", required_role="planner")
        board.claim_next("leo")
        board.submit_for_review(t1.task_id, "plan output")
        board.complete(t1.task_id)

        t2 = board.create("implement", parent_id=t1.task_id)
        board.claim_next("jerry")
        board.submit_for_review(t2.task_id, "code output here")
        board.complete(t2.task_id)

        result = board.collect_results(t1.task_id)
        assert "agent:jerry" in result
        assert "code output here" in result


class TestCritiqueFlow:
    """Test the new critique-based review flow."""

    def test_critique_passed(self, tmp_workdir):
        """Critique passed → directly completed."""
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.submit_for_review(task.task_id, "good result")

        board.add_critique(task.task_id, "alic", True, [], "looks great")
        t = board.get(task.task_id)
        assert t.status == TaskStatus.COMPLETED
        assert t.completed_at is not None

        # Check critique data
        data = board._read()
        critique = data[task.task_id]["critique"]
        assert critique["passed"] is True
        assert critique["reviewer"] == "alic"
        assert critique["comment"] == "looks great"

    def test_critique_rejected(self, tmp_workdir):
        """Critique not passed → CRITIQUE status with suggestions."""
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.submit_for_review(task.task_id, "mediocre result")

        board.add_critique(
            task.task_id, "alic", False,
            ["fix bug in line 5", "add error handling"], "needs work"
        )
        t = board.get(task.task_id)
        assert t.status == TaskStatus.CRITIQUE

        data = board._read()
        assert data[task.task_id]["critique_round"] == 1
        critique = data[task.task_id]["critique"]
        assert critique["passed"] is False
        assert len(critique["suggestions"]) == 2

    def test_critique_flow_full(self, tmp_workdir):
        """Full flow: submit → critique(reject) → claim_critique → fix → complete."""
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.submit_for_review(task.task_id, "initial result")

        # Advisor rejects
        board.add_critique(
            task.task_id, "alic", False,
            ["fix X"], "not good enough"
        )
        assert board.get(task.task_id).status == TaskStatus.CRITIQUE

        # jerry claims critique task for revision
        critique_task = board.claim_critique("jerry")
        assert critique_task is not None
        assert critique_task.task_id == task.task_id
        assert critique_task.status == TaskStatus.CLAIMED

        # jerry submits revised result and completes
        board.submit_for_review(task.task_id, "fixed result")
        board.complete(task.task_id)
        t = board.get(task.task_id)
        assert t.status == TaskStatus.COMPLETED
        assert t.result == "fixed result"

    def test_claim_critique_only_original_executor(self, tmp_workdir):
        """Only the original executor can claim their critique task."""
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.submit_for_review(task.task_id, "result")

        board.add_critique(task.task_id, "alic", False, ["fix it"], "bad")

        # Different agent cannot claim
        other = board.claim_critique("leo")
        assert other is None

        # Original executor can claim
        mine = board.claim_critique("jerry")
        assert mine is not None

    def test_simple_task_skip_review(self, tmp_workdir):
        """Simple complexity tasks: verify the complexity field is stored."""
        board = TaskBoard()
        task = board.create("list all items")

        # Set complexity via raw data (simulating what orchestrator does)
        with board.lock:
            data = board._read()
            data[task.task_id]["complexity"] = "simple"
            board._write(data)

        data = board._read()
        assert data[task.task_id]["complexity"] == "simple"

    def test_critique_max_rounds(self, tmp_workdir):
        """After max critique rounds, task can be force-completed."""
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.submit_for_review(task.task_id, "initial")

        # First critique round
        board.add_critique(task.task_id, "alic", False, ["fix A"], "round 1")
        data = board._read()
        assert data[task.task_id]["critique_round"] == 1

        # jerry claims, fixes, resubmits
        board.claim_critique("jerry")
        board.submit_for_review(task.task_id, "fixed result")

        # Force complete after max rounds
        board.complete(task.task_id)
        t = board.get(task.task_id)
        assert t.status == TaskStatus.COMPLETED

    def test_recover_stale_critique(self, tmp_workdir):
        """Stale CRITIQUE tasks are force-completed after timeout."""
        board = TaskBoard()
        task = board.create("test task")
        board.claim_next("jerry")
        board.submit_for_review(task.task_id, "result")

        board.add_critique(task.task_id, "alic", False, ["fix it"], "bad")
        assert board.get(task.task_id).status == TaskStatus.CRITIQUE

        # Simulate stale: set critique ts far in the past
        data = board._read()
        data[task.task_id]["critique"]["ts"] = time.time() - CLAIMED_TIMEOUT - 10
        board._write(data)

        recovered = board.recover_stale_tasks()
        assert task.task_id in recovered

        t = board.get(task.task_id)
        assert t.status == TaskStatus.COMPLETED
        assert "timeout_recovered:critique" in t.evolution_flags

    def test_add_critique_nonexistent(self, tmp_workdir):
        """add_critique on nonexistent task should not raise."""
        board = TaskBoard()
        board.add_critique("nonexistent", "alic", True, [], "ok")

    def test_claim_critique_none_available(self, tmp_workdir):
        """claim_critique returns None when no critique tasks exist."""
        board = TaskBoard()
        assert board.claim_critique("jerry") is None
