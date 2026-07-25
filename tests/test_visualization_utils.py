import unittest
from types import SimpleNamespace

import pandas as pd

from visualization_utils import (
    available_visualization_types,
    build_management_chart_explanation,
    format_compact_value,
    is_composition_question,
    is_correlation_question,
    is_time_series_question,
    profile_dataframe,
    profile_unit_label,
    time_sort_values,
    unit_kind,
    visual_number_format,
    visual_metric_aggregation,
)


class VisualizationUtilsTests(unittest.TestCase):
    @staticmethod
    def _fake_figure(*traces, x_title="", y_title="", x_suffix="", y_suffix=""):
        return SimpleNamespace(
            data=list(traces),
            layout=SimpleNamespace(
                xaxis=SimpleNamespace(
                    title=SimpleNamespace(text=x_title),
                    ticksuffix=x_suffix,
                ),
                yaxis=SimpleNamespace(
                    title=SimpleNamespace(text=y_title),
                    ticksuffix=y_suffix,
                ),
            ),
        )

    def test_profile_separates_time_measure_category_and_identifiers(self):
        frame = pd.DataFrame(
            {
                "거래일자": ["2025-01-01", "2025-02-01"],
                "계정코드": [40100, 40200],
                "계정명": ["제품매출", "상품매출"],
                "매출액": [120_000_000, 80_000_000],
                "마진율": [32.5, 28.1],
            }
        )

        profile = profile_dataframe(frame, "월별 매출액")

        self.assertEqual(profile.time_columns, ["거래일자"])
        self.assertEqual(profile.category_columns, ["계정명"])
        self.assertEqual(profile.measure_columns[0], "매출액")
        self.assertNotIn("계정코드", profile.measure_columns)

    def test_common_manufacturing_period_columns_become_time_dimensions(self):
        frame = pd.DataFrame(
            {
                "기준월": [202501, 202502],
                "생산주차": [1, 2],
                "생산수량": [120, 140],
            }
        )

        profile = profile_dataframe(frame, "기준월별 생산수량 추이")

        self.assertEqual(profile.time_columns, ["기준월", "생산주차"])
        self.assertEqual(profile.measure_columns, ["생산수량"])

    def test_question_intent_prioritizes_matching_measure(self):
        frame = pd.DataFrame({"제품명": ["A", "B"], "매출액": [10, 20], "마진율": [30, 40]})
        profile = profile_dataframe(frame, "제품별 마진율 비교")
        self.assertEqual(profile.measure_columns[0], "마진율")

    def test_quarters_and_months_sort_chronologically(self):
        quarters = pd.Series(["2025년 4분기", "2025년 1분기", "2025년 3분기"])
        order = time_sort_values(quarters).sort_values().index.tolist()
        self.assertEqual(order, [1, 2, 0])

        months = pd.Series(["2025년 12월", "2025년 2월", "2025년 9월"])
        order = time_sort_values(months).sort_values().index.tolist()
        self.assertEqual(order, [1, 2, 0])

    def test_units_and_compact_labels_follow_metric_meaning(self):
        self.assertEqual(unit_kind("매출액"), "currency")
        self.assertEqual(unit_kind("마진율"), "percent")
        self.assertEqual(unit_kind("실제비용률"), "percent")
        self.assertEqual(unit_kind("불량률"), "percent")
        self.assertEqual(unit_kind("판매수량"), "count")
        self.assertEqual(unit_kind("매출건수"), "count")
        self.assertEqual(unit_kind("매입수량"), "count")
        self.assertEqual(unit_kind("재고금액"), "currency")
        self.assertEqual(format_compact_value(125_000_000, "매출액"), "1.2억원")
        self.assertEqual(format_compact_value(37.25, "마진율"), "37.2%")
        self.assertEqual(format_compact_value(1200, "판매수량"), "1,200개")

    def test_visual_aggregation_does_not_sum_prices_or_snapshots(self):
        self.assertEqual(visual_metric_aggregation("매출액"), "sum")
        self.assertEqual(visual_metric_aggregation("판매수량"), "sum")
        self.assertEqual(visual_metric_aggregation("판매가격"), "mean")
        self.assertEqual(visual_metric_aggregation("표준원가"), "mean")
        self.assertEqual(visual_metric_aggregation("진행률"), "mean")
        self.assertEqual(visual_metric_aggregation("현재재고"), "last")

    def test_single_explicit_inventory_unit_overrides_generic_count_label(self):
        profile = profile_dataframe(
            pd.DataFrame(
                {
                    "원재료명": ["필터", "시약"],
                    "단위": ["kg", "kg"],
                    "안전재고": [100, 20],
                }
            )
        )

        self.assertEqual(profile_unit_label(profile, "안전재고"), "kg")
        self.assertEqual(profile_unit_label(profile, "재고금액"), "원")
        self.assertEqual(profile_unit_label(profile, "매출건수"), "건")
        self.assertEqual(format_compact_value(0.025, "사용수량", unit_override="kg"), "0.025kg")
        self.assertEqual(visual_number_format("사용수량", "kg"), ",.3f")
        self.assertEqual(visual_number_format("매출건수", "건"), ",.0f")

    def test_special_charts_require_explicit_question_intent(self):
        self.assertTrue(is_composition_question("거래처별 매출 비중"))
        self.assertFalse(is_composition_question("거래처별 매출 순위"))
        self.assertFalse(is_composition_question("평균 만족도 분포"))
        self.assertTrue(is_correlation_question("매출과 광고비의 상관관계"))
        self.assertFalse(is_correlation_question("매출과 광고비를 비교"))
        self.assertTrue(is_time_series_question("월별 재고 추이"))
        self.assertTrue(is_time_series_question("분기별 매출 변화"))
        self.assertFalse(is_time_series_question("현재 재고와 안전재고 비교"))

    def test_available_chart_types_match_the_selected_table_shape(self):
        monthly = pd.DataFrame(
            {
                "기준월": [f"2026년 {month}월" for month in range(1, 13)],
                "제품명": ["BCM", "CER"] * 6,
                "매출액": [100 + month * 10 for month in range(12)],
                "매출원가": [70 + month * 8 for month in range(12)],
            }
        )

        choices = available_visualization_types(monthly, "월별 제품 매출 추이")

        self.assertEqual(choices[0], "auto")
        self.assertIn("bar", choices)
        self.assertIn("line", choices)
        self.assertIn("scatter", choices)
        self.assertIn("distribution", choices)

    def test_donut_requires_a_small_nonnegative_composition(self):
        composition = pd.DataFrame(
            {
                "제품명": ["BCM", "CER", "MAL"],
                "매출액": [120, 80, 40],
            }
        )
        negative = composition.assign(매출액=[120, -80, 40])
        prices = composition.rename(columns={"매출액": "판매가격"})
        with_missing_category = pd.DataFrame(
            {"제품명": ["BCM", None], "매출액": [120, 40]}
        )
        with_infinite_value = pd.DataFrame(
            {"제품명": ["BCM", "CER"], "매출액": [float("inf"), 40]}
        )
        stock_snapshot = pd.DataFrame(
            {"제품명": ["BCM", "CER"], "재고금액": [120, 40]}
        )
        cumulative_sales = pd.DataFrame(
            {"제품명": ["BCM", "CER"], "누적매출액": [120, 40]}
        )

        self.assertIn("donut", available_visualization_types(composition))
        self.assertNotIn("line", available_visualization_types(composition))
        self.assertNotIn("donut", available_visualization_types(negative))
        self.assertNotIn("donut", available_visualization_types(prices))
        self.assertIn("donut", available_visualization_types(with_missing_category))
        self.assertNotIn("donut", available_visualization_types(with_infinite_value))
        self.assertNotIn("donut", available_visualization_types(stock_snapshot))
        self.assertNotIn("donut", available_visualization_types(cumulative_sales))

    def test_small_numeric_samples_do_not_offer_scatter_or_distribution(self):
        small = pd.DataFrame(
            {
                "제품명": ["A", "B", "C"],
                "매출액": [10, 20, 30],
                "매출원가": [7, 14, 21],
            }
        )

        choices = available_visualization_types(small)

        self.assertNotIn("scatter", choices)
        self.assertNotIn("distribution", choices)

    def test_mixed_units_and_constant_metrics_hide_unsafe_manual_charts(self):
        mixed_units = pd.DataFrame(
            {
                "원재료명": ["필터", "시약", "용기"],
                "단위": ["EA", "kg", "L"],
                "안전재고": [100, 20, 50],
            }
        )
        constants = pd.DataFrame(
            {
                "제품명": [f"제품{index}" for index in range(10)],
                "매출액": [100] * 10,
                "매출원가": list(range(10)),
            }
        )

        self.assertEqual(available_visualization_types(mixed_units), ["auto"])
        constant_choices = available_visualization_types(constants)
        self.assertNotIn("scatter", constant_choices)
        self.assertNotIn("distribution", constant_choices)

    def test_chart_choice_requires_two_non_null_dimension_values(self):
        sparse = pd.DataFrame(
            {
                "제품명": ["A", "B"],
                "매출액": [10, None],
            }
        )

        choices = available_visualization_types(sparse)

        self.assertNotIn("bar", choices)
        self.assertNotIn("line", choices)

    def test_management_chart_explanation_describes_time_change(self):
        figure = self._fake_figure(
            SimpleNamespace(
                x=["2026년 6월", "2026년 7월"],
                y=[100_000_000, 125_000_000],
                name="매출액",
                orientation=None,
                type="scatter",
            ),
            y_suffix="원",
        )

        explanation = build_management_chart_explanation(
            {"kind": "line", "figure": figure},
        )

        self.assertIn("2026년 6월", explanation)
        self.assertIn("2026년 7월", explanation)
        self.assertIn("25.0% 증가", explanation)

    def test_management_chart_explanation_names_top_category_and_share(self):
        figure = self._fake_figure(
            SimpleNamespace(
                labels=["BCM", "CER", "MAL"],
                values=[60_000_000, 30_000_000, 10_000_000],
                type="pie",
            ),
        )

        explanation = build_management_chart_explanation(
            {"kind": "composition", "figure": figure},
        )

        self.assertIn("BCM", explanation)
        self.assertIn("60.0%", explanation)

    def test_management_chart_explanation_cautions_on_correlation(self):
        figure = self._fake_figure(
            SimpleNamespace(
                x=[10, 20, 30, 40, 50, 60, 70, 80],
                y=[20, 40, 60, 80, 100, 120, 140, 160],
                type="scatter",
            ),
            x_title="광고비",
            y_title="매출액",
        )

        explanation = build_management_chart_explanation(
            {"kind": "correlation", "figure": figure},
        )

        self.assertIn("상관계수", explanation)
        self.assertIn("원인을 단정", explanation)

    def test_management_chart_explanation_uses_benchmark_alerts(self):
        figure = self._fake_figure(
            SimpleNamespace(
                name="현재재고",
                x=[5, 12],
                y=["BCM", "CER"],
                orientation="h",
                type="bar",
            ),
            SimpleNamespace(
                name="안전재고",
                x=[10, 10],
                y=["BCM", "CER"],
                orientation="h",
                type="bar",
            ),
            x_suffix="개",
        )

        explanation = build_management_chart_explanation(
            {
                "kind": "benchmark",
                "figure": figure,
                "note": "빨간색은 안전재고보다 부족한 항목입니다.",
            }
        )

        self.assertIn("1개가 기준 미달", explanation)
        self.assertIn("BCM", explanation)


if __name__ == "__main__":
    unittest.main()
