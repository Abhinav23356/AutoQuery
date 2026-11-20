"""Focused validation tests; run with `python -m unittest discover -s tests`."""
import os, unittest
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
from backend.agent.sql_guard import validate_query
from backend.agent.chart_guard import chart_config
generate_bar_chart=lambda data,title,x,y:chart_config("bar",data,title,x,y)


class SqlGuardTests(unittest.TestCase):
    def test_select_from_allowed_relation(self):
        self.assertEqual(validate_query("SELECT region, COUNT(*) FROM customers GROUP BY region"), "SELECT region, COUNT(*) FROM customers GROUP BY region")
    def test_rejects_mutation_and_multiple_statements(self):
        for sql in ("DELETE FROM orders", "SELECT 1; DROP TABLE orders"):
            with self.assertRaises(ValueError): validate_query(sql)
    def test_rejects_unlisted_and_qualified_relations(self):
        for sql in ("SELECT * FROM secrets", "SELECT * FROM private.customers"):
            with self.assertRaises(ValueError): validate_query(sql)

class ChartGuardTests(unittest.TestCase):
    def test_returns_recharts_configuration(self):
        chart=generate_bar_chart([{"month":"Jan","revenue":12}],"Revenue","month","revenue")
        self.assertEqual(chart["type"],"bar"); self.assertEqual(chart["data"][0]["revenue"],12)
    def test_keeps_all_requested_chart_series(self):
        rows=[{"month":"Jan","East":10,"West":14}]
        chart=chart_config("bar",rows,"Monthly revenue by region","month","East",["East","West"])
        self.assertEqual(chart["series"],["East","West"])
    def test_rejects_missing_or_nonnumeric_series(self):
        with self.assertRaises(ValueError): chart_config("bar",[{"month":"Jan","East":10}],"Revenue","month","East",["East","West"])
        with self.assertRaises(ValueError): chart_config("bar",[{"month":"Jan","East":10,"West":"14"}],"Revenue","month","East",["East","West"])
    def test_rejects_empty_or_non_numeric_data(self):
        with self.assertRaises(ValueError): generate_bar_chart([],"Revenue","month","revenue")
        with self.assertRaises(ValueError): generate_bar_chart([{"month":"Jan","revenue":"12"}],"Revenue","month","revenue")

if __name__=="__main__": unittest.main()
