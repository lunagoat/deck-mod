import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'deck',
  component: 'Pane',
  requestId: 'deck',
  props: { title: 'Deck', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const

test('the pane explains itself while there are no delegates', async ($, on) => {
  on('command.register', () => ({ value: { command: 'deck' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /No delegates yet/ })).toBeTruthy()
  await ui.unmount()
})

test('a finished delegate wakes Claude once, after it has sat unread for a few seconds', async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/h' })
  on('command.register', () => ({ value: { command: 'deck' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  const run = { id: 1, label: 'codex', agent: 'codex', status: 'done', seconds: 4, parent: null, top: true, reported: false,
    waiting: '', model: '', tokens: null, cost: null, lines: [] }
  const file = () => JSON.stringify({ pid: 1, ppid: 42, cwd: '/w', updated: clock.now() / 1000, runs: [run], usage: {} })
  on('fs.list', () => ({ value: [{ name: '1.json', kind: 'file', size: 1, mtimeMs: 0, isLink: false }] }))
  on('fs.read', () => ({ value: file() }))
  const sent: string[] = []
  on('process.run', () => ({ value: { exitCode: 0, stdout: '42\n', stderr: '' } }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('prompt.submit', (_, e) => { sent.push(e.text); return { text: e.text } })

  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.advance(2000)
  expect(sent.length).toBe(0) // still settling
  await clock.advance(5000)
  expect(sent.length).toBe(1)
  expect(sent[0]).toMatch(/codex \(done\) finished/)
  await clock.advance(10_000)
  expect(sent.length).toBe(1) // once per delegate

  run.id = 2
  run.reported = true
  await clock.advance(10_000)
  expect(sent.length).toBe(1) // already read: no wake
})

test('a second server in the same folder is ignored', async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/h' })
  on('command.register', () => ({ value: { command: 'deck' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '42\n', stderr: '' } }))
  const status: (string | undefined)[] = []
  on('ui.status', (_, e) => { status.push(e.text); return { value: undefined } })
  on('ui.toast', () => ({ value: undefined }))
  const mk = (ppid: number, label: string) => JSON.stringify({ pid: ppid, ppid, cwd: '/w', updated: clock.now() / 1000 + 1, usage: {},
    runs: [{ id: 1, label, agent: label, status: 'running', seconds: 1, parent: null, top: true, reported: false, waiting: '', model: '', tokens: null, cost: null, lines: [] }] })
  on('fs.list', () => ({ value: ['a', 'b'].map(name => ({ name: name + '.json', kind: 'file', size: 1, mtimeMs: 0, isLink: false })) }))
  on('fs.read', (_, e) => ({ value: e.path.endsWith('a.json') ? mk(42, 'codex') : mk(99, 'intruder') }))
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(status.some(s => s?.includes('intruder'))).toBe(false)
  expect(status.some(s => s?.includes('codex'))).toBe(true)
})

test('the settings pane lists each agent and saves a model and an effort', async ($, on) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/h', XDG_STATE_HOME: '/s' })
  on('command.register', () => ({ value: { command: 'deck' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '42\n', stderr: '' } }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  const snap = { pid: 1, ppid: 42, cwd: '/w', updated: 0, runs: [], usage: {}, agents: {
    codex: { model: '', effort: '', models: [['gpt-a', 'A'], ['gpt-b', 'B']], efforts: ['low', 'high'] },
    copilot: { model: '', effort: '', models: [], efforts: [] } } }
  on('fs.list', () => ({ value: [{ name: '1.json', kind: 'file', size: 1, mtimeMs: 0, isLink: false }] }))
  const written: Record<string, string> = {}
  on('fs.read', (_, e) => e.path.endsWith('1.json')
    ? { value: JSON.stringify({ ...snap, updated: clock.now() / 1000 }) }
    : { value: written[e.path] ?? '{}' })
  on('fs.write', (_, e) => { written[e.path] = e.text; return { value: undefined } })

  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.advance(2000)

  const ui = await $.ui.mount({ plugin: 'deck', component: 'Pane', requestId: 'deck-settings', surface: 'terminal',
    props: { title: 'Settings', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} } })
  expect(await ui.find({ key: 'model-codex' })).toBeTruthy()
  expect(await ui.find({ key: 'model-copilot' })).toBeTruthy() // no model list: a text field
  await ui.select({ key: 'model-codex', value: 'gpt-b' })
  await ui.select({ key: 'effort-codex', value: 'high' })
  await ui.input({ key: 'model-copilot', text: 'gpt-5' })
  await ui.unmount()
  expect(JSON.parse(written['/s/deck/models.json'])).toEqual({ codex: 'gpt-b', copilot: 'gpt-5' })
  expect(JSON.parse(written['/s/deck/efforts.json'])).toEqual({ codex: 'high' })
})
