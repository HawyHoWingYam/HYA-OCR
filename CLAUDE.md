# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HYA-OCR is a full-stack document recognition platform combining:
- Python/FastAPI backend for AI-powered OCR using Google Gemini API
- Next.js/TypeScript frontend for user portal
- PostgreSQL database (SQLite for dev)
- AWS S3 + OneDrive integration for document storage/sync

## Common Commands

### Backend
```bash
cd backend && python app.py              # Start dev server (reads backend.env)
cd backend && pytest ../test/backend -v  # Run tests
cd backend && python init_db.py          # Initialize database
cd backend && python check_db.py         # Database health check
```

### Frontend
```bash
cd frontend && npm install               # Install dependencies
cd frontend && npm run dev               # Start dev server (port 3000)
cd frontend && npm run build             # Production build
cd frontend && npm run lint              # Run linter
```

## Architecture

### Project Structure
```
backend/
  app.py              # Main FastAPI app with all API routes
  main.py             # Gemini OCR logic + image processing
  config_loader.py    # Environment variable configuration
  db/models.py        # SQLAlchemy ORM models (23 tables)
  utils/              # Core utilities
    order_processor.py       # OCR order execution pipeline
    ocr_schedule_runner.py   # Scheduled OCR + OneDrive automation
    onedrive_client.py       # Microsoft Graph API client
    prompt_schema_manager.py # Prompt/schema loading & caching
    file_storage.py          # Local/S3 file abstraction
frontend/
  src/app/            # Next.js App Router pages
  src/lib/api.ts      # Typed API client
test/backend/         # Pytest test suite
```

### Configuration
- Single source of truth: `backend/backend.env`
- Required variables: `API_BASE_URL`, `PORT`, `DATABASE_URL`, `GEMINI_API_KEY`
- Frontend loads backend.env via `next.config.ts`

### OCR Pipeline Flow
1. User submits `OcrOrder` with items
2. For each item: fetch prompt/schema → call Gemini API → save JSON → apply mapping → generate Excel/CSV
3. Aggregate results → broadcast status via WebSocket

### Key Database Entities
- `Company` / `DocumentType` → `CompanyDocTypeConfig` (maps company+doctype to OCR settings)
- `OcrOrder` → `OcrOrderItem` → `OrderItemFile`
- `OcrSchedule` → `OcrScheduleRun`

### API Patterns
- All datetime responses use Hong Kong timezone (UTC+8)
- WebSocket endpoints: `/ws/orders/{order_id}`, `/ws/orders/summary`
- Swagger UI available at `/docs`

## Development Guidelines

### Tool Responsibilities

**Clear separation between Claude Code CLI and Codex MCP:**

| Tool | Responsibility | Examples |
|------|----------------|----------|
| **Claude Code CLI** | All code implementation | Writing code, editing files, creating components, refactoring |
| **Codex MCP** | Analysis & review only | Planning, troubleshooting, bug analysis, code review, reading, design |

**IMPORTANT: Codex MCP must NEVER be used for code implementation.** Always use `sandbox="read-only"` with Codex.

### MCP-First Approach

**For every task, always consider and leverage available MCP tools for collaboration:**

1. **Use Codex MCP for analysis tasks** - Requirements analysis, troubleshooting, bug analysis, code review, design thinking, reading comprehension
2. **Use Claude Code CLI for implementation** - All actual code changes, file edits, and modifications
3. **Use specialized MCP tools** - PostgreSQL MCP for database operations, AWS API MCP for cloud resources, Playwright MCP for UI testing
4. **Evaluate applicability** - Not all tasks require MCP, but assess each task to determine if MCP collaboration would improve quality or efficiency

This approach ensures better code quality, catches issues early, and leverages AI collaboration throughout the development process.

### ⚠️ CRITICAL: Codex MCP Rules & Responsibilities

**This section emphasizes non-negotiable rules for Codex MCP usage. Violations compromise code quality and project integrity.**

**RULE 1: Codex MCP is ANALYSIS-ONLY**
- Codex MCP MUST NEVER write, edit, create, or modify any files
- Codex MCP MUST NEVER execute code changes or implementations
- Codex MCP MUST NEVER use `sandbox="workspace-write"` or `sandbox="danger-full-access"`
- **ALWAYS use `sandbox="read-only"`** - this is non-negotiable
- Codex MCP reads and analyzes code; Claude Code CLI implements changes

**RULE 2: Clear Role Separation**
- **Codex MCP Role**: Objective analysis partner for planning, design, troubleshooting, code review, and critical thinking
- **Claude Code CLI Role**: Sole executor of all code implementation, file edits, and modifications
- **Never blur these roles** - if you're tempted to use Codex for implementation, use Claude Code CLI instead

**RULE 3: When to Use Codex MCP**
- ✅ Requirements analysis and validation
- ✅ Architecture design and planning
- ✅ Bug analysis and troubleshooting (reading logs, tracing issues)
- ✅ Code review after implementation (verify requirements met)
- ✅ Design thinking and critical evaluation
- ✅ Reading and understanding existing code
- ❌ Writing any code
- ❌ Editing any files
- ❌ Creating components or modules
- ❌ Modifying configurations or data

**RULE 4: Workflow Pattern**
1. Use Codex MCP to analyze requirements and design approach
2. Use Claude Code CLI to implement the designed solution
3. Use Codex MCP to review the implementation and verify correctness
4. Iterate if needed, maintaining strict role separation

**RULE 5: Sandbox Policy**
- Every Codex MCP call MUST include `sandbox="read-only"`
- If you need to modify files, use Claude Code CLI instead
- Never use `yolo=true` with Codex MCP
- Never attempt to bypass sandbox restrictions

**Why These Rules Matter**
- Prevents accidental file corruption or unintended modifications
- Maintains clear audit trail of who made what changes
- Ensures Claude Code CLI remains the single source of truth for implementations
- Protects code quality through proper separation of concerns
- Enables effective collaboration between analysis and implementation

### Code Strategy

**Priority order for implementing changes:**
1. **Reuse existing code** - Search for and leverage existing functions, components, and utilities first
2. **Modify existing code** - Extend or adapt existing implementations when reuse isn't sufficient
3. **Create new code** - Only create new files/modules when absolutely necessary

### Code Style
- Python: 4-space indent, type hints, snake_case functions, PascalCase classes
- TypeScript: functional components with hooks, PascalCase component names
- Never hardcode secrets; use `backend/backend.env`

### Testing
- Use pytest + pytest-asyncio
- Tests must be idempotent, mock external services
- Run single test: `cd backend && pytest ../test/backend/test_specific.py -v`

### Commits
Follow conventional prefix style: `feat:`, `fix:`, `chore:`, `refactor:`

### OCR Prompt & Schema Design

When creating OCR configurations, generate two **perfectly synchronized** artifacts:

**1. Extraction Prompt Requirements**

Structure the prompt with these elements:

- **Overall Document Guidelines**
  - Identify document type (e.g., "Telecommunication Bill", "Purchase Order")
  - State global rules: language priority (English over Chinese), positive/negative number conventions, pages to skip

- **Section-Based Extraction Logic**
  - Divide document into logical sections (Header, Bill Summary, Statement Details, etc.)
  - For each field specify:
    - **Label Identification**: field label or keyword (e.g., "Look for 'Account Number' or '賬戶號碼'")
    - **Format Specification**: expected format (e.g., "Date in YYYY-MM-DD", "Amount with 2 decimal places")
    - **Value Extraction**: how to extract the value (e.g., "a 9-digit number")

- **Handling Complex Structures**
  - For tables/line items: provide explicit extraction logic
  - Define grouping rules (e.g., "Group charge items under their respective phone number")
  - Detail all fields for each line item (description, service period, amount)

- **Special Rules & Constraints**
  - Handle special cases: discounts (negative), fees (positive)
  - Include validation rules (e.g., "Sum of subtotals should equal Total Current Charges")

**2. JSON Schema Requirements**

- **Field Naming**: English, descriptive, snake_case
- **Data Types**: accurate types (`string`, `number`, `array`, `object`, `boolean`)
- **Structure**: mirror document hierarchy using `object` for groups, `array` for lists/tables
- **Metadata**: add `description` to each field; use `required` array for mandatory fields

## MCP Integration Guidelines

### Available MCP Tools

Use the appropriate MCP tool for each domain:

| Domain | MCP Tool | Usage |
|--------|----------|-------|
| AWS Resources | AWS API MCP | Inspect or modify S3 buckets, Lambda functions, etc. |
| Database | PostgreSQL MCP | Query, inspect, or modify database records |
| UI Testing | Playwright MCP | Inspect or interact with UI elements |
| Code Assistance | Codex MCP | AI-assisted code analysis, prototyping, and review |

### Resource Management

**Browser Resources (Playwright MCP)**
- Always clean up browser contexts and instances after use
- Prevents resource leaks, especially network connections
- Close contexts explicitly when done with UI operations

### Codex Collaboration Workflow

Codex MCP serves as an objective analysis partner for **non-implementation tasks only**. Follow these steps:

1. **Requirements Analysis**: Share requirements and approach with Codex for refinement and validation
2. **Design & Planning**: Use Codex to help design architecture, plan implementation steps, and identify potential issues
3. **Troubleshooting**: When debugging, use Codex to analyze error logs, trace issues, and suggest fixes
4. **Code Review**: After Claude Code CLI implements changes, use Codex to review and verify requirement completion
5. **Critical Thinking**: Codex provides analysis and recommendations only. Maintain independent judgment and challenge Codex's answers when appropriate

**Remember**: Codex analyzes and advises; Claude Code CLI implements. Never use Codex with `sandbox="workspace-write"` or for actual code modifications.

### Codex Tool Specification

**Tool Overview**

Codex MCP provides the `codex` tool for AI-assisted coding tasks via MCP protocol (no CLI needed).

**Required Parameters**
- `PROMPT` (string): Task instruction for Codex
- `cd` (Path): Working directory root path

**Optional Parameters**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `sandbox` | string | "read-only" | Sandbox policy: "read-only", "workspace-write", "danger-full-access" |
| `SESSION_ID` | UUID | null | Continue previous session for multi-turn interaction |
| `skip_git_repo_check` | boolean | false | Allow running in non-Git repositories |
| `return_all_messages` | boolean | false | Return all messages including reasoning and tool calls |
| `image` | List[Path] | null | Attach image files to the prompt |
| `model` | string | null | Specify model (uses user default if null) |
| `yolo` | boolean | false | Run without approvals (skips sandbox) |
| `profile` | string | null | Load config from `~/.codex/config.toml` |

**Return Value**
```json
{
  "success": true,
  "SESSION_ID": "uuid-string",
  "agent_messages": "response text",
  "all_messages": []  // Only when return_all_messages=true
}
```

**Usage Guidelines**
- **ALWAYS use `sandbox="read-only"`** - Codex must never modify files directly
- Save `SESSION_ID` from each call to continue conversations
- Ensure `cd` points to an existing directory
- Set `return_all_messages=true` when debugging or tracing reasoning
- Use Codex for: planning, analysis, troubleshooting, code review, design thinking
- Do NOT use Codex for: writing code, editing files, creating components (use Claude Code CLI instead)
