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
  plugins: [react()],
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
      external: ['react', 'react-dom', 'react-dom/client'],
      output: {
        globals: {
          react: '__lyndrix_react',
          'react-dom': '__lyndrix_react',
          'react-dom/client': '__lyndrix_react_dom_client',
        },
      },
    },
  },
})
