Инцидент: сожжённая сессия Telegram в GitHub Actions
Симптом
Run в workflow ChatSum scheduled summaries падает на подключении к Telegram (~за 20 секунд после старта):
❌ Не удалось подключиться к Telegram: The authorization key (session file) was used
under two different IP addresses simultaneously, and can no longer be used. Use the same
session exclusively, or use different sessions
(caused by InvokeWithLayerRequest(InitConnectionRequest(GetConfigRequest)))
Термин: StringSession — строка, в которой закодирован ключ входа в Telegram; лежит в секрете TELEGRAM_SESSION. Лидер — единственный Run, которому разрешено подключать Telegram. Standby — выжидающий Run без подключения, следит за флагом лидера в ветке state.
Причина
Telegram аннулировал ключ сессии, потому что её использовали с двух разных адресов одновременно. Строку нельзя оживить — нужна новая. Триггерные окна в коде (были закрыты 2026-10-14):
1. Гонка на старте. Два близких тика крона (или standby, объявивший живого лидера мёртвым из-за задержки чтения флага) поднимались лидерами и подключались почти одновременно. В коде между leader_claim(me) и telegram_client.start() не было паузы и перепроверки флага.
2. Уступка без отключения. Обнаружив более нового лидера, старый Run возвращал 'standby', но НЕ отключал Telegram. Два живых соединения с одной сессией — сжигание ключа.
3. Ручной прогон. run_manual (--chat-id) во время живой вахты подключался вторым соединением с той же сессией.
Исправление (коммит c2029dca)
- STARTUP_BARRIER_SEC = 15: пауза между leader_claim и telegram_client.start(), затем перечитывание флага.
- Новая чистая функция startup_should_yield(mine, remote): конкурент — свежий живой флаг лидера (leader_is_live), готовность ready НЕ требуется (стартующая заявка тоже конкурент). Порядок «новее побеждает»: по watch_started_utc, при равенстве — больший run_id. Все одновременные стартующие выбирают одного победителя → подключается ровно один.
- Проигравший на барьере уходит в standby без подключения.
- Проигравший после connect (поздний более новый лидер) делает явный telegram_client.disconnect() перед уходом в standby.
- run_manual: при handoff_enabled() и живом лидере (не retiring, leader_is_live) — отказ до connect.
- На ошибку "two different ip" печатается инструкция восстановления.
- Тест test_startup_election (14 проверок). Весь набор: ALL TESTS PASSED.
Восстановление (когда ключ уже сожжён)
1. Остановить ВСЕ потребители сессии: VPS-бот (scripts/stop_bot.sh, screen -ls, systemd), отменить висящие Runs (gh run list, gh run cancel), временно отключить крон: gh workflow disable <id>. Должен остаться ноль живых подключений.
2. Создать новую сессию только локально: ./venv/bin/python tools/gen_session.py (придёт код в Telegram; пишет telegram_session.txt, gitignored).
3. Залить секрет без печати значения: ./scripts/push_github_secrets.sh (печатает только имена и длины).
4. Включить крон: gh workflow enable <id>, запустить пробный прогон, убедиться, что в логе Заявляю лидерство → Дежурство засеяно (не Не удалось подключиться).
Профилактика — чек-лист для агента
- Одна сессия = ровно одно живое подключение. Никогда не держать VPS-бот и Actions одновременно (README §"Выбор площадки").
- Не запускать ручной --chat-id, пока идёт вахта.
- Любое окно, где возможны два connect с одной сессией, обязано закрываться disconnect у проигравшего.
- Изменения run_once.py/main.py/.github/workflows/ идут прямо в прод — прогонять ./venv/bin/python tests/test_run_once.py до пуша.
Побочный дефект, найденный тут же
Шаг Persist state падал (git checkout -B state origin/state → local changes would be overwritten), потому что refresh_runtime_files_best_effort во время рана перезаписывает SCHEDULE.txt из origin/main. Ветка state несёт только state.json — исправлено на git checkout -f -B state.
Наблюдение на будущее
После повторного включения workflow (gh workflow disable/enable) тики крона 7,19,33,46,58 возобновились не сразу — несколько слотов подряд не сработали. Пробный ручной --watch держит дежурство; проверить появление event=schedule в gh run list; при сохранении тишины — тоглать disable/enable ещё раз или пересоздать триггер.