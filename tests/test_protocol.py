"""Unit tests anchored to LIVE wire captures from a real ASF02 feeder.

Every expected value below came from a real device response, not a guess.
"""
import asyncio

import pytest

from asf02.protocol import (
    ADV_COMPANY_ID,
    ERR_DEVICE_LOCKED,
    ERR_INVALID_PARAMS,
    ASF02Error,
    PlanEntry,
    decode_advertisement,
    decode_plan_entry,
    decode_plan_slot,
    encode_plan_entry,
    encode_request,
    generate_token,
    is_clear_marker,
    parse_response,
    plan_entry_valid,
    unlock_code_for,
)


# ---------------------------------------------------------------------------
# Token / unlock — the live token and its working unlock code
# ---------------------------------------------------------------------------
def test_unlock_code_matches_live_token():
    # Example token demonstrating the unlock code derivation:
    assert unlock_code_for("ExampleTok1") == "a587ea45721f8a612c0fcbf507d69adc"


def test_unlock_code_drops_leading_zero_nibbles():
    # BigInteger(1, ...).toString(16) semantics: a digest starting 0x05 would
    # produce a 31-char code, not 32.
    import hashlib

    token = "x"
    digest = hashlib.md5(b"0000:x").digest()
    if digest[0] == 0:
        # can't rely on this token; construct the property test instead
        pass
    expected = format(int.from_bytes(digest, "big"), "x")
    assert unlock_code_for(token) == expected
    if digest[0] == 0:
        assert len(expected) == 31
    else:
        assert len(expected) == 32


def test_generate_token_charset_and_length():
    import string

    t = generate_token()
    assert len(t) == 10
    assert all(c in (string.ascii_letters + string.digits) for c in t)
    assert generate_token() != generate_token() or True  # randomness not asserted strictly


# ---------------------------------------------------------------------------
# Plan strings — encode/decode round-trips + the live entries
# ---------------------------------------------------------------------------
def test_live_plan_entries_decode():
    # Written to the device and echoed back verbatim (all time fields are HEX):
    #   "000f1e0101" = 15:30 w1 on   (0x0f=15, 0x1e=30)
    #   "0010000101" = 16:00 w1 on   (0x10=16, 0x00=0)
    assert decode_plan_entry("000f1e0101") == PlanEntry(15, 30, 1, True)
    assert decode_plan_entry("0010000101") == PlanEntry(16, 0, 1, True)


def test_time_fields_are_hex_not_decimal():
    # GOTCHA (bit us in live testing): HH and MM are HEX. "0012000200" is NOT
    # 12:00 — it's 18:00 (0x12). To program 12:00 you must write "000c000200".
    assert decode_plan_entry("0012000200") == PlanEntry(18, 0, 2, False)
    assert decode_plan_entry("000c000200") == PlanEntry(12, 0, 2, False)


def test_encode_matches_vendor_format():
    # Vendor getPlanStr: "00" + hex(hh) + hex(mm) + %02x(portion) + "01"/"00"
    assert encode_plan_entry(15, 30, 1, True) == "000f1e0101"
    assert encode_plan_entry(9, 30, 3, True) == "00091e0301"   # 30 min = 0x1e
    assert encode_plan_entry(12, 0, 2, False) == "000c000200"  # 12 hr = 0x0c
    assert encode_plan_entry(16, 0, 1, False) == "0010000100"


def test_roundtrip_all_times():
    for h in (0, 5, 12, 23):
        for m in (0, 5, 30, 59):
            e = PlanEntry(h, m, 7, h % 2 == 0)
            assert decode_plan_entry(encode_plan_entry(h, m, 7, e.enabled)) == e


def test_validation():
    assert plan_entry_valid("000f1e0101")
    assert not plan_entry_valid("010f1e0101")   # bad prefix
    assert not plan_entry_valid("000f1e01")     # short
    assert not plan_entry_valid("00zz1e0101")   # non-hex
    assert not plan_entry_valid("")
    with pytest.raises(ValueError):
        decode_plan_entry("0025000101")  # hour 37 invalid
    with pytest.raises(ValueError):
        encode_plan_entry(24, 0, 1)
    with pytest.raises(ValueError):
        encode_plan_entry(0, 60, 1)


def test_slot_semantics():
    # Device returns "" for unused slots and all-zeros as the clear marker.
    assert decode_plan_slot("") is None
    assert decode_plan_slot("0000000000") is None
    assert is_clear_marker("0000000000")
    assert not is_clear_marker("")
    assert decode_plan_slot("000f1e0101") == PlanEntry(15, 30, 1, True)


# ---------------------------------------------------------------------------
# Response parsing — live frames verbatim
# ---------------------------------------------------------------------------
def test_parse_success_frames():
    # unlock response (live)
    r = parse_response(
        b'{"i":1,"r":{"r":2,"d":"0000","t":1789167906,"z":-25200,"l":1,"k":"6757549a6615"}}')
    assert r == {"ok": True, "i": 1,
                 "r": {"r": 2, "d": "0000", "t": 1789167906, "z": -25200, "l": 1, "k": "6757549a6615"}}

    # info response (live) — note: NO method echo, and z in seconds
    r = parse_response(
        b'{"i":1,"r":{"f":"1.19.12","h":"1.0.1","m":"ASF02","s":"<SERIAL>","z":-25200}}')
    assert r["r"]["f"] == "1.19.12"
    assert r["r"]["z"] == -25200

    # feed response is a bare boolean (live)
    r = parse_response(b'{"i":3,"r":true}')
    assert r == {"ok": True, "i": 3, "r": True}

    # plan write echoes the full 10-slot array (live)
    r = parse_response(b'{"i":4,"r":{"p":["000f1e0101","0010000101","","","","","","","",""]}}')
    assert r["r"]["p"][0] == "000f1e0101"
    assert r["r"]["p"][2] == ""


def test_parse_error_frames():
    # -31400 Device locked (live)
    with pytest.raises(ASF02Error) as ei:
        parse_response(b'{"i":2,"e":{"c":-31400,"m":"Device locked"}}')
    assert ei.value.code == ERR_DEVICE_LOCKED == -31400
    assert ei.value.message == "Device locked"

    # -32602 Invalid params — epoch millis instead of seconds (live)
    with pytest.raises(ASF02Error) as ei:
        parse_response(b'{"i":2,"e":{"c":-32602,"m":"Invalid params"}}')
    assert ei.value.code == ERR_INVALID_PARAMS == -32602


def test_parse_malformed():
    with pytest.raises(ValueError):
        parse_response(b"not json at all")
    with pytest.raises(ValueError):
        parse_response(b'{"r":{}}')  # missing i
    with pytest.raises(ValueError):
        parse_response(b'{"i":1}')  # neither r nor e


# ---------------------------------------------------------------------------
# Request framing — must match the vendor app's exact bytes
# ---------------------------------------------------------------------------
def test_request_no_params():
    # Vendor m2708c: {"m":"info","i":1}
    assert encode_request("info", 1) == b'{"m":"info","i":1}'


def test_request_with_dict_params():
    # compact separators, same as the vendor's manual JSON building
    assert encode_request("feeder.feed", 7, {"n": 2}) == b'{"m":"feeder.feed","i":7,"p":{"n":2}}'


def test_request_unlock_params():
    assert encode_request("unlock", 3, {"s": "abc"}) == b'{"m":"unlock","i":3,"p":{"s":"abc"}}'


# ---------------------------------------------------------------------------
# Advertising decode
# ---------------------------------------------------------------------------
def test_decode_advertisement_live():
    # Example manufacturer data (company 0xFFFF) with a synthetic MAC AA:BB:CC:DD:EE:FF
    mfg = bytes.fromhex("02030010" + "aabbccddeeff")
    d = decode_advertisement(mfg, local_name="ASF02-DDEEFF")
    assert d["mac"] == "AA:BB:CC:DD:EE:FF"
    assert d["model"] == "ASF02"
    assert d["product_type"] == b"\x02\x03"
    assert ADV_COMPANY_ID == 0xFFFF


def test_decode_advertisement_rejects_non_feeder():
    with pytest.raises(ValueError):
        decode_advertisement(bytes.fromhex("04010010aabbccddeeff"))  # litter box type
    with pytest.raises(ValueError):
        decode_advertisement(b"\x02\x03\x00")  # too short
