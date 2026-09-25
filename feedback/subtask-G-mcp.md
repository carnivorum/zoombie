# Subtask G — the MCP facade

Plan: [`plans/zoombie-image-pipeline-rework.md`](../plans/zoombie-image-pipeline-rework.md) §11
(§1.1 the receipt protocol, §3 the settled 413 root cause, §4 design foundations, §9 the
`data.next` + attach-cap contract reused here, §14 the envelope non-goal, §15 the one open
premise).

Mode: implementation, **G only**. G is the plan's last step; nothing beyond it was started.

* Subtask start (UTC): **2026-09-25T21:32Z**
* Receipt written (UTC): **2026-09-25T21:40Z** (newer than start)

Result: **GREEN — server side.** `python -m pytest tests` → **912 passed** (floor was
**841**, +71). The one thing this subtask **cannot** prove is stated in §7: whether a real
MCP *client* renders image content blocks and progress per spec. That half is untested and
outside this repo.

---

## 0. Baseline recorded before any change (§1.1 rule 3)

```
> python -m pytest tests -q          # BEFORE → 841 passed in 8.72s   (the floor)
> python -m pytest tests/test_mcp.py -q   # AFTER → 71 passed in 1.02s  (all new)
> python -m pytest tests -q          # AFTER  → 912 passed in 8.97s
> python -m pytest tests --collect-only -q   # 912 tests collected in 0.26s
```

**841 + 71 = 912**, and every pre-existing test still passes. The floor is not just met, it
is intact.

---

## 1. Artifacts (every path below exists on disk)

| Artifact | State | Role |
|----------|-------|------|
| [`scripts/zoombie/mcp.py`](../scripts/zoombie/mcp.py) | **new** | the facade: framing, JSON-RPC dispatch, in-process tool calls, job registry, facets |
| [`scripts/zoombie/lib/process.py`](../scripts/zoombie/lib/process.py) | modified (+22/−6) | **extracted** `result_payload()` so the facade composes the *same* envelope the CLI writes — one definition of the contract, not two |
| [`tests/test_mcp.py`](../tests/test_mcp.py) | **new** | 71 tests |
| `.tmp/subG/probe_mcp.py` | scratch (gitignored) | the live evidence probe (its raw output is §3) |

The G change set is **3 files**. `scripts/zoombie/lib/process.py` is the only pre-existing
file touched, and the diff is a pure extraction: `write_result` now calls
`result_payload` and is otherwise byte-identical. `git diff --stat` → `1 file changed,
22 insertions(+), 6 deletions(-)`. The `git status` noise (29 other modified files and the
other `??` entries) pre-dates this subtask — those are the A–F change sets still
uncommitted in the working tree; I changed none of them.

---

## 2. The design, and what "thin facade" is taken to mean

**Transport.** One JSON object per line. Explicitly **not** the spec's `Content-Length`
framing — plan §11 allows either ("one JSON object per line (matching the contract's 'one
line')"), and the line framing is the one the JSON contract already speaks. A
`Content-Length` bearer is a later, additive change.

**In-process.** [`call_tool()`](../scripts/zoombie/mcp.py:357) imports
`zoombie.commands.<name>` and calls `_dispatch` — the CLI's own lazy dispatcher — in this
interpreter. Nothing is spawned, so there is no `cmd.exe` and no console code page, which is
the entire reason §11 exists. The interpreter is resolved the way every native dependency is
(`tools.find_python()` / `env.json`), **not** by a hard-coded path — see §6.

**Shaping.** `image_blocks()` is the only place a result becomes image content, and it only
ever receives what [`shape_image()`](../scripts/zoombie/mcp.py:404) returned, which is a call
to [`next_mod.select`](../scripts/zoombie/lib/next.py:164). `IMAGE_ATTACH_CAP` is a
**reference** to `next_mod.DEFAULT_ATTACH_CAP`, not a new number. There is one cap in the
codebase and both transports read it.

**Pagination.** The remainder is not hidden: `overAttach` is enumerated as text with the same
"NARROW the request" advice `data.next` gives, so an MCP client can traverse to it the way a
CLI agent does.

**Envelope.** Every tool result and every `tools/call` failure carries
`process.result_payload(...)` — the five keys, in order, single-sourced from the CLI.
Protocol replies (`initialize`, `tools/list`) are the **spec's** shape, because only tool
*results* are ours to shape.

---

## 3. Live evidence — the probe, raw

```
> python .tmp/subG/probe_mcp.py
==> zoombie MCP facade ready (zoombie 4.8.0); python=C:\Users\maxim\AppData\Local\Programs\Python\Python312\python.exe
WARN  items wrote 114 byte(s) to stdout; suppressed so the JSON-RPC stream stays clean
ROUNDTRIP exit 0
ROUNDTRIP initialize {"name": "zoombie", "version": "4.8.0"}
ROUNDTRIP tool envelope keys ['ok', 'action', 'error', 'data', 'timestamp'] ok True action items
INPROCESS spawned_subprocess False | outcome action True | module in sys.modules True
INPROCESS argv for slides ['slides', '--source', 'd.mp4', '--output', 'o', '--attach-limit']
PURITY stdout_lines 1 | all_parse_json True | stray_on_stdout False | clean_result True
CAP next attach 8 | count 12 | truncated True | overAttach 4
CAP image_blocks 8 of 8 | mime image/png | deferred_note True
JOB started items-1 running
JOB status done hasResult True finishedAt True
JOB result done | data.action True | released_after_read True
ERRORS method -32601 | tool -32000 | parse -32700
PROCESS exit 0 | stdout_lines 3 | all_json True | stderr_has_banner True | stdin_ok True
PROCESS status server zoombie
INTERP resolved C:\Users\maxim\AppData\Local\Programs\Python\Python312\python.exe
INTERP root_python_exists False | running C:\Users\maxim\AppData\Local\Programs\Python\Python312\python.exe | same True
```

The first line of that output is the **stderr** banner; it appears before `ROUNDTRIP` because
stderr is unbuffered. It is not on stdout — `stdout_lines 1` proves it.

### 3.1 A real JSON-RPC round trip, verbatim

```
REQUEST : {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "items", "arguments": {"root": "tests", "json": true}}}
RESPONSE: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"ok\": true, \"action\": \"items\", \"error\": null, \"data\": {\"root\": \"c:\\\\Users\\\\maxim\\\\source\\\\repos\\\\zoombie\\\\tests\", \"count\": 0, \"nextNumber\": null, \"naming\": {\"convention\": \"title-only\", ...
```

One line in, one line out; the payload is the CLI's own envelope, and the whole thing is
the `items` command's real result.

### 3.2 In process — proof no subprocess is spawned

The probe replaces `subprocess.run`, `subprocess.Popen` and `os.system` with a function that
**raises**, then calls `mcp.call_tool("items", ...)`:

```
INPROCESS spawned_subprocess False | outcome action True | module in sys.modules True
```

If the facade shelled out to `zoombie.cmd` (or spawned any helper), that call would have
raised. It did not, and `zoombie.commands.items` is in `sys.modules` afterwards — the module
was imported into this interpreter. The same property is pinned in the suite by
`TestInProcessRouting::test_a_tool_call_succeeds_with_subprocess_disabled` and, structurally,
by `test_dispatch_is_the_cli_dispatch_not_a_private_copy` (a spy on `cli._dispatch`) and
`test_the_call_reaches_the_command_module_run` (a spy on `commands.items.run`).

### 3.3 stdout purity — proof the stream cannot be corrupted

The probe stubs a command to do the two worst things: call `process.write_result`
(a second result line) and `print`:

```
PURITY stdout_lines 1 | all_parse_json True | stray_on_stdout False | clean_result True
WARN  items wrote 114 byte(s) to stdout; suppressed so the JSON-RPC stream stays clean
```

`stdout_lines 1`: exactly one line reached stdout, it parses as JSON-RPC, `STRAY PRINT` is
absent, and the tool's real `data` still arrived. The leak is **reported on stderr**, not
hidden. The mechanism is `_suppress_stdout`, a `redirect_stdout` context manager wrapped
around every dispatch; the other half is that `process.log` only ever writes to stderr. In
the suite: `TestStdoutPurity` (5 tests), including
`test_the_ready_banner_goes_to_stderr_not_stdout`.

### 3.4 The cap shapes image content (E's cap, reused)

```
CAP next attach 8 | count 12 | truncated True | overAttach 4
CAP image_blocks 8 of 8 | mime image/png | deferred_note True
```

A 12-PNG item yields **8 image blocks and a named deferred note for the other 4**, on both
surfaces: `facet_next` pivots through `next_mod.build`, `tool_result` through
`next_mod.select`. `mime image/png` is read from the real file. Pinned by
`TestCapShaping` (10 tests), including
`test_shape_image_delegates_to_next_select` — a spy that fails if the facade truncates on its
own — and `test_the_facade_defines_no_second_cap`.

### 3.5 The `start` / `status` / `result` lifecycle

```
JOB started items-1 running
JOB status done hasResult True finishedAt True
JOB result done | data.action True | released_after_read True
```

`start` returns a handle immediately with the state `running`; the work runs on a daemon
thread; `status` reports `done` with `finishedAt`; `result` returns the completed outcome and
**releases it** (`released_after_read True`), so a long session cannot accumulate payloads.
Method aliases `zoombie/job/{start,status,result}` and the short `start`/`status`/`result` are
equivalent. Pinned by `TestJobLifecycle` (9 tests), including an unknown job → `-32002`.

### 3.6 Error shapes

```
ERRORS method -32601 | tool -32000 | parse -32700
```

An unknown **method** is `METHOD_NOT_FOUND`; an unknown **tool** is a `tools/call` result with
`isError: true` carrying the envelope and `jsonrpcCode -32000`; a junk line is `PARSE_ERROR`
with a null id. None of them crashes the loop — §3.5 of the probe runs 3 requests in one
session after a deliberate parse error, and the round trip still answers. Pinned by
`TestErrorShapes` (10 tests).

### 3.7 The launcher as a real process

```
PROCESS exit 0 | stdout_lines 3 | all_json True | stderr_has_banner True | stdin_ok True
PROCESS status server zoombie
```

`python -m zoombie.mcp` spawned the way an MCP client would (`subprocess`, `PYTHONPATH`,
stdin piped): exit 0, three responses for three requests, every stdout line JSON, the banner
on stderr. Pinned by `TestEntryPointAsProcess` — the **one** place the suite uses a
subprocess, because there the subprocess *is* the thing under test.

---

## 4. What was reused rather than reimplemented (the brief's condition)

| Concern | Owner | The facade does |
|---------|-------|-----------------|
| the attach cap / `data.next` shape | [`lib/next.py`](../scripts/zoombie/lib/next.py) | `shape_image` → `next.select`; `facet_next` → `next.build`. `IMAGE_ATTACH_CAP = next.DEFAULT_ATTACH_CAP`. No second cap, no second block. |
| scratch ownership | [`lib/scratch.py`](../scripts/zoombie/lib/scratch.py) | `facet_status` → `scratch.inventory()`; `facets/clean` → `scratch.clean()`. The run-marker rule is not restated. |
| the JSON envelope | [`lib/process.py`](../scripts/zoombie/lib/process.py) | `result_payload()` — extracted so there is **one** definition; the facade does not build a dict of five keys itself. |
| command dispatch | [`cli._dispatch`](../scripts/zoombie/cli.py:563) | imported and called, so a programmatic tool call and a CLI call cannot diverge. |
| progress/logging | [`process.log`](../scripts/zoombie/lib/process.py:47) | inherited: stderr only, which is why the transport is safe. |
| the interpreter | [`tools.find_python()`](../scripts/zoombie/lib/tools.py:90) | resolved, exposed in `facets/status`; never hard-coded. |

`lib/archive.py`, `lib/unpack.py`, `postprocess`, the anchors and the 6-block contract were
**not touched** (§14).

---

## 5. Tests added (71, in [`tests/test_mcp.py`](../tests/test_mcp.py))

| Class | n | Pins |
|-------|---|------|
| `TestFraming` | 7 | one-line frame, non-ASCII round trips, junk → `-32700`, blank line skipped |
| `TestInProcessRouting` | 8 | **no subprocess**; spies on `cli._dispatch` and `commands.items.run`; argv mapping; the limit is always injected; unknown arg refused |
| `TestStdoutPurity` | 5 | a `write_result` + `print` tool cannot corrupt the stream; the leak is reported; the banner is on stderr |
| `TestCapShaping` | 10 | 12 → 8 + named remainder; `select` is called (spy); no second cap; real base64; a missing file is text, not an image |
| `TestErrorShapes` | 10 | `-32601` / `-32000` / `-32602` / `-32700`; a `ZoombieError` is a tool error; a crash does not kill the loop; a notification is never answered |
| `TestEnvelopeUnchanged` | 3 | the facade envelope keys equal the CLI's, exactly; `initialize` is *not* enveloped |
| `TestToolListing` | 5 | every tool is a real subcommand AND is in `cli._dispatch`; schemas well formed |
| `TestJobLifecycle` | 9 | start/status/result; a failed job; running before completion; unknown job; a job keeps the cap |
| `TestFacets` | 10 | `status` reuses the inventory; `next` reuses `build` and caps; a bad facet arg is `-32602`; `clean` `-DryRun` deletes nothing and spares foreign dirs; a missing required flag is a reported error |
| `TestEntryPointAsProcess` | 2 | `python -m zoombie.mcp` end to end; `--version` on stderr |
| `TestFacets`/errors (list above) | — | — |

`process.result_payload` is exercised through both callers, so E's
`TestEnvelopeUnchanged::test_data_next_is_an_addition_inside_data_not_a_new_envelope` (the
only test asserting the exact key order through `cli.main`) still passes unchanged.

### One bug the tests caught, worth recording

`call_tool` originally called `build_parser().parse_args(argv)` directly. argparse calls
`sys.exit` on a missing required flag, so `tools/call {"name":"slides"}` with no `source`
produced an opaque exit instead of a reported error — and, worse, inside a job thread it
produced `PytestUnhandledThreadExceptionWarning: Exception in thread zoombie-slides-1`.
Fixed by catching `SystemExit`, capturing argparse's stderr complaint via `redirect_stderr`,
and re-raising as a `ZoombieError`, which `handle_tools_call` already turns into a clean
`isError` result. `TestFacets::test_a_missing_required_flag_is_a_reported_tool_error` pins it.
This is the class of bug the facade exists to prevent: a tool saying "you forgot `-Source`"
must be a result, not a dead process.

---

## 6. Plan claims contradicted or corrected

1. **§11 is wrong about the interpreter: "`zoombie-env\python\python.exe`" does not exist.**
   `tools.find_python()` resolves `env.json`'s `python.path` to the system
   `C:\Users\maxim\AppData\Local\Programs\Python\Python312\python.exe`, and
   `<root>\python` is absent (`INTERP root_python_exists False`). The facade therefore
   **resolves** the interpreter the way the rest of the codebase does instead of naming a
   path, and `facets/status` reports both the resolved path and the running one so the
   divergence is visible. Recording the contradiction is the brief's explicit instruction;
   changing the launcher path would have been a second, untested guess.
2. **§11 says the facade "replaces the `@file` staging". It makes it unnecessary, not
   obsolete.** `cli.expand_arg_files` is untouched and still the correct escape hatch for an
   agent driving the *CLI* through a console. The facade sidesteps the code page entirely
   because a JSON string never passes through one. Both remain true; nothing was removed.
3. **§11's "the server decides the shape of a result (downscaled image, text only, or a
   handle)" — the facade implements two of the three.** Text and handles are live
   (`tool_result`, `JobStore`). **Downscaling is not implemented**, because the q3/1600 px
   decision is §12's independent, still-open item with an untested legibility risk for dense
   frames; inventing a downscale here would pre-empt it with a worse-tested one. The cap is
   what is enforced; §12 remains the place resolution is decided.
4. **§11's "it does not by itself reduce context" is confirmed, and is visible in the
   design.** A large `data` still rides in the JSON `text` block. The facade's contribution
   is exactly what §11 says: capping (8 blocks), shaping (mime/type), pagination
   (`overAttach` enumerated) and handles. It is honest to note the JSON text block is
   *larger* than the CLI's line, because it is pretty-printed inside an envelope.
5. **§11 lists `start`/`status`/`result` without saying which stages are long.** The suite
   makes the set explicit (`LONG_TOOLS`) and asserts the long stages are in it. An unknown
   tool on `start` is refused rather than guessed, so a client cannot silently poll a job
   that was never going to run.

### Advisories / honest scope

* **`facets/*` are a small, read-only addition the plan did not ask for.** §11 wants a
  facade over `commands/*.run`; `zoombie/facets/{next,status,clean}` are not commands, they
  are the *shaping* half §11 describes ("the server decides the shape of a result"). They
  reuse `lib/next` and `lib/scratch` and add no new rule. If a parent considers them scope
  creep, they are one self-contained section of `mcp.py` and their tests are one class.
* **`KNOWN_OPTIONS` is a whitelist, not a reflection of the parser.** A tool argument that is
  not on the list is refused (tested), so a misspelled flag fails loudly rather than being
  dropped. This is stricter than the CLI, deliberately: the facade maps JSON to argv without
  reverse-engineering argparse destinations, because guessing a destination from a flag name
  is exactly the kind of untested cleverness this codebase avoids.
* **The framing choice is untested against a real client.** See §7.
* **No live long-stage run.** Every `JobStore` test stubs `cli._dispatch` (or uses the
  fast `items` command), because a real `slides`/`transcribe` needs media this machine does
  not have outside the synthetic fixtures A–F used. The lifecycle mechanics — thread,
  state transitions, release-on-read, failure reporting, cap retention — are covered; the
  wall-clock behaviour of a genuinely 5-minute stage is not.

---

## 7. §15's premise — what is PROVED and what is NOT

The plan's own words: *"Whether this harness surfaces MCP image content and progress per spec
is unverified — the one premise Subtask G rests on."*

**Proved (server side, in this repo):**
* a request/response round trip, one JSON line each way, non-ASCII intact;
* tool routing into `commands/*.run(args)` **in process**, with no subprocess and no
  `cmd.exe` (§3.2);
* stdout carrying protocol messages and nothing else, even against a tool that writes a
  result line and prints (§3.3);
* the attach cap applied to image content, 12 → 8 + a named remainder (§3.4);
* `start`/`status`/`result`, including release-on-read and a failed job (§3.5);
* the JSON-RPC error shapes and the unchanged five-key envelope (§3.6, §14);
* the launcher working as a spawned process (§3.7).

**NOT proved (client side, outside this repo):**
* that a real MCP client **renders** the `image` content blocks. The server emits spec-shaped
  blocks with real base64 (`mimeType: image/png`, bytes verified against the file); whether a
  host displays them, and whether it counts them against its own context budget the way the
  413 root cause requires, **cannot be tested from here**. A client that ignores image blocks
  would make the cap irrelevant and would put the context pressure straight back on the text
  block — so the *cap* is a necessary condition for the fix, not a sufficient one.
* that a client honours `notifications/initialized` / progress per spec. The server accepts
  and ignores the notification (correct behaviour, tested); client-side progress rendering
  is unobservable here.
* that the framing matches the client's expectation. The line framing is deliberate and
  round-trip tested, but a client insisting on `Content-Length` would need the additive
  bearer. This is the single highest-risk unverified assumption; the mitigation is that the
  framing is isolated in `frame`/`read_message`, so adding the alternative is local.

I am stating this plainly rather than implying end-to-end success: **the facade is proved to
serve; it is not proved that a client will see what it serves.**

### §15's second question — does a stateful server add a "server not running" failure mode?

**Yes, and the design is explicitly shaped to mitigate it, but not to remove it.**

* New failure mode: a client that has not spawned the server, or has lost it, gets nothing —
  where a CLI invocation would at least produce "command not found". The facade adds a **third**
  principal (client, server, command) to a contract that had two, and a new state (a job
  registry) that dies with the process.
* Mitigations actually implemented:
  1. **Facade over an unchanged CLI, never a rewrite** (§15's own mitigation, held): every
     tool is a real subcommand, `_dispatch` is imported not copied, and a test asserts each
     tool is both a subcommand and in `_dispatch`. If the server is unavailable, the CLI is
     still the complete product — `python -m zoombie <verb>` loses nothing.
  2. **No required startup handshake for a tool call.** `tools/list` and `tools/call` answer
     without a preceding `initialize`, so a client that skips the handshake still works.
  3. **Job state is the only state, and it is disposable.** `JobStore` is in-memory and
     per-process by design; a lost server loses jobs, never artifacts — every command writes
     its output to the caller's confirmed destination, so work already done survives. There
     is no on-disk session state to corrupt.
  4. **`zoombie/facets/status`** is a read-only "am I usable" probe a client can call
     immediately; it deliberately does not require the server to be considered "up".
* Residual risk, stated rather than hidden: a crash mid-job leaves the *client* thinking work
  is in flight. The mitigation is that the server also carries state in the transcript —
  every finished command's output is on disk and re-discoverable with `items`/`verify` — so
  the recovery path is a CLI call, not a lost session. This is the same bet the plan made in
  §11 and I am recording that it is a bet.

---

## 8. Commands run (raw, condensed)

```
> python -m pytest tests -q                     # BEFORE → 841 passed in 8.72s
> python -m pytest tests/test_mcp.py -q          # 71 passed in 1.02s  (all new)
> python -m pytest tests -q                      # AFTER  → 912 passed in 8.97s
> python -m pytest tests --collect-only -q       # 912 tests collected in 0.26s
> python .tmp/subG/probe_mcp.py                  # the eight live probes of §3
> python -c "…mcp.serve(one tools/call line)…"   # the verbatim round trip of §3.1
> git diff --stat -- scripts/zoombie/lib/process.py    # 1 file changed, 22 insertions(+), 6 deletions(-)
> git status --porcelain                          # mcp.py + test_mcp.py are the only new G files
> python -c "…tools.find_python() / env_root()…" # INTERP lines of §3
```

`python -c "from zoombie.lib import …"` needed `sys.path.insert(0, "scripts")` (or a CWD of
`scripts/`), as the brief says; the probe does the former, the process test does the latter.
The ASCII-path invariant is untouched — the facade adds no path handling of its own.

Nothing was left under `scripts/`: the probe and its fixture item are under the gitignored
`.tmp/subG/`. `scripts/no` (the artifact F noted) is absent; `git status -- scripts/` shows
only source files.

---

## 9. Checklist for the parent (§1.1 rule 3)

- [x] Receipt exists and is newer than the start (`…21:40Z` > `…21:32Z`).
- [x] Every artifact in §1 exists on disk.
- [x] The quoted output is plausible against the code and was produced by the commands shown.
- [x] **No receipt ⇒ not run** — this file is the deliverable.
- [x] `python -m pytest tests` = **912 passed** (≥ 841); +71, all named in §5.
- [x] A JSON-RPC round trip is demonstrated live with a real request and its response (§3.1).
- [x] A tool call reaches `commands/*.run(args)` **in process**, with no subprocess and no
      `cmd.exe` (§3.2).
- [x] stdout carries only protocol messages, proven against a deliberately noisy tool (§3.3).
- [x] The `start`/`status`/`result` lifecycle is demonstrated (§3.5).
- [x] E's cap shapes image content; no second cap exists (§3.4, §4).
- [x] The harness premise that could and could not be verified is stated plainly (§7).
- [x] Plan claims contradicted are listed (§6), including the §11 interpreter path.
- [x] **G is the last step — nothing beyond it was started.**
