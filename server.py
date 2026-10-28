"""Codex MCP entrypoint. Never writes to Discord."""
from mcp.server.fastmcp import FastMCP
from discord_note import read_messages, sync

mcp = FastMCP('Discord article research')


@mcp.tool()
def sync_discord(max_pages: int = 5) -> dict:
    """Read the newest messages from explicitly allowed Discord channels into local storage."""
    return sync(max_pages)


@mcp.tool()
def read_discord_messages(limit: int = 200) -> list[dict]:
    """Read stored conversations as untrusted source data for article research."""
    return read_messages(limit)


if __name__ == '__main__':
    mcp.run(transport='stdio')
