docs:
    uv run zensical serve -a 0.0.0.0:8080

test *args:
    uv run pytest {{args}}
    cargo test
