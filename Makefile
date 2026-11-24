demo:
	docker compose build --build-arg GIT_SHA=$(shell git rev-parse HEAD) api
	docker compose up -d --wait

eval:
	uv run --frozen python -m scripts.prepare_tokenizers
	uv run --frozen python -m scripts.evaluate --mode local --role stable --provisional-priority --judge --output .local/eval/baseline.json --outputs .local/eval/stable-outputs.json

eval-fixture:
	uv run --frozen python -m scripts.prepare_tokenizers
	uv run --frozen python -m scripts.evaluate --role candidate --provisional-priority --output .local/fixture-eval.json

eval-local: eval
	uv run --frozen python -m scripts.evaluate --mode local --role candidate --provisional-priority --judge --output .local/eval/current.json --outputs .local/eval/candidate-outputs.json
	uv run --frozen python -m scripts.gate .local/eval/baseline.json .local/eval/current.json .local/eval/gate.md

test:
	uv run --frozen python -m scripts.prepare_tokenizers
	uv run --frozen python -m pytest -q

rollback:
	uv run --frozen python -m scripts.rollout 0
