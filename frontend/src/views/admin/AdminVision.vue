<template>
  <div>
    <h2 class="page-title">视觉识别监控</h2>
    <el-row :gutter="16">
      <el-col :span="10">
        <el-card>
          <template #header>视觉服务状态</template>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="服务地址">{{ baseUrl }}</el-descriptions-item>
            <el-descriptions-item label="连接状态">
              <el-tag :type="svc.reachable ? 'success' : 'danger'">{{ svc.reachable ? '正常' : '不可达' }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="详细信息">{{ svc.detail }}</el-descriptions-item>
          </el-descriptions>
          <div style="margin-top: 14px">
            <el-button type="primary" @click="refresh">刷新状态</el-button>
            <el-button type="warning" @click="reset">重置背景基准</el-button>
          </div>
          <el-alert type="info" :closable="false" style="margin-top: 12px"
                    title="说明：座位占用状态由视觉服务(MOG2冻结背景)实时检测，前端每 5 秒自动刷新。检测结果仅保存占用状态与置信度，不保存画面。" />
        </el-card>
      </el-col>
      <el-col :span="14">
        <el-card>
          <template #header>座位实时状态</template>
          <el-table :data="seats" stripe height="420">
            <el-table-column prop="seatNo" label="座位号" width="110" />
            <el-table-column label="实时状态" width="110">
              <template #default="{ row }">
                <el-tag :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="视觉占用">
              <template #default="{ row }">
                <el-tag :type="row.visionOccupied ? 'warning' : 'success'">{{ row.visionOccupied ? '占用' : '空闲' }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { getVisionServiceStatus, resetVision, getVisionStatus, getRooms } from '../../api'

const baseUrl = 'http://127.0.0.1:8001'
const svc = ref({ reachable: false, detail: '' })
const seats = ref([])
let timer = null

const load = async () => {
  try {
    svc.value = await getVisionServiceStatus()
  } catch (e) { /* 拦截器已提示 */ }
  try {
    const rooms = await getRooms()
    if (rooms.length) {
      seats.value = await getVisionStatus(rooms[0].id)
    }
  } catch (e) { /* ignore */ }
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})
onBeforeUnmount(() => clearInterval(timer))

const refresh = () => load()

const reset = async () => {
  await resetVision()
  ElMessage.success('已触发背景重置，请在自习室空场时操作')
}

const statusType = (s) => ({ 0: 'danger', 1: 'success', 2: 'warning', 3: 'warning' }[s])
const statusText = (s) => ({ 0: '禁用', 1: '空闲', 2: '已预约', 3: '使用中' }[s])
</script>

<style scoped>
.page-title { color: #303133; }
</style>
