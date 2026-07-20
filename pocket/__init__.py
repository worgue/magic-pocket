"""Deploy system for Django projects."""

from importlib.metadata import version

from pocket.naming import (
    NEON_DATABASE_URL,
    TIDB_DATABASE_URL,
    UPSTASH_REDIS_URL,
    ecr_image_tag,
    ecr_repo_name,
    pocket_key,
    stored_user_secret_name,
    user_secret_path,
)

__version__ = version("magic-pocket")

__all__ = [
    "NEON_DATABASE_URL",
    "TIDB_DATABASE_URL",
    "UPSTASH_REDIS_URL",
    "ecr_image_tag",
    "ecr_repo_name",
    "pocket_key",
    "stored_user_secret_name",
    "user_secret_path",
    "__version__",
]
