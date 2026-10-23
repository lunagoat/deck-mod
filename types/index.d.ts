export type Line = [kind: string, text: string]
export type RunRow = {
  id: number
  label: string
  agent: string
  status: string
  seconds: number
  parent: string | null
  top: boolean // started from Claude Code itself, so Claude is who waits for it
  reported: boolean // Claude has already been handed its result
  waiting: string
  model: string
  effort: string
  tokens: number | null
  cost: number | null
  lines: Line[]
}
export type UsageWindow = { label: string; used: number; resetsAt: number | null }
export type AgentInfo = { model: string; effort: string; models: [id: string, description: string][]; efforts: string[] }
export type Snapshot = {
  pid: number
  ppid: number // the Claude Code process that started this server
  cwd: string
  updated: number
  runs: RunRow[]
  usage: Record<string, { note: string; windows: UsageWindow[] }>
  agents: Record<string, AgentInfo>
}

declare module 'claude-code' {
  interface PluginState {
    deck: { snapshot: Snapshot | null }
  }
}
