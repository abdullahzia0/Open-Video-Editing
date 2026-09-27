"""Data-driven preset composition; no platform-specific tool branches."""

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from ove.domain.errors import OveError
from ove.domain.models import ExportSpec


class PresetRegistry:
    def __init__(self, directory: Path | None = None):
        root = directory if directory else Path(str(files("ove.formats") / "presets"))
        self.presets: dict[str, dict[str, Any]] = {}
        for path in sorted(root.glob("*.json")):
            preset = json.loads(path.read_text())
            if preset["id"] in self.presets:
                raise OveError("invalid_preset", "Duplicate preset identifier.")
            self.presets[preset["id"]] = preset
        if not self.presets:
            raise OveError("missing_presets", "No preset configuration files were found.")

    def catalog(self) -> list[dict[str, Any]]:
        return list(self.presets.values())

    def resolve(self, ids: list[str], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        overrides = overrides or {}
        fields: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        for identifier in ids:
            if identifier not in self.presets:
                raise OveError("unknown_preset", f"Unknown preset: {identifier}")
            for key, value in self.presets[identifier]["settings"].items():
                if key in fields and fields[key] != value and key not in overrides:
                    raise OveError(
                        "preset_conflict",
                        f"Presets disagree on {key}.",
                        "Supply an explicit override or choose compatible fragments.",
                    )
                fields[key], provenance[key] = value, identifier
        for key, value in overrides.items():
            fields[key], provenance[key] = value, "explicit_override"
        spec = ExportSpec.model_validate(fields)
        return {
            "export": spec.model_dump(mode="json"),
            "provenance": provenance,
            "warnings": ["Platform labels are advisory profiles, not verified upload limits."],
        }
