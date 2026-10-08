import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app.services.clickhouse_client import ClickHouseClient


class QueryLoggingTests(unittest.TestCase):
    def test_database_errors_do_not_log_operational_values_or_response_body(self):
        client = ClickHouseClient()
        response = SimpleNamespace(status_code=400, text='Code: 62. Query error: secret-crew secret-password')
        transport = Mock()
        transport.post.return_value = response
        with patch.object(client, '_http', return_value=transport), self.assertLogs('app.services.clickhouse_client', level='INFO') as logs:
            with self.assertRaisesRegex(RuntimeError, 'error code 62') as raised:
                client.query('SELECT private_column FROM private_table WHERE crew={crew:String}', parameters={'crew': 'secret-crew'})
        logged = '\n'.join(logs.output) + str(raised.exception)
        for value in ('secret-crew', 'secret-password', 'private_column', 'private_table'):
            self.assertNotIn(value, logged)
        self.assertIn('sql_hash=', logged)
        self.assertIn('db_ms=', logged)

    def test_transport_exception_does_not_expose_source_url(self):
        client = ClickHouseClient()
        transport = Mock()
        transport.post.side_effect = httpx.ConnectError('https://username:private-password@example.invalid?param_secret=secret-crew')
        with patch.object(client, '_http', return_value=transport), self.assertLogs('app.services.clickhouse_client', level='WARNING') as logs:
            with self.assertRaisesRegex(RuntimeError, 'connection is unavailable') as raised:
                client.query('SELECT 1')
        logged = '\n'.join(logs.output) + str(raised.exception)
        self.assertNotIn('private-password', logged)
        self.assertNotIn('secret-crew', logged)
