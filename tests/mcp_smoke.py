"""Real local stdio handshake + tool call, no model and no external service."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "logistics_lab.mcp_server"], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {"get_apparel_catalog", "plan_apparel_fulfilment", "audit_fulfilment_report"}
            cases = json.loads((ROOT / "data/scenarios.json").read_text(encoding="utf-8"))
            order = cases[0]["order"]
            result = await session.call_tool("plan_apparel_fulfilment", {"order": order})
            assert not result.isError
            plan = json.loads(result.content[0].text)
            assert plan["total_cost_usd"] == 249 and plan["transit_days"] == 6
            reviewed = await session.call_tool("audit_fulfilment_report", {"order": order, "report": plan})
            assert not reviewed.isError and json.loads(reviewed.content[0].text)["passed"]
            record = {"transport": "stdio", "tool_names": sorted(names), "plan_and_audit_passed": True, "llm_called": False}
            (ROOT / "evidence/mcp_smoke.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(json.dumps(record))


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=30))
