"""Tests for vector_utils.cosine_similarity."""

from __future__ import annotations

import pytest

from vector_utils import cosine_similarity


class TestCosineSimilarity:
    def test_identical_vectors(self):
        assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self):
        assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0

    def test_arbitrary_vectors(self):
        a = [1, 2, 3]
        b = [4, 5, 6]
        # manually: dot=32, |a|=sqrt(14), |b|=sqrt(77)
        expected = 32 / (14**0.5 * 77**0.5)
        assert cosine_similarity(a, b) == pytest.approx(expected)
