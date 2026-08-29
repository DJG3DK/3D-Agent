import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  plugins: [react()],
  // Served under a subpath on a shared domain (alongside another service at
  // /) rather than a new subdomain — avoids provisioning fresh DNS/SSL for a
  // pilot. Dev server stays at root so `npm run dev` still just works
  // without also running nginx locally. api.ts/useTaskStream.ts build
  // request paths off import.meta.env.BASE_URL, which Vite sets from this,
  // so both cases resolve correctly without separate code paths.
  base: command === 'build' ? '/v2/' : '/',
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8100',
        ws: true, // needed for /api/tasks/:id/stream (WebSocket) in addition to plain HTTP routes
      },
    },
  },
}))
