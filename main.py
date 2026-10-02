import asyncio
import copy

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import Message, TextPart
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

        # 1. Restore completion_text via result_chain
        if response.result_chain:
            for comp in response.result_chain.chain:
                if hasattr(comp, "text") and isinstance(comp.text, str):
                    comp.text = await self.replacer.restore_text(comp.text)

        # 2. Also restore raw completion text if available
        if (
            hasattr(response, "_completion_text")
            and response._completion_text
            and isinstance(response._completion_text, str)
        ):
            response._completion_text = await self.replacer.restore_text(
                response._completion_text
            )

        # 3. Restore reasoning_content if present
        if response.reasoning_content:
            response.reasoning_content = await self.replacer.restore_text(
                response.reasoning_content
            )

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
