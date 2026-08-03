"""Structural contract for consistent Dashboard sidebar alignment."""
import re
import unittest
from pathlib import Path

from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[2]
INDEX = (ROOT / "templates/dashboard/index.html").read_text(encoding="utf-8")


class DashboardSidebarAlignmentContractTests(unittest.TestCase):
    def test_all_second_level_groups_use_the_shared_alignment_class(self):
        soup = BeautifulSoup(INDEX, "html.parser")
        sidebar = soup.select_one("#sidebar")
        self.assertIsNotNone(sidebar)
        assert isinstance(sidebar, Tag)

        groups = sidebar.select("nav > ul > li > ul")
        self.assertGreaterEqual(len(groups), 4)
        for group in groups:
            self.assertIn("sidebar-submenu", group.get_attribute_list("class"))

    def test_leading_icon_slots_have_fixed_width_at_each_navigation_level(self):
        style = "\n".join(
            node.get_text(" ", strip=True)
            for node in BeautifulSoup(INDEX, "html.parser").select("head style")
        )

        top_level_rule = re.search(
            r"#sidebar\s+nav\s*>\s*ul\s*>\s*li\s*>\s*a\s*>\s*i:first-child\s*\{(?P<body>[^}]*)\}",
            style,
            re.S,
        )
        self.assertIsNotNone(top_level_rule)
        assert top_level_rule is not None
        self.assertRegex(top_level_rule.group("body"), r"width:\s*1\.25rem")
        self.assertRegex(top_level_rule.group("body"), r"flex:\s*0\s+0\s+1\.25rem")
        self.assertRegex(top_level_rule.group("body"), r"text-align:\s*center")

        child_rule = re.search(
            r"#sidebar\s+\.sidebar-submenu\s*>\s*li\s*>\s*a\s*>\s*i:first-child\s*\{(?P<body>[^}]*)\}",
            style,
            re.S,
        )
        self.assertIsNotNone(child_rule)
        assert child_rule is not None
        self.assertRegex(child_rule.group("body"), r"width:\s*1rem")
        self.assertRegex(child_rule.group("body"), r"flex:\s*0\s+0\s+1rem")
        self.assertRegex(child_rule.group("body"), r"text-align:\s*center")

    def test_second_level_groups_share_one_indent_and_link_padding(self):
        style = "\n".join(
            node.get_text(" ", strip=True)
            for node in BeautifulSoup(INDEX, "html.parser").select("head style")
        )
        group_rule = re.search(r"#sidebar\s+\.sidebar-submenu\s*\{(?P<body>[^}]*)\}", style, re.S)
        self.assertIsNotNone(group_rule)
        assert group_rule is not None
        self.assertRegex(group_rule.group("body"), r"margin-left:\s*1\.5rem")
        self.assertRegex(group_rule.group("body"), r"padding-left:\s*0\.75rem")

        link_rule = re.search(
            r"#sidebar\s+\.sidebar-submenu\s*>\s*li\s*>\s*a\s*\{(?P<body>[^}]*)\}",
            style,
            re.S,
        )
        self.assertIsNotNone(link_rule)
        assert link_rule is not None
        self.assertRegex(link_rule.group("body"), r"padding-left:\s*0\.75rem")
        self.assertRegex(link_rule.group("body"), r"display:\s*flex")
        self.assertRegex(link_rule.group("body"), r"align-items:\s*center")


if __name__ == "__main__":
    unittest.main()
