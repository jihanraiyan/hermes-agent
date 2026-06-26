'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')

// We test build-mode.cjs by controlling process.env directly. The module
// reads process.env.HERMES_DESKTOP_BUILD_MODE at call time (not import time),
// so we can mutate the env and re-require to exercise both modes.

function freshModule() {
  // Bust the require cache so the module re-evaluates with the current env.
  delete require.cache[require.resolve('./build-mode.cjs')]
  return require('./build-mode.cjs')
}

test('isThinClient returns false by default (thick mode)', () => {
  const prev = process.env.HERMES_DESKTOP_BUILD_MODE
  delete process.env.HERMES_DESKTOP_BUILD_MODE
  const { isThinClient } = freshModule()
  assert.equal(isThinClient(), false)
  process.env.HERMES_DESKTOP_BUILD_MODE = prev
})

test('isThinClient returns true when HERMES_DESKTOP_BUILD_MODE=thin', () => {
  const prev = process.env.HERMES_DESKTOP_BUILD_MODE
  process.env.HERMES_DESKTOP_BUILD_MODE = 'thin'
  const { isThinClient } = freshModule()
  assert.equal(isThinClient(), true)
  process.env.HERMES_DESKTOP_BUILD_MODE = prev
})

test('isThinClient returns false for non-thin values', () => {
  const prev = process.env.HERMES_DESKTOP_BUILD_MODE
  process.env.HERMES_DESKTOP_BUILD_MODE = 'thick'
  const { isThinClient } = freshModule()
  assert.equal(isThinClient(), false)
  process.env.HERMES_DESKTOP_BUILD_MODE = 'thick-client'
  const { isThinClient: isThin2 } = freshModule()
  assert.equal(isThin2(), false)
  process.env.HERMES_DESKTOP_BUILD_MODE = prev
})
