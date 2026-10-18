"""Core wallet logic: smoothing camera predictions and tracking the bill stack.

No network or hardware here, so it can be tested on its own.
"""

NONE = "none"
NOTES = ("1", "20", "ariary")
STATES = (NONE,) + NOTES


class Debouncer:
    """Turns a noisy stream of per-frame labels into clean state changes.

    A label only becomes the new state after it has been seen `frames` times in
    a row with at least `min_conf` confidence. Any frame with a different label
    or lower confidence breaks the streak and the count starts over. This
    filters out the in-between frames while a bill is sliding in or out.
    """

    def __init__(self, frames=6, min_conf=0.7):
        self.frames = frames
        self.min_conf = min_conf
        self.candidate = None
        self.count = 0
        self.stable = None

    def feed(self, label, conf):
        """Returns the new stable state if it just changed, otherwise None."""
        if label not in STATES or conf < self.min_conf:
            self.candidate, self.count = None, 0     # streak broken
            return None
        if label == self.candidate:
            self.count += 1
        else:
            self.candidate, self.count = label, 1
        if self.count >= self.frames and label != self.stable:
            self.stable = label
            return label
        return None

    def force(self, label):
        """Used by demo controls: set the state directly."""
        self.candidate, self.count, self.stable = label, self.frames, label


class Wallet:
    """Remembers the stack of notes (bottom ... top) and decides in vs out.

    Rules on a state change:
    - new state is "none" while notes are in the wallet -> everything was
      taken out at once.
    - new state equals the note *below* the current top -> the top note was
      taken out.
    - anything else -> a new note was put in.
    """

    def __init__(self):
        self.stack = []

    @property
    def top(self):
        return self.stack[-1] if self.stack else NONE

    @property
    def below_top(self):
        return self.stack[-2] if len(self.stack) >= 2 else NONE

    def on_state(self, new_state):
        """Returns ("in" | "out", [notes]), or None if nothing happened.

        The list holds one note, except when the wallet is emptied at once:
        then it holds every note that was in it (top first).
        """
        if new_state == self.top:
            return None
        if self.stack and new_state == self.below_top:
            return ("out", [self.stack.pop()])
        if new_state == NONE:
            removed = list(reversed(self.stack))
            self.stack = []
            return ("out", removed)
        self.stack.append(new_state)
        return ("in", [new_state])

    def reset(self):
        self.stack = []
