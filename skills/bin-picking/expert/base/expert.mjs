#!/usr/bin/env node
// Placeholder vector-expert/2 and /3 expert. It shows the message loop with the harness and nothing else: it holds
// the arm's pose with the gripper open, so every pick ends `timed_out`. Replace plan() with your own planner.
//
//   harness -> {"type":"start", format, scene, checkpoint, target, pick, picks, qpos, qvel, obs, limits}
//   expert  -> {"type":"act", "rows": [[q1..q6, grip], ...], "obs": false}
//   harness -> {"type":"state", pick, tick, t, qpos, qvel}      (after each act, until the pick's verdict)
//   expert  -> {"type":"act", "rows": []}                      (no rows: hold the last row until the verdict)
//   harness -> {"type":"pick", pick, outcome, t, tick, outs, next: {pick, target} | null, qpos, qvel, obs}
//              (vector-expert/3: after each pick; the next pick starts from where the robot is)
//   harness -> {"type":"end", outcome, t, tick}                 (after the last pick)
//
// One JSON object per line; stdout carries only protocol messages (log to stderr). Deadlines: 10 s for the first
// act, 2 s for each later one. The same messages must always give the same rows (no clocks, no unseeded random).
import readline from 'node:readline';

const CHUNK = 10; // rows per act; one row is one tick (1/20 s) of robot time

// A row is 6 arm joint targets (rad) and the grip, 0 (open) to 1 (closed).
function plan(start, msg) {
  const arm = start.scene.meta.robot.arm_joints_qpos.map((i) => msg.qpos[i]);
  return Array.from({ length: 40 }, () => [...arm, 0]); // 2 s of holding still
}

const send = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');
let rows = [], start = null;
const sendNext = () => send({ type: 'act', rows: rows.splice(0, CHUNK), obs: false });

for await (const line of readline.createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  if (!line.trim()) continue;
  const msg = JSON.parse(line);
  try {
    if (msg.type === 'start') {
      start = msg;
      rows = plan(start, msg);
      sendNext();
    } else if (msg.type === 'state') {
      sendNext();
    } else if (msg.type === 'pick') {
      if (msg.next) { rows = plan(start, msg); sendNext(); } // the next target: msg.next.target
    } else if (msg.type === 'end') {
      process.stderr.write(`end: ${msg.outcome} at t=${msg.t}\n`);
      break;
    }
  } catch (e) {
    send({ type: 'error', error: String(e?.message || e) }); // gives up on this scene
  }
}
process.exit(0);
