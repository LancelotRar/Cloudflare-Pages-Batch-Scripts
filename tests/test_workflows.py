"""Tests for cf_pages_batch_scripts.workflows"""

from cf_pages_batch_scripts.workflows import parse_selection


class TestParseSelection:
    """Test parse_selection for project/KV selection."""

    def make_items(self, n: int) -> list[dict]:
        return [{"index": i, "name": f"item{i}"} for i in range(1, n + 1)]

    def test_select_single(self):
        items = self.make_items(5)
        result = parse_selection("3", items)
        assert len(result) == 1
        assert result[0]["index"] == 3

    def test_select_multiple_comma(self):
        items = self.make_items(5)
        result = parse_selection("1,3,5", items)
        assert len(result) == 3
        assert [r["index"] for r in result] == [1, 3, 5]

    def test_select_range(self):
        items = self.make_items(5)
        result = parse_selection("2-4", items)
        assert len(result) == 3
        assert [r["index"] for r in result] == [2, 3, 4]

    def test_select_all(self):
        items = self.make_items(3)
        result = parse_selection("A", items)
        assert len(result) == 3

    def test_select_all_lowercase(self):
        items = self.make_items(2)
        result = parse_selection("a", items)
        assert len(result) == 2

    def test_dedup(self):
        items = self.make_items(5)
        result = parse_selection("1,1,1", items)
        assert len(result) == 1

    def test_mixed_format(self):
        items = self.make_items(10)
        result = parse_selection("1,3-5,7", items)
        assert len(result) == 5
        assert [r["index"] for r in result] == [1, 3, 4, 5, 7]

    def test_invalid_number_skipped(self):
        items = self.make_items(3)
        result = parse_selection("abc,2", items)
        assert len(result) == 1
        assert result[0]["index"] == 2

    def test_out_of_range_skipped(self):
        items = self.make_items(3)
        result = parse_selection("1,99", items)
        assert len(result) == 1
        assert result[0]["index"] == 1

    def test_range_ordering(self):
        items = self.make_items(5)
        result = parse_selection("4-2", items)
        assert len(result) == 3
        assert [r["index"] for r in result] == [2, 3, 4]

    def test_empty_string(self):
        items = self.make_items(3)
        result = parse_selection("", items)
        assert result == []

    def test_whitespace_handling(self):
        items = self.make_items(5)
        result = parse_selection(" 1 , 3 ", items)
        assert len(result) == 2
