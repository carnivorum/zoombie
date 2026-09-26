MCP is the transport: every command is a `zoombie` **MCP tool** of the same name,
already registered **globally** at setup (`mcpServers.zoombie`). Call the tool
directly — its schema carries the argument names. (Re-register with the `mcp` tool
`{"apply": true}`, or `python -m zoombie mcp -Apply`; it is a merge, so other
servers are kept.)

Only if those MCP tools are **not available**, use the CLI **fallback**. The
launcher normally lives under the user profile; on a machine whose user name is
not ASCII the toolchain is installed under `%PUBLIC%` instead (the native
libraries break on non-ASCII paths), so check both:

```powershell
$cli = @(
    "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.cmd",
    "$env:PUBLIC\zoombie-env\bin\zoombie\zoombie.cmd"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cli) { throw "zoombie CLI not found. Run the zoombie setup first: fetch https://raw.githubusercontent.com/carnivorum/zoombie/main/setup.md and follow it (or, as a manual fallback, irm https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1 | iex)." }
```
