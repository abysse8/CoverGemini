"""add interview_questions table

Likely interview questions per offer, in the Helene->Camille seam: Helene
(browser) collects them from job boards; Camille (coach) drafts a job-specific
suggested_answer with AI; the user refines it. Kept separate from
application_questions (which are form fields to submit) -- different lifecycle,
different owner.

The live coverai.db also gets this table at startup via storage.init_db()'s
CREATE TABLE IF NOT EXISTS; this migration exists for tracked history and for
fresh databases built through Alembic.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'interview_questions',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), server_default='julien', nullable=False),
        sa.Column('offer_id', sa.String(), nullable=False),
        sa.Column('category', sa.String(), server_default='general', nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('source', sa.String(), server_default='unknown', nullable=False),
        sa.Column('suggested_answer', sa.Text(), server_default='', nullable=False),
        sa.Column('answer', sa.Text(), server_default='', nullable=False),
        sa.Column('answer_source', sa.String(), server_default='unknown', nullable=False),
        sa.Column('confidence', sa.Integer(), server_default='0', nullable=False),
        sa.Column('status', sa.String(), server_default='collected', nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['offer_id'], ['offers.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('interview_questions', schema=None) as batch_op:
        batch_op.create_index('idx_interview_questions_offer', ['offer_id', 'status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('interview_questions', schema=None) as batch_op:
        batch_op.drop_index('idx_interview_questions_offer')
    op.drop_table('interview_questions')
