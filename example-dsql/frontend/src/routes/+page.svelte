<script lang="ts">
	type Message = {
		id: string;
		body: string;
		source: string;
		created_at: string;
	};

	type MessageList = {
		count: number;
		messages: Message[];
	};

	// 一覧はページングなしで全件取得する。日次スケジューラが 1 日 1 件しか
	// 追加しないため、DSQL の 1 クエリ 3,000 行制限に当たるまで約 8.2 年ある
	// (根拠は schema.sql の messages テーブルのコメント)。
	async function load(): Promise<MessageList> {
		const res = await fetch('/api/messages');
		if (!res.ok) {
			throw new Error(`${res.status} ${await res.text()}`);
		}
		return res.json();
	}

	const pending = load();
</script>

<main>
	<h1>example-dsql</h1>
	<p>
		EventBridge Scheduler → SQS → Lambda worker → Aurora DSQL の経路で、
		1 日 1 件ずつ追加されたメッセージです。削除は行いません。
	</p>

	{#await pending}
		<p>読み込み中…</p>
	{:then data}
		<p class="count">{data.count} 件</p>
		{#if data.count === 0}
			<p>まだメッセージがありません。最初のスケジュール実行を待っています。</p>
		{:else}
			<ul>
				{#each data.messages as message (message.id)}
					<li>
						<time datetime={message.created_at}>{message.created_at}</time>
						<span class="body">{message.body}</span>
						<span class="source">{message.source}</span>
					</li>
				{/each}
			</ul>
		{/if}
	{:catch error}
		<p class="error">取得に失敗しました: {error.message}</p>
	{/await}
</main>

<style>
	main {
		max-width: 48rem;
		margin: 0 auto;
		padding: 2rem 1rem;
		font-family: system-ui, sans-serif;
		line-height: 1.6;
	}

	.count {
		font-weight: 600;
	}

	ul {
		list-style: none;
		padding: 0;
	}

	li {
		display: grid;
		grid-template-columns: 1fr auto;
		gap: 0.25rem 1rem;
		padding: 0.75rem 0;
		border-top: 1px solid #ddd;
	}

	time {
		grid-column: 1 / -1;
		font-size: 0.85rem;
		color: #666;
	}

	.source {
		font-size: 0.85rem;
		color: #666;
	}

	.error {
		color: #b00;
	}
</style>
