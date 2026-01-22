"""add_bot_settings_and_tracked_orders

Revision ID: 5a1b2c3d4e5f
Revises: 4ff812e23e57
Create Date: 2026-01-20
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision: str = '5a1b2c3d4e5f'
down_revision: Union[str, None] = '4ff812e23e57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # === bot_settings ===
    if not table_exists('bot_settings'):
        op.create_table(
            'bot_settings',
            sa.Column('id', sa.Integer(), primary_key=True, default=1),
            sa.Column('is_active', sa.Boolean(), default=False),
            sa.Column('mode', sa.String(20), default='manual'),
            sa.Column('last_change_reason', sa.String(200)),
            sa.Column('last_change_by', sa.String(50)),
            sa.Column('last_change_at', sa.DateTime()),
            sa.Column('total_orders', sa.Integer(), default=0),
            sa.Column('total_sl_triggered', sa.Integer(), default=0),
            sa.Column('total_tp_triggered', sa.Integer(), default=0),
            sa.Column('total_pnl_rub', sa.Float(), default=0),
            sa.Column('updated_at', sa.DateTime()),
        )
        
        # Вставляем начальную запись (бот ВЫКЛЮЧЕН)
        op.execute(
            "INSERT INTO bot_settings (id, is_active, mode, last_change_reason) "
            "VALUES (1, false, 'manual', 'Initial setup')"
        )

    # === tracked_orders ===
    if not table_exists('tracked_orders'):
        op.create_table(
            'tracked_orders',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('order_id', sa.String(100), unique=True, nullable=False),
            sa.Column('ticker', sa.String(20), nullable=False),
            sa.Column('figi', sa.String(20), nullable=False),
            sa.Column('order_type', sa.String(20), nullable=False),
            sa.Column('quantity', sa.Integer(), nullable=False),
            sa.Column('entry_price', sa.Float(), nullable=False),
            sa.Column('stop_price', sa.Float(), nullable=False),
            sa.Column('target_price', sa.Float(), nullable=False),
            sa.Column('stop_offset', sa.Float(), default=0),
            sa.Column('take_offset', sa.Float(), default=0),
            sa.Column('lot_size', sa.Integer(), default=1),
            sa.Column('atr', sa.Float(), default=0),
            sa.Column('status', sa.String(20), default='pending'),
            sa.Column('is_executed', sa.Boolean(), default=False),
            sa.Column('executed_price', sa.Float()),
            sa.Column('executed_at', sa.DateTime()),
            sa.Column('parent_order_id', sa.String(100)),
            sa.Column('sl_order_id', sa.String(100)),
            sa.Column('tp_order_id', sa.String(100)),
            sa.Column('pnl_rub', sa.Float()),
            sa.Column('pnl_pct', sa.Float()),
            sa.Column('created_at', sa.DateTime()),
            sa.Column('updated_at', sa.DateTime()),
            sa.Column('created_by', sa.String(50)),
        )
        
        op.create_index('ix_tracked_orders_order_id', 'tracked_orders', ['order_id'])
        op.create_index('ix_tracked_orders_status', 'tracked_orders', ['status'])
        op.create_index('ix_tracked_orders_ticker', 'tracked_orders', ['ticker'])


def downgrade() -> None:
    op.drop_table('tracked_orders')
    op.drop_table('bot_settings')