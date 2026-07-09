"""add user_profile table

The application-facing identity a form asks for (name, email, phone, city,
country, profile links), separate from the auth-ish `users` row. Column names
match the frozen LOGICAL_FIELDS vocabulary in coverai/browser_apply.py so the
submission-packet producer copies them across without renaming. One row per
user: user_id is both primary key and a FK to users.id.

The live coverai.db also gets this table at startup via storage.init_db()'s
CREATE TABLE IF NOT EXISTS, so this migration exists for tracked history and
for fresh databases built through Alembic.

Revision ID: a1b2c3d4e5f6
Revises: 69f48f441dc6
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '69f48f441dc6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'user_profile',
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('first_name', sa.String(), server_default='', nullable=False),
        sa.Column('last_name', sa.String(), server_default='', nullable=False),
        sa.Column('email', sa.String(), server_default='', nullable=False),
        sa.Column('phone', sa.String(), server_default='', nullable=False),
        sa.Column('location_city', sa.String(), server_default='', nullable=False),
        sa.Column('location_country', sa.String(), server_default='', nullable=False),
        sa.Column('linkedin_url', sa.String(), server_default='', nullable=False),
        sa.Column('portfolio_url', sa.String(), server_default='', nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('user_profile')
