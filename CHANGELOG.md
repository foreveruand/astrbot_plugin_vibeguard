# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-10-02

### Added
- Sensitive text & regex pattern detection and replacement prior to sending LLM requests.
- Automatic restoration of placeholders in LLM responses and reasoning text.
- TokenCache with sliding TTL window to support LLM prompt cache / prefix cache hits.
- Seamless preservation of local unmasked chat history in database records.
- Configurable settings via `_conf_schema.json` for AstrBot WebUI.
