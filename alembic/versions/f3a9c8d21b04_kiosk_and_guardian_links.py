"""kiosk_and_guardian_links

Adds the kiosk pivot's backend surface and the guardian-link-request flow
that closes the student-code enumeration hole:
docs/SPEC_KIOSK_AND_VERIFICATION.md, docs/BACKEND_HANDOVER.md §2.1a, §9.

Also makes `student.user_id` nullable and deactivates any pupil-role `user`
rows already on the server — pupils no longer sign in, so an enrolled pupil
is a `student` and a `wallet`, never a login (§2.1b).

Revision ID: f3a9c8d21b04
Revises: 9c96f0c7be5e
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'f3a9c8d21b04'
down_revision: Union[str, None] = '9c96f0c7be5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('student', 'user_id', existing_type=sa.Uuid(), nullable=True)

    # Pupil credentials from before the kiosk pivot: nobody signs in as a
    # pupil any more, so a still-active row is a live, unused authentication
    # surface. Deactivating rather than deleting keeps the audit trail intact
    # and the FK from `student.user_id` (for rows that still carry one) valid.
    op.execute("UPDATE \"user\" SET is_active = false WHERE role = 'student'")

    op.create_table(
        'guardianlinkrequest',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('parent_id', sa.Uuid(), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('school_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.Enum('pending', 'approved', 'rejected', name='guardianlinkstatus'), nullable=False),
        sa.Column('reviewed_by_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('reject_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['parent_id'], ['parent.id']),
        sa.ForeignKeyConstraint(['student_id'], ['student.id']),
        sa.ForeignKeyConstraint(['school_id'], ['school.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'guardianlinkattempt',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('parent_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['parent_id'], ['parent.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'kiosk',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('school_id', sa.Uuid(), nullable=False),
        sa.Column('label', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('status', sa.Enum('pending', 'active', 'revoked', name='kioskstatus'), nullable=False),
        sa.Column('pairing_code_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('pairing_code_expires_at', sa.DateTime(), nullable=True),
        sa.Column('device_token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('paired_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['school_id'], ['school.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_token_hash', name='uq_kiosk_device_token_hash'),
    )

    op.create_table(
        'kioskverification',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('kiosk_id', sa.Uuid(), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['kiosk_id'], ['kiosk.id']),
        sa.ForeignKeyConstraint(['student_id'], ['student.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_kioskverification_token_hash'), 'kioskverification', ['token_hash'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_kioskverification_token_hash'), table_name='kioskverification')
    op.drop_table('kioskverification')
    op.drop_table('kiosk')
    op.drop_table('guardianlinkattempt')
    op.drop_table('guardianlinkrequest')
    op.execute("UPDATE \"user\" SET is_active = true WHERE role = 'student'")
    op.alter_column('student', 'user_id', existing_type=sa.Uuid(), nullable=False)
    sa.Enum(name='guardianlinkstatus').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='kioskstatus').drop(op.get_bind(), checkfirst=True)
