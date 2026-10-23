import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { RunRow, Snapshot } from '../types'

const PANE = 'deck'
const RUN = 'deck-run-' // one tab per delegate: deck-run-<id>
const SETTINGS = 'deck-settings'
const MAX_MODELS = 300 // opencode alone lists hundreds; a Select needs them all to draw
let stateDir = '' // where deck keeps models.json and efforts.json, set at session.start
const SETTLE_TICKS = 3 // seconds a finished delegate sits unread before Claude is woken: batches, and lets a `wait` win
const MAX_WAKES = 10 // per session, so Claude and its delegates can't wake each other forever
const FRESH_MS = 15_000 // the MCP server rewrites its snapshot every second or so
const snapshot = atom({ plugin: 'deck', key: 'snapshot' } as const, null)

const GLYPH: Record<string, string> = { running: '●', queued: '○', done: '✓', failed: '✗', killed: '■' }
const COLOR: Record<string, string> = { running: 'cyan', done: 'green', failed: 'red', killed: 'gray', queued: 'gray' }

// 'openai/gpt-5.1-codex' -> 'gpt-5.1-codex', and not so long it crowds a tab
function short(model: string, max = 22): string {
  const m = model.includes('/') ? model.slice(model.lastIndexOf('/') + 1) : model
  return m.length > max ? m.slice(0, max - 1) + '…' : m
}

function tabTitle(r: RunRow): string {
  return r.model ? `${r.label} · ${short(r.model, 18)}` : r.label
}

// The settings pane writes the same two files the server reads before each run; empty means the agent's default.
async function choose($: EngineInterface, file: 'models' | 'efforts', agent: string, value: string) {
  const path = `${stateDir}/deck/${file}.json`
  let table: Record<string, string> = {}
  try { table = JSON.parse((await $.fs.read(path)) as string) } catch { /* none yet */ }
  if (value) table[agent] = value
  else delete table[agent]
  await $.fs.write(path, JSON.stringify(table, null, 1))
}

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
    await $.command.register({ name: 'deck', description: 'Show the deck panes: delegates, usage, and settings' })
    await $.command.register({ name: 'deck-settings', description: 'Pick each agent\'s model and effort with the keyboard' })

    const home = (await $.env.get('HOME')) ?? ''
    const state = (await $.env.get('XDG_STATE_HOME')) ?? `${home}/.local/state`
    stateDir = state
    const dir = `${state}/deck/live`
    // Several sessions can share a folder, each with a server numbering its delegates from 1: follow only ours,
    // the one started by this Claude Code process. If that can't be told, stay with the first snapshot picked.
    let ours = 0
    try {
      ours = Number((await $.process.run(['sh', '-c', 'echo $PPID'])).stdout.trim()) || 0
    } catch { /* fall back to following the first snapshot we see */ }
    let followed = 0
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
            const isMine = ours ? snap.ppid === ours : !followed || snap.pid === followed
            if (snap.cwd === e.cwd && isMine && now - snap.updated * 1000 < FRESH_MS && (!best || snap.updated > best.updated)) best = snap
          } catch { /* half-written or gone: the next tick will do */ }
        }
      } catch { /* no live folder yet */ }
      if (best && !ours) followed = best.pid
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
      let up = new Map<string, string>()
      try { up = new Map((await $.ui.panes()).map(p => [p.id, p.title])) } catch { /* no surface to ask */ }
      for (const r of best.runs) {
        if (!tabs.has(r.id) && opened) {
          tabs.add(r.id)
          void $.ui.open({ id: RUN + r.id, title: tabTitle(r) })
        } else if (up.has(RUN + r.id) && up.get(RUN + r.id) !== tabTitle(r)) {
          void $.ui.open({ id: RUN + r.id, title: tabTitle(r) }) // the model became known: retitle the open tab
        }
      }
    }
    $.clock.every(1000, () => void tick())

    return next(e)
  })

  // The Settings tab alone, holding the keyboard: Tab walks the fields, arrows and Enter pick, Esc gives it back.
  on('command.run', { command: 'deck-settings' }, async $ => {
    await $.ui.open({ id: SETTINGS, title: 'Settings', focus: true, closeOnEscape: true })
    return { text: 'Settings: Tab moves between fields, ↑/↓ and Enter choose, Esc returns to the prompt.' }
  })

  on('command.run', { command: 'deck' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Deck' })
    const snap = await read($, snapshot)
    for (const r of snap?.runs ?? []) await $.ui.open({ id: RUN + r.id, title: tabTitle(r) })
    await $.ui.open({ id: SETTINGS, title: 'Settings' })
    return { text: 'Deck panes opened: overview, one tab per delegate, and Settings for models and efforts.' }
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
            {GLYPH[r.status] ?? '?'} {r.label} {r.status} {r.seconds}s{r.model ? ` · ${r.model}` : ''}{r.effort ? ` · ${r.effort}` : ''}
            {r.tokens ? ` · ${r.tokens} tok` : ''}
            {r.cost ? ` · $${r.cost.toFixed(2)}` : ''}
          </Text>
          {r.waiting && <Text color="yellow">⚠ waiting for your approval: {r.waiting}</Text>}
          {r.lines.slice(-room).map(([kind, text]) => (
            <Text wrap="wrap" dimColor={kind !== 'text'}>
              {kind === 'tool' ? '$ ' : ''}
              {text}
            </Text>
          ))}
        </Box>
      )
    }
    if (id === SETTINGS) {
      const { Select, Input } = $.ui.resolve(e) as unknown as { Select: any; Input: any } // not on every surface
      const snap = await read($, snapshot)
      const agents = Object.entries(snap?.agents ?? {})
      if (agents.length === 0) return <Text dimColor>No agents found yet (the deck server starts with the session).</Text>
      return (
        <Box flexDirection="column">
          <Text dimColor>Tab / Shift+Tab: next field · ↑ ↓ and Enter: choose · Esc: back to the prompt. Applies from each agent's next run.</Text>
          {agents.map(([name, a], i) => (
            <Box flexDirection="column" marginTop={1}>
              <Text weight="bold">{name}</Text>
              {a.models.length > 0 ? (
                <Select
                  key={`model-${name}`}
                  autoFocus={i === 0 ? true : undefined}
                  label="model "
                  value={a.model}
                  options={[{ value: '', label: '(default)' }, ...(a.model && !a.models.some(m => m[0] === a.model) ? [{ value: a.model }] : []),
                    ...a.models.slice(0, MAX_MODELS).map(([id, d]) => ({ value: id, label: d ? `${id} — ${d}` : id }))]}
                  onSelect={(v: string) => void choose($, 'models', name, v)}
                />
              ) : (
                <Input
                  key={`model-${name}`}
                  autoFocus={i === 0 ? true : undefined}
                  label="model "
                  value={a.model}
                  placeholder="(default) type an id, Enter saves"
                  onSubmit={(v: string) => void choose($, 'models', name, v.trim())}
                />
              )}
              {a.efforts.length > 0 && (
                <Select
                  key={`effort-${name}`}
                  label="effort "
                  value={a.effort}
                  options={[{ value: '', label: '(default)' }, ...a.efforts.map(x => ({ value: x }))]}
                  onSelect={(v: string) => void choose($, 'efforts', name, v)}
                />
              )}
            </Box>
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
              {GLYPH[r.status] ?? '?'} {r.label}{r.model ? ` (${short(r.model, 28)}${r.effort ? ` · ${r.effort}` : ''})` : ''} {r.status} {r.seconds}s
              {r.tokens ? ` · ${r.tokens} tok` : ''}
              {r.cost ? ` · $${r.cost.toFixed(2)}` : ''}
            </Text>
            {r.waiting && <Text color="yellow">⚠ waiting for your approval: {r.waiting}</Text>}
            {r.lines.slice(-perRun).map(([kind, text]) => (
              <Text wrap="wrap" dimColor={kind !== 'text'}>
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
