import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from chatgpt_web.server import app


class ChatRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch('chatgpt_web.server.driver')
    def test_healthz_endpoint_ready(self, mock_driver):
        mock_driver.page = MagicMock()
        mock_driver.session_stats.return_value = {}
        mock_driver.session_keys.return_value = []
        mock_driver.cluster_stats.return_value = {}
        # healthz 会带出「桶 → 页面/会话」映射（T3.1）；假 driver 返回空表
        mock_driver.bucket_map.return_value = {}
        mock_driver.init_error = None

        res = self.client.get('/healthz')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('status'), 'ok')
        self.assertIn('cluster', data)
        self.assertEqual(data.get('buckets'), {})

    @patch('chatgpt_web.server.driver')
    def test_healthz_endpoint_degraded(self, mock_driver):
        mock_driver.page = None
        mock_driver.session_stats.return_value = {}
        mock_driver.session_keys.return_value = []
        mock_driver.cluster_stats.return_value = {}
        mock_driver.bucket_map.return_value = {}
        mock_driver.init_error = "Browser failed to start"

        res = self.client.get('/healthz')
        self.assertEqual(res.status_code, 503)
        data = res.json()
        self.assertEqual(data.get('status'), 'degraded')
        self.assertEqual(data.get('init_error'), "Browser failed to start")

    @patch('chatgpt_web.server.driver')
    def test_readiness_endpoint_ready(self, mock_driver):
        mock_driver.page = MagicMock()
        mock_driver.context = MagicMock()
        mock_driver.dom.find_input = AsyncMock(return_value=MagicMock())
        mock_driver.dom.find_new_chat = AsyncMock(return_value=MagicMock())
        mock_driver.page.url = 'https://chatgpt.com/'
        mock_driver.page.title.return_value = 'ChatGPT'

        res = self.client.get('/readiness')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('ready'))
        self.assertEqual(data['checks'], {
            'browser_ready': True,
            'chatgpt_page_ready': True,
            'authenticated': True,
            'composer_ready': True,
            'new_chat_ready': True,
        })

    @patch('chatgpt_web.server.driver')
    def test_readiness_endpoint_degraded_on_auth_surface(self, mock_driver):
        mock_driver.page = MagicMock()
        mock_driver.context = MagicMock()
        mock_driver.dom.find_input = AsyncMock(return_value=None)
        mock_driver.dom.find_new_chat = AsyncMock(return_value=None)
        mock_driver.page.url = 'https://chatgpt.com/auth/login'
        mock_driver.page.title.return_value = 'Log in - ChatGPT'

        res = self.client.get('/readiness')
        self.assertEqual(res.status_code, 503)
        data = res.json()
        self.assertFalse(data.get('ready'))
        self.assertTrue(data['checks']['chatgpt_page_ready'])
        self.assertFalse(data['checks']['authenticated'])
        self.assertFalse(data['checks']['composer_ready'])
        self.assertFalse(data['checks']['new_chat_ready'])

    @patch('chatgpt_web.server.driver')
    def test_diagnostics_includes_readiness_and_selectors(self, mock_driver):
        mock_driver.page = MagicMock()
        mock_driver.context = MagicMock()
        mock_driver.dom.find_input = AsyncMock(return_value=MagicMock())
        mock_driver.dom.find_new_chat = AsyncMock(return_value=MagicMock())
        mock_driver.dom.selector_diagnostics = AsyncMock(return_value=({'INPUT_SELECTORS': []}, {'INPUT_SELECTORS': False}))
        mock_driver.page.url = 'https://chatgpt.com/'
        mock_driver.page.title.return_value = 'ChatGPT'
        mock_driver.session_stats.return_value = {}
        mock_driver.session_keys.return_value = []
        mock_driver.cluster_stats.return_value = {}
        mock_driver.bucket_map.return_value = {}
        mock_driver.init_error = None

        res = self.client.get('/diagnostics')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn('readiness', data)
        self.assertTrue(data['readiness']['ready'])
        self.assertEqual(data['selectors']['INPUT_SELECTORS'], [])

    @patch('chatgpt_web.server.tasks')
    @patch('chatgpt_web.server.driver')
    def test_bash_fence_reply_is_recovered_as_tool_call(self, mock_driver, mock_tasks):
        """用户报的原例（端到端）：模型回 `bash` 代码块，桥仍要交出 tool_calls。

        背景（2026-10-07 用户实测）：提示词要求 ```tool_call 围栏后，模型回的却是

            ```bash
            git status --short
            ```

        旧行为：非流式路径解析出 0 条 → 回复被当纯文本返回，客户端以为任务结束。
        这里钉住修复后的行为：同一段回复必须返回 finish_reason=tool_calls。
        """
        import json as _json

        mock_driver.page = MagicMock()
        mock_driver.needs_seed.return_value = False
        mock_driver.sent_prompt.return_value = None
        mock_driver.send_chat = AsyncMock(
            return_value=("```bash\ngit status --short\n```", [])
        )
        mock_tasks.resume_block.return_value = None

        body = {
            'model': 'gpt-4o',
            'messages': [{'role': 'user', 'content': '看看仓库状态'}],
            'tools': [{
                'type': 'function',
                'function': {
                    'name': 'bash',
                    'description': 'Execute a bash command.',
                    'parameters': {
                        'type': 'object',
                        'properties': {'command': {'type': 'string'}},
                        'required': ['command'],
                    },
                },
            }],
        }
        res = self.client.post(
            '/v1/chat/completions',
            json=body,
            headers={'X-ChatGPT-Session': 't-shell-fence'},
        )
        self.assertEqual(res.status_code, 200, res.text)
        choice = res.json()['choices'][0]
        self.assertEqual(choice['finish_reason'], 'tool_calls')
        calls = choice['message'].get('tool_calls') or []
        self.assertEqual(len(calls), 1, choice)
        self.assertEqual(calls[0]['function']['name'], 'bash')
        self.assertEqual(
            _json.loads(calls[0]['function']['arguments']),
            {'command': 'git status --short'},
        )

    def test_models_endpoint(self):
        res = self.client.get('/v1/models')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('object'), 'list')
        self.assertTrue(len(data.get('data', [])) > 0)
