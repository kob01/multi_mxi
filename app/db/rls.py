"""数据库层强制隔离: 最小权限角色 + RLS 策略 + 审计表不可变性(层 1 的唯一真防线)。

为什么必须有这一层: 前面所有"模型不许写别的部门"的约束都是**代码里的**约束, 一句
prompt 注入没挡住、一处模板漏写谓词, 越权就发生了, 而且不会留下"本该挡住"的痕迹。
RLS 把判定下推到数据库: 即使上面的 SQL 是模型自由发挥的、即使某个 SELECT 忘了加
过滤, 引擎也只会返回作用域内的行。

幂等与可达性: 全部 DDL 走 ``init_schema()`` 尾部, 因为 ``docker/init/*.sql`` 只在**空卷
首次初始化**时执行 —— 存量库永远不会跑它, 把隔离写在那儿等于写进文档。

三条必须知道的边界:
1. **属主会绕过 RLS**, 所以这里同时做两件事: ``FORCE ROW LEVEL SECURITY``(对属主也生效)
   与"取数/写执行前 ``SET ROLE`` 到非超级用户角色"(见 app/db/sync.py)。少任何一条,
   策略都只是 pg_policies 里的一行字。
2. **登录账号仍是有权限的属主**(要跑 DDL 与其余业务), 因此"切角色"必须由服务端做,
   不能指望调用方自觉 —— 这正是 analytics 域单独建引擎的原因。
3. **非超级用户部署**(云托管 PG 里没有 CREATE ROLE 权限)会抛明确错误而不是静默跳过,
   与 pgvector 扩展的失败口径一致。
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.config import get_settings
from app.db.policy import WRITE_ENTITIES, checked_ident
from app.db.scope import GUC_DEPT_SCOPE, GUC_TENANT, SCOPED_TABLES

logger = logging.getLogger(__name__)

# 审计与回滚数据: 只有属主(维护任务)能删, 任何人都不能改。
IMMUTABLE_TABLES = ("sql_audit_records", "dataop_before_image", "dataop_archive")

_POLICY_NAME = "mxi_tenant_dept"


class RlsError(RuntimeError):
    """RLS DDL 执行失败(权限不足或对象状态异常)。"""


def _role_or_none(value: str) -> str | None:
    role = (value or "").strip()
    return checked_ident(role) if role else None


def read_role() -> str | None:
    """analytics 取数应切到的只读角色(配置为空 = 不切, 仅供排障)。"""
    return _role_or_none(get_settings().pg_role_analytics_read)


def write_role() -> str | None:
    """写执行应切到的角色(只有白名单表的指定列 UPDATE/INSERT)。"""
    return _role_or_none(get_settings().pg_role_analytics_write)


async def _role_exists(conn, role: str) -> bool:
    return bool(
        (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
            )
        ).first()
    )


async def _create_role(conn, role: str) -> None:
    """建 NOLOGIN NOINHERIT NOSUPERUSER 角色(已存在则跳过)。

    - NOLOGIN: 没有任何外部连接能用它直连, 只能由属主 ``SET ROLE`` 切入;
      于是"凭据"这件事仍然只有一份, 但有效权限被切成两份。
    - NOINHERIT: 成员关系不会把角色的权限"顺带"漏给登录账号, 只有显式 SET ROLE 才有。
    - NOBYPASSRLS: 显式写下, 防以后有人改了它却看不出来。
    """
    if await _role_exists(conn, role):
        return
    try:
        await conn.execute(
            text(
                f"CREATE ROLE {role} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOBYPASSRLS"
            )
        )
    except Exception as exc:  # noqa: BLE001 - 包装成可定位的口径(与 pgvector 失败一致)
        raise RlsError(
            f"无法创建最小权限角色 {role}: 当前登录账号不是超级用户也没有 CREATEROLE 权限({exc}); "
            "托管 PG 请让超级用户预建 mxi_analytics_read / mxi_analytics_write 并 GRANT 给登录账号"
        ) from exc
    logger.info("[rls] 已创建最小权限角色 %s", role)


async def _grant_table_sequences(conn, table: str, role: str) -> None:
    """把某表自己的序列授给角色(主键自增需要 USAGE)。

    序列名不假设 ``<table>_id_seq``: SQLAlchemy 的约定是 ``<table>_<列名>_seq``,
    从 information_schema 查实际名字才能对得准, 查不到就是这张表没有自增列。
    """
    rows = (
        await conn.execute(
            text("SELECT sequence_name FROM information_schema.sequences "
                 "WHERE sequence_schema = 'public' AND sequence_name LIKE :pattern"),
            {"pattern": f"{table}_%_seq"},
        )
    ).all()
    for (seq,) in rows:
        await conn.execute(text(f"GRANT USAGE ON SEQUENCE {checked_ident(seq)} TO {role}"))


async def ensure_db_principals() -> None:
    """建两个最小权限角色、将成员关系授给登录账号, 并把授权与资源上限绑到角色上。"""
    from app.db.session import get_engine

    s = get_settings()
    ro, rw = read_role(), write_role()
    if not ro and not rw:
        logger.warning("[rls] 未配置 analytics 角色(PG_ROLE_ANALYTICS_*), 跳过角色建立")
        return
    login = checked_ident(s.pg_user)

    async with get_engine().begin() as conn:
        for role in filter(None, (ro, rw)):
            await _create_role(conn, role)
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(text(f"GRANT {role} TO {login}"))
            # 语句超时绑在角色上: 即使代码忘了 set_config, 服务端仍有一个硬上限。
            # 这里必须把值写进语句文本而不是用绑参: ALTER ROLE ... SET 不接受参数占位符
            # (asyncpg 会在 $1 处报 syntax error), 所以只允许经过 int() 的纯数字。   
            await conn.execute(
                text(f"ALTER ROLE {role} SET statement_timeout = {int(s.pg_statement_timeout_ms)}")
            )
            await conn.execute(
                text(
                    f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = "
                    f"{int(s.pg_statement_timeout_ms) * 2}"
                )
            )

        if ro:
            # 只读角色: 表级 SELECT(不给列级: 列级授权会让 Text2SQL 的高频写法
            # SELECT * 直接报权限错; 列的机密性由出口 DLP 负责, 那是"能不能给人看")。
            for table in SCOPED_TABLES:
                await conn.execute(text(f"GRANT SELECT ON {table} TO {ro}"))
            # 部门注册表也可见: 作用域回显要把 dept_id 说成部门名。
            await conn.execute(text(f"GRANT SELECT ON sys_departments TO {ro}"))

        if rw:
            for spec in WRITE_ENTITIES.values():
                # 授权列集取 spec.grant_update_columns: 可写字段 + 软删三件套。
                # 只授可写字段的话, 软删模板会在 PG 侧碰列级权限不足。
                cols = ", ".join(spec.grant_update_columns)
                # 先收回到零再精确授权: 重跑本函数时不会留下"上一版授过、这一版不该有"
                # 的残余权限(比如某天把某张表从可写名单里拿掉)。
                await conn.execute(text(f"REVOKE ALL ON {spec.table} FROM {rw}"))
                await conn.execute(text(f"GRANT SELECT ON {spec.table} TO {rw}"))
                await conn.execute(
                    text(f"GRANT UPDATE ({cols}) ON {spec.table} TO {rw}")
                )
                if spec.insertable:
                    await conn.execute(
                        text(f"GRANT INSERT ({cols}) ON {spec.table} TO {rw}")
                    )
                # 注意这里永远不含 DELETE: 真删只能由属主的保留期任务执行。
            # 写角色只能 SELECT/INSERT 镜像与归档, 不能 UPDATE/DELETE 它们:
            # 变更前镜像是回滚的唯一依据, 能改就等于没有。
            for table in ("dataop_before_image", "dataop_archive"):
                await conn.execute(text(f"REVOKE ALL ON {table} FROM {rw}"))
                await conn.execute(text(f"GRANT SELECT, INSERT ON {table} TO {rw}"))
                await _grant_table_sequences(conn, table, rw)

        # 控制面表(写计划状态机 + 审计)对两个 analytics 角色都不可见。
        for table in ("dataops_pending", "sql_audit_records"):
            for role in filter(None, (ro, rw)):
                await conn.execute(text(f"REVOKE ALL ON {table} FROM {role}"))


async def ensure_rls_policies() -> None:
    """对每张作用域表 ENABLE + FORCE ROW LEVEL SECURITY 并重建策略。

    策略形状(读用 USING, 写用 WITH CHECK, 同一表达式):

        tenant_id = coalesce(current_setting('app.tenant_id', true), '__none__')
        AND dept_id = ANY(string_to_array(
              coalesce(current_setting('app.dept_scope', true), '__unassigned__'), ','))

    两个 coalesce 的哨兵值都不是任何真实归属, 所以 **GUC 缺失 = 一行也看不见**:
    忘记注入作用域的后果是"查不到数据"(可发现), 不是"查到全部数据"(不可发现)。
    """
    from app.db.session import get_engine

    using_expr = (
        f"tenant_id = coalesce(current_setting('{GUC_TENANT}', true), '__none__') "
        f"AND dept_id = ANY(string_to_array("
        f"coalesce(current_setting('{GUC_DEPT_SCOPE}', true), '__unassigned__'), ','))"
    )

    async with get_engine().begin() as conn:
        for table in SCOPED_TABLES:
            await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            # FORCE 是必须的: 没有它, 表属主(登录账号 mxi)默认绕过所有策略。
            await conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            await conn.execute(
                text(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}")
            )
            await conn.execute(
                text(
                    f"CREATE POLICY {_POLICY_NAME} ON {table} AS PERMISSIVE FOR ALL "
                    f"TO PUBLIC USING ({using_expr}) WITH CHECK ({using_expr})"
                )
            )
        # 控制面表(写计划状态机)也进策略面: 它没有 tenant 列, 用"只有属主能读"的方式
        # 处理 —— 上面已 REVOKE 掉两个 analytics 角色, 这里不再建策略。
    logger.info("[rls] 已对 %d 张表启用并强制行级安全", len(SCOPED_TABLES))


async def ensure_audit_immutability() -> None:
    """审计/镜像/归档表的"逻辑不可变": 收回 UPDATE/DELETE + RULE 拦属主改写。

    注意边界: PG 的 RULE/TRIGGER 拦得住 UPDATE, 但保留期清理需要 DELETE, 所以
    DELETE 只**收回到属主手里**(维护任务用), 不禁止; "不可篡改"在这里的含义是
    "写进去之后没人能改它的内容", 而不是"没人能删"。真 WORM 需要外部存储。
    """
    from app.db.session import get_engine

    async with get_engine().begin() as conn:
        for table in IMMUTABLE_TABLES:
            await conn.execute(text(f"REVOKE UPDATE ON {table} FROM PUBLIC"))
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_rules WHERE rulename = :n AND tablename = :t"),
                    {"n": f"{table}_no_update", "t": table},
                )
            ).first()
            if not exists:
                await conn.execute(
                    text(f"CREATE RULE {table}_no_update AS ON UPDATE TO {table} "
                         "DO INSTEAD NOTHING")
                )


async def rls_status() -> dict[str, object]:
    """自查看一眼当前隔离状态(给 dev_services check 与排障用, 不改变任何东西)。"""
    from app.db.session import get_engine

    async with get_engine().connect() as conn:
        policies = (
            await conn.execute(
                text("SELECT tablename, policyname, cmd FROM pg_policies "
                     "WHERE policyname = :n ORDER BY tablename"),
                {"n": _POLICY_NAME},
            )
        ).all()
        forced = (
            await conn.execute(
                text("SELECT relname FROM pg_class WHERE relrowsecurity = true "
                     "AND relforcerowsecurity = true ORDER BY relname")
            )
        ).all()
        roles = (
            await conn.execute(
                text("SELECT rolname, rolcanlogin, rolsuper, rolbypassrls FROM pg_roles "
                     "WHERE rolname IN (:ro, :rw) ORDER BY rolname"),
                {"ro": read_role() or "-", "rw": write_role() or "-"},
            )
        ).all()
    return {
        "policies": [{"table": p[0], "name": p[1], "cmd": p[2]} for p in policies],
        "forced_tables": [f[0] for f in forced],
        "roles": [
            {"name": r[0], "can_login": r[1], "super": r[2], "bypass_rls": r[3]}
            for r in roles
        ],
    }
