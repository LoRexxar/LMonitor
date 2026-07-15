import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "templates/dashboard/index.html").read_text(encoding="utf-8")
JS = (ROOT / "static/dashboard/js/simc-workbench.js").read_text(encoding="utf-8")
MAIN = (ROOT / "static/dashboard/js/main.js").read_text(encoding="utf-8")


class SimcWorkbenchFrontendContractTests(unittest.TestCase):
    def test_ten_models_have_visible_entries(self):
        resources = (
            "batches", "tasks", "artifacts", "profiles", "secondary-rules",
            "mastery-rules", "templates", "backend", "apl-keywords", "apl-storage",
        )
        for resource in resources:
            self.assertIn(f'data-simc-model="{resource}"', HTML)
        model_block = HTML[HTML.index('aria-label="SimC 十模型入口"'):HTML.index('</div>', HTML.index('aria-label="SimC 十模型入口"'))]
        self.assertNotIn("sr-only", model_block)

    def test_task_is_default_and_has_task_batch_subtabs(self):
        self.assertIn('data-simc-panel="tasks"', HTML)
        self.assertIn('data-task-subtab="tasks"', HTML)
        self.assertIn('data-task-subtab="batches"', HTML)
        self.assertIn("activate('tasks')", JS)
        self.assertIn("switchSimcWorkbenchTab('tasks')", MAIN)

    def test_artifact_uses_actual_sandbox_helper(self):
        self.assertIn('id="simc-workbench-artifacts-panel"', HTML)
        self.assertIn("window.renderSimcArtifactFrame(url", JS)
        self.assertIn('sandbox=""', MAIN)
        self.assertIn("/api/simc-workbench/", JS)
        self.assertIn("artifacts", JS)

    def test_dedicated_api_and_inline_sections(self):
        self.assertIn("const apiRoot = '/api/simc-workbench/'", JS)
        for template_type in ("base_template", "default_apl", "custom_apl", "report_template", "command_fragment"):
            self.assertIn(f'data-template-type="{template_type}"', HTML)
        self.assertIn("UserAplStorage", HTML)
        self.assertIn("AplKeywordPair", HTML)
        self.assertIn('data-rule-subtab="secondary-rules"', HTML)
        self.assertIn('data-rule-subtab="mastery-rules"', HTML)
        for panel in ("tasks", "artifacts", "templates", "apl", "backend"):
            marker = f'id="simc-workbench-{panel}-panel"'
            start = HTML.index(marker)
            self.assertNotIn('></div>', HTML[start:start + len(marker) + 20])

    def test_workbench_controller_has_no_unsafe_or_legacy_navigation(self):
        forbidden = (
            "window.open", 'target="_blank"', "alert(", "prompt(", "confirm(",
            "modal", "appendChild", "开发中", "stub", "'/dashboard/'",
        )
        lowered = JS.lower()
        for token in forbidden:
            self.assertNotIn(token.lower(), lowered)
        self.assertIn("Number.parseInt", JS)
        self.assertIn("window.escapeHtml", JS)
        self.assertIn("startsWith('/')", JS)
        self.assertEqual(MAIN.count("function escapeHtml"), 1)

    def test_script_is_really_loaded(self):
        self.assertIn("{% static 'dashboard/js/simc-workbench.js' %}", HTML)
        self.assertNotIn("moveSimcToolIntoWorkbench", MAIN)
