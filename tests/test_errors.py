import unittest

from chatgpt_web.errors import (
    BridgeError,
    BrowserError,
    BrowserInteractionError,
    BrowserLookupError,
    ChatGPTBusyError,
    ChatGPTContextLimitError,
    ChatGPTTimeoutError,
    ConfigurationError,
    ReplyExtractionError,
    SessionStateError,
    ToolError,
    ToolParseError,
)
from chatgpt_web.toolcalls import ToolCallExecutionError, ToolCallParseError


class DomainErrorHierarchyTests(unittest.TestCase):
    def test_all_domain_errors_share_bridge_base(self) -> None:
        errors = (
            BrowserError,
            BrowserLookupError,
            BrowserInteractionError,
            ReplyExtractionError,
            ToolError,
            ToolParseError,
            SessionStateError,
            ConfigurationError,
            ChatGPTTimeoutError,
            ChatGPTContextLimitError,
            ChatGPTBusyError,
        )
        for error_type in errors:
            with self.subTest(error_type=error_type.__name__):
                self.assertTrue(issubclass(error_type, BridgeError))
                self.assertTrue(issubclass(error_type, RuntimeError))

    def test_tool_pipeline_errors_remain_backward_compatible(self) -> None:
        self.assertTrue(issubclass(ToolCallParseError, ToolParseError))
        self.assertTrue(issubclass(ToolCallExecutionError, ToolError))

    def test_browser_specializations_have_meaningful_base(self) -> None:
        self.assertTrue(issubclass(BrowserLookupError, BrowserError))
        self.assertTrue(issubclass(BrowserInteractionError, BrowserError))


if __name__ == "__main__":
    unittest.main()
