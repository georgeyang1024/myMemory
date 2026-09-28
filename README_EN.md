# myMemory

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[中文](README.md) | **English**

**A local-first memory service for humans and AI agents.**
A shared memory store that both humans and AI agents read from and write to, persisted long-term across sessions.

Memories are plain Markdown / text files stored in a directory you choose — no database,
no cloud, no lock-in. AI agents retrieve and write through MCP tools; you can add, edit,
or delete notes directly in any editor at any time, and your changes still enter the index.

---

## Features

- **Full-text retrieval with BM25** keyword ranking + jieba tokenization — deterministic, explainable, fully offline
- Multiple named *sources* (personal / team / company), each a directory on local disk or a mounted volume (NAS / webDev) → one shared memory across people and devices
- Returns **evidence snippets with provenance** (source + path), not pre-baked answers
- Fast start via persistent index cache; keeps serving when the mount is briefly offline

Use cases: personal AI note-taking, team knowledge bases that agents can query, lightweight local search over your own documents.

Design rationale and decision records: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/adr/`](docs/adr/).

---

## Table of contents

- [Quick start](#quick-start)
- [MCP tools](#mcp-tools)
- [REST endpoints](#rest-endpoints)
- [Configuration](#configuration)
- [Client integration](#client-integration)
- [Deployment (Windows)](#deployment-windows)
- [Changelog](#changelog)
- [License](#license)

---

## Quick start

Requires Python 3.10+ (on Windows, check `Add python.exe to PATH` when installing from python.org).

```powershell
git clone https://github.com/georgeyang1024/myMemory.git
cd myMemory
python3 run.py
```

`run.py` is the single entry point and only uses the standard library: it automatically
creates a virtual environment `.venv`, installs dependencies, starts the service, and
handles background start/stop. Once dependencies are installed, repeated starts finish in seconds.

```powershell
python3 run.py                 # Foreground start (first run interactively asks for the memory directory; press Enter for default)
python3 run.py --check         # Self-check: build the index once and report its size; does not listen on a port
python3 run.py --init          # Create/repair config + install dependencies; exits fast if everything is ready
python3 run.py --reinstall     # Force reinstall dependencies
python3 run.py --recreate      # Delete and recreate the virtual environment (when the env is broken)
python3 run.py --no-venv       # Run with the current interpreter, no venv
python3 run.py --index-url <URL>            # Use an intranet / mirror index
python3 run.py --bundle ./wheels            # Build an offline dependency bundle on a connected machine
python3 run.py --find-links ./wheels --offline   # Install on an offline machine
python3 run.py --help          # All options
```

A few notes:

- **First launch** interactively asks for the memory directory (Enter uses the default
  `~/.myMemory/memory`, created automatically) and generates `~/.myMemory/config.json`.
  In non-interactive environments (stdio, background subprocess), missing config reports
  "no memory storage specified" — finish the initial setup in a terminal first, or run
  `python3 run.py --init`.
- Unrecognized arguments (e.g. `--check`, `--stdio`) are passed through to the service itself.
- When `requirements.txt` changes, dependencies are reinstalled automatically; no manual cleanup needed.
- The only config-related environment variable is `MEMORY_CONFIG` (points to the config
  file location); there is an optional `MYMEMORY_READY_TIMEOUT` to adjust the background
  ready-wait seconds (default 180).

### Background mode

```powershell
python3 run.py --background   # Start in background; prints port and PID once ready, then exits
python3 run.py --status       # Status: PID, port, index size
python3 run.py --stop         # Stop
python3 run.py --restart      # Restart
python3 run.py --logs         # Tail logs (Ctrl-C only exits the tail, service is unaffected)
```

- It waits for "ready", not "started": the service does not listen on a port until the
  index is ready; once `/health` responds, it can take requests.
- Running the command again while it is already up will not spawn a second instance.
- Logs are appended to `~/.myMemory/logs/myMemory.log`; the PID file lives in the same directory.
- `--background` is mutually exclusive with `--stdio` / `--check` and fails fast with a reason.
- For auto-start on boot, use Windows Task Scheduler (see [Deployment](#deployment-windows)).

---

## MCP tools

7 tools always present + 2 deletion tools (hidden by default); the full names clients see
look like `mcp__myMemory__save` (client concatenates server name + tool name).

| Tool | One-line description |
|---|---|
| `search(query, limit=5, source="")` | BM25 full-text search across all sources by default; returns raw snippets with `source`/`path`/`writable`. Source names, categories, file names, dates (`26-08-04`), and model-style identifiers all work directly as query terms |
| `get-document(source, path, offset=0, limit=40000)` | Read the raw document, character-level pagination; response includes `writable` and `stale` (content from cache while the disk is offline) |
| `save(source, filename, content, category="")` | Write a memory to `<source>/<category>/<filename>.md`; **overwrites the whole file if it already exists** (irreversible, no backup); empty content rejected |
| `rename(source, old_path, new_path)` | Rename/move a top-level category within the same source; the old file must exist, **rejects if the target exists**, never overwrites |
| `replace(source, path, old_string, new_string)` | **Strictly literal** replace of old→new across the file (no regex, no case folding); replaces every occurrence and returns `replaced_count`; 0 hits or empty `new_string` rejected |
| `list-sources()` | List all sources (`writable`/`available`/`doc_count`…), **does not return directory paths**; pick your write target from here |
| `recent(limit=10, source="")` | Most recently updated, one entry per file; `edited_by` distinguishes `agent` (written via tools) from `scan` (human edits or out-of-band changes) |
| `merge(source, from_path, to_path)`¹ | Merge one **existing** memory into another (merged section gets a `## Source` heading and a `---` divider), then **deletes the source file**; writes first, deletes second; if deletion fails, flagged with `source_removed: false` |
| `delete(source, path)`¹ | **Real deletion** (no backup, unrecoverable); `path` has the same shape as `search` results |

> ¹ Both `merge` / `delete` really delete files and are gated by the config switch
> `allow_mcp_delete` (default `false`, takes effect on restart). When off, the tools do
> not even appear in tools/list — what the LLM cannot see it cannot call; enabling
> requires a manual config change.

**Common conventions** (detailed rules in each tool's parameter descriptions and docs/):

- `path` is always the same shape as `search` / `recent` return values (includes the
  top-level category, excludes the source name, `.md` suffix optional) — copy it as-is,
  do not build paths yourself
- For write tools, both the top-level category and file name go through the same
  character whitelist; `..` and `\ / : * ? " < > |` plus Windows reserved names are
  rejected at the syntax level — no escape to a landing spot outside the source directory
- Writes are **asynchronously refreshed**: the tool returns immediately, the index
  updates in the background, and it may take a few seconds before the content is
  searchable — the `path` in the response is your proof, do not retry
- Write targets must be `writable: true`; read-only, offline disk, or missing directory
  are all rejected
- **Writes are MCP tools only**; all REST endpoints are read-only

---

## REST endpoints

4 endpoints ship with HTTP mode:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Version, index size, build time, `rebuilding`, `verifying`, current config, source list (with directory paths and availability) |
| `GET /search?q=…&limit=…&source=…` | Identical response structure to MCP `search` |
| `GET /recent?limit=…&source=…` | Identical response structure to MCP `recent` |
| `POST /reindex[?full=1]` | Refresh the index immediately; incremental by default, `full=1` for a full rebuild |

```powershell
curl.exe http://127.0.0.1:7083/health
curl.exe "http://127.0.0.1:7083/search?q=hello&limit=3"
curl.exe -X POST http://127.0.0.1:7083/reindex
```

**No write REST endpoints exist**; writes go through MCP tools only. Note that `/health`
returns each source's directory path (for human debugging) — confirm you accept this
exposure before deploying to a LAN.

---

## Configuration

All configuration lives in one JSON file: default `~/.myMemory/config.json`,
relocatable via the `MEMORY_CONFIG` environment variable. `index.cache` and `logs\`
live in the same directory as the config file, so the code directory holds no runtime
data. Config changes **always require a restart to take effect**.

```json
{
  "host": "127.0.0.1",
  "port": 7083,
  "poll_interval": 600,
  "sources": [
    {"name": "memory",  "dir": "D:\\memories",              "writable": true,  "description": "Personal memory"},
    {"name": "team",    "dir": "\\\\server\\share\\team",    "writable": true,  "description": "Team shared memory"},
    {"name": "org",     "dir": "Z:\\org\\docs",             "writable": false, "description": "Policy documents"}
  ],
  "domain_terms": ["RFC9424", "AES-GCM"],
  "allow_mcp_delete": false
}
```

| Field | Default | Description |
|---|---|---|
| `sources` | First-run setup creates one `memory` | See below |
| `host` | `127.0.0.1` | Bind address. Localhost only by default; set `0.0.0.0` for LAN access |
| `port` | `7083` | Listening port |
| `poll_interval` | `600` | Polling interval (seconds); `0` disables it (does not affect save's active refresh) |
| `extensions` | `[".md", ".txt"]` | Extensions included in the index |
| `chunk_size` / `chunk_overlap` | `800` / `120` | Chunk window and overlap (characters) |
| `max_results` | `20` | Max results per retrieval |
| `snippet_chars` | `1200` | Per-snippet truncation length |
| `max_doc_chars` | `40000` | Max characters per document read |
| `max_create_chars` | `100000` | Max content length per memory |
| `max_cached_docs` | `1000` | Max full texts kept in memory (LRU); `0` unlimited |
| `domain_terms` | `[]` | Domain term list, see below |
| `allow_mcp_delete` | `false` | **AI deletion circuit breaker**: only when `true` are `delete` and `merge` (source deletion) exposed to AI; when off, the two tools do not appear in the tool list |

**source**: `name` + `dir` (+ optional `writable`, `description`, `type`).

- `name`: Chinese/English letters, digits, underscores, hyphens, 1–64 chars, no `/`
- `dir`: use whatever you would type (drive letter or UNC both fine), no mapping or conversion
- `writable`: `false` means read-only; **omitting means `true`**. Read-only is unrelated to the name
- Directories must not overlap or nest (compared by real paths, case-insensitive)
- Invalid names, duplicates, overlaps, misspelled fields, or out-of-range values
  **fail at startup**; unreachable directories only warn, never fail

**`domain_terms` (optional)**: identifiers like model numbers and protocol names that
jieba cannot split. They stay as one token during tokenization, and long alphanumerical
strings additionally emit any contained terms, so "search a full model number by its
series name" hits. Changing it invalidates the index cache (one full rebuild on next start).

**Refresh and index cache**:

- On start with a cache: serve immediately from the cache, verify incrementally in the
  background (`verifying` in `/health`)
- Polling (`poll_interval`), post-save, and `/reindex` all go through the same incremental
  refresh path; atomic swap after the background pass completes, no request interruption
- While a disk is offline, that source's index and cache are neither updated nor deleted;
  other sources work as usual; the next poll after recovery picks up automatically
- `max_cached_docs` only limits full texts kept **resident in memory**; all documents
  still enter the index and remain searchable, and uncached full texts are read from
  disk on demand. `cached_docs` in `/health` is the current cache count

### config.py CLI: manage sources and refresh interval

Standard library only, shares the same validation as the server — **any config the CLI
accepts, the server can start with**; the file is untouched when validation fails.

```powershell
python3 config.py source list
python3 config.py source add    <name> --dir <directory> (--readonly | --writable) [--desc "description"] [--restart]
python3 config.py source edit   <name> [--dir <new dir>] [--name <new name>] [--readonly | --writable] [--desc "description"] [--restart]
python3 config.py source remove <name> [--yes] [--restart]
python3 config.py config set poll_interval <seconds> [--restart]
python3 config.py config set max_cached_docs <docs> [--restart]
python3 config.py config set allow_mcp_delete <true|false> [--restart]   # AI deletion switch, default false
python3 config.py reindex [--full]        # Immediate incremental refresh (--full for full), no restart needed
python3 config.py restart                 # Calls run.py --restart
```

> Always specify directories with `--dir` (required for `add`). Either a drive letter
> (`Z:\…`) or UNC (`\\server\share\…`) works; with a drive letter, the service must run
> under the user who has that drive letter mapped.
> Modifying commands require a manual `config.py restart` by default; add `--restart`
> to restart right after the change.

---

## Client integration

### HTTP (recommended: multiple clients share one instance)

Start the service independently first (`run.py --background`), then configure clients:

```json
{
  "mcpServers": {
    "myMemory": {
      "type": "http",
      "url": "http://127.0.0.1:7083/mcp"
    }
  }
}
```

Writes become globally visible within seconds, and you get `/health`, `/search`,
`/recent`. The trade-off is keeping the process alive separately, and the
**writable + no-auth** exposure surface needs your judgment on placement
(default binds `127.0.0.1` only).

### stdio (client spawns the process)

```powershell
claude mcp add myMemory --scope user `
  -e MEMORY_CONFIG=%USERPROFILE%\.myMemory\config.json `
  -- C:\path\to\mymemory\.venv\Scripts\python.exe C:\path\to\mymemory\src\main.py --stdio
```

Three key points:

1. **The command must point directly to the python inside `.venv` and `src/main.py`**:
   in stdio mode stdout is the JSON-RPC data channel; any extra output breaks client
   parsing. Set up the environment with `run.py` first, then let the client call directly.
2. **`MEMORY_CONFIG` must be an absolute path**: the working directory when the client
   spawns the subprocess is unpredictable.
3. **Multiple stdio processes share one `index.cache`**: each writes back atomically
   after refreshing, last writer wins. When accurate `edited_by` matters, use HTTP mode
   with a shared instance.

---

## Deployment (Windows)

1. **Python 3.10+**: check `Add python.exe to PATH` when installing from python.org.
   If `python3` pops up the Microsoft Store or gives no output, disable the
   `python3.exe` alias under Settings → Apps → Advanced app settings → App execution aliases.
2. **First launch**: `python3 run.py --check` (create config + install deps + self-check),
   then `python3 run.py --background`. You can also set up explicitly with `config.py source add`.
3. **Firewall** (for LAN access, admin PowerShell):

   ```powershell
   New-NetFirewallRule -DisplayName "myMemory MCP" -Direction Inbound `
     -Protocol TCP -LocalPort 7083 -Action Allow -Profile Private
   ```

   The default `host` is `127.0.0.1`; skip this for local-only use. To open it to the
   LAN, change `host` to `0.0.0.0` first and then allow the port. **This service is
   writable and unauthenticated** — make sure the network is trusted.
4. **Auto-start on boot**: create a task in Task Scheduler, program `python3`,
   arguments `"<repo dir>\run.py" --background`, start-in set to the repo directory.

---

## Changelog

Version history in [CHANGELOG.md](CHANGELOG.md): 0.1.0 (first stable release: multiple
sources, writable-surface convergence, config.json + CLI, index cache and incremental
updates), 0.2.0 (expanded write surface with rename / replace / merge / delete and
the deletion circuit breaker `allow_mcp_delete`) and 0.3.0 (data preservation for
offline sources: cache fills only empty slots, cold-start cache rescue, fingerprint
drops the code version).

---

## License

[MIT](LICENSE)
