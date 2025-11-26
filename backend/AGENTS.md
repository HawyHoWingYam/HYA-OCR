# Repository Guidelines

## Project Structure & Module Organization
- `app.py`: main FastAPI entrypoint; HTTP and WebSocket APIs.
- `main.py`: Gemini OCR integration and image/PDF preprocessing helpers.
- `db/`: SQLAlchemy models and database utilities; use `init_db.py` and `check_db.py` for schema setup and health checks.
- `utils/`: shared services (file storage, template/prompt managers, cost allocation helpers, OneDrive/S3 utilities).
- `scripts/`: operational/maintenance scripts (OneDrive ingest, path migrations, exports).
- `cost_allocation/`: cost allocation matching, reporting, and NetSuite export helpers.

## Build, Test, and Development Commands
- Create env and install dependencies:  
  `python -m venv .venv && source .venv/bin/activate`  
  `pip install -r requirements.txt`
- Run API locally (uses `backend.env`):  
  `python app.py`  
  or `uvicorn app:app --reload --port 8000`
- Database setup and checks:  
  `python init_db.py` and `python check_db.py`
- Run tests (pytest):  
  `pytest` or `pytest path/to/test_file.py`

## Coding Style & Naming Conventions
- Python 3.10–3.12, PEP 8, 4-space indentation.
- Use `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants and env keys.
- Prefer type hints and explicit imports over wildcards.
- Keep FastAPI routes thin; move business logic into `utils/`, `db/`, or dedicated service modules.

## Testing Guidelines
- Use `pytest` and `pytest-asyncio` for new tests; place them under `tests/` mirroring the module path (for example, `tests/test_app_routes.py`).
- Name tests `test_<behavior>` and cover both success and error paths for new endpoints and scripts.
- For database-related tests, prefer SQLite or disposable schemas; avoid pointing to shared production data.

## Commit & Pull Request Guidelines
- Follow Conventional Commits: `feat:`, `fix:`, `chore:`, `refactor:`, etc. (for example, `feat: add OCR schedule API`).
- Pull requests should include: clear summary, motivation or linked issue, notes on configuration or migrations, and local testing evidence (`pytest`, key endpoint checks).
- Keep changes focused and small; update `README.md` or this file when behavior or setup materially changes.

## Architecture Overview
- The API layer (`app.py`) exposes HTTP/WebSocket endpoints and orchestrates calls into `main.py`, `db/`, and `utils/`.
- OCR and structured extraction live in `main.py` and related helpers under `utils/`, while persistence is handled through SQLAlchemy models in `db/`.
- Long-running tasks (OCR batches, cost allocation, OneDrive sync) use background tasks, schedulers, or scripts in `scripts/` and `utils/`.
- Storage is abstracted behind file managers and S3/local backends (`utils/file_storage.py`, `utils/s3_storage.py`) configured via `backend.env`.

## Security & Configuration Tips
- Never commit real secrets or `.env` files; use `backend.env` locally and document required variables.
- When adding new services or integrations (S3, OneDrive, Gemini models), prefer configuration via environment variables and keep secure defaults (timeouts, retries, validation).

## Agent-Specific Instructions
- Preserve existing public APIs and response schemas; avoid breaking route paths or payload shapes without updating the frontend and `README.md`.
- When adding new endpoints, prefer small, composable helpers in `utils/` and keep side effects (file I/O, network calls) well-isolated.
- Do not modify `.venv/` or commit generated artifacts; focus changes on source, tests, and scripts.
- If unsure about config or secrets, assume they come from `backend.env` and avoid hardcoding values in code or tests.

