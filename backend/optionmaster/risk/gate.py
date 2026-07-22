from optionmaster.market.models import Signal


def apply_risk_gate(
    signal: Signal,
    *,
    capital: float = 0,
    risk_fraction: float = 0.01,
    daily_loss_fraction: float = 0,
    minimum_signal_score: float = 0.60,
) -> Signal:
    """Keep sizing conservative until broker fills and stop distance are known."""
    if daily_loss_fraction >= 0.02:
        signal.reason = "Blocked by daily loss limit."
        signal.quantity = 0
        return signal
    if not signal.side or not signal.strike or signal.score < minimum_signal_score or capital <= 0:
        signal.quantity = 0
        return signal
    signal.quantity = 1
    return signal
