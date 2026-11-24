from src.models import audit, cases, handovers, reviews, runs, users  # Register the tables.
from src.models.base import metadata


def upgrade(connection):
    # Version 1 is additive and fully declarative.
    metadata.create_all(connection)
