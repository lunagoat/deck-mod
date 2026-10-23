export type Line = [kind: string, text: string]
export type RunRow = {
  id: number
  label: string
  agent: string
  status: string
  seconds: number
  parent: string | null
  waiting: string
  model: string
  tokens: number | null
  cost: number | null
  lines: Line[]
}
export type UsageWindow = { label: string; used: number; resetsAt: number | null }
export type Snapshot = {
  pid: number
  cwd: string
  updated: number
  runs: RunRow[]
  usage: Record<string, { note: string; windows: UsageWindow[] }>
}

declare module 'claude-code' {
  interface PluginState {
    deck: { snapshot: Snapshot | null }
  }
}
