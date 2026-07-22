from optionmaster.instruments.scrip_master import NseScripMaster
from optionmaster.instruments.sizing import resolve_quantity


def test_sem_lot_units_drives_forward_and_backtest_quantity(tmp_path):
    master = NseScripMaster(tmp_path)
    csv = (
        "SEM_EXM_EXCH_ID,SEM_SMST_SECURITY_ID,SEM_LOT_UNITS\n"
        "NSE,44900,75\n"
        "BSE,99001,20\n"
    ).encode()
    master._install(csv, "test")

    resolved = resolve_quantity(scrip_master=master, security_id=44900, lots=2)
    assert resolved.lot_size == 75
    assert resolved.quantity == 150
