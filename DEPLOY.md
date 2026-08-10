# 導入手順（常駐ホスティングへの載せ方）

この Bot を常時起動の環境へ載せるための手順です。Discord 側の準備（アプリ作成・招待・チャンネル ID の取得）は [README のセットアップ](README.md#セットアップ) にありますので、まずそちらを済ませてください。ここには**ホスティングの話だけ**を書いています。

方法は4つ用意しています。**Docker が動く環境なら方法2（Docker Compose）**が最短です。

| 方法 | 向いている環境 | 常駐のさせ方 | 状態の永続化 |
|---|---|---|---|
| 1. Docker 単体 | Docker はあるが Compose を使わない環境 | `--restart unless-stopped` | named volume を `/data` へ |
| 2. Docker Compose（**推奨**） | VPS・自宅サーバー | `restart: unless-stopped` | `compose.yaml` に定義済み |
| 3. uv 直接 + systemd | Docker を入れない Linux VPS | systemd ユニット | リポジトリ直下の `state.json` |
| 4. PaaS（Railway / Koyeb など） | サーバー管理をしたくない場合 | PaaS 側が再起動 | 永続ボリュームを `/data` へ |

必要なリソースは常駐プロセス1つだけです（Web サーバーではないので**ポートの待ち受けはありません**）。イメージサイズは約 270MB、CPU・メモリともに各サービスの最小プランで足ります。

## 1. 前提

- README の「セットアップ」1〜4 を済ませていること。特に次の2つは最頻出のハマりどころです
  - **MESSAGE CONTENT INTENT が ON**（OFF だと本文が空になり何も動きません）
  - 招待 URL の Scopes が **`bot` と `applications.commands` の2つ**（後者が無いと `/topic` の登録に失敗します）
- 常時起動できる環境（VPS / 自宅サーバー / PaaS）を1つ用意すること
- `DISCORD_TOKEN` / `INTRO_CHANNEL_ID` / `CHAT_CHANNEL_ID` の3つが手元にあること

## 2. 共通準備（`.env` を作る）

どの方法でも、まずリポジトリと `.env` を用意します。

```bash
git clone https://github.com/4kito4/intro-topic-bot.git
cd intro-topic-bot
cp .env.example .env
```

`.env` を開いて最低限これらを記入します。項目の意味と既定値は `.env.example` のコメントにあります。

| 変数 | 必要度 | 内容 |
|---|---|---|
| `DISCORD_TOKEN` | 必須 | Bot トークン。未設定だと起動時にエラーで止まります |
| `INTRO_CHANNEL_ID` | 必須 | `#自己紹介` のチャンネル ID |
| `CHAT_CHANNEL_ID` | 必須 | お題を投下する `#雑談` のチャンネル ID |
| `OWNER_USER_ID` | 事実上必須 | `/topic` を実行できる運用者のユーザー ID。未設定だと誰も管理コマンドを使えません |
| `LOG_CHANNEL_ID` | 推奨 | 運用ログの投下先。起動通知・投稿通知・異常がここに流れます。リモートで動かす場合、状態を知る唯一の窓口になります |
| `GEMINI_API_KEY` | 任意 | 未設定だと定型お題モード（自己紹介を読まず、内蔵のお題だけを投稿） |

> **`.env` はコミットもイメージ同梱もされません**
> `.gitignore` と `.dockerignore` の両方で除外済みです。トークンと API キーが入るファイルなので、サーバーへは `scp` などで直接置くか、サーバー上で作成してください。

> **`STATE_PATH` は空のままで構いません**
> `.env.example` の `STATE_PATH=` は空欄ですが、Docker を使う手順（方法1・2・4）では**手順側で明示的に `/data/state.json` を渡します**。理由は後述の注意書きのとおりで、空欄のまま渡すと「空文字で設定済み」となり、イメージの既定値を打ち消してしまうためです。

## 3. 方法1: Docker 単体

```bash
docker build -t intro-topic-bot .
docker volume create intro-topic-bot-state

docker run -d --name intro-topic-bot \
  --env-file .env \
  -e STATE_PATH=/data/state.json \
  -v intro-topic-bot-state:/data \
  --restart unless-stopped \
  intro-topic-bot

docker logs -f intro-topic-bot
```

ローカル運用の `state.json` を引き継ぐ場合は、**この `docker run` を実行する前に**セクション9の手順でコピーしてください。

`--env-file .env` で `.env` の中身が環境変数としてコンテナに渡ります。**コンテナ内に `.env` ファイルは要りません**（設定の読み込みは環境変数が優先されるため、ファイルが無くても同じように動きます）。

> **`-e STATE_PATH=/data/state.json` を省略しないこと**
> `.env.example` の `STATE_PATH=` は空欄です。`--env-file` は「空欄の行」も**空文字の環境変数として**渡すため、イメージ既定の `/data/state.json` が打ち消され、状態ファイルがボリュームの外（コンテナ内の `/app/state.json`）に作られます。この状態でコンテナを作り直すと、処理済み ID・再利用プール・計測待ちがすべて消えます。`-e` は `--env-file` より優先されるので、上記のように明示的に渡してください（`.env` 側に `STATE_PATH=/data/state.json` と書いておくのでも構いません）。

> **ホストのディレクトリをマウントする場合は所有者に注意**
> コンテナは非 root（UID 10001）で動きます。named volume ではなくホストのディレクトリを使う（`-v /opt/intro-topic-bot/data:/data`）場合は、そのディレクトリを `sudo chown -R 10001:10001 /opt/intro-topic-bot/data` しておかないと state.json を書けません。named volume ならこの手当ては不要です。

## 4. 方法2: Docker Compose（推奨）

`compose.yaml` に上記と同じ内容（ビルド・`.env` の読み込み・`/data` の永続ボリューム・`restart: unless-stopped`・`STATE_PATH` の明示指定）が入っているので、コマンドはこれだけです。

```bash
docker compose up -d --build   # ビルドして起動（次回以降は --build 不要）
docker compose logs -f         # ログを追う（Ctrl+C で抜けてもコンテナは動き続けます）
docker compose ps              # 稼働確認
docker compose stop            # 停止（ボリュームは残る）
```

サーバー再起動後も Docker デーモンが上がれば自動で復帰します（`restart: unless-stopped`。ただし `docker compose stop` で明示的に止めた場合は自動起動しません）。

ローカル運用の `state.json` を引き継ぐ場合は、**`up -d` する前に**セクション9の手順（`docker compose create` でボリュームだけ先に作る）を実行してください。

## 5. 方法3: uv 直接 + systemd（Docker を使わない Linux VPS）

リポジトリを `/opt/intro-topic-bot` に置き、`bot` ユーザーで動かす前提で書いています（別の場所・別のユーザーなら読み替えてください）。

```bash
# uv の導入（bot を動かすユーザーで実行する）
curl -LsSf https://astral.sh/uv/install.sh | sh

cd /opt/intro-topic-bot
uv sync --frozen --no-dev
```

`/etc/systemd/system/intro-topic-bot.service`:

```ini
[Unit]
Description=intro-topic-bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bot
WorkingDirectory=/opt/intro-topic-bot
ExecStart=/home/bot/.local/bin/uv run --frozen --no-dev python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now intro-topic-bot
journalctl -u intro-topic-bot -f
```

注意点:

- **`ExecStart` は絶対パスで書くこと**。systemd はログインシェルの `PATH` を引き継がないため `uv` だけでは起動しません。`which uv` で実際のパスを確認してください
- **`EnvironmentFile=` は使いません**。この Bot は `config.py` が**リポジトリ直下（`config.py` と同じディレクトリ）の `.env`** を自分で読み込みます。`WorkingDirectory` やカレントディレクトリとは無関係なので、`.env` は必ず `/opt/intro-topic-bot/.env` に置いてください
  （なお `EnvironmentFile=` で同じ変数を渡すこともできますが、その場合は**環境変数側が優先**され `.env` の値は無視されます。二重管理になって事故のもとなので、`.env` に一本化するのが無難です）
- **書き込み権限**: `STATE_PATH` を設定しない場合、`state.json` はリポジトリ直下に作られます。実行ユーザーが書けるように `sudo chown -R bot:bot /opt/intro-topic-bot` しておいてください
- サーバーのタイムゾーンが UTC でも**投稿時間帯（19〜22時 JST）の判定は正しく動きます**（コード側で JST を固定オフセットとして持っているため）。影響を受けるのは `journalctl` に出るログの時刻表示だけなので、揃えたい場合は `sudo timedatectl set-timezone Asia/Tokyo` してください

## 6. 方法4: PaaS（Railway / Koyeb など）

1. リポジトリを GitHub へ置く（**`.env` はコミットしない**）
2. PaaS 側で「GitHub リポジトリからデプロイ」を選ぶ。リポジトリ直下に `Dockerfile` があるので自動検出されます（ビルダーの選択を求められたら Dockerfile を選ぶ）
3. **永続ボリュームを作成し、マウント先を `/data` にする**
4. 環境変数に `.env` の中身を移す（`DISCORD_TOKEN` / `INTRO_CHANNEL_ID` / `CHAT_CHANNEL_ID` / `OWNER_USER_ID` / `LOG_CHANNEL_ID` / `GEMINI_API_KEY` など）。あわせて **`STATE_PATH=/data/state.json` も明示的に登録**する（イメージの既定値と同じですが、画面に出しておくとボリュームの付け忘れに気づけます）
5. デプロイ後、PaaS のログとログチャンネルの起動通知を確認する

注意点:

- **永続ボリュームが無いと、再デプロイのたびに状態が消えます**。処理済み ID が消えると起動時の履歴補完で過去の自己紹介を最大50件まで取り込み直すため、**同じ人のお題がもう一度投稿されます**。反応の計測結果（形式の重み学習）も失われます
- **アイドル時にスリープするプランは使えません**。5分ごとのワーカーが止まると投稿もされません。常時稼働のプランを選んでください
- この Bot は**ポートを待ち受けません**。「HTTP のヘルスチェックに応答しないとデプロイ失敗」とみなすサービスでは、Web ではなく **Worker / Background 系のサービス種別**を選んでください（Koyeb なら Worker、Railway は既定のままで動きます）

## 7. 更新とロールバック

**更新**（コードを新しくする）: `git pull` してから、使っている方法に応じて再ビルド・再起動します。

- 方法1: `docker build -t intro-topic-bot .` → `docker rm -f intro-topic-bot` → 上記の `docker run` をもう一度実行（`-v` は同じボリューム名を指定する）
- 方法2: `docker compose up -d --build`
- 方法3: `uv sync --frozen --no-dev` → `sudo systemctl restart intro-topic-bot`
- 方法4: GitHub へ push すれば自動で再デプロイされます

ボリューム（または `state.json`）はそのまま引き継がれるので、キューや再利用プールは失われません。

**ロールバック**:

| 状況 | 対処 |
|---|---|
| 投稿だけ止めたい | `/topic pause`（自動投稿だけ止まり、反応の計測とキュー取り込みは続きます。再起動しても止まったままです） |
| プロセスごと止めたい | `docker compose stop` / `docker stop intro-topic-bot` / `sudo systemctl stop intro-topic-bot` |
| 完全に撤収したい | **Bot をサーバーからキックする**。別 Bot として並走しているだけなので、既存 Bot には何の影響もありません |
| 前のコードに戻したい | `git checkout <前のコミット>` して再ビルド・再起動。`state.json` の形式は互換なのでそのまま使えます |

## 8. 動作確認

1. **起動ログ**: `docker compose logs -f`（方法3 なら `journalctl -u intro-topic-bot -f`）に `discord.client` のログインログが出ること
2. **起動通知**: `LOG_CHANNEL_ID` を設定していれば、そのチャンネルに**灰色の embed で起動通知**（DRY_RUN・一時停止の状態・キュー件数）が流れます。意図しない再起動に気づくための導線なので、リモート運用では必ず設定してください
3. **設定の確認**: Discord で `/topic status` を実行し、いま効いている設定（投稿先・投下間隔・時間帯など）が意図どおりか確認する。`/topic queue` でキュー件数も見られます
4. **赤い embed が出ていないか**: 起動直後に「スラッシュコマンドを登録できませんでした」の赤 embed が出た場合は、`applications.commands` スコープ無しで招待されています（README の復旧手順を参照）
5. **`DRY_RUN=true` でソーク**: 数日流し、ログチャンネルの黄 embed で「この文面をこのタイミングで投稿する予定」を確認します。問題なければ `DRY_RUN=false` にして再起動
6. そのうえで **README の「本番切替チェックリスト」**を上から順に確認してください

> **`.env` を書き換えたら再起動が必要です**
> 設定は起動時に一度だけ読み込みます。Docker では `docker compose up -d --force-recreate`（方法1 は `docker rm -f` してから `docker run` をやり直し）、systemd では `sudo systemctl restart intro-topic-bot` してください。

## 9. 既存の `state.json` を引っ越す

ローカル PC で動かしていたものをそのままホスティングへ移す場合の手順です。

> **本番サーバーへ新規導入する場合は引っ越さないでください**
> テストサーバー由来の `message_id` は本番では全件見つからず、テスト中の計測結果が形式の重み学習を汚染します（README の本番切替チェックリスト4）。空の状態から始めるのが正解です。

> **Bot を一度も起動しないままコピーすること**
> 空の `state.json` で起動すると、処理済み ID が無い状態で過去の自己紹介が最大50件キューに入り、条件（投稿時間帯・静穏）が揃っていればその場でお題を投稿してしまいます。**その投稿記録は直後のコピーで上書きされて消える**ため、同じ自己紹介が後日もう一度使われます。以下はいずれも「起動する前にコピーする」手順です。

**方法2（Docker Compose）の場合**: `docker compose create` はコンテナとボリュームを作るだけで**起動しません**。これを使ってボリュームを先に用意します。

```bash
docker compose create     # 起動せずにコンテナとボリュームだけ作る
docker volume ls          # ボリューム名を確認（例: intro-topic-bot_state）

docker run --rm -v intro-topic-bot_state:/data -v "$(pwd)":/src:ro alpine \
  sh -c 'cp /src/state.json /data/state.json && chown 10001:10001 /data/state.json'

docker compose up -d      # ここで初めて起動する
```

Compose の named volume 名には**ディレクトリ名が接頭辞として付く**（`intro-topic-bot_state`）ため、`docker volume ls` で実際の名前を確認してから実行してください。`chown 10001:10001` はコンテナの実行ユーザーに合わせるためのものです。

**方法1（Docker 単体）の場合**: セクション3の手順のうち **`docker run` を実行する前に**コピーします（`docker volume create` はボリュームを作るだけなので Bot は動きません）。

```bash
docker build -t intro-topic-bot .
docker volume create intro-topic-bot-state

docker run --rm -v intro-topic-bot-state:/data -v "$(pwd)":/src:ro alpine \
  sh -c 'cp /src/state.json /data/state.json && chown 10001:10001 /data/state.json'

# このあとにセクション3の「docker run -d --name intro-topic-bot ...」を実行する
```

**方法3（systemd）の場合**: `state.json` をリポジトリ直下に置き、実行ユーザーの所有にするだけです。**セクション5の `systemctl enable --now` を実行する前に**置いておくのが最も安全です（起動済みなら一度止めてからコピーしてください）。

```bash
sudo systemctl stop intro-topic-bot   # すでに起動している場合のみ
sudo cp state.json /opt/intro-topic-bot/state.json
sudo chown bot:bot /opt/intro-topic-bot/state.json
sudo systemctl start intro-topic-bot
```

**方法4（PaaS）の場合**: ボリュームの中身を直接置き換える手段はサービスによって異なります。素直に空から始め、必要なら `/topic backfill` で過去の自己紹介を取り込み直すほうが簡単です。

> **`state.json` には自己紹介の本文が含まれます**
> バックアップやコピーの置き場所は、トークンと同じ扱い（第三者が読めない場所）にしてください。
