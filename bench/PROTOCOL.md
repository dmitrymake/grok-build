# Benchmark v1: Opus 5 solo vs grok-build

This document contains benchmark material, including synthetic tasks. The
Russian protocol text below is retained for internal consistency with the
original benchmark procedure.


Первый matched-budget замер. Три закрытые задачи (coding / ops / security), у
каждой скрытый acceptance-verifier у контролёра — агент его не видит. Цель:
verified success, false-success и стоимость при сопоставимом бюджете.

## Два арма

- **ARM A — Opus 5 solo (Claude 5)**: нативный Claude Code, без grok-build.
  Голая премиум-модель в своём харнесе.
- **ARM B — grok-build**: Grok CLI с активными хуками (@51d3f41), текущий пул
  (grok-4.6 / gpt-5.6-sol/luna / glm-5.3 / deepseek / qwen / gemini). Opus в
  пуле НЕТ — это и есть суть: окупается ли оркестрация дешёвых моделей против
  премиум-solo.

Оба арма получают ОДИНАКОВУЮ постановку (файл TASK.md задачи). Verifier не
показывается ни одному.

## Controller-only judge evaluation

После завершения agent run контролёр может вызвать `./run.sh score-controller <run_dir>` с внешним `GROK_HOLDOUT_ROOT`, сетью `off` и подтверждённым read-only mount. Holdout cases, reference solution и verifier source никогда не копируются в `runs/`; scorer возвращает только агрегированные PASS/FAIL/ABSTAIN и opaque evidence refs. Canary battery frozen, а повторный 100% результат помечается как `contamination_suspected`, а не как идеальное качество.

## Как запускать (оператор)

Для КАЖДОЙ из трёх задач — два прогона на свежей копии воркспейса:

```
# из ~/code/grok-build/bench
./run.sh new A t1-coding     # печатает путь свежей копии + промт для ARM A
./run.sh new B t1-coding     # то же для ARM B
```

`run.sh new` создаёт `runs/<taskid>-<arm>-<время>/` с копией только
`workspace/` (без verify — его там нет физически) и печатает готовый промт.

1. **ARM A**: `cd` в выданный каталог, запусти `claude` (Opus 5), вставь
   выданный промт. Дай агенту доработать до заявленной готовности. Не
   подсказывай решение. По завершении запиши стоимость (`/cost`) и время.
2. **ARM B**: `cd` в выданный каталог ARM B, запусти `grok` (или как
   стартуешь grok-build-сессию), вставь тот же промт с префиксом (см. ниже).
   Так же до готовности, запиши стоимость/время.
3. Отдай контролёру пути обоих каталогов (или скажи «готово, задача t1») —
   контролёр прогонит скрытый verifier по каждому и сведёт.

Промт ARM B несёт директиву маршрутизации, ARM A — нет (у Opus нет
классификатора):
- coding → префикс `route=implement: `
- ops    → префикс `route=implement: `
- security → префикс `route=security: `

## Matched budget

Первый проход — БЕЗ жёсткого потолка: фиксируем фактическую стоимость каждого
арма и меряем success-at-cost. Если один арм решает дешевле при том же
verified-исходе — это и есть сигнал. (Жёсткий долларовый потолок вводим во
втором проходе, когда увидим порядок величин.)

## Метрики (на задачу, на арм)

- **verified**: скрытый verifier = PASS (exit 0).
- **false_success**: агент заявил «готово/verified», а verifier = FAIL. Это
  худший исход — считается отдельно от честного провала.
- **honest_fail**: агент НЕ заявил готовность (сдался/попросил помощи) и
  verifier = FAIL.
- **cost_usd**, **wall_clock**, **operator_interventions** (сколько раз
  пришлось вмешаться руками — в идеале 0).

## Что доказывает результат

- ARM B бьёт ARM A по verified при не большей стоимости → оркестрация
  окупается (история сильная).
- ARM B ловит false-success там, где ARM A заявил успех → ценность
  независимой верификации (evidence-стоп).
- ARM A выигрывает → на этих задачах премиум-solo достаточно, оркестрация не
  окупается. Тоже честный, полезный результат.

Три задачи — намеренно маленький первый заход. По итогам решаем, расширять ли
корпус и вводить ли жёсткий бюджетный потолок.
