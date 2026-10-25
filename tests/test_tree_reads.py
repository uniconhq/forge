"""A tree of blocks is walked once, the same way, by every caller."""

from forge.domain.blocks.tree_reads import leaves, reached


def test_leaves_yields_a_value_inside_a_block_and_can_stop_at_one_block() -> None:
    best = {"block": "best_each", "from": {"results": "islands"}, "value": {"name": "score"}}
    tree = {"block": "add", "of": [{"value": "total"}, best]}

    assert {"value": "total"} in list(leaves(tree))
    assert best not in list(leaves(tree))
    assert best in list(leaves(tree, into=lambda block: block["block"] != "best_each"))


def test_reached_includes_the_start_and_follows_each_edge_once() -> None:
    edges = {"a": ["b"], "b": ["a", "c"], "c": []}

    assert reached(["a"], edges) == {"a", "b", "c"}
    assert reached(["c"], edges) == {"c"}
    assert reached([], edges) == set()
