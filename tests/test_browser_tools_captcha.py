import unittest

from agent_core.tools.browser_tools import (
    _extract_json_from_text,
    _normalize_captcha_result,
    _offset_clicks,
)


class CaptchaHelpersTest(unittest.TestCase):
    def test_extract_json_from_markdown(self):
        self.assertEqual(
            _extract_json_from_text('```json\n{"type":"text","chars":" a b "}\n```')["type"],
            "text",
        )

    def test_normalize_clicks_clamps_coordinates(self):
        result = _normalize_captcha_result(
            {"type": "点选", "confidence": "1.7", "clicks": [{"label": "星", "x": 500, "y": -3}]},
            120,
            80,
        )

        self.assertEqual(result["type"], "click")
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["clicks"], [{"char": "星", "x": 119, "y": 0}])

    def test_offset_clicks_maps_clip_to_viewport(self):
        result = _offset_clicks(
            {"type": "click", "clicks": [{"char": "A", "x": 10, "y": 20}]},
            100,
            50,
        )

        self.assertEqual(result["clicks"], [{"char": "A", "x": 110, "y": 70}])


if __name__ == "__main__":
    unittest.main()
