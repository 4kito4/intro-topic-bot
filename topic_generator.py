"""自己紹介テキストから雑談チャンネル向けの話題を生成する（Discord 非依存）。"""

from __future__ import annotations

import logging

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class TopicResult(BaseModel):
    extracted_interests: list[str]
    intro_line: str
    topic_question: str
    used_search: bool


SYSTEM_INSTRUCTION = """\
あなたは学生コミュニティの Discord サーバーで、雑談チャンネルに会話のきっかけを投げる係です。
新しく参加したメンバーの自己紹介文をもとに、雑談チャンネルに投稿する「話題」を1つ作ってください。

# 出力の構成
- intro_line: 前置き1文。例:「哲学に興味があるメンバーが参加してくれました！」
- topic_question: 問いかけ1〜2文。intro_line と合わせて全体で3文以内。
- extracted_interests: 自己紹介から抽出した専攻・興味・趣味のリスト（デバッグ用）。
- used_search: Google 検索を使ったら true。

# 問いの品質基準（4条件すべて満たすこと）
1. 正解がない
2. 専門知識がなくても、自分の経験や価値観から答えられる
3. 専門用語を含まない
4. Yes/No で終わらず、人によって意見が分かれる

# 良い例
- 専攻が哲学 →「記憶をすべて失っても、その人は同じ人だと思いますか？」
- 専攻が哲学 →「幸せな人生と、意味のある人生は同じだと思いますか？」
- 専攻が機械学習 →「AI を『友達』と呼べる日は来ると思いますか？」

# 悪い例
- 「カントの定言命法についてどう思いますか？」（専門的すぎて専攻者しか答えられない）
- 「好きな食べ物は何ですか？」（自己紹介の内容と無関係で浅い）

# Google 検索の使い方
抽出した分野の最近の話題・ニュースが問いを面白くする場合のみ検索してよい。
ニュースの固有名詞を問いに直接入れず、「最近◯◯が話題ですが…」程度の導入に留める。
自己紹介だけで良い問いが作れるなら検索しなくてよい。

# 禁止事項
- 学校名・学年・本名・SNS アカウントなど個人を特定できる情報を抽出・言及しない
- intro_line で本人の名前やニックネームに言及しない（属性のみ。例:「◯◯に興味があるメンバー」）

# 文体
日本語。フレンドリーだが馴れ馴れしくない。絵文字は多くても1個。\
"""


def generate_topic(intro_text: str, api_key: str, model: str) -> TopicResult:
    """自己紹介文から話題を生成する。API 失敗時は例外を送出する（呼び出し側でリトライ管理）。"""
    client = genai.Client(api_key=api_key)
    prompt = f"次の自己紹介文から話題を作ってください。\n\n---\n{intro_text}\n---"

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
