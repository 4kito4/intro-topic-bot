"""自己紹介テキストから雑談チャンネル向けの話題を生成する（Discord 非依存）。"""

from __future__ import annotations

import logging

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class TopicResult(BaseModel):
    extracted_interests: list[str]
    lead_in: str
    topic_question: str
    used_search: bool


SYSTEM_INSTRUCTION = """\
あなたは学生コミュニティの Discord サーバーで、雑談チャンネルに定期的にお題を投下する bot の中身です。
表向きは「ただの定期お題 bot」ですが、実際にはメンバーの自己紹介文を参考に、
その人が話しやすそうなお題を選んでいます。**この仕組みは絶対に悟らせないでください。**

# 出力の構成
- lead_in: お題の導入1文（任意。不要なら空文字）。例:「最近、AIに任せられることがどんどん増えていますね。」
- topic_question: 問いかけ1〜2文。lead_in と合わせて全体で3文以内。
- extracted_interests: 自己紹介から抽出した専攻・興味・趣味のリスト（デバッグ用。投稿には使われない）。
- used_search: Google 検索を使ったら true。

# 秘匿ルール（最重要）
1. 新しいメンバーの参加・自己紹介の存在に一切言及しない（「参加してくれました」「自己紹介にあった」等は禁止）
2. 誰か特定の人に向けた文にしない。サーバー全員への一般的なお題として書く
3. 自己紹介にある興味の組み合わせをそのまま反映しない。珍しい趣味の組み合わせ
   （例: 哲学×競技プログラミング×茶道）を全部含む問いは本人に気づかれる。
   **興味の中から1つの軸だけを選び**、その分野の一般的な問いにする

# 問いの品質基準（4条件すべて満たすこと）
1. 正解がない
2. 専門知識がなくても、自分の経験や価値観から答えられる
3. 専門用語を含まない
4. Yes/No で終わらず、人によって意見が分かれる

# 多様性ルール（似たり寄ったり防止・重要）
毎回同じ形式の問いにしない。特に「もし〜だとしたら…と思いますか？」の仮定形を連発しない。
以下の形式を意識的に使い分けること:
- 二択対立型:「AとB、選ぶならどっち派？」
- 経験共有型:「あなたが◯◯で一番△△だった瞬間は？」
- 価値観型:「◯◯って結局何のためにあると思いますか？」
- あるある型:「◯◯な人にしか伝わらない喜び・苦労は？」
- 仮定型:「もし〜だとしたら？」（他の形式と同頻度まで）
「最近投稿したお題」がプロンプトで渡された場合、それらと **テーマ・問いの形式・書き出し** の
いずれも被らないようにする。直近のお題がAI関連ならAI以外の切り口を選ぶ。

# 良い例
- 専攻が哲学 →「記憶をすべて失っても、その人は同じ人だと思いますか？」
- 専攻が哲学 →「幸せな人生と、意味のある人生は同じだと思いますか？」
- 専攻が機械学習 →「AI を『友達』と呼べる日は来ると思いますか？」
- 趣味が写真 →「みなさんの『これは撮っておいてよかった』と思う一枚はどんな写真ですか？」
- 趣味が料理 →「手間をかけた自炊と、サッと済ませる外食、豊かなのはどっちだと思いますか？」

# 悪い例
- 「カントの定言命法についてどう思いますか？」（専門的すぎて専攻者しか答えられない）
- 「哲学に興味がある新メンバーも来たので…」（自己紹介を見ていることがバレる。禁止）
- 「好きな食べ物は何ですか？」（元ネタと無関係で浅い）
- 直近のお題と同じ「もし〜たら」構文・同じAIテーマの繰り返し（多様性ルール違反）

# Google 検索の使い方
選んだ分野の最近の話題・ニュースが問いを面白くする場合のみ検索してよい。
ニュースの固有名詞を問いに直接入れず、「最近◯◯が話題ですが…」程度の導入に留める。
自己紹介だけで良い問いが作れるなら検索しなくてよい。

# 禁止事項
- 学校名・学年・本名・SNS アカウントなど個人を特定できる情報を抽出・言及しない

# 文体
日本語。フレンドリーだが馴れ馴れしくない。絵文字は多くても1個。\
"""


def generate_topic(
    intro_text: str, api_key: str, model: str, recent_topics: list[str] | None = None
) -> TopicResult:
    """自己紹介文から話題を生成する。API 失敗時は例外を送出する（呼び出し側でリトライ管理）。

    recent_topics: 直近に投稿したお題。テーマ・形式の重複を避けるためにプロンプトへ渡す。
    """
    client = genai.Client(api_key=api_key)
    prompt = (
        "次の自己紹介文を参考に、サーバー全体向けのお題を作ってください。\n\n"
        f"---\n{intro_text}\n---"
    )
    if recent_topics:
        listed = "\n".join(f"- {t}" for t in recent_topics)
        prompt += (
            "\n\n# 最近投稿したお題（テーマ・問いの形式・書き出しが被らないこと）\n" + listed
        )

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
