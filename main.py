import asyncio
import copy

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
                "[Notice: Strings matching __VG_*__ are security redaction placeholders for sensitive credentials/data. Treat them as valid opaque identifiers and keep them intact when referencing.]",
            )
            if notice_text:
                # Append as a temporary extra content part marked with mark_as_temp()
                # so it is transmitted to the LLM but stripped before database history saving
                req.extra_user_content_parts.append(
                    TextPart(text=f"\n\n{notice_text}").mark_as_temp()
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
