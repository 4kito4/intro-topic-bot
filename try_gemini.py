"""topic_generator を Discord なしで単体確認する CLI。

使い方:
    uv run python try_gemini.py                  # 内蔵サンプルで5形式を1周
    uv run python try_gemini.py "自己紹介文"       # 任意テキストで実行
    uv run python try_gemini.py --news "ニュース本文"  # ニュースを素材に実行
"""

from __future__ import annotations

import logging
import sys

from config import DEFAULT_TOPIC_TITLE, load_gemini_only_settings
from topic_generator import (
    FORMAT_LABELS,
    TOPIC_FORMATS,
    ReviewResult,
    TopicFormat,
    TopicResult,
    generate_topic,
)

SAMPLES: list[tuple[str, str]] = [
    (
        "文系（哲学）",
        "はじめまして！都内の大学3年で哲学を専攻しています。"
        "最近は心の哲学と人格の同一性に興味があります。AI にも関心があってこのサーバーに入りました。"
        "よろしくお願いします！",
    ),
    (
        "理系（機械学習）",
        "こんにちは。情報系の大学院生で、機械学習の研究をしています。"
        "特に自然言語処理が専門で、趣味は競技プログラミングとボードゲームです。"
        "みなさんと技術の話ができたら嬉しいです。",
    ),
    (
        "趣味のみ",
        "初めまして〜。大学1年です。専攻はまだ決めてないけど、写真を撮るのが好きで、"
        "週末はよくカメラを持って散歩してます。あとカフェ巡りも好きです。仲良くしてください！",
    ),
]


def show(
    label: str,
    result: TopicResult,
    review: ReviewResult | None,
    required_format: TopicFormat | None = None,
) -> None:
    print(f"\n=== {label} ===")
    print(f"抽出: {', '.join(result.extracted_interests)}")
    required_label = (
        FORMAT_LABELS[required_format] if required_format is not None else "指定なし"
    )
    print(f"指定形式: {required_label} / 申告形式: {FORMAT_LABELS[result.format]}")
    print(f"検索使用: {result.used_search}")
    if review is None:
        print("審査: 未実施（審査コールが失敗）")
    else:
        verdict = "合格" if review.approved else f"不合格 ({', '.join(review.failed_criteria)})"
        print(f"審査: {verdict} — {review.reason}")
    # 実際の投稿は TOPIC_TITLE / TOPIC_FOOTER で変えられるが、ここは既定タイトルで表示する
    print("--- 投稿イメージ ---")
    print(DEFAULT_TOPIC_TITLE)
    if result.lead_in:
        print(result.lead_in)
    print(f"**{result.topic_question}**")
    if result.poll_options:
        print(f"投票の選択肢: {' / '.join(result.poll_options)}")


def main() -> None:
    # LLM は cp932 にない文字（em-dash・絵文字等）を返すため、Windows コンソールでも
    # UnicodeEncodeError で落ちないよう出力を UTF-8 にする
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    api_key, model = load_gemini_only_settings()

    args = sys.argv[1:]
    if args and args[0] == "--news":
        news = " ".join(args[1:])
        if not news:
            print("--news の後にニュース本文を渡してください")
            return
        # ニュース経路は自己紹介を参照しない（intro_text=None）
        result, review = generate_topic(None, api_key, model, news_text=news)
        show("ニュース", result, review)
        return

    if args:
        text = " ".join(args)
        result, review = generate_topic(text, api_key, model)
        show("自由入力", result, review)
        return

    # 5形式を1周する。サンプルは順番に使い回し、前のお題を履歴として渡して重複を確認する
    recent: list[str] = []
    for i, required_format in enumerate(TOPIC_FORMATS):
        label, text = SAMPLES[i % len(SAMPLES)]
        result, review = generate_topic(text, api_key, model, recent, required_format)
        show(f"{label} × {FORMAT_LABELS[required_format]}", result, review, required_format)
        recent.append(result.topic_question)


if __name__ == "__main__":
    main()
