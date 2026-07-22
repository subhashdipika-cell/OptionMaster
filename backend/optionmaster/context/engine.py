from collections import defaultdict
from datetime import date
from statistics import fmean

from optionmaster.context.models import (
    ConfluenceBreakdown,
    ContextDecision,
    ContextEvaluationRequest,
    FreshnessMetrics,
    LevelKind,
    LevelRole,
    PositionSizeHint,
    ShadowAction,
    StructuralLevel,
)
from optionmaster.market.features import build_features
from optionmaster.market.models import OptionSide


class ContextEngine:
    """Explains price location and momentum quality without changing execution behavior."""

    def evaluate(self, request: ContextEvaluationRequest) -> ContextDecision:
        bars = sorted(request.bars, key=lambda item: item.timestamp)
        price = request.snapshot.underlying
        atr = self._atr(bars, request.settings.atr_period)
        levels = self._build_levels(request, bars, atr)
        signal = request.signal
        if signal.side is None:
            return ContextDecision(
                timestamp=request.snapshot.timestamp,
                symbol=request.snapshot.symbol,
                strategy_id=signal.strategy_id,
                underlying_price=price,
                action=ShadowAction.NO_DIRECTIONAL_SIGNAL,
                position_size_hint=PositionSizeHint.NO_ENTRY,
                freshness=FreshnessMetrics(atr=atr),
                confluence=ConfluenceBreakdown(total=0, location=0, freshness=0, higher_timeframe_trend=0, volume_oi=0),
                levels=levels,
                reasons=["No directional CE/PE signal was generated; context filter remains observational."],
            )

        origin, age = self._origin_and_age(request, bars, price)
        traveled = abs(price - origin)
        traveled_atr = traveled / atr if atr > 0 else 0
        fresh = traveled_atr <= request.settings.max_trigger_extension_atr and age <= request.settings.max_signal_age_candles
        entry_preference = "ENTER_ON_CONFIRMATION" if fresh else "WAIT_FOR_PULLBACK"
        freshness = FreshnessMetrics(
            atr=round(atr, 2),
            trigger_price=round(origin, 2),
            traveled_points=round(traveled, 2),
            traveled_atr=round(traveled_atr, 3),
            signal_age_candles=age,
            fresh=fresh,
            entry_preference=entry_preference,
        )

        opposing, defensive = self._nearest_levels(signal.side, price, levels)
        buffer = max(atr * request.settings.structure_buffer_atr, 0.01)
        stop_distance = max(atr * request.settings.assumed_stop_atr, buffer)
        distance_to_barrier = self._distance_to_barrier(signal.side, price, opposing)
        rr = round(distance_to_barrier / stop_distance, 3) if distance_to_barrier is not None and stop_distance > 0 else None
        target = self._target_before_barrier(signal.side, opposing, atr, request.settings.target_buffer_atr)
        near_opposing = distance_to_barrier is not None and distance_to_barrier <= buffer
        has_room = rr is not None and rr >= request.settings.minimum_structure_rr
        trend_aligned = self._higher_timeframe_alignment(signal.side, bars)
        oi_volume_score = self._oi_volume_score(request)
        location_score = 30 if opposing is not None and not near_opposing and has_room else 0
        freshness_score = 25 if fresh else 0
        trend_score = 20 if trend_aligned else 0
        confluence = ConfluenceBreakdown(
            total=location_score + freshness_score + trend_score + oi_volume_score,
            location=location_score,
            freshness=freshness_score,
            higher_timeframe_trend=trend_score,
            volume_oi=oi_volume_score,
        )

        reasons: list[str] = []
        if not fresh:
            reasons.append(
                f"Would skip: price is {freshness.traveled_atr:.2f} ATR from the trigger or the signal is older than the freshness window."
            )
        if opposing is None:
            reasons.append("Would skip: no opposing structural barrier is available to cap a realistic target.")
        elif near_opposing:
            reasons.append("Would skip: entry is too close to the nearest opposing support/resistance level.")
        elif not has_room:
            reasons.append(
                f"Would skip: structure offers only {rr:.2f}R before the next barrier; minimum is {request.settings.minimum_structure_rr:.2f}R."
            )
        if not trend_aligned:
            reasons.append("Higher-timeframe bar trend is not aligned with the directional signal.")
        if oi_volume_score < 25:
            reasons.append("Volume/OI confirmation is incomplete; this reduces confluence.")
        eligible = fresh and opposing is not None and not near_opposing and has_room and confluence.total >= request.settings.minimum_confluence_score
        if eligible:
            reasons.append("Shadow filter would allow this setup; no order behavior is changed.")
        action = ShadowAction.WOULD_ALLOW if eligible else ShadowAction.WOULD_SKIP
        size_hint = (
            PositionSizeHint.STRONG_SETUP_REVIEW
            if eligible and confluence.total >= request.settings.strong_confluence_score
            else PositionSizeHint.STANDARD_PAPER_ONLY
            if eligible
            else PositionSizeHint.NO_ENTRY
        )
        return ContextDecision(
            timestamp=request.snapshot.timestamp,
            symbol=request.snapshot.symbol,
            strategy_id=signal.strategy_id,
            side=signal.side,
            underlying_price=price,
            action=action,
            position_size_hint=size_hint,
            freshness=freshness,
            confluence=confluence,
            nearest_opposing_level=opposing,
            nearest_defensive_level=defensive,
            recommended_underlying_target=target,
            assumed_stop_distance=round(stop_distance, 2),
            risk_reward_to_structure=rr,
            levels=levels,
            reasons=reasons,
        )

    @staticmethod
    def _atr(bars, period: int) -> float:
        ranges: list[float] = []
        previous_close = bars[0].close
        for bar in bars[1:]:
            ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))
            previous_close = bar.close
        values = ranges[-period:] or [max(bars[-1].high - bars[-1].low, 0.01)]
        return max(fmean(values), 0.01)

    def _origin_and_age(self, request: ContextEvaluationRequest, bars, current_price: float) -> tuple[float, int]:
        if request.signal_origin_price is not None:
            return request.signal_origin_price, request.signal_age_candles or 0
        side = request.signal.side
        if side is OptionSide.CE:
            candidates = [(index, bar.high) for index, bar in enumerate(bars[:-1]) if bar.high < current_price]
        else:
            candidates = [(index, bar.low) for index, bar in enumerate(bars[:-1]) if bar.low > current_price]
        if candidates:
            index, origin = candidates[-1]
            return origin, len(bars) - 1 - index
        fallback_index = max(0, len(bars) - 1 - request.settings.max_signal_age_candles)
        return bars[fallback_index].close, len(bars) - 1 - fallback_index

    def _build_levels(self, request: ContextEvaluationRequest, bars, atr: float) -> list[StructuralLevel]:
        levels: list[StructuralLevel] = []
        latest_date = bars[-1].timestamp.date()
        prior_dates = sorted({bar.timestamp.date() for bar in bars if bar.timestamp.date() < latest_date})
        if prior_dates:
            previous_day = prior_dates[-1]
            previous_bars = [bar for bar in bars if bar.timestamp.date() == previous_day]
            high = max(bar.high for bar in previous_bars)
            low = min(bar.low for bar in previous_bars)
            close = previous_bars[-1].close
            pivot = (high + low + close) / 3
            levels.extend(
                [
                    StructuralLevel(price=high, role=LevelRole.RESISTANCE, kind=LevelKind.PREVIOUS_DAY_HIGH, strength=4, note="Prior session high."),
                    StructuralLevel(price=low, role=LevelRole.SUPPORT, kind=LevelKind.PREVIOUS_DAY_LOW, strength=4, note="Prior session low."),
                    StructuralLevel(price=pivot, role=LevelRole.SUPPORT if request.snapshot.underlying >= pivot else LevelRole.RESISTANCE, kind=LevelKind.PIVOT, strength=3, note="Prior session pivot."),
                    StructuralLevel(price=(2 * pivot) - low, role=LevelRole.RESISTANCE, kind=LevelKind.PIVOT_RESISTANCE, strength=3, note="First pivot resistance."),
                    StructuralLevel(price=(2 * pivot) - high, role=LevelRole.SUPPORT, kind=LevelKind.PIVOT_SUPPORT, strength=3, note="First pivot support."),
                ]
            )
        levels.extend(self._swing_levels(bars))
        levels.extend(self._round_levels(request.snapshot.underlying, request.settings.round_number_interval))
        calls = [quote for quote in request.snapshot.option_quotes if quote.side is OptionSide.CE and quote.oi > 0]
        puts = [quote for quote in request.snapshot.option_quotes if quote.side is OptionSide.PE and quote.oi > 0]
        if calls:
            call = max(calls, key=lambda quote: quote.oi)
            levels.append(StructuralLevel(price=call.strike, role=LevelRole.RESISTANCE, kind=LevelKind.MAX_CALL_OI, strength=4, note="Highest call OI strike; treated as a structural candidate."))
        if puts:
            put = max(puts, key=lambda quote: quote.oi)
            levels.append(StructuralLevel(price=put.strike, role=LevelRole.SUPPORT, kind=LevelKind.MAX_PUT_OI, strength=4, note="Highest put OI strike; treated as a structural candidate."))
        return self._deduplicate(levels, tolerance=max(atr * 0.08, 1.0))

    @staticmethod
    def _swing_levels(bars) -> list[StructuralLevel]:
        levels: list[StructuralLevel] = []
        for index in range(2, max(2, len(bars) - 2)):
            window = bars[index - 2 : index + 3]
            current = bars[index]
            if current.high >= max(item.high for item in window):
                levels.append(StructuralLevel(price=current.high, role=LevelRole.RESISTANCE, kind=LevelKind.SWING_HIGH, strength=2, note="Five-bar swing high."))
            if current.low <= min(item.low for item in window):
                levels.append(StructuralLevel(price=current.low, role=LevelRole.SUPPORT, kind=LevelKind.SWING_LOW, strength=2, note="Five-bar swing low."))
        return levels[-8:]

    @staticmethod
    def _round_levels(price: float, interval: float) -> list[StructuralLevel]:
        base = int(price // interval) * interval
        return [
            StructuralLevel(price=max(interval, base + offset * interval), role=LevelRole.SUPPORT if base + offset * interval <= price else LevelRole.RESISTANCE, kind=LevelKind.ROUND_NUMBER, strength=2, note="Round-number reference level.")
            for offset in (-1, 0, 1, 2)
            if base + offset * interval > 0
        ]

    @staticmethod
    def _deduplicate(levels: list[StructuralLevel], tolerance: float) -> list[StructuralLevel]:
        grouped: dict[LevelRole, list[StructuralLevel]] = defaultdict(list)
        for level in sorted(levels, key=lambda item: (-item.strength, item.price)):
            existing = grouped[level.role]
            if all(abs(level.price - item.price) > tolerance for item in existing):
                existing.append(level)
        return sorted([level for values in grouped.values() for level in values], key=lambda item: item.price)

    @staticmethod
    def _nearest_levels(side: OptionSide, price: float, levels: list[StructuralLevel]):
        supports = [level for level in levels if level.role is LevelRole.SUPPORT]
        resistances = [level for level in levels if level.role is LevelRole.RESISTANCE]
        if side is OptionSide.CE:
            opposing = min((level for level in resistances if level.price > price), key=lambda item: item.price, default=None)
            defensive = max((level for level in supports if level.price < price), key=lambda item: item.price, default=None)
        else:
            opposing = max((level for level in supports if level.price < price), key=lambda item: item.price, default=None)
            defensive = min((level for level in resistances if level.price > price), key=lambda item: item.price, default=None)
        return opposing, defensive

    @staticmethod
    def _distance_to_barrier(side: OptionSide, price: float, barrier: StructuralLevel | None) -> float | None:
        if barrier is None:
            return None
        return max(0.0, barrier.price - price) if side is OptionSide.CE else max(0.0, price - barrier.price)

    @staticmethod
    def _target_before_barrier(side: OptionSide, barrier: StructuralLevel | None, atr: float, buffer_atr: float) -> float | None:
        if barrier is None:
            return None
        buffer = atr * buffer_atr
        target = barrier.price - buffer if side is OptionSide.CE else barrier.price + buffer
        return round(target, 2)

    @staticmethod
    def _higher_timeframe_alignment(side: OptionSide, bars) -> bool:
        closes = [bar.close for bar in bars]
        if len(closes) < 20:
            return False
        fast = fmean(closes[-20:])
        slow = fmean(closes[-50:]) if len(closes) >= 50 else fmean(closes[:20])
        return (closes[-1] >= fast >= slow) if side is OptionSide.CE else (closes[-1] <= fast <= slow)

    @staticmethod
    def _oi_volume_score(request: ContextEvaluationRequest) -> int:
        side = request.signal.side
        if side is None:
            return 0
        features = build_features(request.snapshot)
        oi_aligned = features.put_oi_change > features.call_oi_change if side is OptionSide.CE else features.call_oi_change > features.put_oi_change
        selected = next(
            (quote for quote in request.snapshot.option_quotes if quote.side is side and quote.strike == request.signal.strike),
            None,
        )
        side_volumes = [quote.volume for quote in request.snapshot.option_quotes if quote.side is side and quote.volume > 0]
        volume_aligned = bool(selected and side_volumes and selected.volume >= fmean(side_volumes))
        return 25 if oi_aligned and volume_aligned else 12 if oi_aligned else 0
