"""API regressions for calendar navigation and notebook separation."""

import importlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
PACKAGE = Path(__file__).resolve().parents[1].name
DatabaseManager = importlib.import_module(
    f"{PACKAGE}.databases.db_manager"
).DatabaseManager
api_module = importlib.import_module(f"{PACKAGE}.embedded_api")


class Query(dict):
    def get(self, key, default=None, type=None):
        """Emulate the request query's optional type conversion.

        Args:
            key: Query parameter name.
            default: Missing or invalid value.
            type: Optional callable converting the string value.

        Returns:
            The converted value or default.
        """
        value = super().get(key, default)
        if type is not None:
            try:
                return type(value)
            except (ValueError, TypeError):
                return default
        return value


class TestAPI(api_module.EmbeddedAPI):
    @property
    def _req(self):
        return SimpleNamespace(query=self.query)


class DigestAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        Path(self.temp.name, "memory.db").touch()
        self.db = DatabaseManager({"backup_interval": 0, "lightweight_mode": True})
        self.db.initialize(self.temp.name)
        self.api = TestAPI(self.db, {"search_related_limit": 3})
        self.api.query = Query()
        for day, archived in [("2026-11-09", 0), ("2026-11-08", 0), ("2025-01-02", 1)]:
            self.db.add_digest_items(
                [{"title": "历史资讯 " + day, "summary": "摘要"}], day
            )
            if archived:
                self.db._execute_write(
                    lambda conn, day=day: conn.execute(
                        "UPDATE daily_digest SET archived=1 WHERE digest_date=?", (day,)
                    )
                )

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def test_latest_and_year_month_selection(self):
        result = await self.api.api_digest_list()
        self.assertEqual(result["date"], "2026-11-09")
        self.assertEqual(result["month"], "2026-11")
        self.assertEqual(len(result["dates"]), 2)
        self.assertEqual([m["year"] for m in result["calendar"]], ["2026"])
        self.api.query = Query(year="2025", include_archived="1")
        result = await self.api.api_digest_list()
        self.assertEqual(result["date"], "2025-01-02")
        self.assertEqual(result["items"][0]["archived"], 1)
        self.assertEqual({m["year"] for m in result["calendar"]}, {"2026", "2025"})
        self.api.query = Query(month="2026-11")
        result = await self.api.api_digest_list()
        self.assertEqual(result["date"], "2026-11-09")

    async def test_direct_date_empty_date_and_invalid_filters(self):
        self.api.query = Query(date="2026-11-08")
        self.assertEqual(
            (await self.api.api_digest_list())["items"][0]["digest_date"], "2026-11-08"
        )
        self.api.query = Query(date="2024-02-29")
        result = await self.api.api_digest_list()
        self.assertEqual(result["date"], "2024-02-29")
        self.assertEqual(result["items"], [])
        self.api.query = Query(year="2023")
        result = await self.api.api_digest_list()
        self.assertEqual((result["date"], result["month"]), ("2023-01-01", "2023-01"))
        for params in [
            dict(date="2026-02-30"),
            dict(year="bad"),
            dict(month="2026-13"),
            dict(year="2025", month="2026-11"),
        ]:
            self.api.query = Query(params)
            self.assertEqual((await self.api.api_digest_list())["status"], "error")

    async def test_search_finds_old_archived_items_only_when_requested(self):
        self.api.query = Query(q="2025-01-02")
        self.assertEqual((await self.api.api_digest_search())["items"], [])
        self.api.query["include_archived"] = "1"
        self.assertEqual(len((await self.api.api_digest_search())["items"]), 1)

    async def test_notebook_list_count_search_and_related_agree(self):
        for content, source, category in [
            ("开源模型 手写笔记", "user", "随笔"),
            ("开源模型 私人日记", "journal", "日记"),
            ("开源模型 自动新闻", "daily_digest", "每日日报"),
        ]:
            self.db.write_memory(
                content, category=category, source=source, tags=["开源", "模型"]
            )
        self.api.query = Query(category="日记")
        result = await self.api.api_memories_list()
        self.assertEqual(result["total"], len(result["memories"]))
        self.assertEqual(result["total"], 1)
        self.api.query = Query(include_archived="1")
        result = await self.api.api_memories_list()
        self.assertEqual(result["total"], 2)
        self.api.query = Query(q="开源模型", category="日记", include_archived="1")
        result = await self.api.api_memories_search()
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(all(r["category"] == "日记" for r in result["related"]))

    async def test_manual_fetch_uses_digest_provider_without_learner_module(self):
        provider = object()
        self.api.context = SimpleNamespace(get_all_providers=lambda: [provider])
        digest = importlib.import_module(f"{PACKAGE}.digest")
        with patch.object(digest, "run_digest_once", new_callable=AsyncMock) as run:
            run.return_value = {"status": "ok", "added": 2}
            result = await self.api.api_digest_run()
            self.assertEqual(result["result"]["added"], 2)
            self.assertIs(run.await_args.args[1], provider)

    def test_digest_handlers_are_registered(self):
        routes = []
        context = SimpleNamespace(
            register_web_api=lambda *args, **kwargs: routes.append(args)
        )
        api_module.register_embedded_apis(context, self.db)
        for suffix in ("/api/digest/list", "/api/digest/search", "/api/digest/run"):
            self.assertTrue(any(args[0].endswith(suffix) for args in routes))


if __name__ == "__main__":
    unittest.main()
