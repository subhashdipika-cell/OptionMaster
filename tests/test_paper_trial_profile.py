from optionmaster.journal.store import TradeJournal
from optionmaster.learning.service import StrategyProfileRegistry


def test_momentum_paper_trial_is_explicit_and_uses_tested_exit_geometry(tmp_path):
    profiles = StrategyProfileRegistry(TradeJournal(tmp_path / "optionmaster.db"))

    profile = profiles.start_paper_trial("paper-momentum-10-15-v1")

    assert profile.paper_trial_only is True
    assert profile.paper_stop_loss_fraction == 0.10
    assert profile.paper_target_fraction == 0.15
    assert profiles.active().id == "paper-momentum-10-15-v1"
