import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import database


class TranslationTests(unittest.TestCase):
    def test_parameters_literals_and_sqlite_statements(self):
        self.assertEqual(database.translate("SELECT * FROM users WHERE id=? AND name LIKE 'a?%'"), "SELECT * FROM users WHERE id=%s AND name LIKE 'a?%%'")
        self.assertEqual(database.translate("INSERT OR IGNORE INTO roles VALUES (?,?)"), "INSERT INTO roles VALUES (%s,%s) ON CONFLICT DO NOTHING")
        self.assertEqual(database.translate("CREATE TABLE t (a INTEGER, b REAL, c BLOB)"), "CREATE TABLE t (a BIGINT, b DOUBLE PRECISION, c BYTEA)")
        self.assertEqual(database.translate("UPDATE t SET a=a+1"), "UPDATE t SET a=a+1")

    def test_rows_allow_index_and_name_access(self):
        row_type = type("Row", (database.Row,), {"_names": ("id", "name"), "__slots__": ()})
        row = row_type(("u1", "Іван"))
        self.assertEqual((row[0], row["name"], dict(zip(row.keys(), row))), ("u1", "Іван", {"id": "u1", "name": "Іван"}))
        with self.assertRaises(IndexError):
            row["missing"]

    def test_sqlite_is_the_default(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DATABASE_URL": ""}):
            connection = database.connect(Path(directory) / "nested" / "store.sqlite3", "bi")
            try:
                self.assertIsInstance(connection, sqlite3.Connection)
                self.assertFalse(database.is_postgres(database.TrackedConnection(connection)))
            finally:
                connection.close()

    def test_schema_names_are_safe(self):
        with patch.dict(os.environ, {"DATABASE_SCHEMA": "app; DROP SCHEMA public", "DATABASE_ISOLATE_PATHS": ""}):
            with self.assertRaises(ValueError):
                database.schema_for(Path("x"))

    def test_read_cache_follows_the_revision(self):
        cache = database.ReadCache()
        revision = [1]
        with patch.dict(os.environ, {"STORE_READ_CACHE_SECONDS": "0"}):
            self.assertEqual(cache.current(lambda: revision[0]), 1)
            cache.put("key", 1, "value")
            self.assertEqual(cache.get("key", 1), (True, "value"))
            revision[0] = 2
            self.assertEqual(cache.current(lambda: revision[0]), 2)
            self.assertEqual(cache.get("key", 2), (False, None))


@unittest.skipUnless(database.postgres_enabled(), "needs DATABASE_URL")
class SqliteToPostgresTests(unittest.TestCase):
    def test_copies_every_store_and_refuses_to_overwrite(self):
        from app.bi.store import BiStore
        from app.core.app_configuration import ApplicationConfigurationStore
        from app.core.user_accounts import CreateUserRequest, UserAccountStore
        from app.tools import sqlite_to_postgres
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        paths = {"APP_USERS_DB": root / "users.sqlite3", "APP_CONFIGURATION_DB": root / "configuration.sqlite3", "APP_BI_DB": root / "bi.sqlite3"}
        url = os.environ.pop("DATABASE_URL")
        try:
            UserAccountStore(paths["APP_USERS_DB"]).create_user(CreateUserRequest(username="admin", display_name="Адмін", password="private-test-password-42", role="administrator"))
            ApplicationConfigurationStore(paths["APP_CONFIGURATION_DB"]).snapshot()
            BiStore(paths["APP_BI_DB"]).ensure_seed()
        finally:
            os.environ["DATABASE_URL"] = url
        environment = {name: str(path) for name, path in paths.items()}
        with patch.dict(os.environ, {**environment, "DATABASE_ISOLATE_PATHS": "", "DATABASE_SCHEMA": "migration_test"}):
            database.close_pools()
            try:
                copied = sqlite_to_postgres.migrate(replace=True, out=io.StringIO())
                self.assertEqual(copied["users"], 1)
                self.assertEqual(copied["configuration_versions"], 1)
                self.assertEqual(UserAccountStore(paths["APP_USERS_DB"]).users()[0].username, "admin")
                with self.assertRaises(SystemExit):
                    sqlite_to_postgres.migrate(out=io.StringIO())
            finally:
                connection = database.connect(paths["APP_BI_DB"], "bi")
                try:
                    connection._connection.execute("DROP SCHEMA migration_test CASCADE")
                finally:
                    connection.close()
                database.close_pools()


if __name__ == "__main__":
    unittest.main()
