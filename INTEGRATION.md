# 統合手順（既存 bot へ組み込む）

すでに運用している Discord bot に、この機能を**追加のホスティング無し**で載せるための手順です。この文書だけで統合が完了します。別 bot として並走させたい場合はこの文書ではなく [DEPLOY.md](DEPLOY.md) を読んでください。

## 1. これは何か

表向きは「2日に1回お題を投下する定期 bot」として振る舞いながら、裏では `#自己紹介` の投稿から専攻・興味を読み取り、その人が話しやすそうな問いを `#雑談` の静かなタイミングに投下します。自己紹介を読んでいることは投稿から一切わかりません（本人への言及・メンションはしません）。

```
💭 今日のお題：記憶と自分
**記憶をすべて失っても、その人は同じ人だと思いますか？**
-# 一言でも、リアクションだけでも歓迎
```

投下タイミングの判定・お題の生成・反応の計測といった動作原理は [README.md](README.md#仕組み) に、導入判断のための資料（プライバシー配慮・運用コスト・既知の制限）は [proposal.md](proposal.md) にあります。

## 2. 統合の全体像

やることは4つだけです。既存 bot のコードを書き換えるのは**3番目の1行だけ**です。

1. `intro_topic/` フォルダを既存 bot のリポジトリへコピーする
2. 依存パッケージを4つ追加する（`discord.py` はすでに入っているはず）
3. `await bot.load_extension("intro_topic")` を1行足す
4. 環境変数を設定して既存 bot を再起動する

`intro_topic/` はパッケージの外を一切 import しない自己完結した構成なので、**このフォルダをコピーするだけ**で動きます。撤収したくなったら 3 の1行を消せば完全に元へ戻ります（[9](#9-更新の受け取り方とロールバック)）。

## 3. 前提条件

| 項目 | 必要なもの | 補足 |
|---|---|---|
| Python | **3.10 以上** | 開発・検証は 3.12 です。3.10 が下限なのは依存パッケージ（google-genai / python-dotenv）の要求と、pydantic が `X \| Y` 記法を実行時に解決するためです |
| discord.py | **2.4 以上** | 二択お題を Discord の投票として出すため `discord.Poll` を使います（2.4 で追加）。検証は 2.7.1 |
| 追加の依存 | `google-genai` / `pydantic` / `python-dotenv` | pydantic は google-genai の依存でもありますが、こちらからも直接 import するので明示します |

`requirements.txt` を使っている場合:

```
discord.py>=2.4
google-genai>=2.12.1
pydantic>=2.13.4
python-dotenv>=1.2.2
```

`pyproject.toml`（uv / Poetry / PDM など）の場合:

```toml
dependencies = [
    "discord.py>=2.4",
    "google-genai>=2.12.1",
    "pydantic>=2.13.4",
    "python-dotenv>=1.2.2",
]
```

> **`python-dotenv` を入れたくない場合**
> `intro_topic/config.py` が `.env` の読み込みに使っています。既存 bot が環境変数を自前で管理していて `.env` を使わない場合でも、import だけは走るので依存としては必要です（環境変数がすでに設定されていれば `.env` が無くてもそのまま動きます）。

## 4. 手順

### 4-1. `intro_topic/` をコピーする

```bash
git clone https://github.com/4kito4/intro-topic-bot.git /tmp/intro-topic-bot
cp -r /tmp/intro-topic-bot/intro_topic <既存 bot のリポジトリ>/
```

コピーするのは `intro_topic/` フォルダだけです（`bot.py` は単体起動用のランチャーなので不要、`tests/` や `try_*.py` も不要）。中身は次の5ファイル＋プロンプト2枚です。

```
intro_topic/
  __init__.py         # load_extension のエントリポイント（setup 関数）
  cog.py              # 本体。検知・静穏判定・投稿・反応計測・/topic コマンド
  topic_generator.py  # Gemini 呼び出し（Discord 非依存）
  store.py            # state.json の読み書き
  config.py           # 環境変数の読み込みと検証
  prompts/            # system.md（お題の書き方）/ judge.md（審査基準）
```

**置き場所**: リポジトリ直下（`bot.py` と並ぶ位置）を推奨します。`cogs/` のようなサブディレクトリの下でも動きます（内部は相対 import なので階層が変わっても壊れません）。その場合、次の手順の拡張名を `"cogs.intro_topic"` のようにドット区切りへ読み替えてください。

### 4-2. `load_extension` を1行足す

既存 bot の `setup_hook` に1行足します。`commands.Bot` を継承している場合:

```python
class MyBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.load_extension("cogs.music")      # 既存の Cog
        await self.load_extension("intro_topic")     # ← これを足すだけ
```

`bot = commands.Bot(...)` をそのまま使っている場合:

```python
@bot.event
async def setup_hook():
    await bot.load_extension("intro_topic")
```

> **`commands.Bot` であることが前提です**
> `load_extension` と Cog は `discord.ext.commands` の仕組みなので、素の `discord.Client` では使えません。`discord.Client` で組んでいる場合は `commands.Bot` へ移行してください（`commands.Bot` は `discord.Client` のサブクラスなので、既存のイベントハンドラはそのまま動きます）。

### 4-3. MESSAGE CONTENT INTENT を確認する

自己紹介の**本文**を読むため、次の2つが必要です。**最頻出のハマりどころ**で、OFF だと本文が空になり何も動きません（エラーも出ません）。

1. [Discord Developer Portal](https://discord.com/developers/applications) → 対象のアプリ → **Bot** → Privileged Gateway Intents → **MESSAGE CONTENT INTENT が ON**
2. コード側で `intents.message_content = True` になっていること

```python
intents = discord.Intents.default()
intents.message_content = True          # ← これが要る
bot = commands.Bot(command_prefix="!", intents=intents)
```

既存 bot がプレフィックスコマンドを使っているなら、ほぼ確実に両方とも有効になっています。スラッシュコマンドだけで運用している bot では OFF のことがあるので確認してください。

### 4-4. コマンドを同期する

この Cog は `/topic` をコマンドツリーへ**積むだけ**で、`tree.sync()` は呼びません（同期のタイミングはホスト bot の運用に任せる方が行儀がよく、レート制限の面でも安全なため）。

- **既存 bot に `await bot.tree.sync()` がある場合**: 何もする必要はありません。次回の同期で `/topic` が載ります（グローバル同期の反映には最大1時間かかります）
- **同期している場所が無い場合**: 一度だけ実行すれば済みます。下の行を足して1回起動し、そのあと行を消して構いません

```python
async def setup_hook(self) -> None:
    await self.load_extension("intro_topic")
    await self.tree.sync()     # 一度動かせば以降は不要
```

> **すぐ確認したいときのギルド同期には副作用があります**
> `tree.copy_global_to(guild=...)` → `tree.sync(guild=...)` なら即時反映されますが、これは**既存 bot のグローバルコマンドも全部そのギルドへ複製**します。一覧が二重に見えるので、既存 bot にグローバルコマンドがあるなら使わず、グローバル同期の反映を待つのが無難です。

## 5. 環境変数

必要なものだけを抜き出した表です。**全項目の一覧と既定値は [`.env.example`](.env.example) のコメント**にあります（投稿タイミング・見出し・通知ロール・投票の有無など、任意項目が20個ほどあります）。

| 変数 | 必要度 | 内容 |
|---|---|---|
| `DISCORD_TOKEN` | **不要** | 単体起動（`bot.py`）でログインするときだけ必要な項目です。組み込みではログインするのはホスト bot なので、**設定しないでください**（`.env.example` には載っていますが、組み込みでは無視して構いません） |
| `INTRO_CHANNEL_ID` | 必須 | `#自己紹介` のチャンネル ID |
| `CHAT_CHANNEL_ID` | 必須 | お題を投下する `#雑談` のチャンネル ID |
| `OWNER_USER_ID` | 事実上必須 | `/topic` を実行できる運用者のユーザー ID。未設定（空 / 0）だと**誰も管理コマンドを実行できません** |
| `LOG_CHANNEL_ID` | 推奨 | 運用ログ（起動・投稿・失敗）の投下先。何が起きているかを知る唯一の窓口になります |
| `GEMINI_API_KEY` | 任意 | 未設定だと定型お題モード（自己紹介を読まず、内蔵のお題12件だけを投稿） |

環境変数は「すでに設定されている値」が優先され、`.env` はその穴埋めにしか使われません。既存 bot が独自に環境変数を組み立てているなら、その仕組みにこれらを足すだけで済みます。`.env` で渡す場合は、**ホスト bot を起動するディレクトリ**に置いてください（カレントディレクトリを起点に探し、見つからなければ親へ遡ります）。

> **変数名が汎用的なので、既存 bot の環境変数と衝突しないか確認してください**
> 特に `DRY_RUN` / `LOG_CHANNEL_ID` / `OWNER_USER_ID` / `STATE_PATH` は、既存 bot がすでに別の意味で使っている可能性があります。**同名の変数がすでにあると、お互いの設定を読み合ってしまいます**（例: 既存 bot の `DRY_RUN=true` に引きずられてお題が投稿されない）。衝突していたら、`intro_topic/config.py` の `load_settings()` に並んでいる変数名のリテラル（`_require_bool("DRY_RUN", False)` など）にプレフィックス（`INTRO_TOPIC_` など）を付けて読み替えるのが最小の対処です。

## 6. 既存 bot と干渉しないことの確認

統合にあたって、この Cog が既存 bot から奪うものはありません。

- **イベントは追加式のリスナーだけを使います**。`on_message` は `@commands.Cog.listener()` として登録されるため、既存 bot の `on_message` はそのまま呼ばれます（Cog のリスナーが「もう1人の購読者」として増えるだけです）。ここが統合安全性の核なので、[テスト](tests/test_extension.py)でも検証しています
- **独自のプレフィックスコマンドを持ちません**。操作はすべて `/topic` に集約しているので、既存 bot のコマンド名・プレフィックスと衝突しません
- **`tree.sync()` を勝手に呼びません**（[4-4](#4-4-コマンドを同期する)）
- **`/topic` は一般メンバーには見えません**。コマンド一覧に出るのは**サーバー管理権限（Manage Server）を持つ人だけ**で、実際に実行できるのは `OWNER_USER_ID` 本人だけです。応答はすべて ephemeral なのでチャンネルには何も残りません

**権限**: 既存 bot にすでに付いているもので足りるかを確認してください。追加で必要なのは次の3つだけです。

| 権限 | 必要な場所 |
|---|---|
| View Channels / Read Message History | `#自己紹介`（本文を読む）と `#雑談`（静穏判定と反応の計測） |
| Send Messages | `#雑談`（お題の投下） |
| Embed Links（**任意**） | `LOG_CHANNEL_ID` のチャンネルのみ。無い場合はプレーンテキストへ自動で落ちるので、権限を増やさない運用も成立します |

管理者権限・メンバー情報・DM へのアクセスは不要です。OAuth2 スコープは既存 bot が `applications.commands` を持っていれば追加は不要です（スラッシュコマンドを1つでも使っているなら持っています）。

## 7. `state.json` の扱い

処理済みメッセージ ID・順番待ちのキュー・再利用プール・反応の計測結果を1枚の JSON に保存します。

- **置き場所**: 既定は bot を起動したカレントディレクトリ直下の `state.json` です。`STATE_PATH` で明示的に指定できます（例: `STATE_PATH=/var/lib/mybot/intro_topic_state.json`）。コンテナで動かしているなら**永続ボリュームの上**を指してください。消えると処理済み ID が失われ、過去の自己紹介が最大50件取り込み直されて**同じ人のお題がもう一度投稿されます**
- **`.gitignore` に必ず追加してください**。`state.json` には**自己紹介の本文がそのまま入ります**。トークンと同じ扱い（第三者が読めない場所）にしてください

```gitignore
state.json*
```

（`*` を付けているのは、保存中に一瞬だけできる `state.json.tmp` とバックアップの `state.json.bak` もまとめて除外するためです。`STATE_PATH` で別の場所を指定した場合は、そのパスを除外してください。）

- **バックアップ**: 取る場合も置き場所は上と同じ基準です。ファイルの保存はアトミック（一時ファイル → `os.replace`）なので、稼働中にコピーしても壊れた JSON を掴むことはありません。なお本文は `POOL_MAX_AGE_DAYS`（既定90日）を過ぎると自動で削除されますが、**バックアップ側は自動削除されません**。古いバックアップを残し続けないでください

## 8. 動作確認

1. **`DRY_RUN=true` で数日ソークする**。実際には投稿せず、「このタイミングでこの文面を投稿する予定」という内容だけが `LOG_CHANNEL_ID` のチャンネルに黄色の embed で流れます。本番チャンネルを一切汚さずに、お題の質と投下タイミングを確認できます
2. **`/topic status`** で、いま効いている設定（投稿先・投下間隔・時間帯・静穏判定）が意図どおりか確認する。トークンと API キーの値は出力されません
3. **`/topic now`** で条件を無視して1件出させる。`DRY_RUN=true` の間はログに予定が出るだけなので、文面の確認に使えます
4. 問題なければ `DRY_RUN=false` にして再起動する（設定は起動時に一度だけ読み込みます）

そのうえで、[README の本番切替チェックリスト](README.md#本番切替チェックリスト)のうち統合モードに関係する項目を確認してください。

| チェックリスト項目 | 統合モードでの読み替え |
|---|---|
| 1. 再招待と権限 | 再招待は不要。[6](#6-既存-bot-と干渉しないことの確認)の権限が既存 bot に届いているかだけ確認する |
| 2. MESSAGE CONTENT INTENT が ON | そのまま該当（[4-3](#4-3-message-content-intent-を確認する)） |
| 3. 本番値の確認 | 環境変数が本番値になっているか。特に**動作確認用に短縮した閾値**（`MIN_DELAY_MINUTES` / `QUIET_MINUTES` / `POST_INTERVAL_HOURS` / `POST_WINDOW_START` / `POST_WINDOW_END`）が残っていないか。既定は 30 / 15 / 48 / 19 / 22 |
| 4. `state.json` を空にする | テストサーバーで試してから本番へ向ける場合のみ。テスト由来の `message_id` は本番では全件見つからず、テスト中の計測結果が形式の重み学習を汚染します |
| 5. 煙テスト | `try_gemini.py` はこのリポジトリ側で実行してください（お題に専門用語・学校名・本名が混ざっていないかの目視確認） |
| 6. `DRY_RUN` でソーク | 上記1と同じ |
| 7. 異常時の一次対応 | まず `/topic pause` で自動投稿を止め、ログチャンネルの**赤 embed**（人が対処すべき事象）を確認する |

## 9. 更新の受け取り方とロールバック

**更新**: このリポジトリの `intro_topic/` をもう一度コピーして上書きするだけです。`state.json` はそのまま引き継げます（形式に互換性のない変更を入れる場合は、リリース側で移行方法を書きます）。

```bash
cd /tmp/intro-topic-bot && git pull
rm -rf <既存 bot のリポジトリ>/intro_topic
cp -r intro_topic <既存 bot のリポジトリ>/
```

**ロールバック**: 段階を選べます。

| やりたいこと | 対処 |
|---|---|
| 投稿だけ止めたい | `/topic pause`。自動投稿だけ止まり、反応の計測とキュー取り込みは続きます。再起動しても止まったままです |
| 機能を止めたい | `await bot.load_extension("intro_topic")` の行を消して再起動する。ワーカーもリスナーも `/topic` も残りません |
| 完全に撤収したい | 上記に加えて `intro_topic/` フォルダと `state.json` を削除する（`state.json` を消せば自己紹介の本文も残りません）。既存 bot 側に書き足したのは `load_extension` の1行と依存4つだけなので、これで元どおりです |

> **稼働中に外したい場合**
> `await bot.unload_extension("intro_topic")` で、再起動せずにその場で撤収できます。5分ワーカーは `cog_unload` で確実に停止し、`/topic` もツリーから外れます。

## 10. discord.py 以外のスタックだった場合

既存 bot が Python + discord.py ではない場合、このパッケージをそのまま載せることはできません。再実装のための材料は揃えてあります。

- **[proposal.md](proposal.md) の「3. アーキテクチャ」**に、検知から投稿・計測までの制御フローと閾値がすべて書いてあります
- **[proposal.md](proposal.md) の「7. 設定項目」**に、調整可能なパラメータと既定値の一覧があります
- **`intro_topic/topic_generator.py` と `intro_topic/prompts/`** は Discord に一切依存しない「テキスト入力 → 構造化出力」なので、**移植せずそのまま残せる部分**です（Gemini の構造化出力を使う別言語の SDK でも、`prompts/system.md` と `prompts/judge.md` をそのまま流用できます）
- お題の品質を左右するのはコードよりプロンプトです。**`prompts/` の2枚は必ず流用してください**（品質基準7条件・秘匿ルール・良い例と悪い例・審査基準が入っています）
