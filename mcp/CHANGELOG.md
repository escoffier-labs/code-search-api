# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Moved the package source under
  `escoffier-labs/code-search-api/tree/main/mcp`; the standalone
  `escoffier-labs/code-search-mcp` repository is deprecated.

## [0.2.0] - 2026-06-16

### Fixed
- Strip the draft-07 `$schema` key the MCP SDK stamps on tool schemas before
  sending `tools/list` output. Some MCP clients reject draft-07 schemas, so the
  stdio transport now removes the key from each tool's `inputSchema` and
  `outputSchema` on the wire.
- Apply a 30s `AbortController` request timeout in the OpenClaw plugin entry
  point (`index.mjs`) so a hung code-search-api request can no longer block
  indefinitely, matching the existing timeout in `src/client.ts`.

### Added
- Single verification entrypoint at `scripts/verify` that runs typecheck, tests,
  and build in order. The Definition of Done in `AGENTS.md` defers to it.
- Compact and `by_file` search responses now include `applied_min_score` and a
  note when the `min_score` floor filters out every match, so callers know to
  lower the floor.

### Changed
- Updated repository references and links to the `escoffier-labs` GitHub
  organization.
- The MCP server version is now derived from `package.json` instead of a
  separate hardcoded constant, so a single version bump covers both.
