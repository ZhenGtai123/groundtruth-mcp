"""Launch the server as a real subprocess and talk to it over stdio.

    python scripts/mcp_smoke.py [path/to/groundtruth.toml]

Registering tools in-process proves the wiring; this proves the thing an agent
actually does — spawn the command from `.mcp.json`, initialize, list tools,
call one, read the text back. Run it after wiring up your own project, before
blaming the agent for not seeing your tools.

Needs the MCP SDK: `pip install groundtruth-mcp[mcp]`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "examples" / "checkout-flow" / "groundtruth.toml"


async def main(config: Path) -> int:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        print("the MCP SDK is not installed: pip install groundtruth-mcp[mcp]", file=sys.stderr)
        return 2

    # Inherit the interpreter's path so this works from a checkout as well as
    # from an installed package.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(ROOT / "src"), env.get("PYTHONPATH", "")])
    )
    env["PYTHONIOENCODING"] = "utf-8"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "groundtruth_mcp.cli", "--config", str(config), "serve"],
        env=env,
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        listed = await session.list_tools()
        names = [tool.name for tool in listed.tools]
        print(f"tools: {', '.join(names)}")

        if "lint" not in names:
            print("no lint tool registered", file=sys.stderr)
            return 1

        result = await session.call_tool("lint", {"target": "broken_checkout"})
        text = "\n".join(
            block.text for block in result.content if getattr(block, "type", "") == "text"
        )
        print("\n--- lint(broken_checkout) ---")
        print(text)

        result = await session.call_tool("simulate", {"target": "standard_checkout", "runs": 200})
        text = "\n".join(
            block.text for block in result.content if getattr(block, "type", "") == "text"
        )
        print("\n--- simulate(standard_checkout, runs=200) ---")
        print(text)

    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    raise SystemExit(asyncio.run(main(target)))
