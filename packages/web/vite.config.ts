import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  root: 'renderer-v2',
  base: '/',
  resolve: {
    alias: {
      '@v2': path.resolve(__dirname, './renderer-v2/src'),
    },
  },
  server: {
    port: 15169,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: path.resolve(__dirname, '../../web_dist'),
    emptyOutDir: true,
  },
});
