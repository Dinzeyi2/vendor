// Rapier worker: 3-D rigid-body simulation via the official @dimforge/rapier3d-compat binding.
// Same JSON-lines protocol as app/worker_main.py. Protocol messages go to stdout;
// console.* is redirected to stderr so library output cannot corrupt the stream.
import RAPIER from "@dimforge/rapier3d-compat";
import readline from "node:readline";

for (const k of ["log", "info", "warn", "debug"]) console[k] = (...a) => process.stderr.write(a.join(" ") + "\n");
const send = (m) => process.stdout.write(JSON.stringify(m) + "\n");

class InvalidInput extends Error {}
const fail = (msg) => { throw new InvalidInput(msg); };
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const vec3 = (v, name, dflt) => {
  if (v === undefined) return dflt;
  if (!Array.isArray(v) || v.length !== 3 || !v.every(isNum)) fail(`'${name}' must be [x, y, z]`);
  return v;
};
const num = (v, name, dflt, lo, hi) => {
  if (v === undefined) return dflt;
  if (!isNum(v) || v < lo || v > hi) fail(`'${name}' must be a number between ${lo} and ${hi}`);
  return v;
};
const int = (v, name, dflt, lo, hi) => {
  const n = num(v, name, dflt, lo, hi);
  if (!Number.isInteger(n)) fail(`'${name}' must be an integer`);
  return n;
};

function collider(shape, i) {
  if (!shape || typeof shape !== "object") fail(`bodies[${i}].shape is required`);
  switch (shape.type) {
    case "ball": return RAPIER.ColliderDesc.ball(num(shape.radius, `bodies[${i}].shape.radius`, undefined, 1e-4, 1e4));
    case "cuboid": {
      const h = vec3(shape.half_extents, `bodies[${i}].shape.half_extents`);
      if (!h || h.some((x) => x <= 0)) fail(`bodies[${i}].shape.half_extents must be positive`);
      return RAPIER.ColliderDesc.cuboid(...h);
    }
    case "capsule": return RAPIER.ColliderDesc.capsule(
      num(shape.half_height, `bodies[${i}].shape.half_height`, undefined, 1e-4, 1e4),
      num(shape.radius, `bodies[${i}].shape.radius`, undefined, 1e-4, 1e4));
    default: fail(`bodies[${i}].shape.type must be ball, cuboid or capsule`);
  }
}

function simulate(inputs) {
  const bodies = inputs.bodies;
  if (!Array.isArray(bodies) || bodies.length === 0) fail("'bodies' must be a non-empty list");
  if (bodies.length > 2000) fail("'bodies' has more than 2000 items");
  const steps = int(inputs.steps, "steps", 120, 1, 100000);
  const dt = num(inputs.timestep, "timestep", 1 / 60, 1e-4, 0.1);
  const recordEvery = int(inputs.record_every, "record_every", 0, 0, 100000);
  if (recordEvery && (steps / recordEvery) * bodies.length > 200000) fail("trajectory too large; raise 'record_every'");

  const world = new RAPIER.World(new RAPIER.Vector3(...vec3(inputs.gravity, "gravity", [0, -9.81, 0])));
  world.timestep = dt;
  try {
    const handles = bodies.map((b, i) => {
      if (!b || typeof b !== "object") fail(`bodies[${i}] must be an object`);
      const kind = b.type ?? "dynamic";
      const desc = kind === "dynamic" ? RAPIER.RigidBodyDesc.dynamic()
        : kind === "fixed" ? RAPIER.RigidBodyDesc.fixed()
        : kind === "kinematic" ? RAPIER.RigidBodyDesc.kinematicVelocityBased()
        : fail(`bodies[${i}].type must be dynamic, fixed or kinematic`);
      desc.setTranslation(...vec3(b.position, `bodies[${i}].position`, [0, 0, 0]));
      desc.setLinvel(...vec3(b.linvel, `bodies[${i}].linvel`, [0, 0, 0]));
      desc.setAngvel(new RAPIER.Vector3(...vec3(b.angvel, `bodies[${i}].angvel`, [0, 0, 0])));
      const body = world.createRigidBody(desc);
      const col = collider(b.shape, i)
        .setRestitution(num(b.restitution, `bodies[${i}].restitution`, 0, 0, 1))
        .setFriction(num(b.friction, `bodies[${i}].friction`, 0.5, 0, 10))
        .setDensity(num(b.density, `bodies[${i}].density`, 1, 1e-6, 1e6));
      world.createCollider(col, body);
      return { id: b.id ?? String(i), body };
    });

    const state = () => handles.map(({ id, body }) => {
      const t = body.translation(), r = body.rotation(), v = body.linvel();
      return { id, position: [t.x, t.y, t.z], rotation: [r.x, r.y, r.z, r.w],
               linvel: [v.x, v.y, v.z], sleeping: body.isSleeping() };
    });
    const trajectory = [];
    for (let s = 1; s <= steps; s++) {
      world.step();
      if (recordEvery && s % recordEvery === 0) trajectory.push({ step: s, time: s * dt, bodies: state() });
    }
    return { steps, timestep: dt, simulated_time: steps * dt, bodies: state(),
             ...(recordEvery ? { trajectory } : {}), rapier_version: RAPIER.version() };
  } finally {
    world.free();
  }
}

const OPERATIONS = { "rapier.simulate": simulate };

await RAPIER.init();
send({ ready: true, operations: Object.keys(OPERATIONS) });

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let req;
  try { req = JSON.parse(line); } catch { return; }
  try {
    const fn = OPERATIONS[req.op];
    if (!fn) fail(`unknown operation '${req.op}'`);
    send({ id: req.id, ok: true, output: fn(req.inputs ?? {}) });
  } catch (e) {
    send({ id: req.id, ok: false, error_type: e instanceof InvalidInput ? "invalid_input" : "operation_failed",
           error: String(e?.message ?? e).slice(0, 2000) });
  }
});
rl.on("close", () => process.exit(0));
