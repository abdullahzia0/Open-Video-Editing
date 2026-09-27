"""Run with: uv run python examples/edit_video.py INPUT OUTPUT (configured roots required)."""

import json
import shutil
import sys
from pathlib import Path

from ove.application.bootstrap import build_service
from ove.application.worker import Worker
from ove.domain.models import Intent, Lighting, PlanRequest


def main() -> None:
    source, destination = sys.argv[1:]
    if Path(destination).exists():
        raise SystemExit("Output already exists; choose a new path.")
    service = build_service()
    asset = service.import_asset(source)
    project = service.create_project("Lighting example", [asset["id"]])
    plan = service.create_plan(
        PlanRequest(
            project_id=project["id"],
            revision=1,
            asset_id=asset["id"],
            intent=Intent(request="Only improve lighting", allowed_effects={"lighting"}),
            operations=[Lighting(gamma=1.1)],
        )
    )
    job = service.submit_render(plan["id"], plan["hash"], f"example-{plan['id']}")
    # Drain preceding jobs too, while respecting the exclusive worker lock.
    while service.repository.job(job["id"])["state"] == "queued":
        Worker(service).run(once=True)
    finished = service.repository.job(job["id"])
    if finished["state"] != "succeeded":
        raise SystemExit(json.dumps(finished))
    artifact = service.artifact(finished["result"]["artifact_id"])
    with (
        Path(destination).open("xb") as output,
        Path(artifact["local_path"]).open("rb") as input_file,
    ):
        shutil.copyfileobj(input_file, output)
    print(json.dumps({"output": str(Path(destination).resolve()), "artifact": artifact}, indent=2))


if __name__ == "__main__":
    main()
