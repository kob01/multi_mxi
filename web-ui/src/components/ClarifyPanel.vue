<script setup>
// 智能问数的选项式澄清面板。
//
// 为什么是"点选"而不是让用户再打一句话: 澄清的目的是消除口径歧义, 开放式反问会得到
// 另一句同样含糊的话, 下一轮还得再问一次。这里每个选项都带语义层 id, 点选后前端
// 拼一条**带精确引用**的消息发回去, 链接阶段就不必再猜了。
import { computed } from 'vue'

const props = defineProps({
    payload: { type: Object, default: () => ({}) },
    disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['choose', 'none'])

const metrics = computed(() => props.payload.metrics || [])
const times = computed(() => props.payload.times || [])
const dims = computed(() => props.payload.dims || [])
const needTime = computed(() => props.payload.has_time === false)
const lead = computed(() => props.payload.lead || '请确认要统计的口径:')
const reason = computed(() => props.payload.reason || '')

function chooseMetric(m) {
    emit('choose', { kind: 'metric', id: m.id, label: m.label, caliber: m.caliber || '' })
}

function chooseTime(t) {
    emit('choose', { kind: 'time', token: t.token, label: t.label })
}

function chooseDim(d) {
    emit('choose', { kind: 'dim', id: d.id, label: d.label })
}
</script>

<template>
    <div class="clarify">
        <div class="clarify-head">
            <el-tag size="small" type="warning" effect="light" round>需要确认口径</el-tag>
            <span class="clarify-lead">{{ lead }}</span>
        </div>
        <div v-if="reason" class="clarify-reason">原因：{{ reason }}</div>

        <div v-if="metrics.length" class="clarify-group">
            <div class="group-label">指标口径</div>
            <div class="group-items">
                <el-button v-for="m in metrics" :key="m.id" size="small" :disabled="disabled" class="opt"
                    @click="chooseMetric(m)">
                    {{ m.label }}
                    <span v-if="m.score" class="score">{{ Math.round(m.score * 100) }}%</span>
                </el-button>
            </div>
            <div class="calibers">
                <div v-for="m in metrics" :key="'c-' + m.id" class="caliber">
                    <code>{{ m.id }}</code> {{ m.caliber || '(口径见语义层)' }}
                </div>
            </div>
        </div>

        <div v-if="needTime" class="clarify-group">
            <div class="group-label">统计区间</div>
            <div class="group-items">
                <el-button v-for="t in times" :key="t.token" size="small" :disabled="disabled" class="opt"
                    @click="chooseTime(t)">
                    {{ t.label }}
                </el-button>
            </div>
        </div>

        <div v-if="dims.length" class="clarify-group">
            <div class="group-label">下钻维度（可选）</div>
            <div class="group-items">
                <el-button v-for="d in dims" :key="d.id" size="small" plain :disabled="disabled" class="opt"
                    @click="chooseDim(d)">
                    {{ d.label }}
                </el-button>
            </div>
        </div>

        <div class="clarify-foot">
            <span class="tip">点选即按该口径重新问数；也可以直接在下面补充说明。</span>
            <el-button size="small" text :disabled="disabled" @click="emit('none')">
                都不是
            </el-button>
        </div>
    </div>
</template>

<style scoped>
.clarify {
    margin-top: 8px;
    padding: 10px 12px;
    background: #fbf7ee;
    border: 1px solid #f0e2c4;
    border-radius: 10px;
    max-width: 720px;
}

.clarify-head {
    display: flex;
    align-items: center;
    gap: 8px;
}

.clarify-lead {
    font-size: 13px;
    color: #5c4a1e;
}

.clarify-reason {
    font-size: 12px;
    color: #8a7a4d;
    margin-top: 4px;
}

.clarify-group {
    margin-top: 10px;
}

.group-label {
    font-size: 12px;
    color: #8a7a4d;
    margin-bottom: 4px;
}

.group-items {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
}

.opt {
    margin: 0 !important;
}

.score {
    margin-left: 6px;
    font-size: 11px;
    color: #b08a3e;
}

.calibers {
    margin-top: 6px;
}

.caliber {
    font-size: 11px;
    color: #7a6a45;
    line-height: 1.6;
}

.caliber code {
    background: #f1e6cd;
    border-radius: 4px;
    padding: 0 4px;
    margin-right: 4px;
}

.clarify-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 10px;
}

.tip {
    font-size: 11px;
    color: #98a0b3;
}
</style>
