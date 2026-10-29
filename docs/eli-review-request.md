# Eli review request notification

Status: code port only; secret provisioning, receiver admission, and end-to-end
delivery have not been verified. This integration is not claimed provisioned or
active. Infrastructure creation is blocked for this task: no webhook creation,
secret changes, or receiver configuration changes are part of this port.

Activation has three separate stages (parent/integrator owns external operations):

1. **Code merged:** merge this workflow, helper, tests, and documentation into `garibong-labs/test-survival` **main**. Production events must target `main`; the trigger, job gate, event validation, and live PR validation all enforce this base constraint. Other base branches are intentionally unsupported.
2. **Secret provisioned and receiver admitted:** when separately authorized, the operator must provision the repository Actions secret `ELI_REVIEW_DISCORD_WEBHOOK_URL` with the full Discord webhook URL ending in exactly `?thread_id=1565259949536313415`. Accepted format: `https://discord.com/api/webhooks/<numeric-id>/<token>?thread_id=1565259949536313415` (`discordapp.com` also accepted). The webhook must have access to thread `1565259949536313415` under parent channel `1534851587359375430`. The receiver must admit notifications for `garibong-labs/test-survival` in this exact thread to Eli bot `1469949769605583050`, with requester `Hamjoon` and individual GitHub reviewer `agent-eli`. Verify the thread's parent and receiver admission operationally; the sender validates the exact thread query but cannot establish its parent or receiver policy. Do not paste the secret into code, logs, PRs, or dispatch inputs. Neither merging code nor a passing test provisions these prerequisites.
3. Optionally, as `Hamjoon`, run **Eli review request notification → Run workflow → main** once. It sends a clearly marked delivery probe to bot `1469949769605583050`: acknowledge delivery only, no PR review.
4. **Real request end-to-end verification:** as `Hamjoon`, open a non-draft PR targeting main and **Request** review from `agent-eli`. Verify the workflow, message in the exact thread, and Eli's receipt and independent live GitHub verification. Existing review requests are not backfilled: remove the old request and **Request** again after the prerequisites are ready. A probe alone does not verify the real review-request path.

Only the exact repository, requester/actor `Hamjoon`, and individually requested reviewer `agent-eli` qualify. The helper independently verifies the event identity, then reads the current PR and pending review requests using a token with only `contents: read` and `pull-requests: read`. Closed/draft PRs, changed heads, and requests no longer pending are skipped. Team requests do not qualify. No PR code is executed: checkout uses trusted base `github.sha`, disables persisted credentials, and selects only the sender helper. No package installation is needed.

The fixed message contains only notification instructions, a constructed repository PR URL, validated head SHA, and workflow run URL. No PR title, body, branch name, event URL, or arbitrary instructions enter the message. Both review notifications and manual delivery probes set Discord `flags: 4` (`SUPPRESS_EMBEDS`) to suppress automatic link previews, which could otherwise include PR-controlled titles or descriptions. Allowed mentions enable only the designated bot and disable automatic parsing. The bot must independently verify GitHub's live request and head before reviewing. A notification grants no merge or deployment permission.

Webhook validation requires HTTPS, an exact Discord host, canonical webhook path, and the exact thread query; alternate ports, credentials, fragments, redirects, and extra query parameters are rejected. After validating the configured URL, the helper appends fixed `&wait=true` only to the actual POST URL so Discord confirms saving the message and reports creation errors. Keep the secret ending in exactly `?thread_id=1565259949536313415`; do not add `wait` to it. `Notification delivered.` means Discord confirmed message creation, which is distinct from the agent receiving, acknowledging, or acting on the notification. GitHub credentials are sent only to fixed GitHub API endpoints. Discord response bodies are never read or logged, and exception details are never logged. Each network operation has a 10-second timeout; responses read from GitHub are capped at 1 MiB. There are at most two GETs and one POST per real event, or one POST per probe. There are no retries, including 429s and ambiguous POST failures; run attempts above 1 always skip. A failed or timed-out POST may already have delivered: inspect the thread before issuing a new probe/request. A fresh remove-and-Request event is a new notification, not a rerun.

Live checks and delivery cannot be atomic: a request/head can change after checking. Separate fresh events may each notify; this is bounded first-attempt delivery, not durable exactly-once delivery. The receiver's independent live verification remains necessary. Offline tests verify the integration contract, not actual GitHub event delivery, secret provisioning, Discord permissions, or bot acknowledgment.

Focused local check (stdlib only, mocked network, no secrets required):

```sh
python3 -I -m unittest discover -s .github/scripts -p 'test_review_request_discord.py' -v
```

The existing `.github/workflows/python-tests.yml` runs this focused contract on
both Python 3.12 and 3.14. All test network operations are mocked; no secret is
required. The workflow wiring assertions are text checks, not a GitHub Actions
runtime test.
