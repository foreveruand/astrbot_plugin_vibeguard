# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] - 2026-10-04

### Added
- Tool call argument restoration (`on_using_llm_tool`): placeholders passed by the LLM as tool parameters are automatically restored to original values before execution. Controlled by `replace_in_tool_args` (default `true`).
- Tool result masking (`on_llm_tool_respond`): sensitive data found in tool execution results is masked before further processing. Controlled by `replace_in_tool_results` (default `true`). Note: AstrBot appends tool results to the agent loop context before dispatching this hook, so same-turn masking is best-effort; cross-turn protection is provided via history context masking.
- Protected path guard: LLM tool access to the plugin config file and plugin source directory is blocked at the tool call layer (file read/write/edit, grep, shell, shell session, python tools). Extra paths can be configured via `protected_paths`.
- Hardened guard notice: the default `guard_notice_text` now explicitly forbids guessing/decoding placeholders, accessing VibeGuard configs or mappings, and using tools to inspect plugin directories.
- Optional system prompt injection via `inject_to_system_prompt` (default `false`).
- `on_agent_done` now also restores placeholders in `tool` role messages before history persistence.

## [1.0.1] - 2026-10-02

### Fixed
- Fix placeholders leaking to user in streaming delivery (e.g. weixin_oc): streaming deltas are sent via `send_streaming` before `on_llm_response`, so collect-then-restore them in a per-turn wrapper to handle placeholders split across chunks.
- Add `on_decorating_result` safety net for non-streaming outbound results.
- Fix reasoning copy in event extra staying masked and `_completion_text` handling when `result_chain` exists.

## [1.0.0] - 2026-10-02

### Added
- Sensitive text & regex pattern detection and replacement prior to sending LLM requests.
- Automatic restoration of placeholders in LLM responses and reasoning text.
- TokenCache with sliding TTL window to support LLM prompt cache / prefix cache hits.
- Seamless preservation of local unmasked chat history in database records.
- Configurable settings via `_conf_schema.json` for AstrBot WebUI.
- Guard notice injection to inform the LLM about placeholder identifiers without polluting chat history.
