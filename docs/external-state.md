# External memory and skills state

Hermes can keep its built-in curated memory and writable skills in an
authoritative external text-object engine. When enabled, the local filesystem
is only a process-scoped compatibility cache and is deleted at process exit.
Hermes will not silently fall back to persistent local files if the external
engine is unavailable.

## MCP backend

The built-in driver is a regular MCP Streamable HTTP client. It initializes the
connection, sends the `notifications/initialized` notification, and calls four
ordinary MCP tools:

- `state_get(namespace, key)` returns `found`, `content`, `content_type`, and
  `etag`;
- `state_list(namespace, prefix, limit)` returns object metadata in `objects`;
- `state_put(namespace, key, content, content_type, expected_etag)` creates or
  replaces an object;
- `state_delete(namespace, key, expected_etag)` deletes an object.

`expected_etag` provides optimistic concurrency. An empty value means “create
only”; a non-empty value must match the current object. This contract is not
Hermes-specific: any conforming MCP client can call the tools and any MCP
server can implement them.

Example configuration:

```yaml
external_state:
  backend: mcp
  strict: true
  mcp:
    url_env: SLC_MCP_URL
    header_env:
      X-Seat-ID: SLC_SEAT_ID
    timeout_seconds: 20
  memory:
    enabled: true
    namespace: hermes-memory
  skills:
    enabled: true
    namespace: hermes-skills
```

Never put credentials directly in `config.yaml`. Use `header_env` or
`bearer_token_env`. The state namespace is additionally isolated by the
server-authenticated seat identity when SLC is used.

Memory objects are `MEMORY.md` and `USER.md`. Skill object keys are relative
paths such as `frontend-review/SKILL.md` and
`frontend-review/references/checklist.md`. Bundled or operator-mounted skill
directories remain read-only inputs; only the profile's writable skill tree is
stored externally.

## Other engines

Set `external_state.backend` to `package.module:factory`. The factory receives
the full `external_state` mapping and must return an
`external_state.TextStateBackend` implementation. This keeps the persistence
contract independent from SLC and from MCP while retaining the same strict,
external-only durability semantics.
