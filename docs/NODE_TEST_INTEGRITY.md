# Node test integrity

`node.test-integrity` compares a bounded lexical inventory of JS/TS tests with a
local Git commit. It flags removed literal-named test/suite declarations and new
suppression or focus markers. It does not execute JavaScript or require Node,
npm, a parser package, or any new Python dependency. Keep the project's real
test, lint and type-check commands alongside it.

## Enable it

For a new Node policy, select the trusted baseline explicitly:

```sh
exitzero init --profile node --test-integrity-base origin/main
exitzero check --format json
```

Initialization does not resolve or fetch the ref. The check requires that commit
to exist locally. The generated paths cover `test.js` and its TS/module variants,
`*.test.*`, `*.spec.*`, and files under `test/`, `tests/` or `__tests__/`, including
nested packages. Supported extensions are `.js`, `.mjs`, `.cjs`, `.ts`, `.mts`
and `.cts`. Review these paths for your repository; JSX/TSX is not covered.

For an existing policy, add the check without rerunning profile generation:

```toml
[[checks]]
id = "test-integrity"
kind = "node.test-integrity"
paths = ["test.js", "tests/**/*.js", "tests/**/*.ts"]
reuse = false
enforcement = "block"

[checks.options]
base = "origin/main"
functions = ["test", "it", "describe", "suite"]
```

Then run `exitzero init --sync` and `exitzero lint-config`. `functions` lists the
simple identifier names that declare tests or suites; defaults are shown above.
Add explicitly bound aliases used in your project. This check does not resolve
imports, local shadowing or conditional alias assignments. `describe` and `suite`
are inventoried as suites; other configured names are inventoried as tests.

`reuse = false` is mandatory and validated even during config lint. A moving Git
ref is not a hashed file input. `enforcement = "warn"` can support adoption while
retaining failed-check evidence. Missing Git/commits, invalid options and a ref
that moves during the check remain operational errors (exit 2).

Use an immutable trusted commit or a protected base-branch ref in merge CI.
The default `base = "HEAD"` is useful for local uncommitted edits, but comparing
a committed PR against its own HEAD cannot detect the changes it already contains.
CI must make the independent baseline available; the check never fetches missing
objects and disables Git replacement objects. `check --diff` narrows this check
too, so a full merge gate is preferable when comparing moves across files.

## What is compared

- Literal string titles on direct configured calls, including simple property
  chains and literal bracket members. Quote changes and common string escapes
  preserve identity. Duplicate titles are counted separately.
- `.skip`, `.todo`, `.only`, `.failing`, `.fails`, `.skipIf` and `.runIf` references.
  The last two are conservative configuration changes: conditions are not evaluated.
  Existing markers can remain unchanged; moving a marker to a different title is
  reported even if the total marker count stays equal.
- Literal second-argument Node options such as `{skip: true}` or `{only: true}`.
  An explicit `false` is allowed. Dynamic option expressions are treated as
  potentially suppressing; spread objects or dynamic keys cannot be analyzed.
- `skip`/`todo` references on the simple first parameter of a directly recognized
  callback, and `this.skip` in function callbacks. External callbacks and complex
  parameter/type signatures are not resolved.
- Selected files removed from the worktree, including files deleted under
  `check --diff`. Byte-identical renames inside scope pass. Splitting or moving
  recognized declarations inside the complete selected scope also passes if the
  global inventory is retained. Moving outside the scope fails.

Ordinary `.serial`/`.concurrent` calls and literal-titled `.each(...)`/`.for(...)`
calls are supported. Framework hook registrations such as `test.beforeEach(...)`
are excluded. Supported forms follow the official
[Node test API](https://nodejs.org/api/test.html),
[Jest API](https://jestjs.io/docs/api) and
[AVA test documentation](https://github.com/avajs/ava/blob/main/docs/01-writing-tests.md).
These references do not imply full framework syntax support.

## Limits to keep visible

This is a lexical comparison, not a JS/TS parser, binding resolver or semantic
test-quality proof. Comments, string contents, regex literals and template text
do not count as declarations. Dynamic titles, malformed/unbalanced source,
unsupported declarations, JSX and ambiguous slash syntax after a closing brace
or contextual keyword (`of`, `await`, `yield`)
produce findings instead of an assumed pass. Analysis is bounded to 2 MiB per
file, 200,000 tokens and 128 delimiter levels.

It does not count assertions, interpret branches, expand generated tests, follow
external callbacks or test declarations inside template interpolations, or prove
that a retained name still tests the same behavior. Replacing a body with a vacuous
assertion while retaining its title can pass. Names repeated across suites/files
share the inventory; removing one and adding another with the same title can
mask the removal. Pure title changes are conservatively reported as removals and
need review. Custom aliases must be configured; test discovery remains the
upstream runner's responsibility.

The [p-limit pilot](PILOT_NODE.md) retains the original command-only miss and the
follow-up that enables this check. Its real test-deletion case demonstrates the
bounded additional protection: AVA, XO and tsd pass, while test integrity fails.
