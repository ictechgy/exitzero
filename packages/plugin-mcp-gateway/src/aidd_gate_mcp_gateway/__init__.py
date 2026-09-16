"""Reserved v1.2 extension point. No proxy or tool authorization is implemented."""

API_VERSION = 1


def register(registry):
    registry.add_command("mcp-gateway", unavailable)


def unavailable(context, argv):
    print("MCP gateway is planned for v1.2; no gateway is running.")
    return 2
