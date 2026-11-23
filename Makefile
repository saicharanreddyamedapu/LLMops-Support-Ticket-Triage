demo:
	docker compose up --build -d --wait

eval:
	uv run --frozen python -m scripts.evaluate

test:
	uv run --frozen python -m pytest -q

rollback:
	uv run --frozen python -m scripts.rollout 0
