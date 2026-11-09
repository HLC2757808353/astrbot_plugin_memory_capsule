"""Exercise initialization, registered digest APIs, journal writes, and unload."""

import asyncio
import importlib
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_registers_working_digest_and_keeps_notebook_separate(self):
        package = Path(__file__).resolve().parents[1].name
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "memory.db").touch()
            routes = []
            api = types.ModuleType("astrbot.api")
            api.logger = logging.getLogger(__name__)
            event_api = types.ModuleType("astrbot.api.event")
            event_api.AstrMessageEvent = object
            event_api.filter = types.SimpleNamespace(
                llm_tool=lambda **kwargs: lambda fn: fn,
                on_llm_request=lambda **kwargs: lambda fn: fn,
            )
            provider_api = types.ModuleType("astrbot.api.provider")
            provider_api.ProviderRequest = object
            star_api = types.ModuleType("astrbot.api.star")
            star_api.Context = object

            class Star:
                def __init__(self, context):
                    self.context = context

            star_api.Star = Star
            star_api.register = lambda *args: lambda cls: cls
            star_api.StarTools = types.SimpleNamespace(
                get_data_dir=lambda _: Path(directory)
            )
            context = types.SimpleNamespace(
                register_web_api=lambda *args, **kwargs: routes.append(args)
            )
            modules = {
                "astrbot": types.ModuleType("astrbot"),
                "astrbot.api": api,
                "astrbot.api.event": event_api,
                "astrbot.api.provider": provider_api,
                "astrbot.api.star": star_api,
            }
            with patch.dict(sys.modules, modules):
                main = importlib.import_module(f"{package}.main")
                plugin = main.MemoryCapsulePlugin(
                    context,
                    {
                        "backup_interval": 0,
                        "lightweight_mode": True,
                        "digest_enabled": True,
                        "glossary_collect_enabled": False,
                    },
                )
                try:
                    await plugin.initialize()
                    self.assertGreater(len(routes), 20)
                    digest_route = next(
                        args for args in routes if args[0].endswith("/api/digest/list")
                    )
                    self.assertTrue(callable(digest_route[1]))
                    self.assertIsNotNone(plugin._background_task)
                    self.assertFalse(plugin._background_task.done())
                    plugin.db_manager.add_digest_items(
                        [{"title": "今天的资讯"}], "2026-11-10"
                    )
                    result = await plugin.write_journal(None, "今天写了一条笔记")
                    self.assertIn("saved", result.lower())
                    self.assertEqual(plugin.db_manager.get_memories_count(), 1)
                    self.assertEqual(
                        len(plugin.db_manager.get_digest_by_date("2026-11-10")), 1
                    )
                finally:
                    task = plugin._background_task
                    await plugin.terminate()
                    if task:
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                self.assertIsNone(plugin.db_manager)
                self.assertIsNone(plugin._background_task)


if __name__ == "__main__":
    unittest.main()
