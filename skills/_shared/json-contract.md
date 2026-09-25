The CLI prints one JSON line on stdout: `{ ok, action, error, data, timestamp }`.
Progress goes to stderr and the exit code is 0 on success, 1 on failure.

Every line also carries `timestamp` (ISO 8601 UTC, milliseconds). Exactly one
result is emitted per invocation, so a timestamp older than the run you just
issued means the payload is stale - re-issue the command rather than reading it.

### `data.next` - the recommended next step

The pipeline commands (`slides`, `readimages`, `readpdf`, `postprocess`) add one
key *inside* `data`:

```
data.next = { command, args, why, attach: [...], budget: { images, bytes } }
```

- `command` / `args` / `why` are the next invocation and why. `command` is
  `null` when the next step is YOUR action (read, write prose) rather than
  another CLI call.
- `attach` is what you may open inline. **It is hard-capped at 8 images.** The
  cap is enforced by the CLI, not by you: `attachCap` is the limit in force,
  `count` is the honest total, `truncated` is `true` when more exist, and the
  deferred paths are in `overAttach`. Read `attach`, not `overAttach`.
- Each `attach` path is a **compressed reading copy** (a JPEG, plan §12), not the
  full PNG: open the path you are given, do not reconstruct a PNG path from
  `file`. The JPEG is legible at the source resolution and costs ~3-7x fewer bytes.
- `budget` is what you are about to spend (`images`, `bytes`) - the size of the
  files that actually exist (so the JPEG sizes, not the PNGs).
- `reason` names any cap that bit. An empty string means nothing was refused.

`-AttachLimit N` raises the cap for a deliberately small set; it is a transport
guarantee, so `-Force` does not bypass it. Reaching more than 8 means NARROWING
the request (e.g. `slides -Times`), not raising the cap.
