# Declared Python connections

`python.connections` catches a specific kind of superficial pass: code exists,
but a test calls a local substitute, or the application never registers the
declared implementation. It checks an explicit import and use relationship in
Python ASTs without importing or executing the source.

```toml
[[checks]]
id = "billing-connections"
kind = "python.connections"
paths = ["src/**/*.py", "tests/**/*.py"]

[checks.options]
roots = ["src"]

[[checks.options.connections]]
source = "tests/test_billing.py"
target = "src/billing.py"
symbol = "total"
within = "BillingTests.test_total"
usage = "call"

[[checks.options.connections]]
source = "src/app.py"
target = "src/billing.py"
symbol = "total"
within = "<module>"
usage = "argument"
consumer = "router.register"
```

The first connection accepts a named or module import of `billing.total` used
as a call in the declared test method, including ordinary aliases. Defining a
different `total` inside the test does not satisfy it. The second requires
passing that imported symbol to `router.register(...)` at module scope.
Deleting the registration or leaving only a string containing its text fails.

Each target must contain one unconditional module-level function or class
declaration. Ambiguous module roots, wildcard imports, conditional imports,
direct name/attribute rebindings are conservatively rejected. A scope is a
function, method, nested function, or `<module>`. Uses inside branches, loop bodies,
exception handlers, `with` bodies, short-circuit operands, assert messages or
comprehensions do not satisfy the declared connection. Choose a straight-line
site or a project-specific command check for a more dynamic framework.

## Evidence and limits

The receipt fingerprints both declared files and Python files under the module
roots, so additions that change name resolution invalidate reuse. Files are
limited to 2 MiB for AST analysis. Missing inputs or unsafe paths are errors;
an analyzed but absent/ambiguous connection is a check violation. Normal
`enforcement = "warn"` is available for advisory rollout.

`--diff` rechecks the full original check scope when a declared dependency
changes, even if the consumer file is unchanged. This also applies to the
existing Python import check. Untracked, unignored files participate in changed
input discovery. Full merge CI should still run without `--diff`.

This establishes a declared static relationship, not runtime reachability,
test assertion quality, framework semantics, or correctness of the target.
Run actual behavior/contract tests alongside it. For example, a test should
assert the exact result or error of `total(...)`; the connection check only
establishes that the declared test refers to the production symbol. Dynamic
imports, reflection or helper-driven monkeypatching, dependency injection,
decorators that replace implementations, and arbitrary control flow need
executable project checks. Preceding exceptions/returns and interpreter options
can also prevent a syntactically connected call from executing. Native hook/gateway
events are needed to observe intermediate tool actions; this check does not
invent an execution trace from a final diff.
