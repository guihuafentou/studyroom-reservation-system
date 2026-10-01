<template>
  <div>
    <el-page-header @back="goBack" :content="room?.name || '座位预约'" style="margin-bottom: 16px" />
    <el-card>
      <!-- 选择日期与时段 -->
      <div class="selector-bar">
        <el-date-picker v-model="date" type="date" placeholder="选择日期" :disabled-date="disablePast"
                        value-format="YYYY-MM-DD" style="width: 160px" @change="load" />
        <el-select v-model="slotIds" placeholder="选择时段（可多选连续时段）" multiple collapse-tags
                   style="width: 300px" @change="load">
          <el-option v-for="s in slots" :key="s.id" :label="`${s.startTime} ~ ${s.endTime}`" :value="s.id" />
        </el-select>
        <el-button type="primary" :disabled="!date || !slotIds.length" @click="load">查 询</el-button>
        <el-button @click="loadVision">刷新实时状态</el-button>
        <span class="hint">提示：可多选连续时段批量预约；绿色可预约，点击座位即可预约</span>
      </div>

      <!-- 图例 -->
      <div class="legend">
        <span><i class="dot free"></i>空闲</span>
        <span><i class="dot me"></i>我已预约</span>
        <span><i class="dot other"></i>他人已约</span>
        <span><i class="dot inuse"></i>使用中</span>
        <span><i class="dot disabled"></i>禁用</span>
      </div>

      <!-- 座位网格 -->
      <div class="seat-grid" v-if="seats.length">
        <div v-for="row in rows" :key="row" class="seat-row">
          <div v-for="seat in seatsInRow(row)" :key="seat.id" class="seat-cell" @click="select(seat)">
            <div class="seat" :class="seatClass(seat)">
              <span class="seat-no">{{ seat.seatNo }}</span>
              <el-tag v-if="seat.seatType === 1" size="small" class="type-tag">窗</el-tag>
              <el-tag v-else-if="seat.seatType === 2" size="small" type="warning" class="type-tag">电</el-tag>
              <el-tag v-else-if="seat.seatType === 3" size="small" type="info" class="type-tag">隔</el-tag>
            </div>
          </div>
        </div>
      </div>
      <el-empty v-else description="请选择日期和时段后查询" />
    </el-card>

    <!-- 预约对话框 -->
    <el-dialog v-model="dialogVisible" title="确认预约" width="420px">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="自习室">{{ room?.name }}</el-descriptions-item>
        <el-descriptions-item label="座位">{{ current?.seatNo }}</el-descriptions-item>
        <el-descriptions-item label="日期">{{ date }}</el-descriptions-item>
        <el-descriptions-item label="时段">{{ slotLabel }}</el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">确认预约</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { getRoom, getSlots, getSeats, createReservation } from '../api'

const route = useRoute()
const router = useRouter()
const roomId = Number(route.params.id)

const room = ref(null)
const slots = ref([])
const seats = ref([])
const date = ref('')
const slotIds = ref([])
const dialogVisible = ref(false)
const submitting = ref(false)
const current = ref(null)

onMounted(async () => {
  room.value = await getRoom(roomId)
  slots.value = await getSlots(roomId)
  // 默认今天（本地时区日期），若今天已无可用时段则选明天
  const today = new Date()
  date.value = formatDate(today)
  const usable = slots.value.filter((s) => s.startTime > nowTime())
  slotIds.value = usable.length ? [usable[0].id] : (slots.value[0] ? [slots.value[0].id] : [])
  await load()
})

const formatDate = (d) => {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

const nowTime = () => {
  const d = new Date()
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

const disablePast = (d) => d.getTime() < new Date(new Date().toDateString()).getTime()

const rows = computed(() => [...new Set(seats.value.map((s) => s.rowNo))])

const seatsInRow = (row) => seats.value.filter((s) => s.rowNo === row)

const slotLabel = computed(() => {
  const selected = slots.value.filter((s) => slotIds.value.includes(s.id))
  return selected.length ? selected.map((s) => `${s.startTime}~${s.endTime}`).join('、') : ''
})

const load = async () => {
  if (!date.value || !slotIds.value.length) return
  // 座位状态按所选第一个时段展示（多时段预约以最终提交校验为准）
  seats.value = await getSeats(roomId, { date: date.value, slotId: slotIds.value[0] })
}

const loadVision = async () => {
  await load()
  ElMessage.success('已刷新')
}

const seatClass = (seat) => {
  if (seat.status === 0) return 'disabled'
  if (seat.reservedByMe) return 'me'
  if (seat.reservedByOther || seat.status === 2) return 'other'
  if (seat.status === 3) return 'inuse'
  return 'free'
}

const select = (seat) => {
  if (seat.status === 0) {
    ElMessage.warning('该座位已禁用')
    return
  }
  if (seat.reservedByOther || seat.status === 2 || seat.status === 3) {
    ElMessage.warning('该座位该时段不可预约')
    return
  }
  current.value = seat
  dialogVisible.value = true
}

const submit = async () => {
  submitting.value = true
  try {
    await createReservation({ seatId: current.value.id, date: date.value, slotIds: slotIds.value })
    ElMessage.success('预约成功')
    dialogVisible.value = false
    await load()
  } finally {
    submitting.value = false
  }
}

const goBack = () => router.push('/rooms')
</script>

<style scoped>
.selector-bar { display: flex; gap: 12px; align-items: center; margin-bottom: 14px; }
.hint { color: #909399; font-size: 13px; }
.legend { display: flex; gap: 18px; margin-bottom: 14px; font-size: 13px; color: #606266; }
.dot { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 4px; vertical-align: -1px; }
.dot.free { background: #67c23a; }
.dot.me { background: #409eff; }
.dot.other { background: #f56c6c; }
.dot.inuse { background: #e6a23c; }
.dot.disabled { background: #c0c4cc; }
.seat-grid { display: flex; flex-direction: column; gap: 12px; align-items: center; }
.seat-row { display: flex; gap: 12px; }
.seat-cell { cursor: pointer; }
.seat { width: 68px; height: 56px; border-radius: 8px; display: flex; flex-direction: column; align-items: center; justify-content: center; position: relative; border: 2px solid transparent; transition: all 0.15s; }
.seat.free { background: #e1f3d8; border-color: #67c23a; }
.seat.me { background: #d9ecff; border-color: #409eff; }
.seat.other { background: #fde2e2; border-color: #f56c6c; }
.seat.inuse { background: #fdf6ec; border-color: #e6a23c; }
.seat.disabled { background: #f4f4f5; border-color: #c0c4cc; cursor: not-allowed; opacity: 0.6; }
.seat-no { font-size: 13px; font-weight: 600; color: #303133; }
.type-tag { position: absolute; top: -6px; right: -6px; transform: scale(0.8); }
</style>
