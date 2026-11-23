demo:
	docker compose up --build -d --wait

eval:
	uv run --frozen python -m scripts.evaluate

eval-local:
	uv run --frozen python -m scripts.evaluate --mode local --version v1 --provisional-priority --judge --output results/baseline.json
	uv run --frozen python -m scripts.evaluate --mode local --version v2 --provisional-priority --judge --output results/local/current.json
	uv run --frozen python -m scripts.gate results/baseline.json results/local/current.json results/local/gate.md

test:
	uv run --frozen python -m scripts.prepare_tokenizers
	uv run --frozen python -m pytest -q

rollback:
	uv run --frozen python -m scripts.rollout 0
