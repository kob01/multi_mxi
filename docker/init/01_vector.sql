-- PostgreSQL 容器首次初始化时以超级用户执行 (docker-entrypoint-initdb.d)。
--
-- 为什么在这里装扩展, 而不是靠应用启动时的 CREATE EXTENSION:
-- 生产/云上的应用账号通常没有创建扩展的权限, 让 init_schema() 独占这一步
-- 会导致网关启动即失败。容器化部署由镜像超级用户一次性装好, 应用侧只需
-- 具备使用 vector 类型的权限即可 (pgvector 官方镜像自带该扩展)。
CREATE EXTENSION IF NOT EXISTS vector;
