# Makefile — shortcuts for working with the Telegram Agent in Docker

.PHONY: up down build logs shell restart clean watch

## Start agent (build if needed)
up:
	docker compose up --build -d
	@echo "Agent running. Follow logs with: make logs"

## Stop agent
down:
	docker compose down

## Force rebuild image and restart
build:
	docker compose build --no-cache
	docker compose up -d

## Follow agent logs
logs:
	docker compose logs -f agent

## Open a shell inside the running agent container
shell:
	docker compose exec agent bash

## Restart agent without rebuilding
restart:
	docker compose restart agent

## Start with Watchtower for auto-reload on rebuild
watch:
	docker compose --profile watch up --build -d

## Remove containers, volumes, and cached image
clean:
	docker compose down -v --rmi local
	@echo "Cleaned up."

## Show agent status
status:
	docker compose ps
	@echo ""
	docker compose exec agent python3 -c "from tool_registry import ToolRegistry; r=ToolRegistry(); r.scan(); print(r.summary())"
