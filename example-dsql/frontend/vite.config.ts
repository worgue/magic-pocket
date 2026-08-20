import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// CloudFront + S3 配信の SPA として動かすため adapter-static を使う。
			// SPA fallback: 未知パスでも 200.html を返し、クライアント側ルーティングに委譲
			adapter: adapter({
				fallback: '200.html'
			})
		})
	],
	server: {
		// forge コンテナ内で `forge just front` を使うためのホスト許可
		// (.front.forge とドットで始めることで全プロジェクト名を許可)
		allowedHosts: ['.front.forge'],
		// 開発時は axum (port 8000) に /api を proxy。
		// 本番デプロイ時は同一ドメインで配信されるため proxy 設定は不要。
		proxy: {
			'/api': {
				target: 'http://127.0.0.1:8000',
				changeOrigin: true
			}
		}
	}
});
