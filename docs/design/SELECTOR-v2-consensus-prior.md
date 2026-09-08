# Selector v2: artifact-consensus prior + supply/selection split + oracle-regret eval

Статус: принято архитектурно (контролёр, 2026-09-01), реализация — в portfolio-стадии
(ПОСЛЕ Stage-5 и бенчмарка v1). Источник: research-апдейт Sol (три работы: candidate
supply/answer selection 2026-08-26; JudgePanel-14B 2026-09-01; COVER coalition
routing). Числа ниже — из пересказа Sol; при имплементации свериться с
первоисточниками. Занести в repo docs/design/ следующим executor-прогоном
(НЕ коммитить в master, пока batch/fixes-stage5 в полёте).

## 1. Схема селектора меняется

Было (WP-6): hard evidence -> pairwise judge -> winner.
Станет:

    hard evidence (лексикографический прецеденс, как есть)
        v
    artifact equivalence / behavioral clustering
        v
    consensus prior (support кластера)  +  pairwise artifact judge
        v
    selector

Эмпирика: чистый judge-ranking хуже гибрида консенсус+judge (63.82% ->
~70.9% на 81k pools / 16k задач). Judge ценен, когда правильный кандидат в
меньшинстве; вредит, когда правильный уже доминирует.

## 2. Как это сохраняет наши инварианты (ключевое)

- Consensus у нас — НЕ голосование моделей. Кластеризация АРТЕФАКТОВ по
  поведенческой эквивалентности: нормализованный diff, одинаковые исходы
  hard-verifiers/acceptance, одна архитектурная гипотеза. Support = свойство
  множества артефактов -> artifact-derived signal, model identity по-прежнему
  не существует ни для судьи, ни для селектора. Identity-leak инварианту
  ничего не грозит.
- Кластеризация — чистая функция (§4): вход — canonical bundles, выход —
  разбиение + support. Без I/O/спавна/state.
- Лексикографика сохраняется: hard evidence > всё. Consensus — prior, не
  hard-гейт: перебить большой support может только (а) hard evidence
  (discriminating test, который проходит лишь minority-кандидат — это уже
  hard-слой) или (б) единогласный reversed-order judge с высокой confidence.
- Анти-паттерн «judge портит доминирующий консенсус» закрывается нашим же
  принципом «0 сравнений, если хватило»: при доминирующем support и
  отсутствии hard-различий judge НЕ вызывается вовсе (плюс экономия).
  Pairwise гоняется между ПРЕДСТАВИТЕЛЯМИ кластеров, не всеми парами.
- Порог «когда judge может перебить support» — data-dependent (cold start,
  как frontier-коэффициенты): стартовое правило + сбор в существующий
  judge-calibration-v1; обучение позже.

## 3. Новая метрика и атрибуция провалов

candidate_supply = доля задач, где портфель произвёл >=1 verified-acceptable
артефакт. Разделение:
- supply = 0 -> GENERATION failure (не вина селектора/судьи);
- supply > 0, выбран не лучший -> SELECTION failure.
Поля в escalation-eval-v1 / R1 run-record. Связка с frontier-escalation:
низкий candidate_supply — сигнал эскалации К ГЕНЕРАЦИИ (frontier как
мета-планер нового кандидата), а НЕ к более дорогому судейству: judge
бесполезен при крайне редком правильном кандидате (half-rise ~14.7%
correct-candidate availability в пересказе).

## 4. Eval селектора: oracle regret (COVER-фрейм)

End-to-end сравнение не изолирует качество routing/selection. Для selector-eval
фиксировать в run-record: candidate portfolio, downstream verifier stack,
judge versions, selection policy. Считать: portfolio oracle, actual selector,
oracle regret = oracle - actual, selector_capture = selector_gain/oracle_gain
(наша метрика — та же алгебра). Это ВТОРОЙ, изолирующий слой eval;
end-to-end бенчмарк «Opus solo vs grokBuild» он не заменяет и не блокирует
(в бенчмарке v1 селектора нет — портфель ещё не включён).

## 5. JudgePanel-14B — challenger watchlist

Специализированный 14B judge (deliberation traces панели; заявка: бьёт
judge-модели до 70B, хорошая position consistency, доучивается на сотнях
примеров). Весов/API пока нет -> прод-пул НЕ меняется (Luna primary +
Gemini independent -> frontier escalation). При появлении весов: прогнать
через наш identity-leak/position-bias калибратор ДО включения; свап =
конфиг-биндинг judge-primary (roles-primary делает это тривиальным).

## 6. Очерёдность (не менять)

Stage-5 (routing quality) -> верификация -> бенчмарк v1 (текущая система,
без портфеля) -> portfolio-стадия: селектор строится СРАЗУ по этой схеме
(clustering + consensus prior + judge + supply/selection метрики + COVER-eval).
