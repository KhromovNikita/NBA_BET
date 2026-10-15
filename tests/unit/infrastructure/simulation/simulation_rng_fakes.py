"""Explicit count/side scripts; multinomial mass is placed at the supplied PMF mode."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nba_live_totals.infrastructure.simulation.numpy_engine import FloatArray, IntArray


class ScriptedSimulationRng:
    def __init__(
        self, counts: tuple[tuple[int, ...], ...], bits: tuple[tuple[int, ...], ...] | None = None
    ) -> None:
        self._counts = iter(counts)
        self._bits = iter(bits) if bits is not None else None
        self.calls: list[str] = []
        self.sizes: list[int] = []
        self.lambdas: list[float] = []
        self._scoring_calls = 0

    def poisson(self, lam: float, size: int) -> IntArray:
        import numpy as np

        self.calls.append("poisson")
        self.sizes.append(size)
        self.lambdas.append(lam)
        values = next(self._counts)
        assert len(values) == size, "wrong active-path population"
        return np.array(values, dtype=np.int64)

    def first_side_bits(self, size: int) -> IntArray:
        import numpy as np

        self.calls.append("bits")
        values = next(self._bits) if self._bits is not None else (1,) * size
        assert len(values) == size, "wrong admitted-path population"
        return np.array(values, dtype=np.int64)

    def multinomial(self, n: IntArray, pvals: FloatArray) -> IntArray:
        import numpy as np

        self.calls.append("home" if self._scoring_calls % 2 == 0 else "away")
        self._scoring_calls += 1
        matrix = np.zeros((len(n), len(pvals)), dtype=np.int64)
        matrix[:, int(np.argmax(pvals))] = n
        return matrix


class MonotoneSimulationRng(ScriptedSimulationRng):
    def __init__(self) -> None:
        super().__init__(())
        self._next = 0

    def poisson(self, lam: float, size: int) -> IntArray:
        import numpy as np

        self.calls.append("poisson")
        self.sizes.append(size)
        values = np.arange(self._next, self._next + size, dtype=np.int64)
        self._next += size
        return values
