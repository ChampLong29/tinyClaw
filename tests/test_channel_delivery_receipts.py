from tinyclaw.channel.base import Channel, ChannelSendResult
from tinyclaw.channel.feishu import FeishuChannel
from tinyclaw.channel.telegram import TelegramChannel


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Http:
    def __init__(self, payload):
        self.payload = payload

    def post(self, *_args, **_kwargs):
        return _Response(self.payload)


class BoolOnlyChannel(Channel):
    def receive(self):
        return None

    def send(self, _to, _text, **_kwargs):
        return True


def test_base_channel_reports_bool_send_as_unconfirmed_acceptance():
    result = BoolOnlyChannel().send_with_receipt("peer-1", "hello")

    assert result == ChannelSendResult(accepted=True)
    assert result.confirmed is False
    assert result.platform_message_id is None


def test_telegram_returns_confirmed_platform_message_id():
    channel = object.__new__(TelegramChannel)
    channel._api = lambda *_args, **_kwargs: {"message_id": 42}

    result = channel.send_with_receipt("chat-1", "hello")

    assert result.accepted is True
    assert result.confirmed is True
    assert result.platform_message_id == "42"


def test_feishu_returns_confirmed_platform_message_id():
    channel = object.__new__(FeishuChannel)
    channel.api_base = "https://example.invalid"
    channel._refresh_token = lambda: "token"
    channel._http = _Http(
        {
            "code": 0,
            "data": {"message_id": "om_123"},
        }
    )

    result = channel.send_with_receipt("chat-1", "hello")

    assert result.accepted is True
    assert result.confirmed is True
    assert result.platform_message_id == "om_123"
