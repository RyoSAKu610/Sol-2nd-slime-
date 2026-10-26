"""GHA workflow tests: state persistence, serialization, and secret hygiene.

Standard library only: the workflows are inspected as text so the suite does not
depend on PyYAML, but embedded Python heredocs are parsed with ast.
"""
import ast
import re
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
COLLECT = WORKFLOWS / "collect.yml"
SHARE = WORKFLOWS / "share.yml"

# Values that would mean a real credential was pasted into a workflow file.
SECRET_PATTERNS = (
    r"sk-[A-Za-z0-9_-]{8,}",
    r"gh[pousr]_[A-Za-z0-9]{8,}",
    r"github_pat_[A-Za-z0-9_]{8,}",
    r"xox[baprs]-[A-Za-z0-9-]{8,}",
    r"AKIA[0-9A-Z]{12,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9/+_-]{20,}",
)

ONCE = "--state .gha_state --config radar/config.gha.json once"


def text_of(path):
    return path.read_text(encoding="utf-8")


class WorkflowStateTests(unittest.TestCase):
    def test_state_cache_restore_concurrency_and_prune(self):
        for path in (COLLECT, SHARE):
            text = text_of(path)
            with self.subTest(workflow=path.name):
                self.assertIn("actions/cache@v6", text)
                self.assertIn("path: .gha_state", text)
                self.assertIn("key: gha-state-${{ github.run_id }}", text)
                self.assertIn("restore-keys: gha-state-", text)
                # collect and share must serialise on the same cached state.
                self.assertIn("group: gha-radar-state", text)
                self.assertIn("cancel-in-progress: false", text)
                self.assertIn("timeout-minutes: 10", text)
                self.assertIn("prune_observations(5000)", text)
                # Restore happens after checkout, the pass runs once, prune comes after it.
                self.assertLess(text.index("actions/checkout@v7"), text.index("actions/cache@v6"))
                self.assertLess(text.index("actions/cache@v6"), text.index(ONCE))
                self.assertLess(text.index(ONCE), text.index("prune_observations(5000)"))
                self.assertLess(text.index("prune_observations(5000)"),
                                text.index("radar.gha_snapshot"))

    def test_prune_step_is_valid_python_and_bounded(self):
        for path in (COLLECT, SHARE):
            text = text_of(path)
            with self.subTest(workflow=path.name):
                blocks = re.findall(r"<<'PY'\n(.*?)\n[ \t]*PY\n", text, re.S)
                self.assertEqual(len(blocks), 1)
                script = textwrap.dedent(blocks[0])
                ast.parse(script)  # SyntaxError fails the test.
                for needle in ("prune_observations(5000)", "wal_checkpoint(TRUNCATE)",
                               "observations", "trades", "tokens", "gha_state_bytes"):
                    self.assertIn(needle, script)

    def test_paper_only_env_and_no_secret_values(self):
        for path in (COLLECT, SHARE):
            text = text_of(path)
            with self.subTest(workflow=path.name):
                self.assertIn('NANSEN_API_KEY: ""', text)
                self.assertNotIn("${{ secrets.", text)
                self.assertNotIn("NANSEN_API_KEY: ${{", text)
                for pattern in SECRET_PATTERNS:
                    self.assertIsNone(re.search(pattern, text), pattern)

    def test_share_keeps_write_permission_and_docs_push(self):
        text = text_of(SHARE)
        self.assertIn("contents: write", text)
        self.assertIn("mkdir -p docs", text)
        self.assertIn("--out docs/slim.json", text)
        self.assertIn("docs/index.html", text)
        self.assertIn("docs/org.html", text)
        self.assertIn("git add docs/slim.json docs/index.html docs/org.html", text)
        self.assertIn("git commit -m", text)
        self.assertIn("git push", text)
        # Intermittent series stays annotated as separate from the resident series.
        self.assertIn("互換表示しない", text)
        # No profitability promise in the published page template.
        self.assertNotIn("利益を保証", text)

    def test_collect_stays_read_only_artifact_only(self):
        text = text_of(COLLECT)
        self.assertIn("contents: read", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("git push", text)
        self.assertIn("actions/upload-artifact@v7", text)
        self.assertIn("name: slim-fragment", text)
        self.assertNotIn("docs/", text)


if __name__ == "__main__":
    unittest.main()
