"""Run with:  python test_wallet.py"""
from wallet import Debouncer, Wallet


def run(states):
    w, out = Wallet(), []
    for s in states:
        out.append(w.on_state(s))
    return w, out


def test_in_then_out():
    w, ev = run(["1", "20", "1", "none"])
    assert ev == [("in", ["1"]), ("in", ["20"]), ("out", ["20"]), ("out", ["1"])]
    assert w.stack == []


def test_ariary_in_middle():
    w, ev = run(["20", "ariary", "1", "ariary", "20"])
    assert ev == [("in", ["20"]), ("in", ["ariary"]), ("in", ["1"]),
                  ("out", ["1"]), ("out", ["ariary"])]
    assert w.stack == ["20"]


def test_new_note_not_matching_below_is_in():
    w, ev = run(["1", "20", "ariary"])          # ariary != 1 (below 20) -> came in
    assert ev[-1] == ("in", ["ariary"])
    assert w.stack == ["1", "20", "ariary"]


def test_none_empties_wallet():
    w, ev = run(["1", "20", "ariary", "none"])  # "none" with notes inside -> all taken out
    assert ev[-1] == ("out", ["ariary", "20", "1"])
    assert w.stack == []


def test_debouncer():
    d = Debouncer(frames=3, min_conf=0.7)
    seq = [("none", .9)] * 3 + [("20", .9), ("1", .9)] + [("20", .5)] * 5 + [("20", .9)] * 3
    out = [d.feed(l, c) for l, c in seq]
    assert [x for x in out if x] == ["none", "20"]


def test_six_in_a_row_strict():
    d = Debouncer(frames=6, min_conf=0.7)
    # 5 good, 1 low-confidence, 5 good -> never 6 in a row -> no change
    seq = [("20", .9)] * 5 + [("20", .4)] + [("20", .9)] * 5
    assert not any(d.feed(l, c) for l, c in seq)
    # one more good frame makes 6 in a row
    assert d.feed("20", .9) == "20"
    # 5 x "1" interrupted by a "20" -> no change
    seq = [("1", .9)] * 5 + [("20", .9)] + [("1", .9)] * 5
    assert not any(d.feed(l, c) for l, c in seq)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
