import unittest

from chatgpt_web.events import (
    AssistantTextFinal,
    EventKind,
    GenerationFinished,
    GenerationStarted,
    ToolCall,
    completion_events,
    event_final_text,
    event_tool_calls,
)


class BridgeEventModelTests(unittest.TestCase):
    def test_text_completion_has_shared_event_sequence(self):
        events = completion_events("hello")

        self.assertEqual(
            [event.kind for event in events],
            [
                EventKind.GENERATION_STARTED,
                EventKind.ASSISTANT_TEXT_FINAL,
                EventKind.GENERATION_FINISHED,
            ],
        )
        self.assertIsInstance(events[0], GenerationStarted)
        self.assertIsInstance(events[1], AssistantTextFinal)
        self.assertIsInstance(events[2], GenerationFinished)
        self.assertEqual(event_final_text(events), "hello")
        self.assertEqual(event_tool_calls(events), [])

    def test_tool_completion_has_no_final_text_event(self):
        events = completion_events(
            'ignored browser text',
            [
                {"id": "call_1", "name": "bash", "arguments": {"command": "echo hi"}},
                {"id": "call_2", "name": "edit_markdown", "arguments": {"path": "README.md"}},
            ],
        )

        self.assertEqual(
            [event.kind for event in events],
            [
                EventKind.GENERATION_STARTED,
                EventKind.TOOL_CALL,
                EventKind.TOOL_CALL,
                EventKind.GENERATION_FINISHED,
            ],
        )
        calls = event_tool_calls(events)
        self.assertEqual([call.tool_call_id for call in calls], ["call_1", "call_2"])
        self.assertEqual([call.name for call in calls], ["bash", "edit_markdown"])
        self.assertEqual(calls[0].arguments, {"command": "echo hi"})
        self.assertIsNone(event_final_text(events))

    def test_kind_is_stable_for_protocol_adapters(self):
        text_event = AssistantTextFinal("done")
        tool_event = ToolCall("call_1", "bash", {"command": "echo hi"})

        self.assertEqual(text_event.kind, EventKind.ASSISTANT_TEXT_FINAL)
        self.assertEqual(tool_event.kind, EventKind.TOOL_CALL)


if __name__ == "__main__":
    unittest.main()
