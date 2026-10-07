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

    def test_models_endpoint(self):
        res = self.client.get('/v1/models')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('object'), 'list')
        self.assertTrue(len(data.get('data', [])) > 0)
