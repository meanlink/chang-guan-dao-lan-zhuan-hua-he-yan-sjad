# 场馆导览转化核验

分类独立性检验的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览、审计轨迹和防篡改凭据。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。

## 防篡改凭据与核验包
客流接入、核验完成、批准结论、简报归档四类业务动作各写一条链式凭据（`ledger_entries`）：序号连续递增，每条记录前一条的摘要，任何改写、抽条、调序都会让后续摘要对不上。业务写入与凭据写入在同一数据库事务提交，任一方失败整体回滚，不存在“业务已落定但凭据缺失”；并发请求在单行链状态上排队，仍按唯一序号衔接；带 `Idempotency-Key` 的重试回放首次结果，不重复办理也不新增凭据。

负责人（admin）在“凭据核验”页按截至序号截取核验包，并为它另存一份 Ed25519 签名的可信锚点。同一截止序号只对应一个包，重试复用；出包中断后按同一份内容从中间文件续写，之后到达的凭据留待下一包。核验包、锚点互不依赖原库：`python -m scripts.ledger_verify 核验包.json --anchor 锚点.json`（`--key-id` 传入预先登记的公钥指纹可防锚点被替换）即可离线查出改文、缺项、调序与尾部截断，结果分为“完整 / 受损 / 缺少可信依据”。页面同一入口支持制作、下载、上传检查；旧的操作审计仅作历史展示，不冒充受保护记录，也不进入核验包。


## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18204；后端文档 http://localhost:19204/docs；数据库宿主端口 17704。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；PostgreSQL 16；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。
凭据与核验包的工程测试不依赖 Docker、Redis 或 PostgreSQL（使用临时 SQLite 与临时产物目录）：
```bash
cd backend && python -m unittest discover -s tests -v
```
离线核验可脱离本工程运行，只需 `backend/src/services/ledger_package.py` 及标准库；确认锚点签名时另需 `cryptography`，缺失时会明确报告“缺少可信依据”而不是误判完整。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py、backend/scripts/ledger_verify.py；backend/migrations/initial.py；backend/tests/test_ledger.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=guide-chi；FRONTEND_PORT=18204；BACKEND_PORT=19204；DB_PORT=17704；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1005/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
