"""Render literal submission text using the Bot API 10.5 block schema."""

from dataclasses import dataclass
from typing import Optional, Tuple

from utils.content_normalizer import NormalizedContent, web_url

TEMPLATE_VERSION = 1


def paragraph(text):
    return {"type": "paragraph", "text": text}


def heading(text):
    return {"type": "heading", "text": text, "size": 3}


def collapsed(*texts):
    return {
        "type": "expandable_blockquote",
        "blocks": [paragraph(t) for t in texts if t],
    }


@dataclass(frozen=True)
class PostContent:
    title: str
    summary: str
    body: str
    tags: str
    link: str
    submitter: str
    document: Optional[Tuple[str, str]]
    prefix: str = ""

    @classmethod
    def from_submission(cls, data, documents, *, show_submitter, prefix=""):
        data = dict(data)
        document = None
        if len(documents) == 1:
            kind, _, rest = documents[0].partition(":")
            file_id, _, name = rest.partition(":")
            if (
                kind == "document"
                and file_id
                and file_id.isascii()
                and file_id.replace("-", "").replace("_", "").isalnum()
            ):
                document = (file_id, name)
        author = (
            ("@" + (data.get("username") or str(data.get("user_id", ""))))
            if show_submitter
            else ""
        )
        return cls(
            data.get("title") or "",
            data.get("note") or "",
            data.get("text_content") or "",
            data.get("tags") or "",
            data.get("link") or "",
            author,
            document,
            prefix,
        )

    def lines(self):
        return tuple(
            dict.fromkeys(
                filter(
                    None,
                    (
                        self.prefix,
                        self.title,
                        self.summary,
                        self.tags,
                        self.link,
                        self.body,
                        f"投稿人：{self.submitter}" if self.submitter else "",
                    ),
                )
            )
        )

    def source(self):
        link = web_url(self.link)
        return NormalizedContent(
            text="\n\n".join(self.lines()),
            rich=True,
            links=[link] if link else [],
            documents=[self.document] if self.document else [],
            filenames=[self.document[1]] if self.document and self.document[1] else [],
        )

    def render(self):
        blocks = [heading(self.title or "频道投稿")]
        for text in (self.prefix, self.summary, self.tags):
            if text:
                blocks.append(paragraph(text))
        if self.link:
            url = web_url(self.link)
            blocks.append(
                paragraph(
                    {"type": "url", "text": url, "url": url} if url else self.link
                )
            )
        if self.body and self.body != self.summary:
            blocks.append(collapsed(self.body))
        if self.document:
            blocks.append(
                {
                    "type": "document",
                    "document": {"type": "document", "media": self.document[0]},
                }
            )
        if self.submitter:
            blocks.append(paragraph(f"投稿人：{self.submitter}"))
        return {"blocks": blocks, "skip_entity_detection": False}


def review_blocks(data, user_id, username, ai_result):
    content = PostContent.from_submission(data, [], show_submitter=False)
    body = "\n\n".join(content.lines()) or "（无文字说明）"
    return {
        "blocks": [
            heading(content.title or "新投稿待审核"),
            paragraph(f"投稿人：@{username or user_id}（{user_id}）"),
            paragraph((content.summary or content.body or body)[:220]),
            collapsed("完整内容", body),
            collapsed(
                "AI 审核结果",
                f"置信度：{float(ai_result.get('confidence') or 0):.0%}",
                f"分类：{ai_result.get('category') or '无'}",
                f"原因：{ai_result.get('reason') or '无'}",
            ),
        ]
    }
