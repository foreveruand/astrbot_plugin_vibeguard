import asyncio
import copy
import fnmatch

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

from .replacer import SensitiveReplacer
from .token_cache import TokenCache


class VibeGuardPlugin(Star):
    """VibeGuard: An AstrBot plugin for protecting sensitive user data sent to LLMs.

    Features:
    - Replaces sensitive text and patterns with safe placeholders before reaching the LLM.
    - Restores placeholders back to original values in the LLM response before presentation.
    - Preserves unmasked original conversation history locally in AstrBot's database.
    - Cache reuse with sliding TTL: identical sensitive values map to the same placeholder
      across turns within the TTL window to leverage LLM prefix caching.
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """Initialize VibeGuardPlugin.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration dictionary.
        """
        super().__init__(context, config)
        self.config: dict = config or {}
        self.cache: TokenCache | None = None
        self.replacer: SensitiveReplacer | None = None
        self._cleanup_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        """Initialize cache, replacer, and periodic cleanup task."""
        ttl = self.config.get("token_ttl_seconds", 3600)
        prefix = self.config.get("placeholder_prefix", "__VG_")
        suffix = self.config.get("placeholder_suffix", "__")

        self.cache = TokenCache(ttl_seconds=ttl, prefix=prefix, suffix=suffix)
        self.replacer = SensitiveReplacer(
            sensitive_words=self.config.get("sensitive_words", []),
            sensitive_patterns=self.config.get("sensitive_patterns", []),
            cache=self.cache,
        )

        async def _periodic_cleanup() -> None:
            while True:
                try:
                    await asyncio.sleep(600)
                    if self.cache:
                        await self.cache.cleanup_expired()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.warning(f"Error during cache cleanup: {e}")

        self._cleanup_task = asyncio.create_task(_periodic_cleanup())
        self.logger.info("VibeGuard initialized successfully.")

    async def terminate(self) -> None:
        """Cancel background tasks and clear resources on plugin shutdown."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        if self.cache:
            self.cache.clear()
        self.logger.info("VibeGuard terminated.")

    @filter.on_llm_request(priority=100)
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Intercept LLM request, replace sensitive tokens with placeholders.

        Args:
            event: AstrMessageEvent context.
            req: Outbound LLM ProviderRequest.
        """
        if not self.config.get("enabled", True) or not self.replacer:
            return

        # 1. Back up original prompt for history restoration
        event.set_extra("_vg_original_prompt", req.prompt)

        # 2. Replace sensitive words in current user prompt
        replaced_any = False
        if req.prompt:
            original_p = req.prompt
            req.prompt = await self.replacer.replace_text(req.prompt)
            if req.prompt != original_p:
                replaced_any = True

        # 3. Replace in extra user content parts if present
        if req.extra_user_content_parts:
            for part in req.extra_user_content_parts:
                if (
                    hasattr(part, "type")
                    and part.type == "text"
                    and hasattr(part, "text")
                ):
                    orig_text = part.text
                    part.text = await self.replacer.replace_text(part.text)
                    if part.text != orig_text:
                        replaced_any = True
                elif isinstance(part, dict) and part.get("type") == "text":
                    orig_text = part.get("text", "")
                    part["text"] = await self.replacer.replace_text(orig_text)
                    if part["text"] != orig_text:
                        replaced_any = True

        # 4. Replace in prior conversation contexts sent to LLM
        if self.config.get("replace_in_contexts", True) and req.contexts:
            # Deepcopy to keep a reference to clean historical contexts
            event.set_extra("_vg_original_contexts", copy.deepcopy(req.contexts))
            for ctx in req.contexts:
                content = ctx.get("content")
                if isinstance(content, str):
                    replaced_content = await self.replacer.replace_text(content)
                    if replaced_content != content:
                        replaced_any = True
                    ctx["content"] = replaced_content
                elif isinstance(content, list):
                    for sub in content:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            sub_text = sub.get("text", "")
                            replaced_sub = await self.replacer.replace_text(sub_text)
                            if replaced_sub != sub_text:
                                replaced_any = True
                            sub["text"] = replaced_sub

        # 5. Inject guard explanation notice if any placeholder was substituted
        if replaced_any and self.config.get("inject_guard_notice", True):
            notice_text = self.config.get(
                "guard_notice_text",
                "[Security Notice: Strings matching __VG_*__ are opaque security "
                "redaction placeholders.]",
            )
            if notice_text:
                # Append as a temporary extra content part marked with mark_as_temp()
                # so it is transmitted to the LLM but stripped before database history saving
                req.extra_user_content_parts.append(
                    TextPart(text=f"\n\n{notice_text}").mark_as_temp()
                )
                # Optionally mirror the notice into the system prompt, where
                # instruction weight is usually higher than in user content.
                if self.config.get("inject_to_system_prompt", False):
                    try:
                        req.system_prompt = (
                            f"{req.system_prompt or ''}\n\n{notice_text}"
                        )
                    except Exception as e:
                        self.logger.warning(
                            f"VibeGuard system prompt injection failed: {e}"
                        )

        # 6. Wrap streaming delivery so placeholders split across chunks
        # are still restored before reaching the user.
        # Background: on_llm_response only runs once at agent completion,
        # while streaming deltas (MessageChain chunks) are sent to the
        # platform via event.send_streaming BEFORE that hook. Without this
        # wrapper, platforms using streaming (e.g. weixin_oc with default
        # realtime_segmenting, which aggregates deltas) would show raw
        # placeholders to the user, even though DB history (fixed in
        # on_agent_done) looks correct.
        if replaced_any:
            self._wrap_send_streaming(event)

    def _wrap_send_streaming(self, event: AstrMessageEvent) -> None:
        """Install a per-turn send_streaming wrapper that restores placeholders.

        Args:
            event: Current turn message event.
        """
        if not self.replacer:
            return
        if event.get_extra("_vg_stream_wrapped"):
            return
        event.set_extra("_vg_stream_wrapped", True)

        orig_send_streaming = event.send_streaming
        replacer = self.replacer
        logger = self.logger

        async def _restored_generator(generator):
            texts: list[str] = []
            reasoning_texts: list[str] = []
            passthrough: list = []
            try:
                async for chain in generator:
                    if chain is None:
                        continue
                    ctype = getattr(chain, "type", None)
                    chain_list = getattr(chain, "chain", None) or []
                    if ctype == "break" or not chain_list:
                        continue
                    if ctype == "reasoning":
                        for comp in chain_list:
                            if isinstance(comp, Plain):
                                reasoning_texts.append(comp.text or "")
                            else:
                                passthrough.append(comp)
                        continue
                    has_plain = any(isinstance(c, Plain) for c in chain_list)
                    if not has_plain:
                        passthrough.append(chain)
                        continue
                    for comp in chain_list:
                        if isinstance(comp, Plain):
                            texts.append(comp.text or "")
                        else:
                            passthrough.append(comp)
            except Exception as e:
                logger.warning(f"VibeGuard streaming collect failed: {e}")

            # Restore after full collection so placeholders split
            # across chunk boundaries are joined before replacement.
            if reasoning_texts:
                try:
                    restored_r = await replacer.restore_text("".join(reasoning_texts))
                except Exception as e:
                    logger.warning(f"VibeGuard reasoning restore failed: {e}")
                    restored_r = "".join(reasoning_texts)
                if restored_r:
                    yield MessageChain(type="reasoning").message(restored_r)
            for item in passthrough:
                if hasattr(item, "chain"):
                    yield item
                else:
                    yield MessageChain(chain=[item])
            if texts:
                full = "".join(texts)
                try:
                    restored = await replacer.restore_text(full)
                except Exception as e:
                    logger.warning(f"VibeGuard streaming restore failed: {e}")
                    restored = full
                if restored:
                    yield MessageChain().message(restored)

        async def _vg_send_streaming(generator, use_fallback: bool = False):
            return await orig_send_streaming(
                _restored_generator(generator), use_fallback
            )

        event.send_streaming = _vg_send_streaming  # type: ignore[method-assign]

    @filter.on_using_llm_tool(priority=100)
    async def on_using_llm_tool(
        self, event: AstrMessageEvent, tool, tool_args: dict | None
    ) -> None:
        """Intercept tool calls: block protected paths and restore placeholders.

        AstrBot passes ``tool_args`` (valid_params) by reference, so in-place
        mutation takes effect on the actual tool execution. Exceptions raised
        here are swallowed by the agent runner, so blocking is implemented by
        rewriting arguments into harmless values instead of raising.

        Args:
            event: AstrMessageEvent context.
            tool: The FunctionTool about to be executed.
            tool_args: Mutable tool arguments dict.
        """
        if not self.config.get("enabled", True) or not self.replacer:
            return
        if not tool_args:
            return

        tool_name = getattr(tool, "name", "") or ""
        blocked = self._guard_tool_path_access(tool_name, tool_args)
        if blocked:
            return

        if self.config.get("replace_in_tool_args", True):
            try:
                await self._restore_tool_args(tool_args)
            except Exception as e:
                self.logger.warning(f"VibeGuard tool args restore failed: {e}")

    @filter.on_llm_tool_respond(priority=100)
    async def on_llm_tool_respond(
        self, event: AstrMessageEvent, tool, tool_args: dict | None, tool_result
    ) -> None:
        """Mask sensitive data found in tool execution results.

        Note: AstrBot appends the tool result text to the agent loop context
        BEFORE dispatching this hook, so in-loop masking via this hook is
        best-effort only. It still protects downstream consumers of the
        result object, and persisted history is handled in on_agent_done.
        Cross-turn protection comes from replace_in_contexts masking when
        tool outputs re-enter the LLM as history contexts.

        Args:
            event: AstrMessageEvent context.
            tool: The FunctionTool that was executed.
            tool_args: Original tool arguments.
            tool_result: CallToolResult (or None) returned by the tool.
        """
        if not self.config.get("enabled", True) or not self.replacer:
            return
        if not self.config.get("replace_in_tool_results", True):
            return
        if tool_result is None:
            return
        try:
            content = getattr(tool_result, "content", None)
            if isinstance(content, list):
                for item in content:
                    await self._mask_result_item(item)
            elif isinstance(tool_result, str):
                self.logger.debug("VibeGuard got str tool result; nothing to mutate.")
        except Exception as e:
            self.logger.warning(f"VibeGuard tool result masking failed: {e}")

    async def _mask_result_item(self, item) -> None:
        """Mask sensitive text inside a single tool result content item.

        Args:
            item: A result content item (TextContent-like or EmbeddedResource-like).
        """
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            masked = await self.replacer.replace_text(text)
            if masked != text:
                item.text = masked
            return
        resource = getattr(item, "resource", None)
        resource_text = getattr(resource, "text", None)
        if isinstance(resource_text, str) and resource_text:
            masked = await self.replacer.replace_text(resource_text)
            if masked != resource_text:
                resource.text = masked

    async def _restore_tool_args(self, obj) -> None:
        """Recursively restore placeholders in tool args to original values.

        Mutates dicts and lists in place so the tool executes with real values.

        Args:
            obj: Tool args dict, list, or nested structure to restore.
        """
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str):
                    restored = await self.replacer.restore_text(value)
                    if restored != value:
                        obj[key] = restored
                elif isinstance(value, (dict, list)):
                    await self._restore_tool_args(value)
        elif isinstance(obj, list):
            for index, item in enumerate(obj):
                if isinstance(item, str):
                    restored = await self.replacer.restore_text(item)
                    if restored != item:
                        obj[index] = restored
                elif isinstance(item, (dict, list)):
                    await self._restore_tool_args(item)

    def _is_protected_target(self, value: str) -> bool:
        """Check whether a path or command string targets protected locations.

        Always protects the plugin config file and plugin source directory
        (except skills subdirectories). User-configured protected_paths
        entries are matched as substrings or glob patterns.

        Args:
            value: Path or command string to inspect.

        Returns:
            True if the value targets a protected location.
        """
        if not value or not isinstance(value, str):
            return False
        lowered = value.lower()
        if "astrbot_plugin_vibeguard_config" in lowered:
            return True
        if "astrbot_plugin_vibeguard" in lowered and "skills" not in lowered:
            return True
        for entry in self.config.get("protected_paths", []) or []:
            if not isinstance(entry, str) or not entry.strip():
                continue
            pattern = entry.strip()
            if pattern in value:
                return True
            try:
                if fnmatch.fnmatch(value, pattern) or fnmatch.fnmatch(
                    lowered, pattern.lower()
                ):
                    return True
            except Exception:
                continue
        return False

    def _guard_tool_path_access(self, tool_name: str, tool_args: dict) -> bool:
        """Rewrite tool args to block access to protected paths.

        Args:
            tool_name: Name of the tool about to be executed.
            tool_args: Mutable tool arguments dict.

        Returns:
            True if access was blocked (args rewritten), False otherwise.
        """
        denied_notice = (
            "[VibeGuard] Access denied: this path is restricted by VibeGuard."
        )
        if tool_name in (
            "astrbot_file_read_tool",
            "astrbot_file_write_tool",
            "astrbot_file_edit_tool",
        ):
            path = tool_args.get("path", "")
            if self._is_protected_target(path):
                self.logger.warning(
                    f"VibeGuard blocked {tool_name} access to protected path."
                )
                tool_args["path"] = ""
                return True
            return False
        if tool_name == "astrbot_grep_tool":
            path = tool_args.get("path", "")
            if path and self._is_protected_target(path):
                self.logger.warning(
                    "VibeGuard blocked astrbot_grep_tool access to protected path."
                )
                tool_args["path"] = ""
                return True
            return False
        if tool_name == "astrbot_execute_shell":
            command = tool_args.get("command", "")
            if self._is_protected_target(command):
                self.logger.warning(
                    "VibeGuard blocked astrbot_execute_shell targeting protected path."
                )
                tool_args["command"] = f"echo '{denied_notice}'"
                return True
            return False
        if tool_name == "astrbot_shell_session":
            action = tool_args.get("action", "")
            if action in ("write", "write_line") and self._is_protected_target(
                tool_args.get("chars", "")
            ):
                self.logger.warning(
                    "VibeGuard blocked astrbot_shell_session write targeting protected path."
                )
                tool_args["chars"] = ""
                return True
            return False
        if tool_name in ("astrbot_execute_python", "astrbot_execute_ipython"):
            code = tool_args.get("code", "")
            if self._is_protected_target(code):
                self.logger.warning(
                    f"VibeGuard blocked {tool_name} targeting protected path."
                )
                tool_args["code"] = f"print('{denied_notice}')"
                return True
            return False
        return False

    @filter.on_llm_response(priority=100)
    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """Restore placeholders in LLM response back to original sensitive values.

        Args:
            event: AstrMessageEvent context.
            response: Inbound LLMResponse.
        """
        if not self.config.get("enabled", True) or not self.replacer:
            return

        # 1. Restore result_chain Plain parts. When result_chain exists,
        # completion_text property reads from it, so fixing parts is enough.
        if response.result_chain:
            for comp in response.result_chain.chain:
                if hasattr(comp, "text") and isinstance(comp.text, str):
                    comp.text = await self.replacer.restore_text(comp.text)
        elif (
            hasattr(response, "_completion_text")
            and response._completion_text
            and isinstance(response._completion_text, str)
        ):
            # 2. No result_chain: restore via property setter to keep
            # _completion_text and chain in sync.
            response.completion_text = await self.replacer.restore_text(
                response._completion_text
            )

        # 3. Restore reasoning_content if present
        if response.reasoning_content:
            response.reasoning_content = await self.replacer.restore_text(
                response.reasoning_content
            )

        # 4. MainAgentHooks copies reasoning into event extra BEFORE this
        # hook runs, so the queued copy would stay masked. Restore it too,
        # otherwise ResultDecorateStage injects masked reasoning to user.
        try:
            extra_reasoning = event.get_extra("_llm_reasoning_content")
        except Exception:
            extra_reasoning = None
        if isinstance(extra_reasoning, str) and extra_reasoning:
            try:
                event.set_extra(
                    "_llm_reasoning_content",
                    await self.replacer.restore_text(extra_reasoning),
                )
            except Exception as e:
                self.logger.warning(f"VibeGuard reasoning extra restore failed: {e}")

    @filter.on_decorating_result(priority=100)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """Final safety net: restore any placeholders in outbound result.

        Covers non-streaming results built from LLMResponse after
        on_llm_response, plus any other path that forwards masked text.
        Streaming delivery is handled separately by the send_streaming
        wrapper, since ResultDecorateStage skips this hook for
        STREAMING_RESULT.

        Args:
            event: AstrMessageEvent context.
        """
        if not self.config.get("enabled", True) or not self.replacer:
            return
        try:
            result = event.get_result()
        except Exception:
            return
        if result is None or not getattr(result, "chain", None):
            return
        for comp in result.chain:
            try:
                if isinstance(comp, Plain):
                    if comp.text:
                        comp.text = await self.replacer.restore_text(comp.text)
                elif hasattr(comp, "text") and isinstance(comp.text, str) and comp.text:
                    comp.text = await self.replacer.restore_text(comp.text)
            except Exception as e:
                self.logger.warning(f"VibeGuard result restore failed: {e}")

    @filter.on_agent_done(priority=100)
    async def on_agent_done(
        self,
        event: AstrMessageEvent,
        run_context,
        response: LLMResponse,
    ) -> None:
        """Restore run_context messages before database history persistence.

        AstrBot's _save_to_history dumps run_context.messages straight into SQLite.
        By replacing placeholders back with original values here, local history
        records remain completely genuine and unmasked.

        Args:
            event: AstrMessageEvent context.
            run_context: Agent run context containing messages.
            response: Final LLM response.
        """
        if not self.config.get("enabled", True) or not self.replacer:
            return

        original_prompt = event.get_extra("_vg_original_prompt")
        messages: list[Message] = getattr(run_context, "messages", [])
        if not messages:
            return

        # Restore user messages and assistant messages in run_context
        for msg in messages:
            if msg.role == "user":
                # If this is the current turn user message, restore original prompt
                if original_prompt is not None:
                    if isinstance(msg.content, str):
                        msg.content = original_prompt
                    elif isinstance(msg.content, list):
                        for part in msg.content:
                            if (
                                hasattr(part, "type")
                                and part.type == "text"
                                and hasattr(part, "text")
                            ):
                                part.text = await self.replacer.restore_text(part.text)
                else:
                    # Otherwise restore any placeholders found
                    if isinstance(msg.content, str):
                        msg.content = await self.replacer.restore_text(msg.content)
                    elif isinstance(msg.content, list):
                        for part in msg.content:
                            if (
                                hasattr(part, "type")
                                and part.type == "text"
                                and hasattr(part, "text")
                            ):
                                part.text = await self.replacer.restore_text(part.text)
            elif msg.role == "assistant":
                # Ensure assistant message saved in DB has no lingering placeholders
                if isinstance(msg.content, str):
                    msg.content = await self.replacer.restore_text(msg.content)
                elif isinstance(msg.content, list):
                    for part in msg.content:
                        if (
                            hasattr(part, "type")
                            and part.type == "text"
                            and hasattr(part, "text")
                        ):
                            part.text = await self.replacer.restore_text(part.text)
            elif msg.role == "tool":
                # Tool result blocks may echo placeholders back (e.g. a tool
                # echoing its own arguments). Restore them so persisted
                # history stays genuine and unmasked.
                if isinstance(msg.content, str):
                    msg.content = await self.replacer.restore_text(msg.content)
                elif isinstance(msg.content, list):
                    for part in msg.content:
                        if (
                            hasattr(part, "type")
                            and part.type == "text"
                            and hasattr(part, "text")
                        ):
                            part.text = await self.replacer.restore_text(part.text)
