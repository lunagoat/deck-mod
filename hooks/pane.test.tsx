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
  const file = () => JSON.stringify({ pid: 1, cwd: '/w', updated: clock.now() / 1000, runs: [run], usage: {} })
  on('fs.list', () => ({ value: [{ name: '1.json', kind: 'file', size: 1, mtimeMs: 0, isLink: false }] }))
  on('fs.read', () => ({ value: file() }))
  const sent: string[] = []
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
