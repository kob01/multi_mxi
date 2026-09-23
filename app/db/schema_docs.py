"""Business table schemas exposed to agents for Text2SQL prompt injection.

单一事实来源: 与 app/db/models.py 保持一致; 修改表结构时同步更新此处。
HR/Finance Agent 的 System Prompt 会注入对应 DDL 说明, LLM 据此生成 SQL,
最终由 MCP server 的 execute_sql 工具做只读硬校验 (app/db/sql_guard.py)。

注意: 发给 LLM 的只是字段说明, 不是真实建表语句, 不含任何数据。
"""

from __future__ import annotations

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
  created_at DATETIME        -- 创建时间

hr_leave_records 请假记录:
  id BIGINT PK 自增
  emp_id VARCHAR(32)         -- 请假人工号
  leave_type VARCHAR(16)     -- 类型: 年假/事假/病假/调休/婚假
  start_date DATE, end_date DATE
  days DECIMAL(5,1)          -- 请假天数
  status VARCHAR(16)         -- 审批中/已批准/已驳回
  created_at DATETIME"""

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
  created_at DATETIME        -- 提交时间

fin_department_budgets 部门年度预算:
  department VARCHAR(64)     -- 部门
  year INT                   -- 年度
  annual_budget DECIMAL(14,2) -- 年度预算总额(元)
  used_amount DECIMAL(14,2)  -- 已用金额(元)

hr_employees 员工主数据:
  emp_id VARCHAR(32) PK, name VARCHAR(64), department VARCHAR(64),
  position VARCHAR(64), hire_date DATE, status VARCHAR(16)"""
