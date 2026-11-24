import { env } from 'node:process'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const tunnelHost = env.INTERVIEWAI_VITE_ALLOWED_HOST

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: tunnelHost ? [tunnelHost] : [],
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
