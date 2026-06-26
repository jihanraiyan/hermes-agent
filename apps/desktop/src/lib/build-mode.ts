/**
 * Build-mode flag for the renderer.
 *
 * `__VITE_THIN_CLIENT__` is injected at compile time by vite.config.ts
 * (see `define`). In dev it reads the live env var so
 * `HERMES_DESKTOP_BUILD_MODE=thin npm run dev` works for testing.
 *
 * Thin-client builds:
 *   - No first-launch bootstrap (the app has no bundled installer)
 *   - No local backend spawn (the app connects ONLY to a remote gateway)
 *   - No in-app self-update (updates come from the package manager)
 */

// In a packaged build, __VITE_THIN_CLIENT__ is replaced with a literal
// boolean by vite's `define`. In dev (no define) the typeof check falls
// through to the live env-var read so `HERMES_DESKTOP_BUILD_MODE=thin npm
// run dev` works.
const THIN_CLIENT =
  typeof __VITE_THIN_CLIENT__ !== 'undefined'
    ? __VITE_THIN_CLIENT__
    : false

export function isThinClient(): boolean {
  return THIN_CLIENT
}
