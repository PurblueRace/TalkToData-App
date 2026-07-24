import unittest

import pandas as pd

from query_result import is_effectively_empty_result


class QueryResultTests(unittest.TestCase):
    def test_zero_rows_are_empty(self):
        self.assertTrue(is_effectively_empty_result(pd.DataFrame()))

    def test_all_null_aggregate_row_is_empty(self):
        frame = pd.DataFrame({"합계": [None], "최종일": [None]})
        self.assertTrue(is_effectively_empty_result(frame))

    def test_count_zero_is_a_real_result(self):
        frame = pd.DataFrame({"건수": [0]})
        self.assertFalse(is_effectively_empty_result(frame))

    def test_partial_null_row_is_a_real_result(self):
        frame = pd.DataFrame({"거래처명": ["미지정"], "매출": [None]})
        self.assertFalse(is_effectively_empty_result(frame))


if __name__ == "__main__":
    unittest.main()
