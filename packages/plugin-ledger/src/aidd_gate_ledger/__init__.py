"""Reserved v1.3 extension point; core already writes version 1 receipts."""

API_VERSION = 1


def register(registry):
    registry.add_command("ledger-publish", unavailable)


def unavailable(context, argv):
    print("PR receipt publishing and rollback hints are planned for v1.3.")
    return 2
