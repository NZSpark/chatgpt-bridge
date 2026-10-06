import unittest

from chatgpt_web.task_state import TaskState, TaskStateName


class TaskStateTransitionTests(unittest.TestCase):
    def test_happy_path(self):
        state = TaskState()
        state.transition(TaskStateName.PROMPT_BUILT)
        state.transition(TaskStateName.MODEL_GENERATING)
        state.transition(TaskStateName.TOOL_CALL_DETECTED)
        state.transition(TaskStateName.TOOL_EXECUTING)
        state.transition(TaskStateName.TOOL_RESULT_RETURNED)
        state.transition(TaskStateName.MODEL_GENERATING)
        state.complete()

        self.assertEqual(state.state, TaskStateName.COMPLETED)
        self.assertTrue(state.terminal)
        self.assertEqual(state.transition_count, 7)

    def test_plain_text_completion_is_terminal(self):
        state = TaskState()
        state.transition(TaskStateName.PROMPT_BUILT)
        state.transition(TaskStateName.MODEL_GENERATING)
        state.complete()

        self.assertEqual(state.state, TaskStateName.COMPLETED)
        with self.assertRaises(ValueError):
            state.transition(TaskStateName.MODEL_GENERATING)

    def test_completed_cannot_execute_tool(self):
        state = TaskState()
        state.transition(TaskStateName.PROMPT_BUILT)
        state.transition(TaskStateName.MODEL_GENERATING)
        state.complete()

        with self.assertRaises(ValueError):
            state.transition(TaskStateName.TOOL_EXECUTING)

    def test_invalid_transitions_are_rejected(self):
        state = TaskState()
        with self.assertRaises(ValueError):
            state.transition(TaskStateName.TOOL_CALL_DETECTED)

        state.transition(TaskStateName.PROMPT_BUILT)
        with self.assertRaises(ValueError):
            state.transition(TaskStateName.TOOL_RESULT_RETURNED)

    def test_terminal_failure_states_cannot_restart(self):
        for terminal in (
            TaskStateName.FAILED,
            TaskStateName.TIMEOUT,
            TaskStateName.CONTEXT_LIMIT,
            TaskStateName.UPSTREAM_BUSY,
        ):
            state = TaskState()
            state.transition(TaskStateName.PROMPT_BUILT)
            state.transition(TaskStateName.MODEL_GENERATING)
            state.transition(terminal, error="test failure")

            self.assertTrue(state.terminal)
            self.assertEqual(state.last_error, "test failure")
            with self.assertRaises(ValueError):
                state.transition(TaskStateName.MODEL_GENERATING)

    def test_session_recovery_can_return_to_prompt_or_generation(self):
        state = TaskState()
        state.transition(TaskStateName.PROMPT_BUILT)
        state.transition(TaskStateName.MODEL_GENERATING)
        state.transition(TaskStateName.SESSION_RECOVERY)
        state.transition(TaskStateName.PROMPT_BUILT)
        state.transition(TaskStateName.MODEL_GENERATING)
        state.complete()

    def test_duplicate_transition_is_rejected(self):
        state = TaskState()
        state.transition(TaskStateName.PROMPT_BUILT)
        with self.assertRaises(ValueError):
            state.transition(TaskStateName.PROMPT_BUILT)


if __name__ == "__main__":
    unittest.main()
