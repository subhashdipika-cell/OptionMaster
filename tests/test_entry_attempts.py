from datetime import datetime, timezone

from optionmaster.journal.store import EntryAttempt, TradeJournal


def test_entry_attempts_are_durable(tmp_path):
    journal = TradeJournal(tmp_path / "optionmaster.db")
    journal.record_entry_attempt(
        EntryAttempt(
            id="attempt-1", recorded_at=datetime.now(timezone.utc), mode="PAPER",
            outcome="DECLINED", reason="No trade signal", symbol="NIFTY",
        )
    )

    attempts = journal.list_entry_attempts()
    assert len(attempts) == 1
    assert attempts[0].reason == "No trade signal"
