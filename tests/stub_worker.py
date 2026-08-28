"""A worker that speaks the protocol without needing whisper.cpp."""
import json, sys

def emit(**kw):
    sys.stdout.write(json.dumps(kw) + "\n"); sys.stdout.flush()

emit(event="ready", pid=0)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    cmd = msg.get("cmd")
    if cmd == "load":
        emit(event="loaded", model=msg["model"], cached=False, system_info="STUB")
    elif cmd == "transcribe":
        if msg.get("fail"):
            emit(event="error", message="stub failure")
            continue
        if msg.get("crash"):
            sys.exit(9)
        emit(event="segment", text="hello ")
        emit(event="segment", text="world")
        emit(event="result", text="hello world", language="en", duration_ms=42)
    elif cmd == "unload":
        emit(event="unloaded")
    elif cmd == "ping":
        emit(event="pong")
    elif cmd == "quit":
        break
