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
from ove.domain.models import MediaJobRequest, PlanRequest


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
    jobs = sub.add_parser("jobs", help="List queued and historical jobs")
    jobs.add_argument("--state", default=None)
    jobs.add_argument("--limit", type=int, default=20)
    cancel = sub.add_parser("cancel", help="Cancel a queued/running job")
    cancel.add_argument("job_id")
    retry = sub.add_parser("retry", help="Re-queue a failed or cancelled job")
    retry.add_argument("job_id")
    retry.add_argument("--key", required=True)
    inspect = sub.add_parser("info", help="Probe an asset id or an allowlisted local path")
    inspect.add_argument("source")
    inspect.add_argument("--path", action="store_true", help="Treat source as a filesystem path")
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


def _plan_from_file(path: Path) -> PlanRequest | MediaJobRequest:
    """Accept either a single-source render plan or a multi-input job request."""
    document = json.loads(path.read_text())
    if "job" in document:
        return MediaJobRequest.model_validate(document)
    return PlanRequest.model_validate(document)


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
                value = service.asset_info(service.import_asset(args.source)["id"])
            case "project":
                value = service.create_project(args.name, args.asset_ids)
            case "plan":
                request = _plan_from_file(args.file)
                value = (
                    service.create_plan(request)
                    if isinstance(request, PlanRequest)
                    else service.create_media_plan(request)
                )
            case "render":
                value = service.submit_plan(args.plan_id, args.plan_hash, args.key)
            case "job":
                value = service.job_status(args.job_id)
            case "jobs":
                value = service.list_jobs(args.state, args.limit)
            case "cancel":
                value = service.cancel_job(args.job_id)
            case "retry":
                value = service.retry(args.job_id, args.key)
            case "info":
                value = service.media_info(args.source, args.path)
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
        print(json.dumps(value, indent=2, default=str))
    except (OveError, ValidationError, OSError, ValueError) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, OveError)
            else {
                "code": "INVALID_INPUT",
                "message": str(exc),
                "retryable": False,
            }
        )
        print(json.dumps({"error": error}), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
