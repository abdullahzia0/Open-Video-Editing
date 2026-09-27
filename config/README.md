# Configuration ownership

Runtime configuration is validated by `ove.config.Settings`. The supported environment variables and defaults are in [`.env.example`](../.env.example). Unknown `.env` keys are rejected to catch misspellings; keep unrelated project settings in a separate file.

The executable format presets are packaged under [`src/ove/formats/presets`](../src/ove/formats/presets). Set `OVE_PRESET_DIR` to replace that registry with trusted JSON preset files. Preset files contain `id`, `version`, `status`, `description`, and `settings` matching the export schema. Conflicting fragments require explicit overrides.

No secrets or provider credentials are required by the baseline local pipeline. Canva credential configuration will be added only with a working and tested OAuth adapter.
