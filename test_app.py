import csv
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from app import Ledger, cents, csv_export

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.ledger=Ledger(Path(self.tmp.name)/"test.db")
    def tearDown(self): self.tmp.cleanup()
    def add(self, **kw):
        data=dict(day="2026-02-02",merchant="Coffee",category="Food",amount="4.10",note="")
        data.update(kw);return self.ledger.add(data)
    def test_money_is_exact(self):
        self.assertEqual(cents("0.29"),29)
        for bad in ("NaN","Infinity","-1","0","1.001","1e100","x"):
            with self.assertRaises(ValueError): cents(bad)
    def test_month_boundaries_budget_and_delete(self):
        item=self.add();self.add(day="2026-03-01",amount="99")
        self.ledger.budget("2026-02","100")
        r=self.ledger.report("2026-02",today=date(2026,2,14))
        self.assertEqual((r['total'],r['remaining'],len(r['daily'])),(410,9590,28))
        self.assertEqual(r['projection'],820)
        self.assertTrue(self.ledger.remove(item));self.assertFalse(self.ledger.remove(item))
    def test_invalid_fields(self):
        for kw in (dict(day="2026-02-30"),dict(merchant=""),dict(category="Invalid")):
            with self.assertRaises(ValueError):self.add(**kw)
    def test_export_escapes_formulas_and_quotes(self):
        self.add(merchant='=1+1',note='comma, and "quote"')
        rows=list(csv.reader(io.StringIO(csv_export(self.ledger.report('2026-02')).decode())))
        self.assertEqual(rows[1][1],"'=1+1");self.assertEqual(rows[1][-1],'comma, and "quote"')
    def test_persistence(self):
        self.add();other=Ledger(self.ledger.path);self.assertEqual(other.report('2026-02')['total'],410)
    def test_demo_never_overwrites(self):
        self.ledger.demo()
        with self.assertRaises(ValueError):self.ledger.demo()
if __name__=='__main__':unittest.main()
