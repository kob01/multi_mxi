// 当前登录员工的共享状态（mock 切换，聊天页与文档页共用）
// 选中的员工持久化到 localStorage，刷新后保持不变；未选过则默认郑爽(E10010)。
import { reactive, readonly } from 'vue'
import { EMPLOYEES } from '../mock/employees'

const STORAGE_KEY = 'mxi_current_employee'
const DEFAULT_EMP_ID = 'E10010'

function loadInitial() {
  let saved = null
  try {
    saved = localStorage.getItem(STORAGE_KEY)
  } catch {
    /* localStorage 不可用时降级为默认值 */
  }
  return EMPLOYEES.find((e) => e.empId === saved) || EMPLOYEES.find((e) => e.empId === DEFAULT_EMP_ID)
}

const state = reactive({
  current: { ...loadInitial() },
})

// readonly 代理引用保持稳定：切换员工时原地 mutate，触发响应式更新
const current = readonly(state).current

export function useEmployee() {
  function switchEmployee(empId) {
    const emp = EMPLOYEES.find((e) => e.empId === empId)
    if (!emp) return
    Object.assign(state.current, emp)
    try {
      localStorage.setItem(STORAGE_KEY, emp.empId)
    } catch {
      /* 忽略存储失败 */
    }
  }
  return { current, switchEmployee }
}
