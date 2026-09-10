"""simplify_order_status

Collapses the seven-state kitchen pipeline (pending/accepted/preparing/ready/
completed/rejected/cancelled) into three: paid (checkout, wallet already
debited), confirmed (vendor accepted it), cancelled (vendor declined it,
wallet refunded). There is no student-initiated cancel and no intermediate
kitchen-stage tracking any more — one vendor decision, one terminal outcome.

Existing rows are remapped rather than dropped: pending -> paid;
accepted/preparing/ready/completed -> confirmed (the vendor had already
acted, or the order finished, under the old model); rejected -> cancelled
(same refunded-and-declined outcome, one name now instead of two).

Revision ID: b2d84f61c937
Revises: a1c7e5f930d2
Create Date: 2026-09-07 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2d84f61c937'
down_revision: Union[str, None] = 'a1c7e5f930d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REMAP = {
    'pending': 'paid',
    'accepted': 'confirmed',
    'preparing': 'confirmed',
    'ready': 'confirmed',
    'completed': 'confirmed',
    'rejected': 'cancelled',
    'cancelled': 'cancelled',
}


def upgrade() -> None:
    # Postgres enums can't drop values in place, so the column has to leave
    # the enum type, get remapped as plain text, and come back under a freshly
    # built type with only the three values that survive.
    op.alter_column('order', 'status', existing_type=sa.Enum(name='orderstatus'), type_=sa.String(), postgresql_using='status::text')
    op.alter_column('orderevent', 'status', existing_type=sa.Enum(name='orderstatus'), type_=sa.String(), postgresql_using='status::text')

    for old, new in _REMAP.items():
        op.execute(f"UPDATE \"order\" SET status = '{new}' WHERE status = '{old}'")
        op.execute(f"UPDATE orderevent SET status = '{new}' WHERE status = '{old}'")

    op.execute('DROP TYPE orderstatus')
    new_status = sa.Enum('paid', 'confirmed', 'cancelled', name='orderstatus')
    new_status.create(op.get_bind())

    op.alter_column('order', 'status', existing_type=sa.String(), type_=new_status, postgresql_using='status::orderstatus', server_default='paid')
    op.alter_column('orderevent', 'status', existing_type=sa.String(), type_=new_status, postgresql_using='status::orderstatus')


def downgrade() -> None:
    op.alter_column('order', 'status', existing_type=sa.Enum(name='orderstatus'), type_=sa.String(), postgresql_using='status::text')
    op.alter_column('orderevent', 'status', existing_type=sa.Enum(name='orderstatus'), type_=sa.String(), postgresql_using='status::text')

    # The reverse remap is lossy by construction (three buckets can't recover
    # seven original states) — everything lands on the closest single old
    # value rather than pretending to restore history that no longer exists.
    op.execute("UPDATE \"order\" SET status = 'pending' WHERE status = 'paid'")
    op.execute("UPDATE \"order\" SET status = 'ready' WHERE status = 'confirmed'")
    op.execute("UPDATE \"order\" SET status = 'rejected' WHERE status = 'cancelled'")
    op.execute("UPDATE orderevent SET status = 'pending' WHERE status = 'paid'")
    op.execute("UPDATE orderevent SET status = 'ready' WHERE status = 'confirmed'")
    op.execute("UPDATE orderevent SET status = 'rejected' WHERE status = 'cancelled'")

    op.execute('DROP TYPE orderstatus')
    old_status = sa.Enum('pending', 'accepted', 'preparing', 'ready', 'completed', 'rejected', 'cancelled', name='orderstatus')
    old_status.create(op.get_bind())

    op.alter_column('order', 'status', existing_type=sa.String(), type_=old_status, postgresql_using='status::orderstatus', server_default='pending')
    op.alter_column('orderevent', 'status', existing_type=sa.String(), type_=old_status, postgresql_using='status::orderstatus')
