# Repository Guidelines

## Project Structure & Modules
- Backend (FastAPI, Python): `backend/` – API, DB, OneDrive/Gemini logic, scripts under `backend/scripts/`, DB code under `backend/db/`, helpers in `backend/utils/`.
- Frontend (Next.js, TypeScript): `frontend/` – UI in `frontend/src/`, static assets in `frontend/public/`.
- Tests: `test/backend/` – pytest suite for core backend features (OCR, schedules, OneDrive, preview).
- Other: `ref/` and `storage/` hold reference data and local files; avoid committing secrets or large binaries.

## Build, Test & Development
- Backend dev server: `cd backend && python app.py` (reads `backend.env` and exposes FastAPI on configured `PORT`).
- Backend tests: `cd backend && pytest ../test/backend` (run all backend tests).
- Frontend dev: `cd frontend && npm install && npm run dev` (Next.js dev server on port 3000).
- Frontend build: `cd frontend && npm run build` then `npm start` for production-like run.

## Coding Style & Naming
- Python: 4-space indentation, type hints where practical, snake_case for functions/variables, PascalCase for classes. Prefer small, focused modules under `backend/`.
- TypeScript/React: follow existing patterns in `frontend/src` – functional components, hooks, and PascalCase component names.
- Keep configuration in `backend/backend.env` and `.env.local` (frontend); never hard-code secrets.

## Testing Guidelines
- Use `pytest` and `pytest-asyncio` for backend tests; new features should include or update tests under `test/backend/`.
- Name tests `test_*.py` and ensure they are idempotent and do not rely on external services without guards or mocks.

## Commit & Pull Requests
- Commit messages follow a conventional prefix style, e.g. `feat:`, `fix:`, `chore:`, `refactor:` plus a concise summary (see `git log` for examples).
- For PRs, include: purpose/summary, key changes, how to run/verify (commands), any DB/migration or config changes, and screenshots/GIFs for UI-impacting work.

## Tool Usage & Integration
- If browser automation is involved, clean up browser contexts/instances after use to avoid occupying system resources, especially network resources.
- If AWS is involved, use the AWS API MCP to inspect or modify resources instead of ad-hoc scripts.
- If a database is involved, use the PostgreSQL MCP to inspect or modify data rather than direct shell access.
- If the UI is involved, use the Playwright MCP to inspect or modify elements.
- Code strategy: prioritize reusing existing code, then modifying existing modules; only create new code when absolutely necessary.

## Agent-Specific: Claude Code MCP Usage

- MCP-first principle: before starting any development task, evaluate whether using MCP can help; always prioritize Claude Code MCP for code-related work, PostgreSQL MCP for database work, AWS API MCP for cloud resources, and Playwright MCP for UI testing.
- Hard rule (MUST NOT break): when using Claude Code MCP, it must never invoke or chain-call the Codex MCP tool (`codex`) from within Claude Code MCP. Any Codex MCP usage, if needed, must be initiated outside of Claude Code MCP.
- Using Claude models through Claude Code MCP is allowed and encouraged for analysis, planning, prototyping, and review; the only strict prohibition is having a Claude Code MCP session internally call the Codex MCP tool and create a closed feedback loop.
- Use the right MCP for each domain:
  - AWS resources: use AWS API MCP to inspect/modify S3, Lambda, etc.
  - Database: use PostgreSQL MCP to query, inspect, and modify data.
  - UI testing: use Playwright MCP to open pages, interact with elements, capture screenshots, and perform end-to-end checks; always clean up browser contexts/instances explicitly.
  - Code collaboration: use Claude Code MCP for requirements analysis, code prototyping, and code review.
- Resource management with MCP:
  - When using Playwright MCP, always close browser contexts and instances when done to avoid resource and network leaks.
- Collaboration workflow with Claude Code MCP (strongly recommended):
  - Requirements analysis: form an initial understanding, then share requirements and your approach with Claude Code MCP to get refinements.
  - Code prototyping: before implementing, ask Claude Code MCP for a prototype in unified diff style as a logical reference; rewrite the final code to production quality instead of blindly applying the diff.
  - Code review: after changes, immediately use Claude Code MCP to review the diff and verify that requirements are met.
  - Critical thinking: treat Claude Code MCP as a reference, keep independent judgment, and challenge its suggestions when needed.

## Agent-Specific: OCR Prompt & Schema Design
- When a sample document (invoice, receipt, statement, etc.) is provided, generate two artifacts: (1) a detailed extraction prompt for an LLM (e.g. Gemini) and (2) a matching JSON schema.
- Keep prompt and schema perfectly synchronized: every field described in the prompt must exist in the schema with the same meaning, and the schema must not introduce extra fields.
- In the prompt, clearly define document type, global rules (language preference, sign conventions, pages to skip), and section-based extraction logic: labels/keywords to search for, expected formats (date, amount, IDs), and value extraction rules, including handling of tables, line items, and grouping.
- In the schema, use English `snake_case` field names, accurate JSON types, and a hierarchy that mirrors the document structure; add `description` for each field and use `required` to mark mandatory fields.
- For complex monetary or summary sections, include validation rules in the prompt (e.g. subtotals vs totals, discounts as negative amounts), ensuring they conceptually align with the schema structure.
