// 员工 mock 数据（与 scripts/seed_business_data.py 中的 EMPLOYEES 保持一致）
// 用于聊天/文档页顶部的员工切换 select。

export const ROLE_LABELS = {
  employee: '员工',
  manager: '经理',
  hr: 'HR',
  finance: '财务',
  admin: '管理员',
}

export const EMPLOYEES = [
  { empId: 'E10001', name: '张伟', department: '研发部', position: '高级工程师', role: 'employee' },
  { empId: 'E10002', name: '李娜', department: '研发部', position: '工程师', role: 'employee' },
  { empId: 'E10003', name: '王强', department: '市场部', position: '市场经理', role: 'manager' },
  { empId: 'E10004', name: '赵敏', department: '人事部', position: 'HR专员', role: 'hr' },
  { empId: 'E10005', name: '刘洋', department: '财务部', position: '财务专员', role: 'finance' },
  { empId: 'E10006', name: '陈静', department: '研发部', position: '测试工程师', role: 'employee' },
  { empId: 'E10007', name: '杨帆', department: '市场部', position: '市场专员', role: 'employee' },
  { empId: 'E10008', name: '周琳', department: '研发部', position: '架构师', role: 'manager' },
  { empId: 'E10009', name: '吴昊', department: '人事部', position: '招聘专员', role: 'hr' },
  { empId: 'E10010', name: '郑爽', department: '财务部', position: '会计', role: 'finance' },
  { empId: 'E90000', name: '系统管理员', department: '信息技术部', position: '管理员', role: 'admin' },
]
