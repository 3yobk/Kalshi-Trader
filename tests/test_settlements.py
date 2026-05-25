from main import _settlement_outcome_from_market


def test_settlement_outcome_from_market_result_field() -> None:
    assert _settlement_outcome_from_market({"result": "yes"}) is True
    assert _settlement_outcome_from_market({"result": "no"}) is False


def test_settlement_outcome_from_market_expiration_value_field() -> None:
    assert _settlement_outcome_from_market({"expiration_value": "YES"}) is True
    assert _settlement_outcome_from_market({"expiration_value": "0"}) is False
    assert _settlement_outcome_from_market({"expiration_value": ""}) is None
