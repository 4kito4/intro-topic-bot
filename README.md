# intro-topic-bot

表向きは「定期的にお題を投下する bot」。裏では自己紹介チャンネルの投稿から専攻・興味を抽出し、**その人が話しやすそうなお題**を雑談チャンネルが静かなタイミングで投下する Discord Bot のプロトタイプ。自己紹介を見ていることは投稿から一切わからないようにする（見てる感を出さない）。

例: 専攻が哲学の自己紹介がある → 雑談チャンネルに
> 💭 **お題**
> **記憶をすべて失っても、その人は同じ人だと思いますか？**

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
その他（投稿タイミング、オプトアウト絵文字、投票、`DRY_RUN` など）はすべて任意です。項目の意味と既定値は `.env.example` のコメントを参照してください。

### 4. 起動

```powershell
uv sync
uv run python bot.py
```

## 動作確認

### Gemini 部の単体確認（Discord 不要）

```powershell
uv run python try_gemini.py                 # 内蔵サンプル（文系/理系/趣味のみ）で5形式を1周
uv run python try_gemini.py "自己紹介文..."  # 任意テキスト
```

指定した形式・LLM が申告した形式・自己批評（審査）の合否と理由・投票の選択肢が表示されます。
確認観点: 問いに専門用語が含まれていないか、学校名・本名などが漏れていないか。

### ユニットテスト（Discord / Gemini を呼ばない）

```powershell
uv run pytest -q
```

静穏判定・候補選択・形式ローテーション・`state.json` の読み書きを検証します。

### E2E（テストサーバー）

`.env` の閾値を短縮してから起動すると数分で一巡確認できます:

```
MIN_DELAY_MINUTES=1
QUIET_MINUTES=1
POST_INTERVAL_HOURS=0
POST_WINDOW_START=0
POST_WINDOW_END=24
```

1. `#自己紹介` に50文字以上の自己紹介を投稿 → 数分以内に `#雑談` へお題が投稿される
2. Bot を再起動 → 同じ自己紹介が再処理されない（`state.json` の `processed_ids`）
3. Bot 停止中に自己紹介を投稿 → 起動後に補完されて処理される
4. `#雑談` で発言し続けている間は投稿されず、静かになってから投稿される
5. `GEMINI_API_KEY` を壊して起動 → エラーログが出てキューに残る（5分後→20分後と間隔を空けて再試行し、3回失敗で破棄）
6. キューが空の状態で `POST_INTERVAL_HOURS=0` → 過去の自己紹介（プール）由来、それも無ければ自己紹介を参照しない汎用お題が投稿される
7. `#自己紹介` の投稿に 🚫（`OPTOUT_EMOJI`）を付ける → その自己紹介は使われず破棄される
8. 自己紹介を編集 → 編集後の内容でお題が作られる / 削除 → 破棄されて次の候補に進む
9. 二択形式（choice）のお題 → Discord の投票が立つ（`USE_POLL=false` で無効化）
10. `DRY_RUN=true` → 実際には投稿されず、投稿予定の内容がログと `LOG_CHANNEL_ID` のチャンネルに出る（`state.json` も変化しない）
11. `OWNER_USER_ID` に自分の ID を設定し `!topic now` と発言 → 時間帯・間隔・静穏を無視して即投稿（他の人が打っても無反応）
12. 投稿から `MEASURE_AFTER_HOURS`（既定6時間）経過後 → `state.json` の `topic_stats` に返信・リアクション・投票の数が記録される

確認後、閾値を既定値（30 / 15 / 48 / 19 / 22）に戻すこと。

## 仕組み

```
#自己紹介 に投稿
  → on_message で検知し state.json のキューへ（Bot停止中の分は起動時に補完）
  → 5分ごとのワーカーが毎回:
      ・保持期限（POOL_MAX_AGE_DAYS=90日）を過ぎた自己紹介本文を削除
      ・投稿から MEASURE_AFTER_HOURS（6時間）経ったお題の反応を集計
    そのうえで、全条件を満たしたときだけ1件投稿:
      ・前回のお題投下から48時間経過（2日に1回の定期お題を装う）
      ・19:00〜22:00 JST の投稿時間帯内
      ・#雑談 の静穏判定（Discord の返信ラグを考慮）:
          直近60分に3件以上発言 =「会話中」→ 45分静かになるまで待つ
          それ未満 =「まばら」→ 15分でOK
  → ネタを3段構えで選ぶ（新規が無い週もお題が途切れないように）:
      ① 新規キュー: 古いものから順に（30分未経過・リトライ待ちは除外）
      ② 再利用プール: 使用回数が少ない順 → 最後に使ったのが古い順
      ③ どちらも無ければ、自己紹介を参照しない汎用お題
  → 選んだ自己紹介は生成の直前に元メッセージを取り直す:
      🚫 リアクションあり / 削除済み / 編集で短くなった → 破棄して次の候補へ
      編集されていた → 最新の本文で生成（取得失敗時は破棄せず今回だけ見送り）
  → お題の形式を直近2回と違うものから選ぶ
    （二択対立 / 経験共有 / 価値観 / あるある / 仮定 の5形式をローテーション）
  → Gemini (gemini-3.5-flash) で抽出+検索+お題生成
    → 別コールで自己批評（正解がない・専門知識不要・一言で答えられる など7観点）
    → 不合格なら理由を添えて1回だけ作り直す（それでも不合格ならログに残して投稿）
  → 「💭 お題」として #雑談 に投稿。問いは太字。二択形式なら Discord の投票として投稿
    （本人への言及・メンションなし。自己紹介由来であることは出さない）
  → 投稿を計測待ちに登録。集計した反応（返信×3 + 投票×2 + リアクション）の上位3件は
    次回以降のプロンプトに「反応が良かったお題の例」として還元される
```

## ファイル構成

| ファイル | 責務 |
|---|---|
| `bot.py` | エントリポイント。検知・補完・静穏判定・候補選択・投稿・反応計測 |
| `topic_generator.py` | Gemini 呼び出し（Discord 非依存）。生成と自己批評 |
| `prompts/` | `system.md`（お題の書き方・秘匿ルール・品質基準）と `judge.md`（審査基準）。コードを触らずに文面だけ調整できる |
| `store.py` | `state.json` の読み書き（アトミック保存） |
| `config.py` | `.env` の読み込みと検証 |
| `try_gemini.py` | Gemini 部の単体確認 CLI |
| `tests/` | pytest。Discord / Gemini を呼ばないロジックのテスト |
| `proposal.md` | Bot 運用者向けの組み込み提案資料 |
