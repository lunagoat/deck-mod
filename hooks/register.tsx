import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { RunRow, Snapshot } from '../types'

const PANE = 'deck'
const RUN = 'deck-run-' // one tab per delegate: deck-run-<id>
const SETTLE_TICKS = 3 // seconds a finished delegate sits unread before Claude is woken: batches, and lets a `wait` win
const MAX_WAKES = 10 // per session, so Claude and its delegates can't wake each other forever
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
    ? `${live.map(r => r.label).join(', ')} running${tail}`
    : `${runs.length} delegate${runs.length > 1 ? 's' : ''} done${tail}`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'deck', description: 'Show the deck pane: delegates and agent usage' })

    const home = (await $.env.get('HOME')) ?? ''
    const state = (await $.env.get('XDG_STATE_HOME')) ?? `${home}/.local/state`
    const dir = `${state}/deck/live`
    let opened = false
    let hinted = false
    const tabs = new Set<number>() // delegates that already got a tab; a tab the person closed stays closed
    const waiting = new Map<number, number>() // finished, unread delegate id -> ticks seen so
    const woken = new Set<number>()
    let wakes = 0
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
      const unread = best.runs.filter(r => r.top && !r.reported && !woken.has(r.id) && !['running', 'queued'].includes(r.status))
      for (const id of [...waiting.keys()]) if (!unread.some(r => r.id === id)) waiting.delete(id)
      for (const r of unread) waiting.set(r.id, (waiting.get(r.id) ?? 0) + 1)
      if (unread.length > 0 && wakes < MAX_WAKES && unread.every(r => (waiting.get(r.id) ?? 0) >= SETTLE_TICKS)) {
        wakes++
        for (const r of unread) woken.add(r.id)
        const still = best.runs.filter(r => r.top && ['running', 'queued'].includes(r.status)).map(r => r.label)
        const text =
          `[deck] ${unread.map(r => `${r.label} (${r.status})`).join(', ')} finished. ` +
          'Call the deck `poll` tool to read the result' + (unread.length > 1 ? 's' : '') + ' and carry on with the task. ' +
          (still.length ? `Still running: ${still.join(', ')}. ` : '') +
          'If you have already read these, ignore this message.'
        void $.prompt.submit({ text })
      }
      if (!opened && best.runs.length > 0) {
        opened = true
        const shown = await $.ui.open({ id: PANE, title: 'Deck' })
        if (!shown.isPlaced && !hinted) {
          hinted = true // unasked panes need a wide terminal; the command is always placed
          void $.ui.toast('deck: run /deck to see the delegates (the terminal is too narrow to open it unasked)')
        }
      }
      for (const r of best.runs) {
        if (!tabs.has(r.id) && opened) {
          tabs.add(r.id)
          void $.ui.open({ id: RUN + r.id, title: r.label })
        }
      }
    }
    $.clock.every(1000, () => void tick())

    return next(e)
  })

  on('command.run', { command: 'deck' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Deck' })
    const snap = await read($, snapshot)
    for (const r of snap?.runs ?? []) await $.ui.open({ id: RUN + r.id, title: r.label })
    return { text: 'Deck pane opened.' }
  })

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    const { Box, Text } = $.ui.resolve(e)
    const id = e.requestId ?? ''
    if (id.startsWith(RUN)) {
      const snap = await read($, snapshot)
      const r = snap?.runs.find(x => x.id === Number(id.slice(RUN.length)))
      if (!r) return <Text dimColor>This delegate is gone.</Text>
      const room = Math.max(1, (e.viewport?.rows ?? 30) - 4)
      return (
        <Box flexDirection="column">
          <Text color={COLOR[r.status]} weight="bold">
            {GLYPH[r.status] ?? '?'} {r.label} {r.status} {r.seconds}s{r.model ? ` · ${r.model}` : ''}
            {r.tokens ? ` · ${r.tokens} tok` : ''}
            {r.cost ? ` · $${r.cost.toFixed(2)}` : ''}
          </Text>
          {r.waiting && <Text color="yellow">⚠ waiting for your approval: {r.waiting}</Text>}
          {r.lines.slice(-room).map(([kind, text]) => (
            <Text dimColor={kind !== 'text'}>
              {kind === 'tool' ? '$ ' : ''}
              {text}
            </Text>
          ))}
        </Box>
      )
    }
    if (id !== PANE) return next(e)
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
