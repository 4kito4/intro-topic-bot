# intro-topic-bot

自己紹介チャンネルへの新規投稿から専攻・興味を抽出し、**雑談チャンネルが静かなタイミング**で「専門でない人も答えられる粒度の問い」を投稿する Discord Bot のプロトタイプ。

例: 専攻が哲学の自己紹介 → 雑談チャンネルに
> 哲学に興味があるメンバーが参加してくれました！
> 記憶をすべて失っても、その人は同じ人だと思いますか？

## セットアップ

### 1. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. 左メニュー **Bot** タブ → **Reset Token** でトークンを発行（後で `.env` へ）
3. 同じ Bot タブの **Privileged Gateway Intents** で **MESSAGE CONTENT INTENT を必ず ON**
   （OFF のままだとメッセージ本文が空になり、何も動きません。最頻出のハマりポイント）
4. 左メニュー **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `View Channels` / `Send Messages` / `Read Message History` の3つのみ
5. 生成された URL を開き、テストサーバーに招待
6. テストサーバーに `#自己紹介` と `#雑談` チャンネルを作成
7. Discord の設定 → 詳細設定 → **開発者モード** を ON にし、各チャンネルを右クリック → 「IDをコピー」

### 2. Gemini API キーの取得

[Google AI Studio](https://aistudio.google.com) → Get API key。無料枠（gemini-3-flash: 10 RPM / 1,500 req/day）で十分動きます。

### 3. 環境変数

```powershell
Copy-Item .env.example .env
```

`.env` を開き、`DISCORD_TOKEN` / `GEMINI_API_KEY` / `INTRO_CHANNEL_ID` / `CHAT_CHANNEL_ID` を記入。

### 4. 起動

```powershell
uv sync
uv run python bot.py
```

## 動作確認

### Gemini 部の単体確認（Discord 不要）

```powershell
uv run python try_gemini.py                 # 内蔵サンプル3件（文系/理系/趣味のみ）
uv run python try_gemini.py "自己紹介文..."  # 任意テキスト
```

確認観点: 問いに専門用語が含まれていないか、学校名・本名などが漏れていないか。

### E2E（テストサーバー）

`.env` の閾値を短縮してから起動すると数分で一巡確認できます:

```
MIN_DELAY_MINUTES=1
QUIET_MINUTES=1
COOLDOWN_MINUTES=2
```

1. `#自己紹介` に50文字以上の自己紹介を投稿 → 1〜2分後に `#雑談` へ話題が投稿される
2. Bot を再起動 → 同じ自己紹介が再処理されない（`state.json` の `processed_ids`）
3. Bot 停止中に自己紹介を投稿 → 起動後に補完されて処理される
4. `#雑談` で発言し続けている間は投稿されず、静かになってから投稿される
5. `GEMINI_API_KEY` を壊して起動 → エラーログが出てキューに残る（3回失敗で破棄）

確認後、閾値を既定値（30 / 15 / 180）に戻すこと。

## 仕組み

```
#自己紹介 に投稿
  → on_message で検知し state.json のキューへ（Bot停止中の分は起動時に補完）
  → 5分ごとのワーカーが全条件を満たしたときだけ1件処理:
      ・自己紹介から30分経過（本人へのリアクションの時間を確保）
      ・#雑談 が15分静か（会話に割り込まない）
      ・前回のBot投稿から3時間経過（連投防止）
      ・9:00〜23:00 JST
  → Gemini (gemini-3-flash) 1コールで抽出+検索+話題生成
  → #雑談 に投稿（本人には言及しない・メンションしない）
```

## ファイル構成

| ファイル | 責務 |
|---|---|
| `bot.py` | エントリポイント。検知・補完・静穏判定・投稿 |
| `topic_generator.py` | Gemini 呼び出し（Discord 非依存）。プロンプトはここ |
| `store.py` | `state.json` の読み書き（アトミック保存） |
| `config.py` | `.env` の読み込みと検証 |
| `try_gemini.py` | Gemini 部の単体確認 CLI |
| `proposal.md` | Bot 運用者向けの組み込み提案資料 |
