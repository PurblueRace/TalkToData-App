import unittest
from types import SimpleNamespace

import pandas as pd

try:
    import plotly.graph_objects as go
except ImportError:
    go = None

from analysis_visuals import (
    build_management_chart_html,
    embed_management_chart_in_rendered_report,
    prepare_analysis_visual_data,
    should_embed_management_chart,
    summarize_management_chart,
)


class AnalysisVisualTests(unittest.TestCase):
    def test_visual_data_removes_sensitive_columns_values_and_script_markup(self):
        frame = pd.DataFrame(
            [
                ["홍길동", "hong@example.com", "BCM</script><script>alert(1)", 100],
                ["김노을", "010-1234-5678", "CER", 120],
            ],
            columns=["사원명", "사원명", "제품명", "매출액"],
        )

        safe_frame, safe_question = prepare_analysis_visual_data(
            frame,
            "홍길동 담당자의 제품별 매출",
            'SELECT * FROM "매출" WHERE "사원명" = \'홍길동\'',
        )

        safe_text = safe_frame.to_string()
        self.assertNotIn("사원명", safe_frame.columns)
        self.assertNotIn("홍길동", safe_text)
        self.assertNotIn("hong@example.com", safe_text)
        self.assertNotIn("</script>", safe_text.lower())
        self.assertNotIn("홍길동", safe_question)
        self.assertIn("민감정보 마스킹", safe_question)

    def test_visual_data_redacts_sensitive_values_inside_category_columns(self):
        frame = pd.DataFrame(
            {
                "담당자명": ["홍길동", "김노을"],
                "표시구분": pd.Series(["홍길동", "일반"], dtype="category"),
                "매출액": [100, 120],
            }
        )

        safe_frame, _ = prepare_analysis_visual_data(frame, "구분별 매출")

        self.assertNotIn("담당자명", safe_frame.columns)
        self.assertNotIn("홍길동", safe_frame.to_string())
        self.assertIn("민감정보 마스킹", safe_frame.to_string())

    def test_chart_is_skipped_for_empty_or_malformed_ai_report(self):
        self.assertFalse(
            should_embed_management_chart(
                "",
                {"html_fragment": "<section>AI가 분석 결과를 반환하지 않았습니다.</section>"},
            )
        )
        self.assertFalse(
            should_embed_management_chart(
                "{broken",
                {"html_fragment": "<section>분석 결과 형식 오류</section>"},
            )
        )
        self.assertTrue(
            should_embed_management_chart(
                "<section>정상 분석</section>",
                {"html_fragment": "<section>정상 분석</section>"},
            )
        )

    def test_chart_is_inserted_into_legacy_rendered_report(self):
        rendered_report = """
        <style>.report-section{background:#fff}</style>
        <article class="report-shell">
          <section class="report-section"><h2>핵심 지표와 변화</h2><p>지표</p></section>
          <section class="report-section"><h2>핵심 진단</h2><p>진단</p></section>
          <section class="report-section"><h2>실행 계획</h2><p>실행</p></section>
        </article>
        """
        chart_html = '<section data-analysis-chart="1">차트</section>'

        result = embed_management_chart_in_rendered_report(
            rendered_report,
            chart_html,
        )

        self.assertEqual(result.count('data-analysis-chart="1"'), 1)
        self.assertLess(result.index("핵심 지표와 변화"), result.index("차트"))
        self.assertLess(result.index("차트"), result.index("핵심 진단"))

    @unittest.skipUnless(go is not None, "Plotly is installed by the app requirements")
    def test_chart_html_contains_exactly_one_chart_block_and_safe_payload(self):
        figure = go.Figure(
            go.Bar(
                x=[10, 20],
                y=["BCM", "CER</script><script>alert(1)"],
                orientation="h",
                name="매출액",
            )
        )
        chart = {"kind": "ranking", "figure": figure, "note": "항목 비교"}

        rendered = build_management_chart_html(
            chart,
            source_number=2,
            question="제품별 매출 비교",
            row_count=2,
        )

        self.assertEqual(rendered.count('data-analysis-chart="1"'), 1)
        self.assertIn("표2 · 제품별 매출 비교", rendered)
        self.assertIn("차트 해석", rendered)
        self.assertNotIn("</script><script>alert(1)", rendered.lower())

    def test_chart_summary_uses_visible_first_and_last_values(self):
        figure = SimpleNamespace(
            data=[
                SimpleNamespace(
                    x=["2026년 6월", "2026년 7월"],
                    y=[100_000_000, 125_000_000],
                    name="매출액",
                    orientation=None,
                )
            ],
            layout=SimpleNamespace(
                xaxis=SimpleNamespace(
                    title=SimpleNamespace(text=""),
                    ticksuffix="",
                ),
                yaxis=SimpleNamespace(
                    title=SimpleNamespace(text=""),
                    ticksuffix="원",
                ),
            ),
        )

        summary = summarize_management_chart({"kind": "line", "figure": figure})

        self.assertIn("2026년 6월", summary)
        self.assertIn("2026년 7월", summary)
        self.assertIn("25.0% 증가", summary)


if __name__ == "__main__":
    unittest.main()
