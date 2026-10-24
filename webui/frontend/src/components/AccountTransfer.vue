<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import http from '@/api/request'
import { exportRegistered } from '@/api/register'
import { downloadAndCleanupTransfer } from '@/api/accountTransfer'
import TransferExportStatus from '@/components/TransferExportStatus.vue'

const props = defineProps({ emails: { type: Array, default: () => [] } })
const emit = defineEmits(['imported', 'cleaned'])
const visible = ref(false)
const text = ref('')
const filename = ref('')
const conflict = ref('skip')
const busy = ref(false)
const report = ref(null)
const canImport = computed(() => report.value?.dry_run && report.value.inserted + report.value.updated > 0)

async function chooseFile(event) {
  text.value = ''
  filename.value = ''
  report.value = null
  const file = event.target.files?.[0]
  if (!file) return
  if (file.size > 512 * 1024 * 1024) { ElMessage.error('文件不能超过 512 MB'); event.target.value = ''; return }
  busy.value = true
  try {
    text.value = await file.text()
    filename.value = file.name
  } catch (_) { ElMessage.error('读取文件失败，请重新选择') }
  finally { busy.value = false }
}

async function runImport(dryRun) {
  if (!text.value || (!dryRun && !canImport.value)) return
  busy.value = true
  try {
    report.value = await http.post('/api/registered/transfer/import', {
      text: text.value, conflict: conflict.value, dry_run: dryRun,
    }, { timeout: 600000 })
    if (!dryRun) { ElMessage.success('迁移导入已完成，请查看结果'); emit('imported') }
  } catch (error) { report.value = null; ElMessage.error(error.message) }
  finally { busy.value = false }
}

async function download(all) {
  if (busy.value) return
  busy.value = true
  try {
    const result = await exportRegistered({ format: 'account_transfer', all, emails: all ? undefined : props.emails })
    const cleaned = await downloadAndCleanupTransfer(result)
    emit('cleaned')
    ElMessage.success(`已发起互转文件下载，并删除本系统 ${cleaned.deleted_registered} 个账号和 ${cleaned.deleted_mailboxes} 条邮箱记录`)
  } catch (error) { ElMessage.error(error.message) }
  finally { busy.value = false }
}
</script>

<template>
  <el-button type="primary" plain @click="visible = true">账号互转</el-button>
  <el-dialog v-model="visible" title="账号互转 · turb 老系统" width="680px" :close-on-click-modal="!busy" :show-close="!busy" :close-on-press-escape="!busy" destroy-on-close>
    <p>迁移密码、2FA、登录令牌及邮箱取码信息。导出的 JSON 可在另一系统的「账号互转」中导入。</p>
    <el-space wrap>
      <el-button :disabled="busy || !emails.length" @click="download(false)">导出并删除选中（{{ emails.length }}）</el-button>
      <el-button :disabled="busy" @click="download(true)">导出并删除全部已保存账号</el-button>
    </el-space>
    <p class="transfer-hint">全部导出包含凭证未补齐的账号，不受列表筛选影响。单次最多 100,000 个账号 / 512 MB。</p>
    <el-alert type="warning" :closable="false" title="互转导出会自动删除本系统这一批账号及对应邮箱记录。系统先保留完整备份，再发起下载并清理。" />
    <TransferExportStatus @cleaned="emit('cleaned')" />
    <el-divider />
    <el-form label-position="top">
      <el-form-item label="选择另一系统导出的迁移 JSON">
        <input type="file" accept=".json,application/json" aria-label="迁移 JSON 文件" :disabled="busy" @change="chooseFile" />
        <span v-if="filename" class="transfer-hint">{{ filename }}</span>
      </el-form-item>
      <el-form-item label="遇到已有账号">
        <el-radio-group v-model="conflict" :disabled="busy" @change="report = null">
          <el-radio value="skip">跳过已有账号</el-radio>
          <el-radio value="fill_missing">仅补全缺失字段</el-radio>
        </el-radio-group>
      </el-form-item>
    </el-form>
    <div v-if="report" aria-live="polite">
      <el-alert :closable="false" :type="report.failed ? 'warning' : 'success'" :title="`${report.dry_run ? '导入预览' : '导入完成'}：共 ${report.total} 个，新增 ${report.inserted}，补全 ${report.updated}，跳过 ${report.skipped}，失败 ${report.failed}`" />
      <p v-if="report.incomplete" class="transfer-hint">{{ report.incomplete }} 个账号缺少密码、2FA 或 AT，凭证会保存；补齐后才会出现在注册结果列表中，也可随时通过「导出全部已保存账号」取回。</p>
      <p v-if="report.mail_unavailable" class="transfer-hint">{{ report.mail_unavailable }} 个账号的邮箱类型在本系统不支持，原始邮箱信息会保留，需在使用取码功能前配置。</p>
      <el-table v-if="report.details.length" :data="report.details" max-height="220" size="small">
        <el-table-column prop="index" label="序号" width="65" />
        <el-table-column prop="email" label="邮箱" min-width="170" />
        <el-table-column prop="reason" label="说明" min-width="180" />
      </el-table>
      <p v-if="report.skipped + report.failed > 100" class="transfer-hint">明细最多显示前 100 条，统计包含全部账号。</p>
    </div>
    <template #footer>
      <el-button :disabled="busy" @click="visible = false">关闭</el-button>
      <el-button :disabled="busy || !text" @click="runImport(true)">预览导入</el-button>
      <el-button type="primary" :disabled="busy || !canImport" :loading="busy" @click="runImport(false)">确认导入</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.transfer-hint { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.7; }
p { line-height: 1.7; }
</style>
