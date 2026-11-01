.DEFAULT_GOAL := help
DC = docker compose
NODE ?= pg1
PORT ?= 5000
DURATION ?= 60
INTERVAL ?= 0.1
JOURNAL ?= latest

.PHONY: help setup up status sql ctl logs down check acceptance load load-report
help:
	@echo 'make up         				— собрать и запустить кластер'
	@echo 'make status     				— посмотреть роли узлов'
	@echo 'make sql        				— SQL через HAProxy к лидеру'
	@echo 'make sql PORT=5001 			— SQL через HAProxy к репликам'
	@echo 'make ctl ARGS="show-config" 	— команда patronictl'
	@echo 'make logs      				— логи всех сервисов'
	@echo 'make down       				— остановить, сохранив данные'
	@echo 'make acceptance 				— проверить отказы в отдельном временном кластере'
	@echo 'make load DURATION=60 		— непрерывная запись с журналом подтверждений'
	@echo 'make load-report 			— сверить последний журнал с текущим лидером'
	@echo 'При остановленном pg1 добавь NODE=pg2 или NODE=pg3.'

setup:
	python3 setup.py

up: setup
	$(DC) up --build --detach --wait --wait-timeout 180

status:
	$(DC) exec $(NODE) patronictl -c /etc/patroni.yaml list

sql:
	$(DC) exec -e LAB_SQL_PORT=$(PORT) $(NODE) sh -c 'PGPASSWORD="$$PATRONI_SUPERUSER_PASSWORD" exec psql -X -v ON_ERROR_STOP=1 -h haproxy -p "$$LAB_SQL_PORT" -U postgres -d postgres'

ctl:
	$(DC) exec $(NODE) patronictl -c /etc/patroni.yaml $(ARGS)

logs:
	$(DC) logs --follow --tail=50

down:
	$(DC) down

check: setup
	$(DC) config --quiet
	python3 -c "import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('.').glob('*.py')]"
	python3 -m unittest -v test_workload test_request_client

acceptance: setup
	python3 acceptance.py

load: setup
	python3 workload.py write --duration "$(DURATION)" --interval "$(INTERVAL)"

load-report:
	@python3 workload.py report "$(JOURNAL)"
