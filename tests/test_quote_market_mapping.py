from data.sources import _common as common
from portfolio.portfolio_snapshot import _resolve_price


def test_exchange_mapping_covers_shanghai_etf_and_beijing_codes():
    assert common.a_prefix('563800') == 'sh'
    assert common.tencent_code('563800') == 'sh563800'
    assert common.sina_code('563800') == 'sh563800'
    assert common.em_secid('563800') == '1.563800'
    assert common.a_prefix('920819') == 'bj'
    assert common.tencent_code('920819') == 'bj920819'


def test_snapshot_rejects_implausible_quote_jump():
    price, source, anomalous = _resolve_price(100, 1.2989, 1.2989)
    assert price == 1.2989
    assert source == 'previous_anomaly'
    assert anomalous is True


def test_snapshot_uses_previous_price_when_quote_is_missing():
    price, source, anomalous = _resolve_price(None, 4.059, 3.8)
    assert price == 4.059
    assert source == 'previous_missing'
    assert anomalous is False
