"""自己紹介テキストから雑談チャンネル向けの話題を生成する（Discord 非依存）。"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)

TopicFormat = Literal["choice", "experience", "values", "aruaru", "hypothetical"]
TOPIC_FORMATS: tuple[TopicFormat, ...] = get_args(TopicFormat)
FORMAT_LABELS: dict[TopicFormat, str] = {
    "choice": "二択対立型",
    "experience": "経験共有型",
    "values": "価値観型",
    "aruaru": "あるある型",
    "hypothetical": "仮定型",
}

MAX_REGENERATE = 1  # 審査で不合格だったときに作り直す回数

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class FallbackTopic:
    """Gemini を使わずに投稿できる定型お題。"""

    topic_question: str
    format: TopicFormat


# 定型お題。API キー未設定・Gemini 障害でも「定期お題 bot」の見た目を保つための最後の砦。
# 品質基準5条件（正解がない / 経験から答えられる / 専門用語なし / 意見が分かれる / 一言で成立）を
# 満たす普遍的な問いだけを置く。投票の選択肢を持たないので choice 形式は入れない
FALLBACK_TOPICS: tuple[FallbackTopic, ...] = (
    FallbackTopic("最近、思わず時間を忘れて夢中になったことは何ですか？", "experience"),
    FallbackTopic("最近「これは買ってよかった」と思ったものは何ですか？", "experience"),
    FallbackTopic("誰かに言われて、今でも覚えている一言は何ですか？", "experience"),
    FallbackTopic("これだけは譲れない、という自分ルールはありますか？", "values"),
    FallbackTopic("休みの日って、結局何のためにあると思いますか？", "values"),
    FallbackTopic("上手な息抜きって、どんなものだと思いますか？", "values"),
    FallbackTopic("疲れているときにしか出ない、自分のクセってありませんか？", "aruaru"),
    FallbackTopic("やる気が出ないとき、つい何をしてしまいますか？", "aruaru"),
    FallbackTopic("自分だけかも、と思っているちょっとした習慣はありますか？", "aruaru"),
    FallbackTopic("もし1日だけ休みが増えるとしたら、何に使いますか？", "hypothetical"),
    FallbackTopic("もう一度だけ同じ場所に行けるとしたら、どこを選びますか？", "hypothetical"),
    FallbackTopic("明日から何か新しいことを始めるなら、何をやってみたいですか？", "hypothetical"),
)


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise RuntimeError(f"プロンプトファイルが見つかりません: {path}")
    return path.read_text(encoding="utf-8").strip()


SYSTEM_INSTRUCTION = _load_prompt("system.md")
JUDGE_INSTRUCTION = _load_prompt("judge.md")


class TopicResult(BaseModel):
    extracted_interests: list[str]
    lead_in: str
    topic_question: str
    used_search: bool
    format: TopicFormat
    poll_options: list[str] | None = None  # choice 形式のときだけ投票の選択肢が入る


class ReviewResult(BaseModel):
    approved: bool
    failed_criteria: list[str]
    reason: str


def fallback_topic(
    recent_topics: list[str], rng: random.Random | None = None
) -> TopicResult:
    """定型お題を1件選ぶ（Gemini を呼ばない純関数）。

    recent_topics と被らない候補から選ぶ。全候補が被る場合は沈黙を避けるため全体から選ぶ
    （候補は直近保持件数より多いので、通常はここへ落ちない）。
    """
    chooser = rng or random
    candidates = [
        topic for topic in FALLBACK_TOPICS if topic.topic_question not in recent_topics
    ] or list(FALLBACK_TOPICS)
    chosen = chooser.choice(candidates)
    return TopicResult(
        extracted_interests=[],
        lead_in="",
        topic_question=chosen.topic_question,
        used_search=False,
        format=chosen.format,
        poll_options=None,
    )


def generate_topic(
    intro_text: str | None,
    api_key: str,
    model: str,
    recent_topics: list[str] | None = None,
    required_format: TopicFormat | None = None,
    good_examples: list[str] | None = None,
) -> tuple[TopicResult, ReviewResult | None]:
    """自己紹介文から話題を生成する。API 失敗時は例外を送出する（呼び出し側でリトライ管理）。

    intro_text: 参考にする自己紹介文。None なら自己紹介を参照しない汎用お題を作る。
    recent_topics: 直近に投稿したお題。テーマ・形式の重複を避けるためにプロンプトへ渡す。
    required_format: 今回書かせるお題の形式。None ならモデルに任せる。
    good_examples: 実際に反応が良かったお題。few-shot としてプロンプトへ渡す。
    戻り値: (生成結果, 審査結果)。審査自体が失敗した場合の審査結果は None。
    """
    client = genai.Client(api_key=api_key)
    prompt = _build_prompt(intro_text, recent_topics, required_format, good_examples)

    result = _generate(client, model, prompt)
    review = _review_or_none(client, model, result, required_format)
    for _ in range(MAX_REGENERATE):
        if review is None or review.approved:
            break
        logger.info("審査で不合格のため作り直します: %s", review.reason)
        result = _generate(client, model, prompt + _rejection_note(review))
        review = _review_or_none(client, model, result, required_format)

    if review is not None and not review.approved:
        logger.warning(
            "作り直し後も審査不合格だが、沈黙を避けるため採用: %s (%s)",
            review.reason,
            ", ".join(review.failed_criteria),
        )
    if required_format is not None and result.format != required_format:
        logger.warning(
            "指定形式と申告形式が不一致: 指定=%s 申告=%s", required_format, result.format
        )
    return result, review


def _build_prompt(
    intro_text: str | None,
    recent_topics: list[str] | None,
    required_format: TopicFormat | None,
    good_examples: list[str] | None,
) -> str:
    if intro_text is None:
        prompt = (
            "自己紹介文はありません。サーバー（学生コミュニティで、AI に興味がある人が多い）"
            "全体に向けた一般的なお題を作ってください。"
            "extracted_interests は空リストにしてください。"
        )
    else:
        prompt = (
            "次の自己紹介文を参考に、サーバー全体向けのお題を作ってください。\n\n"
            f"---\n{intro_text}\n---"
        )
    if recent_topics:
        listed = "\n".join(f"- {t}" for t in recent_topics)
        prompt += (
            "\n\n# 最近投稿したお題（テーマ・問いの形式・書き出しが被らないこと）\n" + listed
        )
    if required_format is not None:
        prompt += (
            f"\n\n# 今回の指定形式\n今回は **{FORMAT_LABELS[required_format]}** で書くこと。"
            f"format フィールドには `{required_format}` を入れる。"
        )
    if good_examples:
        listed = "\n".join(f"- {t}" for t in good_examples)
        prompt += (
            "\n\n# 実際に反応が良かったお題の例（雰囲気の参考。丸写しはしないこと）\n" + listed
        )
    return prompt


def _rejection_note(review: ReviewResult) -> str:
    failed = "\n".join(f"- {c}" for c in review.failed_criteria) or "- （明示なし）"
    return (
        "\n\n# 直前の案が審査で不合格になった理由（同じ失敗を繰り返さないこと）\n"
        f"{failed}\n- 講評: {review.reason}"
    )


def _generate(client: genai.Client, model: str, prompt: str) -> TopicResult:
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_mime_type="application/json",
                response_schema=TopicResult,
            ),
        )
        parsed = response.parsed
        if isinstance(parsed, TopicResult):
            return parsed
        # parsed が None の場合はテキストから再構築を試みる
        return TopicResult.model_validate_json(response.text or "")
    except Exception as exc:  # noqa: BLE001 - SDK/モデルの組み合わせ差異を吸収する境界
        logger.warning("検索+構造化出力の同時指定に失敗。2段構成にフォールバック: %s", exc)
        return _generate_two_step(client, model, prompt)


def _generate_two_step(client: genai.Client, model: str, prompt: str) -> TopicResult:
    """フォールバック: 1回目=検索付き自由生成、2回目=構造化整形。"""
    draft = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    draft_text = draft.text
    if not draft_text:
        raise RuntimeError("Gemini から空の応答が返りました")

    formatted = client.models.generate_content(
        model=model,
        contents=(
            "次のテキストを、指示されたスキーマの JSON に整形してください。内容は変えないこと。\n\n"
            + draft_text
        ),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=TopicResult,
        ),
    )
    parsed = formatted.parsed
    if isinstance(parsed, TopicResult):
        return parsed
    return TopicResult.model_validate_json(formatted.text or "")


def _review_or_none(
    client: genai.Client,
    model: str,
    result: TopicResult,
    required_format: TopicFormat | None,
) -> ReviewResult | None:
    """審査を試みる。審査 API 自体が失敗した場合は投稿を優先して None を返す。"""
    try:
        return _review_topic(client, model, result, required_format)
    except Exception as exc:  # noqa: BLE001 - 審査失敗で生成結果ごと捨てない
        logger.warning("お題の審査に失敗したため審査なしで採用します: %s", exc)
        return None


def _review_topic(
    client: genai.Client,
    model: str,
    result: TopicResult,
    required_format: TopicFormat | None,
) -> ReviewResult:
    """生成したお題を LLM に自己批評させる（検索なしの軽い1コール）。"""
    required_label = (
        FORMAT_LABELS[required_format] if required_format is not None else "指定なし"
    )
    contents = (
        "次のお題を判定軸に照らして審査してください。\n\n"
        f"- lead_in: {result.lead_in}\n"
        f"- topic_question: {result.topic_question}\n"
        f"- 抽出された興味: {', '.join(result.extracted_interests)}\n"
        f"- 指定形式: {required_label}\n"
        f"- 申告形式: {FORMAT_LABELS[result.format]}"
    )
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=JUDGE_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=ReviewResult,
        ),
    )
    parsed = response.parsed
    if isinstance(parsed, ReviewResult):
        return parsed
    return ReviewResult.model_validate_json(response.text or "")
