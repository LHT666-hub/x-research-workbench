"""Avoid rescanning an append-only page accumulator at every checkpoint."""


class RowDeltaTracker:
    """One owned append-only list; a new list (e.g. retry) is scanned in full.

    Call acknowledge only after the checkpoint is durable. Empty new tails must
    still persist page/cursor metadata; this class never decides when to save.
    """
    def __init__(self):
        self._rows = None
        self._count = 0

    def pending(self, rows):
        if not isinstance(rows, list):
            raise TypeError("Checkpoint accumulator must be a list")
        if rows is not self._rows:
            return rows
        if len(rows) < self._count:
            raise ValueError("Append-only checkpoint rows shrank")
        return rows[self._count:]

    def acknowledge(self, rows):
        self._rows, self._count = rows, len(rows)
