import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development the browser talks only to Vite (http://localhost:5173), and
// Vite proxies /api (including the SSE stream) to the FastAPI backend. In
// production FastAPI serves the built files itself, so everything is
// same-origin. Secrets never reach the browser: there are no VITE_* secrets.
const backend = process.env.RESEARCH_HIVE_BACKEND ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    host: 'localhost',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: backend, changeOrigin: true },
    },
  },
  build: {
    sourcemap: false,
  },
})
