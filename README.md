# Discord → Codex → noteの記事候補

指定したDiscordチャンネルを公式Bot APIで読み取り、ローカルSQLiteに保存します。CodexにはMCP経由で同期と会話の読み出しを提供します。記事候補の生成はCodexに依頼します。GitHub Actionsで毎朝の定期取得と暗号化保存ができます。記事候補の生成はCodexへの依頼時に行います。自動投稿は含みません。

## 接続準備

1. Discord Developer PortalでアプリとBotを作り、BotページでMessage Content Intentを有効にします。必要な場合はDiscordの承認を受けてください。
2. OAuth2のbotスコープで管理サーバーに追加します。権限は「チャンネルを見る」「メッセージ履歴を読む」。管理者権限やメッセージ送信権限は不要です。
3. Discordの開発者モードを有効にし、サーバーIDと取得対象チャンネルIDをコピーします。config.example.jsonをconfig.jsonにコピーし、対象を明示します。スレッドは個別のIDを追加してください。Botがそのスレッドを閲覧できる必要があります。
4. Botトークンを実行環境のシークレット DISCORD_BOT_TOKEN に登録します。チャットや設定ファイルには貼り付けないでください。

## インストール

Python 3.10以上で、このディレクトリに移動して実行します。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

CodexのMCP設定に次の設定を追加します。既存設定は上書きせず、このセクションを追加してください。別環境では絶対パスを変更します。Codexの実行環境にもBotトークンをシークレットとして渡す必要があります。

```toml
[mcp_servers.discord_note]
command = "/YOUR/CLONE/PATH/.venv/bin/python"
args = ["/YOUR/CLONE/PATH/server.py"]
env_vars = ["DISCORD_BOT_TOKEN"]
```

Codexを再接続してARTICLE_WORKFLOW.mdの依頼例を使います。同期は依頼時に実行されます。

## 単独で確認

```sh
.venv/bin/python discord_note.py sync --pages 5
.venv/bin/python discord_note.py read --limit 200
.venv/bin/python -m unittest discover -s tests
```

初期設定は各チャンネルの最新500件までです。pagesは1〜100で変更できます。再同期は最新ページを読み直し、同じ投稿を更新します。取得範囲内の編集は反映しますが、削除検知、添付ファイル本文取得、全履歴の継続バックフィル、スレッドの自動発見はありません。大量のチャンネルでは一度に同期するページ数を減らしてください。

保存対象は投稿ID、時刻、本文、返信先、チャンネルIDです。投稿者名と添付ファイルは保存しません。ただし本文に個人情報が含まれる可能性はあります。対象チャンネルの参加者に利用目的を説明し、記事化する内容を確認してください。会話をCodexで読むと、その本文がCodexの処理対象になります。

保存データを消す場合はdataディレクトリを削除します。対象から外したチャンネルも保存データには残るため、削除が必要なら同様に消してください。読み出しは現在の設定で許可されたチャンネルに限定します。

Botトークンと対象IDが未設定の状態では実サーバーへの接続は行えません。


## スマホから定期取得を設定する（GitHub Actions）

Codex Cloudの環境作成は、この定期取得には不要です。

GitHubのこのリポジトリで Settings → Secrets and variables → Actions → Secrets → New repository secret を開き、次の3件を登録します。トークンやパスワードをチャットに貼らないでください。

| 名前 | 内容 |
| --- | --- |
| DISCORD_BOT_TOKEN | Developer PortalのBot画面で発行するBotトークン |
| DISCORD_CHANNEL_CONFIG | 下記のJSON。サーバー・対象チャンネルを指定 |
| ARCHIVE_PASSWORD | パスワード管理アプリ等で作った、20文字以上の長くランダムな暗号化用パスワード。復号にも必要なので保管する |

対象は利用者が指定した「城下町の成果報告」「実験室」「雑談」です。

```json
{"guild_id":"1556547789180108800","channel_ids":["1557866851252445204","1563563489442664529","1557228324370124830"]}
```

続いて同じActions設定の Variables → New repository variable で、ENABLE_DISCORD_SYNC を true として登録します。

リポジトリの Actions → Collect Discord conversations → Run workflow から最初の実行をします。成功すると encrypted-discord-snapshot という暗号化ファイルをダウンロードできます。毎朝8時17分ごろ（日本時間）にも実行します。時刻はGitHubの混雑で遅れる場合があります。一定期間活動がない公開リポジトリではGitHubが定期実行を停止することがあります。

この公開リポジトリの実行履歴や暗号化ファイルは第三者に見られる可能性があります。会話本文は実行ログに出さず、保存するのは認証付き暗号化ファイルだけです。暗号化ファイルの保持期間は7日。コード変更権限を持つ人は実行コードを書き換えられるため、共同管理者は信頼できる人に限定してください。

各実行は最新500件/チャンネルを取得する独立したスナップショットです。全履歴の累積保管ではありません。分析用の出力は全体で最新1000件までです。発言数が多いと間の会話が取得されない場合があります。

## 暗号化ファイルから記事候補を作る

Pythonを実行できる手元の環境で、依存関係をインストールした後に実行します。

```sh
.venv/bin/python decrypt_archive.py discord-snapshot.enc
```

パスワードは対話入力します。既存ファイルへの上書きはしません。data/conversations.jsonをCodexで読み、ARTICLE_WORKFLOW.mdの基準で記事候補を作ります。暗号化ファイルだけではCodexは会話を読めません。スマホだけで復号できる画面は、この実装にはまだありません。直接MCP接続する場合は、Codex側でこのサーバーを起動でき、Botトークンを秘密設定として渡せる環境が別途必要です。

## 実装の確認範囲

ローカルで同期処理・対象制限・暗号化/復号をテストしています。Botの認証情報は未登録のため、実Discordへの接続と実際の記事候補の抽出は未実施です。GitHubへのコード配置だけでMCP接続が自動登録されるわけではありません。
