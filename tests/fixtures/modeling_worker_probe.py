"""Controlled standalone child for real process ownership and pipe-failure tests."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--mode", required=True)
parser.add_argument("--job", required=True)
args = parser.parse_args()
directory = Path(args.job).resolve().parent
marker = {
    "pid": os.getpid(),
    "secret_seen": any(
        name in os.environ for name in ("DATABASE_URL", "CLICKHOUSE_PASSWORD", "REDIS_URL")
    ),
}
(directory / "started.json").write_text(json.dumps(marker), encoding="utf-8")
if args.mode == "block":
    while True:
        (directory / "heartbeat").write_text(str(time.monotonic()), encoding="utf-8")
        time.sleep(0.05)
elif args.mode == "noise":
    sys.stdout.buffer.write(b"x" * 200_000)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(b"y" * 200_000)
    sys.stderr.buffer.flush()
    raise SystemExit(2)
elif args.mode == "invalid":
    (directory / "result.json").write_bytes(b"{}\n")
    sys.stdout.buffer.write(b'{"status":"succeeded"}\n')
else:
    sys.stdout.buffer.write(b'{"code":"model_training_failed","status":"failed"}\n')
    raise SystemExit(2)
