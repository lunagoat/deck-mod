import { expect, test } from 'claude-code/testing'

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
