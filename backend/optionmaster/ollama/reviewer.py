"""Low-frequency local LLM review; never part of the execution decision."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import requests

from optionmaster.config import Settings
from optionmaster.context.models import ContextDecision
from optionmaster.journal.store import TradeJournal
from optionmaster.market.models import AnalysisResult
from optionmaster.ollama.models import OllamaReview, OllamaStatus, OllamaVerdict


class OllamaReviewError(RuntimeError):
    pass


class OllamaSetupReviewer:
    def __init__(self, *, settings: Settings, journal: TradeJournal) -> None:
        self._settings = settings
        self._journal = journal
        self._last_error: str | None = None

    def status(self) -> OllamaStatus:
        if not self._settings.ollama_enabled:
            return OllamaStatus(enabled=False, available=False, model=self._settings.ollama_model, host=self._settings.ollama_host)
        try:
            response = requests.get(f"{self._settings.ollama_host.rstrip('/')}/api/tags", timeout=3)
            response.raise_for_status()
            models = response.json().get("models", [])
            available = any(item.get("name") == self._settings.ollama_model for item in models if isinstance(item, dict))
            return OllamaStatus(enabled=True, available=available, model=self._settings.ollama_model, host=self._settings.ollama_host, last_error=self._last_error)
        except (requests.RequestException, ValueError) as exc:
            self._last_error = str(exc)
            return OllamaStatus(enabled=True, available=False, model=self._settings.ollama_model, host=self._settings.ollama_host, last_error=self._last_error)

    def review(self, *, analysis: AnalysisResult, context: ContextDecision | None) -> OllamaReview | None:
        signal = analysis.signal
        if not self._settings.ollama_enabled or signal.side is None:
            return None
        quote = next((item for item in analysis.snapshot.option_quotes if item.side is signal.side and item.strike == signal.strike), None)
        if quote is None:
            return None
        prompt = self._prompt(analysis=analysis, context=context, quote=quote)
        try:
            response = requests.post(
                f"{self._settings.ollama_host.rstrip('/')}/api/generate",
                json={
                    "model": self._settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1, "num_predict": 180},
                },
                timeout=self._settings.ollama_timeout_seconds,
            )
            response.raise_for_status()
            raw = response.json().get("response", "{}")
            payload = json.loads(raw)
            review = OllamaReview(
                recorded_at=datetime.now(timezone.utc),
                model=self._settings.ollama_model,
                context_decision_id=context.id if context else None,
                symbol=analysis.snapshot.symbol,
                side=signal.side,
                verdict=OllamaVerdict(str(payload.get("verdict", "REVIEW")).upper()),
                confidence=max(0, min(100, int(payload.get("confidence", 0)))),
                rationale=str(payload.get("rationale") or "No rationale returned.")[:600],
                risk_flags=[str(item)[:120] for item in (payload.get("risk_flags") or [])[:5]],
            )
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise OllamaReviewError(self._last_error) from exc
        self._last_error = None
        self._journal.record_ollama_review(review)
        return review

    def review_async(self, *, analysis: AnalysisResult, context: ContextDecision | None) -> None:
        """Queue a local review without delaying the deterministic order pipeline."""
        if not self._settings.ollama_enabled or analysis.signal.side is None:
            return

        def run() -> None:
            try:
                self.review(analysis=analysis, context=context)
            except OllamaReviewError:
                # Status retains the failure; market processing must not wait.
                pass

        threading.Thread(target=run, daemon=True, name="om-ollama-review").start()

    @staticmethod
    def _prompt(*, analysis: AnalysisResult, context: ContextDecision | None, quote) -> str:
        context_payload = {
            "action": context.action.value,
            "confluence": context.confluence.total,
            "fresh": context.freshness.fresh,
            "risk_reward_to_structure": context.risk_reward_to_structure,
            "reasons": context.reasons,
        } if context else {"action": "UNAVAILABLE"}
        setup = {
            "symbol": analysis.snapshot.symbol,
            "underlying": analysis.snapshot.underlying,
            "underlying_change_pct": analysis.features.underlying_change_pct,
            "momentum_pct": analysis.features.momentum,
            "india_vix": analysis.features.india_vix,
            "regime": analysis.signal.regime.value,
            "side": analysis.signal.side.value if analysis.signal.side else None,
            "strike": analysis.signal.strike,
            "signal_score": analysis.signal.score,
            "option": {"ltp": quote.ltp, "bid": quote.bid, "ask": quote.ask, "spread_fraction": quote.spread_fraction, "delta": quote.delta, "gamma": quote.gamma, "iv": quote.iv, "oi_change": quote.oi_change, "volume": quote.volume},
            "context": context_payload,
        }
        return (
            "You are a conservative Indian index-options setup reviewer. Evaluate this single supplied setup only. "
            "Do not invent prices, news, levels, or instructions. This review cannot place an order. "
            "Return JSON only with verdict (ALLOW, REVIEW, or SKIP), confidence (0-100), rationale (max 80 words), and risk_flags (max 5).\n"
            f"SETUP={json.dumps(setup, separators=(',', ':'))}"
        )
