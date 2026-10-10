"""Small unit tests for repair merge behavior; run: python -m unittest test_repair_etf_master.py"""
import unittest
import pandas as pd
from repair_etf_master import merge_non_null, KEYS

class MergeRepairTests(unittest.TestCase):
    def test_existing_valid_value_is_not_overwritten(self):
        old = pd.DataFrame([{"날짜":"2026-01-02","시장":"US","티커/종목코드":"SPY","종가":100.0,"수정종가":None}])
        new = pd.DataFrame([{"날짜":"2026-01-02","시장":"US","티커/종목코드":"SPY","종가":101.0,"수정종가":99.0}])
        result = merge_non_null(old, new)
        self.assertEqual(result.loc[0, "종가"], 100.0)
        self.assertEqual(result.loc[0, "수정종가"], 99.0)

    def test_new_date_row_is_added(self):
        old = pd.DataFrame([{"날짜":"2026-01-02","시장":"US","티커/종목코드":"SPY","종가":100.0}])
        new = pd.DataFrame([{"날짜":"2026-01-03","시장":"US","티커/종목코드":"SPY","종가":101.0}])
        result = merge_non_null(old, new)
        self.assertEqual(len(result), 2)
        self.assertFalse(result.duplicated(KEYS).any())

if __name__ == "__main__":
    unittest.main()
