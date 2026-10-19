import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { handleScheduled, slotId } from "./worker.mjs";

const DAY = "2,17,32,47 0-12,23 * * *";
const START = 1792147620000;

function fixture(t) {
  const sqlite = new DatabaseSync(":memory:");
  sqlite.exec(readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));
  t.after(() => sqlite.close());
  const DB = {
    prepare(sql) {
      const statement = sqlite.prepare(sql);
      let args = [];
      return {
        bind(...values) {
          args = values;
          return this;
        },
        async run() {
          const result = statement.run(...args);
          return { success: true, meta: { changes: Number(result.changes) }, results: [] };
        },
        async first() {
          return statement.get(...args) ?? null;
        },
        async all() {
          return { success: true, results: statement.all(...args), meta: {} };
        },
      };
    },
  };
  return {
    sqlite,
    env: {
      DB,
      DISPATCH_ENABLED: "true",
      GITHUB_TOKEN: "synthetic-test-credential",
      GITHUB_REPOSITORY: "example/monitor-runner",
    },
    controller: { cron: DAY, scheduledTime: START },
    deps: { now: () => START + 5000, log: () => {} },
    row(id) {
      return sqlite.prepare("SELECT * FROM dispatch_slots WHERE slot_id = ?").get(id);
    },
  };
}

function staleSlot(f, overrides = {}) {
  const row = {
    slot_id: "syokugan-29869112-day",
    cron: DAY,
    scheduled_minute: 29869112,
    status: "failed",
    attempts: 1,
    lease_owner: "previous-owner",
    lease_until: START - 600000,
    created_at: START - 900000,
    updated_at: START - 900000,
    ...overrides,
  };
  f.sqlite.prepare(
    `INSERT INTO dispatch_slots
     (slot_id, cron, scheduled_minute, status, attempts, lease_owner,
      lease_until, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(...Object.values(row));
  return row.slot_id;
}

test("duplicate events a few seconds apart share a minute key", () => {
  assert.equal(slotId({ cron: DAY, scheduledTime: START }), "syokugan-29869127-day");
  assert.equal(
    slotId({ cron: DAY, scheduledTime: START + 4000 }),
    "syokugan-29869127-day",
  );
  assert.throws(() => slotId({ cron: "unknown", scheduledTime: START }));
});

test("concurrent invocations issue one dispatch with the correlation input", async (t) => {
  const f = fixture(t);
  const requests = [];
  f.deps.fetch = async (url, options) => {
    requests.push({ url, options });
    return new Response(null, { status: 204 });
  };
  await Promise.all(
    Array.from({ length: 20 }, (_, i) =>
      handleScheduled(
        { ...f.controller, scheduledTime: START + (i % 5) * 1000 },
        f.env,
        f.deps,
      ),
    ),
  );
  assert.equal(requests.length, 1);
  assert.equal(requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    ref: "main",
    inputs: { mode: "monitor", slot_id: "syokugan-29869127-day" },
  });
  assert.equal(f.row("syokugan-29869127-day").status, "dispatched");
});

test("different scheduled minutes both dispatch", async (t) => {
  const f = fixture(t);
  let calls = 0;
  f.deps.fetch = async () => {
    calls += 1;
    return new Response(null, { status: 204 });
  };
  await handleScheduled(f.controller, f.env, f.deps);
  await handleScheduled(
    { ...f.controller, scheduledTime: START + 15 * 60000 },
    f.env,
    { ...f.deps, now: () => START + 15 * 60000 },
  );
  assert.equal(calls, 2);
});

test("dispatch uses a Cloudflare-supported non-following redirect mode", async (t) => {
  const f = fixture(t);
  f.deps.fetch = async (_url, options) => {
    if (!["follow", "manual"].includes(options.redirect)) {
      throw new TypeError("Unsupported Workers redirect mode");
    }
    assert.equal(options.redirect, "manual");
    return new Response(null, { status: 204 });
  };
  const result = await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(result.status, "dispatched");
  assert.equal(f.row("syokugan-29869127-day").status, "dispatched");
});

test("reconciliation supports Workers without following credential redirects", async (t) => {
  const f = fixture(t);
  const id = staleSlot(f, { status: "uncertain" });
  f.deps.fetch = async (_url, options) => {
    if (options.method === "GET") {
      if (!["follow", "manual"].includes(options.redirect)) {
        throw new TypeError("Unsupported Workers redirect mode");
      }
      assert.equal(options.redirect, "manual");
      return Response.json({
        total_count: 1,
        workflow_runs: [{ id: 456, display_title: `monitor ${id}` }],
      });
    }
    return new Response(null, { status: 204 });
  };
  await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(f.row(id).status, "dispatched");
  assert.equal(f.row(id).github_run_id, "456");
  assert.equal(f.row(id).attempts, 1);
});

test("a dispatch redirect remains uncertain and is not accepted", async (t) => {
  const f = fixture(t);
  f.deps.fetch = async (_url, options) => {
    assert.equal(options.redirect, "manual");
    return new Response(null, {
      status: 302,
      headers: { Location: "https://example.invalid/not-followed" },
    });
  };
  const result = await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(result.status, "uncertain");
  assert.equal(f.row("syokugan-29869127-day").error_code, "http_302");
  assert.equal(f.row("syokugan-29869127-day").attempts, 1);
});

test("default fetch keeps the Workers receiver for dispatch and reconciliation", async (t) => {
  const f = fixture(t);
  const id = staleSlot(f, { status: "uncertain" });
  t.mock.method(globalThis, "fetch", async function (_url, options) {
    if (this !== undefined && this !== globalThis) {
      throw new TypeError("Illegal invocation: incorrect this reference");
    }
    if (options.method === "GET") {
      return Response.json({
        total_count: 1,
        workflow_runs: [{ id: 789, display_title: `monitor ${id}` }],
      });
    }
    return new Response(null, { status: 204 });
  });
  const result = await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(result.status, "dispatched");
  assert.equal(f.row(id).status, "dispatched");
  assert.equal(f.row(id).github_run_id, "789");
  assert.equal(f.row(id).attempts, 1);
});

test("disabled scheduler does not access missing bindings or dispatch", async () => {
  const result = await handleScheduled({}, { DISPATCH_ENABLED: "false" }, {
    log: () => {},
    fetch: () => assert.fail("disabled scheduler dispatched"),
  });
  assert.equal(result.status, "disabled");
});

test("missing credentials fail before claiming or dispatching", async (t) => {
  const f = fixture(t);
  delete f.env.GITHUB_TOKEN;
  await assert.rejects(handleScheduled(f.controller, f.env, f.deps), /configuration/i);
  assert.equal(f.sqlite.prepare("SELECT count(*) AS n FROM dispatch_slots").get().n, 0);
});

test("HTTP rejection remains recoverable and does not expose response data", async (t) => {
  const f = fixture(t);
  const logs = [];
  f.deps.log = (entry) => logs.push(entry);
  f.deps.fetch = async () =>
    new Response("private server detail synthetic-test-credential", { status: 403 });
  await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(f.row("syokugan-29869127-day").status, "failed");
  assert.equal(f.row("syokugan-29869127-day").error_code, "http_403");
  assert.doesNotMatch(JSON.stringify(logs), /private server detail|synthetic-test-credential/);
});

test("uncertain dispatch is reconciled to an existing run without resending", async (t) => {
  const f = fixture(t);
  f.deps.fetch = async () => {
    throw new Error("synthetic-test-credential network failed");
  };
  await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(f.row("syokugan-29869127-day").status, "uncertain");
  let oldSlotPosts = 0;
  f.deps.fetch = async (_url, options) => {
    if (options.method === "GET") {
      return Response.json({
        total_count: 1,
        workflow_runs: [{ id: 123, display_title: "monitor syokugan-29869127-day" }],
      });
    }
    if (JSON.parse(options.body).inputs.slot_id === "syokugan-29869127-day") {
      oldSlotPosts += 1;
    }
    return new Response(null, { status: 204 });
  };
  await handleScheduled(
    { ...f.controller, scheduledTime: START + 15 * 60000 },
    f.env,
    { ...f.deps, now: () => START + 15 * 60000 },
  );
  assert.equal(oldSlotPosts, 0);
  assert.equal(f.row("syokugan-29869127-day").status, "dispatched");
  assert.equal(f.row("syokugan-29869127-day").github_run_id, "123");
});

test("recovery retries a rejected dispatch only after a complete empty run lookup", async (t) => {
  const f = fixture(t);
  f.deps.fetch = async () => new Response(null, { status: 429 });
  await handleScheduled(f.controller, f.env, f.deps);
  const actions = [];
  f.deps.fetch = async (_url, options) => {
    if (options.method === "GET") {
      actions.push("lookup");
      return Response.json({ total_count: 0, workflow_runs: [] });
    }
    const id = JSON.parse(options.body).inputs.slot_id;
    actions.push(id);
    return new Response(null, { status: 204 });
  };
  await handleScheduled(
    { ...f.controller, scheduledTime: START + 15 * 60000 },
    f.env,
    { ...f.deps, now: () => START + 15 * 60000 },
  );
  assert.ok(actions.indexOf("lookup") < actions.indexOf("syokugan-29869127-day"));
  assert.equal(f.row("syokugan-29869127-day").status, "dispatched");
  assert.equal(f.row("syokugan-29869127-day").attempts, 2);
});

test("failed reconciliation cannot cause a blind resend", async (t) => {
  const f = fixture(t);
  f.deps.fetch = async () => {
    throw new Error("dispatch response lost");
  };
  await handleScheduled(f.controller, f.env, f.deps);
  let oldSlotPosts = 0;
  f.deps.fetch = async (_url, options) => {
    if (options.method === "GET") return new Response(null, { status: 503 });
    if (JSON.parse(options.body).inputs.slot_id === "syokugan-29869127-day") oldSlotPosts += 1;
    return new Response(null, { status: 204 });
  };
  await handleScheduled(
    { ...f.controller, scheduledTime: START + 15 * 60000 },
    f.env,
    { ...f.deps, now: () => START + 15 * 60000 },
  );
  assert.equal(oldSlotPosts, 0);
  assert.equal(f.row("syokugan-29869127-day").status, "uncertain");
});

test("recovery checks later pages before retrying", async (t) => {
  const f = fixture(t);
  f.deps.fetch = async () => {
    throw new Error("response lost");
  };
  await handleScheduled(f.controller, f.env, f.deps);
  let oldSlotPosts = 0;
  f.deps.fetch = async (url, options) => {
    if (options.method === "GET") {
      const page = new URL(url).searchParams.get("page");
      return Response.json({
        total_count: 101,
        workflow_runs: page === "1"
          ? Array.from({ length: 100 }, (_, i) => ({ id: i + 1, display_title: `other ${i}` }))
          : [{ id: 101, display_title: "monitor syokugan-29869127-day" }],
      });
    }
    if (JSON.parse(options.body).inputs.slot_id === "syokugan-29869127-day") oldSlotPosts += 1;
    return new Response(null, { status: 204 });
  };
  await handleScheduled(
    { ...f.controller, scheduledTime: START + 15 * 60000 },
    f.env,
    { ...f.deps, now: () => START + 15 * 60000 },
  );
  assert.equal(oldSlotPosts, 0);
  assert.equal(f.row("syokugan-29869127-day").github_run_id, "101");
});

test("a recovered owner that loses its lease cannot POST", async (t) => {
  const f = fixture(t);
  const id = staleSlot(f);
  let oldPosts = 0;
  f.deps.fetch = async (_url, options) => {
    if (options.method === "GET") {
      f.sqlite.prepare(
        "UPDATE dispatch_slots SET lease_owner = ?, lease_until = ? WHERE slot_id = ?",
      ).run("replacement-owner", START + 600000, id);
      return Response.json({ total_count: 0, workflow_runs: [] });
    }
    if (JSON.parse(options.body).inputs.slot_id === id) oldPosts += 1;
    return new Response(null, { status: 204 });
  };
  await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(oldPosts, 0);
  assert.equal(f.row(id).lease_owner, "replacement-owner");
  assert.equal(f.row(id).attempts, 1);
});

test("reconciliation that outlives its lease cannot authorize a POST", async (t) => {
  const f = fixture(t);
  const id = staleSlot(f);
  let now = START + 5000;
  f.deps.now = () => now;
  let oldPosts = 0;
  f.deps.fetch = async (_url, options) => {
    if (options.method === "GET") {
      now += 180001;
      return Response.json({ total_count: 0, workflow_runs: [] });
    }
    if (JSON.parse(options.body).inputs.slot_id === id) oldPosts += 1;
    return new Response(null, { status: 204 });
  };
  await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(oldPosts, 0);
  assert.equal(f.row(id).attempts, 1);
});

test("each recovered row has a fresh lease through a slow paginated lookup", async (t) => {
  const f = fixture(t);
  for (let i = 0; i < 3; i += 1) {
    staleSlot(f, { slot_id: `older-${i}`, created_at: START - 900000 - i });
  }
  let now = START + 5000;
  f.deps.now = () => now;
  const leases = [];
  f.deps.fetch = async (url, options) => {
    if (options.method === "GET") {
      now += 19000;
      const page = Number(new URL(url).searchParams.get("page"));
      return Response.json({
        total_count: 401,
        workflow_runs: Array.from({ length: page < 5 ? 100 : 1 }, (_, i) => ({
          id: (page - 1) * 100 + i + 1,
          display_title: `unrelated ${(page - 1) * 100 + i + 1}`,
        })),
      });
    }
    const id = JSON.parse(options.body).inputs.slot_id;
    if (id.startsWith("older-")) {
      const row = f.row(id);
      leases.push({ until: row.lease_until, started: now, status: row.status });
    }
    now += 19000;
    return new Response(null, { status: 204 });
  };
  await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(leases.length, 3);
  for (const lease of leases) {
    assert.ok(lease.until > lease.started + 20000, "POST must have an unexpired lease");
    assert.equal(lease.status, "pending", "in-flight POST must not look rejected");
  }
});

test("changing pagination cannot authorize retry after a rejection", async (t) => {
  const f = fixture(t);
  const id = staleSlot(f);
  let oldPosts = 0;
  f.deps.fetch = async (url, options) => {
    if (options.method === "GET") {
      const page = Number(new URL(url).searchParams.get("page"));
      return Response.json({
        total_count: page === 1 ? 101 : 102,
        workflow_runs: Array.from({ length: page === 1 ? 100 : 2 }, (_, i) => ({
          id: (page - 1) * 100 + i + 1,
          display_title: `unrelated ${(page - 1) * 100 + i + 1}`,
        })),
      });
    }
    if (JSON.parse(options.body).inputs.slot_id === id) oldPosts += 1;
    return new Response(null, { status: 204 });
  };
  await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(oldPosts, 0);
  assert.equal(f.row(id).attempts, 1);
});

test("malformed correlation records cannot authorize a retry", async (t) => {
  const f = fixture(t);
  const id = staleSlot(f);
  let oldPosts = 0;
  f.deps.fetch = async (_url, options) => {
    if (options.method === "GET") {
      return Response.json({ total_count: 1, workflow_runs: [{}] });
    }
    if (JSON.parse(options.body).inputs.slot_id === id) oldPosts += 1;
    return new Response(null, { status: 204 });
  };
  await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(oldPosts, 0);
  assert.equal(f.row(id).attempts, 1);
});

for (const status of ["pending", "uncertain"]) {
  test(`absence cannot authorize retry of an ambiguous ${status} attempt`, async (t) => {
    const f = fixture(t);
    const id = staleSlot(f, { status });
    let oldPosts = 0;
    f.deps.fetch = async (_url, options) => {
      if (options.method === "GET") {
        return Response.json({ total_count: 0, workflow_runs: [] });
      }
      if (JSON.parse(options.body).inputs.slot_id === id) oldPosts += 1;
      return new Response(null, { status: 204 });
    };
    await handleScheduled(f.controller, f.env, f.deps);
    assert.equal(oldPosts, 0);
    assert.equal(f.row(id).status, "uncertain");
    assert.equal(f.row(id).attempts, 1);
  });
}

test("the last allowed attempt is still reconciled before stopping recovery", async (t) => {
  const f = fixture(t);
  const id = staleSlot(f, { status: "uncertain", attempts: 3 });
  f.deps.fetch = async (_url, options) => {
    if (options.method === "GET") {
      return Response.json({
        total_count: 1,
        workflow_runs: [{ id: 9001, display_title: `monitor ${id}` }],
      });
    }
    return new Response(null, { status: 204 });
  };
  await handleScheduled(f.controller, f.env, f.deps);
  assert.equal(f.row(id).status, "dispatched");
  assert.equal(f.row(id).github_run_id, "9001");
});
