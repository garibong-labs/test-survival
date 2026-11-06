## User language preference

The user uses English only in terminals and Codex CLI because Korean text looks unattractive there and they want to practice English. Use English for terminal/CLI conversations and prompts intended to be pasted into a terminal or Codex CLI. This preference does not require English in desktop chat; follow the user's language there.

## Commit attribution

For every commit you create, append this commit-message trailer exactly once:

```text
Co-authored-by: Codex <noreply@openai.com>
```

## PR comment and review attribution

This rule applies only when you are Codex (OpenAI). Other agents (for example Claude Code) must not use this marker; they follow their own attribution rules instead.

If you are Codex, start every PR comment, review submission body, inline review comment, and review-thread reply you post with the following marker as the very first line, followed by a blank line:

```markdown
> 🤖 **Codex (OpenAI)**
```

Include the marker exactly once so readers can immediately identify AI-generated content. Preserve it when editing your own comments or replies. This attribution rule does not authorize posting; follow the user's authorization for external communication.

## Internal working documents

This repository is public. `docs/` holds only user documentation for the CLI: how it works, how to configure it, and what its outputs mean.

Write every internal working document under `.local/docs/` instead. This includes implementation reviews and plans, verification and comparison records, evidence files (JSON, CSV, logs, profiles), handovers, and decision records. `.local/` is ignored by git, so these files stay on the local disk only.

Tracked files (README, `docs/`, code, comments, PR descriptions) must not link to or name files under `.local/`. When a user document needs a verified fact, state the fact directly in that document.

.local/docs/ holds documents and small evidence files only. Put clones, experiment outputs, build products, and other large scratch material under .local/work/ (also ignored by git).
