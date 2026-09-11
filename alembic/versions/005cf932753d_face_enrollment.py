"""face_enrollment

Adds face_template, the one enrolled face embedding per pupil used to
confirm identity at a kiosk. Holds only the embedding vector a trusted
device computed at enrollment — never the source photo, which this system
never persists at all.

Revision ID: 005cf932753d
Revises: b2d84f61c937
Create Date: 2026-09-11 00:19:38.970029

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '005cf932753d'
down_revision: Union[str, None] = 'b2d84f61c937'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'facetemplate',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('embedding', sa.JSON(), nullable=False),
        sa.Column('model_version', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('enrolled_by', sa.Uuid(), nullable=False),
        sa.Column('enrolled_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['student.id']),
        sa.ForeignKeyConstraint(['enrolled_by'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id'),
    )


def downgrade() -> None:
    op.drop_table('facetemplate')
