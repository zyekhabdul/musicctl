# AI AGENT WORKSPACE STANDARDS & SSOT POINTER

All agents (Antigravity CLI, Claude Code, Codex, OpenCode) MUST adhere to the centralized rules and deterministic hooks in `~/.gemini/config/`:
- Master Index: `~/.gemini/config/GEMINI.md` (symlinked as `CLAUDE.md` / `AGENTS.md`)
- Modular Rules: `~/.gemini/config/rules/` (**6 Core `always_on`** + **10 On-Demand `model_decision`**)
- Deterministic Hooks: `~/.gemini/config/hooks.json` (`PreToolUse` safety & push guard)

### Core `always_on` Rules (6)
- `agent-persona-invariants.md`: Objective Technical Mentor, caveman terse, strict no-emoji, autonomous single-stream, subagent `Model: 'inherit'`
- `inspect-before-apply.md`: Read-before-mutate, surgical `edit_file` for existing files, AST/grep call-site scan
- `ponytail-yagni.md`: Minimal diff radius, zero speculative abstraction, native stdlib first, zero dead code
- `empirical-verification.md`: Terminal exit code 0, non-empty pass (`tests > 0`, `skipped == 0`), 3-try circuit breaker, Two-Tier Verification
- `git-push-restriction.md`: Local `git commit` allowed; `git push` blocked without explicit user command; SSH remotes; pre-mutation `git pull`
- `sensitive-area-guard.md`: Hard-stop + RFC/diff confirmation for auth, financial/payments, destructive DB, `.env`/secrets, prod CI/CD

### On-Demand `model_decision` Rules (10 — Load Only When Relevant)
- `agy-runtime-troubleshooting.md` | `ai-proposal-protocol.md` | `autonomous-failure-modes.md` (16 anti-blunder invariants)
- `deterministic-machine-harness.md` | `environment-topology.md` | `hardware-cluster-topology.md`
- `mcp-discovery.md` | `obsidian-rag.md` | `system-diagnostics.md` | `workflow-ai-agent.md`
