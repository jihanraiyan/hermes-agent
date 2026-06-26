/**
 * Desktop bundles ship precompiled renderer assets. Returning false here tells
 * electron-builder to skip the node_modules collector/install step, which
 * avoids workspace dependency graph explosions and keeps packaging
 * deterministic across environments.
 *
 * In thin-client builds we also strip the install-stamp and native-deps
 * extraResources entries — no bootstrap, no local PTY.
 */
const path = require('node:path')

const THIN_CLIENT = process.env.HERMES_DESKTOP_BUILD_MODE === 'thin'

module.exports = async function beforeBuild(context) {
  if (THIN_CLIENT && context.packager) {
    // Strip install-stamp.json and native-deps from extraResources — neither
    // exists in a thin build (write-build-stamp and stage-native-deps are
    // skipped in build:thin).
    const buildConfig = context.packager.config
    if (Array.isArray(buildConfig.extraResources)) {
      buildConfig.extraResources = buildConfig.extraResources.filter(
        entry => {
          const to = typeof entry === 'object' && entry ? entry.to : null
          if (to === 'install-stamp.json' || to === 'native-deps') {
            return false
          }
          return true
        }
      )
    }
  }
  return false
}
