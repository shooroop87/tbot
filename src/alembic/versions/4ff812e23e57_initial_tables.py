"""initial_tables

Revision ID: 4ff812e23e57
Revises: 
Create Date: 2026-01-02 18:14:30.307217

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4ff812e23e57'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def table_exists(table_name: str) -> bool:
    """Проверяет существование таблицы."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def column_exists(table_name: str, column_name: str) -> bool:
    """Проверяет существование колонки."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def index_exists(table_name: str, index_name: str) -> bool:
    """Проверяет существование индекса."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = [i['name'] for i in inspector.get_indexes(table_name)]
    return index_name in indexes


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════
    # Удаление старых таблиц (если существуют)
    # ═══════════════════════════════════════════════════════════
    if table_exists('daily_stats'):
        op.drop_table('daily_stats')
    
    if table_exists('daily_indicators'):
        if index_exists('daily_indicators', 'ix_daily_indicators_date_instrument'):
            op.drop_index('ix_daily_indicators_date_instrument', table_name='daily_indicators')
        op.drop_table('daily_indicators')

    # ═══════════════════════════════════════════════════════════
    # indicators_daily: добавление EMA колонок
    # ═══════════════════════════════════════════════════════════
    if table_exists('indicators_daily'):
        if not column_exists('indicators_daily', 'ema_13'):
            op.add_column('indicators_daily', sa.Column('ema_13', sa.Float(), nullable=True))
        if not column_exists('indicators_daily', 'ema_26'):
            op.add_column('indicators_daily', sa.Column('ema_26', sa.Float(), nullable=True))
        if not column_exists('indicators_daily', 'ema_trend'):
            op.add_column('indicators_daily', sa.Column('ema_trend', sa.String(length=10), nullable=True))
        if not column_exists('indicators_daily', 'ema_diff_pct'):
            op.add_column('indicators_daily', sa.Column('ema_diff_pct', sa.Float(), nullable=True))
        if not column_exists('indicators_daily', 'ema_13_slope'):
            op.add_column('indicators_daily', sa.Column('ema_13_slope', sa.Float(), nullable=True))
        if not column_exists('indicators_daily', 'ema_26_slope'):
            op.add_column('indicators_daily', sa.Column('ema_26_slope', sa.Float(), nullable=True))
        if not column_exists('indicators_daily', 'distance_to_ema_13_pct'):
            op.add_column('indicators_daily', sa.Column('distance_to_ema_13_pct', sa.Float(), nullable=True))
        if not column_exists('indicators_daily', 'distance_to_ema_26_pct'):
            op.add_column('indicators_daily', sa.Column('distance_to_ema_26_pct', sa.Float(), nullable=True))

    # ═══════════════════════════════════════════════════════════
    # instruments: добавление колонок
    # ═══════════════════════════════════════════════════════════
    if table_exists('instruments'):
        if not column_exists('instruments', 'exchange'):
            op.add_column('instruments', sa.Column('exchange', sa.String(length=50), nullable=True))
        if not column_exists('instruments', 'expiration_date'):
            op.add_column('instruments', sa.Column('expiration_date', sa.Date(), nullable=True))
        if not column_exists('instruments', 'basic_asset'):
            op.add_column('instruments', sa.Column('basic_asset', sa.String(length=20), nullable=True))
        if not column_exists('instruments', 'is_active'):
            op.add_column('instruments', sa.Column('is_active', sa.Boolean(), nullable=True))
        if not column_exists('instruments', 'avg_spread_pct'):
            op.add_column('instruments', sa.Column('avg_spread_pct', sa.Float(), nullable=True))
        if column_exists('instruments', 'spread_pct'):
            op.drop_column('instruments', 'spread_pct')

    # ═══════════════════════════════════════════════════════════
    # signals: добавление колонок и индексов
    # ═══════════════════════════════════════════════════════════
    if table_exists('signals'):
        if not column_exists('signals', 'signal_date'):
            op.add_column('signals', sa.Column('signal_date', sa.Date(), nullable=True))
        if not column_exists('signals', 'position_value'):
            op.add_column('signals', sa.Column('position_value', sa.Float(), nullable=True))
        if not column_exists('signals', 'max_loss'):
            op.add_column('signals', sa.Column('max_loss', sa.Float(), nullable=True))
        if not column_exists('signals', 'indicators_snapshot'):
            op.add_column('signals', sa.Column('indicators_snapshot', sa.JSON(), nullable=True))
        
        if not index_exists('signals', 'ix_signals_date'):
            op.create_index('ix_signals_date', 'signals', ['signal_date'], unique=False)
        if not index_exists('signals', 'ix_signals_instrument'):
            op.create_index('ix_signals_instrument', 'signals', ['instrument_id'], unique=False)

    # ═══════════════════════════════════════════════════════════
    # trades: добавление колонок
    # ═══════════════════════════════════════════════════════════
    if table_exists('trades'):
        if not column_exists('trades', 'signal_id'):
            op.add_column('trades', sa.Column('signal_id', sa.Integer(), nullable=True))
        if not column_exists('trades', 'entry_date'):
            op.add_column('trades', sa.Column('entry_date', sa.Date(), nullable=True))
        if not column_exists('trades', 'exit_date'):
            op.add_column('trades', sa.Column('exit_date', sa.Date(), nullable=True))


def downgrade() -> None:
    # Downgrade не поддерживается для этой миграции
    # Таблицы создаются через create_all() при первом запуске
    pass