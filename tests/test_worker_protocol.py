"""The worker's stdout carries protocol lines and nothing else."""
import json, os, subprocess, sys

WORKER = os.path.join(os.path.dirname(__file__), "..", "src", "worker.py")
SRC = os.path.dirname(WORKER)


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], cwd=SRC, capture_output=True, text=True,
        timeout=30,
    )


def test_stray_prints_go_to_stderr_not_the_protocol():
    r = run(
        "import worker, sys, os\n"
        "worker._protect_stdout()\n"
        "print('junk from a library')\n"
        "os.write(1, b'raw junk without newline')\n"
        "worker.emit(event='pong')\n"
    )
    assert r.returncode == 0, r.stderr
    assert [json.loads(l) for l in r.stdout.splitlines()] == [{"event": "pong"}]
    assert "junk from a library" in r.stderr
    assert "raw junk" in r.stderr


def test_ping_round_trip_over_the_real_worker():
    r = subprocess.run(
        [sys.executable, WORKER], input='{"cmd": "ping"}\n{"cmd": "quit"}\n',
        capture_output=True, text=True, timeout=30,
    )
    events = [json.loads(l)["event"] for l in r.stdout.splitlines()]
    assert events == ["ready", "pong"]
