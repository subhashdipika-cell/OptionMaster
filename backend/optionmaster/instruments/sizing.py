from dataclasses import dataclass

from optionmaster.instruments.scrip_master import NseScripMaster, ScripMasterError


class QuantityResolutionError(ValueError):
    """Raised if a backtest or forward-test order violates lot-size rules."""


@dataclass(frozen=True, slots=True)
class QuantityResolution:
    security_id: int
    lot_size: int
    lots: int
    quantity: int

    def as_dict(self) -> dict[str, int]:
        return {
            "security_id": self.security_id,
            "lot_size": self.lot_size,
            "lots": self.lots,
            "quantity": self.quantity,
        }


def resolve_quantity(
    *,
    scrip_master: NseScripMaster,
    security_id: int,
    lots: int = 1,
    requested_quantity: int | None = None,
) -> QuantityResolution:
    """Shared quantity rule for historical backtests and live forward tests."""
    if lots <= 0:
        raise QuantityResolutionError("Lots must be at least one.")
    try:
        lot_size = scrip_master.lot_size_for_security(security_id)
    except ScripMasterError as exc:
        raise QuantityResolutionError(str(exc)) from exc
    quantity = requested_quantity if requested_quantity is not None else lot_size * lots
    if quantity <= 0 or quantity % lot_size != 0:
        raise QuantityResolutionError(
            f"Quantity {quantity} must be a positive multiple of current lot size {lot_size}."
        )
    return QuantityResolution(
        security_id=security_id,
        lot_size=lot_size,
        lots=quantity // lot_size,
        quantity=quantity,
    )
