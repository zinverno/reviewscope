"""Local delivery evidence. Positions are one-based logical records within the input SHA."""

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

VERSION = "canonical-preview-1"
MAX_BYTES = 4 * 1024**2
MAX_CONFIG = 32 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 64
MAX_FIELD = 16_384
MAX_DEPTH = 8

# Messages can be logged verbatim. Never interpolate source values into them.
MESSAGES = {
    "empty_input": "В поставке нет записей.",
    "over_limit": "Превышен предел ресурсов профиля импорта.",
    "invalid_encoding": "Файл не соответствует выбранной кодировке.",
    "invalid_csv": "Нарушена структура CSV.",
    "invalid_json": "Нарушена структура JSON или кодировка значения.",
    "invalid_shape": "Ожидается массив или документ с единственным массивом reviews.",
    "duplicate_key": "Имя поля JSON встречается несколько раз.",
    "duplicate_header": "Имя столбца CSV повторяется.",
    "invalid_header": "Заголовок CSV содержит пустое имя.",
    "invalid_mapping": "Профиль сопоставления не соответствует контракту.",
    "conflicting_mapping": "Несколько полей назначены одному полю результата.",
    "unknown_column": "Указанное в профиле поле отсутствует в поставке.",
    "unexpected_column": "Поле отсутствует в явном профиле сопоставления.",
    "invalid_passport": "Параметры паспорта отсутствуют или недопустимы.",
    "invalid_format": "Формат поставки не поддерживается.",
    "invalid_json_item": "Элемент массива не является объектом.",
    "invalid_field_type": "Тип поля не соответствует контракту строки.",
    "row_width": "Число ячеек отличается от числа заголовков.",
    "missing_source": "Источник не указан.",
    "missing_place": "Точка источника не указана.",
    "binding_mismatch": "Привязка строки противоречит паспорту.",
    "observation_mismatch": "Время наблюдения строки расходится с паспортом.",
    "conflicting_id": "У одной идентичности в поставке разные данные.",
    "confirmed_duplicate": "Подтверждён точный повтор записи в поставке.",
    "missing_review_id": "Внешний ID отсутствует.",
    "missing_reviewer": "ID автора отсутствует.",
    "missing_text": "Пригодный для анализа текст отсутствует.",
    "missing_date": "Дата события отсутствует.",
    "invalid_date": "Дата или точность недопустима для профиля.",
    "unknown_timezone": "Смещение локального времени неизвестно.",
    "ambiguous_time": "Местное время соответствует двум моментам.",
    "nonexistent_time": "Местное время пропущено при смене смещения.",
    "ineligible_event_basis": "Смысл даты не позволяет сравнивать публикации или отправки.",
    "missing_rating": "Оценка отсутствует.",
    "invalid_rating": "Оценка некорректна для объявленной шкалы.",
    "unmapped_scale": "Переход к звёздам 1–5 не установлен.",
    "invalid_url": "Ссылка не соответствует разрешённой форме.",
    "private_output": "Нужен закрытый каталог вывода в validation_data/private.",
    "io_error": "Локальный файл недоступен для операции.",
    "invalid_arguments": "Недопустимые аргументы команды.",
}


class PreviewError(ValueError):
    def __init__(self, code: str):
        super().__init__(MESSAGES[code])
        self.code = code


@dataclass(frozen=True, slots=True)
class EventTime:
    kind: Literal["missing", "invalid", "date", "instant", "local"]
    raw: str | None = None
    value: str | None = None  # ISO date, UTC instant, or unresolved local timestamp
    precision: str | None = None
    offset: str | None = None  # literal source offset, including Z


@dataclass(slots=True)
class CanonicalRow:
    source_position: int
    status: Literal["accepted", "quarantined", "confirmed_duplicate"] = "accepted"
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    related_position: int | None = None
    source: str | None = None
    source_place_id: str | None = None
    source_review_id: str | None = None
    reviewer_id: str | None = None
    text: str | None = None
    rating_raw: str | bool | None = None
    rating_1_to_5: int | None = None
    event_basis: str = "unknown"
    event: EventTime = field(default_factory=lambda: EventTime("missing"))
    source_url: str | None = None
    field_provenance: dict[str, str] = field(default_factory=dict)

    def quarantine(self, code: str):
        self.status = "quarantined"
        if code not in self.reasons:
            self.reasons.append(code)

    @property
    def eligible_for(self) -> dict[str, bool]:
        accepted = self.status == "accepted"
        return {
            "text_analysis": accepted and bool(self.text and self.text.strip()),
            "rating": accepted and self.rating_1_to_5 is not None,
            "dated_comparison": accepted
            and self.event.kind in {"instant", "date"}
            and self.event_basis in {"published", "submitted"},
            "reviewer_features": accepted and self.reviewer_id is not None,
        }


@dataclass(slots=True)
class ImportSummary:
    total_input_rows: int
    accepted: int
    quarantined: int
    confirmed_duplicates: int
    warning_count: int
    missing_author_count: int
    missing_date_count: int
    missing_text_count: int
    missing_review_id_count: int
    missing_rating_count: int
    rating_excluded_count: int
    reason_counts: dict[str, int]

    @classmethod
    def count(cls, rows: list[CanonicalRow]):
        statuses = Counter(r.status for r in rows)
        reasons = Counter(code for r in rows for code in (*r.warnings, *r.reasons))
        accepted = [r for r in rows if r.status == "accepted"]
        result = cls(
            len(rows),
            len(accepted),
            statuses["quarantined"],
            statuses["confirmed_duplicate"],
            sum(len(r.warnings) for r in rows),
            sum(r.reviewer_id is None for r in accepted),
            sum(r.event.kind == "missing" for r in accepted),
            sum(not (r.text and r.text.strip()) for r in accepted),
            sum(r.source_review_id is None for r in accepted),
            sum("missing_rating" in r.warnings for r in accepted),
            sum(r.rating_1_to_5 is None for r in accepted),
            dict(sorted(reasons.items())),
        )
        assert (
            result.total_input_rows
            == result.accepted + result.quarantined + result.confirmed_duplicates
        )
        return result
