from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_identity_checks_current_application_and_rejects_wrong_bot():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test", extra={"expected_bot_username": "@expected", "expected_bot_id": "42"}))
    adapter._bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=42, username="expected")))
    adapter._app = SimpleNamespace(bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=99, username="other"))))
    with pytest.raises(RuntimeError, match="identity mismatch"):
        await adapter._verify_bot_identity()
    adapter._bot.get_me.assert_not_awaited()
    adapter._app.bot.get_me.return_value = SimpleNamespace(id=42, username="Expected")
    await adapter._verify_bot_identity()


def test_source_prompt_is_recovered_when_synthetic_event_loses_hint():
    from gateway import run_turn_runner
    from gateway.config import Platform
    runner_class = next(v for v in vars(run_turn_runner).values() if isinstance(v, type) and '_combined_ephemeral_prompt' in vars(v))
    turn = object.__new__(runner_class)
    turn._ctx = SimpleNamespace(context_prompt="context", channel_prompt=None, source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="100", thread_id="562", parent_chat_id=None))
    turn._runner = SimpleNamespace(adapters={Platform.TELEGRAM: SimpleNamespace(config=SimpleNamespace(extra={"channel_prompts": {"562": "topic policy"}}))}, _get_system_prompt_for_channel=lambda *a, **kw: None)
    assert turn._combined_ephemeral_prompt() == "context\n\ntopic policy"
