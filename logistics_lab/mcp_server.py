"""Read-only stdio MCP adapter. Requires mcp >= 1.29, < 2."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .planning import audit_report, load_world, plan_fulfilment

mcp = FastMCP("Synthetic Apparel Logistics Lab")


@mcp.tool()
def get_apparel_catalog() -> dict[str, Any]:
    """Read synthetic clothing variants and warehouse stock; never live inventory."""
    world = load_world()
    return {k: world[k] for k in ("dataset_id", "notice", "products", "warehouses")}


@mcp.tool()
def plan_apparel_fulfilment(order: dict[str, Any]) -> dict[str, Any]:
    """Plan one synthetic B2B order under quantity, budget, deadline and route constraints."""
    return plan_fulfilment(order)


@mcp.tool()
def audit_fulfilment_report(order: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Check structured report claims against versioned synthetic records, not prose style."""
    return audit_report(order, report)


if __name__ == "__main__":
    mcp.run(transport="stdio")
