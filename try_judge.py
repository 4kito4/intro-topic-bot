"""prompts/judge.md（審査プロンプト）を固定のお題で検査する CLI。

生成を挟まずに `_review_topic` だけを直接叩き、「落としてほしい問いを落とし、
通してほしい問いを通す」かを機械的に確認する。judge.md / system.md を触ったときの
回帰確認に使う（try_gemini.py と同格の手動確認ツール）。

グループと期待値:
- A: system.md の良い例。**全件合格**が期待値（落ちたら judge が厳しすぎる）
- B: topic_generator.FALLBACK_TOPICS 全件。定型お題は審査を通らずに投稿されるので、
     **全件合格**が期待値（落ちる定型お題は問い自体を差し替える）
- C: 落ちてほしい問い。**全件不合格**が期待値（1回でも合格したら judge が緩すぎる）

使い方:
    uv run python try_judge.py               # A+B+C を各3回判定
    uv run python try_judge.py --group B     # グループを絞る（A,B,C をカンマ区切り）
    uv run python try_judge.py --repeat 1    # 回数を減らす（無料枠の節約）
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from dataclasses import dataclass

from google import genai

from config import load_gemini_only_settings
from topic_generator import (
    FALLBACK_TOPICS,
    TopicFormat,
    TopicResult,
    _review_topic,
)

# 無料枠のレート制限に当てないための1コールごとの待ち時間（秒）
SLEEP_RANGE = (2.0, 4.0)


@dataclass(frozen=True)
class JudgeCase:
    group: str
    question: str
    format: TopicFormat
    expected_approved: bool


# A: system.md「# 良い例」の5件。judge を締めたときにここが落ちたら締めすぎ
GOOD_EXAMPLE_CASES: tuple[JudgeCase, ...] = (
    JudgeCase("A", "記憶をすべて失っても、その人は同じ人だと思いますか？", "hypothetical", True),
    JudgeCase("A", "幸せな人生と、意味のある人生は同じだと思いますか？", "values", True),
    JudgeCase("A", "AI を『友達』と呼べる日は来ると思いますか？", "values", True),
    JudgeCase(
        "A", "みなさんが最近スマホで撮った写真は、どんなものが多いですか？", "experience", True
    ),
    JudgeCase(
        "A",
        "手間をかけた自炊と、サッと済ませる外食、豊かなのはどっちだと思いますか？",
        "choice",
        True,
    ),
)

# C: 落ちてほしい問い。ゲートが飾りになっていないかの見張り
PROBE_CASES: tuple[JudgeCase, ...] = (
    # 住んでいる場所が推測できる（system.md 禁止事項）
    JudgeCase("C", "地元や今住んでいる地域のお気に入りの場所はどこですか？", "experience", False),
    # 「何のためにある」型の目的の抽象論（旧 values テンプレの典型）
    JudgeCase("C", "隙間時間って、結局何のためにあると思いますか？", "values", False),
    JudgeCase("C", "休みの日って、結局何のためにあると思いますか？", "values", False),
    # system.md「# 悪い例」より。専攻者しか答えられない
    JudgeCase("C", "カントの定言命法についてどう思いますか？", "values", False),
    # 記憶の総ざらいと重い自己開示を要求する
    JudgeCase("C", "人生で一番頑張ったことは何ですか？", "experience", False),
)


def _fallback_cases() -> tuple[JudgeCase, ...]:
    return tuple(
        JudgeCase("B", topic.topic_question, topic.format, True)
        for topic in FALLBACK_TOPICS
    )


def _all_cases() -> tuple[JudgeCase, ...]:
    return GOOD_EXAMPLE_CASES + _fallback_cases() + PROBE_CASES


def _as_topic_result(case: JudgeCase) -> TopicResult:
    return TopicResult(
        extracted_interests=[],
        lead_in="",
        topic_question=case.question,
        used_search=False,
        format=case.format,
        poll_options=None,
    )


def _judge_once(client: genai.Client, model: str, case: JudgeCase) -> tuple[bool | None, str]:
    """1回審査する。審査コール自体が失敗したら (None, エラー内容) を返す。"""
    try:
        review = _review_topic(client, model, _as_topic_result(case), None)
    except Exception as exc:  # noqa: BLE001 - 1件の失敗で全体を止めない
        return None, f"審査コール失敗: {exc}"
    detail = ", ".join(review.failed_criteria)
    return review.approved, f"{review.reason}{f' [{detail}]' if detail else ''}"


def _mark(approved: bool | None) -> str:
    if approved is None:
        return "!"
    return "○" if approved else "×"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="judge.md を固定のお題で検査する")
    parser.add_argument(
        "--group", default="A,B,C", help="検査するグループ（A,B,C のカンマ区切り）"
    )
    parser.add_argument("--repeat", type=int, default=3, help="1件あたりの判定回数")
    return parser.parse_args()


def main() -> None:
    # LLM は cp932 にない文字を返すため、Windows コンソールでも落ちないよう UTF-8 にする
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    groups = {g.strip().upper() for g in args.group.split(",") if g.strip()}
    api_key, model = load_gemini_only_settings()
    client = genai.Client(api_key=api_key)

    cases = [case for case in _all_cases() if case.group in groups]
    counters: dict[str, int] = {}
    rows: list[tuple[str, JudgeCase, list[bool | None]]] = []
    first_call = True
    for case in cases:
        counters[case.group] = counters.get(case.group, 0) + 1
        case_id = f"{case.group}{counters[case.group]}"
        print(f"\n=== [{case_id}] {case.question} ===")
        print(f"形式: {case.format} / 期待: {'合格' if case.expected_approved else '不合格'}")
        verdicts: list[bool | None] = []
        for i in range(args.repeat):
            if not first_call:
                time.sleep(random.uniform(*SLEEP_RANGE))
            first_call = False
            approved, reason = _judge_once(client, model, case)
            verdicts.append(approved)
            print(f"  {i + 1}回目 {_mark(approved)} {reason}")
        rows.append((case_id, case, verdicts))

    _print_summary(rows)


def _print_summary(rows: list[tuple[str, JudgeCase, list[bool | None]]]) -> None:
    print("\n\n========== サマリ ==========")
    mismatched: list[tuple[str, JudgeCase, list[bool | None]]] = []
    for case_id, case, verdicts in rows:
        marks = "".join(_mark(v) for v in verdicts)
        ok = all(v is case.expected_approved for v in verdicts)
        if not ok:
            mismatched.append((case_id, case, verdicts))
        expected = "合格" if case.expected_approved else "不合格"
        print(f"[{case_id}] {marks} 期待:{expected} {'OK ' if ok else 'ZURE'} {case.question}")

    print(f"\n検査 {len(rows)} 件 / 期待どおり {len(rows) - len(mismatched)} 件 / ズレ {len(mismatched)} 件")
    for group in sorted({case.group for _, case, _ in rows}):
        target = [(cid, c, v) for cid, c, v in rows if c.group == group]
        approved = sum(1 for _, _, verdicts in target for v in verdicts if v is True)
        total = sum(len(verdicts) for _, _, verdicts in target)
        print(f"  グループ {group}: {approved}/{total} が合格")
    if mismatched:
        print("\n--- 期待とズレたケース ---")
        for case_id, case, verdicts in mismatched:
            marks = "".join(_mark(v) for v in verdicts)
            expected = "合格" if case.expected_approved else "不合格"
            print(f"[{case_id}] 期待:{expected} 実測:{marks} {case.question}")


if __name__ == "__main__":
    main()
