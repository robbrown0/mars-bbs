"""Minimal binding to the distribution-provided libsodium password API."""

from __future__ import annotations

import ctypes
import ctypes.util

LIB_PATH = ctypes.util.find_library("sodium")
if not LIB_PATH:
    raise RuntimeError("libsodium unavailable")
LIB = ctypes.CDLL(LIB_PATH)
LIB.sodium_init.restype = ctypes.c_int
LIB.crypto_pwhash_str_alg.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                      ctypes.c_ulonglong, ctypes.c_ulonglong,
                                      ctypes.c_size_t, ctypes.c_int]
LIB.crypto_pwhash_str_alg.restype = ctypes.c_int
LIB.crypto_pwhash_str_verify.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                         ctypes.c_ulonglong]
LIB.crypto_pwhash_str_verify.restype = ctypes.c_int

STR_BYTES = 128
OPSLIMIT = 2
MEMLIMIT = 19 * 1024 * 1024
ARGON2ID13 = 2

if LIB.sodium_init() < 0:
    raise RuntimeError("libsodium initialization failed")


def hash_password(clear: str) -> str:
    encoded = clear.encode("utf-8")
    output = ctypes.create_string_buffer(STR_BYTES)
    rc = LIB.crypto_pwhash_str_alg(output, encoded, len(encoded), OPSLIMIT,
                                   MEMLIMIT, ARGON2ID13)
    if rc != 0:
        raise RuntimeError("password hashing failed")
    return output.value.decode("ascii")


def verify_password(stored: str, clear: str) -> bool:
    try:
        return LIB.crypto_pwhash_str_verify(stored.encode("ascii"),
                                            clear.encode("utf-8"),
                                            len(clear.encode("utf-8"))) == 0
    except (UnicodeError, ValueError):
        return False
