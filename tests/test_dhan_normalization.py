from datetime import datetime

from optionmaster.dhan.service import DhanMarketService


class FakeClient:
    def option_chain(self, **kwargs):
        return {
            "data": {
                "last_price": 25000,
                "oc": {
                    "25000": {
                        "ce": {
                            "security_id": 44900,
                            "last_price": 120,
                            "top_bid_price": 119,
                            "top_ask_price": 121,
                            "implied_volatility": 14,
                            "oi": 1000,
                            "previous_oi": 900,
                            "volume": 500,
                            "greeks": {"delta": 0.5},
                        },
                        "pe": {
                            "security_id": 44901,
                            "last_price": 110,
                            "top_bid_price": 109,
                            "top_ask_price": 111,
                            "implied_volatility": 15,
                            "oi": 1200,
                            "previous_oi": 1000,
                            "volume": 600,
                            "greeks": {"delta": -0.5},
                        },
                    }
                },
            }
        }

    def intraday_minute_data(self, **kwargs):
        security_id = kwargs["security_id"]
        if security_id == "21":
            return {"data": {"close": [14.2, 14.3, 14.4, 14.3, 14.2, 14.1]}}
        return {"data": {"close": [24950, 24960, 24965, 24975, 24990, 25000]}}


class FakeFactory:
    def create(self):
        return FakeClient()


def test_dhan_chain_is_normalized_to_snapshot():
    snapshot = DhanMarketService(FakeFactory()).option_chain_snapshot(
        symbol="NIFTY", security_id=13, segment="IDX_I", expiry="2026-07-30"
    )

    assert snapshot.symbol == "NIFTY"
    assert snapshot.underlying == 25000
    assert len(snapshot.option_quotes) == 2
    assert snapshot.option_quotes[0].security_id in (44900, 44901)
    assert snapshot.option_quotes[0].iv in (0.14, 0.15)


def test_live_snapshot_includes_intraday_context_and_vix():
    snapshot = DhanMarketService(FakeFactory()).live_snapshot(
        symbol="NIFTY", security_id=13, segment="IDX_I", expiry="2026-07-21"
    )

    assert snapshot.underlying_momentum > 0
    assert snapshot.underlying_change_pct > 0
    assert snapshot.india_vix == 14.1
