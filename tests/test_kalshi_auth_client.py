from cryptography.hazmat.primitives.asymmetric import rsa

from data_ingestors.kalshi_auth_client import build_post_only_yes_bid_order, _sign_pss_text
from config import BotConfig, RuntimeSettings, _clean_env, _env_bool


def test_sign_pss_text_returns_base64_signature() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    signature = _sign_pss_text(private_key, "123GET/trade-api/v2/api_keys")

    assert isinstance(signature, str)
    assert len(signature) > 100


def test_clean_env_strips_accidental_quotes_and_spaces() -> None:
    assert _clean_env(" 'abc-123' ") == "abc-123"
    assert _clean_env("") is None


def test_env_bool_parses_explicit_truthy_values() -> None:
    assert _env_bool("true")
    assert _env_bool("1")
    assert _env_bool("YES")
    assert not _env_bool("false")
    assert not _env_bool(None)


def test_runtime_defaults_stay_paper() -> None:
    assert RuntimeSettings().bot_env == "paper"
    assert RuntimeSettings().live_trading_enabled is False
    assert BotConfig().paper_only is True


def test_build_post_only_yes_bid_order_payload_is_limit_only() -> None:
    order = build_post_only_yes_bid_order("KXHIGHNY-26MAY25-T72", quantity=2, limit_price_cents=44)

    payload = order.as_payload()

    assert payload["ticker"] == "KXHIGHNY-26MAY25-T72"
    assert payload["side"] == "bid"
    assert payload["count"] == "2.00"
    assert payload["price"] == "0.4400"
    assert payload["time_in_force"] == "good_till_canceled"
    assert payload["self_trade_prevention_type"] == "taker_at_cross"
    assert payload["post_only"] is True
    assert payload["cancel_order_on_pause"] is True
    assert payload["exchange_index"] == 0
    assert str(payload["client_order_id"]).startswith("weatherbot-")
