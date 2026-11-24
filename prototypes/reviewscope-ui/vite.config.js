import {defineConfig} from 'vite';
export default defineConfig({
  base: './',
  build: {rolldownOptions: {input: {index: 'index.html', cabinet: 'cabinet.html'}}},
});
