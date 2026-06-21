"""Serial wire-protocol round-trip tests (CLAUDE.md rule 4).

The firmware parser (SIGNAL_ADAPT.ino Section 6.10/6.12) and adapt/serial_bridge.py
must agree exactly. These tests pin the REMAP grammar and the telemetry CSV format.
"""
import pytest

from adapt.serial_bridge import build_remap, parse_remap, parse_telemetry


def test_build_remap_simple():
    assert build_remap("SELECT", "SHORT") == "REMAP,SELECT,SHORT\n"


def test_build_remap_compound():
    assert build_remap("HOME", "SHORT_SHORT") == "REMAP,HOME,SHORT_SHORT\n"


def test_remap_round_trip_compound():
    line = build_remap("HOME", "SHORT_SHORT")
    logical, tokens = parse_remap(line)
    assert logical == "HOME"
    assert tokens == ["SHORT", "SHORT"]


def test_remap_round_trip_single():
    logical, tokens = parse_remap(build_remap("BACK", "DOUBLE"))
    assert logical == "BACK"
    assert tokens == ["DOUBLE"]


def test_build_remap_rejects_bad_logical():
    with pytest.raises(ValueError):
        build_remap("LONG", "SHORT_SHORT")  # LONG is a burst cmd, not a logical


def test_build_remap_rejects_bad_pattern():
    with pytest.raises(ValueError):
        build_remap("HOME", "TRIPLE")
    with pytest.raises(ValueError):
        build_remap("HOME", "SHORT_SHORT_SHORT")  # >2 tokens


def test_parse_telemetry_valid():
    row = parse_telemetry("1234,5.0,8000,3,1,2")
    assert row["t_ms"] == 1234
    assert row["emg_rms"] == pytest.approx(5.0)
    assert row["state"] == 3 and row["state_name"] == "S_ACTUATE"
    assert row["last_cmd"] == 1 and row["last_cmd_name"] == "CMD_SHORT"
    assert row["false_neg_count"] == 2


def test_parse_telemetry_skips_human_lines():
    assert parse_telemetry("SIGNAL+ADAPT ready. 'c'=calibrate.") is None
    assert parse_telemetry("REMAPPED HOME") is None
    assert parse_telemetry("1,2,3") is None          # wrong field count
    assert parse_telemetry("a,b,c,d,e,f") is None     # non-numeric
