"""Unit tests for the specificity score (SPEC.md §17)."""

from __future__ import annotations

from reviewscope.analysis.specificity import specificity_score


class TestSpecificity:
    def test_detailed_review_scores_high(self) -> None:
        text = (
            "Ждали столик 25 минут в субботу вечером, но борщ и домашние "
            "котлеты стоили ожидания. Принесли первое через 40 минут, "
            "официантка Мария извинилась и предложила десерт в подарок. "
            "Заказ на двоих обошёлся в 2600 рублей — справедливо за такое качество."
        )
        result = specificity_score(text)
        assert result.name == "Specificity"
        assert result.value >= 60, result
        assert result.signals

    def test_generic_review_scores_low(self) -> None:
        text = "Всё понравилось, рекомендую всем! Супер!"
        result = specificity_score(text)
        assert result.value <= 55, result
        assert result.counter_signals

    def test_empty_text_is_zero(self) -> None:
        result = specificity_score("   ")
        assert result.value == 0.0
        assert result.confidence.value == "LOW"

    def test_short_text_penalized(self) -> None:
        result = specificity_score("Отлично.")
        assert result.value < 50

    def test_bounded_0_100(self) -> None:
        tiny = specificity_score("т")
        detailed = specificity_score(
            "Пришли в четверг в 18:00, очередь была 3 человека, взяли "
            "капучино на овсяном молоке за 350 рублей, бариста Виктор "
            "посоветовал кенийский эспрессо, пенка была плотной, "
            "выпечка свежая, телефон зарядили на ресепшене перед уходом."
        )
        assert 0 <= tiny.value <= 100
        assert 0 <= detailed.value <= 100
        assert detailed.value >= tiny.value

    def test_specific_review_edges_above_generic(self) -> None:
        generic = specificity_score("Отлично, супер, рекомендую")
        specific = specificity_score("Пришлось ждать 5 минут, эспрессо горький, попросил заменить, замена заняла 2 минуты")
        assert specific.value > generic.value
