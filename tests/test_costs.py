from optionmaster.costs.calculator import NseOptionCostCalculator


def test_nse_option_costs_reduce_gross_profit_to_net_profit():
    result = NseOptionCostCalculator().net_result(entry_price=100, exit_price=130, quantity=75)

    assert result.gross_pnl == 2250
    assert result.costs.entry.brokerage == 20
    assert result.costs.exit.brokerage == 20
    assert result.costs.exit.stt > 0
    assert result.net_pnl < result.gross_pnl
