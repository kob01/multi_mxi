"""Seed HR/Finance business tables with mock data (idempotent).

Usage:
    uv run python scripts/seed_business_data.py            # 首次导入 (表空时)
    uv run python scripts/seed_business_data.py --force    # 清空业务表后重新导入

Creates all ORM tables if missing (documents + hr_* + fin_*), then inserts
mock rows into hr_employees / hr_tickets / hr_leave_records /
fin_reimbursements / fin_department_budgets. Existing document tables are
never touched; --force only wipes the five business tables.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select

from app.db.models import (
    DepartmentBudget,
    Employee,
    HRTicket,
    LeaveRecord,
    Reimbursement,
)
from app.db.session import get_session_factory, init_schema

BUSINESS_TABLES = (Employee, HRTicket, LeaveRecord, Reimbursement, DepartmentBudget)


def _ts(dt: datetime) -> datetime:
    """业务时间列已是 timestamptz: 统一按 UTC 附上时区, 避免驱动对 naive 值的不一致解释。"""
    return dt.replace(tzinfo=timezone.utc)


EMPLOYEES = [
    # emp_id, name, department, position, hire_date, annual_total, annual_used
    ("E10001", "张伟", "研发部", "高级工程师", date(2021, 3, 15), 10, 3),
    ("E10002", "李娜", "研发部", "工程师", date(2022, 7, 1), 10, 5),
    ("E10003", "王强", "市场部", "市场经理", date(2019, 5, 20), 15, 8),
    ("E10004", "赵敏", "人事部", "HR专员", date(2020, 11, 10), 12, 4),
    ("E10005", "刘洋", "财务部", "财务专员", date(2018, 9, 1), 15, 10),
    ("E10006", "陈静", "研发部", "测试工程师", date(2023, 2, 13), 5, 1),
    ("E10007", "杨帆", "市场部", "市场专员", date(2022, 4, 18), 10, 6),
    ("E10008", "周琳", "研发部", "架构师", date(2017, 6, 12), 15, 7),
    ("E10009", "吴昊", "人事部", "招聘专员", date(2023, 8, 21), 5, 2),
    ("E10010", "郑爽", "财务部", "会计", date(2021, 1, 11), 10, 4),
]

TICKETS = [
    # ticket_no, emp_id, category, title, description, status, created_at
    ("HR1000", "E10001", "考勤", "三月考勤异常申诉", "3月12日打卡记录缺失, 实际已到岗", "DONE", datetime(2026, 3, 13, 9, 30)),
    ("HR1001", "E10002", "证明开具", "在职证明开具", "用于办理签证", "DONE", datetime(2026, 3, 20, 14, 5)),
    ("HR1002", "E10006", "入职", "新员工入职手续咨询", "办公设备与账号开通进度", "DONE", datetime(2026, 4, 1, 10, 0)),
    ("HR1003", "E10007", "薪酬", "工资条疑问", "3月补贴发放金额与审批单不一致", "DONE", datetime(2026, 4, 8, 16, 45)),
    ("HR1004", "E10003", "其他", "团建经费申请", "市场部Q2团建场地预订", "CANCELLED", datetime(2026, 4, 15, 11, 20)),
    ("HR1005", "E10009", "考勤", "外勤打卡报备", "4月22日客户现场外勤", "DONE", datetime(2026, 4, 22, 8, 50)),
    ("HR1006", "E10005", "证明开具", "收入证明开具", "用于房贷审批", "DONE", datetime(2026, 5, 6, 15, 30)),
    ("HR1007", "E10008", "其他", "工位调整申请", "临近研发二组申请换工位", "PROCESSING", datetime(2026, 5, 18, 13, 10)),
    ("HR1008", "E10004", "薪酬", "社保基数调整咨询", "年度社保基数调整时间与流程", "PROCESSING", datetime(2026, 6, 1, 9, 15)),
    ("HR1009", "E10002", "考勤", "加班调休登记", "5月30日周末加班申请调休", "OPEN", datetime(2026, 6, 2, 18, 40)),
    ("HR1010", "E10010", "离职", "离职流程咨询", "跨部门资产交接流程确认", "OPEN", datetime(2026, 6, 10, 10, 25)),
    ("HR1011", "E10001", "其他", "培训证书补办", "PMP培训证书遗失补办", "OPEN", datetime(2026, 6, 15, 14, 0)),
]

LEAVES = [
    # emp_id, leave_type, start, end, days, status, created_at
    ("E10001", "年假", date(2026, 4, 7), date(2026, 4, 8), 2, "已批准", datetime(2026, 3, 30, 10, 0)),
    ("E10002", "病假", date(2026, 4, 14), date(2026, 4, 15), 2, "已批准", datetime(2026, 4, 13, 9, 20)),
    ("E10003", "年假", date(2026, 5, 11), date(2026, 5, 15), 5, "已批准", datetime(2026, 4, 28, 14, 30)),
    ("E10004", "事假", date(2026, 5, 20), date(2026, 5, 20), 1, "已批准", datetime(2026, 5, 18, 11, 0)),
    ("E10005", "年假", date(2026, 6, 1), date(2026, 6, 3), 3, "已批准", datetime(2026, 5, 22, 16, 45)),
    ("E10006", "调休", date(2026, 6, 5), date(2026, 6, 5), 1, "已批准", datetime(2026, 6, 3, 9, 10)),
    ("E10007", "年假", date(2026, 6, 16), date(2026, 6, 17), 2, "审批中", datetime(2026, 6, 10, 15, 20)),
    ("E10008", "年假", date(2026, 7, 6), date(2026, 7, 10), 5, "审批中", datetime(2026, 6, 15, 10, 40)),
    ("E10009", "事假", date(2026, 6, 22), date(2026, 6, 23), 2, "已驳回", datetime(2026, 6, 17, 13, 55)),
    ("E10010", "年假", date(2026, 7, 20), date(2026, 7, 21), 2, "审批中", datetime(2026, 6, 18, 9, 30)),
    ("E10002", "年假", date(2026, 8, 3), date(2026, 8, 4), 2, "审批中", datetime(2026, 6, 19, 17, 0)),
    ("E10003", "调休", date(2026, 3, 27), date(2026, 3, 27), 1, "已批准", datetime(2026, 3, 25, 10, 15)),
    ("E10005", "病假", date(2026, 2, 10), date(2026, 2, 11), 2, "已批准", datetime(2026, 2, 9, 8, 45)),
    ("E10001", "调休", date(2026, 5, 9), date(2026, 5, 9), 1, "已批准", datetime(2026, 5, 7, 14, 25)),
    ("E10008", "事假", date(2026, 4, 27), date(2026, 4, 28), 2, "已批准", datetime(2026, 4, 24, 11, 35)),
]

REIMBURSEMENTS = [
    # order_no, emp_id, title, amount, category, reason, status, current_node, created_at
    ("FIN5000", "E10001", "上海出差高铁票", 553.0, "差旅费", "客户现场支持", "PAID", "已打款", datetime(2026, 3, 5, 10, 0)),
    ("FIN5001", "E10001", "上海出差酒店", 1280.0, "差旅费", "客户现场支持", "PAID", "已打款", datetime(2026, 3, 6, 9, 30)),
    ("FIN5002", "E10002", "市内打车费", 86.5, "交通费", "客户拜访通勤", "PAID", "已打款", datetime(2026, 3, 12, 18, 20)),
    ("FIN5003", "E10003", "广州出差机票", 1820.0, "差旅费", "渠道商洽谈", "PAID", "已打款", datetime(2026, 3, 18, 14, 10)),
    ("FIN5004", "E10003", "客户业务晚宴", 2680.0, "餐饮费", "重点客户招待", "APPROVED", "财务复核", datetime(2026, 4, 2, 20, 15)),
    ("FIN5005", "E10006", "测试机配件采购", 399.0, "办公用品", "测试环境搭建", "PAID", "已打款", datetime(2026, 4, 8, 11, 40)),
    ("FIN5006", "E10007", "展会物料快递", 156.0, "交通费", "展会布展物料运输", "REJECTED", "已驳回", datetime(2026, 4, 15, 16, 5)),
    ("FIN5007", "E10008", "技术书籍采购", 268.0, "办公用品", "团队能力建设", "PAID", "已打款", datetime(2026, 4, 22, 10, 55)),
    ("FIN5008", "E10005", "审计差旅住宿", 1450.0, "差旅费", "分公司年度审计", "PAID", "已打款", datetime(2026, 5, 6, 9, 20)),
    ("FIN5009", "E10004", "招聘会布展交通", 230.0, "交通费", "高校招聘会往返", "APPROVED", "财务复核", datetime(2026, 5, 12, 15, 30)),
    ("FIN5010", "E10002", "Python进阶培训", 1980.0, "培训费", "年度技能提升计划", "APPROVED", "财务复核", datetime(2026, 5, 20, 13, 45)),
    ("FIN5011", "E10001", "深圳出差高铁票", 720.0, "差旅费", "合作伙伴技术评审", "SUBMITTED", "部门主管审批", datetime(2026, 6, 1, 10, 10)),
    ("FIN5012", "E10007", "客户招待午餐", 860.0, "餐饮费", "新客户签约洽谈", "SUBMITTED", "部门主管审批", datetime(2026, 6, 3, 12, 40)),
    ("FIN5013", "E10010", "办公用品补货", 175.5, "办公用品", "部门打印机耗材", "APPROVED", "财务复核", datetime(2026, 6, 5, 9, 55)),
    ("FIN5014", "E10009", "招聘差旅住宿", 980.0, "差旅费", "外地校招行程", "SUBMITTED", "部门主管审批", datetime(2026, 6, 9, 17, 25)),
    ("FIN5015", "E10008", "架构师认证考试", 2600.0, "培训费", "岗位认证要求", "SUBMITTED", "部门主管审批", datetime(2026, 6, 12, 11, 15)),
    ("FIN5016", "E10003", "市内 taxi 交通费", 143.0, "交通费", "多客户网点走访", "SUBMITTED", "部门主管审批", datetime(2026, 6, 16, 19, 5)),
    ("FIN5017", "E10005", "税务培训课程", 1500.0, "培训费", "金税四期政策培训", "SUBMITTED", "部门主管审批", datetime(2026, 6, 18, 14, 50)),
]

BUDGETS = [
    # department, year, annual, used
    ("研发部", 2026, 200000.0, 135000.0),
    ("市场部", 2026, 350000.0, 210000.0),
    ("人事部", 2026, 120000.0, 48000.0),
    ("财务部", 2026, 90000.0, 12000.0),
]


async def seed(force: bool) -> None:
    await init_schema()
    factory = get_session_factory()
    async with factory() as session:
        total = 0
        for model in BUSINESS_TABLES:
            count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
            if count and not force:
                print(f"[skip] {model.__tablename__} 已有 {count} 行, 使用 --force 重新导入")
                return
            if count and force:
                await session.execute(delete(model))
                print(f"[clean] {model.__tablename__} 清空 {count} 行")

        for row in EMPLOYEES:
            session.add(Employee(emp_id=row[0], name=row[1], department=row[2], position=row[3],
                                 hire_date=row[4], annual_leave_total=row[5], annual_leave_used=row[6]))
        for row in TICKETS:
            session.add(HRTicket(ticket_no=row[0], emp_id=row[1], category=row[2], title=row[3],
                                 description=row[4], status=row[5], created_at=_ts(row[6]),
                                 updated_at=_ts(row[6])))
        for row in LEAVES:
            session.add(LeaveRecord(emp_id=row[0], leave_type=row[1], start_date=row[2],
                                    end_date=row[3], days=row[4], status=row[5],
                                    created_at=_ts(row[6])))
        for row in REIMBURSEMENTS:
            session.add(Reimbursement(order_no=row[0], emp_id=row[1], title=row[2], amount=row[3],
                                      category=row[4], reason=row[5], status=row[6],
                                      current_node=row[7], created_at=_ts(row[8])))
        for row in BUDGETS:
            session.add(DepartmentBudget(department=row[0], year=row[1],
                                         annual_budget=row[2], used_amount=row[3]))
        total = len(EMPLOYEES) + len(TICKETS) + len(LEAVES) + len(REIMBURSEMENTS) + len(BUDGETS)
        await session.commit()
    print(f"[done] 共导入 {total} 行 mock 数据 (员工{len(EMPLOYEES)}/工单{len(TICKETS)}/"
          f"请假{len(LEAVES)}/报销{len(REIMBURSEMENTS)}/预算{len(BUDGETS)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed HR/Finance mock data")
    parser.add_argument("--force", action="store_true", help="清空业务表后重新导入")
    asyncio.run(seed(parser.parse_args().force))
