import os, struct, sys, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from audio import _rms


def pcm(samples):
    return b"".join(struct.pack("<f", s) for s in samples)


def test_silence_is_zero():
    assert _rms(pcm([0.0] * 128)) == 0.0


def test_empty_buffer_is_safe():
    assert _rms(b"") == 0.0
    assert _rms(b"\x00\x00") == 0.0


def test_full_scale_is_one():
    assert _rms(pcm([1.0, -1.0] * 64)) == 1.0


def test_louder_reads_higher():
    quiet = _rms(pcm([0.01, -0.01] * 64))
    loud = _rms(pcm([0.5, -0.5] * 64))
    assert 0.0 < quiet < loud < 1.0


def test_result_always_in_range():
    for amp in (1e-9, 1e-3, 0.1, 0.9, 1.0, 5.0):
        v = _rms(pcm([amp, -amp] * 32))
        assert 0.0 <= v <= 1.0, f"amp={amp} gave {v}"


def test_truncated_trailing_bytes_ignored():
    # A buffer that is not a whole number of float32 frames must not raise.
    assert 0.0 <= _rms(pcm([0.3] * 10) + b"\x01\x02") <= 1.0
