import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { RunRow, Snapshot } from '../types'

const PANE = 'deck'
const FRESH_MS = 15_000 // the MCP server rewrites its snapshot every second or so
const snapshot = atom({ plugin: 'deck', key: 'snapshot' } as const, null)

const GLYPH: Record<string, string> = { running: '●', queued: '○', done: '✓', failed: '✗', killed: '■' }
const COLOR: Record<string, string> = { running: 'cyan', done: 'green', failed: 'red', killed: 'gray', queued: 'gray' }

function bar(used: number, width = 10): string {
  const filled = Math.max(0, Math.min(width, Math.round(used * width)))
  return '█'.repeat(filled) + '░'.repeat(width - filled)
}

function resets(at: number | null, now: number): string {
  if (!at) return ''
  const s = Math.max(0, at - now / 1000)
  return s >= 86400 ? ` ↻${Math.floor(s / 86400)}d` : s >= 3600 ? ` ↻${Math.floor(s / 3600)}h` : ` ↻${Math.ceil(s / 60)}m`
}

function summary(runs: RunRow[]): string | undefined {
  if (runs.length === 0) return undefined
  const asking = runs.filter(r => r.waiting).length
  const live = runs.filter(r => r.status === 'running' || r.status === 'queued')
  const tail = asking ? ` · ${asking} waiting for you` : ''
  return live.length
    ? `deck: ${live.map(r => r.label).join(', ')} running${tail}`
    : `deck: ${runs.length} delegate${runs.length > 1 ? 's' : ''} done${tail}`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'deck', description: 'Show the deck pane: delegates and agent usage' })

    const home = (await $.env.get('HOME')) ?? ''
    const state = (await $.env.get('XDG_STATE_HOME')) ?? `${home}/.local/state`
    const dir = `${state}/deck/live`
    let opened = false
    const seen = new Map<number, string>() // run id -> last status, to toast the changes

    const tick = async () => {
      let best: Snapshot | null = null
      try {
        const now = await $.clock.now()
        for (const f of await $.fs.list(dir)) {
          if (!f.name.endsWith('.json')) continue
          try {
            const snap = JSON.parse(await $.fs.read(`${dir}/${f.name}`) as string) as Snapshot
            if (snap.cwd === e.cwd && now - snap.updated * 1000 < FRESH_MS && (!best || snap.updated > best.updated)) best = snap
          } catch { /* half-written or gone: the next tick will do */ }
        }
      } catch { /* no live folder yet */ }
      await update($, snapshot, () => best)
      void $.ui.status(best ? summary(best.runs) : undefined)
      if (!best) return
      for (const r of best.runs) {
        const before = seen.get(r.id)
        seen.set(r.id, r.status)
        if (before && before !== r.status && r.status !== 'running') void $.ui.toast(`${r.label} ${r.status}`)
      }
      if (!opened && best.runs.length > 0) {
        opened = true
        void $.ui.open({ id: PANE, title: 'Deck' })
      }
    }
    $.clock.every(1000, () => void tick())

    return next(e)
  })

  on('command.run', { command: 'deck' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Deck' })
    return { text: 'Deck pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const snap = await read($, snapshot)
    if (!snap || snap.runs.length === 0) {
      return <Text dimColor>No delegates yet. Ask Claude to delegate to codex, opencode, agy or copilot.</Text>
    }
    const rows = e.viewport?.rows ?? 30
    const now = Date.now()
    const agents = Object.entries(snap.usage).filter(([, u]) => u.windows.length > 0)
    const perRun = Math.max(2, Math.floor((rows - 2 - agents.length) / snap.runs.length) - 1)

    return (
      <Box flexDirection="column">
        {agents.map(([name, u]) => (
          <Text dimColor>
            {name.padEnd(9)}
            {u.windows.map(w => `${w.label} ${bar(w.used)} ${Math.round(w.used * 100)}%${resets(w.resetsAt, now)}`).join('  ')}
          </Text>
        ))}
        {snap.runs.map(r => (
          <Box flexDirection="column" marginTop={1}>
            <Text color={COLOR[r.status]} weight="bold">
              {r.parent ? '  ↳ ' : ''}
              {GLYPH[r.status] ?? '?'} {r.label} {r.status} {r.seconds}s
              {r.tokens ? ` · ${r.tokens} tok` : ''}
              {r.cost ? ` · $${r.cost.toFixed(2)}` : ''}
            </Text>
            {r.waiting && <Text color="yellow">⚠ waiting for your approval: {r.waiting}</Text>}
            {r.lines.slice(-perRun).map(([kind, text]) => (
              <Text dimColor={kind !== 'text'}>
                {r.parent ? '    ' : '  '}
                {kind === 'tool' ? '$ ' : ''}
                {text}
              </Text>
            ))}
          </Box>
        ))}
      </Box>
    )
  })
}
