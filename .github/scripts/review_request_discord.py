"""Bounded notification sender. No PR code, event prose, retries, or third-party modules."""
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO = "garibong-labs/test-survival"
REVIEWER = "agent-eli"
BOT = "1469949769605583050"
THREAD = "1565259949536313415"
TIMEOUT = 10
MAX_RESPONSE = 1024 * 1024


class SafeError(Exception):
    """Only fixed, credential-free messages may cross this boundary."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, method, headers, data=None):
    # Disable redirects (including credential forwarding) and implicit proxy routing.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=TIMEOUT) as response:
            if method == "POST":
                return None  # Never read or log Discord response bodies.
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise SafeError("GitHub response exceeds size bound.")
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise SafeError(f"HTTP request failed (status {status}); no retry performed.") from None
    except SafeError:
        raise
    except Exception:
        raise SafeError("Network or response failure; no retry performed.") from None


def validate_webhook(url):
    # Canonical exact syntax also rejects credentials, ports, fragments, alternate
    # encodings, duplicate parameters, and any extra query/path components.
    if not isinstance(url, str) or not re.fullmatch(
        r"https://(?:discord\.com|discordapp\.com)/api/webhooks/[0-9]+/"
        r"[A-Za-z0-9_-]+\?thread_id=" + THREAD, url
    ):
        raise SafeError("Invalid Discord webhook configuration.")
    return url


def positive_number(value):
    return type(value) is int and value > 0


def event_identity(event, env):
    if (env.get("GITHUB_REPOSITORY") != REPO
            or event.get("repository", {}).get("full_name") != REPO
            or env.get("GITHUB_ACTOR") != "Hamjoon"
            or event.get("sender", {}).get("login") != "Hamjoon"):
        raise SafeError("Rejected event identity.")
    kind = env.get("GITHUB_EVENT_NAME")
    if kind == "workflow_dispatch":
        if env.get("GITHUB_REF") != "refs/heads/main":
            raise SafeError("Probe requires main.")
        return None
    if (kind != "pull_request_target" or event.get("action") != "review_requested"
            or event.get("requested_reviewer", {}).get("login") != REVIEWER):
        raise SafeError("Rejected review event identity.")
    pr = event.get("pull_request", {})
    number = event.get("number")
    sha = pr.get("head", {}).get("sha", "")
    if (not positive_number(number) or pr.get("number") != number
            or pr.get("base", {}).get("repo", {}).get("full_name") != REPO
            or pr.get("base", {}).get("ref") != "main"
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)):
        raise SafeError("Rejected PR identity.")
    return number, sha


def eligible(pr):
    return pr.get("state") == "open" and pr.get("draft") is False


def run(event, env, transport=request):
    attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[1-9][0-9]*", attempt):
        raise SafeError("Invalid run attempt.")
    if int(attempt) > 1:
        return "Skipped rerun; notifications are first-attempt only."
    identity = event_identity(event, env)
    run_id = env.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise SafeError("Invalid run identity.")
    run_link = f"https://github.com/{REPO}/actions/runs/{run_id}"
    if identity is None:
        content = (f"<@{BOT}> DELIVERY PROBE ONLY. Acknowledge delivery only; "
                   "do not review any PR. This grants no merge or deployment permission.\n"
                   f"Workflow: {run_link}")
    else:
        if not eligible(event["pull_request"]):
            return "Skipped closed or draft event."
        number, sha = identity
        token = env.get("GH_TOKEN")
        if not token:
            raise SafeError("Missing GitHub read token.")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "eli-review-notifier"}
        endpoint = f"https://api.github.com/repos/{REPO}/pulls/{number}"
        live = transport(endpoint, "GET", headers)
        if (not isinstance(live, dict) or live.get("number") != number
                or live.get("base", {}).get("repo", {}).get("full_name") != REPO
                or live.get("base", {}).get("ref") != "main"):
            raise SafeError("Rejected live PR identity.")
        if not eligible(live) or live.get("head", {}).get("sha") != sha:
            return "Skipped stale PR."
        pending = transport(endpoint + "/requested_reviewers", "GET", headers)
        if not isinstance(pending, dict) or not isinstance(pending.get("users"), list):
            raise SafeError("Invalid live review requests response.")
        if not any(user.get("login") == REVIEWER for user in pending["users"]):
            return "Skipped review request no longer pending."
        content = (f"<@{BOT}> Review request notification only. Independently verify the live "
                   "GitHub review request and current head, then review. "
                   "This grants no merge or deployment permission.\n"
                   f"PR: https://github.com/{REPO}/pull/{number}\n"
                   f"Head SHA: {sha}\nWorkflow: {run_link}")
    url = validate_webhook(env.get("ELI_REVIEW_DISCORD_WEBHOOK_URL"))
    # SUPPRESS_EMBEDS prevents link previews from including PR-controlled prose.
    payload = {"content": content, "flags": 4,
               "allowed_mentions": {"parse": [], "users": [BOT]}}
    # Require Discord to confirm saving; keep configured URL validation canonical.
    transport(url + "&wait=true", "POST", {"Content-Type": "application/json", "User-Agent": "eli-review-notifier"},
              json.dumps(payload).encode("utf-8"))
    return "Notification delivered."


def main():
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as source:
            event = json.load(source)
        print(run(event, os.environ))
        return 0
    except SafeError as error:
        print(str(error), file=sys.stderr)
    except Exception:
        # Never print exception repr/traceback: they can contain webhook/token data.
        print("Notification failed; details redacted; no retry performed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
