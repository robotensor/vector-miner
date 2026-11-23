import json
import os
import sys

args = sys.argv[1:]
log = os.environ.get("FAKE_DOCKER_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(args) + "\n")
if args[:1] == ["kill"]:
    sys.exit(0)
if args[:1] == ["version"]:
    print("27.0.0")
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    print(1000)
    sys.exit(0)
if args[:1] == ["run"]:
    here = os.path.dirname(os.path.abspath(__file__))
    os.execv(sys.executable, [sys.executable, os.path.join(here, "fake_expert.py"), "track", "3"])
sys.exit(1)
