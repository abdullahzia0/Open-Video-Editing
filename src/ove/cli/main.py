"""Local CLI and MCP launchers. JSON output is suitable for scripted use."""

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ove.application.bootstrap import build_service
from ove.application.worker import Worker
from ove.domain.errors import OveError
from ove.domain.models import PlanRequest


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ove", description="Open Video Editing local tools")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Inspect available engines and providers")
    sub.add_parser("formats", help="List data-driven format presets")
    ingest = sub.add_parser("import", help="Stage a media file from an allowed local root")
    ingest.add_argument("source")
    project = sub.add_parser("project", help="Create a project")
    project.add_argument("name")
    project.add_argument("asset_ids", nargs="+")
    plan = sub.add_parser("plan", help="Validate and store a plan JSON document")
    plan.add_argument("file", type=Path)
    render = sub.add_parser("render", help="Queue a stored plan; run a worker to process it")
    render.add_argument("plan_id")
    render.add_argument("plan_hash")
    render.add_argument("--key", required=True)
    job = sub.add_parser("job", help="Read persisted job state")
    job.add_argument("job_id")
    cancel = sub.add_parser("cancel", help="Cancel a queued/running job")
    cancel.add_argument("job_id")
    worker = sub.add_parser("worker", help="Process durable jobs with an exclusive local worker")
    worker.add_argument("--once", action="store_true", help="Process at most one queued job")
    artifact = sub.add_parser(
        "artifact", help="Inspect a completed artifact or copy it to a new file"
    )
    artifact.add_argument("artifact_id")
    artifact.add_argument("--output", type=Path)
    server = sub.add_parser(
        "serve", help="Serve MCP; remote production authentication is not implemented"
    )
    server.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    return root


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        service = build_service()
        value: Any = None
        match args.command:
            case "doctor":
                value = service.capabilities()
            case "formats":
                value = service.presets.catalog()
            case "import":
                value = service.import_asset(args.source)
            case "project":
                value = service.create_project(args.name, args.asset_ids)
            case "plan":
                value = service.create_plan(PlanRequest.model_validate_json(args.file.read_text()))
            case "render":
                value = service.submit_render(args.plan_id, args.plan_hash, args.key)
            case "job":
                value = service.repository.job(args.job_id)
            case "cancel":
                value = service.repository.cancel(args.job_id)
            case "worker":
                Worker(service).run(once=args.once)
                return
            case "artifact":
                value = service.artifact(args.artifact_id)
                if args.output:
                    # Exclusive creation prevents accidental overwriting of user files.
                    with (
                        args.output.open("xb") as target,
                        Path(value["local_path"]).open("rb") as source,
                    ):
                        shutil.copyfileobj(source, target)
                    value["copied_to"] = str(args.output.resolve())
            case "serve":
                from ove.mcp.server import create_server

                create_server(service).run(transport=args.transport)
                return
        print(json.dumps(value, indent=2))
    except (OveError, ValidationError, OSError, ValueError) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, OveError)
            else {
                "code": "invalid_input_or_environment",
                "message": str(exc),
                "retryable": False,
            }
        )
        print(json.dumps({"error": error}), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
