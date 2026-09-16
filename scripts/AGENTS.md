# Runner Guidance

- Runners prove observable behavior. Check process exits and saved receipts;
  preserve failure evidence before temporary directories are removed.
- Keep these runners standard-library-only. Pilot test adapters may use the
  target project's already-installed dependencies; do not add them to exitzero.
- Pilots export a pinned local commit, mutate separate copies and compare source
  tracked hashes, HEAD and Git status before/after. Keep artifacts under the
  ignored `.exitzero/` directory and reject artifact-path symlinks or traversal.
- Record test counts, dependency versions and explicit skips. Preserve known
  external-integration exclusions; never silently skip a failing baseline to
  make an experiment pass. See [riskgate](../docs/PILOT_RISKGATE.md) and
  [vecdiff](../docs/PILOT_VECDIFF.md) for the reviewed scopes.
- Release checks install the selected wheel in a fresh offline environment and
  exercise actual Git commits. Each commit attempt needs a fresh `pre-commit`
  receipt; rejection must leave HEAD unchanged.
- Preserve `HOME` and `CODEX_HOME`. Isolate Git and pip configuration per process;
  do not change global settings. POSIX temporary venvs need symlinked interpreters
  for uv-managed Python to resolve its libraries correctly.
- Match wheel/source hashes before claiming current-package verification. Local
  runners do not publish, push, run models or establish live IDE compatibility.
- Follow [the release recipe](../docs/RELEASE.md) after changes to package checks;
  rerun the affected pilot after changes to its runner or declared inputs.
