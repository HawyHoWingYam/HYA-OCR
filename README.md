# HYA-OCR Backend

文档识别与映射管理的 FastAPI 后端，集成 Google Gemini 结构化抽取、PostgreSQL、S3 文件存储与 OneDrive 自动同步能力。

## 运行环境
- Python 3.10–3.12（建议 3.11/3.12）
- 可选：PostgreSQL（生产环境推荐）
- 可选：AWS 账号与 S3（当 `STORAGE_BACKEND=s3`）
- 可选：Microsoft Entra ID 应用（启用 OneDrive 同步时）

## 安装
- 创建虚拟环境并安装依赖
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install --upgrade pip`
  - `pip install -r requirements.txt`

## 配置（backend.env）
后端仅从环境变量读取配置。建议在 `backend/` 目录放置 `backend.env` 文件（已被自动加载）。必填/可选项如下：

- 基础应用
  - `API_BASE_URL`：前端/调用方访问的 API 根，例如 `http://localhost:8000`
  - `PORT`：服务端口，例如 `8000`
  - `MODEL_NAME`：Gemini 模型名，例如 `gemini-1.5-pro` 或 `gemini-1.5-flash`
  - `ENVIRONMENT`：`development`/`test`/`sandbox`/`uat`/`production`
    - 注意：`development`/`test` 环境会自动建表，其他环境请使用迁移或 `init_db.py`

- 数据库
  - `DATABASE_URL`：PostgreSQL/SQLite 连接串，例如：
    - PostgreSQL：`postgresql://user:password@host:5432/dbname`
    - SQLite（开发用）：`sqlite:///./dev.db`

- Gemini API Key（至少一种方式其一）
  - `GEMINI_API_KEY` 或 `API_KEY`
  - 或多把轮换：`GEMINI_API_KEY_1`、`GEMINI_API_KEY_2`、…

- 存储后端（二选一）
  - 本地：
    - `STORAGE_BACKEND=local`
    - `LOCAL_UPLOAD_DIR=/absolute/path/to/uploads`（可写目录）
  - S3：
    - `STORAGE_BACKEND=s3`
    - `S3_BUCKET_NAME=your-bucket`
    - `AWS_DEFAULT_REGION=ap-southeast-1`
    - `AWS_ACCESS_KEY_ID=...`
    - `AWS_SECRET_ACCESS_KEY=...`

- OneDrive 同步（可选）
  - `ONEDRIVE_SYNC_ENABLED=true|false`
  - 若启用，需要：
    - `ONEDRIVE_CLIENT_ID`、`ONEDRIVE_CLIENT_SECRET`
    - `ONEDRIVE_TENANT_ID`、`ONEDRIVE_TARGET_USER_UPN`（目标用户 UPN）

- 提示/Schema 管理（可选）
  - `PROMPT_SCHEMA_CACHE_ENABLED=true|false`
  - `PROMPT_SCHEMA_CACHE_SIZE=200`
  - `PROMPT_SCHEMA_LOCAL_BACKUP_PATH=/path/to/backup`

示例 `backend.env` 片段：
```
API_BASE_URL=http://localhost:8000
PORT=8000
ENVIRONMENT=development
MODEL_NAME=gemini-1.5-pro

# 例：本地開發數據庫
DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/hya
# 例：RDS（production）格式
# DATABASE_URL=postgresql+psycopg2://dbmasteruser:<password>@ls-c61bcd2164a8d5b9fb0ea1d96e90291bada031ae.c7yiqy8go8c7.ap-southeast-1.rds.amazonaws.com:5432/document_processing_platform?sslmode=require

# 至少配置一把
GEMINI_API_KEY=your-gemini-key
# 或多把轮换
# GEMINI_API_KEY_1=xxx
# GEMINI_API_KEY_2=yyy

# 本地文件存储
STORAGE_BACKEND=local
LOCAL_UPLOAD_DIR=/data/hya-ocr

# 推荐的本地目录结构（相对于 LOCAL_UPLOAD_DIR）：
#   orders/
#     {order_id}/
#       items/{item_id}/                # 主文件、附件、合并 CSV 等
#       mapping/                        # 映射配置上传（本地模式）
#   prompt_schema/
#     {company_code}/{doc_type_code}/
#       prompt/{filename}.txt          # Prompt 文本
#       schema/{filename}.json         # Schema JSON
#   configs/
#     {company_id}/{doc_type_id}/
#       prompts/{original_filename}.txt
#       schemas/{original_filename}.json
#   templates/
#     document_types/{doc_type_id}/
#       template_{version}.json        # 文档类型模板（本地模式）
#
# 若使用 S3 改为：
# STORAGE_BACKEND=s3
# S3_BUCKET_NAME=hya-ocr-pro
# AWS_DEFAULT_REGION=ap-southeast-1
# AWS_ACCESS_KEY_ID=...
# AWS_SECRET_ACCESS_KEY=...

# OneDrive（可选）
ONEDRIVE_SYNC_ENABLED=false
# 若启用：
# ONEDRIVE_CLIENT_ID=...
# ONEDRIVE_CLIENT_SECRET=...
# ONEDRIVE_TENANT_ID=...
# ONEDRIVE_TARGET_USER_UPN=user@domain.com
```

## 启动
- 方式一（读取 `PORT` 配置）：
  - `python app.py`
- 方式二（手动指定端口）：
  - `uvicorn app:app --host 0.0.0.0 --port 8000`

访问：
- 健康检查：`GET /health`
- OpenAPI 文档：`/docs`（Swagger UI）
- WebSocket：`/ws/orders/{order_id}`（订单状态推送）

## 数据库初始化与检查
- 初始化（建表/索引/初始数据）：`python init_db.py`
- 健康检查：`python check_db.py`

注意：`ENVIRONMENT=development|test` 时，应用启动会尝试自动建表；生产环境建议使用迁移或运行 `init_db.py` 来显式创建。

## OneDrive 集成
- 库：`O365`
- 在 `app.py` 中内置了后台任务调度（可选，需安装 APScheduler）。也可使用脚本：
  - 访问验证：`python scripts/verify_onedrive_access.py`
  - 导入/对账：`python scripts/onedrive_ingest.py`（被 API 调用或计划任务触发）

## 依赖说明（要点）
- Web：FastAPI、Uvicorn、Starlette、python-multipart
- DB：SQLAlchemy、psycopg2-binary、asyncpg（脚本与池化使用）
- AI：google-generativeai（支持 response_schema 结构化输出）
- 数据处理：pandas、numpy、openpyxl、Pillow、opencv-python-headless（可选）
- 云服务：boto3（S3/Secrets 等）
- 其它：python-dotenv、PyYAML、simpleeval、APScheduler（可选）、O365（OneDrive）

## 常见问题
- 启动报缺少环境变量：请检查 `backend.env` 是否包含必填项，或在当前终端 `export VAR=value`。
- S3 上传失败：确认 `S3_BUCKET_NAME`、`AWS_*` 与 `AWS_DEFAULT_REGION` 是否正确，且凭证具备权限。
- 结构化抽取报错：确认 Gemini 模型名与 API Key 有效；可在 `main.py` 里打开更多日志。
- OpenCV 不可用：该库仅用于增强预处理，缺失时会自动降级为原始图像处理。

---
如需我协助生成一份最小可用的 `backend.env` 或帮你运行本地健康检查，请告诉我当前运行环境与数据库信息。
