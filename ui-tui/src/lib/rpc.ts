import { describeRpcError } from '../app/userMessages.js'
import { t } from '../i18n/runtime.js'

export type RpcResult = Record<string, any>

export const asRpcResult = <T extends RpcResult = RpcResult>(value: unknown): T | null =>
  !value || typeof value !== 'object' || Array.isArray(value) ? null : (value as T)

// Every `error: …` line the TUI prints for a failed RPC goes through here, so
// transport-level failures (backend down, stale session id, version skew,
// timeouts) read as what happened + what to do instead of the wire text.
export const rpcErrorMessage = (err: unknown) => {
  const message =
    err instanceof Error && err.message
      ? describeRpcError(err)
      : typeof err === 'string' && err.trim()
        ? describeRpcError(err)
        : t('rpc.requestFailed')

  // A gateway refusal's message is its bare reason code (`model_resolution_failed`); the human why
  // rides `data.detail` (gateway/session_controls.py::_refusal_data). Print it under the code.
  const data = (err as { data?: { detail?: unknown; reason?: unknown } } | null)?.data
  const detail = typeof data?.detail === 'string' ? data.detail.trim() : ''

  return detail && data?.reason === message ? `${message}\n${detail}` : message
}
