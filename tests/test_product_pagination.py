import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jd_product_pipeline as pipeline


def response(identifiers, total, page=1):
    return {"data": [{"productId": identifier} for identifier in identifiers], "paginationData": {"totalItems": total, "currentPage": page}}


class ProductPaginationTests(unittest.TestCase):
    def test_complete_scan_requests_stable_sort_and_preserves_filters(self):
        with patch.object(pipeline.jingmai, "get", side_effect=[response([11, 12], 3), response([13], 3, 2)]) as request:
            self.assertEqual([row["productId"] for row in pipeline.list_products(31, "item")], [11, 12, 13])
        for call in request.call_args_list:
            params = call.args[2]
            self.assertEqual(params.get("sortBy"), "created")
            self.assertEqual(params.get("sortType"), "asc")
            self.assertEqual(params["categoryIdList"], [31])
            self.assertEqual(params["itemNum"], "item")

    def test_duplicate_product_is_not_counted_as_complete(self):
        with patch.object(pipeline.jingmai, "get", side_effect=[response([11, 12], 3), response([12], 3, 2)]):
            with self.assertRaises(pipeline.PipelineError):
                pipeline.list_products()

    def test_premature_empty_page_stops(self):
        with patch.object(pipeline.jingmai, "get", side_effect=[response([11], 3), response([], 3, 2)]):
            with self.assertRaises(pipeline.PipelineError):
                pipeline.list_products()

    def test_changing_total_stops(self):
        with patch.object(pipeline.jingmai, "get", side_effect=[response([11], 2), response([12, 13], 3, 2)]):
            with self.assertRaises(pipeline.PipelineError):
                pipeline.list_products()

    def test_missing_pagination_is_not_success(self):
        with patch.object(pipeline.jingmai, "get", return_value={"data": [{"productId": 11}]}):
            with self.assertRaises(pipeline.PipelineError):
                pipeline.list_products()

    def test_confirmed_empty_result_is_valid(self):
        with patch.object(pipeline.jingmai, "get", return_value=response([], 0)):
            self.assertEqual(pipeline.list_products(), [])

    def test_confirmed_empty_result_without_current_page_is_valid(self):
        empty = {"success": True, "data": None, "paginationData": {"totalItems": 0}}
        with patch.object(pipeline.jingmai, "get", return_value=empty):
            self.assertEqual(pipeline.list_products(item_num="TB123"), [])


if __name__ == "__main__":
    unittest.main()
