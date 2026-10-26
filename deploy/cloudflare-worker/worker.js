// Cloudflare Worker: kick GitHub Actions `collect.yml` every 10 minutes.
//
// GitHub's own `schedule` trigger is throttled in practice (measured ~6 runs/day,
// median gap 4.1h), which starves the 20-minute quote freshness window. This Worker
// sends an authenticated workflow_dispatch instead, so the collection cadence is
// actually every 10 minutes.
//
// PAPER only. No live trading, no Nansen key, no repository writes.
// GITHUB_TOKEN lives only in Cloudflare Secrets and is never logged or returned.

const MAX_ERROR_CHARS = 300;
const USER_AGENT = "secondary-brain-radar-cron";

function workflowName(env) {
  return env.GITHUB_WORKFLOW || "collect.yml";
}

function refName(env) {
  return env.GITHUB_REF || "main";
}

// GitHub error bodies are usually {"message": "..."} but can be plain text or HTML.
// Only the message itself is surfaced: headers and token never enter the error.
function githubErrorMessage(body) {
  const text = typeof body === "string" ? body : "";
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed.message === "string" && parsed.message) {
      return parsed.message.slice(0, MAX_ERROR_CHARS);
    }
  } catch (_) {
    // Not JSON; fall through to the truncated raw body.
  }
  return text.slice(0, MAX_ERROR_CHARS);
}

export default {
  async scheduled(controller, env, ctx) {
    if (!env.GITHUB_TOKEN) {
      throw new Error("GITHUB_TOKEN secret is not set");
    }

    const owner = env.GITHUB_OWNER;
    const repo = env.GITHUB_REPO;
    const workflow = workflowName(env);
    const url =
      "https://api.github.com/repos/" +
      encodeURIComponent(owner) +
      "/" +
      encodeURIComponent(repo) +
      "/actions/workflows/" +
      encodeURIComponent(workflow) +
      "/dispatches";

    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: refName(env) }),
    });

    // workflow_dispatch answers 204 No Content on success.
    if (response.status === 204) {
      return;
    }

    const body = await response.text();
    throw new Error(
      "GitHub workflow_dispatch failed: status " +
        response.status +
        ": " +
        githubErrorMessage(body)
    );
  },

  // Unauthenticated health check. Configuration names are fine to expose; the
  // token itself is not, so only its presence is reported as a boolean.
  async fetch(request, env) {
    return Response.json({
      ok: true,
      owner: env.GITHUB_OWNER,
      repo: env.GITHUB_REPO,
      workflow: workflowName(env),
      ref: refName(env),
      token_configured: Boolean(env.GITHUB_TOKEN),
    });
  },
};
