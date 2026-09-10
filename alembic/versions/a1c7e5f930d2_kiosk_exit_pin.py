"""kiosk_exit_pin

Adds the staff-exit PIN a kiosk checks device-side via `POST /kiosks/exit`
(SPEC_KIOSK_AND_VERIFICATION.md §2.1) so a paired device can be taken out of
service by whoever is standing next to it, without an admin's JWT.

Revision ID: a1c7e5f930d2
Revises: f3a9c8d21b04
Create Date: 2026-09-07 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a1c7e5f930d2'
down_revision: Union[str, None] = 'f3a9c8d21b04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('kiosk', sa.Column('exit_pin_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column('kiosk', 'exit_pin_hash')
