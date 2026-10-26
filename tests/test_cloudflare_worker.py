"""Cloudflare Worker tests: dispatch endpoint, secret hygiene, and Cron config.

Standard library only. worker.js is inspected as text so the suite needs no Node;
the syntax check runs `node --check` when Node is installed and skips otherwise.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER_DIR = ROOT / "deploy" / "cloudflare-worker"
WORKER = WORKER_DIR / "worker.js"
WRANGLER = WORKER_DIR / "wrangler.toml"
README = WORKER_DIR / "README.md"

# Anything that looks like a pasted credential rather than a reference.
SECRET_PATTERNS = (
    r"ghp_[A-Za-z0-9]{8,}",
    r"gh[pousr]_[A-Za-z0-9]{8,}",
    r"github_pat_[A-Za-z0-9_]{8,}",
    r"xox[baprs]-[A-Za-z0-9-]{8,}",
    r"AKIA[0-9A-Z]{12,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    # `Bearer ` followed by a literal value, not the runtime expression.
    r"Bearer\s+(?!\$\{)[A-Za-z0-9_./+=-]{8,}",
)

CONSOLE = re.compile(r"console\.(log|error|warn|info|debug)\s*\(")


def text_of(path):
    return path.read_text(encoding="utf-8")


class WorkerSourceTests(unittest.TestCase):
    def test_worker_is_es_module(self):
        text = text_of(WORKER)
        self.assertIn("export default {", text)
        self.assertRegex(text, r"async scheduled\(controller, env, ctx\)")
        self.assertRegex(text, r"async fetch\(request, env\)")

    def test_worker_has_no_credential_values(self):
        text = text_of(WORKER)
        for pattern in SECRET_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, text))

    def test_dispatch_endpoint_and_headers(self):
        text = text_of(WORKER)
        self.assertIn("https://api.github.com/repos/", text)
        self.assertIn("/actions/workflows/", text)
        self.assertIn("/dispatches", text)
        self.assertIn("Bearer ${env.GITHUB_TOKEN}", text)
        self.assertIn("application/vnd.github+json", text)
        self.assertIn("X-GitHub-Api-Version", text)
        self.assertIn("2022-11-28", text)
        self.assertIn("secondary-brain-radar-cron", text)
        self.assertIn('method: "POST"', text)
        self.assertIn("JSON.stringify({ ref: refName(env) })", text)

    def test_token_missing_and_failure_errors(self):
        text = text_of(WORKER)
        self.assertIn("GITHUB_TOKEN secret is not set", text)
        # Success is 204; failure surfaces only status + message, capped at 300 chars.
        self.assertIn("response.status === 204", text)
        self.assertIn("300", text)
        self.assertIn("githubErrorMessage", text)
        self.assertIn("status ", text)
        # The thrown message must not interpolate the token or headers.
        error_line = next(
            line for line in text.splitlines() if "GitHub workflow_dispatch failed" in line
        )
        self.assertNotIn("GITHUB_TOKEN", error_line)
        self.assertNotIn("headers", error_line)

    def test_health_check_never_returns_token_value(self):
        text = text_of(WORKER)
        self.assertIn("Response.json({", text)
        self.assertIn("ok: true", text)
        self.assertIn("owner: env.GITHUB_OWNER", text)
        self.assertIn("repo: env.GITHUB_REPO", text)
        self.assertIn("workflow: workflowName(env)", text)
        self.assertIn("ref: refName(env)", text)
        self.assertIn("token_configured: Boolean(env.GITHUB_TOKEN)", text)
        # Never echo the raw token back to the caller.
        self.assertNotRegex(text, r"token_configured:\s*env\.GITHUB_TOKEN")
        self.assertNotRegex(text, r"token\s*:\s*env\.GITHUB_TOKEN")

    def test_no_console_output_of_secrets(self):
        text = text_of(WORKER)
        calls = CONSOLE.findall(text)
        self.assertEqual(calls, [], calls)
        for line in text.splitlines():
            if CONSOLE.search(line):
                with self.subTest(line=line):
                    self.assertNotIn("GITHUB_TOKEN", line)
                    self.assertNotIn("headers", line)

    def test_readme_documents_secret_and_recovery(self):
        text = text_of(README)
        for needle in (
            "Fine-grained tokens",
            "Actions: Read and write",
            "Secret",
            "*/10 * * * *",
            "token_configured",
            "401",
            "403",
            "404",
            "422",
            "Revoke",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        # The token value placeholders must stay generic, never a real pattern.
        for pattern in SECRET_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, text))


class WranglerConfigTests(unittest.TestCase):
    def test_worker_identity_and_vars(self):
        text = text_of(WRANGLER)
        self.assertIn('name = "secondary-brain-radar-cron"', text)
        self.assertIn('main = "worker.js"', text)
        self.assertRegex(text, r'compatibility_date = "20(2[6-9]|[3-9][0-9])-\d{2}-\d{2}"')
        self.assertIn('GITHUB_OWNER = "RyoSAKu610"', text)
        self.assertIn('GITHUB_REPO = "Sol-2nd-slime-"', text)
        self.assertIn('GITHUB_WORKFLOW = "collect.yml"', text)
        self.assertIn('GITHUB_REF = "main"', text)

    def test_cron_every_ten_minutes(self):
        text = text_of(WRANGLER)
        self.assertIn("[triggers]", text)
        self.assertIn('crons = ["*/10 * * * *"]', text)

    def test_token_is_secret_only_not_in_config(self):
        text = text_of(WRANGLER)
        self.assertIn("wrangler secret put GITHUB_TOKEN", text)
        # No assignment of a token value anywhere in the config.
        self.assertNotRegex(text, r"GITHUB_TOKEN\s*=\s*[\"'][^\"']")
        for pattern in SECRET_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, text))

    def test_root_readme_notes_interval_measurement(self):
        text = text_of(ROOT / "README.md")
        self.assertIn("Cloudflare Cron", text)
        self.assertIn("4.1", text)
        self.assertIn("*/10", text)
        self.assertIn("deploy/cloudflare-worker/README.md", text)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class WorkerSyntaxTests(unittest.TestCase):
    def test_node_check(self):
        proc = subprocess.run(
            ["node", "--check", "deploy/cloudflare-worker/worker.js"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
