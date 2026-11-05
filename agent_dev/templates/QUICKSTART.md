# Agent Dev workspace

Requires Python 3.11+, Git with a committed source repository, and an authenticated Codex or Claude CLI for agent execution. Run commands from the project root or any child directory.

1. Run `devroster doctor`. Optional `devroster doctor --check-auth` checks provider login status without generating content.
2. Review `.agent-dev/config/profile.json`: source repository paths are relative to the project root. Restrict `allowed_paths` and add real test commands to `verification.commands`/`required_gates`; the starter diff check only checks whitespace. After login, set the intended runner's `authenticated` flag to true. This is a local configuration declaration, not an authentication proof.
3. Apply the edited profile: `devroster project profile PROJECT-ID .agent-dev/config/profile.json`. Find PROJECT-ID in `workspace.json` or `devroster doctor`. Existing approved snapshots stay immutable; use the revised profile for newly approved tasks.
4. Replace the starter design and task definitions with your actual requirement:

```sh
devroster design refine PROJECT-ID DESIGN-001 .agent-dev/starter/design.md
devroster design approve DESIGN-001 1
devroster plan create PROJECT-ID PLAN-001 DESIGN-001 1 .agent-dev/starter/tasks.json
devroster plan approve PLAN-001
devroster refactor configure execute
devroster task run TASK-001
devroster task verify TASK-001
devroster task explain TASK-001
devroster dashboard
```

Approvals are explicit. Editing design requires a new approved version. Publish remains disabled and additionally requires repository remote/default-branch/CI policy configuration.

Add `/.agent-dev/` and your generated output directory to Git ignore rules. Keep secrets in your provider login store or environment, not in profile JSON.

```sh
devroster backup create /safe/project-backup.zip
devroster backup inspect /safe/project-backup.zip
devroster backup restore /safe/project-backup.zip /path/to/project-clone
```

Backup includes the database, managed configuration and task evidence. Source Git, worktrees, dependencies and credentials are excluded. Restore refuses existing managed files, disables execution/publish, invalidates old approvals and stops pending side effects. Clone your source separately; register the destination profile and create newly approved tasks rather than resuming old absolute-path snapshots.

## Presets / upgrades / limits
- Inspect `devroster preset list`, or initialize a new project with `devroster init --preset python|react|spring-boot`. Presets do not install project dev tools.
- Run `devroster upgrade` for preflight of an older portable workspace; `--apply` creates a backup first; `--recover` repairs an interrupted journal.
- Review profile `execution_limits` before approving new tasks; `devroster task usage TASK-ID` shows cumulative usage. Resume does not reset limits.
- `devroster task list --search TEXT --state blocked --project PROJECT --page 2` and dashboard filters/page controls find historical tasks.
- Base install has no runtime dependencies. OpenAI Responses legacy provider uses the `[openai]` extra.
