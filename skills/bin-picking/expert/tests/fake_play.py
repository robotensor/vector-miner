import json
import os
import sys
import time

job = json.loads(sys.stdin.read())
pids = os.environ.get("FAKE_EXPERT_PIDS")
if pids:
    with open(os.path.join(pids, str(os.getpid())), "w") as f:
        f.write(json.dumps(job["index"]))
mode, secs = job["expert"]["command"][1], float(job["expert"]["command"][2])
sc = job["scene"]
if mode == "hang":
    time.sleep(3600)
if mode == "crash":
    sys.stderr.write("the harness broke\n")
    sys.exit(3)
time.sleep(secs)
score = round(0.1 * (job["index"] + 1), 6)
claim = {"scene_id": sc["scene_id"], "outcome": "done", "success": False, "error": None, "infra": False, "ctrl": "",
         "n": 0, "act_rows": [], "dropped_ctrl": "", "action_hash": "cbf29ce484222325", "rows_sent": 0, "t": 0.0,
         "tick": 0, "outs": 0, "out_parts": [], "tcp_path_m": 0.0, "joint_path_rad": 0.0, "crate_hits": 0,
         "target_class": None, "score": score, "expert_s": 0.0, "wall_s": secs, "picks": [], "picks_planned": 10,
         "successes": 0}
print(json.dumps({"result": claim, "errors": ["pick 0 score 1 is not 0.0"] if mode == "bad" else []}))
