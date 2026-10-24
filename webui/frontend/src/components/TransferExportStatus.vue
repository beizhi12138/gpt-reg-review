<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { cleanupTransfer, lastTransferExport, transferBackupUrl } from '@/api/accountTransfer'

const emit = defineEmits(['cleaned'])
const busy = ref(false)
const title = computed(() => {
  const record = lastTransferExport.value
  if (record?.status === 'completed') return `互转导出已清理：账号 ${record.deleted_registered} 个，邮箱记录 ${record.deleted_mailboxes} 条`
  if (record?.status === 'blocked') return '互转文件已备份，自动清理被阻止'
  if (record?.status === 'unconfirmed') return '互转文件已备份，清理结果待确认'
  if (record?.status === 'download_failed') return '互转文件已备份，下载失败，账号未删除'
  return '互转文件已备份，等待完成清理'
})
async function retry() {
  busy.value = true
  try { await cleanupTransfer(); emit('cleaned'); ElMessage.success('互转清理已完成') }
  catch (error) { ElMessage.error(error.message) }
  finally { busy.value = false }
}
</script>

<template>
  <el-alert v-if="lastTransferExport" :title="title" :type="lastTransferExport.status === 'completed' ? 'success' : 'warning'" :closable="false" class="transfer-export-status">
    <p v-if="lastTransferExport.error">{{ lastTransferExport.error }}</p>
    <p>互转文件已在本机服务端保留备份；浏览器下载被取消或文件丢失时，可重新下载。</p>
    <el-space>
      <a :href="transferBackupUrl(lastTransferExport.export_id)" download="gpt-account-transfer.json">重新下载互转备份</a>
      <el-button v-if="lastTransferExport.status !== 'completed'" size="small" :loading="busy" @click="retry">重试清理／确认结果</el-button>
    </el-space>
  </el-alert>
</template>

<style scoped>
.transfer-export-status { margin: 12px 0; }
p { margin: 6px 0; }
a { color: var(--el-color-primary); }
</style>
