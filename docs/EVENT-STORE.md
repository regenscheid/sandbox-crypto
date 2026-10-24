# Event store

The event store keeps monitor evidence in an append-only SQLite database. Each record carries the hash of the record before it, so a record changed, removed, inserted or moved later breaks the chain, and checkpoints kept elsewhere also show records removed from its end (D74). The code is in `packages/event-store` (`sandbox_science.event_store`), standard library only, with the command `sandbox-science-events`.

Three components write to a store: the [policy middleware](POLICY-MIDDLEWARE.md), the worker service (into the middleware's store) and the [MCP proxy](MCP-CONNECTORS.md#the-mcp-proxy-as-built) (into its own). Operators' answers to permission requests go into the same stores. [Record kinds](#record-kinds) lists every kind and its writer.

## Records

| Field | Contents |
| --- | --- |
| `seq` | 1, 2, 3, … without gaps |
| `time` | When the store appended the record, in UTC with microseconds: `2026-10-11T08:00:00.000000Z` |
| `kind` | What the record is, such as `escalation`, `alert` or `monitor` ([record kinds](#record-kinds)) |
| `source` | The component that wrote it, such as `policy-middleware` |
| `payload` | A JSON object in canonical form, at most 1 MiB |
| `prev_hash` | The previous record's hash; 64 zeros for record 1 |
| `hash` | This record's hash |

Kinds and sources are 1 to 64 lowercase ASCII letters, digits, `.`, `_` or `-`, starting with a letter. Hashes are SHA-256, written as 64 lowercase hex digits. Times follow the sequence unless the system clock is set back, so order records by `seq`, not by time.

## Record kinds

There are two stores in a deployment: the middleware's (`events.db` in its state directory, which the worker's `event_store` setting also names, by default `events.db` in its `policy_store`) and the MCP proxy's (`events.db` in the proxy's state directory, inside its sandbox). These are all the kinds written to them:

| Kind | Source | Written by | Store | Described in |
| --- | --- | --- | --- | --- |
| `escalation` | `policy-middleware` | `policy_middleware.evaluator`: the first occurrence of an escalated request | Middleware | [Policy middleware](POLICY-MIDDLEWARE.md#permission-requests) |
| `escalation` | `worker` | `worker.permissions`: a provisioning operation's request for a package file set | Middleware | [Policy middleware](POLICY-MIDDLEWARE.md#permission-requests), [worker](WORKER.md#provisioning-operations) |
| `escalation` | `mcp-proxy` | `mcp_proxy.proxy`: the first occurrence of an escalated call | MCP proxy | [MCP connectors](MCP-CONNECTORS.md#the-mcp-proxy-as-built) |
| `approval` | `operator` | The `approve` command of `sandbox-science-policy-middleware` or `sandbox-science-mcp-proxy` (`policy.approvals.prepare_answer`) | Either | [Policy middleware](POLICY-MIDDLEWARE.md#permission-requests) |
| `denial` | `operator` | The `deny` command of either | Either | [Policy middleware](POLICY-MIDDLEWARE.md#permission-requests) |
| `approval`, `denial` | `workspace` | `worker.permissions`: the user's answer to a network or package request, which the workspace relays through the worker (D97) | Middleware | [Policy middleware](POLICY-MIDDLEWARE.md#permission-requests) |
| `denial` | `worker` | `worker.permissions`: a withdrawal (`withdrawn`) of a file-set request once its operation ends | Middleware | [Policy middleware](POLICY-MIDDLEWARE.md#permission-requests) |
| `approved` | `policy-middleware` | `policy_middleware.evaluator`: a request allowed by an approval or a file-set grant | Middleware | [Policy middleware](POLICY-MIDDLEWARE.md#permission-requests) |
| `approved` | `mcp-proxy` | `mcp_proxy.proxy`: a call allowed by an approval | MCP proxy | [MCP connectors](MCP-CONNECTORS.md#the-mcp-proxy-as-built) |
| `alert` | `policy-middleware` | `policy_middleware.evaluator`: an `alert` decision, or an escalation that timed out into one | Middleware | [Policy middleware](POLICY-MIDDLEWARE.md#results) |
| `alert` | `mcp-proxy` | `mcp_proxy.proxy`: the same for a call | MCP proxy | [MCP connectors](MCP-CONNECTORS.md#the-mcp-proxy-as-built) |
| `monitor` | `policy-middleware` | `policy_middleware.evaluator`: a monitor threshold crossed | Middleware | [Policy middleware](POLICY-MIDDLEWARE.md#results) |
| `mcp-call` | `mcp-proxy` | `mcp_proxy.proxy`: every message an authenticated caller sends | MCP proxy | [MCP connectors](MCP-CONNECTORS.md#the-mcp-proxy-as-built) |
| `provision.requested`, `provision.awaiting_permission`, `provision.finished` | `worker` | `worker.provision_events`, through `worker.permissions` | Middleware | [Provisioning events](PROVISION-EVENTS.md) |

Payloads are each writer's own; the pages in the last column describe them. The permission logic (`sandbox_science.policy.approvals`) reads only `escalation`, `approval` and `denial` records, whatever their source; other kinds are never requests or answers, and the worker's writer for provisioning observations refuses any kind but the three `provision.*` ones.

## Hash

```
hash = SHA-256( F("sandbox-science-event-store/v1")
              ‖ F(previous hash, as its 32 bytes)
              ‖ F(seq, as 8 bytes, unsigned big-endian)
              ‖ F(time) ‖ F(kind) ‖ F(source)
              ‖ F(payload) )

F(x) = the length of x in bytes, as 8 bytes, unsigned big-endian ‖ x
```

Text fields are UTF-8; time, kind and source are ASCII. Every field carries its length, so no two records encode to the same bytes, and the format name keeps these hashes apart from other uses of SHA-256.

Test vector: record 1 with time `2026-10-11T08:00:00.000000Z`, kind `alert`, source `policy-middleware` and payload `{"rule":"watched","sandbox_id":"sbx-0001"}` hashes 217 bytes to `7155c4ba8d22fd08663539e775ac8df53cf3d200c771acbddba28937242d1419`.

**Canonical JSON.** Payloads are written in the canonical form of RFC 8785 (JCS), restricted so that every implementation agrees: objects, arrays, strings, integers within ±(2^53 − 1), `true`, `false` and `null`. Floats are refused. In that subset the form is: no whitespace; object keys sorted by their UTF-16 code units (which differs from code point order above U+FFFF); in strings, only `"`, `\` and U+0000 to U+001F escaped, as `\b`, `\t`, `\n`, `\f`, `\r` or lowercase `\u00xx`, and everything else written as UTF-8; integers in plain decimal. Any JCS library therefore produces the same bytes, and the hash of an exported record can be recomputed from its parsed payload.

## Durability and concurrency

- **Durable before acknowledged.** The database uses write-ahead logging with `synchronous=FULL`, which syncs the log at every commit, so an append returns only once its record would survive a crash or a power loss. On macOS, `fullfsync` also flushes the drive's cache.
- **Threads and asyncio.** Within a process, appends that arrive while a commit is under way wait for it, and the next of them commits them all in one transaction (group commit). asyncio code uses `append_async`, which commits in a thread so that the event loop keeps running.
- **Processes.** Several processes on one host may append to one store. Each commit takes SQLite's write lock and reads the newest record inside the same transaction, so commits from different processes are serialized and the chain cannot fork. A process waits for the lock up to its timeout (the middleware: 250 ms; the MCP proxy: 1 s; the worker: 5 s) and then fails the append. Group commit does not span processes. The store must be on a local filesystem: the write-ahead log needs memory shared between the processes, which network filesystems do not provide.
- **Readers.** `verify` and `export` read one consistent snapshot and do not hold up appends.
- **Append-only triggers** refuse `UPDATE` and `DELETE` on the records. They guard against mistakes only, since anyone who can write the file can drop them; `verify` reports dropped triggers.

Measured on babbage (XFS on NVMe), 2,000 appends each:

| Case | Median | p99 | Max |
| --- | --- | --- | --- |
| One append | 2.1 ms | 7.1 ms | 11 ms |
| One append with `synchronous=NORMAL`, for comparison (not durable after a power loss) | 0.03 ms | 0.09 ms | 11 ms |
| 16 threads appending at once, each append | 4.6 ms | 9.2 ms | 19 ms |
| The same without group commit | 28 ms | 65 ms | 104 ms |

With group commit the 16 threads stored 3,250 records a second, against 550 without. In asyncio code, appending with `append` on the event loop delayed a 1 ms timer by 15 ms (median); `append_async` kept the delay at 0.07 ms (median; p99 0.9 ms).

## What verification shows

`verify` reads every record in order, recomputes each hash and checks each link. It also checks each record's form (sequence numbers, times, names, hashes, canonical payloads) and the schema.

The hashes are not keyed. Anyone who can write the database can change a record and recompute the hashes of every record after it, or remove the newest records; what remains is a valid chain. Only a checkpoint kept where they cannot reach shows such changes, and only for records up to the checkpoint. Nor does the chain say who wrote a record: anyone who can write the file can append records that verify.

| Change | From the store alone | Against a checkpoint at or after it |
| --- | --- | --- |
| A record changed, its hash left alone | Found: the record does not match its hash | Found |
| A record changed and its own hash recomputed | Found: the next record does not follow it (unless it is the newest) | Found |
| A record removed from the middle, inserted or moved | Found: a gap, or broken hashes and links | Found |
| Any of these, with every later hash recomputed | Not found | Found: the record at the checkpoint differs |
| The newest records removed | Not found | Found: the store ends before the checkpoint |
| Records appended by someone else | Not found | Not found |

## Checkpoints

A checkpoint is the head's sequence number and hash:

```json
{"format":"sandbox-science-event-store/v1","hash":"7155c4ba…","seq":1}
```

`sandbox-science-events checkpoint` verifies the store first and writes nothing unless it is intact, so a checkpoint never vouches for a broken chain. Keep checkpoints where the store's writers cannot change them: another machine, the orchestrator's store, or a repository the middleware's account cannot write. Take them regularly; records after the newest checkpoint have only the chain's protection.

### Checkpoints in the journal

On a single-user host such as babbage, a systemd user timer can take a checkpoint every five minutes and log it to the journal. The journal's files belong to root: the user's processes can read them, but cannot change or delete what is in them. A process running as the user, as the middleware does, could log lines that look like checkpoints, but not undo the ones logged before. A later checkpoint for the same record that disagrees with an earlier one is itself reported.

The templates are in [`examples/systemd`](../examples/systemd). Their instance is the store's path, escaped:

```bash
cp examples/systemd/sandbox-science-events-checkpoint@.* ~/.config/systemd/user/
# set the path to sandbox-science-events in the .service file, then:
STORE=/path/to/middleware/state/events.db
UNIT=sandbox-science-events-checkpoint@$(systemd-escape --path "$STORE")
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT.timer"
```

To verify the store against every checkpoint the journal holds:

```bash
journalctl --user -u "$UNIT.service" -o cat > checkpoints.jsonl
sandbox-science-events verify "$STORE" --checkpoint checkpoints.jsonl
```

A checkpoint file may hold one checkpoint, or one per line; lines that do not start with `{`, such as the journal's own messages, are skipped, and a file with no checkpoint in it is an error. A failed checkpoint (a broken chain) makes the service fail, with its problems in the journal. The journal keeps entries as long as its size limits allow, so copy the checkpoints elsewhere for longer retention. On babbage, a store rewritten with every hash recomputed passed the chain check alone and failed against the checkpoints a transient timer had logged.

## Command line

```bash
sandbox-science-events verify STATE/events.db --checkpoint saved/checkpoint-1.json
sandbox-science-events checkpoint STATE/events.db --output saved/checkpoint-2.json
sandbox-science-events export STATE/events.db --after 1200 --output events.jsonl
```

- `verify` prints each problem and exits with status 1 if there are any. `--checkpoint` can be repeated.
- `checkpoint` also takes `--checkpoint`, to check earlier checkpoints before writing a new one.
- `export` writes one canonical JSON object per line, with every field and the payload as an object, so other tools can read the records and recompute their hashes. `--after` exports only newer records. It stops at a malformed record.

```json
{"hash":"7155c4ba…","kind":"alert","payload":{"rule":"watched","sandbox_id":"sbx-0001"},"prev_hash":"0000…","seq":1,"source":"policy-middleware","time":"2026-10-11T08:00:00.000000Z"}
```

To back up a running store, export it or use SQLite's backup; a copy of `events.db` alone can miss records still in `events.db-wal`.

## Not yet done

- **Other writers.** The behavior monitor and the orchestrator's own model and fetch requests (D47) write nothing yet. The orchestrator's MCP calls are recorded by the MCP proxy, with the orchestrator as their caller.
- **Checkpoints** go to the journal only where someone installs the timer; nothing sends them off the host. The MCP proxy's store, in its sandbox, has no timer; its records leave the sandbox only through `export`.
- **Retention.** The store only grows. Rotating to a new file that starts from the old one's final checkpoint is a possible later step.
- **Several hosts.** A store is one file on one host; the worker writes to the middleware's store through a shared filesystem, and components on other hosts would need a store service.
