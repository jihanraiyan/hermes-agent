/// <reference types="vite/client" />

// Build-time constant injected by vite.config.ts `define`.
// True for thin-client builds (no bootstrap, no local backend, no self-update).
declare const __VITE_THIN_CLIENT__: boolean
