'use strict'

/**
 * build-mode.cjs — pure helper for the desktop's thin-vs-thick build mode.
 *
 * The desktop ships in two shapes:
 *   - thick (default): bundles the first-launch bootstrap installer, can
 *     spawn a local Hermes backend, and supports in-app self-update.
 *   - thin: no bootstrap, no local backend, no self-update. Connects ONLY
 *     to a remote gateway. Used for sandboxed/package-managed deployments
 *     (Flatpak, Snap, etc.) where the agent lives elsewhere.
 * 
 * The esbuild bundler bakes this env var into the source code, so it's read at build time, not runtime.
 */

function isThinClient() {
  return process.env.HERMES_DESKTOP_BUILD_MODE === 'thin'
}

module.exports = { isThinClient }
