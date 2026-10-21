import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app.services.clickhouse_client import ClickHouseClient


class QueryLoggingTests(unittest.TestCase):
    def test_database_errors_do_not_log_operational_values_or_response_body(self):
        client = ClickHouseClient()
        response = SimpleNamespace(status_code=400, read=lambda: b'Code: 62. Query error: secret-crew secret-password')
        transport = Mock()
        transport.stream.return_value.__enter__ = Mock(return_value=response)
        transport.stream.return_value.__exit__ = Mock(return_value=False)
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
        transport.stream.side_effect = httpx.ConnectError('https://username:private-password@example.invalid?param_secret=secret-crew')
        with patch.object(client, '_http', return_value=transport), self.assertLogs('app.services.clickhouse_client', level='WARNING') as logs:
            with self.assertRaisesRegex(RuntimeError, 'connection is unavailable') as raised:
                client.query('SELECT 1')
        logged = '\n'.join(logs.output) + str(raised.exception)
        self.assertNotIn('private-password', logged)
        self.assertNotIn('secret-crew', logged)


class StreamedRowsTests(unittest.TestCase):
    def test_rows_are_parsed_line_by_line_with_shared_texts(self):
        client = ClickHouseClient()
        lines = ['{"crew": "' + 'M1' + '", "n": 1}', '', '{"crew": "' + 'M' + '1' + '", "n": 2.5}']
        response = SimpleNamespace(status_code=200, iter_lines=lambda: iter(lines))
        transport = Mock()
        transport.stream.return_value.__enter__ = Mock(return_value=response)
        transport.stream.return_value.__exit__ = Mock(return_value=False)
        with patch.object(client, '_http', return_value=transport):
            rows = client.query('SELECT crew, n FROM t')
        self.assertEqual(rows, [{'crew': 'M1', 'n': 1}, {'crew': 'M1', 'n': 2.5}])
        self.assertIs(rows[0]['crew'], rows[1]['crew'])
        self.assertIs(next(iter(rows[0])), next(iter(rows[1])))
