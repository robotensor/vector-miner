#!/usr/bin/env node
// An almost blind vector-expert/2 expert. It shows the message loop with the harness and nothing else: it never
// looks at the parts or the target, it only reads the arm's start pose, then lowers the arm a little, closes the
// gripper and goes back up. Most scenes end without a pick. Replace plan() with your own planner; @vector/sim
// (installed by package.json) gives you the cell, the IK and runPlan (the vector-sim README, section API).
//
//   harness -> {"type":"start", scene, checkpoint, target, qpos, qvel, obs, limits}
//   expert  -> {"type":"act", "rows": [[q1..q6, grip], ...], "obs": false}
//   harness -> {"type":"state", tick, t, qpos, qvel}            (after each act, until the verdict)
//   expert  -> {"type":"act", "rows": []}                      (no rows: hold the last row until the verdict)
//   harness -> {"type":"end", outcome, t, tick}
//
// One JSON object per line; stdout carries only protocol messages (log to stderr). Deadlines: 10 s for the first
// act, 2 s for each later one. The same messages must always give the same rows (no clocks, no unseeded random).
import readline from 'node:readline';

const CHUNK = Number(process.env.EXPERT_CHUNK || 20); // rows per act; one row is one tick (1/20 s) of robot time
const DOWN = [0, 0.15, -0.15, 0, 0, 0]; // a fixed joint offset (rad) from the start pose, the same for every scene

// A row is 6 arm joint targets (rad) and the grip, 0 (open) to 1 (closed).
function plan(start) {
  const q0 = start.scene.meta.robot.arm_joints_qpos.map((i) => start.qpos[i]);
  const q1 = q0.map((v, k) => v + DOWN[k]);
  const rows = [];
  const move = (a, b, n, grip) => { for (let i = 1; i <= n; i++) rows.push([...a.map((v, k) => v + (b[k] - v) * i / n), grip]); };
  move(q0, q1, 30, 0); // 1.5 s down
  for (let i = 0; i < 12; i++) rows.push([...q1, 1]); // 0.6 s closing the gripper
  move(q1, q0, 30, 1); // 1.5 s back up
  return rows.slice(0, start.limits.max_rows);
}

const send = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');
let rows = [];
const next = () => send({ type: 'act', rows: rows.splice(0, CHUNK), obs: false });

for await (const line of readline.createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  if (!line.trim()) continue;
  const msg = JSON.parse(line);
  try {
    if (msg.type === 'start') {
      process.stderr.write(`start: scene ${msg.scene.scene_id}, target ${msg.target}, camera ${msg.obs.width}x${msg.obs.height}\n`);
      rows = plan(msg);
      next();
    } else if (msg.type === 'state') {
      next(); // [] once the rows are used up: the harness holds the last row until the verdict
    } else if (msg.type === 'end') {
      process.stderr.write(`end: ${msg.outcome} at t=${msg.t}\n`);
      break;
    }
  } catch (e) {
    send({ type: 'error', error: String(e?.message || e) }); // gives up on this scene
  }
}
process.exit(0);
