"""
AI 审核响应解析测试
"""
from utils.ai_reviewer import AIReviewer


def _reviewer() -> AIReviewer:
    return AIReviewer.__new__(AIReviewer)


def test_parse_plain_json_response():
    result = _reviewer()._parse_response(
        """
        {
          "approved": true,
          "confidence": 0.95,
          "reason": "内容与接码服务相关",
          "category": "相关",
          "requires_manual": false
        }
        """
    )

    assert result.approved is True
    assert result.confidence == 0.95
    assert result.category == "相关"
    assert result.requires_manual is False


def test_parse_json_with_prefix_text():
    result = _reviewer()._parse_response(
        """
        审核结果如下：
        {
          "approved": true,
          "confidence": 0.9,
          "reason": "包含接码和短信验证",
          "category": "相关",
          "requires_manual": false
        }
        """
    )

    assert result.approved is True
    assert result.confidence == 0.9
    assert result.reason == "包含接码和短信验证"


def test_parse_uppercase_json_fence():
    result = _reviewer()._parse_response(
        """
        ```JSON
        {
          "approved": false,
          "confidence": 0.8,
          "reason": "主题不明确",
          "category": "待定",
          "requires_manual": true
        }
        ```
        """
    )

    assert result.approved is False
    assert result.confidence == 0.8
    assert result.category == "待定"
    assert result.requires_manual is True


def test_parse_response_without_json_uses_default():
    result = _reviewer()._parse_response("抱歉，我无法完成该审核。")

    assert result.approved is False
    assert result.confidence == 0.5
    assert result.reason == "无法解析 AI 响应"
    assert result.category == "待定"
    assert result.requires_manual is True


def test_parse_string_bool_fields():
    result = _reviewer()._parse_response(
        """
        {
          "approved": "false",
          "confidence": "0.7",
          "reason": "需要人工确认",
          "category": "待定",
          "requires_manual": "true"
        }
        """
    )

    assert result.approved is False
    assert result.confidence == 0.7
    assert result.requires_manual is True
