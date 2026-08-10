# intro-topic-bot の実行イメージ。
# 依存の解決は uv（ロックファイル固定）、実行は非 root ユーザー、状態は /data に置く。

# --- 依存インストール用のステージ（uv 本体はランタイムに持ち込まない） ---
FROM python:3.12-slim AS builder

# uv は公式イメージからバイナリだけをコピーする（apt/pip での導入より速く、版が固定できる）
COPY --from=ghcr.io/astral-sh/uv:0.10.6 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# ソースより先に依存だけを入れる。コード修正だけの再ビルドでこの層を使い回すため
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# --- 実行ステージ ---
FROM python:3.12-slim

# JST（19〜22時の投稿時間帯）は bot.py が固定オフセット timezone(+9) で持っているため、
# コンテナの TZ 設定や tzdata の有無に関係なく投稿時間帯の判定は正しく動く。
# ここで TZ を設定しているのはコンソールログの時刻表示（logging の %(asctime)s は
# ローカル時刻）を JST に揃えて、運用者がログと投稿時間帯を突き合わせやすくするためだけ。
ENV TZ=Asia/Tokyo \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    STATE_PATH=/data/state.json

# 非 root で動かす。UID を固定するのはボリュームの所有者を手順書に書けるようにするため
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home app \
    && mkdir -p /data \
    && chown app:app /data

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# 実行に必要なものだけ入れる（tests/ や try_*.py は含めない）
COPY bot.py config.py store.py topic_generator.py ./
COPY prompts ./prompts

# /data（状態ファイルの置き場所）には永続ボリュームをマウントすること。
# VOLUME 宣言はあえて置いていない（-v の付け忘れが匿名ボリュームで隠れてしまうため）
USER app

CMD ["python", "bot.py"]
