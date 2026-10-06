import unittest
from unittest.mock import MagicMock, patch

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

    def test_models_endpoint(self):
        res = self.client.get('/v1/models')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('object'), 'list')
        self.assertTrue(len(data.get('data', [])) > 0)
