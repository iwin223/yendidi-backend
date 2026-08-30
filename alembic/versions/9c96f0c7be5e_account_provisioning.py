"""account_provisioning

Adds the tables backing account provisioning, the vendor review queue,
favourites and idempotent account creation:
docs/SPEC_ACCOUNT_PROVISIONING.md, docs/BACKEND_HANDOVER.md §3-4.

Revision ID: 9c96f0c7be5e
Revises: 89b31585d79e
Create Date: 2026-08-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '9c96f0c7be5e'
down_revision: Union[str, None] = '89b31585d79e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'favorite',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('target_type', sa.Enum('menu_item', 'vendor', name='favoritetargettype'), nullable=False),
        sa.Column('target_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['student.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'target_type', 'target_id', name='uq_favorite_target'),
    )

    op.create_table(
        'invitation',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_invitation_token_hash'), 'invitation', ['token_hash'], unique=True)

    op.create_table(
        'vendorsubmission',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('school_id', sa.Uuid(), nullable=False),
        sa.Column('submitted_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('submitted_by_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('business_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('owner_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('phone', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('email', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('opens_at_minutes', sa.Integer(), nullable=False),
        sa.Column('closes_at_minutes', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('pending', 'approved', 'rejected', name='vendorsubmissionstatus'), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), nullable=False),
        sa.Column('reviewed_by_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('review_note', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('vendor_id', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['school.id']),
        sa.ForeignKeyConstraint(['submitted_by_user_id'], ['user.id']),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendor.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'idempotencyrecord',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('actor_id', sa.Uuid(), nullable=False),
        sa.Column('scope', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('idempotency_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('response_body', sa.JSON(), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['actor_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('actor_id', 'scope', 'idempotency_key', name='uq_idempotency_scope_key'),
    )


def downgrade() -> None:
    op.drop_table('idempotencyrecord')
    op.drop_table('vendorsubmission')
    op.drop_index(op.f('ix_invitation_token_hash'), table_name='invitation')
    op.drop_table('invitation')
    op.drop_table('favorite')
