import { atom } from 'nanostores'

import { agentPackageName, desktopPackageName } from '@/app/capabilities/plugins/plugin-packages'
import { $pluginRecords } from '@/contrib/plugins-store'
import type { ToolOwnership } from '@/lib/tool-media-renderers'
import { $agentPlugins, type AgentPluginRow } from '@/store/agent-plugins'
import {
  activeGateway,
  activeGatewayConnectionId,
  activeGatewayProfileKey,
  gatewayActivationEpoch
} from '@/store/gateway'

/**
 * Which tools each installed package's own code registered, per active backend
 * route — the authority behind tool media renderers. Read from the backend's
 * `plugins.manage list` (`tools` / `tool_prefixes`), never from the renderer:
 * a desktop half could otherwise name any tool. Loaded lazily the first time a
 * claimed attachment needs it and refetched when the route or the agent plugin
 * list changes. A backend that predates the fields reports nothing, so every
 * claim falls back to the built-in player.
 */

export type ToolMediaOwnersState =
  | { status: 'idle' | 'loading' | 'error'; key: string }
  | { status: 'ready'; key: string; owners: ReadonlyMap<string, ToolOwnership> }

export const $toolMediaOwners = atom<ToolMediaOwnersState>({ key: '', status: 'idle' })

type OwnershipRow = AgentPluginRow & { tool_prefixes?: string[]; tools?: string[] }

function routeKey(): string {
  return `${activeGatewayConnectionId() ?? ''}|${activeGatewayProfileKey()}|${gatewayActivationEpoch()}`
}

export function ownersFromPluginRows(rows: readonly OwnershipRow[]): Map<string, ToolOwnership> {
  const owners = new Map<string, ToolOwnership>()

  for (const row of rows) {
    const tools = Array.isArray(row.tools) ? row.tools.filter(name => typeof name === 'string') : []

    const prefixes = Array.isArray(row.tool_prefixes)
      ? row.tool_prefixes.filter(prefix => typeof prefix === 'string' && prefix.length > 0)
      : []

    if (tools.length > 0 || prefixes.length > 0) {
      owners.set(agentPackageName(row), { prefixes, tools: new Set(tools) })
    }
  }

  return owners
}

/** Fetch the ownership list for the active route unless it is loaded or loading. */
export function ensureToolMediaOwners(): void {
  const key = routeKey()
  const current = $toolMediaOwners.get()

  if (current.key === key && current.status !== 'idle') {
    return
  }

  const gateway = activeGateway()

  if (!gateway) {
    return
  }

  $toolMediaOwners.set({ key, status: 'loading' })
  void gateway
    .request<{ plugins?: OwnershipRow[] }>('plugins.manage', { action: 'list' })
    .then(result => {
      if ($toolMediaOwners.get().key === key) {
        $toolMediaOwners.set({ key, owners: ownersFromPluginRows(result?.plugins ?? []), status: 'ready' })
      }
    })
    .catch(() => {
      if ($toolMediaOwners.get().key === key) {
        $toolMediaOwners.set({ key, status: 'error' })
      }
    })
}

// A toggle/install/update in Capabilities reloads `$agentPlugins`: re-read ownership on next use.
$agentPlugins.listen(() => $toolMediaOwners.set({ key: '', status: 'idle' }))

/** The agent package a contribution's plugin is the desktop half of (`plugin:<id>` →
 *  its record's `packageName`); null for standalone desktop plugins and core. */
export function packageOfContributionSource(source: string | undefined): null | string {
  if (!source?.startsWith('plugin:')) {
    return null
  }

  const record = $pluginRecords.get()[source.slice('plugin:'.length)]

  return record?.status === 'loaded' ? desktopPackageName(record) : null
}
