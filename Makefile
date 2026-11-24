demo:
	docker compose up --build -d --wait

eval:
	uv run --frozen python -m scripts.evaluate --role candidate --provisional-priority --output .local/fixture-eval.json

eval-local:
	uv run --frozen python -m scripts.evaluate --mode local --role stable --provisional-priority --judge --output results/baseline.json --outputs 'results/local/{version}-outputs.json'
	uv run --frozen python -m scripts.evaluate --mode local --role candidate --provisional-priority --judge --output results/local/current.json --outputs 'results/local/{version}-outputs.json'
	uv run --frozen python -m scripts.gate results/baseline.json results/local/current.json results/local/gate.md

test:
	uv run --frozen python -m scripts.prepare_tokenizers
	uv run --frozen python -m pytest -q

rollback:
	uv run --frozen python -m scripts.rollout 0
