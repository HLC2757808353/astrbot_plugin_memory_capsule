"""Database regressions using an empty temporary store and no AstrBot runtime."""

import importlib
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "memory_capsule_database_tests"


class DigestDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        # initialize() imports the repository database if the target is absent.
        self.db_path = self.data_dir / "memory.db"
        self.db_path.touch()

        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        astrbot = types.ModuleType("astrbot")
        astrbot.__path__ = []
        api = types.ModuleType("astrbot.api")
        api.__path__ = []
        api.logger = logging.getLogger(__name__)
        star = types.ModuleType("astrbot.api.star")
        star.StarTools = Mock()
        star.StarTools.get_data_dir.return_value = self.data_dir
        backup = types.ModuleType(f"{PACKAGE}.databases.backup")
        backup.BackupManager = Mock()
        modules = patch.dict(
            sys.modules,
            {
                PACKAGE: package,
                "astrbot": astrbot,
                "astrbot.api": api,
                "astrbot.api.star": star,
                f"{PACKAGE}.databases.backup": backup,
            },
        )
        modules.start()
        self.addCleanup(modules.stop)
        copy = patch(
            "shutil.copy2",
            side_effect=AssertionError("Repository database must not be copied"),
        )
        copy.start()
        self.addCleanup(copy.stop)
        database = importlib.import_module(f"{PACKAGE}.databases.db_manager")
        self.db = database.DatabaseManager(
            {
                "lightweight_mode": True,
                "memory_decay_enabled": False,
                "mmr_enabled": False,
                "tfidf_search_limit": 3,
            }
        )
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.assertEqual(Path(self.db.db_path), self.db_path)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM memories")[0]["n"], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM daily_digest")[0]["n"], 0)

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def memory(
        self,
        content,
        source="user",
        category="journal",
        archived=0,
        tags="news",
        created_at="2026-01-01 08:00:00",
        importance=8,
    ):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO memories (content, source, category, archived, tags, "
                    "created_at, updated_at, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        content,
                        source,
                        category,
                        archived,
                        tags,
                        created_at,
                        created_at,
                        importance,
                    ),
                )
                return cursor.lastrowid

    def digest(self, digest_date, title, archived=0):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO daily_digest (digest_date, title, archived) VALUES (?, ?, ?)",
                    (digest_date, title, archived),
                )

    def test_initialize_migrates_legacy_news_idempotently_without_changing_originals(
        self,
    ):
        self.memory(
            "[2025-12-31] Original title：Original summary",
            source="daily_digest",
            category="每日日报",
            tags="news,2026-02-01",
            archived=1,
            created_at="2026-01-02 09:10:11",
        )
        self.memory(
            "Tagged title：Tagged summary",
            source="daily_digest",
            tags="news,2024-06-09",
            created_at="2026-01-03 09:10:11",
        )
        self.memory(
            "[2026-99-99] Fallback title：Fallback summary",
            source="daily_digest",
            tags="2023-02-30,2023-02-28",
            created_at="2026-01-04 09:10:11",
        )
        self.memory(
            "Created title",
            source="daily_digest",
            tags="news",
            created_at="2022-03-04 10:11:12",
        )
        self.memory(
            '[2021-07-08] {"title":"Metadata title","summary":"Metadata summary",'
            '"source":"Publisher","url":"https://example.test/story"}',
            source="daily_digest",
            created_at="2021-07-09 10:11:12",
        )
        self.memory(
            "[2020-04-03] Text source title：Summary\nsource: Text publisher\n"
            "https://example.test/text",
            source="daily_digest",
        )
        self.memory(
            "[2026-01-05] Existing title：Old truncated summary", source="daily_digest"
        )
        journal_id = self.memory(
            "[2026-01-05] My news journal：news, 热点, 每日日报", category="每日日报"
        )
        self.db.add_digest_items(
            [
                {
                    "title": "Existing title",
                    "summary": "Full original summary",
                    "source": "Existing publisher",
                    "url": "https://example.test/original",
                }
            ],
            "2026-01-05",
        )
        originals = self.rows("SELECT * FROM memories ORDER BY id")
        existing = self.db.get_digest_by_date("2026-01-05", True)

        self.db.initialize(self.data_dir)
        first_pass = self.rows("SELECT * FROM daily_digest ORDER BY id")
        self.db.initialize(self.data_dir)

        self.assertEqual(self.rows("SELECT * FROM memories ORDER BY id"), originals)
        self.assertEqual(
            self.rows("SELECT * FROM daily_digest ORDER BY id"), first_pass
        )
        self.assertEqual(len(first_pass), 7)
        self.assertEqual(self.db.get_digest_by_date("2026-01-05", True), existing)
        migrated = {row["title"]: row for row in first_pass}
        original = migrated["Original title"]
        self.assertEqual(original["summary"], "Original summary")
        self.assertEqual(original["digest_date"], "2025-12-31")
        self.assertEqual(original["created_at"], "2026-01-02 09:10:11")
        self.assertEqual(original["archived"], 1)
        self.assertEqual(migrated["Tagged title"]["digest_date"], "2024-06-09")
        self.assertEqual(migrated["Fallback title"]["digest_date"], "2023-02-28")
        self.assertEqual(migrated["Created title"]["digest_date"], "2022-03-04")
        self.assertEqual(migrated["Metadata title"]["source"], "Publisher")
        self.assertEqual(
            migrated["Metadata title"]["url"], "https://example.test/story"
        )
        self.assertEqual(migrated["Text source title"]["source"], "Text publisher")
        self.assertEqual(
            migrated["Text source title"]["url"], "https://example.test/text"
        )
        self.assertEqual(
            [row["id"] for row in self.db.get_all_memories(include_archived=True)],
            [journal_id],
        )

    def test_migration_does_not_resurrect_deleted_digest_items(self):
        memory_id = self.memory(
            "[2025-12-31] Migrated once：Summary", source="daily_digest"
        )
        self.db.initialize(self.data_dir)
        self.assertEqual(len(self.db.get_digest_by_date("2025-12-31")), 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("DELETE FROM daily_digest")
        self.db.initialize(self.data_dir)
        self.assertEqual(self.db.get_recent_digest_dates(None), [])
        self.assertEqual(self.rows("SELECT id FROM memories"), [{"id": memory_id}])
        self.assertEqual(self.db.get_all_memories(include_archived=True), [])
        self.memory("[2026-01-01] New legacy row", source="daily_digest")
        self.db.initialize(self.data_dir)
        self.assertEqual(
            self.db.get_recent_digest_dates(None),
            [{"digest_date": "2026-01-01", "n": 1}],
        )

    def test_notebook_lists_counts_categories_tags_and_stats_share_filters(self):
        for i in range(25):
            self.memory(
                f"news automatic {i}",
                source="daily_digest",
                category="digest-only",
                tags="digest-only",
                archived=i % 2,
                importance=10,
                created_at="2030-01-01 00:00:00",
            )
        active = self.memory("My manual news journal", tags="manual-news")
        archived = self.memory(
            "My archived news journal", archived=1, tags="archived-only"
        )
        other = self.memory(
            "Other note", category="notes", source="daily_digest_manual"
        )
        null_source = self.memory("Unmarked old note", source=None, category="notes")

        for include_archived, expected in (
            (False, {active, other, null_source}),
            (True, {active, archived, other, null_source}),
        ):
            for category in (None, "journal", "notes", "digest-only"):
                with self.subTest(include_archived=include_archived, category=category):
                    listed = self.db.get_all_memories(
                        100, 0, category, include_archived
                    )
                    wanted = {row["id"] for row in listed}
                    self.assertTrue(wanted <= expected)
                    self.assertEqual(
                        self.db.get_memories_count(category, include_archived),
                        len(listed),
                    )
                    if category is None:
                        self.assertEqual(wanted, expected)
            self.assertEqual(
                self.db.get_memory_stats(include_archived)["memories"], len(expected)
            )
            self.assertEqual(
                {r["id"] for r in self.db.get_recent_memories(100, include_archived)},
                expected,
            )
            self.assertEqual(
                set(self.db.get_memory_categories(include_archived)),
                {"journal", "notes"},
            )
            self.assertNotIn("digest-only", self.db.get_all_tags(include_archived))
        self.assertEqual(self.db.get_memories_count(), 3)
        self.assertNotIn("archived-only", self.db.get_all_tags())
        self.assertIn("archived-only", self.db.get_all_tags(True))
        self.assertEqual(len(self.db.get_all_memories(1)), 1)
        self.assertEqual(len(self.db.get_all_memories(1, 1)), 1)

    def test_search_filters_all_candidate_engines_before_limits(self):
        for i in range(25):
            self.memory(
                f"needle news automatic {i}",
                source="daily_digest",
                importance=10,
                tags="needle",
                category="journal",
            )
            self.memory(
                f"needle other category {i}",
                category="other",
                importance=10,
                tags="needle",
            )
        active = self.memory("needle manual news journal", tags="needle", importance=7)
        archived = self.memory(
            "needle archived journal", tags="needle", archived=1, importance=9
        )
        engines = ("_fts_search", "_fallback_search", "_tag_retrieve", "_tfidf_search")
        with closing(self.db._get_connection()) as conn:
            for engine_name in engines:
                engine = getattr(self.db, engine_name)
                for include_archived, expected in (
                    (False, {active}),
                    (True, {active, archived}),
                ):
                    with self.subTest(
                        engine=engine_name, include_archived=include_archived
                    ):
                        results = engine(conn, "needle", 2, "journal", include_archived)
                        self.assertEqual({r["id"] for r in results}, expected)
        self.assertEqual(
            {r["id"] for r in self.db.search_memory("needle", "journal", 2)}, {active}
        )
        self.assertEqual(
            {r["id"] for r in self.db.search_memory("needle", "journal", 2, True)},
            {active, archived},
        )
        with (
            patch.object(self.db, "_fts_search", return_value=[]),
            patch.object(self.db, "_tag_retrieve", return_value=[]),
            patch.object(self.db, "_tfidf_search", return_value=[]),
        ):
            self.assertEqual(
                {r["id"] for r in self.db.search_memory("needle", "journal", 1)},
                {active},
            )
        self.assertEqual(self.db.get_memories_count("journal"), 1)

    def test_related_search_excludes_digest_and_archive_before_limits(self):
        for i in range(25):
            self.memory(
                f"needle automatic {i}",
                source="daily_digest",
                category="digest-only",
                importance=10,
            )
        active = self.memory("needle manual journal", importance=8)
        archived = self.memory("needle archived journal", archived=1, importance=9)
        self.assertEqual(
            {r["id"] for r in self.db.search_memory_related("needle", limit=2)},
            {active},
        )
        self.assertEqual(
            {
                r["id"]
                for r in self.db.search_memory_related(
                    "needle", limit=2, include_archived=True
                )
            },
            {active, archived},
        )
        self.assertEqual(
            {r["id"] for r in self.db.search_memory_related("unmatched", limit=2)},
            {active},
        )
        self.assertEqual(self.db.search_memory_related("needle", [active], 2), [])
        other = self.memory("needle other note", category="other", importance=10)
        self.assertEqual(
            {
                r["id"]
                for r in self.db.search_memory_related(
                    "needle", limit=2, category_filter="journal"
                )
            },
            {active},
        )
        self.assertEqual(
            {
                r["id"]
                for r in self.db.search_memory_related(
                    "unmatched", limit=2, category_filter="other"
                )
            },
            {other},
        )

    def test_digest_deduplication_does_not_archive_manual_news_journals(self):
        self.memory("[2026-01-01] Shared title", category="每日日报")
        self.memory("[2026-01-02] Shared title", category="每日日报")
        self.memory("[2026-01-01] Automatic title", source="daily_digest")
        self.memory("[2026-01-02] Automatic title", source="daily_digest")
        self.assertEqual(self.db.dedupe_digest_memories(), 1)
        self.assertEqual(self.db.get_memories_count(), 2)
        self.assertEqual(
            self.rows("SELECT archived FROM memories WHERE source = 'user'"),
            [{"archived": 0}, {"archived": 0}],
        )

    def test_digest_dates_and_calendar_cover_years_and_archived_only_months(self):
        start = date(2025, 12, 15)
        for i in range(50):
            stamp = (start + timedelta(days=i)).isoformat()
            self.digest(stamp, f"News {i}")
        self.digest("2026-01-01", "Second active item")
        self.digest("2026-01-01", "Archived item on mixed date", archived=1)
        self.digest("2024-04-03", "Archived-only month", archived=1)
        self.assertEqual(len(self.db.get_recent_digest_dates()), 7)
        self.assertEqual(len(self.db.get_recent_digest_dates(1)), 1)
        self.assertEqual(len(self.db.get_recent_digest_dates(None)), 51)
        self.assertEqual(len(self.db.get_recent_digest_dates(None, 2026)), 33)
        january = self.db.get_recent_digest_dates(None, "2026", "2026-01", False)
        self.assertEqual(len(january), 31)
        self.assertEqual(january, self.db.get_recent_digest_dates(None, 2026, 1, False))
        self.assertEqual(
            january,
            self.db.get_recent_digest_dates(
                None, month="2026-01", include_archived=False
            ),
        )
        self.assertEqual(
            next(r["n"] for r in january if r["digest_date"] == "2026-01-01"), 2
        )
        with_archive = self.db.get_recent_digest_dates(None, 2026, 1)
        self.assertEqual(
            next(r["n"] for r in with_archive if r["digest_date"] == "2026-01-01"), 3
        )
        self.assertEqual(self.db.get_recent_digest_dates(None, 2024, 4, False), [])
        self.assertEqual(
            self.db.get_recent_digest_dates(None, 2024, 4),
            [{"digest_date": "2024-04-03", "n": 1}],
        )
        self.assertEqual(
            self.db.get_digest_calendar(),
            [
                {"year": "2026", "month": "2026-02", "days": 2, "items": 2},
                {"year": "2026", "month": "2026-01", "days": 31, "items": 32},
                {"year": "2025", "month": "2025-12", "days": 17, "items": 17},
            ],
        )
        all_months = self.db.get_digest_calendar(True)
        self.assertEqual(
            all_months[-1], {"year": "2024", "month": "2024-04", "days": 1, "items": 1}
        )
        self.assertEqual(all_months[1]["items"], 33)
        self.assertEqual(self.db.get_recent_digest_dates(0), [])
        for params in (
            {"year": "2026' OR 1=1--"},
            {"year": "0000"},
            {"year": 2026, "month": "2025-01"},
            {"month": "2026-13"},
            {"month": 1},
            {"month": "2026-01' OR 1=1--"},
            {"limit": -1},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.db.get_recent_digest_dates(**params)

    def test_digest_search_can_reach_all_history_and_archived_items(self):
        self.digest("2001-01-01", "needle ancient active")
        self.digest("2002-01-01", "needle ancient archived", archived=1)
        self.digest(date.today().isoformat(), "needle today")
        self.assertEqual(len(self.db.search_digest("needle")), 1)
        self.assertEqual(len(self.db.search_digest("needle", 10, None)), 2)
        self.assertEqual(len(self.db.search_digest("needle", 10, None, True)), 3)
        self.assertEqual(self.db.search_digest("missing", 10, None, True), [])


if __name__ == "__main__":
    unittest.main()
