Resolve your transport **first, and prefer MCP**. The toolchain exposes every
command as a `zoombie` **MCP server**, and setup registers it **globally** in the
client's MCP settings (`mcpServers.zoombie`) — so in a normal install the tools are
already there. **Use them whenever the client offers them**: they run the commands
in process, so there is no `cmd.exe` and no console code page, and a Cyrillic
`-Output` never round-trips through a shell. Treat the CLI below as the **fallback**
for when the `zoombie` MCP tools are not available. (Re-register the global server
with `python -m zoombie mcp -Apply`; it is a merge, so other servers are kept.)

If — and only if — the `zoombie` MCP tools are not available, use the CLI. It
normally lives under the user profile, but on a machine whose user name is not
ASCII the toolchain is installed under `%PUBLIC%` instead (the native libraries
break on non-ASCII paths), so check both:

```powershell
$cli = @(
    "$env:USERPROFILE\zoombie-env\bin\zoombie\zoombie.cmd",
    "$env:PUBLIC\zoombie-env\bin\zoombie\zoombie.cmd"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cli) { throw "zoombie CLI not found. Run the zoombie setup first: fetch https://raw.githubusercontent.com/carnivorum/zoombie/main/setup.md and follow it (or, as a manual fallback, irm https://raw.githubusercontent.com/carnivorum/zoombie/main/scripts/bootstrap.ps1 | iex)." }
```
