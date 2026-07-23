from app.agents.channel_adapters.base import ChannelAdapter
from app.agents.channel_adapters.instagram import InstagramAdapter
from app.agents.channel_adapters.telegram import TelegramAdapter

_INSTAGRAM_CHANNELS = frozenset({"instagram", "instagram_comment"})


def get_adapter(channel: str) -> ChannelAdapter:
    """Return the correct adapter for a channel name. Defaults to Telegram."""
    if channel in _INSTAGRAM_CHANNELS:
        return InstagramAdapter()
    return TelegramAdapter()
