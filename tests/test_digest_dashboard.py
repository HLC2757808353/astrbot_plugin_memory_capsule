"""Browser regressions for digest navigation and text-sized cards.

Run with: python -m unittest discover -s tests -p test_digest_dashboard.py -v
Set CAPSULE_BROWSER_EXECUTABLE when Chromium is not in Playwright's cache.
"""

import os
from pathlib import Path
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


FIXTURE = r"""
window.digestRequests = [];
window.digestPosts = [];
window.failDigestRun = false;
const issues = {
    '2026-11-09': Array.from({length: 12}, (_, i) => ({
        id: i + 1, digest_date: '2026-11-09', category: 'AI',
        title: i === 0 ? 'AI 新闻：中文标题完整显示' : `第 ${i + 1} 条新闻`,
        summary: '这是一段完整的新闻摘要。页面应随文字自然变高，无需留出大块空白。',
        keywords: '人工智能,游戏', source: '测试资讯', chat_value: 5, hits: 4,
        url: i === 0 ? 'javascript:alert(1)' : 'https://example.com/news', archived: 0
    })),
    '2026-11-08': [{id: 20, digest_date: '2026-11-08', title: '昨天的新闻', summary: '', archived: 0}],
    '2026-10-01': [{id: 30, digest_date: '2026-10-01', title: '十月的新闻', summary: '', archived: 0}],
    '2025-12-20': [{id: 40, digest_date: '2025-12-20', title: '去年的新闻', summary: '', archived: 0}],
    '2024-02-29': [{id: 50, digest_date: '2024-02-29', title: '归档闰日新闻', summary: '', archived: 1}]
};
window.AstrBotPluginPage = {
    ready: async () => {},
    apiGet: async (endpoint, params = {}) => {
        if (endpoint === 'api/stats') return {memories: 1, relationships: 0, activities: 0};
        if (endpoint === 'api/activities') return {activities: []};
        if (endpoint === 'api/glossary/stats') return {total: 0};
        if (endpoint === 'api/glossary/categories') return {categories: []};
        if (endpoint === 'api/memories') return {memories: [{id: 100, content: '手写笔记', category: '日记'}], total: 1, total_pages: 1};
        if (endpoint === 'api/digest/search') {
            window.digestRequests.push({...params, endpoint});
            const items = Object.values(issues).flat().filter(it =>
                (params.include_archived || !it.archived) && it.title.includes(params.q));
            return {items};
        }
        if (endpoint !== 'api/digest/list') return {};
        window.digestRequests.push({...params, endpoint});
        if (params.date === '2026-11-08') await new Promise(resolve => setTimeout(resolve, 180));
        const available = Object.keys(issues).filter(d => params.include_archived || !issues[d][0].archived).sort().reverse();
        const calendar = [...new Set(available.map(d => d.slice(0, 7)))].map(month => {
            const days = available.filter(d => d.startsWith(month));
            return {year: month.slice(0, 4), month, days: days.length, items: days.reduce((n, d) => n + issues[d].length, 0)};
        });
        const selected = params.date || available.find(d =>
            (!params.year || d.startsWith(params.year)) && (!params.month || d.startsWith(params.month)))
            || (params.month ? params.month + '-01' : '2026-11-09');
        return {
            date: selected, year: selected.slice(0, 4), month: selected.slice(0, 7), calendar,
            dates: available.filter(d => d.startsWith(selected.slice(0, 7))).map(d => ({digest_date: d, n: issues[d].length})),
            items: issues[selected] || [], total: 16, today: 12, hits: 4, archived: 1,
            last_run: {created_at: '2026-11-09 09:00:00', status: 'ok', added: 12, sources: '测试资讯'}
        };
    },
    apiPost: async (endpoint, body) => {
        window.digestPosts.push(endpoint);
        return window.failDigestRun ? {status: 'error', message: '模型暂不可用'} : {result: {status: 'ok', added: 3}};
    }
};
"""


@unittest.skipIf(sync_playwright is None, "Playwright is optional for browser tests")
class DigestDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        candidates = [
            os.environ.get("CAPSULE_BROWSER_EXECUTABLE", ""),
            cls.runtime.chromium.executable_path,
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ]
        executable = next((p for p in candidates if p and Path(p).is_file()), None)
        if not executable:
            cls.runtime.stop()
            raise unittest.SkipTest(
                "Set CAPSULE_BROWSER_EXECUTABLE to an installed Chromium browser"
            )
        cls.browser = cls.runtime.chromium.launch(
            executable_path=executable, headless=True
        )
        cls.html = (
            Path(__file__).resolve().parents[1] / "pages" / "dashboard" / "index.html"
        ).read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1589, "height": 900})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.add_init_script(FIXTURE)
        self.page.route(
            "https://capsule.test/**",
            lambda route: route.fulfill(content_type="text/html", body=self.html),
        )
        self.page.goto("https://capsule.test/")
        self.page.wait_for_function("typeof window.dgLoad === 'function'")
        self.page.locator('[data-tab="digest"]').click()
        self.wait_issue("2026-11-09")

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def wait_issue(self, day):
        self.page.wait_for_function(
            "day => document.getElementById('dg-title').textContent.startsWith(day) && document.getElementById('dg-list').getAttribute('aria-busy') === 'false'",
            arg=day,
        )

    def test_cards_have_no_aspect_ratio_or_truncated_summary(self):
        cards = self.page.locator(".digest-card")
        self.assertEqual(cards.count(), 12)
        self.assertLess(cards.first.bounding_box()["height"], 250)
        self.assertEqual(self.page.locator("#dg-list .memory-card").count(), 0)
        self.assertEqual(
            self.page.locator('#dg-list a[href^="javascript:"]').count(), 0
        )
        self.assertTrue(self.page.locator(".digest-card-summary").first.is_visible())
        self.assertTrue(
            self.page.locator("#dg-list .digest-card-meta").first.is_visible()
        )
        self.assertEqual(
            self.page.locator("#view-digest").evaluate(
                "el => el.parentElement.className"
            ),
            "page-wrap",
        )
        self.assertEqual(self.page.locator("#view-memories #view-digest").count(), 0)
        self.page.locator('[data-tab="memories"]').click()
        self.assertFalse(self.page.locator("#view-digest").is_visible())

    def test_click_date_year_month_and_jump(self):
        self.page.locator('[data-digest-date="2026-11-08"] span').click()
        self.wait_issue("2026-11-08")
        self.assertEqual(
            self.page.locator('[aria-current="date"]').get_attribute(
                "data-digest-date"
            ),
            "2026-11-08",
        )
        self.page.locator("#dg-month").select_option("2026-10")
        self.wait_issue("2026-10-01")
        self.page.locator("#dg-year").select_option("2025")
        self.wait_issue("2025-12-20")
        self.page.locator("#dg-date").fill("2026-11-01")
        self.page.locator("#dg-date").dispatch_event("change")
        self.wait_issue("2026-11-01")
        self.assertIn("没有日报", self.page.locator("#dg-list").inner_text())
        self.page.locator("#dg-latest").click()
        self.wait_issue("2026-11-09")

    def test_archived_search_and_manual_run_controls(self):
        self.page.locator("#dg-arch").check()
        self.page.wait_for_function(
            "!!document.querySelector('#dg-year option[value=\"2024\"]')"
        )
        self.page.locator("#dg-year").select_option("2024")
        self.wait_issue("2024-02-29")
        self.assertIn("已归档", self.page.locator("#dg-list").inner_text())
        self.page.locator("#dg-search").fill("去年的")
        self.page.locator("#dg-search").press("Enter")
        self.page.wait_for_function(
            "document.getElementById('dg-title').textContent.startsWith('搜索')"
        )
        self.assertIn("去年的新闻", self.page.locator("#dg-list").inner_text())
        self.page.get_by_role("button", name="按日期看", exact=True).click()
        self.wait_issue("2024-02-29")
        self.page.locator("#dg-run-btn").click()
        self.wait_issue("2026-11-09")
        self.assertIn("新增 3 条", self.page.locator("#app-toast").inner_text())
        self.page.evaluate("window.failDigestRun = true")
        self.page.locator("#dg-run-btn").click()
        self.page.wait_for_function(
            "document.getElementById('app-toast').textContent.includes('模型暂不可用')"
        )
        self.assertFalse(self.page.locator("#dg-run-btn").is_disabled())

    def test_latest_response_wins_when_clicking_quickly(self):
        self.page.evaluate("dgLoad('2026-11-08'); dgLoad('2026-10-01')")
        self.wait_issue("2026-10-01")
        self.page.wait_for_timeout(250)
        self.assertTrue(
            self.page.locator("#dg-title").inner_text().startswith("2026-10-01")
        )

    def test_mobile_cards_and_controls_fit_viewport(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertLess(
            self.page.locator(".digest-card").first.bounding_box()["height"], 400
        )
        self.assertTrue(self.page.locator("#dg-date").is_visible())
        self.assertEqual(
            self.page.locator("#view-digest .stats-row").evaluate(
                "el => getComputedStyle(el).gridTemplateColumns.split(' ').length"
            ),
            2,
        )
        self.assertTrue(
            self.page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
        )
        self.page.locator("#dg-latest").click()
        self.wait_issue("2026-11-09")


if __name__ == "__main__":
    unittest.main()
