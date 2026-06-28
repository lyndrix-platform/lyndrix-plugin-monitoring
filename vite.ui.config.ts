import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

/**
 * Vite config for building the lyndrix-plugin-monitoring React UI bundle.
 *
 * Run: npm run build
 * Output: ui_static/ui_bundle.js (IIFE, served by lyndrix-core)
 *
 * The shell exposes React via window globals:
 *   window.__lyndrix_react        → React
 *   window.__lyndrix_react_dom_client → ReactDOMClient
 */
export default defineConfig({
  // Automatic JSX runtime (the source uses JSX without importing React). vite
  // bundles react/jsx-runtime; the define below replaces its
  // `process.env.NODE_ENV` reference (undefined in the browser, which would make
  // the IIFE throw so PluginApp is never exposed) with a literal so it resolves
  // to the production jsx and references no `process` global.
  plugins: [react()],
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    lib: {
      entry: resolve(__dirname, 'src/ui/index.tsx'),
      name: '__lyndrix_plugin_lyndrix_plugin_state_monitoring',
      formats: ['iife'],
      fileName: () => 'ui_bundle.js',
    },
    outDir: 'ui_static',
    emptyOutDir: true,
    rollupOptions: {
      // Shared from the host shell via window globals — never bundled.
      external: ['react', 'react-dom', 'react-dom/client', 'react-i18next', 'i18next', '@lyndrix/ui'],
      output: {
        globals: {
          react: '__lyndrix_react',
          'react-dom': '__lyndrix_react',
          'react-dom/client': '__lyndrix_react_dom_client',
          'react-i18next': '__lyndrix_react_i18next',
          i18next: '__lyndrix_i18n',
          '@lyndrix/ui': '__lyndrix_ui',
        },
      },
    },
  },
})
