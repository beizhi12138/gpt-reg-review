import { ref } from 'vue'
import http, { API_BASE } from './request'

const STORAGE_KEY = 'reg.lastTransferExport'
function readReceipt() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
    return /^[0-9a-f]{32}$/.test(saved?.export_id || '') ? saved : null
  } catch (_) { return null }
}

export const lastTransferExport = ref(readReceipt())

function remember(receipt) {
  lastTransferExport.value = receipt
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(receipt)) } catch (_) { /* Backup stays on the server. */ }
}

export function transferBackupUrl(exportId) {
  return API_BASE + '/api/registered/transfer/backups/' + encodeURIComponent(exportId)
}

export async function cleanupTransfer(receipt = lastTransferExport.value) {
  if (!receipt) throw new Error('没有可清理的互转记录')
  try {
    const result = await http.post('/api/registered/transfer/cleanup', { export_id: receipt.export_id }, { timeout: 600000 })
    remember({ ...receipt, ...result, status: 'completed', error: '' })
    return result
  } catch (error) {
    // A lost response may follow a committed transaction. Retrying the same receipt is safe.
    remember({ ...receipt, status: error.status === 409 ? 'blocked' : 'unconfirmed', error: error.message })
    throw error
  }
}

export async function downloadAndCleanupTransfer(result) {
  if (!/^[0-9a-f]{32}$/.test(result.export_id || '') || !result.backup_path) {
    throw new Error('后端尚未支持互转自动清理，请重启本系统后刷新页面；账号未删除')
  }
  if (!result.count || !result.size) throw new Error('迁移文件不完整，账号未删除，请重新导出')
  const receipt = { export_id: result.export_id, count: result.count, backup_path: result.backup_path, status: 'prepared' }
  remember(receipt)
  let blob
  try {
    if (result.download_url) {
      blob = await http.get('/api/registered/transfer/backups/' + encodeURIComponent(result.export_id), {
        responseType: 'blob', timeout: 600000,
      })
    } else {
      const bytes = Uint8Array.from(atob(result.b64 || ''), ch => ch.charCodeAt(0))
      blob = new Blob([bytes], { type: result.mime || 'application/json' })
    }
    if (blob.size !== result.size) throw new Error('迁移文件不完整，账号未删除，请重新下载备份')
  } catch (error) {
    remember({ ...receipt, status: 'download_failed', error: error.message })
    throw error
  }
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  try {
    link.href = url
    link.download = result.filename || 'gpt-account-transfer.json'
    document.body.appendChild(link)
    link.click()
  } finally {
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 60000)
  }
  // Anchor downloads cannot acknowledge disk completion. The server has already
  // fsynced and verified a recoverable copy before issuing this receipt.
  return cleanupTransfer(receipt)
}
