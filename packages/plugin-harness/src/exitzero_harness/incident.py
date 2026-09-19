"""Generate an unfinished regression kit and wire it into the existing policy."""
import argparse
import json
import os
import re
import shlex

from exitzero.api import Context
from exitzero.services import (parse_policy, safe_path, select_files,
                              updated_agents, validate_relative, write_atomic)

_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_TEST = '''from unittest import TestCase


class IncidentTests(TestCase):
    def test_reported_regression(self):
        self.fail("Replace with a regression assertion against production code.")

    def test_expected_behavior_is_preserved(self):
        self.fail("Replace with an assertion for a neighboring valid case.")
'''


def incident_kit(context: Context, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="exitzero plugin incident-kit")
    parser.add_argument("--id", required=True, help="Lowercase incident name (letters, digits and hyphens)")
    parser.add_argument("--description", required=True, help="Short incident description; stored locally, not executed")
    parser.add_argument("--path", action="append", default=[], help="Input glob; repeat for code and fixtures (default: **/*.py)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 2
    try:
        if not _ID.fullmatch(args.id):
            raise ValueError("Use a lowercase incident id of at most 64 letters, digits or hyphens.")
        if not args.description.strip() or len(args.description) > 1000:
            raise ValueError("Provide an incident description of 1 to 1000 characters.")
        if not {"verify", "exitzero_verify"} & set(context.policy["plugins"]):
            raise ValueError("Enable the bundled verify plugin before generating an incident kit.")
        paths = args.path or ["**/*.py"]
        for pattern in paths:
            validate_relative(pattern)
        select_files(context.root, paths)
        relative = f"tests/incidents/{args.id}"
        directory = safe_path(context.root, relative)
        if os.path.lexists(directory):
            raise ValueError("The incident directory already exists; existing files are never overwritten.")
        # Resolve every destination before any write, including non-regular files.
        policy_path = safe_path(context.root, context.policy_path.relative_to(context.root).as_posix())
        agents_path = safe_path(context.root, "AGENTS.md")
        if os.path.lexists(agents_path) and not agents_path.is_file():
            raise ValueError("AGENTS.md must be a regular file.")
        test_path = f"{relative}/test_incident.py"
        quality_id, regression_id = f"incident.{args.id}.quality", f"incident.{args.id}.regression"
        fragment = (
            f'\n[[checks]]\nid = "{quality_id}"\nkind = "python.test-quality"\n'
            f'paths = {json.dumps([test_path])}\n'
            f'\n[[checks]]\nid = "{regression_id}"\nkind = "command"\nreuse = false\n'
            f'paths = {json.dumps(list(dict.fromkeys([*paths, test_path])), ensure_ascii=False)}\n'
            '[checks.options]\n'
            f'argv = {json.dumps(["{python}", "-B", "-m", "unittest", "discover", "-s", relative, "-p", "test_incident.py"])}\n'
            'timeout = 60\n'
            f'\n[[requirements]]\nid = "incident.{args.id}"\n'
            'description = "Regression and neighboring behavior are covered by the incident tests."\n'
            f'checks = {json.dumps([quality_id, regression_id])}\n'
        )
        policy_text = policy_path.read_text(encoding="utf-8").rstrip() + "\n" + fragment
        policy = parse_policy(policy_text)
        agents = updated_agents(agents_path.read_text(encoding="utf-8") if agents_path.is_file() else "", policy)
        prefix = shlex.join(["exitzero", "--policy", policy_path.relative_to(context.root).as_posix()])
        readme = f'''# Incident regression kit

`incident.json` holds the incident description as data. The generated tests
deliberately fail until you replace both placeholders with real assertions.

1. Import the production behavior in `test_incident.py` and add the reported
   regression plus a neighboring valid case. Do not replace the placeholders
   with skip markers or constant assertions.
2. On the buggy implementation, run `{prefix} check --format json`; confirm
   `{regression_id}` fails for the reported behavior, and keep its receipt.
3. Repair production code and run the same command; both tests must pass.
   Keep the new receipt. A green placeholder edit is not evidence of a repair.

The new command and test-quality check are already in the repository policy,
with a requirement mapping. Existing exitzero hooks and CI use that policy.
For a new client run `{prefix} doctor`, then `{prefix} hooks install --adapter NAME`
for the desired supported adapter. Configure required CI as described in
https://github.com/ictechgy/exitzero/blob/main/docs/HOOKS.md#required-ci-setup.
No hooks are installed by this generator.

The command fingerprints the supplied input paths and always runs afresh.
Include non-Python fixtures and configuration with repeated `--path` arguments
when generating a kit. Review the policy inputs as the regression evolves.
The generator does not infer correct assertions or prove semantic test quality.
'''
        outputs = {
            f"{relative}/test_incident.py": _TEST,
            f"{relative}/incident.json": json.dumps({"schema_version": 1, "id": args.id,
                                                     "description": args.description}, ensure_ascii=False, indent=2) + "\n",
            f"{relative}/README.md": readme,
        }
        destinations = [(safe_path(context.root, name), text) for name, text in outputs.items()]
        # Publish tests first: a partial write never adds a passing placeholder.
        directory.mkdir(parents=True, exist_ok=False)
        for path, text in destinations:
            write_atomic(path, text)
        write_atomic(policy_path, policy_text)
        write_atomic(agents_path, agents)
    except (OSError, ValueError):
        print("incident-kit: unable to generate; check id, paths, verify plugin, policy and AGENTS markers. Existing kits are not overwritten; inspect any partial output after an I/O error.")
        return 2
    print(f"Created {relative}; checks and requirement mapping added. Run exitzero check: the unfinished tests must fail.")
    return 0
