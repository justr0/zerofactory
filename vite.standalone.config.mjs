import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react({ jsxRuntime: 'classic' })],
  build: {
    outDir: path.resolve(root, 'dashboard/standalone_assets'),
    emptyOutDir: true,
    lib: {
      entry: path.resolve(root, 'dashboard/src/standalone.jsx'),
      name: 'ZeroFactoryStandalone',
      formats: ['iife'],
      fileName: () => 'app.js',
    },
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
    minify: true,
  },
});
