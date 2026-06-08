docs:
    uv run zensical serve -a 0.0.0.0:8080

test:
    uv run pytest
    cargo test
