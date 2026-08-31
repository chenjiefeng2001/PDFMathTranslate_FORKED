"""PDF numeric serialization contract — 7H-2B.

We observed that MuPDF's tokenizer splits a scientific-notation token like
``-9.000000001435637e-05`` at the ``e`` and reports ``unknown keyword:
'-9.000000001435637e'``, corrupting the page content stream.  The probe (see
``tests/test_pdfnum_contract.py``) proved **both** full ``repr`` and ``%.9g``
still emit an exponent for small values:

    repr( -9e-05 )  → '-9.000000001435637e-05'   MuPDF ERROR
    format(-9e-05, ".9g") → '-9e-05'              MuPDF ERROR
    '-0.000090'                                   MuPDF OK

So ``%.9g`` is NOT a sufficient emitter contract.  The contract is:

    PDF numeric token = finite decimal, bounded precision, NO exponent.

This module is the single source of truth for that contract, so a future
emitter change cannot silently reintroduce scientific notation.

``pdf_num(v)`` serializes a finite float to a no-exponent fixed-point token that
keeps ~``SIG_DIGITS`` significant digits (capped), and coerces NaN/Inf/``-0.0``
to ``"0"`` so a bad value can never corrupt a content stream.  Any token this
module emits round-trips through MuPDF's parser (locked by the contract tests).
"""

from __future__ import annotations

__all__ = ["pdf_num", "parse_num", "SIG_DIGITS", "never_emits_exponent"]

SIG_DIGITS = 9
_MAX_FRAC = 15  # cap fraction digits so large exponents stay bounded


def pdf_num(value) -> str:
    """Serialize a finite float to a no-exponent, bounded-precision token.

    - NaN / ±Inf / non-float → ``"0"`` (a bad value must not break a stream);
    - ``-0.0`` → ``"0"``;
    - integer-valued within precision → no trailing ``.000…``;
    - never contains ``e``/``E`` (MuPDF-safe), keeps ~9 significant digits.
    """
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return "0"
    if v != v or v in (float("inf"), float("-inf")):  # NaN / ±Inf
        return "0"
    if v == 0.0:
        return "0"
    import math

    abs_v = abs(v)
    int_digits = int(math.floor(math.log10(abs_v))) + 1
    frac = SIG_DIGITS - int_digits
    frac = max(0, min(frac, _MAX_FRAC))
    token = f"{v:.{frac}f}"  # fixed-point => no exponent, by construction
    # Strip only *fractional* trailing zeros: integer zeros are significant
    # (e.g. 1e15 must stay "1000000000000000", not "1").
    if frac:  # a decimal point exists only when frac > 0
        token = token.rstrip("0").rstrip(".")
    return token or "0"


def parse_num(token: str) -> float:
    """Parse back a token the contract produced (float(token))."""
    return float(token)


def never_emits_exponent(token: str) -> bool:
    """Contract invariant: a serialized token must contain no exponent marker."""
    return not any(ch in token for ch in "eE")
