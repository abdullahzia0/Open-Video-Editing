"""Generate public contract files from executable models and tool registrations."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from ove.application.bootstrap import build_service
from ove.config import Settings
from ove.domain.models import PlanRequest
from ove.mcp.server import create_server

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        service = build_service(Settings(_env_file=None, data_dir=Path(directory)))
        tools = await create_server(service).list_tools()
        documents = {
            "plan.schema.json": PlanRequest.model_json_schema(),
            "mcp-tools.json": [tool.model_dump(mode="json", exclude_none=True) for tool in tools],
        }
    target = ROOT / "schemas"
    target.mkdir(exist_ok=True)
    for name, document in documents.items():
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        path = target / name
        if args.check:
            if not path.exists() or path.read_text() != text:
                raise SystemExit(f"Stale schema: {path}; run scripts/export_schemas.py")
        else:
            path.write_text(text)


if __name__ == "__main__":
    asyncio.run(main())
