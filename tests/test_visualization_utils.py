import unittest

import pandas as pd

from visualization_utils import (
    format_compact_value,
    is_composition_question,
    is_correlation_question,
    is_time_series_question,
    profile_dataframe,
    time_sort_values,
    unit_kind,
)


class VisualizationUtilsTests(unittest.TestCase):
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

    def test_special_charts_require_explicit_question_intent(self):
        self.assertTrue(is_composition_question("거래처별 매출 비중"))
        self.assertFalse(is_composition_question("거래처별 매출 순위"))
        self.assertFalse(is_composition_question("평균 만족도 분포"))
        self.assertTrue(is_correlation_question("매출과 광고비의 상관관계"))
        self.assertFalse(is_correlation_question("매출과 광고비를 비교"))
        self.assertTrue(is_time_series_question("월별 재고 추이"))
        self.assertTrue(is_time_series_question("분기별 매출 변화"))
        self.assertFalse(is_time_series_question("현재 재고와 안전재고 비교"))


if __name__ == "__main__":
    unittest.main()
