from optionmaster.instruments.scrip_master import NseScripMaster
from optionmaster.instruments.sizing import QuantityResolution, resolve_quantity


def resolve_backtest_quantity(
    *,
    scrip_master: NseScripMaster,
    contract_security_id: int,
    lots: int = 1,
    requested_quantity: int | None = None,
) -> QuantityResolution:
    """Use the same current/archived master lot size in every backtest order."""
    return resolve_quantity(
        scrip_master=scrip_master,
        security_id=contract_security_id,
        lots=lots,
        requested_quantity=requested_quantity,
    )
