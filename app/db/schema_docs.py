"""Business table schemas exposed to agents for Text2SQL prompt injection.

单一事实来源: 与 app/db/models.py 保持一致; 修改表结构时同步更新此处。
HR/Finance Agent 的 System Prompt 会注入对应 DDL 说明, LLM 据此生成 SQL,
最终由 MCP server 的 execute_sql 工具做只读硬校验 (app/db/sql_guard.py)。

注意: 发给 LLM 的只是字段说明, 不是真实建表语句, 不含任何数据。
"""

from __future__ import annotations

# 方言语义: LLM 习惯写 MySQL, 这里显式约束到 PostgreSQL, 避免
# DATE_FORMAT / IFNULL / GROUP_CONCAT 这类在上游必然报错的写法。
SQL_DIALECT_NOTES = """\

SQL 方言 (PostgreSQL) 硬性要求:
- 只写单条 SELECT (或 WITH ... SELECT), 不要分号、不要注释、不要 SET/COPY/INTO。
- 日期格式化用 to_char(col, 'YYYY-MM-DD'); 取年份用 EXTRACT(YEAR FROM col)
  或 date_part('year', col); 没有 DATE_FORMAT / CURDATE / IFNULL。
- 空值兜底用 COALESCE; 字符串聚合用 string_agg(x, ',') (没有 GROUP_CONCAT)。
- 取当年数据: created_at >= date_trunc('year', now())。
- 表/列名小写不加反引号, 字符串用单引号; 无 LIMIT 时系统会自动补 LIMIT 50。"""

HR_SCHEMA_DDL = """\
-- HR 业务表 (只读, Text2SQL 可查)
hr_employees 员工主数据:
  emp_id VARCHAR(32) PK      -- 工号, 如 E1001
  name VARCHAR(64)           -- 姓名
  department VARCHAR(64)     -- 部门: 研发部/市场部/人事部/财务部
  position VARCHAR(64)       -- 职位
  hire_date DATE             -- 入职日期
  annual_leave_total INT     -- 年假总额(天)
  annual_leave_used INT      -- 已用年假(天)
  status VARCHAR(16)         -- 在职/离职

hr_tickets HR 工单:
  ticket_no VARCHAR(32) PK   -- 工单号, 如 HR1000
  emp_id VARCHAR(32)         -- 提单人工号 (可 JOIN hr_employees)
  category VARCHAR(32)       -- 类别: 入职/离职/考勤/薪酬/证明开具/其他
  title VARCHAR(255)         -- 标题
  description TEXT           -- 详细描述
  status VARCHAR(16)         -- OPEN/PROCESSING/DONE/CANCELLED
  created_at TIMESTAMPTZ     -- 创建时间

hr_leave_records 请假记录:
  id BIGINT PK 自增
  emp_id VARCHAR(32)         -- 请假人工号
  leave_type VARCHAR(16)     -- 类型: 年假/事假/病假/调休/婚假
  start_date DATE, end_date DATE
  days DECIMAL(5,1)          -- 请假天数
  status VARCHAR(16)         -- 审批中/已批准/已驳回
  created_at TIMESTAMPTZ""" + SQL_DIALECT_NOTES

FINANCE_SCHEMA_DDL = """\
-- Finance 业务表 (只读, Text2SQL 可查; hr_employees 用于员工->部门关联)
fin_reimbursements 报销单:
  order_no VARCHAR(32) PK    -- 单号, 如 FIN5000
  emp_id VARCHAR(32)         -- 报销人工号 (可 JOIN hr_employees 取部门)
  title VARCHAR(255)         -- 费用事项
  amount DECIMAL(12,2)       -- 金额(元)
  category VARCHAR(32)       -- 类别: 差旅费/交通费/餐饮费/办公用品/培训费
  reason TEXT                -- 事由
  status VARCHAR(16)         -- SUBMITTED(已提交)/APPROVED(已批准)/REJECTED(已驳回)/PAID(已打款)
  current_node VARCHAR(64)   -- 当前审批节点
  created_at TIMESTAMPTZ     -- 提交时间

fin_department_budgets 部门年度预算:
  department VARCHAR(64)     -- 部门
  year INT                   -- 年度
  annual_budget DECIMAL(14,2) -- 年度预算总额(元)
  used_amount DECIMAL(14,2)  -- 已用金额(元)

hr_employees 员工主数据:
  emp_id VARCHAR(32) PK, name VARCHAR(64), department VARCHAR(64),
  position VARCHAR(64), hire_date DATE, status VARCHAR(16)""" + SQL_DIALECT_NOTES


# ---------------------------------------------------------------------------
# 数据洞察 (Analyst_Agent): 跨域可查表的全量字段说明
# ---------------------------------------------------------------------------
# 与 HR/FINANCE 两份 DDL 的区别: 那两份是给"单域智能体"看的, 本份是跨域汇总,
# 供 analytics 的 run_sql 使用同一张表白名单 (见 app/mcp_servers/analytics_server.py)。
#
# tenant_id/dept_id 必须写进来但不是"给模型用的过滤条件": 它们由数据库的行级安全
# (RLS)自动生效, 说明写在这里是为了让模型知道"为什么我只看到部分行", 而不是
# 看到空结果就去编一个 WHERE tenant_id='T001' 出来。
ANALYTICS_SCHEMA_DDL = """\
-- 数据洞察可查表 (只读, 跨 HR/Finance/Procurement 三域)
-- 每张业务表都有 tenant_id / dept_id 两列: 它们由服务端行级安全自动限定,
-- 你不需要也不应该自己写这两个条件。已软删除的行(is_deleted=true)不在分析范围内。
hr_employees 员工主数据:
  emp_id VARCHAR(32) PK, name VARCHAR(64), department VARCHAR(64),
  position VARCHAR(64), hire_date DATE, annual_leave_total INT,
  annual_leave_used INT, status VARCHAR(16),  -- 在职/离职
  tenant_id VARCHAR(32), dept_id VARCHAR(32)  -- 作用域列(服务端维护)

hr_tickets HR 工单:
  ticket_no VARCHAR(32) PK, emp_id VARCHAR(32), category VARCHAR(32),
  title VARCHAR(255), description TEXT, status VARCHAR(16),  -- OPEN/PROCESSING/DONE/CANCELLED
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ

hr_leave_records 请假记录:
  id BIGINT PK, emp_id VARCHAR(32), leave_type VARCHAR(16),
  start_date DATE, end_date DATE, days DECIMAL(5,1),
  status VARCHAR(16), created_at TIMESTAMPTZ  -- 审批中/已批准/已驳回

fin_reimbursements 报销单:
  order_no VARCHAR(32) PK, emp_id VARCHAR(32), title VARCHAR(255),
  amount DECIMAL(12,2), category VARCHAR(32), reason TEXT,
  status VARCHAR(16),  -- SUBMITTED/APPROVED/REJECTED/PAID
  current_node VARCHAR(64), created_at TIMESTAMPTZ

fin_department_budgets 部门年度预算:
  id BIGINT PK, department VARCHAR(64), year INT,
  annual_budget DECIMAL(14,2), used_amount DECIMAL(14,2)

proc_orders 采购申请单:
  order_no VARCHAR(32) PK, emp_id VARCHAR(32), department VARCHAR(64),
  title VARCHAR(255), category VARCHAR(64), amount DECIMAL(14,2),
  currency VARCHAR(8), supplier_name VARCHAR(128), quotes_count INT,
  budget_year INT, reason TEXT, status VARCHAR(16),  -- DRAFT/PRECHECK/PENDING/APPROVED/REJECTED/PAID
  current_node VARCHAR(64), precheck_result TEXT, created_at TIMESTAMPTZ

proc_contracts 合同台账:
  contract_no VARCHAR(32) PK, title VARCHAR(255), party_a VARCHAR(128),
  party_b VARCHAR(128), category VARCHAR(64), amount DECIMAL(14,2),
  currency VARCHAR(8), sign_date DATE, effective_date DATE, expiry_date DATE,
  status VARCHAR(16),  -- DRAFT/PRECHECKED/APPROVED/RISK/REJECTED
  risk_level VARCHAR(16),  -- 低/中/高
  reviewer VARCHAR(64), opinion TEXT, created_at TIMESTAMPTZ

proc_suppliers 供应商主数据:
  supplier_code VARCHAR(32) PK, name VARCHAR(128), category VARCHAR(64),
  qualification VARCHAR(32), risk_status VARCHAR(16)  -- 正常/关注/黑名单""" + SQL_DIALECT_NOTES


# ---------------------------------------------------------------------------
# 采购与合同 (Contract_Agent)
# ---------------------------------------------------------------------------
PROCUREMENT_SCHEMA_DDL = """\
-- Procurement 业务表 (只读, Text2SQL 可查; hr_employees/fin_department_budgets 用于关联)
proc_orders 采购申请单:
  order_no VARCHAR(32) PK   -- 单号, 如 PO3000
  emp_id VARCHAR(32)        -- 申请人工号 (可 JOIN hr_employees)
  department VARCHAR(64)    -- 申请部门 (预算判定用)
  title VARCHAR(255)        -- 采购事项
  category VARCHAR(64)      -- IT设备/办公用品/咨询服务/市场推广/培训服务/其他
  amount DECIMAL(14,2)      -- 金额(元)
  currency VARCHAR(8)       -- CNY/USD
  supplier_name VARCHAR(128)-- 供应商名称 (可 JOIN proc_suppliers)
  quotes_count INT          -- 比价份数 (合规门槛: 金额>5000 需 >=3)
  budget_year INT           -- 占用预算年度
  reason TEXT               -- 申请事由
  status VARCHAR(16)        -- DRAFT/PRECHECK/PENDING/APPROVED/REJECTED/PAID
  current_node VARCHAR(64)  -- 当前节点
  precheck_result TEXT      -- 初审结论摘要
  created_at TIMESTAMPTZ

proc_contracts 合同台账:
  contract_no VARCHAR(32) PK, title VARCHAR(255),
  party_a VARCHAR(128), party_b VARCHAR(128), category VARCHAR(64),
  amount DECIMAL(14,2), currency VARCHAR(8),
  sign_date DATE, effective_date DATE, expiry_date DATE,
  status VARCHAR(16), risk_level VARCHAR(16),  -- 低/中/高
  reviewer VARCHAR(64), opinion TEXT, created_at TIMESTAMPTZ

proc_suppliers 供应商主数据:
  supplier_code VARCHAR(32) PK, name VARCHAR(128), category VARCHAR(64),
  bank_account VARCHAR(64), qualification VARCHAR(32),
  risk_status VARCHAR(16)  -- 正常/关注/黑名单

fin_department_budgets 部门年度预算 (采购占用同一预算池):
  department VARCHAR(64), year INT, annual_budget DECIMAL(14,2), used_amount DECIMAL(14,2)

hr_employees 员工主数据:
  emp_id VARCHAR(32) PK, name VARCHAR(64), department VARCHAR(64),
  position VARCHAR(64), status VARCHAR(16)""" + SQL_DIALECT_NOTES
