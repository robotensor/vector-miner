import json
import os
import random
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "track"
per = int(sys.argv[2]) if len(sys.argv) > 2 else 5
pids = os.environ.get("FAKE_EXPERT_PIDS")
if pids:
    with open(os.path.join(pids, str(os.getpid())), "w") as f:
        f.write("")
arm = None


def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def rows(m):
    q = [m["qpos"][i] for i in arm]
    k = int(m["tick"])
    out = [[q[j] + 0.003 * ((k + r + j) % 7) - 0.009 for j in range(6)] + [0.0] for r in range(per)]
    if mode == "noisy":
        out = [[x + random.uniform(-1e-6, 1e-6) for x in r] for r in out]
    return out


for line in sys.stdin:
    m = json.loads(line)
    if "scene_key" in m:
        send({"type": "error", "error": "the bridge leaked scene_key"})
        continue
    if m["type"] == "end":
        if pids:
            with open(os.path.join(pids, f"{os.getpid()}.end"), "w") as f:
                json.dump(m, f)
        break
    if m["type"] == "start":
        arm = m["scene"]["meta"]["robot"]["arm_joints_qpos"]
        if mode == "crash":
            sys.exit(3)
        if mode == "junk":
            print("hello", flush=True)
            continue
        if mode == "hang":
            time.sleep(3600)
    send({"type": "act", "rows": rows(m), "obs": False})
