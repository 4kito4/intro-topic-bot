"""topic_generator を Discord なしで単体確認する CLI。

使い方:
    uv run python try_gemini.py            # 内蔵サンプル3件を実行
    uv run python try_gemini.py "自己紹介文"  # 任意テキストで実行
"""

from __future__ import annotations

import logging
import sys

from config import load_gemini_only_settings
from topic_generator import TopicResult, generate_topic

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


def show(label: str, result: TopicResult) -> None:
    print(f"\n=== {label} ===")
    print(f"抽出: {', '.join(result.extracted_interests)}")
    print(f"検索使用: {result.used_search}")
    print("--- 投稿イメージ ---")
    print("💭 お題")
    if result.lead_in:
        print(result.lead_in)
    print(result.topic_question)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    api_key, model = load_gemini_only_settings()

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
        show("自由入力", generate_topic(text, api_key, model))
        return

    # 前のお題を履歴として渡し、テーマ・形式が被らないことを確認する
    recent: list[str] = []
    for label, text in SAMPLES:
        result = generate_topic(text, api_key, model, recent)
        show(label, result)
        recent.append(result.topic_question)


if __name__ == "__main__":
    main()
